"""Gated four-module point execution; no I/O until explicit run authorization."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime
from enum import Enum
import hashlib
from pathlib import Path
from types import SimpleNamespace

from . import config as configuration
from .combination_scan import AxisPoint, ScanAxis, CombinationPlan, _audit_value, _run_combination
from .combination_store import utc_now
from .lockin_points import LockinPointSession
from .models import LockinRole
from .config import RunMode
from .three_smu import ThreeSmuSession
from .three_smu_config import load_three_smu_operation_config, validate_plan_targets, SourceMode, ScanMode
from .cryostat_points import CryostatPointSession
from .models import VectorField
from .safety import plan_ordered_field_transitions


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


def load_hardware_combination(path: str | Path) -> HardwareCombinationConfig:
    """Reuse standalone grids/limits; do not parse or access inactive modules."""
    path = Path(path).resolve()
    document = configuration._load_document(path)
    known = {"project", "cryostat", "magnet", "temperature_stability", "temperature_run",
             "temperature_scan", "temperature_excitation_scan", "magnetic_field_run",
             "cleanup", "visa", "lockin_xx", "lockin_xy", "lockin_sweep",
             "smu_bias", "gate_top", "gate_bottom", "three_smu_run", "combination_scan"}
    if set(document) - known:
        raise ValueError("Unknown top-level combination configuration table")
    project = configuration._parse_project(configuration._table(document, "project"))
    if project.mode is not RunMode.HARDWARE:
        raise ValueError("Hardware combination requires project.mode='hardware'")
    table = configuration._table(document, "combination_scan")
    configuration._strict_keys(
        table, "combination_scan",
        {"backend", "order", "samples_per_condition", "repeats", "run_name", "note"})
    if table["backend"] != "hardware":
        raise ValueError("Hardware combination requires backend='hardware'")
    order = table["order"]
    if (not isinstance(order, list) or not order
            or not all(isinstance(m, str) and m in {"temperature", "magnetic", "smu", "lockin"} for m in order)
            or len(set(order)) != len(order)):
        raise ValueError("Hardware order requires distinct temperature/magnetic/smu/lockin axes")
    axes = {}
    smu = lockin = None
    hardware = {}
    temperature = magnetic = cryostat = None
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
            qualification="fresh_dwell_after_all_axis_settings", pre_measure_wait_s_used=False)
    if "magnetic" in order:
        magnetic = configuration.load_magnetic_field_operation_config(path)
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
        effective = replace(limits, hardware_x_max_t=min(limits.hardware_x_max_t, 3.0),
                            hardware_z_max_t=min(limits.hardware_z_max_t, 3.0))
        cryostat = SimpleNamespace(project=project, cryostat=selected.cryostat,
            magnet=replace(selected.magnet, limits=effective),
            temperature_stability=(temperature.temperature_stability if temperature is not None
                                   else selected.magnet.stability))
        hardware["effective_cryostat"] = {name: asdict(value) for name, value in vars(cryostat).items()}
        if magnetic is not None:
            # Offline check exact float32 endpoints and every component corner
            # under the universal integrated envelope, not standalone pure-Z 9T.
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
    if "lockin" in order:
        safety_path = path.with_name("lockin_safety.toml")
        safety = configuration._parse_lockin_safety(configuration._load_document(safety_path))
        xx = configuration._parse_lockin(configuration._table(document, "lockin_xx"),
                                        LockinRole.XX, "lockin_xx", safety.lockin_xx, safety)
        xy = configuration._parse_lockin(configuration._table(document, "lockin_xy"),
                                        LockinRole.XY, "lockin_xy", safety.lockin_xy, safety)
        configuration._validate_lockin_pair(xx, xy)
        sweep = configuration._parse_lockin_sweep(configuration._table(document, "lockin_sweep"),
                                                  safety, xx, xy)
        lockin = SimpleNamespace(
            lockin_xx=xx, lockin_xy=xy, lockin_safety=safety, lockin_sweep=sweep,
            visa=configuration._parse_visa(configuration._table(document, "visa")))
        prepared = LockinPointSession(path, lockin, lambda *_: None)
        axes["lockin"] = ScanAxis("lockin", tuple(AxisPoint({
            "lockin_excitation_v_rms": spec.value, "lockin_frequency_hz": xx.frequency_hz},
            str(spec.segment_index) if spec.segment_index is not None else "main",
            "ascending") for spec in prepared.args.point_specs))
        hardware["lockin"] = {name: asdict(getattr(lockin, name)) for name in vars(lockin)}
        hardware["lockin"]["safety_sha256"] = hashlib.sha256(safety_path.read_bytes()).hexdigest()
    addresses = []
    if smu is not None:
        addresses += [h.address.strip().upper() for h in smu.hardware.by_role().values()]
    if lockin is not None:
        addresses += [lockin.lockin_xx.address.strip().upper(), lockin.lockin_xy.address.strip().upper()]
    if len(set(addresses)) != len(addresses):
        raise ValueError("All active resources, including SMU and SR830 roles, must be distinct")
    plan = CombinationPlan(tuple(axes[m] for m in order), table["samples_per_condition"],
                           table["repeats"], table["run_name"], table["note"])
    snapshot = plan.snapshot()
    snapshot.update(mode="hardware", hardware_scope="four-module-excitation-v1",
                    hardware=_json(hardware), config_path=str(path),
                    config_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    cleanup_policy={"lockin": "4mV_restore_ranges_h1", "smu": "zero_disable",
                        "temperature": "normal_hold_failure_disable",
                        "magnetic": (magnetic.cleanup.normal_end_field_policy.value if magnetic else "inactive"),
                        "magnetic_failure": "monitored_zero_or_manual_verification"},
                    resume_supported=False)
    # Include point engines and shared safety code, not just the Cartesian loop.
    snapshot["source_sha256"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in Path(__file__).parent.glob("*.py")}
    return HardwareCombinationConfig(path, plan, smu, lockin, snapshot, temperature, magnetic, cryostat)


class _Events:
    def __init__(self, sink):
        self.sink = sink
        self.cleaning = False
        self.errors = []

    def event(self, kind, payload):
        try:
            self.sink(kind, _json(payload))
        except BaseException as exc:
            if not self.cleaning:
                raise
            # Disk/audit failure must never prevent later safety actions.
            self.errors.append(f"{kind}: {type(exc).__name__}: {exc}")


class HardwareCombinationStation:
    def __init__(self, config, *, smu_adapter_factory=None, manager_factory=None, sleep=None,
                 dll=None, monotonic=None):
        self.config = config
        self.smu_adapter_factory = smu_adapter_factory
        self.manager_factory = manager_factory
        self.sleep = sleep
        self.smu = self.lockin = None
        self.point = None
        self.open_completed = False
        self.cryo = None
        self.dll, self.monotonic = dll, monotonic

    def set_event_sink(self, sink):
        self.events = _Events(sink)

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
            self.smu = ThreeSmuSession.open(
                self.config.smu.hardware, self.config.smu.plan, authorize_writes=True,
                authorize_status_consumption=True,
                on_preflight=lambda role, state: self.events.event(
                    "smu_preflight_role", {"role": role, "state": asdict(state)}), **kwargs)
            preflight["smu"] = {r: asdict(v) for r, v in self.smu.preflight.items()}
            self.events.event("smu_preflight", preflight["smu"])
        if self.config.lockin is not None:
            self.lockin = LockinPointSession(
                self.config.path, self.config.lockin, self.events.event,
                manager_factory=self.manager_factory)
            preflight["lockin"] = self.lockin.open()
        # All active preflights must succeed before any configuration writes.
        if self.lockin is not None:
            self.lockin.configure()
        if self.smu is not None:
            self.smu.begin_points(self.events)
        self.open_completed = True
        return _json({"mode": "hardware", "identities_and_initial_state": preflight})

    def apply(self, module, point):
        if module in {"temperature", "magnetic"}:
            return self.cryo.apply(module, point)
        if module == "smu":
            self.point = point
            self.smu.set_point({key[:-2]: v for key, v in point.values.items()},
                               segment=point.segment)
            # Actual sensed coordinates are produced only by the formal read.
            return {"requested": point.values}
        return {"actual": self.lockin.set_point(point.values["lockin_excitation_v_rms"])}

    def qualify(self, modules):
        if self.lockin is not None:
            self.lockin.qualify()
        if self.cryo is not None:
            self.cryo.qualify()
        return {"ready": True, "mode": "hardware", "active_modules": modules}

    def begin_sample(self):
        if self.cryo is not None:
            self.cryo.begin_sample()

    def end_sample(self, reads):
        if self.cryo is not None:
            self.cryo.end_sample(reads)

    def read(self, module):
        if module in {"temperature", "magnetic"}:
            return self.cryo.read(module)
        if module == "lockin":
            return self.lockin.sample_point(
                measurement_context=self.cryo.capture if self.cryo is not None else None)
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
        if self.config.smu is not None:
            for role in self.config.smu.hardware.by_role():
                expected.update({role + "_voltage_v", role + "_current_a"})
        if self.lockin is not None:
            for role, harmonics in self.lockin.harmonics_by_role.items():
                expected.update(f"lockin_{role}_h{h}_{metric}" for h in harmonics
                                for metric in ("x_v", "y_v", "amplitude_v", "phase_deg"))
        return expected

    def cleanup(self, module, failed):
        self.events.cleaning = True
        if module == "lockin" and self.lockin is not None:
            result = self.lockin.cleanup()
            clean = result["verified"]
        elif module == "smu" and self.smu is not None:
            result = self.smu.cleanup_points(self.events, reason="failed" if failed else "completed")
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
        for device in (self.lockin, self.smu, self.cryo):
            if device is not None:
                try:
                    device.close()
                except BaseException as exc:
                    errors.append(f"{type(exc).__name__}: {exc}")
        if errors:
            raise OSError("; ".join(errors))


def run_hardware_combination(config, store, run_id, *, authorize_hardware=False,
                               confirm_xy_sine_disconnected=False,
                               authorize_cryostat=False, dll=None, monotonic=None,
                               smu_adapter_factory=None, manager_factory=None, sleep=None):
    if not authorize_hardware:
        raise ValueError("Explicit combined connection/write/status-consumption authorization required")
    if config.lockin is not None and not confirm_xy_sine_disconnected:
        raise ValueError("Confirm physical disconnection of XY SINE OUT before opening hardware")
    if config.cryostat is not None and not authorize_cryostat:
        raise ValueError("Separate cryostat temperature/field write authorization required")
    station = HardwareCombinationStation(config, smu_adapter_factory=smu_adapter_factory,
                                           manager_factory=manager_factory, sleep=sleep,
                                           dll=dll, monotonic=monotonic)
    snapshot = {**config.snapshot, "authorization": {
        "combined_connection_writes_status_consumption": True,
        "xy_sine_disconnected": confirm_xy_sine_disconnected,
        "cryostat_connection_and_selected_axis_writes": authorize_cryostat}}
    return _run_combination(config.plan, store, run_id, station=station, snapshot=snapshot)


# Compatibility for callers of the earlier electrical-only milestone.
ElectricalCombinationConfig = HardwareCombinationConfig
ElectricalCombinationStation = HardwareCombinationStation
load_electrical_combination = load_hardware_combination
run_electrical_combination = run_hardware_combination
