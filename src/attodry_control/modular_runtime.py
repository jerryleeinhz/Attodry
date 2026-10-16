"""Resolve module plans through the existing commissioned device contracts.

Construction and validation are offline. No unselected laser, detector or source
is constructed; the backend marker for an absent laser is configuration only.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
import tomllib

from .nkt_config import NktError, NKT_TABLES, OTHER_TABLES, load_nkt_config, number, integer, keys
from .photoresponse_hardware import load_electrical_hardware, identity, address
from .modular_environment import load_environment_axes


def json_config(value):
    if isinstance(value, (Path, Enum)):
        return str(value) if isinstance(value, Path) else value.value
    if isinstance(value, dict):
        return {k: json_config(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_config(v) for v in value]
    return value


@dataclass(frozen=True)
class SourceHardware:
    source: dict
    timeout_ms: int
    wiring_confirmed: bool

    def addresses(self):
        return {"6221": self.source["address"]}

    def validate(self, config):
        keys(self.source, "current_source", {"address", "expected_idn", "compliance_v", "max_step_a", "settle_s", "tolerance_a"})
        address(self.source["address"])
        identity(self.source["expected_idn"], "6221")
        integer(self.timeout_ms, "timeout_ms", 1000, 120000)
        if type(self.wiring_confirmed) is not bool:
            raise NktError("Current-source wiring confirmation must be boolean")
        number(config.max_abs_current_a, "6221 sample current limit", 1e-13, .105)
        number(self.source["compliance_v"], "6221 compliance_v", .1, min(105., config.max_abs_voltage_v))
        from .photoresponse_devices import CurrentSource6221
        CurrentSource6221(None, **{k: v for k, v in self.source.items() if k != "address"},
                          max_current_a=config.max_abs_current_a)

    def require_run(self, config):
        self.validate(config)
        if not self.wiring_confirmed:
            raise NktError("Confirm current-source wiring before hardware operation")


class RuntimeConfig:
    """Existing electrical/SMU adapters consume the same semantic properties."""
    def __init__(self, modular):
        self.modular, self.plan = modular, modular.plan
        doc, settings = modular.document, modular.document["combination_scan"]
        self.path = modular.source_path
        raw_mode = settings.get("backend", doc.get("project", {}).get("mode"))
        if raw_mode is None and modular.optics:
            raw_mode = (doc.get("nkt_run", {}).get("backend") if modular.optics["laser"] == "nkt"
                        else doc.get("cld1010", {}).get("backend"))
            raw_mode = "simulation" if raw_mode == "simulation" else "hardware" if raw_mode else None
        if raw_mode not in ("simulation", "hardware"):
            raise NktError("Specify combination_scan.backend explicitly")
        self.hardware_mode = raw_mode == "hardware"
        self.samples_per_window, self.repeats = self.plan.samples_per_condition, self.plan.repeats
        measurement = doc.get("measurement", doc.get("photoresponse", {}))
        self.sample_interval_s = number(measurement.get("sample_interval_s", .1), "sample_interval_s", .001, 3600)
        self.settle_s = number(measurement.get("settle_s", 0.), "settle_s", 0, 3600)
        self.dark_recovery_s = measurement.get("dark_recovery_s")
        if self.dark_recovery_s is not None:
            number(self.dark_recovery_s, "dark_recovery_s", 0, 3600)
        self.max_abs_voltage_v = measurement.get("max_abs_voltage_v")
        self.max_abs_current_a = measurement.get("max_abs_current_a")
        self.detector_selected = "nanovoltmeter" in self.plan.order
        self.detector = doc.get("nanovoltmeter", {}).get("model", "keithley2182a") if self.detector_selected else "none"
        if self.detector_selected and self.detector not in ("keithley2182a", "keithley2400"):
            raise NktError("Unsupported nanovoltmeter model")
        self.mode = "dc_current_voltage" if "current_source" in self.plan.order else "open_circuit_voltage"
        self.quantity = "voltage_v" if self.detector_selected else None
        self.excitation = "dc_current" if "current_source" in self.plan.order else "none"
        self.bias_points = tuple(p.values["bias_current_a"] for p in self.axis("current_source").points) if "current_source" in self.plan.order else (None,)
        self.hardware = self.power = self.smu = self.pem = None
        self.environment = load_environment_axes(doc, self.plan.order, hardware=self.hardware_mode)
        directory = settings.get("output_directory")
        if directory is None:
            directory = doc.get("nkt_run", {}).get("output_directory", doc.get("cld1010", {}).get("output_directory"))
        if directory is None:
            raise NktError("Specify combination_scan.output_directory")
        self.output_directory = self.resolve(directory)
        database = settings.get("database_path", doc.get("photoresponse_storage", {}).get("database_path"))
        self.database_path = self.resolve(database) if database else self.output_directory / "audit.sqlite"
        self.nkt = self._laser_config() if modular.optics else SimpleNamespace(
            backend="simulation" if not self.hardware_mode else "unselected", port="")
        if modular.optics and modular.optics["use_pem"]:
            from .pem_config import load_pem_config
            self.pem = load_pem_config(doc)
            if self.pem is None:
                raise NktError("Selected PEM requires its explicit device configuration")
            if (self.pem.backend != "simulation") != self.hardware_mode:
                raise NktError("PEM backend differs from combination backend")
            waves = modular.optics["peak_retardance_waves"]
            for point in self.axis("optics").points:
                self.pem.target(point.values.get("optical_wavelength_nm"), waves)
        if self.detector_selected:
            number(self.max_abs_voltage_v, "measurement.max_abs_voltage_v", 1e-15, 1e6)
        if self.excitation != "none":
            number(self.max_abs_current_a, "measurement.max_abs_current_a", 1e-13, .105)
            number(self.max_abs_voltage_v, "measurement.max_abs_voltage_v", .1 if self.hardware_mode else 1e-15, 105)
        self._electrical_config()
        self._smu_config()
        self.validate()

    def resolve(self, value):
        p = Path(value)
        return p.resolve() if p.is_absolute() else (self.path.parent / p).resolve()

    def axis(self, module):
        return next(axis for axis in self.plan.axes if axis.module == module)

    def _laser_config(self):
        doc = deepcopy(self.modular.document)
        axis = self.axis("optics")
        if self.modular.optics["laser"] == "nkt":
            points = []
            for point in axis.points:
                p = {k.removeprefix("optical_"): v for k, v in point.values.items()
                     if k in {"optical_source_level_pct", "optical_wavelength_nm", "optical_bandwidth_nm",
                              "optical_nd_pct", "optical_pulse_picker_ratio"}}
                # Every optical point has its own identity, including dark.
                points.append(p)
            doc.setdefault("nkt_run", {})["points"] = points
            doc["nkt_run"].update(output_directory=str(self.output_directory), run_name=self.plan.run_name, note=self.plan.note)
            normalized = {k: v for k, v in doc.items() if k in NKT_TABLES | OTHER_TABLES}
            laser = load_nkt_config(self.path, document=normalized)
        else:
            from .photoresponse_cld import load_cld_config
            currents = [p.values["optical_current_a"] for p in axis.points]
            raw = doc.setdefault("cld1010", {})
            raw.update(current_points_a=currents, wavelength_nm=axis.points[0].values["optical_wavelength_nm"],
                       output_directory=str(self.output_directory), run_name=self.plan.run_name, note=self.plan.note)
            laser = load_cld_config(doc, self.path)
        if (laser.backend != "simulation") != self.hardware_mode:
            raise NktError("Laser backend differs from combination backend")
        if not laser.emit or laser.dwell_s != 0:
            raise NktError("Combination optics requires emit=true and device dwell_s=0; use measurement.settle_s")
        if self.modular.optics["use_power_meter"]:
            from .power_config import load_photoresponse_power
            # Resolve the original power-coordinate identities across the whole
            # expanded plan. A paired grid need not expose every power in its
            # first wavelength group; duplicate numeric targets stay distinct.
            dark_first = any(p.metadata["optical_state"] == "dark" for p in axis.points)
            by_power_index = {}
            for point in axis.points:
                if point.metadata["optical_state"] != "light":
                    continue
                index = point.metadata["power_index"] - int(dark_first)
                value = point.values["optical_target_power_w"]
                if index in by_power_index and by_power_index[index] != value:
                    raise NktError("One optical power index cannot name different target powers")
                by_power_index[index] = value
            if set(by_power_index) != set(range(len(by_power_index))):
                raise NktError("Optical power indices must preserve the complete ordered target coordinate")
            targets = [by_power_index[i] for i in range(len(by_power_index))]
            feedback = deepcopy(doc.get("power_feedback", {}))
            if any(k in feedback for k in ("target_power_w", "target_powers_w", "power_points")):
                raise NktError("Define target powers only in optics_scan, not power_feedback")
            if dark_first:
                feedback["power_points"] = ["dark", *targets]
            else:
                feedback["target_powers_w"] = targets
            self.power = load_photoresponse_power({"pm100d": doc.get("pm100d", {}), "power_feedback": feedback})
            self.power.validate(laser)
        return laser

    def _electrical_config(self):
        if not self.hardware_mode:
            if self.detector_selected:
                self._validate_simulated_detector()
            return
        doc = deepcopy(self.modular.document)
        source = doc.get("current_source", {})
        if source:
            source = deepcopy(source)
            if source.pop("model", "keithley6221") != "keithley6221":
                raise NktError("current_source only supports keithley6221")
            if "photoresponse_6221" in doc:
                raise NktError("Define current_source or photoresponse_6221, not both")
            doc["photoresponse_6221"] = (source if self.excitation == "dc_current" else
                                         {k: v for k, v in source.items() if k in {"address", "expected_idn"}})
        if self.detector_selected:
            raw = deepcopy(doc.get("nanovoltmeter", {}))
            raw.pop("model", None)
            old = "photoresponse_2182" if self.detector == "keithley2182a" else "photoresponse_2400"
            if raw and old in doc:
                raise NktError("Define nanovoltmeter or legacy detector settings, not both")
            if raw:
                doc[old] = raw
            self.hardware = load_electrical_hardware(doc, self)
        elif self.excitation == "dc_current":
            raw = doc.get("photoresponse_6221", {})
            keys(raw, "current_source", {"address", "expected_idn", "compliance_v", "max_step_a", "settle_s", "tolerance_a"})
            address(raw["address"])
            identity(raw["expected_idn"], "6221")
            h = doc.get("photoresponse_hardware", {})
            timeout = integer(h.get("timeout_ms"), "timeout_ms", 1000, 120000)
            self.hardware = SourceHardware(raw, timeout, h.get("wiring_confirmed", False))
            self.hardware.validate(self)

    def _validate_simulated_detector(self):
        """Exercise the same setting contract without requiring physical IDs."""
        raw = deepcopy(self.modular.document.get("nanovoltmeter", {}))
        raw.pop("model", None)
        raw.pop("address", None)
        if self.detector == "keithley2182a":
            optional = {"expected_idn", "voltage_range_v", "nplc", "channel", "analog_lowpass_enabled",
                        "autozero", "front_autozero", "digital_filter"}
            keys(raw, "nanovoltmeter", set(), optional)
            from .photoresponse_devices import Voltmeter2182
            settings = {"expected_idn": "SIMULATED,MODEL 2182A,NONE,1", "voltage_range_v": 10., "nplc": 1., **raw}
            Voltmeter2182(None, **settings, max_voltage_v=self.max_abs_voltage_v)
        else:
            optional = {"expected_idn", "voltage_range_v", "current_range_a", "nplc", "compliance",
                        "max_step_v", "settle_s", "tolerance_v"}
            keys(raw, "nanovoltmeter", set(), optional)
            from .photoresponse_devices import Meter2400
            settings = {"expected_idn": "SIMULATED,MODEL 2400,NONE,1", "voltage_range_v": 20.,
                        "current_range_a": 1e-6, "nplc": 1., "compliance": max(.02, self.max_abs_voltage_v*2),
                        "max_step_v": min(.01, self.max_abs_voltage_v), "settle_s": 0., "tolerance_v": 0., **raw}
            Meter2400(None, **settings, current_mode=False, max_voltage_v=self.max_abs_voltage_v,
                      max_current_a=self.max_abs_current_a)

    def _smu_config(self):
        if "smu" not in self.plan.order:
            return
        from .photoresponse_smu import PhotoresponseSmuConfig
        from .photonic_smu.three_smu_config import load_three_smu_operation_config, SourceMode, ScanPoint, SEMANTIC_ROLES
        # Reuse the pinned loader exactly; reject a changed/missing source file
        # rather than secretly parsing a second set of hardware limits.
        try:
            with self.path.open("rb") as stream:
                current = tomllib.load(stream)
        except (OSError, ValueError) as exc:
            raise NktError("SMU runtime requires its unchanged source TOML") from exc
        for table in ("three_smu_run", *SEMANTIC_ROLES):
            if current.get(table) != self.modular.document.get(table):
                raise NktError("SMU source TOML differs from the parsed module configuration")
        operation = load_three_smu_operation_config(self.path)
        selected = self.modular.document.get("photoresponse_smu", {})
        # The module list owns activation; station table retains wiring and IDs.
        points = []
        for i, point in enumerate(self.axis("smu").points):
            values = {}
            for key, value in point.values.items():
                role, unit = key.rsplit("_", 1)
                h = operation.hardware.require_role(role)
                expected_unit = "v" if h.source_mode is SourceMode.VOLTAGE else "a"
                if unit != expected_unit:
                    raise NktError("SMU scan unit differs from configured source mode")
                bound = h.max_abs_voltage_v if unit == "v" else h.max_abs_current_a
                number(value, key, -bound, bound)
                values[role] = value
            points.append(ScanPoint(i, point.segment, values))
        self.smu = SelectedSmuConfig(operation.hardware, operation.plan,
            selected.get("wiring_confirmed", False), selected.get("expected_idn"), tuple(points))
        if "smu_bias" in self.smu.roles:
            if self.excitation != "none":
                raise NktError("Two sample excitation sources cannot be combined")
            number(self.max_abs_voltage_v, "SMU sample voltage bound", 1e-15, 210.)
            number(self.max_abs_current_a, "SMU sample current bound", 1e-15, 1.05)
            h = self.smu.hardware.require_role("smu_bias")
            self.mode = "smu_current_voltage" if h.source_mode is SourceMode.CURRENT else "smu_voltage_voltage"
            self.excitation = "smu_current" if h.source_mode is SourceMode.CURRENT else "smu_voltage"

    def validate(self):
        self.plan.validate()
        if self.excitation == "dc_current":
            for value in self.bias_points:
                number(value, "current source coordinate", -self.max_abs_current_a, self.max_abs_current_a)
        if self.modular.optics:
            self.nkt.validate()
        if self.hardware:
            if hasattr(self.hardware, "validate"):
                self.hardware.validate(self)
        if self.smu:
            self.smu.validate(self)
            expected = set(self.smu.roles)
            if any(set(point.coordinates) != expected for point in self.smu.points):
                raise NktError("Every SMU point must specify exactly the active roles")
        from .photoresponse_smu import validate_selected_resources
        validate_selected_resources(self)
        if self.pem and self.pem.port:
            from .photoresponse_smu import normalized_resource
            owned = dict(self.hardware.addresses()) if self.hardware else {}
            owned["laser"] = getattr(self.nkt, "address", None) or getattr(self.nkt, "port", "")
            if self.power:
                owned["pm100d"] = self.power.meter.resource
            if self.environment:
                owned["environment"] = self.environment.cryostat.com_port
            if self.smu:
                owned.update({role: self.smu.hardware.require_role(role).address for role in self.smu.roles})
            if any(value and normalized_resource(value) == normalized_resource(self.pem.port) for value in owned.values()):
                raise NktError("PEM cannot share a resource with another selected module")
        if self.environment:
            for axis in self.plan.axes:
                if axis.module == "temperature":
                    for point in axis.points:
                        number(point.values["temperature_k"], "temperature", self.environment.cryostat.temperature_min_k,
                               self.environment.cryostat.temperature_max_k)

    @property
    def shared_dark(self):
        return bool(self.modular.optics and any(p.metadata["optical_state"] == "dark" for p in self.axis("optics").points))

    def require_run(self):
        self.validate()
        if self.hardware:
            self.hardware.require_run(self)
        if self.modular.optics:
            self.nkt.require_hardware(writes=True)
        if self.power:
            self.power.meter.require_hardware(writes=True)
        if self.pem:
            self.pem.require_hardware(writes=True)
        if self.smu:
            self.smu.require_hardware()

    def snapshot(self):
        result = {"mode": self.mode, "detector": self.detector, "excitation": self.excitation,
            "quantity": self.quantity, "samples_per_window": self.samples_per_window, "repeats": self.repeats,
            "sample_interval_s": self.sample_interval_s, "settle_s": self.settle_s,
            "dark_recovery_s": self.dark_recovery_s, "max_abs_voltage_v": self.max_abs_voltage_v,
            "max_abs_current_a": self.max_abs_current_a, "bias_points": self.bias_points,
            "nkt": self.nkt.snapshot() if self.modular.optics else None,
            "power": asdict(self.power) if self.power else None,
            "pem": asdict(self.pem) if self.pem else None,
            "hardware": asdict(self.hardware) if self.hardware else None,
            "environment": self.environment.snapshot() if self.environment else None,
            "smu": self.smu.snapshot() if self.smu else None,
            "database_path": str(self.database_path), "combination": self.plan.snapshot(),
            "modular": self.modular.snapshot()}
        return json_config(result)


from .photoresponse_smu import PhotoresponseSmuConfig


@dataclass(frozen=True)
class SelectedSmuConfig(PhotoresponseSmuConfig):
    selected_points: tuple = ()

    @property
    def points(self):
        return self.selected_points

    def validate(self, photo):
        if photo.detector == "none" and "smu_bias" in self.roles:
            # The legacy optical workflow requires a separate 2182A. A freely
            # selected source-only module acquires its own SMU readbacks instead.
            # Reuse all source-mode, sample-limit, ramp and finish validation;
            # only the irrelevant external-detector requirement is discharged.
            validation = SimpleNamespace(mode=photo.mode, detector="keithley2182a",
                max_abs_voltage_v=photo.max_abs_voltage_v, max_abs_current_a=photo.max_abs_current_a)
            return super().validate(validation)
        return super().validate(photo)
