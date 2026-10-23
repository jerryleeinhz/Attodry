"""Modular plan, shared coordinator and acquisition-order-independent records.

The public simulator entry accepts only simulated stations. The separate
hardware entry validates authorization/configuration before using the core.
This module deliberately imports no DLL, VISA, or device adapter.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
import itertools
import hashlib
import math
from pathlib import Path

from .combination_store import CombinationStore, utc_now
from .models import VectorField
from .measurement_timing import MeasurementTiming
from .optical_recovery import OpticalPowerDeviation, OpticalTargetUnreachable
from .reference_transients import ReferenceTransientError
from .safety import FieldReadbackPolicy, MagnetLimits, float32_field


MODULE_KEYS = {
    "rotator": {"rotator_1_angle_deg", "rotator_2_angle_deg"},
    "temperature": {"temperature_k"},
    "magnetic": {"field_x_t", "field_z_t"},
    "smu": {"smu_bias_v", "smu_bias_a", "gate_top_v", "gate_top_a",
            "gate_bottom_v", "gate_bottom_a"},
    "lockin": {"lockin_excitation_v_rms", "lockin_frequency_hz"},
    "optical": {"optical_source_level_pct", "optical_wavelength_nm",
                "optical_bandwidth_nm", "optical_nd_pct", "optical_pulse_picker_ratio",
                "optical_target_power_w", "optical_current_a"},
}
SMU_ROLES = ("smu_bias", "gate_top", "gate_bottom")
COMBINATION_FIELD_LIMIT_POLICY = "planned-axis-configured-v2"
COMMAND_SCOPED_FIELD_LIMIT_POLICY = "command-scoped-configured-v1"
RETRYABLE_SAMPLE_ERRORS = (ReferenceTransientError, OpticalPowerDeviation)


def _audit_value(value: object) -> object:
    """Preserve invalid numeric readbacks explicitly in valid JSON audit events."""
    if isinstance(value, float) and not math.isfinite(value):
        return {"invalid_numeric": repr(value)}
    if isinstance(value, dict):
        return {key: _audit_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_audit_value(item) for item in value]
    return value


@dataclass(frozen=True)
class AxisPoint:
    values: dict[str, float]
    segment: str = "main"
    direction: str = "ordered"
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ScanAxis:
    module: str
    points: tuple[AxisPoint, ...]


@dataclass(frozen=True)
class CombinationPlan:
    # Tuple order is the literal outer-to-inner acquisition order.
    axes: tuple[ScanAxis, ...]
    samples_per_condition: int = 1
    repeats: int = 1
    run_name: str = ""
    note: str = ""
    magnet_limits: MagnetLimits = MagnetLimits()
    readback_tolerance_t: float | None = None
    lockin_coordinate_limits: tuple[float, float, float, float] | None = None
    strict_resultant: bool = False
    unreachable_target_policy: str = "abort"
    command_scoped_field: bool = False

    def validate(self) -> None:
        if not isinstance(self.magnet_limits, MagnetLimits):
            raise ValueError("Combination magnet limits must be validated MagnetLimits")
        if type(self.strict_resultant) is not bool:
            raise ValueError("strict_resultant must be a boolean")
        if type(self.command_scoped_field) is not bool:
            raise ValueError("command_scoped_field must be a boolean")
        if self.command_scoped_field and self.strict_resultant:
            raise ValueError("Command-scoped and universal field policies are mutually exclusive")
        if self.strict_resultant and max(self.magnet_limits.hardware_x_max_t,
                                         self.magnet_limits.hardware_z_max_t,
                                         self.magnet_limits.experiment_vector_max_t) > 3:
            raise ValueError("Photonics field limits must remain within 3 T")
        source_min, source_max, reference_min, reference_max = (0.004, 5.0, 0.001, 102000.0)
        if self.lockin_coordinate_limits is not None:
            values = self.lockin_coordinate_limits
            if (not isinstance(values, tuple) or len(values) != 4
                    or not all(type(v) in (float, int) and math.isfinite(v) for v in values)
                    or not 0 < values[0] <= values[1] <= 5
                    or not 0.001 <= values[2] <= values[3] <= 4_000_000):
                raise ValueError("Invalid archived lock-in coordinate limits")
            source_min, source_max, reference_min, reference_max = values
        if not isinstance(self.run_name, str) or not isinstance(self.note, str):
            raise ValueError("Run name and note must be strings")
        modules = [axis.module for axis in self.axes]
        if not modules or len(set(modules)) != len(modules):
            raise ValueError("Provide distinct module axes")
        if self.unreachable_target_policy not in ("abort", "skip_wavelength"):
            raise ValueError("Unsupported unreachable_target_policy")
        if self.unreachable_target_policy == "skip_wavelength":
            if modules not in (["optical", "lockin"], ["optical", "smu", "lockin"]):
                raise ValueError("skip_wavelength requires outer optical and only inner SMU/lockin")
            waves = [p.values.get("optical_wavelength_nm") for p in self.axes[0].points]
            if None in waves or len(set(waves)) != len(waves):
                raise ValueError("skip_wavelength requires one optical target per distinct wavelength")
        for value in (self.samples_per_condition, self.repeats):
            if type(value) is not int or not 1 <= value <= 1000:
                raise ValueError("Sample/repeat counts must be integers in 1..1000")
        count = self.repeats
        for axis in self.axes:
            if axis.module not in MODULE_KEYS or not axis.points:
                raise ValueError("Unknown or empty module axis")
            count *= len(axis.points)
            if count * self.samples_per_condition > 100000:
                raise ValueError("Offline plan exceeds 100000 formal samples")
            keys = set(axis.points[0].values)
            if not keys or not keys <= MODULE_KEYS[axis.module]:
                raise ValueError(f"Unsupported {axis.module} coordinate keys")
            if axis.module in {"temperature", "magnetic", "lockin"}:
                if keys != MODULE_KEYS[axis.module]:
                    raise ValueError(f"Specify every {axis.module} coordinate (fixed values included)")
            for role in SMU_ROLES:
                if {role + "_v", role + "_a"} <= keys:
                    raise ValueError("An SMU role cannot source voltage and current together")
            for point in axis.points:
                if set(point.values) != keys:
                    raise ValueError("An axis must keep the same active coordinates")
                if not isinstance(point.segment, str) or not isinstance(point.direction, str):
                    raise ValueError("Segment and direction labels must be strings")
                if not all(type(v) in (float, int) and math.isfinite(v)
                           for v in point.values.values()):
                    raise ValueError("Coordinates must be finite real numbers")
                if axis.module == "temperature" and point.values["temperature_k"] <= 0:
                    raise ValueError("Temperature must be positive")
                if axis.module == "rotator" and any(not 0 <= v <= 360 for v in point.values.values()):
                    raise ValueError("Waveplate mechanical angles must be in 0..360 degrees")
                if axis.module == "lockin":
                    if not source_min <= point.values["lockin_excitation_v_rms"] <= source_max:
                        raise ValueError("Lock-in excitation is outside the configured limits")
                    if not reference_min <= point.values["lockin_frequency_hz"] <= reference_max:
                        raise ValueError("Lock-in reference frequency is outside the configured limits")
                if axis.module == "optical":
                    v = point.values
                    if ("optical_source_level_pct" in v) == ("optical_current_a" in v):
                        raise ValueError("Specify exactly one NKT percent or CLD ampere source coordinate")
                    if "optical_source_level_pct" in v and not 0 <= v["optical_source_level_pct"] <= 100:
                        raise ValueError("Optical source setting must be within 0..100 percent")
                    if "optical_current_a" in v and not 0 <= v["optical_current_a"] <= 1:
                        raise ValueError("CLD optical current must be within its hardware capability")
                    for key in ("optical_wavelength_nm", "optical_bandwidth_nm", "optical_target_power_w"):
                        if key in v and v[key] <= 0:
                            raise ValueError(f"{key} must be positive")
                    if "optical_nd_pct" in v and not 0 <= v["optical_nd_pct"] <= 100:
                        raise ValueError("Optical ND setting must be within 0..100 percent")
                    if "optical_pulse_picker_ratio" in v and (type(v["optical_pulse_picker_ratio"]) is not int
                            or not 1 <= v["optical_pulse_picker_ratio"] <= 65535):
                        raise ValueError("Optical pulse picker ratio must be an integer in 1..65535")
        # Legacy policies classify the whole plan; the explicit new policy binds
        # each actual readback to the current command/transition context.
        self.magnetic_readback_policy()

    def snapshot(self) -> dict:
        self.validate()
        policy = self.magnetic_readback_policy()
        return {**asdict(self), "mode": "simulation", "field_limit_policy": (
                    COMMAND_SCOPED_FIELD_LIMIT_POLICY if self.command_scoped_field
                    else COMBINATION_FIELD_LIMIT_POLICY),
                **({"field_readback_policy": policy.snapshot()} if policy is not None else {}),
                "record_contract": "combination-v1",
                "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}

    def magnetic_readback_policy(self):
        axis = next((a for a in self.axes if a.module == "magnetic"), None)
        if axis is None:
            return None
        return FieldReadbackPolicy.from_targets(
            tuple(VectorField(p.values["field_x_t"], p.values["field_z_t"]) for p in axis.points),
            self.magnet_limits, self.readback_tolerance_t, strict_resultant=self.strict_resultant,
            command_scoped=self.command_scoped_field)

    def conditions(self) -> list[dict]:
        self.validate()
        conditions = []
        for repeat in range(self.repeats):
            for indices in itertools.product(*(range(len(a.points)) for a in self.axes)):
                requested: dict[str, float] = {}
                axes = {}
                for axis, index in zip(self.axes, indices):
                    point = axis.points[index]
                    requested.update(point.values)
                    axes[axis.module] = {"index": index, "segment": point.segment,
                                         "direction": point.direction}
                    if point.metadata:
                        axes[axis.module]["grid"] = point.metadata
                sequence = len(conditions)
                conditions.append({
                    "condition_id": f"condition-{sequence:06d}", "sequence_index": sequence,
                    "repeat_index": repeat, "loop_order": [a.module for a in self.axes],
                    "axes": axes, "requested": requested,
                })
        return conditions


class SimulatedCombinationStation:
    """Deterministic synthetic response; not a device model or calibration.

    One station represents the sole owner, with one shared cryostat session.
    Subclasses can inject errors without adding any hardware dependencies.
    """

    def __init__(self) -> None:
        self.values: dict[str, float] = {}
        self.operations: list[tuple] = []
        self.closed = False

    def open(self, modules: tuple[str, ...]) -> dict:
        self.operations.append(("open", modules))
        return {"mode": "simulation", "shared_cryostat": bool(
            {"temperature", "magnetic"} & set(modules)),
            "identities": {m: f"SIMULATED::{m}" for m in modules}}

    def apply(self, module: str, point: AxisPoint) -> dict:
        self.values.update(point.values)
        self.operations.append(("apply", module, dict(point.values)))
        return {"actual": dict(point.values), "simulated": True}

    def qualify(self, modules: tuple[str, ...]) -> dict:
        # Leaf readiness remains fresh; environmental dwell belongs to axis entry.
        self.operations.append(("qualify", modules))
        return {"ready": True, "simulated": True,
                "temperature_rechecked": False, "field_rechecked": False}

    def qualify_axis(self, module, point, scope):
        self.operations.append(("qualify_axis", module, dict(scope)))
        return {"ready": True, "simulated": True, "axis_scope": scope}

    def read(self, module: str) -> dict:
        self.operations.append(("read", module))
        actual = {k: v for k, v in self.values.items() if k in MODULE_KEYS[module]}
        measured: dict[str, float] = {}
        excitation = self.values.get("lockin_excitation_v_rms", 0)
        if module == "smu":
            for role in SMU_ROLES:
                # Deliberately excitation-dependent: stale outer-loop readings
                # fail the SMU-outer/Lock-in-inner regression.
                resistance = 1000 * (1 + excitation)
                if role + "_v" in actual:
                    voltage = actual[role + "_v"]
                    current = voltage / resistance
                elif role + "_a" in actual:
                    current = actual[role + "_a"]
                    voltage = current * resistance
                else:
                    continue
                measured.update({role + "_voltage_v": voltage, role + "_current_a": current})
        elif module == "lockin":
            for role, factor in (("lockin_xx", 1.0), ("lockin_xy", 0.1)):
                measured.update({role + "_h1_x_v": excitation * factor,
                                 role + "_h1_y_v": 0.0,
                                 role + "_h1_amplitude_v": excitation * factor,
                                 role + "_h1_phase_deg": 0.0})
        return {"module": module, "captured_at_utc": utc_now(), "actual": actual,
                "measurements": measured, "clean": True, "problems": [],
                "status": {"simulated": True}}

    def cleanup(self, module: str, failed: bool) -> dict:
        self.operations.append(("cleanup", module, failed))
        return {"module": module, "clean": True, "simulated": True}

    def close(self) -> None:
        self.operations.append(("close",))
        self.closed = True

    def expected_measurements(self, condition: dict) -> set[str]:
        expected = set()
        for role in SMU_ROLES:
            if set(condition["requested"]) & {role + "_v", role + "_a"}:
                expected.update({role + "_voltage_v", role + "_current_a"})
        if "lockin" in condition["axes"]:
            expected.update(f"{role}_h1_{metric}" for role in
                            ("lockin_xx", "lockin_xy") for metric in
                            ("x_v", "y_v", "amplitude_v", "phase_deg"))
        return expected


def run_simulated_combination(
    plan: CombinationPlan, store: CombinationStore, run_id: str, *,
    resume: bool = False,
    station: SimulatedCombinationStation | None = None,
) -> dict:
    """Execute every leaf freshly, retaining raw reads even on partial failure.

    No automatic retry or magnetic resume. Cleanup independently attempts every
    active module; a failure never implies zero/off or erases the primary error.
    """
    station = station if station is not None else SimulatedCombinationStation()
    if not isinstance(station, SimulatedCombinationStation):
        raise TypeError("Only the offline simulated station is supported")
    return _run_combination(plan, store, run_id, station=station,
                            snapshot=plan.snapshot(), resume=resume)


def _run_combination(plan, store, run_id, *, station, snapshot, resume=False,
                     on_registered=None) -> dict:
    """Internal engine; backend entry points own pre-I/O validation."""
    from .lockin_overload import reading_allows_continuation
    conditions = plan.conditions()
    field_policy = None
    if "field_readback_policy" in snapshot:
        field_policy = FieldReadbackPolicy.from_snapshot(snapshot["field_readback_policy"])
        planned_policy = plan.magnetic_readback_policy()
        if planned_policy is None or field_policy.snapshot() != planned_policy.snapshot():
            raise ValueError("Field policy differs from the complete target plan")
    if plan.magnetic_readback_policy() is not None:
        expected_policy = (COMMAND_SCOPED_FIELD_LIMIT_POLICY if plan.command_scoped_field
                           else COMBINATION_FIELD_LIMIT_POLICY)
        if snapshot.get("field_limit_policy") == expected_policy:
            if field_policy is None:
                raise ValueError("Configured field targets require their archived readback policy")
        else:
            # Historical combinations retain their universal 3 T contract.
            if snapshot.get("field_limit_policy") not in {None, "universal-3T"}:
                raise ValueError("Unsupported archived combination field limit policy")
            for point in next(a for a in plan.axes if a.module == "magnetic").points:
                if math.hypot(point.values["field_x_t"], point.values["field_z_t"]) > 3:
                    raise ValueError("Legacy combination target exceeds its archived 3 T envelope")
            if field_policy is not None and max(field_policy.limits.hardware_x_max_t,
                                               field_policy.limits.hardware_z_max_t) > 3:
                raise ValueError("Legacy combination axis limits cannot exceed 3 T")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id must be nonempty")
    if resume and plan.unreachable_target_policy == "skip_wavelength":
        raise ValueError("Wavelength skipping requires a new run; resume is unsupported")
    completed = store.begin_run(run_id, snapshot, conditions, resume=resume)
    modules = tuple(a.module for a in plan.axes)
    current: dict | None = None
    attempt: int | None = None
    primary: BaseException | None = None
    cleanup_errors: list[str] = []
    cleanup_actions: list[dict] = []

    def store_event(kind: str, payload: dict) -> None:
        context = {} if current is None else {
            "condition_id": current["condition_id"], "attempt_index": attempt}
        store.event(run_id, kind, {**context, **payload})

    timing = MeasurementTiming(store_event, monotonic=getattr(station, "monotonic", None))
    emit = timing.emit

    def phase(name, **context):
        # Explicit start markers survive unrelated raw/timing events. The
        # hardware station also retains the enclosing dark/formal context.
        if hasattr(station, "set_monitor_phase"):
            station.set_monitor_phase(name, **context)
        else:
            emit("scan_phase", {"phase": name, **context})

    def prepare_condition(condition, indices, previous_indices):
        # Bind the profile to the complete requested condition before optical
        # reference transitions/qualification can consume lock-in status.
        range_prepared = False
        # Index prefixes preserve duplicate points and inner-axis resets.
        for depth, axis in enumerate(plan.axes):
            if axis.module in {"optical", "lockin"} and not range_prepared:
                if hasattr(station, "prepare_lockin_ranges"):
                    with timing.timed_stage("lockin_range_prepare"):
                        station.prepare_lockin_ranges(condition)
                range_prepared = True
            if previous_indices is None or indices[:depth + 2] != previous_indices[:depth + 2]:
                point = axis.points[condition["axes"][axis.module]["index"]]
                emit("axis_setting", {"module": axis.module, "requested": point.values})
                phase("axis_apply", module=axis.module)
                with timing.timed_stage("axis_apply", module=axis.module):
                    emit("axis_ready", {"module": axis.module, **station.apply(axis.module, point)})
                if hasattr(station, "qualify_axis"):
                    scope = {"repeat_index": condition["repeat_index"],
                             "axis_indices": list(indices[1:depth + 2]),
                             "condition_id": condition["condition_id"]}
                    phase("axis_qualification", module=axis.module)
                    with timing.timed_stage("axis_qualification", module=axis.module):
                        result = station.qualify_axis(axis.module, point, scope)
                        emit("axis_qualification", {"module": axis.module, **result})
                        if result.get("ready") is not True:
                            raise ValueError("Axis did not qualify: " + axis.module)
        with timing.timed_stage("condition_qualification"):
            result = station.qualify(modules)
            emit("condition_qualification", result)
            if result.get("ready") is not True:
                raise ValueError("Condition did not qualify")

    skipped_groups = {}
    skipped_count = 0

    def archive_skip(evidence):
        store.finish_attempt(run_id, current["condition_id"], attempt,
                             expected_samples=plan.samples_per_condition,
                             error=evidence["original_error"])
        store.skip_condition(run_id, current["condition_id"], attempt, evidence)

    try:
        # Only a successfully committed run may become the monitor default.
        # Registration failure stops before opening any instrument.
        if hasattr(station, "set_event_sink"):
            station.set_event_sink(emit)
        if on_registered is not None:
            emit("launch_registered", on_registered(store, run_id))
        emit("run_started", {"resume": resume, "plan": snapshot})
        phase("preflight")
        with timing.timed_stage("preflight"):
            emit("preflight", station.open(modules))
        previous_indices: tuple | None = None
        for condition in conditions:
            if condition["condition_id"] in completed:
                continue
            current = condition
            attempt = store.begin_attempt(run_id, condition["condition_id"])
            emit("condition_started", condition)
            if plan.unreachable_target_policy == "skip_wavelength" and timing.audit_errors:
                raise OSError("Measurement audit failure prevents wavelength continuation")
            indices = (condition["repeat_index"], *(
                condition["axes"][module]["index"] for module in modules))
            group = (condition["repeat_index"], condition["axes"].get("optical", {}).get("index"))
            if group in skipped_groups:
                archive_skip(skipped_groups[group])
                skipped_count += 1
                attempt = None
                continue
            try:
                prepare_condition(condition, indices, previous_indices)
            except OpticalTargetUnreachable as exc:
                if plan.unreachable_target_policy != "skip_wavelength":
                    raise
                if timing.audit_errors:
                    raise OSError("Measurement audit failure prevents wavelength skip") from exc
                # This catch excludes formal sampling/recovery. All fresh
                # station guards must pass before any remaining leaves skip.
                with timing.timed_stage("unreachable_wavelength_guard"):
                    evidence = station.skip_unreachable_wavelength(exc)
                evidence = {**evidence, "trigger_condition_id": condition["condition_id"]}
                archive_skip(evidence)
                skipped_groups[group] = evidence
                skipped_count += 1
                emit("wavelength_skipped", evidence)
                if timing.audit_errors:
                    raise OSError("Wavelength skip audit failure prevents continuation") from exc
                previous_indices = None
                attempt = None
                continue
            for sample_index in range(plan.samples_per_condition):
                with timing.timed_stage("sample", sample_index=sample_index):
                    start = utc_now()
                    candidate_index = 0

                    def acquire_window():
                        nonlocal candidate_index, start
                        with timing.timed_stage("sample_window", sample_index=sample_index,
                                candidate_index=candidate_index + 1):
                            start = utc_now()
                            candidate_index += 1
                            reads = []
                            phase("formal_sampling", sample_index=sample_index)
                            try:
                                if hasattr(station, "begin_sample"):
                                    station.begin_sample()
                                # Every retry starts fresh optical/environment brackets
                                # and repeats ALL modules, never joining old/new roles.
                                read_error = None
                                try:
                                    for module in modules:
                                        with timing.timed_stage("module_read", module=module,
                                                sample_index=sample_index, candidate_index=candidate_index):
                                            phase("formal_sampling", module=module,
                                                  sample_index=sample_index)
                                            reading = station.read(module)
                                            emit("raw_reading", {"sample_index": sample_index,
                                                "candidate_index": candidate_index,
                                                "reading": _audit_value(reading)})
                                        if reading.get("module") != module:
                                            raise ValueError("Reading belongs to the wrong module")
                                        reads.append(reading)
                                        if not reading_allows_continuation(reading):
                                            break
                                except BaseException as exc:
                                    read_error = exc
                                    raise
                                finally:
                                    if hasattr(station, "end_sample"):
                                        try:
                                            station.end_sample(reads)
                                        except BaseException as exc:
                                            if (isinstance(exc, RETRYABLE_SAMPLE_ERRORS)
                                                    and any(not reading_allows_continuation(r) for r in reads)):
                                                raise ValueError("Noncontinuable formal readback before retryable sample anomaly") from exc
                                            if read_error is None:
                                                raise
                                            # Only the same recovery category may retain
                                            # the original retryable exception. A hard
                                            # after-read fault must stop either recovery.
                                            if isinstance(read_error, RETRYABLE_SAMPLE_ERRORS):
                                                if not isinstance(exc, RETRYABLE_SAMPLE_ERRORS):
                                                    raise exc from read_error
                                                if isinstance(read_error, ReferenceTransientError) != isinstance(exc, ReferenceTransientError):
                                                    raise ValueError("Mixed reference and optical power anomalies; recovery is not permitted: "
                                                        f"read {type(read_error).__name__}: {read_error}; "
                                                        f"after {type(exc).__name__}: {exc}") from exc
                                            read_error.add_note(f"Environmental after-read failed: {exc}")
                                            try:
                                                emit("environment_after_read_failed", {"error": str(exc)})
                                            except BaseException as audit_error:
                                                if isinstance(read_error, RETRYABLE_SAMPLE_ERRORS):
                                                    raise audit_error from read_error
                                                cleanup_errors.append(
                                                    f"environment after-read: {exc}; audit: {audit_error}")
                            except RETRYABLE_SAMPLE_ERRORS as exc:
                                reference = isinstance(exc, ReferenceTransientError)
                                emit("reference_sample_rejected" if reference else "optical_power_sample_rejected", {"sample_index": sample_index,
                                    "candidate_index": candidate_index, "accepted": False,
                                    "valid_for_analysis": False, "reason": str(exc),
                                    "kind": exc.kind if reference else "optical_power_deviation", "evidence": exc.evidence,
                                    "partial_reads": _audit_value(reads)})
                                raise
                            return reads

                    def acquire_audited_window():
                        try:
                            return acquire_window()
                        except RETRYABLE_SAMPLE_ERRORS as exc:
                            if timing.audit_errors:
                                # Includes the timing event emitted after the
                                # window exits: audit failure is never retried.
                                raise ValueError("Measurement timing audit failed during sample recovery") from exc
                            raise

                    reads = (station.run_sample_operation(acquire_audited_window)
                             if hasattr(station, "run_sample_operation") else acquire_audited_window())
                    actual: dict = {}
                    measurements: dict = {}
                    for reading in reads:
                        actual.update(reading["actual"])
                        measurements.update(reading["measurements"])
                    finite = all(type(v) in (int, float) and math.isfinite(v)
                                 for v in (*actual.values(), *measurements.values()))
                    expected_keys = set(condition["requested"])
                    expected_measured = station.expected_measurements(condition)
                    finish = utc_now()
                    fresh = all(datetime.fromisoformat(start) <=
                                datetime.fromisoformat(r["captured_at_utc"]) <=
                                datetime.fromisoformat(finish) for r in reads)
                    acquisition_accepted = (finite and set(actual) == expected_keys and all(
                        reading_allows_continuation(r) for r in reads)
                        and expected_measured <= set(measurements) and fresh)
                    field_context = ({"command": float32_field(VectorField(
                        condition["requested"]["field_x_t"], condition["requested"]["field_z_t"]))}
                        if field_policy is not None and field_policy.command_scoped else {})
                    if "magnetic" in modules and finite and set(actual) == expected_keys:
                        try:
                            if field_policy is None:
                                # Historical snapshots retain their strict 3 T rule.
                                if math.hypot(actual["field_x_t"], actual["field_z_t"]) > 3:
                                    raise ValueError("Historical formal field exceeds 3 T")
                            else:
                                field_policy.validate_readback(
                                    VectorField(actual["field_x_t"], actual["field_z_t"]), **field_context)
                        except (ValueError, KeyError, TypeError):
                            acquisition_accepted = False
                    if finite and set(actual) == expected_keys:
                        try:
                            other_axes = tuple(ScanAxis(module, (AxisPoint({
                                key: value for key, value in actual.items() if key in MODULE_KEYS[module]
                            }),)) for module in modules if module != "magnetic")
                            if other_axes:
                                CombinationPlan(other_axes,
                                    lockin_coordinate_limits=plan.lockin_coordinate_limits).validate()
                        except ValueError:
                            acquisition_accepted = False
                    if not finite:
                        # Raw problematic values are already auditable via emit; JSON
                        # rejects non-finite numbers, so a malformed backend fails closed.
                        raise ValueError("Non-finite formal readback")
                    clean = acquisition_accepted and all(r.get("clean") is True for r in reads)
                    sample = {
                        "acquisition_accepted": acquisition_accepted,
                        "started_at_utc": start, "finished_at_utc": finish,
                        "actual": actual, "measurements": measurements, "reads": reads,
                        "clean": clean, "simulated": snapshot["mode"] == "simulation",
                    }
                    if field_policy is not None and {"field_x_t", "field_z_t"} <= actual.keys():
                        sample["field_readback_assessment"] = field_policy.assessment(
                            VectorField(actual["field_x_t"], actual["field_z_t"]), **field_context)
                    with timing.timed_stage("sample_store", sample_index=sample_index):
                        store.sample(run_id, condition["condition_id"], attempt, sample_index, sample)
                    emit("formal_sample", {"sample_index": sample_index, "clean": clean})
                    if not acquisition_accepted:
                        raise ValueError("Formal sample is incomplete or unclean")
            store.finish_attempt(run_id, condition["condition_id"], attempt,
                                 expected_samples=plan.samples_per_condition)
            emit("condition_accepted", {})
            phase("condition_complete")
            previous_indices = indices
            attempt = None
        current = None
    except BaseException as exc:
        primary = exc
        if current is not None and attempt is not None:
            try:
                store.finish_attempt(run_id, current["condition_id"], attempt,
                                     expected_samples=plan.samples_per_condition,
                                     error=f"{type(exc).__name__}: {exc}")
            except BaseException as audit_error:
                cleanup_errors.append(f"attempt audit: {audit_error}")
    finally:
        # An audit/presentation failure must not prevent later cleanup actions.
        for module in ("optical", "lockin", "smu", "rotator", "magnetic", "temperature"):
            if module not in modules:
                continue
            try:
                phase("cleanup", module=module)
            except BaseException as exc:
                cleanup_errors.append(f"cleanup phase audit: {exc}")
            try:
                emit("cleanup_started", {"module": module})
            except BaseException as exc:
                cleanup_errors.append(f"cleanup audit: {exc}")
            try:
                with timing.timed_stage("cleanup", module=module) as stage:
                    action = _audit_value(station.cleanup(module, primary is not None or bool(cleanup_errors)))
                    cleanup_actions.append(action)
                    stage["verified"] = action.get("clean") is True
                    if action.get("clean") is not True:
                        cleanup_errors.append(f"{module}: cleanup not certified; review recorded actions")
                    emit("cleanup_result", action)
            except BaseException as exc:
                cleanup_errors.append(f"{module}: {type(exc).__name__}: {exc}")
        try:
            with timing.timed_stage("close"):
                station.close()
        except BaseException as exc:
            cleanup_errors.append(f"close: {exc}")
        try:
            emit("measurement_timing_summary", timing.summary())
        except BaseException as exc:
            cleanup_errors.append(f"timing summary audit: {exc}")
        cleanup_errors.extend("timing audit: " + error for error in timing.audit_errors)
    communication_uncertain = isinstance(primary, OSError)
    cleanup = {"clean": not cleanup_errors and not communication_uncertain,
               "actions": cleanup_actions, "errors": cleanup_errors,
               "communication_uncertain": communication_uncertain,
               "manual_verification_required": bool(cleanup_errors) or communication_uncertain}
    status = ("interrupted" if isinstance(primary, KeyboardInterrupt) else
              "failed" if primary is not None or cleanup_errors else "completed")
    error = f"{type(primary).__name__}: {primary}" if primary else None
    store.finish_run(run_id, status, cleanup, error)
    from .lockin_overload import overload_summary
    import json
    payloads = [json.loads(r[0]) for r in store.connection.execute(
        "SELECT payload_json FROM combination_samples WHERE run_id=?", (run_id,))]
    summary = overload_summary([p["reads"] for p in payloads])
    completion_message = (f"completed with {skipped_count} skipped conditions; inspect wavelength skip evidence"
                          if status == "completed" and skipped_count else
                          "completed with overload points; inspect per-channel validity"
                          if status == "completed" and summary["data_quality"] == "overload_recorded"
                          else status)
    emit("data_quality_summary", {**summary, "completion_message": completion_message,
                                  "skipped_conditions": skipped_count})
    return {"run_id": run_id, "status": status, "cleanup": cleanup, "error": error,
            "overload_summary": summary, "completion_message": completion_message,
            "skipped_conditions": skipped_count}
