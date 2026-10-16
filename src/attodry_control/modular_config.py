"""Strict, hardware-free configuration for freely selected Combination modules.

Only selected module scan tables are expanded. Device tables remain owned by the
existing instrument parsers; the runtime bridge validates those before opening a
resource. Both original input and expanded points are available for provenance.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from itertools import product
from pathlib import Path
import tomllib

from .combination_plan import AxisPoint, CombinationPlan, MODULE_KEYS, ScanAxis
from .nkt_config import NKT_TABLES, OTHER_TABLES, NktError, integer, keys, number
from .safety import MagnetLimits
from .scan_grid import GridValue, expand_grid


NEW_TABLES = {"combination_scan", "optics", "optics_scan", "nanovoltmeter",
              "current_source_scan", "magnetic_scan", "smu_scan", "pem",
              "measurement", "environment", "current_source"}


@dataclass(frozen=True)
class ModularConfig:
    document: dict
    plan: CombinationPlan
    source_path: Path
    optics: dict | None = None

    @property
    def path(self):
        return self.source_path

    @property
    def selected_modules(self):
        return self.plan.order

    @property
    def expanded_scan(self):
        return self.plan.snapshot()

    def snapshot(self):
        return {"schema": "modular-combination-v1", "source_path": str(self.source_path),
                "original_config": deepcopy(self.document), "expanded_plan": self.plan.snapshot(),
                "optics": deepcopy(self.optics)}


def load_modular_config(path):
    source = Path(path).resolve()
    with source.open("rb") as stream:
        return parse_modular_config(tomllib.load(stream), source)


def is_modular_config(document):
    return "combination_scan" in document


def parse_modular_config(document, path="modular.toml"):
    if type(document) is not dict:
        raise NktError("Configuration document must be a table")
    keys(document, "top level", {"combination_scan"}, NKT_TABLES | OTHER_TABLES | NEW_TABLES)
    if "photoresponse_combination" in document:
        raise NktError("Do not combine combination_scan with legacy photoresponse_combination")
    settings = document["combination_scan"]
    keys(settings, "combination_scan", {"order"}, {"samples_per_condition", "repeats", "run_name", "note",
         "backend", "output_directory", "database_path"})
    if "backend" in settings and settings["backend"] not in {"simulation", "hardware"}:
        raise NktError("combination_scan.backend must be simulation or hardware")
    for field in ("output_directory", "database_path"):
        if field in settings and (type(settings[field]) is not str or not settings[field].strip()):
            raise NktError(f"combination_scan.{field} must be a nonempty path string")
    if "measurement" in document:
        measurement = document["measurement"]
        keys(measurement, "measurement", set(), {"max_abs_voltage_v", "max_abs_current_a",
             "sample_interval_s", "settle_s", "dark_recovery_s"})
        for field, value in measurement.items():
            number(value, "measurement." + field,
                   0 if field in {"settle_s", "dark_recovery_s"} else 1e-15, 1e6)
    raw_order = settings["order"]
    if type(raw_order) is not list or not raw_order or any(type(v) is not str for v in raw_order):
        raise NktError("combination_scan.order requires a nonempty module array")
    order = tuple("optics" if name == "optical" else name for name in raw_order)
    if len(order) != len(set(order)) or set(order) - MODULE_KEYS.keys():
        raise NktError("Combination requires distinct supported module names")
    optics = None
    axes = []
    for module in order:
        if module == "optics":
            optics, axis = _optics(document)
        elif module == "temperature":
            axis = _scalar_axis(document, "temperature_scan", "temperature", "points_k", "ranges_k", "temperature_k")
        elif module == "current_source":
            axis = _scalar_axis(document, "current_source_scan", module, "points_a", "ranges_a", "bias_current_a")
            bounds = document.get("measurement", {}).get("max_abs_current_a",
                     document.get("photoresponse", {}).get("max_abs_current_a"))
            if bounds is not None and any(abs(p.values["bias_current_a"]) > number(bounds, "max_abs_current_a", 1e-15, 1e6) for p in axis.points):
                raise NktError("Current source scan exceeds the configured sample current bound")
        elif module == "magnetic":
            axis = _magnetic(document)
        elif module == "smu":
            axis = _smu(document, path)
        else:
            # Settings are validated by the shared 2182A parser, rather than
            # duplicated here. No detector configuration is needed when omitted.
            detector = document.get("nanovoltmeter", {})
            if type(detector) is not dict:
                raise NktError("nanovoltmeter must be a table")
            axes.append(ScanAxis(module, (AxisPoint({}, metadata={"settings": deepcopy(detector)}),)))
            continue
        axes.append(axis)
    limits = MagnetLimits()
    if "magnetic" in order and "magnet" in document:
        from .config import _parse_magnet
        limits = _parse_magnet(document["magnet"]).limits
    cryostat = document.get("cryostat", {}) if "temperature" in order else {}
    bounds = None
    if "temperature_min_k" in cryostat or "temperature_max_k" in cryostat:
        bounds = (number(cryostat.get("temperature_min_k"), "temperature_min_k", 1e-9, 1e6),
                  number(cryostat.get("temperature_max_k"), "temperature_max_k", 1e-9, 1e6))
        if bounds[0] >= bounds[1]:
            raise NktError("Invalid cryostat temperature bounds")
    step = document.get("temperature_scan", {}).get("max_step_k") if "temperature" in order else None
    if step is None and "temperature" in order:
        step = document.get("environment", {}).get("max_temperature_step_k")
    if step is None and "temperature" in order:
        step = document.get("photoresponse_environment", {}).get("max_temperature_step_k")
    if step is None and "temperature" in order:
        step = document.get("temperature_run", {}).get("max_delta_k")
    plan = CombinationPlan(tuple(axes), settings.get("samples_per_condition", 1),
        settings.get("repeats", 1), settings.get("run_name", "combination"), settings.get("note", ""),
        limits, bounds, step)
    plan.validate()
    return ModularConfig(deepcopy(document), plan, Path(path).resolve(), optics)


def _scalar_axis(doc, table_name, module, points_key, ranges_key, coordinate):
    table = doc.get(table_name, {})
    optional = {"max_step_k"} if module == "temperature" else set()
    keys(table, table_name, set(), {points_key, ranges_key} | optional)
    values = expand_grid(table, points_key, ranges_key, table_name)
    return ScanAxis(module, tuple(AxisPoint({coordinate: value.value}, value.segment, value.direction,
                         {"segment_index": value.segment_index, "point_index": value.point_index}) for value in values))


def _product_rows(dimensions, pattern="cartesian"):
    if pattern not in {"cartesian", "paired"}:
        raise NktError("Scan pattern must be cartesian or paired")
    if pattern == "cartesian":
        return product(*(tuple(enumerate(values)) for values in dimensions.values()))
    count = max((len(values) for values in dimensions.values()), default=1)
    if any(len(values) not in (1, count) for values in dimensions.values()):
        raise NktError("Paired scan arrays must share a length or contain one fixed value")
    return (tuple((0 if len(values) == 1 else i, values[0 if len(values) == 1 else i])
                  for values in dimensions.values()) for i in range(count))


def _magnetic(doc):
    table = doc.get("magnetic_scan", {})
    keys(table, "magnetic_scan", set(), {"points", "bx_points_t", "bx_ranges_t", "bz_points_t", "bz_ranges_t", "pattern"})
    if "points" in table:
        if set(table) != {"points"} or type(table["points"]) is not list or not 1 <= len(table["points"]) <= 10000:
            raise NktError("magnetic_scan.points requires bounded explicit vectors, without independent axes")
        points = []
        for i, point in enumerate(table["points"]):
            keys(point, "magnetic point", {"bx_t", "bz_t"})
            points.append(AxisPoint({"field_x_t": point["bx_t"], "field_z_t": point["bz_t"]}, metadata={"point_index": i}))
        return ScanAxis("magnetic", tuple(points))
    dims = {"field_x_t": expand_grid(table, "bx_points_t", "bx_ranges_t", "magnetic_scan"),
            "field_z_t": expand_grid(table, "bz_points_t", "bz_ranges_t", "magnetic_scan")}
    return _coordinate_axis("magnetic", dims, table.get("pattern", "cartesian"))


def _smu(doc, path):
    if "smu_scan" not in doc:
        return _existing_smu_axis(doc, path)
    table = doc.get("smu_scan", {})
    allowed = {"points", "pattern"}
    for coordinate in MODULE_KEYS["smu"]:
        stem, unit = coordinate.rsplit("_", 1)
        allowed.update({stem+"_points_"+unit, stem+"_ranges_"+unit})
    keys(table, "smu_scan", set(), allowed)
    if "points" in table:
        if set(table) != {"points"} or type(table["points"]) is not list or not 1 <= len(table["points"]) <= 10000:
            raise NktError("smu_scan.points requires bounded paired coordinate tables only")
        return ScanAxis("smu", tuple(AxisPoint(deepcopy(point), metadata={"point_index": i}) for i, point in enumerate(table["points"])))
    dims = {}
    for coordinate in sorted(MODULE_KEYS["smu"]):
        stem, unit = coordinate.rsplit("_", 1)
        p, r = stem+"_points_"+unit, stem+"_ranges_"+unit
        if p in table or r in table:
            dims[coordinate] = expand_grid(table, p, r, "smu_scan")
    if not dims:
        raise NktError("SMU scan requires at least one selected coordinate")
    return _coordinate_axis("smu", dims, table.get("pattern", "cartesian"))


def _existing_smu_axis(doc, path):
    """Reuse the pinned module's own grid when no Combination override exists."""
    from .photonic_smu import SOURCE_COMMIT
    from .photonic_smu.three_smu_config import (
        SEMANTIC_ROLES, SourceMode, load_three_smu_operation_config, validate_plan_targets,
    )
    try:
        with Path(path).open("rb") as stream:
            source_doc = tomllib.load(stream)
    except (OSError, ValueError) as exc:
        raise NktError("Reusing the Three-SMU plan requires its unchanged source TOML") from exc
    for table in ("three_smu_run", *SEMANTIC_ROLES):
        if source_doc.get(table) != doc.get(table):
            raise NktError("SMU source TOML differs from the parsed module configuration")
    try:
        operation = load_three_smu_operation_config(path)
        points = validate_plan_targets(operation.hardware, operation.plan)
    except ValueError as exc:
        raise NktError(str(exc)) from exc
    expanded = []
    for point in points:
        values = {}
        for role, value in point.coordinates.items():
            hardware = operation.hardware.require_role(role)
            suffix = "v" if hardware.source_mode is SourceMode.VOLTAGE else "a"
            values[role + "_" + suffix] = value
        expanded.append(AxisPoint(values, segment=point.segment, metadata={
            "point_index": point.index, "scan_source": "three_smu_run",
            "source_commit": SOURCE_COMMIT}))
    return ScanAxis("smu", tuple(expanded))


