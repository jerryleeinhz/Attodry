"""Gated module point execution; no I/O until explicit run authorization."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from enum import Enum
import hashlib
import math
import re
from pathlib import Path
from types import SimpleNamespace

from . import config as configuration
from .combination_scan import AxisPoint, ScanAxis, CombinationPlan, _audit_value, _run_combination
from .combination_store import utc_now
from .lockin_points import LockinPointSession
from .models import LockinRole
from .config import RunMode
from .three_smu import ThreeSmuSession
from .three_smu_config import load_three_smu_operation_config, validate_plan_targets, SourceMode, ScanMode, active_smu_roles
from .cryostat_points import CryostatPointSession
from .models import VectorField
from .safety import MagnetLimits, plan_ordered_field_transitions
from .photonics_lockin_config import PHOTONICS_REFERENCE_TOPOLOGIES
from .reference_transients import ReferenceTransientError
from .optical_recovery import OpticalPowerDeviation, OpticalTargetUnreachable


def _window_error(errors):
    """A recoverable power/reference observation cannot hide any other fault."""
    hard = [error for error in errors if not isinstance(error, (ReferenceTransientError, OpticalPowerDeviation))]
    primary = hard[0] if hard else errors[0]
    if not hard and len({type(error) for error in errors}) > 1:
        primary = ValueError("Mixed optical/reference faults require a stopped, reviewed run")
    for error in errors:
        if error is not primary:
            primary.add_note(f"Additional window failure: {type(error).__name__}: {error}")
    return primary


def _json(value):
    if isinstance(value, dict):
        return {k: _json(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(v) for v in value]
    if isinstance(value, (Path, datetime)):
        return str(value) if isinstance(value, Path) else value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return _audit_value(value)


@dataclass(frozen=True)
class HardwareCombinationConfig:
    path: Path
    plan: CombinationPlan
    smu: object | None
    lockin: object | None
    snapshot: dict
    temperature: object | None = None
    magnetic: object | None = None
    cryostat: object | None = None
    database_path: Path | None = None
    run_id: str = "auto"
    lockin_mode: str = "excitation"
    optical: object | None = None
    reference_topology: str = "internal_xx_xy"
    illumination_policy: str = "off_before_axis_change"
    rotator: object | None = None
    lockin_range_schedule: object | None = None
    positioner: object | None = None


def load_hardware_combination(path: str | Path) -> HardwareCombinationConfig:
    """Reuse standalone grids/limits; do not parse or access inactive modules."""
    path = Path(path).resolve()
    document = configuration._load_document(path)
    known = {"project", "cryostat", "magnet", "temperature_stability", "temperature_run",
             "temperature_scan", "temperature_excitation_scan", "magnetic_field_run",
             "cleanup", "visa", "lockin_xx", "lockin_xy", "lockin_aux", "lockin_sweep",
             "smu_bias", "gate_top", "gate_bottom", "three_smu_run", "combination_scan",
             "rotator_1", "rotator_2", "rotator_scan"}
    known |= configuration.OPTICAL_CONFIG_TABLES
    if set(document) - known:
        raise ValueError("Unknown top-level combination configuration table")
    project = configuration._parse_project(configuration._table(document, "project"))
    if project.mode is not RunMode.HARDWARE:
        raise ValueError("Hardware combination requires project.mode='hardware'")
    table = configuration._table(document, "combination_scan")
    configuration._strict_keys_with_optional(
        table, "combination_scan",
        {"backend", "order", "samples_per_condition", "repeats", "run_name", "note"},
        {"lockin_mode", "run_id", "reference_topology", "illumination_policy", "unreachable_target_policy",
         "temperature_deviation_policy", "magnetic_deviation_policy", "lockin_range_rules"})
    temperature_policy = table.get("temperature_deviation_policy", "abort")
    if temperature_policy not in ("abort", "record_continue"):
        raise ValueError("combination_scan.temperature_deviation_policy must be abort or record_continue")
    magnetic_policy = table.get("magnetic_deviation_policy", "abort")
    if magnetic_policy not in ("abort", "record_continue"):
        raise ValueError("combination_scan.magnetic_deviation_policy must be abort or record_continue")
    illumination = table.get("illumination_policy", "off_before_axis_change")
    if illumination not in ("off_before_axis_change", "continuous_gate_scan"):
        raise ValueError("Unsupported combination_scan.illumination_policy")
    topology = table.get("reference_topology", "internal_xx_xy")
    if topology not in {"internal_xx_xy", *PHOTONICS_REFERENCE_TOPOLOGIES}:
        raise ValueError("Unsupported combination reference_topology")
    photonics = topology in PHOTONICS_REFERENCE_TOPOLOGIES
    mode = table.get("lockin_mode", "excitation")
    if not isinstance(mode, str) or mode not in {"excitation", "frequency", "frequency_excitation"}:
        raise ValueError("combination_scan.lockin_mode must be excitation, frequency or frequency_excitation")
    if photonics and mode != "excitation":
        raise ValueError("PEM owns the reference frequency; internal frequency scans are unsupported")
    run_id = table.get("run_id", "auto")
    if not isinstance(run_id, str) or not run_id.strip() or run_id != run_id.strip():
        raise ValueError("combination_scan.run_id must be a nonempty string without outer whitespace")
    if table["backend"] != "hardware":
        raise ValueError("Hardware combination requires backend='hardware'")
    order = table["order"]
    if (not isinstance(order, list) or not order
            or not all(isinstance(m, str) and m in {"temperature", "magnetic", "smu", "lockin", "optical", "rotator", "positioner"} for m in order)
            or len(set(order)) != len(order)):
        raise ValueError("Hardware order requires distinct supported module axes")
    if "positioner" in order:
        pivot = order.index("positioner")
        if (any(m not in {"temperature", "magnetic"} for m in order[:pivot])
                or any(m in {"temperature", "magnetic"} for m in order[pivot + 1:])
                or illumination != "off_before_axis_change"):
            raise ValueError("Positioner requires outer temperature/magnetic, inner optical/SMU/lockin and OFF before motion")
    rotation_order = [m for m in order if m != "positioner"]
    if "rotator" in order and (rotation_order not in (["rotator", "optical", "lockin"],
            ["rotator", "optical", "smu", "lockin"]) or illumination != "off_before_axis_change"):
        raise ValueError("Waveplate rotator requires rotator -> optical -> optional smu -> lockin and OFF before axis changes")
    if temperature_policy != "abort" and "temperature" not in order:
        raise ValueError("temperature_deviation_policy requires the temperature axis")
    if magnetic_policy != "abort" and "magnetic" not in order:
        raise ValueError("magnetic_deviation_policy requires the magnetic axis")
    if photonics and "lockin" not in order:
        raise ValueError("PEM reference topologies require the lockin axis")
    aux_table = document.get("lockin_aux", {})
    if not isinstance(aux_table, dict) or (aux_table and type(aux_table.get("enabled")) is not bool):
        raise ValueError("lockin_aux requires explicit boolean enabled")
    if aux_table.get("enabled") and (topology != "internal_xx_xy" or "lockin" not in order):
        raise ValueError("The third channel requires the internal XX reference and lockin axis")
    axes = {}
    smu = lockin = None
    hardware = {}
    temperature = magnetic = cryostat = None
    optical = None
    rotator = None
    positioner = None
    lockin_limits = None
    if "positioner" in order:
        from .positioner_config import load_positioner_config
        positioner = load_positioner_config(document)
        if positioner is None:
            raise ValueError("The positioner axis requires enabled positioner configuration")
        if "optical" not in order and not positioner.external_light_off_confirmed:
            raise ValueError("Positioner without optical ownership requires external_light_off_confirmed=true")
        axes["positioner"] = ScanAxis("positioner", tuple(AxisPoint(dict(p), "main", "ordered",
            {"positioner_point_index": index}) for index, p in enumerate(positioner.points)))
        hardware["positioner"] = positioner.snapshot()
    if "rotator" in order:
        from .rotator_config import load_rotator_config
        rotator = load_rotator_config(document)
        if rotator is None or not rotator.devices:
            raise ValueError("The rotator axis requires at least one enabled waveplate rotator")
        axes["rotator"] = ScanAxis("rotator", tuple(AxisPoint(dict(p)) for p in rotator.points))
        hardware["rotator"] = {device.role: _json(asdict(device)) for device in rotator.devices}
    if "temperature" in order:
        temperature = configuration.load_temperature_operation_config(path)
        points = (temperature.temperature_scan.points_k if temperature.temperature_scan is not None
                  else (temperature.temperature_run.target_k,))
        # An inner axis returns from its last point to its first on each outer
        # iteration. Include that reset (and repeats), not just ascending steps.
        transitions = list(zip(points, points[1:]))
        if order.index("temperature") > 0 or table["repeats"] != 1:
            transitions.append((points[-1], points[0]))
        if any(abs(b - a) > temperature.temperature_run.max_delta_k for a, b in transitions):
            raise ValueError("Temperature step/reset exceeds temperature_run.max_delta_k")
        axes["temperature"] = ScanAxis("temperature", tuple(
            AxisPoint({"temperature_k": v}, "main", "ordered") for v in points))
        hardware["temperature"] = asdict(temperature)
        hardware["temperature"].update(effective_interrupt_policy="abort",
            qualification="fresh_dwell_per_axis_entry", formal_temperature_policy=temperature_policy,
            pre_measure_wait_s_used=False)
    if "magnetic" in order:
        magnetic = configuration.load_magnetic_field_operation_config(path, command_scoped=True)
        segments = magnetic.run.segment_plan
        axes["magnetic"] = ScanAxis("magnetic", tuple(AxisPoint(
            {"field_x_t": point.bx_t, "field_z_t": point.bz_t},
            str(segments.point_segment_indices[i]) if segments else "main",
            segments.segments[segments.point_segment_indices[i]].direction if segments else "ordered")
            for i, point in enumerate(magnetic.run.points)))
        hardware["magnetic"] = asdict(magnetic)
    if temperature is not None or magnetic is not None:
        selected = temperature or magnetic
        limits = selected.magnet.limits
        # A selected magnetic plan supplies its complete-plan axis policy.
        # Temperature-only operation keeps the existing ambient 3 T envelope.
        effective = (limits if magnetic is not None else
                     replace(limits, hardware_z_max_t=min(limits.hardware_z_max_t, 3.0)))
        command_scoped_field = magnetic is not None
        if (photonics or "optical" in order) and not command_scoped_field:
            effective = replace(effective,
                hardware_x_max_t=min(effective.hardware_x_max_t, effective.experiment_vector_max_t, 3.0),
                hardware_z_max_t=min(effective.hardware_z_max_t, effective.experiment_vector_max_t, 3.0))
            if magnetic is not None:
                magnetic = replace(magnetic, magnet=replace(magnetic.magnet, limits=effective))
                hardware["magnetic"] = asdict(magnetic)
        cryostat = SimpleNamespace(project=project, cryostat=selected.cryostat,
            magnet=replace(selected.magnet, limits=effective),
            temperature_stability=(temperature.temperature_stability if temperature is not None
                                   else selected.magnet.stability),
            strict_resultant=(photonics or "optical" in order) and not command_scoped_field,
            command_scoped_field=command_scoped_field,
            temperature_deviation_policy=temperature_policy,
            magnetic_deviation_policy=magnetic_policy)
        hardware["effective_cryostat"] = {name: asdict(value) if hasattr(value, "__dataclass_fields__") else value
                                         for name, value in vars(cryostat).items()}
        if magnetic is not None:
            # Offline check exact float32 endpoints and every component corner
            # under the configured nominal limits; the parser already checked
            # complete-plan single-axis/vector classification.
            plan_ordered_field_transitions(VectorField(0.0, 0.0), magnetic.run.points,
                magnetic.run.max_step_t, magnetic.run.transition_policy, effective)
    if "smu" in order:
        smu = load_three_smu_operation_config(path)
        if smu.plan.mode is ScanMode.SOFTWARE_PULSE:
            raise ValueError("Software-pulse timing is not supported by combination scans")
        points = validate_plan_targets(smu.hardware, smu.plan)
        def values(point):
            return {role + ("_v" if smu.hardware.require_role(role).source_mode is SourceMode.VOLTAGE
                            else "_a"): value for role, value in point.coordinates.items()}
        axes["smu"] = ScanAxis("smu", tuple(
            AxisPoint(values(point), point.segment, "ordered") for point in points))
        hardware["smu"] = {"hardware": asdict(smu.hardware), "plan": asdict(smu.plan),
                           "effective_finish_action": "zero_disable",
                           "sampling_owner": "combination_scan.samples_per_condition"}
    if "lockin" in order and photonics:
        from .photonics_lockin_config import load_photonics_lockin_config
        from .photonics_lockin_points import PhotonicsLockinPointSession
        lockin = load_photonics_lockin_config(path, document=document)
        prepared = PhotonicsLockinPointSession(path, lockin, lambda *_: None, mode=mode)
        axes["lockin"] = ScanAxis("lockin", tuple(AxisPoint({
            "lockin_excitation_v_rms": point.source_v_rms, "lockin_frequency_hz": point.frequency_hz},
            "main", "ordered", point.metadata()) for point in prepared.grid))
        lockin_limits = (lockin.minimum_source_voltage_v, lockin.maximum_source_voltage_v,
                         lockin.reference_min_hz, lockin.reference_max_hz)
        hardware["lockin"] = _json(asdict(lockin))
        hardware["lockin"].update(point_mode=mode, harmonics_by_role=prepared.harmonics_by_role,
                                  skipped_harmonics_by_frequency={})
    elif "lockin" in order:
        safety_path = path.with_name("lockin_safety.toml")
        safety_document = configuration._load_document(safety_path)
        safety = configuration._parse_lockin_safety({k: v for k, v in safety_document.items() if k != "lockin_aux"})
        xx = configuration._parse_lockin(configuration._table(document, "lockin_xx"),
                                        LockinRole.XX, "lockin_xx", safety.lockin_xx, safety,
                                        allow_disconnected_source="optical" in order)
        xy = configuration._parse_lockin(configuration._table(document, "lockin_xy"),
                                        LockinRole.XY, "lockin_xy", safety.lockin_xy, safety)
        configuration._validate_lockin_pair(xx, xy)
        sweep = configuration._parse_lockin_sweep(configuration._table(document, "lockin_sweep"),
                                                  safety, xx, xy)
        lockin = SimpleNamespace(
            lockin_xx=xx, lockin_xy=xy, lockin_safety=safety, lockin_sweep=sweep,
            visa=configuration._parse_visa(configuration._table(document, "visa")))
        from .internal_lockin_group import load_auxiliary_config, InternalLockinGroupSession
        lockin.lockin_aux = load_auxiliary_config(document, safety_document, xx.frequency_hz)
        session_type = (InternalLockinGroupSession if lockin.lockin_aux is not None
                        or "lockin_range_rules" in table else LockinPointSession)
        prepared = session_type(path, lockin, lambda *_: None, mode=mode)
        axes["lockin"] = ScanAxis("lockin", tuple(AxisPoint({
            "lockin_excitation_v_rms": point.source_v_rms, "lockin_frequency_hz": point.frequency_hz},
            (f"f{point.frequency_segment}:e{point.excitation_segment}" if mode != "excitation"
             else str(point.excitation_segment) if point.excitation_segment is not None else "main"),
            "ascending", point.metadata()) for point in prepared.grid))
        hardware["lockin"] = {name: asdict(value) if value is not None else None for name, value in vars(lockin).items()}
        hardware["lockin"]["safety_sha256"] = hashlib.sha256(safety_path.read_bytes()).hexdigest()
        hardware["lockin"]["point_mode"] = mode
        hardware["lockin"]["harmonics_by_role"] = prepared.harmonics_by_role
        hardware["lockin"]["skipped_harmonics_by_frequency"] = prepared.skipped_by_frequency
        hardware["lockin"]["sample_excitation_connected"] = xx.sine_output_connected
        hardware["lockin"]["excitation_definition"] = (
            "instrument_sine_rms_setting_at_sample_route" if xx.sine_output_connected
            else "instrument_setting_only_sample_physically_disconnected")
    if "optical" in order:
        from .optical_points import load_optical_point_config
        optical = load_optical_point_config(path)
        if optical.background.enabled and (lockin is None or photonics):
            raise ValueError("Matched complex backgrounds require the internal-XX dual SR830 path")
        if (rotator is not None or positioner is not None) and optical.background.enabled and optical.background.capture_mode != "per_condition":
            raise ValueError("Position/waveplate scans require per_condition dark backgrounds for matched visits")
        if photonics and not optical.scan.use_pem:
            raise ValueError("PEM reference optical scans require PEM enabled")
        if not photonics and lockin is not None and optical.scan.use_pem:
            raise ValueError("An internal-XX optical scan must disable PEM; select a PEM topology to use its reference")
        if optical.chopper is not None:
            if photonics or lockin is None:
                raise ValueError("The chopper combination requires the internal-XX lock-in reference topology")
            for point in prepared.grid:
                modulation = optical.chopper.validate_reference_frequency(point.frequency_hz)
                if optical.chopper.reference_source != "external":
                    raise ValueError("XX-synchronized chopping requires external reference")
                for harmonics in prepared.harmonics_by_role.values():
                    for harmonic in harmonics:
                        ratio = harmonic * point.frequency_hz / modulation
                        if ratio < 1 or not math.isclose(ratio, round(ratio), rel_tol=0, abs_tol=1e-9):
                            raise ValueError("Lock-in detection must be an integer harmonic of the selected optical chopping frequency")
        axes["optical"] = ScanAxis("optical", tuple(AxisPoint(values, "main", "ordered",
            {"optical_point_index": index}) for index, values in enumerate(optical.points)))
        hardware["optical"] = _json(asdict(optical))
    if illumination == "continuous_gate_scan":
        if order != ["optical", "smu", "lockin"] or mode != "excitation":
            raise ValueError("continuous_gate_scan requires optical -> smu -> lockin with fixed excitation")
        if len(axes["lockin"].points) != 1:
            raise ValueError("continuous_gate_scan requires one fixed lock-in excitation point")
        if not optical.nkt.emit or optical.scan.mode != "power_stabilized" or optical.pm is None:
            raise ValueError("continuous_gate_scan requires illuminated, power-stabilized PM acquisition")
        active_gates = smu.hardware.by_role()
        if not active_gates or any(role not in {"gate_top", "gate_bottom"}
                                  or device.source_mode is not SourceMode.VOLTAGE
                                  for role, device in active_gates.items()):
            raise ValueError("continuous_gate_scan supports only voltage-source gates; smu_bias must be off")
        if any(getattr(smu.plan, role).ramp is None
               and getattr(smu.plan, role).zero_readback_tolerance_v is None for role in active_gates):
            raise ValueError("continuous_gate_scan direct gates require explicit zero_readback_tolerance_v")
    addresses = []
    if smu is not None:
        addresses += [h.address.strip().upper() for h in smu.hardware.by_role().values()]
    if lockin is not None:
        addresses += [lockin.lockin_xx.address.strip().upper(), lockin.lockin_xy.address.strip().upper()]
        if getattr(lockin, "lockin_aux", None) is not None:
            addresses.append(lockin.lockin_aux.address.strip().upper())
    if cryostat is not None and cryostat.cryostat.com_port:
        addresses.append(cryostat.cryostat.com_port)
    if optical is not None:
        addresses += [optical.nkt.port] if getattr(optical.nkt, "port", "") else []
        if optical.pem is not None and optical.pem.port:
            addresses.append(optical.pem.port)
        if optical.pm is not None and optical.pm.resource:
            addresses.append(optical.pm.resource)
        if optical.chopper is not None and optical.chopper.port:
            addresses.append(optical.chopper.port)
        if optical.scan.laser_source == "cld" and optical.nkt.address:
            addresses.append(optical.nkt.address)
    identities = [re.sub(r"^ASRL(\d+)::INSTR$", r"COM\1", value.strip().upper()) for value in addresses]
    if len(set(identities)) != len(identities):
        raise ValueError("All active resources, including optical ports and lock-in roles, must be distinct")
    plan = CombinationPlan(tuple(axes[m] for m in order), table["samples_per_condition"],
                           table["repeats"], table["run_name"], table["note"],
                           magnet_limits=(cryostat.magnet.limits if cryostat else
                                          MagnetLimits(3.0, 3.0, 3.0) if photonics or optical is not None else MagnetLimits()),
                           readback_tolerance_t=cryostat.magnet.readback_tolerance_t if cryostat else None,
                           lockin_coordinate_limits=lockin_limits,
                           strict_resultant=(photonics or optical is not None) and magnetic is None,
                           command_scoped_field=magnetic is not None,
                           unreachable_target_policy=table.get("unreachable_target_policy", "abort"))
    plan.validate()
    if plan.unreachable_target_policy == "skip_wavelength":
        feedback = optical.scan.feedback if optical is not None else None
        if (topology != "internal_xx_xy" or illumination != "off_before_axis_change"
                or feedback is None or feedback.actuator != "source_current"
                or optical.scan.laser_source != "nkt"
                or len(axes["lockin"].points) != 1
                or axes["lockin"].points[0].values["lockin_excitation_v_rms"] != 0.004
                or any(c.sine_output_connected for c in
                       (lockin.lockin_xx, lockin.lockin_xy, *(
                           (lockin.lockin_aux,) if getattr(lockin, "lockin_aux", None) else ())))):
            raise ValueError("skip_wavelength requires NKT current feedback, internal XX, fixed4mV and disconnected SINE outputs")
    range_schedule = None
    if "lockin_range_rules" in table:
        from .lockin_range_rules import compile_lockin_range_rules
        from .lockin_backend import capabilities_for
        if lockin is None or mode != "excitation":
            raise ValueError("lockin_range_rules requires fixed-sensitivity lock-in excitation mode")
        roles = {"lockin_xx": lockin.lockin_xx, "lockin_xy": lockin.lockin_xy}
        if getattr(lockin, "lockin_aux", None) is not None:
            roles["lockin_aux"] = lockin.lockin_aux
        safety_path = Path(lockin.safety_path) if photonics else path.with_name("lockin_safety.toml")
        safety_doc = configuration._load_document(safety_path)
        allowed = {}
        for role, cfg in roles.items():
            if getattr(cfg.sensitivity_mode, "value", cfg.sensitivity_mode) != "fixed" or getattr(cfg, "harmonic_settings", ()):
                raise ValueError("lockin_range_rules cannot combine with auto/per-harmonic sensitivity")
            scales = safety_doc[role]["allowed_fixed_full_scales_v"]
            allowed[role] = tuple(v for v in scales if v in capabilities_for(cfg.model).sensitivity_full_scales_v)
        if not photonics and any(
                p.range_spec.xx_full_scale_v is not None or p.range_spec.xy_full_scale_v is not None
                for p in prepared.grid):
            raise ValueError("lockin_range_rules cannot combine with segment sensitivity overrides")
        range_schedule = compile_lockin_range_rules(table["lockin_range_rules"], plan, allowed)
        if illumination == "continuous_gate_scan":
            group_ranges = {}
            for row in range_schedule.preview():
                group = row["outer_indices"]["optical"]
                ranges = tuple(sorted(row["full_scales_v"].items()))
                if group in group_ranges and group_ranges[group] != ranges:
                    raise ValueError("continuous_gate_scan requires invariant lock-in ranges within each optical group")
                group_ranges[group] = ranges
        if (optical is not None and optical.background.enabled
                and optical.background.capture_mode == "once_before_scan"
                and len({tuple(sorted(row["full_scales_v"].items())) for row in range_schedule.preview()}) > 1):
            raise ValueError("Variable lockin_range_rules requires per_condition dark backgrounds")
    snapshot = plan.snapshot()
    if range_schedule is not None:
        snapshot["lockin_range_rules"] = range_schedule.snapshot()
    snapshot.update(mode="hardware", hardware_scope="four-module-lockin-grid-v2",
                    qualification_policy="axis-entry-strict-environment-v1",
                    lockin_mode=mode,
                    illumination_policy=illumination,
                    magnetic_deviation_policy=magnetic_policy,
                    hardware=_json(hardware), config_path=str(path),
                    config_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    cleanup_policy={"lockin": ("4mV_restore_ranges_h1" if mode == "excitation"
                        else "4mV_h1_restore_baseline_frequency_ranges_reserve"), "smu": "zero_disable",
                        "temperature": "normal_hold_failure_disable",
                        "magnetic": (magnetic.cleanup.normal_end_field_policy.value if magnetic else "inactive"),
                        "magnetic_failure": "monitored_zero_or_manual_verification"},
                    resume_supported=False)
    if photonics:
        from .lockin_settling import SETTLING_POLICY
        snapshot.update(hardware_scope="photonics-lockin-v1", reference_topology=topology)
        snapshot["hardware"]["lockin"]["settling_policy"] = SETTLING_POLICY
        snapshot["cleanup_policy"]["lockin"] = "explicit_source_cleanup_preserve_external_reference"
    if lockin is not None and getattr(lockin, "lockin_aux", None) is not None:
        snapshot["hardware"]["lockin"]["active_roles"] = ["xx", "xy", "aux"]
        snapshot["cleanup_policy"]["lockin"] = "explicit_source_cleanup_preserve_internal_xx_ttl_receivers"
    if range_schedule is not None and not photonics:
        snapshot["cleanup_policy"]["lockin"] = "explicit_source_cleanup_preserve_internal_xx_ttl_receivers"
    if optical is not None:
        snapshot["hardware_scope"] = "photonics-combination-v1"
        snapshot["cleanup_policy"]["optical"] = "laser_off_then_electrical_protection_then_pem_finish"
        snapshot["reference_topology"] = topology
    if rotator is not None:
        snapshot["cleanup_policy"]["rotator"] = "stop_if_moving_preserve_angle_no_automatic_home"
    if positioner is not None:
        snapshot["hardware_scope"] = "positioner-combination-v1"
        snapshot["cleanup_policy"]["positioner"] = "stop_owned_motion_hold_or_explicit_success_return_start"
        if positioner.focus_plane is not None and positioner.focus_plane.calibration_temperature_k is not None and cryostat is None:
            raise ValueError("Temperature-bound focus calibration requires an active temperature/magnetic cryostat observation")
    # Include point engines and shared safety code, not just the Cartesian loop.
    snapshot["source_sha256"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in Path(__file__).parent.glob("*.py")}
    database = project.database_path
    if not database.is_absolute():
        database = path.parent / database
    return HardwareCombinationConfig(path, plan, smu, lockin, snapshot, temperature, magnetic,
                                     cryostat, database.resolve(), run_id, mode, optical, topology, illumination, rotator, range_schedule, positioner)


class _Events:
    def __init__(self, sink):
        self.sink = sink
        self.cleaning = False
        self.errors = []
        self.measurement_stage = None
        self.monitor_phase = None

    def phase(self, name, **context):
        payload = {"phase": name, **context}
        self.event("scan_phase", payload)
        self.monitor_phase = payload

    def event(self, kind, payload):
        try:
            if self.measurement_stage is not None:
                payload = {**payload, "measurement_stage": self.measurement_stage}
            self.sink(kind, _json(payload))
        except BaseException as exc:
            if not self.cleaning:
                raise
            # Disk/audit failure must never prevent later safety actions.
            self.errors.append(f"{kind}: {type(exc).__name__}: {exc}")


class HardwareCombinationStation:
    def __init__(self, config, *, smu_adapter_factory=None, manager_factory=None, sleep=None,
                 dll=None, monotonic=None, optical_backend_factory=None,
                 pem_factory=None, meter_factory=None, chopper_factory=None, rotator_device_factory=None,
                 positioner_device_factory=None):
        self.config = config
        self.smu_adapter_factory = smu_adapter_factory
        self.manager_factory = manager_factory
        self.sleep = sleep
        self.smu = self.lockin = None
        self.optical = None
        self.rotator = None
        self.rotator_device_factory = rotator_device_factory
        self._rotator_actual_context = {}
        self._rotator_motion_active = False
        self.positioner = None
        self.positioner_device_factory = positioner_device_factory
        self._positioner_motion_active = False
        self._positioner_context = {}
        self._cleanup_failure = False
        self.source_cleanup_verified = False
        self.optical_backend_factory = optical_backend_factory
        self.pem_factory, self.meter_factory = pem_factory, meter_factory
        self.chopper_factory = chopper_factory
        self.point = None
        self.open_completed = False
        self.cryo = None
        self.dll, self.monotonic = dll, monotonic
        self._optical_group_ready = False
        self._optical_group_sequence = 0
        self._applied = {}
        self._axis_scopes = {}
        self._background_visit = 0
        self.background = None

    def set_event_sink(self, sink):
        self.events = _Events(sink)

    def set_monitor_phase(self, name, **context):
        self.events.phase(name, **context)

    @contextmanager
    def _monitor_lockin_wait(self):
        previous = self.events.monitor_phase
        context = ({"sample_index": previous["sample_index"]}
                   if previous is not None and "sample_index" in previous else {})
        self.set_monitor_phase("lockin_settling", module="lockin", **context)
        yield
        # Do not imply sampling resumed after an interrupted/failed wait.
        if previous is not None:
            self.set_monitor_phase(previous["phase"],
                **{key: value for key, value in previous.items() if key != "phase"})

    def open(self, modules):
        if modules != tuple(a.module for a in self.config.plan.axes):
            raise ValueError("Station modules differ from validated plan")
        preflight = {}
        if self.config.cryostat is not None:
            self.cryo = CryostatPointSession(self.config.cryostat, self.config.temperature,
                self.config.magnetic, self.events.event, dll=self.dll,
                monotonic=self.monotonic, sleep=self.sleep)
            preflight["cryostat"] = self.cryo.open()
        if self.config.smu is not None:
            kwargs = {}
            if self.smu_adapter_factory is not None:
                kwargs["adapter_factory"] = self.smu_adapter_factory
            if self.sleep is not None:
                kwargs["sleep"] = self.sleep
            if self.monotonic is not None:
                kwargs["monotonic"] = self.monotonic
            self.smu = ThreeSmuSession.open(
                self.config.smu.hardware, self.config.smu.plan, authorize_writes=True,
                authorize_status_consumption=True,
                on_preflight=lambda role, state: self.events.event(
                    "smu_preflight_role", {"role": role, "state": asdict(state)}), **kwargs)
            active = {role: self.config.smu.hardware.require_role(role)
                      for role in active_smu_roles(self.config.smu.plan)}
            explicit_optical_gates = (
                self.config.optical is not None
                and [axis.module for axis in self.config.plan.axes if axis.module != "rotator"] == ["optical", "smu", "lockin"]
                and bool(active)
                and all(role in {"gate_top", "gate_bottom"}
                        and device.source_mode is SourceMode.VOLTAGE
                        and (getattr(self.config.smu.plan, role).ramp is not None
                             or getattr(self.config.smu.plan, role).zero_readback_tolerance_v is not None)
                        for role, device in active.items()))
            if self.config.illumination_policy == "continuous_gate_scan" or explicit_optical_gates:
                self.smu.enable_guarded_direct_gates()
            preflight["smu"] = {r: asdict(v) for r, v in self.smu.preflight.items()}
            self.events.event("smu_preflight", preflight["smu"])
        if self.config.lockin is not None:
            session_type = LockinPointSession
            if (getattr(self.config.lockin, "lockin_aux", None) is not None
                    or getattr(self.config, "lockin_range_schedule", None) is not None):
                from .internal_lockin_group import InternalLockinGroupSession
                session_type = InternalLockinGroupSession
            if self.config.reference_topology in PHOTONICS_REFERENCE_TOPOLOGIES:
                from .photonics_lockin_points import PhotonicsLockinPointSession
                session_type = PhotonicsLockinPointSession
            kwargs = {key: value for key, value in {"sleep": self.sleep,
                ("clock" if self.config.reference_topology in PHOTONICS_REFERENCE_TOPOLOGIES else "monotonic"): self.monotonic
                }.items() if value is not None}
            self.lockin = session_type(
                self.config.path, self.config.lockin, self.events.event,
                manager_factory=self.manager_factory, mode=self.config.lockin_mode, **kwargs)
            self.lockin.monitor_wait_context = self._monitor_lockin_wait
            preflight["lockin"] = self.lockin.open()
        if self.config.optical is not None:
            from .optical_points import OpticalPointSession
            kwargs = {key: value for key, value in {
                "backend_factory": self.optical_backend_factory, "pem_factory": self.pem_factory,
                "meter_factory": self.meter_factory, "chopper_factory": self.chopper_factory,
                "clock": self.monotonic,
                "sleep": self.sleep}.items() if value is not None}
            self.optical = OpticalPointSession(self.config.optical, self.events.event,
                on_settings_applied=self._signal_change_callback("optical.tuning_completed"),
                state_guard=self._rotator_state_guard if (getattr(self.config, "rotator", None)
                    or getattr(self.config, "positioner", None)) else None,
                **kwargs)
            preflight["optical"] = self.optical.open(authorize_writes=True, confirm_manual_route=True)
            if self.config.optical.background.enabled:
                from .optical_background import MatchedLockinBackground
                self.background = MatchedLockinBackground(self.config.optical.background, clock=self.optical.clock)
        if self.config.reference_topology in PHOTONICS_REFERENCE_TOPOLOGIES:
            self.lockin.reference_recovery_guard = self._reference_recovery_guard
        elif self.lockin is not None and self.optical is not None:
            self.lockin.wait_guard = self._internal_wait_guard
        if getattr(self.config, "rotator", None) is not None:
            from .rotator_points import RotatorPointSession
            kwargs = {key: value for key, value in {
                "device_factory": self.rotator_device_factory, "clock": self.monotonic,
                "sleep": self.sleep}.items() if value is not None}
            self.rotator = RotatorPointSession(self.config.rotator, event_sink=self.events.event, **kwargs)
            preflight["rotator"] = self.rotator.open()
        if getattr(self.config, "positioner", None) is not None:
            from .positioner_points import PositionerPointSession
            kwargs = {key: value for key, value in {
                "device_factory": self.positioner_device_factory, "clock": self.monotonic,
                "sleep": self.sleep}.items() if value is not None}
            self.positioner = PositionerPointSession(self.config.positioner, event_sink=self.events.event, **kwargs)
            preflight["positioner"] = self.positioner.open()
        # All active preflights must succeed before any configuration writes.
        if self.optical is not None:
            if self.lockin is not None:
                self.lockin.protect_source()
            self.optical.configure()
            # Prepare a reference while laser OFF; HARM/reference setup needs
            # the actual PEM frequency even when modulation starts disabled.
            self.optical.set_point(0)
        if self.rotator is not None:
            self._rotator_motion_active = True
            try:
                self._rotator_off_guard()
                self.rotator.begin(guard=self._rotator_off_guard)
            finally:
                self._rotator_motion_active = False
        if self.lockin is not None and self.optical is None and self.positioner is not None:
            self.lockin.wait_guard = self._internal_wait_guard
        if self.lockin is not None:
            self.lockin.configure()
            # The first lock-in axis may request high excitation before an
            # optical axis is visited. Certify the prepared external clock now.
            if self.optical is not None:
                self._prepare_optical_reference()
                self._verify_optical_reference()
        if self.smu is not None:
            self.smu.begin_points(self.events)
        self.open_completed = True
        return _json({"mode": "hardware", "identities_and_initial_state": preflight})

    def prepare_lockin_ranges(self, condition):
        """Select from frozen requested axes before optical/lock-in preparation."""
        schedule = getattr(self.config, "lockin_range_schedule", None)
        if schedule is None:
            return
        selection = schedule.resolve({module: value["index"] for module, value in condition["axes"].items()})
        if self.optical is not None:
            current = self.lockin.range_context()
            held_continuous = (self.config.illumination_policy == "continuous_gate_scan"
                and self._optical_group_ready and current.get("verified") is True
                and current.get("confirmed_full_scales_v") == selection.full_scales_v)
            if not held_continuous:
                self.optical.suspend()
        result = self.lockin.apply_range_profile({"name": selection.rule_name,
                                                 "ranges": selection.full_scales_v})
        if self.background is not None and result.get("changed_roles"):
            self.background.invalidate("lock-in sensitivity profile changed")
        self.events.event("lockin_range_selected", {"condition_id": condition["condition_id"],
            "outer_indices": list(selection.outer_indices), "requested": selection.requested, **result})

    def apply(self, module, point):
        self._applied[module] = dict(point.values)
        if self.config.illumination_policy == "continuous_gate_scan":
            return self._apply_continuous_gate(module, point)
        # All axis movement occurs with confirmed laser OFF, including electrical
        # inner-axis changes while the optical coordinates themselves stay fixed.
        if self.optical is not None:
            self.optical.suspend()
        if module == "positioner":
            self._positioner_motion_active = True
            try:
                if not self.positioner.begun:
                    # Environmental outer axes qualify before freezing the
                    # single origin or preparing any positioner outputs.
                    modules = [axis.module for axis in self.config.plan.axes]
                    self.positioner.begin(guard=self._positioner_off_guard,
                        wrap_required=modules.index("positioner") > 0 or self.config.plan.repeats > 1)
                result = self.positioner.set_point(point.values, guard=self._positioner_off_guard)
            finally:
                self._positioner_motion_active = False
            self._positioner_context = {key: result[key] for key in
                ("coordinate_frame", "mode", "z_mode", "focus_plane_id", "origin", "target",
                 "arrival_policy", "arrival_evidence") if key in result}
            if self.background is not None:
                self.background.invalidate("positioner visit changed")
            if result.get("changed"):
                self._signal_changed("positioner.motion_completed")
            return result
        if module == "rotator":
            self._rotator_motion_active = True
            try:
                result = self.rotator.set_point(point.values, guard=self._rotator_off_guard)
            finally:
                self._rotator_motion_active = False
            self._rotator_actual_context = dict(result["actual"])
            if self.background is not None:
                self.background.invalidate("waveplate angle visit changed")
            if result.get("changed"):
                self._signal_changed("rotator.motion_completed")
            return result
        if module == "optical":
            axis = next(a for a in self.config.plan.axes if a.module == "optical")
            index = point.metadata.get("optical_point_index")
            if (type(index) is not int or not 0 <= index < len(axis.points)
                    or axis.points[index] != point):
                raise ValueError("Optical point identity does not match the approved grid")
            if self.lockin is not None and self.config.reference_topology in PHOTONICS_REFERENCE_TOPOLOGIES:
                self.lockin.prepare_reference_transition()
            return self.optical.set_point(index)
        if module in {"temperature", "magnetic"}:
            result = self.cryo.apply(module, point)
            # Magnetic execution already includes the arrival/stability window.
            if module == "magnetic":
                self._signal_changed("magnetic.qualified")
            return result
        if module == "smu":
            self.point = point
            self.smu.set_point({key[:-2]: v for key, v in point.values.items()},
                               segment=point.segment,
                               on_settings_applied=self._signal_change_callback("smu.settings_applied"))
            # Actual sensed coordinates are produced only by the formal read.
            return {"requested": point.values}
        if module != "lockin":
            raise ValueError("Unknown combination module")
        lockin_axis = next(axis for axis in self.config.plan.axes if axis.module == "lockin")
        index = lockin_axis.points.index(point)
        actual = self.lockin.set_point(point.values["lockin_excitation_v_rms"],
            point.values["lockin_frequency_hz"], point_index=index)
        self._prepare_optical_reference()
        return {"actual": actual}

    def _guard_continuous_optical(self):
        if not self._optical_group_ready:
            raise ValueError("A continuous optical group must qualify before gate movement")
        return self.optical.observe_hold(phase="gate_transition")

    def _rotator_off_guard(self):
        if self.optical is None:
            raise ValueError("Waveplate movement requires an owned optical OFF guard")
        return self.optical.observe_dark(phase="rotator_motion_guard")

    def _rotator_state_guard(self):
        # During our owned motion the separate fresh optical OFF guard runs;
        # target-position checks apply once the angle axis has qualified.
        if (self.rotator is not None and self.rotator.begun and self.rotator.targets
                and not self._rotator_motion_active):
            self.rotator.verify()
        self._positioner_verify()

    def _positioner_temperature_guard(self):
        plane = self.config.positioner.focus_plane
        if plane is not None and plane.calibration_temperature_k is not None:
            if self.cryo is None:
                raise ValueError("Temperature-bound focus plane needs fresh cryostat readback")
            observation = self.cryo.capture("positioner_focus_temperature")
            plane.validate_temperature(observation["state"]["sample_temperature_k"])

    def _positioner_off_guard(self):
        if self.optical is not None:
            self.optical.observe_dark(phase="positioner_motion_guard")
        elif not self.config.positioner.external_light_off_confirmed:
            raise ValueError("Positioner motion needs confirmed laser OFF")
        if self.cryo is not None:
            self.cryo.capture("positioner_motion_guard")
        self._positioner_temperature_guard()

    def _positioner_verify(self):
        if (self.positioner is not None and self.positioner.begun and self.positioner.targets
                and not self._positioner_motion_active):
            self._positioner_temperature_guard()
            return self.positioner.verify()
        return {}

    def _positioner_reading(self):
        actual = self._positioner_verify()
        if not actual:
            raise ValueError("Positioner target has not qualified")
        plane = self.config.positioner.focus_plane
        measurements = ({"positioner_focus_error_um": plane.focus_error(
            actual["positioner_x_um"], actual["positioner_y_um"], actual["positioner_z_um"])} if plane else {})
        captured = utc_now()
        return {"module": "positioner", "captured_at_utc": captured, "actual": actual,
                "measurements": measurements, "clean": True, "problems": [],
                "status": {"positioner": {**self._positioner_context,
                    "verified": True, "units": "um", "captured_at_utc": captured,
                    "sampling": "sequential_xyz_bracket_not_continuous",
                    "readback": _json(self.positioner.last_confirmed)}}}

    def _signal_change_callback(self, reason):
        if self.lockin is not None:
            return lambda: self.lockin.disturbed(reason)
        return None

    def _signal_changed(self, reason):
        callback = self._signal_change_callback(reason)
        if callback is not None:
            callback()

    def qualify_axis(self, module, point, scope):
        self._axis_scopes[module] = dict(scope)
        if module in {"temperature", "magnetic"}:
            result = self.cryo.qualify(modules=(module,), scope=scope)
            if module == "temperature":
                self._signal_changed("temperature.qualified")
            return result
        return {"ready": True, "axis_scope": scope}

    def _apply_continuous_gate(self, module, point):
        if module == "optical":
            self._optical_group_ready = False
            self.optical.suspend()
            axis = next(a for a in self.config.plan.axes if a.module == "optical")
            index = point.metadata.get("optical_point_index")
            if (type(index) is not int or not 0 <= index < len(axis.points)
                    or axis.points[index] != point):
                raise ValueError("Optical point identity does not match the approved grid")
            if self.config.reference_topology in PHOTONICS_REFERENCE_TOPOLOGIES:
                self.lockin.prepare_reference_transition()
            prepared = self.optical.set_point(index)
            fixed = next(a for a in self.config.plan.axes if a.module == "lockin").points[0]
            # The fixed excitation is selected while dark, before any gate is
            # moved. The coordinator later reapplies this inner axis; that call
            # verifies the held setting rather than writing or suspending light.
            self.lockin.set_point(fixed.values["lockin_excitation_v_rms"],
                fixed.values["lockin_frequency_hz"], point_index=0)
            self._prepare_optical_reference()
            self._verify_optical_reference()
            if self.config.reference_topology in PHOTONICS_REFERENCE_TOPOLOGIES:
                self.lockin.restore_source_after_reference_transition()
            if self.config.reference_topology == "internal_xx_xy":
                self.lockin.verify_ready()
            else:
                self.lockin.qualify()
            self.set_monitor_phase("optical_tuning", module="optical")
            self.optical.qualify()
            self.lockin.qualify()
            self._verify_optical_reference()
            self._optical_group_sequence += 1
            self._optical_group_ready = True
            self.events.event("continuous_optical_group_qualified", {
                "group_sequence": self._optical_group_sequence,
                "optical_point_index": index, "illumination_policy": self.config.illumination_policy,
                "fixed_lockin": fixed.values, "requested": point.values})
            return {**prepared, "optical_group_qualified": True,
                    "group_sequence": self._optical_group_sequence}
        if module == "smu":
            self._guard_continuous_optical()
            self._verify_optical_reference()
            self.point = point
            self.smu.set_point({key[:-2]: value for key, value in point.values.items()},
                segment=point.segment, transition_guard=self._guard_continuous_optical,
                on_settings_applied=self._signal_change_callback("smu.settings_applied"))
            self._guard_continuous_optical()
            return {"requested": point.values, "illumination_held": True,
                    "group_sequence": self._optical_group_sequence}
        if module == "lockin":
            fixed = next(a for a in self.config.plan.axes if a.module == "lockin").points[0]
            if point != fixed:
                raise ValueError("The illuminated gate group cannot change lock-in excitation")
            self._guard_continuous_optical()
            return {"actual": self.lockin.read_coordinates(), "source_settings_reused": True}
        raise ValueError("Unsupported axis in continuous_gate_scan")

    def qualify(self, modules):
        self._positioner_verify()
        if self.rotator is not None:
            self.rotator.verify()
        if self.config.illumination_policy == "continuous_gate_scan":
            self._guard_continuous_optical()
            if self.background is not None and self.background.needs_capture:
                self._capture_dark_background()
                self.set_monitor_phase("optical_tuning", module="optical")
                self.optical.qualify()
            self.lockin.qualify()
            self._verify_optical_reference()
            return {"ready": True, "mode": "hardware", "active_modules": modules,
                    "illumination_held": True, "group_sequence": self._optical_group_sequence}
        if (self.optical is not None and self.lockin is not None
                and self.config.reference_topology in PHOTONICS_REFERENCE_TOPOLOGIES):
            # Prepared PEM frequency must match the external clock while XX is
            # still protected, before restoring excitation or enabling emission.
            self._verify_optical_reference()
            self.lockin.restore_source_after_reference_transition()
        if self.lockin is not None:
            if self.optical is not None and self.config.reference_topology == "internal_xx_xy":
                # Fresh configuration/source proof is required while dark.
                # Optical preparation will disturb the detector again, so its
                # complete filter wait belongs after illumination has settled.
                self.lockin.verify_ready()
            else:
                self.lockin.qualify()
        if self.cryo is not None:
            self.cryo.qualify()
        if self.optical is not None:
            if self.background is not None and self.background.needs_capture:
                self._capture_dark_background()
            self.set_monitor_phase("optical_tuning", module="optical")
            self.optical.qualify()
            self._positioner_verify()
            if self.rotator is not None:
                self.rotator.verify()
            if self.lockin is not None:
                # PEM activation or power/wavelength changes may disturb the
                # reference; formal electrical qualification follows them.
                self.lockin.qualify()
            self._verify_optical_reference()
        return {"ready": True, "mode": "hardware", "active_modules": modules}

    def _background_context(self):
        context = {"requested_conditions": self._applied, "axis_visits": self._axis_scopes,
            "background_visit": self._background_visit,
            "reference_topology": self.config.reference_topology,
            "frequency_hz": self.lockin.target_frequency_hz,
            "harmonics_by_role": self.lockin.harmonics_by_role,
            "phase_shift_deg": {device.role.value: device.read_phase_shift() for device in self.lockin.pair},
            "chopper": ({key: self.optical.chopper.last_confirmed_state.get(key)
                 for key in ("phase", "ref", "nharmonic", "dharmonic", "blade", "enable")}
                if self.optical.chopper else None),
            "xx_sample_connected": self.config.lockin.lockin_xx.sine_output_connected}
        if getattr(self.config, "lockin_range_schedule", None) is not None:
            profile = self.lockin.range_context()
            if not profile.get("verified"):
                raise ValueError("No verified lock-in range profile for dark matching")
            context["lockin_full_scales_v"] = profile["confirmed_full_scales_v"]
        if self.rotator is not None:
            context["actual_conditions"] = {"rotator": dict(self._rotator_actual_context)}
        if self.positioner is not None:
            context["positioner"] = dict(self._positioner_context)
        if getattr(self.config.lockin, "lockin_aux", None) is not None:
            context["lockin_identities"] = dict(self.lockin.identities)
            context["active_roles"] = [c.role.value for c in self.lockin.roles]
        if self.background.config.capture_mode == "once_before_scan":
            # Optical settings may vary; electrical/environment conditions may
            # not inherit a different condition's dark. Visit counters are not
            # physical settings and do not invalidate a run-wide baseline.
            context["requested_conditions"] = {key: value for key, value in self._applied.items()
                                                if key != "optical"}
            context.pop("axis_visits")
            context.pop("background_visit")
        return context

    def _capture_dark_background(self):
        self.set_monitor_phase("dark_acquisition", module="lockin")
        self.events.event("optical_background_status", {"stage": "dark_acquisition",
            "capture_mode": self.background.config.capture_mode, "samples": self.background.config.samples})
        self.optical.suspend()
        self._background_visit += 1
        self.background.invalidate("new dark/illuminated condition visit")
        self._signal_changed("optical.dark_transition")
        self.optical.observe_dark(phase="background_before")
        self.lockin.qualify()
        context = self._background_context()
        readings = []
        self.events.measurement_stage = "dark_background"
        try:
            for sample_index in range(self.background.config.samples):
                self.set_monitor_phase("dark_acquisition", module="lockin", sample_index=sample_index)
                dark_angles = {}
                dark_position = self._positioner_reading() if self.positioner is not None else None
                if self.rotator is not None:
                    dark_angles = self.rotator.verify()
                self.optical.observe_dark(phase="background_sample_before")
                self._background_smu_guard()
                temperature_observation = None
                magnetic_observation = None
                if self.cryo is not None:
                    environment = self.cryo.capture("dark_background")
                    if self.config.temperature is not None:
                        temperature_observation = {
                            "sampling": "pre_read_not_window_mean",
                            "captured_at_utc": environment["captured_at_utc"],
                            "temperature_k": environment["state"]["sample_temperature_k"],
                            "assessment": environment.get("temperature_assessment")}
                    if self.config.magnetic is not None:
                        magnetic_observation = {
                            "sampling": "pre_read_not_window_mean",
                            "captured_at_utc": environment["captured_at_utc"],
                            "field_x_t": environment["state"]["field"]["bx_t"],
                            "field_z_t": environment["state"]["field"]["bz_t"],
                            "assessment": environment.get("magnetic_assessment")}
                reading = self.lockin.sample_point(
                    measurement_context=self.cryo.capture if self.cryo is not None else None)
                if temperature_observation is not None:
                    reading = {**reading,
                        "actual": {**reading["actual"], "temperature_k": temperature_observation["temperature_k"]},
                        "status": {**reading["status"], "temperature_observation": temperature_observation}}
                if magnetic_observation is not None:
                    reading = {**reading,
                        "actual": {**reading["actual"],
                            "field_x_t": magnetic_observation["field_x_t"],
                            "field_z_t": magnetic_observation["field_z_t"]},
                        "status": {**reading["status"], "magnetic_observation": magnetic_observation}}
                if dark_angles:
                    reading = {**reading, "actual": {**reading["actual"], **dark_angles}}
                if dark_position is not None:
                    reading = {**reading,
                        "actual": {**reading["actual"], **dark_position["actual"]},
                        "measurements": {**reading["measurements"], **dark_position["measurements"]},
                        "status": {**reading["status"], **dark_position["status"]}}
                readings.append(reading)
                self.optical.observe_dark(phase="background_sample_after")
                self._positioner_verify()
                if self.rotator is not None:
                    self.rotator.verify()
                self._background_smu_guard()
                if self._background_context() != context:
                    raise ValueError("Reference/phase changed during dark baseline acquisition")
            baseline = self.background.capture(readings, context)
            self.events.event("optical_dark_background", baseline)
            self.events.event("optical_background_status", {"stage": "dark_captured",
                "baseline_id": baseline["baseline_id"], "valid_for_analysis": baseline["valid_for_analysis"],
                "capture_mode": self.background.config.capture_mode})
        finally:
            self.events.measurement_stage = None

    def _background_smu_guard(self):
        if self.smu is not None:
            sample = self.smu.sample_point({key[:-2]: value for key, value in self.point.values.items()},
                                           segment=self.point.segment)
            self.events.event("smu_dark_background_guard", asdict(sample))
            if not sample.clean:
                raise ValueError("SMU dark-background state failed its existing electrical protection")

    def skip_unreachable_wavelength(self, error):
        """Handle one expected tuning bound, with fresh guards and no retry.

        This runs inside an open, healthy station before any formal light read.
        It never recovers a failed/closed SMU or turns an output ON for evidence.
        A failed guard propagates to the unchanged fail-closed run cleanup.
        """
        if (self.config.plan.unreachable_target_policy != "skip_wavelength"
                or type(error) is not OpticalTargetUnreachable
                or not getattr(error, "evidence", {}).get("skip_eligible")
                or getattr(error, "optical_point_index", None) != self.optical.index
                or self.optical.sample_active or self.events.errors):
            raise ValueError("Optical failure is not eligible for wavelength skipping") from error
        self.optical.suspend()
        self.optical.observe_dark(phase="skip_unreachable_wavelength", deadline=self.optical.deadline)
        self.lockin.protect_source()
        self.lockin.verify_ready()
        # Unlike dark readiness alone, qualify checks the receiver references.
        previous_deadline = self.lockin.wait_deadline
        self.lockin.wait_deadline = self.optical.deadline
        try:
            self.lockin.qualify()
        finally:
            self.lockin.wait_deadline = previous_deadline
        self._verify_optical_reference()
        if getattr(self.config.lockin, "lockin_aux", None) is not None:
            for cfg, device in zip(self.lockin.roles, self.lockin.pair):
                status = device.read_reference_status(current_status_supported=cfg.current_status_supported)
                problems = self.lockin._status_problems(cfg, status, allow_reference_unlock=False)
                self.events.event("unreachable_skip_reference_status", {"role": cfg.role.value,
                                                                        "status": asdict(status)})
                if problems:
                    raise ValueError("Mixed lock-in fault prevents wavelength skip: " + "; ".join(problems))
        else:
            for device in self.lockin.pair:
                lia, errors = device.read_status_latches()
                self.events.event("unreachable_skip_reference_status", {"role": device.role.value,
                    "lia_status": asdict(lia), "error_status": errors})
                if lia.raw != 0 or errors != 0:
                    raise ValueError("Mixed SR830 status prevents wavelength skip")
        self._background_smu_guard()
        self.optical.observe_dark(phase="skip_unreachable_wavelength_after", deadline=self.optical.deadline)
        if self.background is not None:
            self.background.invalidate("unreachable wavelength skipped")
        if self.events.errors:
            raise OSError("Wavelength skip audit failed: " + "; ".join(self.events.errors))
        return {"ready": True, "laser_off_confirmed": True,
                "reference_verified": True, "electrical_guard_verified": True,
                "optical_point_index": self.optical.index,
                "wavelength_nm": self.optical.wavelength, "reason": "target_unreachable",
                "original_error": f"{type(error).__name__}: {error}",
                "target_evidence": error.evidence}

    def begin_sample(self):
        self._positioner_verify()
        if self.rotator is not None:
            self.rotator.verify()
        if self.optical is not None:
            self.optical.begin_sample()
            try:
                self._verify_optical_reference()
            except BaseException as primary:
                # begin_sample may fail after the optical bracket has opened.
                # Close/check that bracket before any whole-window retry.
                try:
                    self.optical.end_sample()
                except BaseException as secondary:
                    raise _window_error([primary, secondary]) from primary
                raise
        if self.cryo is not None:
            self.cryo.begin_sample()

    def end_sample(self, reads):
        errors = []
        try:
            self._positioner_verify()
        except BaseException as exc:
            errors.append(exc)
        if self.rotator is not None:
            try:
                self.rotator.verify()
            except BaseException as exc:
                errors.append(exc)
        for device in (self.optical, self.cryo):
            if device is not None:
                try:
                    device.end_sample(reads)
                except BaseException as exc:
                    errors.append(exc)
        if errors:
            raise _window_error(errors)
        self._verify_optical_reference()

    def run_sample_operation(self, operation):
        """Retry the whole before/all-modules/after window, never a partial pair."""
        def capture():
            if self.config.reference_topology not in PHOTONICS_REFERENCE_TOPOLOGIES:
                return operation()
            return self.lockin.run_reference_operation("combination_formal_sample", operation)
        maximum = self.config.optical.scan.max_power_retries if self.optical else 0
        deadline = None
        try:
            for retry in range(maximum + 1):
                try:
                    if deadline is not None and self.optical.clock() >= deadline:
                        raise TimeoutError("Optical whole-window recovery exhausted its shared deadline")
                    result = capture()
                    if deadline is not None and self.optical.clock() >= deadline:
                        raise TimeoutError("Optical recovery deadline expired during the complete sample window")
                    return result
                except OpticalPowerDeviation as exc:
                    self.events.event("optical_window_rejected", {"retry_index": retry,
                        "accepted": False, "valid_for_analysis": False,
                        "reason": str(exc), "evidence": exc.evidence})
                    if retry >= maximum:
                        raise
                    now = self.optical.clock()
                    if deadline is None:
                        deadline = now + self.config.optical.scan.power_retry_timeout_s
                    if now >= deadline:
                        raise TimeoutError("Optical whole-window recovery exhausted its shared deadline") from exc
                    if self.optical.sample_active:
                        raise ValueError("Cannot retune inside an unclosed optical bracket") from exc
                    self.optical.retry_deadline = deadline
                    if self.lockin is not None and self.config.reference_topology == "internal_xx_xy":
                        self.lockin.wait_deadline = deadline
                    self.optical.suspend()
                    if self.background is not None and self.background.needs_capture:
                        self._capture_dark_background()
                    self.set_monitor_phase("optical_tuning", module="optical")
                    self.optical.qualify()
                    if self.lockin is not None:
                        self.lockin.qualify()
                    self._verify_optical_reference()
                    self.events.event("optical_window_retry", {"retry_index": retry + 1,
                        "remaining_s": max(0, deadline - self.optical.clock()),
                        "scope": "all modules and both optical brackets"})
        finally:
            if self.optical is not None:
                self.optical.retry_deadline = None
            if self.lockin is not None and self.config.reference_topology == "internal_xx_xy":
                self.lockin.wait_deadline = None

    def _reference_recovery_guard(self, deadline):
        self._rotator_state_guard()
        if self.optical is not None:
            self.optical.guard_reference_recovery(deadline=deadline)
            if self.lockin.configured:
                self._check_pem_reference(wait_for_lock=False)
        if self.cryo is not None:
            self.cryo.capture("reference_recovery")

    def _internal_wait_guard(self):
        self._rotator_state_guard()
        if self.optical is not None and self.optical.point is not None:
            self.optical.guard_reference_recovery(deadline=self.lockin.wait_deadline)
        if self.cryo is not None:
            self.cryo.capture("lockin_settling")

    def _prepare_optical_reference(self):
        if (self.optical is not None and self.lockin is not None
                and self.config.reference_topology == "internal_xx_xy"):
            # XX owns the independent internal clock. XY may still be acquiring
            # it after a source transition; full companion matching belongs to
            # illuminated qualification/formal entry, after remaining settling.
            frequency = self.lockin.read_coordinates(verify_companion=False)["lockin_frequency_hz"]
            return self.optical.prepare_reference(frequency)

    def _verify_optical_reference(self):
        if self.optical is None:
            return
        if self.config.reference_topology == "internal_xx_xy":
            return self.optical.verify_reference()
        return self.lockin.run_reference_operation("optical_reference_check", self._check_pem_reference)

    def _check_pem_reference(self, *, wait_for_lock=True):
        if self.config.reference_topology not in PHOTONICS_REFERENCE_TOPOLOGIES or self.optical is None:
            return
        if self.optical.pem is None or self.optical.last_state is None:
            raise ValueError("Prepared PEM reference evidence is missing")
        if wait_for_lock:
            self.lockin.wait_for_reference_lock()
        reference_frequencies = (self.lockin.read_reference_frequencies() if wait_for_lock
                                else self.lockin.read_reference_frequencies(wait_for_lock=False))
        pem_frequency = self.optical.last_state["pem"]["frequency_hz"]
        if not math.isfinite(pem_frequency) or pem_frequency <= 0:
            raise ValueError("PEM mechanical frequency must be positive and finite")
        pem_reference_harmonic = self.config.lockin.pem_reference_harmonic
        expected_reference = pem_reference_harmonic * pem_frequency
        self.events.event("photonics_reference_check", {
            "pem_frequency_hz": pem_frequency,
            "pem_reference_harmonic": pem_reference_harmonic,
            "expected_external_reference_hz": expected_reference,
            "xx_reference_frequency_hz": reference_frequencies["xx"],
            "xy_reference_frequency_hz": reference_frequencies["xy"],
            "pair_tolerance_hz": self.config.lockin.pair_tolerance_hz,
            "captured_at_utc": utc_now()})
        if any(abs(expected_reference - value) > self.config.lockin.pair_tolerance_hz for value in reference_frequencies.values()):
            from .reference_transients import ReferenceTransientError
            raise ReferenceTransientError("PEM frequency differs from the lock-in external reference after applying the selected harmonic",
                kind="pem_reference_mismatch", evidence={"pem_frequency_hz": pem_frequency,
                    "pem_reference_harmonic": pem_reference_harmonic,
                    "expected_external_reference_hz": expected_reference,
                    "reference_frequencies_hz": reference_frequencies,
                    "pair_tolerance_hz": self.config.lockin.pair_tolerance_hz})

    def read(self, module):
        if module == "positioner":
            return self._positioner_reading()
        if module == "rotator":
            actual = self.rotator.verify()
            return {"module": "rotator", "captured_at_utc": utc_now(), "actual": actual,
                    "measurements": {}, "clean": True, "problems": [],
                    "status": {"verified": True, "angle_units": "deg"}}
        if module == "optical":
            return self.optical.read()
        if module in {"temperature", "magnetic"}:
            return self.cryo.read(module)
        if module == "lockin":
            reading = self.lockin.sample_point(
                measurement_context=self.cryo.capture if self.cryo is not None else None)
            if getattr(self.config, "lockin_range_schedule", None) is not None:
                reading.setdefault("status", {})["lockin_range_profile"] = self.lockin.range_context()
            if self.background is not None:
                reading = self.background.apply(reading, self._background_context())
                reading["status"]["background"] = reading["background"]
                self.events.event("optical_background_status", {"stage": "light_sampling",
                    **{key: reading["background"].get(key) for key in
                       ("baseline_id", "age_s", "capture_mode", "valid_for_analysis", "expired", "context_matches")}})
            return _json(reading)
        sample = self.smu.sample_point({k[:-2]: v for k, v in self.point.values.items()},
                                       segment=self.point.segment)
        self.events.event("smu_formal_sample", asdict(sample))
        measured, actual = {}, {}
        for role, timed in sample.readings.items():
            reading = timed.reading
            measured.update({role + "_voltage_v": reading.voltage_v,
                             role + "_current_a": reading.current_a})
            key = role + ("_v" if self.config.smu.hardware.require_role(role).source_mode
                          is SourceMode.VOLTAGE else "_a")
            actual[key] = reading.voltage_v if key.endswith("_v") else reading.current_a
        return {"module": "smu", "captured_at_utc": utc_now(), "actual": actual,
                "measurements": measured, "clean": sample.clean,
                "problems": list(sample.problems), "status": _json(asdict(sample))}

    def expected_measurements(self, condition):
        expected = set()
        if self.positioner is not None and self.config.positioner.focus_plane is not None:
            expected.add("positioner_focus_error_um")
        if self.config.smu is not None:
            for role in self.config.smu.hardware.by_role():
                expected.update({role + "_voltage_v", role + "_current_a"})
        if self.lockin is not None:
            for role, harmonics in self.lockin.harmonics_by_role.items():
                metrics = (("x_v", "y_v", "amplitude_v") if (self.config.reference_topology in PHOTONICS_REFERENCE_TOPOLOGIES
                           or getattr(self.config.lockin, "lockin_aux", None) is not None)
                           else ("x_v", "y_v", "amplitude_v", "phase_deg"))
                expected.update(f"lockin_{role}_h{h}_{metric}" for h in harmonics
                                if h in self.lockin.point_harmonics for metric in metrics)
        if self.optical is not None:
            expected.update(self.optical.expected_measurements())
        return expected

    def cleanup(self, module, failed):
        try:
            return self._cleanup_module(module, failed)
        except BaseException:
            self._cleanup_failure = True
            raise

    def _cleanup_module(self, module, failed):
        self.events.cleaning = True
        if module == "optical" and self.optical is not None:
            self._optical_group_ready = False
            # PM100D and lock-ins can share the default VISA manager. Keep it
            # and the PEM reference open until all electrical protection finishes.
            if (self.positioner is not None and self.config.positioner.finish == "return_start"
                    and self.open_completed and not failed):
                result = self.optical.prepare_positioner_return()
            else:
                result = self.optical.cleanup(finish_pem=False, close_meter=False)
            clean = result["verified"]
        elif module == "rotator" and self.rotator is not None:
            result = self.rotator.cleanup(failed=failed)
            clean = result.get("verified") is True
        elif module == "positioner" and self.positioner is not None:
            self._positioner_motion_active = True
            try:
                result = self.positioner.cleanup(failed=failed or self._cleanup_failure,
                    guard=self._positioner_off_guard)
            finally:
                self._positioner_motion_active = False
            clean = result.get("verified") is True
        elif module == "lockin" and self.lockin is not None:
            result = self.lockin.cleanup()
            clean = result["verified"]
            self.source_cleanup_verified = result.get("source_protection_verified", False) is True
        elif module == "smu" and self.smu is not None:
            result = self.smu.cleanup_points(self.events, reason="failed" if failed else "completed", failed=failed)
            clean = not result["manual_verification_required"]
        elif module in {"temperature", "magnetic"} and self.cryo is not None:
            try:
                result = self.cryo.cleanup(module, failed)
                clean = result["verified"] and not result.get("scan_limits_violated", False)
            except BaseException as exc:
                state = self.cryo.driver.last_confirmed_state if self.cryo.driver is not None else None
                result = {"verified": False, "error": f"{type(exc).__name__}: {exc}",
                          "last_confirmed_state_not_current": asdict(state) if state else None}
                clean = False
        else:
            result, clean = {"attempted": False, "reason": "not opened"}, True
        self._cleanup_failure = self._cleanup_failure or not clean or bool(self.events.errors)
        return _json({"module": module,
                      # VISA backends need not raise OSError. Never certify a
                      # failed hardware attempt just because later cleanup reads
                      # succeed; keep those readbacks but require operator review.
                      "clean": clean and self.open_completed and not self.events.errors and not failed,
                      "failure_requires_manual_review": failed,
                      "startup_completed": self.open_completed,
                      "result": result, "audit_errors": list(self.events.errors)})

    def close(self):
        errors = []
        for device in (self.lockin, self.smu, self.positioner, self.cryo, self.rotator, self.optical):
            if device is not None:
                try:
                    result = (device.close(finish_pem=self.lockin is None or self.source_cleanup_verified)
                              if device is self.optical else device.close())
                    if device is self.optical:
                        self.events.event("optical_final_cleanup", result)
                        if result.get("verified") is not True:
                            errors.append("Optical final cleanup unverified: " + str(result))
                    elif (device is self.rotator or device is self.positioner) and result:
                        errors.extend(str(error) for error in result)
                except BaseException as exc:
                    errors.append(f"{type(exc).__name__}: {exc}")
        errors.extend(self.events.errors)
        if errors:
            raise OSError("; ".join(errors))


def run_hardware_combination(config, store, run_id, *, authorize_hardware=False,
                               confirm_xy_sine_disconnected=False,
                               authorize_cryostat=False, dll=None, monotonic=None,
                               smu_adapter_factory=None, manager_factory=None, sleep=None,
                               on_registered=None, authorize_optical=False,
                               confirm_optical_route=False, optical_backend_factory=None,
                               pem_factory=None, meter_factory=None, chopper_factory=None,
                               rotator_device_factory=None, positioner_device_factory=None):
    if not authorize_hardware:
        raise ValueError("Explicit combined connection/write/status-consumption authorization required")
    if config.lockin is not None:
        if config.reference_topology == "pem_xy_xx_sine":
            reference_output = config.lockin.reference_output
            if (reference_output is None or reference_output.sample_connected is not False
                    or reference_output.destination != "lockin_xx_ref_in"
                    or config.lockin.lockin_xy.sine_output_connected is not True):
                raise ValueError("XY SINE reference must connect only to XX REF IN and be disconnected from the sample")
        elif not confirm_xy_sine_disconnected:
            raise ValueError("Confirm physical disconnection of XY SINE OUT before opening hardware")
    if config.cryostat is not None and not authorize_cryostat:
        raise ValueError("Separate cryostat temperature/field write authorization required")
    if config.optical is not None:
        if not authorize_optical or not confirm_optical_route:
            raise ValueError("Optical connection/settings/emission authorization and confirmed optical route required")
        config.optical.nkt.require_hardware(writes=True)
    station = HardwareCombinationStation(config, smu_adapter_factory=smu_adapter_factory,
                                           manager_factory=manager_factory, sleep=sleep,
                                           dll=dll, monotonic=monotonic,
                                           optical_backend_factory=optical_backend_factory,
                                           pem_factory=pem_factory, meter_factory=meter_factory,
                                           chopper_factory=chopper_factory,
                                           rotator_device_factory=rotator_device_factory,
                                           positioner_device_factory=positioner_device_factory)
    snapshot = {**config.snapshot, "authorization": {
        "combined_connection_writes_status_consumption": True,
        "xy_sine_disconnected": (False if config.reference_topology == "pem_xy_xx_sine" else confirm_xy_sine_disconnected),
        "xy_sine_reference_only": config.reference_topology == "pem_xy_xx_sine",
        "cryostat_connection_and_selected_axis_writes": authorize_cryostat,
        "optical_connection_settings_and_emission": authorize_optical,
        "optical_route_confirmed": confirm_optical_route,
        "positioner_connection_output_and_motion": getattr(config, "positioner", None) is not None}}
    return _run_combination(config.plan, store, run_id, station=station, snapshot=snapshot,
                            on_registered=on_registered)


# Compatibility for callers of the earlier electrical-only milestone.
ElectricalCombinationConfig = HardwareCombinationConfig
ElectricalCombinationStation = HardwareCombinationStation
load_electrical_combination = load_hardware_combination
run_electrical_combination = run_hardware_combination
