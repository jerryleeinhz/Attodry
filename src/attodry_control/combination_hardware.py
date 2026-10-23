"""Gated four-module point execution; no I/O until explicit run authorization."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace, is_dataclass
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
from .safety import MagnetLimits, plan_ordered_field_transitions


def _json(value):
    if is_dataclass(value) and not isinstance(value, type):
        return _json(asdict(value))
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
    lockin_range_schedule: object | None = None


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
    configuration._strict_keys_with_optional(
        table, "combination_scan",
        {"backend", "order", "samples_per_condition", "repeats", "run_name", "note"},
        {"lockin_mode", "run_id", "lockin_range_rules"})
    mode = table.get("lockin_mode", "excitation")
    if not isinstance(mode, str) or mode not in {"excitation", "frequency", "frequency_excitation"}:
        raise ValueError("combination_scan.lockin_mode must be excitation, frequency or frequency_excitation")
    run_id = table.get("run_id", "auto")
    if not isinstance(run_id, str) or not run_id.strip() or run_id != run_id.strip():
        raise ValueError("combination_scan.run_id must be a nonempty string without outer whitespace")
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
            qualification="fresh_dwell_per_axis_entry", formal_temperature_policy="record_with_quality_flags",
            pre_measure_wait_s_used=False)
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
        # A selected magnetic plan supplies its complete-plan axis policy.
        # Temperature-only operation keeps the existing ambient 3 T envelope.
        effective = (limits if magnetic is not None else
                     replace(limits, hardware_z_max_t=min(limits.hardware_z_max_t, 3.0)))
        cryostat = SimpleNamespace(project=project, cryostat=selected.cryostat,
            magnet=replace(selected.magnet, limits=effective),
            temperature_stability=(temperature.temperature_stability if temperature is not None
                                   else selected.magnet.stability))
        hardware["effective_cryostat"] = {name: asdict(value) for name, value in vars(cryostat).items()}
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
        prepared = LockinPointSession(path, lockin, lambda *_: None, mode=mode)
        axes["lockin"] = ScanAxis("lockin", tuple(AxisPoint({
            "lockin_excitation_v_rms": point.source_v_rms, "lockin_frequency_hz": point.frequency_hz},
            (f"f{point.frequency_segment}:e{point.excitation_segment}" if mode != "excitation"
             else str(point.excitation_segment) if point.excitation_segment is not None else "main"),
            "ascending", point.metadata()) for point in prepared.grid))
        hardware["lockin"] = {name: asdict(getattr(lockin, name)) for name in vars(lockin)}
        hardware["lockin"]["safety_sha256"] = hashlib.sha256(safety_path.read_bytes()).hexdigest()
        hardware["lockin"]["point_mode"] = mode
        from .lockin_settling import SETTLING_POLICY
        hardware["lockin"]["settling_policy"] = SETTLING_POLICY
        hardware["lockin"]["harmonics_by_role"] = prepared.harmonics_by_role
        hardware["lockin"]["skipped_harmonics_by_frequency"] = prepared.skipped_by_frequency
    addresses = []
    if smu is not None:
        addresses += [h.address.strip().upper() for h in smu.hardware.by_role().values()]
    if lockin is not None:
        addresses += [lockin.lockin_xx.address.strip().upper(), lockin.lockin_xy.address.strip().upper()]
    if len(set(addresses)) != len(addresses):
        raise ValueError("All active resources, including SMU and SR830 roles, must be distinct")
    plan = CombinationPlan(tuple(axes[m] for m in order), table["samples_per_condition"],
                           table["repeats"], table["run_name"], table["note"],
                           magnet_limits=cryostat.magnet.limits if cryostat else MagnetLimits(),
                           readback_tolerance_t=cryostat.magnet.readback_tolerance_t if cryostat else None)
    range_schedule = None
    if "lockin_range_rules" in table:
        from .lockin_backend import capabilities_for
        from .lockin_range_rules import compile_lockin_range_rules
        if lockin is None:
            raise ValueError("lockin_range_rules requires an active lockin axis")
        allowed = {}
        for role in ("lockin_xx", "lockin_xy"):
            device = getattr(lockin, role)
            if device.sensitivity_mode is not configuration.SensitivityMode.FIXED or device.harmonic_settings:
                raise ValueError("lockin_range_rules cannot combine with auto/per-harmonic sensitivity")
            allowed[role] = set(capabilities_for(device.model).sensitivity_full_scales_v).intersection(
                getattr(lockin.lockin_safety, role).allowed_fixed_full_scales_v)
        if any(point.xx_full_scale_v is not None or point.xy_full_scale_v is not None
               for name in ("frequency_point_specs", "excitation_point_specs")
               for point in getattr(lockin.lockin_sweep, name)):
            raise ValueError("lockin_range_rules cannot combine with segment sensitivity overrides")
        range_schedule = compile_lockin_range_rules(table["lockin_range_rules"], plan, allowed)
    snapshot = plan.snapshot()
    if range_schedule is not None:
        snapshot["lockin_range_rules"] = {
            **range_schedule.snapshot(), "condition_bindings": range_schedule.condition_preview(plan)}
    snapshot.update(mode="hardware", hardware_scope="four-module-lockin-grid-v2",
                    lockin_mode=mode,
                    hardware=_json(hardware), config_path=str(path),
                    config_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    cleanup_policy={"lockin": ("4mV_restore_ranges_h1" if mode == "excitation"
                        else "4mV_h1_restore_baseline_frequency_ranges_reserve"), "smu": "zero_disable",
                        "temperature": "normal_hold_failure_disable",
                        "magnetic": (magnetic.cleanup.normal_end_field_policy.value if magnetic else "inactive"),
                        "magnetic_failure": "monitored_zero_or_manual_verification"},
                    resume_supported=True,
                    hardware_resume_contract="whole-condition-reviewed-executions-v1")
    # Include point engines and shared safety code, not just the Cartesian loop.
    snapshot["source_sha256"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in Path(__file__).parent.glob("*.py")}
    if lockin is not None:
        from .lockin_test import SWEEP_FREQUENCY_REL_TOLERANCE, SWEEP_FREQUENCY_ABS_TOLERANCE_HZ
        snapshot["lockin_frequency_readback_policy"] = {
            "relative_tolerance": SWEEP_FREQUENCY_REL_TOLERANCE,
            "absolute_tolerance_hz": SWEEP_FREQUENCY_ABS_TOLERANCE_HZ,
            "applies_to": ["point_coordinates", "formal_samples", "cleanup"],
            "lock_status_required": True,
        }
    database = project.database_path
    if not database.is_absolute():
        database = path.parent / database
    return HardwareCombinationConfig(path, plan, smu, lockin, snapshot, temperature, magnetic,
                                     cryostat, database.resolve(), run_id, mode, range_schedule)


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
                 dll=None, monotonic=None, authorize_hardware=False, recovery=None,
                 recovery_owner_guard=None):
        self.config = config
        self.authorize_hardware = authorize_hardware is True
        self.smu_adapter_factory = smu_adapter_factory
        self.manager_factory = manager_factory
        self.sleep = sleep
        self.smu = self.lockin = None
        self.point = None
        self.open_completed = False
        self.cryo = None
        self.dll, self.monotonic = dll, monotonic
        self.recovery = recovery
        # Kept as an ignored constructor argument for older callers. Process
        # inventories, Jupyter identities, GUI checks and ownership leases no
        # longer participate in acquisition or cleanup.

    def set_event_sink(self, sink):
        self.events = _Events(sink)

    def open(self, modules):
        if modules != tuple(a.module for a in self.config.plan.axes):
            raise ValueError("Station modules differ from validated plan")
        if (self.config.lockin is not None
                and self.config.lockin.lockin_xy.model == "SR865A"
                and not self.authorize_hardware):
            raise ValueError("Mixed lock-in hardware requires explicit run authorization before any instrument I/O")
        preflight = {}
        if self.config.cryostat is not None:
            self.cryo = CryostatPointSession(self.config.cryostat, self.config.temperature,
                self.config.magnetic, self.events.event, dll=self.dll,
                monotonic=self.monotonic, sleep=self.sleep)
            preflight["cryostat"] = self.cryo.open()
            if self.recovery is not None:
                self._validate_recovery_environment()
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
            if self.recovery is not None:
                self.smu.verify_recovery_zero_off(self.events)
        if self.config.lockin is not None:
            self.lockin = LockinPointSession(
                self.config.path, self.config.lockin, self.events.event,
                manager_factory=self.manager_factory, mode=self.config.lockin_mode,
                authorize_writes=self.authorize_hardware, monotonic=self.monotonic, sleep=self.sleep)
            preflight["lockin"] = (self.lockin.open(recovery=True) if self.recovery is not None
                                   else self.lockin.open())
        # All active preflights must succeed before any configuration writes.
        if self.lockin is not None:
            if self.recovery is not None:
                self.lockin.configure(recovery=True,
                    cleanup_only=bool(self.recovery.get("cleanup_only")))
            else:
                self.lockin.configure()
        if self.smu is not None:
            if self.recovery and self.recovery.get("cleanup_only"):
                self.smu.prepare_recovery_cleanup(self.events)
            else:
                self.smu.begin_points(self.events)
        self.open_completed = True
        if self.recovery is not None:
            self.events.event("recovery_preflight_verified", {
                "identities_and_initial_state": preflight,
                "cleanup_only": bool(self.recovery.get("cleanup_only")),
                "fresh_queries_required": True,
                "qualified_baselines_reused": False})
        return _json({"mode": "hardware", "identities_and_initial_state": preflight})

    def _validate_recovery_environment(self):
        """Validate present readbacks and the pending path before electrical writes."""
        from .models import VectorField
        state = self.cryo.driver.last_confirmed_state
        if state is None:
            raise ValueError("Recovery requires a complete current cryostat state")
        self.events.event("recovery_environment_initial", {"state": asdict(state)})
        # Recovery retains the operator's universal 3 T ceiling. Ordinary runs
        # keep their existing complete-plan single-axis policy.
        for field in (state.field, state.field_setpoint):
            if field.magnitude_t > 3.0:
                raise ValueError("Recovery actual field or setpoint exceeds 3 T")
        conditions = self.config.plan.conditions()
        pending = self.recovery.get("next_condition")
        if pending is None and self.recovery.get("cleanup_only") and conditions:
            pending = conditions[-1]
        if pending is None:
            raise ValueError("Recovery has no validated pending/last condition")
        sequence = pending["sequence_index"]
        if not 0 <= sequence < len(conditions) or pending != conditions[sequence]:
            raise ValueError("Recovery condition differs from validated plan")
        if self.config.temperature is not None:
            request = self.config.temperature.temperature_run
            target = pending["requested"]["temperature_k"]
            if abs(target - state.sample_temperature_k) > request.max_delta_k:
                raise ValueError("Recovery temperature movement exceeds max_delta_k")
            ceiling = max(target, state.sample_temperature_k) + request.max_overshoot_k
            if ceiling > self.config.cryostat.cryostat.temperature_max_k:
                raise ValueError("Recovery temperature transition ceiling exceeds maximum")
        if self.config.magnetic is not None:
            targets = tuple(VectorField(c["requested"]["field_x_t"], c["requested"]["field_z_t"])
                            for c in conditions[sequence:])
            limits = self.config.cryostat.magnet.limits
            limits = replace(limits, hardware_x_max_t=min(limits.hardware_x_max_t, 3.0),
                             hardware_z_max_t=min(limits.hardware_z_max_t, 3.0),
                             experiment_vector_max_t=min(limits.experiment_vector_max_t, 3.0))
            # Check both actual and command-register starts. The driver repeats
            # this planning against fresh reads immediately before a field write.
            for initial in (state.field, state.field_setpoint):
                plans = plan_ordered_field_transitions(initial, targets,
                    self.config.magnetic.run.max_step_t,
                    self.config.magnetic.run.transition_policy, limits)
                self.events.event("recovery_magnetic_path_verified", {
                    "start": asdict(initial), "target_count": len(targets),
                    "transition_count": len(plans), "maximum_vector_t": 3.0})

    def prepare_cleanup_recovery(self, last_condition):
        """Re-establish the final accepted environment without formal sampling."""
        if not self.recovery or not self.recovery.get("cleanup_only"):
            raise ValueError("Cleanup recovery was not selected")
        if last_condition != self.config.plan.conditions()[-1]:
            raise ValueError("Cleanup recovery requires the last accepted condition")
        for axis in self.config.plan.axes:
            if axis.module in {"temperature", "magnetic"}:
                self.apply(axis.module, axis.points[last_condition["axes"][axis.module]["index"]])
        if self.cryo is not None:
            self.cryo.qualify()
        self.events.event("recovery_cleanup_environment_verified", {
            "condition_id": last_condition["condition_id"], "formal_samples_taken": 0})
        return {"ready": True, "condition_id": last_condition["condition_id"],
                "formal_samples_taken": 0}

    def prepare_lockin_ranges(self, condition):
        """Apply the compiled target profile before this condition's Lock-in entry."""
        schedule = self.config.lockin_range_schedule
        if schedule is None:
            return
        selection = schedule.resolve({module: item["index"]
                                      for module, item in condition["axes"].items()})
        result = self.lockin.apply_range_profile({"name": selection.rule_name,
                                                "ranges": selection.full_scales_v})
        if result.get("verified") is not True:
            raise ValueError("Condition Lock-in sensitivity profile was not verified")
        self.events.event("lockin_range_selected", {
            "condition_id": condition["condition_id"], "repeat_index": condition["repeat_index"],
            "outer_indices": dict(zip(schedule.outer_modules, selection.outer_indices)),
            "requested": selection.requested, **result})

    def apply(self, module, point):
        if module in {"temperature", "magnetic"}:
            result = self.cryo.apply(module, point)
            if self.lockin is not None:
                self.lockin.disturbed(module)
            return result
        if module == "smu":
            self.point = point
            self.smu.set_point({key[:-2]: v for key, v in point.values.items()},
                               segment=point.segment,
                               on_settings_applied=(lambda: self.lockin.disturbed("smu"))
                                   if self.lockin is not None else None)
            # Actual sensed coordinates are produced only by the formal read.
            return {"requested": point.values}
        lockin_axis = next(axis for axis in self.config.plan.axes if axis.module == "lockin")
        index = lockin_axis.points.index(point)
        return {"actual": self.lockin.set_point(point.values["lockin_excitation_v_rms"],
            point.values["lockin_frequency_hz"], point_index=index)}

    def qualify_axis(self, module, point, scope):
        if module in {"temperature", "magnetic"}:
            return self.cryo.qualify(modules=(module,), scope=scope)
        return {"ready": True, "axis_scope": scope}

    def qualify(self, modules):
        if self.lockin is not None:
            self.lockin.qualify()
        return {"ready": True, "mode": "hardware", "active_modules": modules,
                "temperature_qualification": (self.cryo.temperature_qualification
                    if self.cryo is not None else None)}

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
            return _json(self.lockin.sample_point(
                measurement_context=self.cryo.capture if self.cryo is not None else None))
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
            skipped = {(item.get("role"), item["harmonic"])
                       for item in self.lockin.point_record.get("skipped_harmonics", ())}
            for role, harmonics in self.lockin.harmonics_by_role.items():
                expected.update(f"lockin_{role}_h{h}_{metric}" for h in harmonics
                                if h in self.lockin.point_harmonics
                                and (role, h) not in skipped
                                and ("lockin_" + role, h) not in skipped
                                for metric in ("x_v", "y_v", "amplitude_v", "phase_deg"))
            expected.difference_update(getattr(self.lockin, "unavailable_phase_keys", ()))
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
                               smu_adapter_factory=None, manager_factory=None, sleep=None,
                               on_registered=None, recovery=None, recovery_owner_guard=None):
    if not authorize_hardware:
        raise ValueError("Explicit combined connection/write/status-consumption authorization required")
    if config.lockin is not None and not confirm_xy_sine_disconnected:
        raise ValueError("Confirm physical disconnection of XY SINE OUT before opening hardware")
    if config.cryostat is not None and not authorize_cryostat:
        raise ValueError("Separate cryostat temperature/field write authorization required")
    if recovery is not None:
        review = recovery.get("review", {})
        if review.get("ready") is not True or review.get("acknowledged_failure") is not True:
            raise ValueError("Recovery requires the complete acknowledged configuration/cleanup review")
    station = HardwareCombinationStation(config, smu_adapter_factory=smu_adapter_factory,
                                           manager_factory=manager_factory, sleep=sleep,
                                           dll=dll, monotonic=monotonic,
                                           authorize_hardware=authorize_hardware,
                                           recovery=recovery)
    snapshot = {**config.snapshot, "authorization": {
        "combined_connection_writes_status_consumption": True,
        "xy_sine_disconnected": confirm_xy_sine_disconnected,
        "cryostat_connection_and_selected_axis_writes": authorize_cryostat,
        "process_guards_disabled": True}}
    return _run_combination(config.plan, store, run_id, station=station, snapshot=snapshot,
                            on_registered=on_registered, recovery=recovery)


# Compatibility for callers of the earlier electrical-only milestone.
ElectricalCombinationConfig = HardwareCombinationConfig
ElectricalCombinationStation = HardwareCombinationStation
load_electrical_combination = load_hardware_combination
run_electrical_combination = run_hardware_combination