def _coordinate_axis(module, dimensions, pattern):
    points = []
    for entries in _product_rows(dimensions, pattern):
        if len(points) >= 10000:
            raise NktError(f"{module} grid exceeds 10000 points")
        points.append(AxisPoint({key: value.value for key, (_, value) in zip(dimensions, entries)},
            metadata={"dimension_indices": {key: index for key, (index, _) in zip(dimensions, entries)}}))
    return ScanAxis(module, tuple(points))


def _optics(doc):
    table = doc.get("optics", {})
    keys(table, "optics", {"laser", "use_pem", "use_power_meter", "mode"},
         {"initial_source_level_pct", "initial_current_a", "peak_retardance_waves"})
    for field in ("use_pem", "use_power_meter"):
        if type(table[field]) is not bool:
            raise NktError(f"optics.{field} must be boolean")
    laser = table["laser"]
    if laser not in {"nkt", "cld1010lp"} or table["mode"] not in {"direct", "power_stabilized"}:
        raise NktError("Unsupported optics laser or mode")
    if table["mode"] == "power_stabilized" and not table["use_power_meter"]:
        raise NktError("Power-stabilized optics requires a power meter")
    if table["mode"] == "direct" and table["use_power_meter"]:
        raise NktError("A selected power meter requires power_stabilized mode")
    if table["use_pem"]:
        from .pem_config import load_pem_config
        if load_pem_config(doc) is None:
            raise NktError("optics.use_pem requires a configured pem table")
        number(table.get("peak_retardance_waves"), "optics.peak_retardance_waves", 1e-9, 100)
    elif "peak_retardance_waves" in table:
        raise NktError("peak_retardance_waves requires optics.use_pem")
    scan = doc.get("optics_scan", {})
    dimensions_spec = {"wavelength": ("wavelength_points_nm", "wavelength_ranges_nm", "optical_wavelength_nm"),
                       "bandwidth": ("bandwidth_points_nm", "bandwidth_ranges_nm", "optical_bandwidth_nm"),
                       "power": ("power_points_w", "power_ranges_w", "optical_target_power_w"),
                       "source_level": ("source_level_points_pct", "source_level_ranges_pct", "optical_source_level_pct"),
                       "current": ("current_points_a", "current_ranges_a", "optical_current_a")}
    allowed = {"order", "pattern", "dark_first"}
    for p, r, _ in dimensions_spec.values():
        allowed.update({p, r})
    keys(scan, "optics_scan", set(), allowed)
    if "dark_first" in scan and type(scan["dark_first"]) is not bool:
        raise NktError("optics_scan.dark_first must be boolean")
    dark_first = scan.get("dark_first", False)
    dimensions = {}
    for name, (p, r, _) in dimensions_spec.items():
        if p in scan or r in scan:
            dimensions[name] = expand_grid(scan, p, r, "optics_scan")
    # VARIA edges use a 0.1 nm grid. Generated wavelength ranges explicitly
    # resolve to that device grid and preserve the ideal values in metadata;
    # explicit lists are left exact for the shared device validator to check.
    ideal_wavelengths = None
    if laser == "nkt" and "wavelength_ranges_nm" in scan and doc.get("nkt_run", {}).get("mode", "varia_bandpass") == "varia_bandpass":
        ideal_wavelengths = tuple(value.value for value in dimensions["wavelength"])
        dimensions["wavelength"] = tuple(GridValue(round(value.value*10)/10,
            value.segment, value.direction, value.segment_index, value.point_index)
            for value in dimensions["wavelength"])
    first = (doc.get("nkt_run", {}).get("points") or [{}])[0] if laser == "nkt" else {}
    if laser == "nkt":
        for name, field in (("wavelength", "wavelength_nm"), ("bandwidth", "bandwidth_nm")):
            if name not in dimensions and first.get(field) is not None:
                dimensions[name] = (GridValue(number(first[field], field, 1e-15, 1e9)),)
        seed = table.get("initial_source_level_pct", first.get("source_level_pct"))
        if "current" in dimensions or "initial_current_a" in table:
            raise NktError("NKT optics does not accept diode current coordinates")
        seed_key = "optical_source_level_pct"
        if seed is not None:
            seed = number(seed, "initial_source_level_pct", 0, doc.get("nkt_source", {}).get("max_level_pct", 100))
    else:
        cld = doc.get("cld1010", {})
        if "bandwidth" in dimensions or "source_level" in dimensions or "initial_source_level_pct" in table:
            raise NktError("CLD has fixed wavelength and no programmable bandwidth/percentage setting")
        configured_wave = cld.get("wavelength_nm")
        if "wavelength" not in dimensions and configured_wave is not None:
            dimensions["wavelength"] = (GridValue(number(configured_wave, "wavelength_nm", 100, 10000)),)
        if "wavelength" not in dimensions or len(dimensions["wavelength"]) != 1:
            raise NktError("CLD optics requires exactly one nominal wavelength")
        if configured_wave is not None and dimensions["wavelength"][0].value != configured_wave:
            raise NktError("CLD scan wavelength must match its fixed nominal diode wavelength")
        currents = cld.get("current_points_a") or []
        seed = table.get("initial_current_a", currents[0] if len(currents) == 1 else None)
        seed_key = "optical_current_a"
        if seed is not None:
            seed = number(seed, "initial_current_a", 0, cld.get("max_current_a", 1))
    if table["mode"] == "power_stabilized":
        if "power" not in dimensions:
            raise NktError("Power-stabilized optics requires power_points_w or power_ranges_w")
        if "source_level" in dimensions or "current" in dimensions:
            raise NktError("Feedback controls the actuator; configure one initial actuator value")
        if seed is None:
            raise NktError("Configure an explicit initial optical actuator value")
    elif "power" in dimensions:
        raise NktError("Requested W targets require power_stabilized mode; direct mode scans actuator settings")
    if any(value.value <= 0 for value in dimensions.get("power", ())):
        raise NktError("Bright target power must be positive; use dark_first for the dark point")
    if seed is None and not ({"source_level", "current"} & dimensions.keys()):
        raise NktError("Optics requires an explicit actuator setting or actuator scan")
    intensity = "power" if "power" in dimensions else ("source_level" if "source_level" in dimensions
                 else "current" if "current" in dimensions else None)
    default_order = list(dimensions)
    if dark_first and intensity is not None:
        default_order = [name for name in default_order if name != intensity] + [intensity]
    order = scan.get("order", default_order)
    if type(order) is not list or len(order) != len(set(order)) or any(name not in dimensions for name in order):
        raise NktError("optics_scan.order must name distinct configured dimensions")
    missing = [name for name in dimensions if name not in order]
    if any(len(dimensions[name]) > 1 for name in missing):
        raise NktError("Every multipoint optics dimension must appear in optics_scan.order")
    order = order + missing
    if intensity is not None and dark_first and order[-1] != intensity:
        raise NktError("Dark-first optics requires power/actuator as the innermost optical dimension")
    # Optical groups contain the wavelength/bandwidth/actuator coordinates held
    # throughout this power visit. A duplicate numeric group remains a new visit.
    group_dimensions = {name: dimensions[name] for name in order if name != intensity}
    points = []
    def append_point(group, indices, values, intensity_value, power_index, dark):
        if len(points) >= 10000:
            raise NktError("Optics grid exceeds 10000 points")
        requested = dict(values)
        if seed is not None:
            requested.setdefault(seed_key, seed)
        if intensity is not None:
            coordinate = dimensions_spec[intensity][2]
            # A direct dark row records its actuator coordinate for matching;
            # emission-off is represented only by optical_state, not inferred
            # from any zero setpoint. A W dark placeholder is never measured W.
            if intensity == "power":
                requested[coordinate] = 0.0 if dark else intensity_value.value
            else:
                requested[coordinate] = intensity_value.value
        for coordinate, field in (("optical_nd_pct", "nd_pct"),
                                  ("optical_pulse_picker_ratio", "pulse_picker_ratio")):
            if first.get(field) is not None:
                requested[coordinate] = first[field]
        metadata = {"optical_state": "dark" if dark else "light", "optical_group_index": group,
                    "power_index": power_index, "dimension_indices": dict(indices),
                    "laser": laser, "power_stabilized": table["mode"] == "power_stabilized"}
        if isinstance(intensity_value, GridValue):
            metadata["power_segment_index"] = intensity_value.segment_index
            metadata["power_point_index"] = intensity_value.point_index
        if ideal_wavelengths is not None:
            metadata["ideal_wavelength_nm"] = ideal_wavelengths[indices["wavelength"]]
            metadata["wavelength_resolution_nm"] = .1
        points.append(AxisPoint(requested, metadata=metadata))

    if not dark_first:
        # Respect the literal user order, including power outer to wavelength.
        # Optical groups still identify identical non-intensity input indices.
        ordered = {name: dimensions[name] for name in order}
        groups = {}
        for entries in _product_rows(ordered, scan.get("pattern", "cartesian")):
            indices = {name: index for name, (index, _) in zip(ordered, entries)}
            identity = tuple((name, indices[name]) for name in group_dimensions)
            group = groups.setdefault(identity, len(groups))
            values = {dimensions_spec[name][2]: value.value for name, (_, value) in zip(ordered, entries) if name != intensity}
            intensity_value = entries[list(ordered).index(intensity)][1] if intensity else None
            append_point(group, indices, values, intensity_value, indices.get(intensity, 0), False)
        return deepcopy(table), ScanAxis("optics", tuple(points))

    for group, entries in enumerate(_product_rows(group_dimensions, scan.get("pattern", "cartesian"))):
        values = {dimensions_spec[name][2]: value.value for name, (_, value) in zip(group_dimensions, entries)}
        indices = {name: index for name, (index, _) in zip(group_dimensions, entries)}
        intensity_values = dimensions[intensity] if intensity is not None else (None,)
        append_point(group, indices, values, intensity_values[0], 0, True)
        for power_index, power in enumerate(intensity_values, 1):
            append_point(group, {**indices, **({intensity: power_index-1} if intensity else {})},
                         values, power, power_index, False)
    return deepcopy(table), ScanAxis("optics", tuple(points))
