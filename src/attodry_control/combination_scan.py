"""Four-module plan, shared coordinator and acquisition-order-independent records.

The public simulator entry accepts only simulated stations. The separate
hardware entry validates authorization/configuration before using the core.
This module deliberately imports no DLL, VISA, or device adapter.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import itertools
import hashlib
import math
from pathlib import Path

from .combination_store import CombinationStore, utc_now


MODULE_KEYS = {
    "temperature": {"temperature_k"},
    "magnetic": {"field_x_t", "field_z_t"},
    "smu": {"smu_bias_v", "smu_bias_a", "gate_top_v", "gate_top_a",
            "gate_bottom_v", "gate_bottom_a"},
    "lockin": {"lockin_excitation_v_rms", "lockin_frequency_hz"},
}
SMU_ROLES = ("smu_bias", "gate_top", "gate_bottom")


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

    def validate(self) -> None:
        if not isinstance(self.run_name, str) or not isinstance(self.note, str):
            raise ValueError("Run name and note must be strings")
        modules = [axis.module for axis in self.axes]
        if not modules or len(set(modules)) != len(modules):
            raise ValueError("Provide one to four distinct module axes")
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
                if axis.module == "magnetic":
                    if math.hypot(point.values["field_x_t"], point.values["field_z_t"]) > 3:
                        raise ValueError("Integrated field resultant must be <= 3 T")
                if axis.module == "lockin":
                    if not 0.004 <= point.values["lockin_excitation_v_rms"] <= 5:
                        raise ValueError("SR830 excitation must be in 0.004..5 V RMS")
                    if not 0.001 <= point.values["lockin_frequency_hz"] <= 102000:
                        raise ValueError("SR830 frequency is out of range")

    def snapshot(self) -> dict:
        self.validate()
        return {**asdict(self), "mode": "simulation", "field_limit_policy": "universal-3T",
                "record_contract": "combination-v1",
                "implementation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}

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
        # Called after *all* changed axes, including a field move, before sampling.
        self.operations.append(("qualify", modules))
        return {"ready": True, "simulated": True,
                "temperature_rechecked": "temperature" in modules,
                "field_rechecked": "magnetic" in modules}

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


def _run_combination(plan, store, run_id, *, station, snapshot, resume=False) -> dict:
    """Internal engine; backend entry points own pre-I/O validation."""
    conditions = plan.conditions()
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("run_id must be nonempty")
    completed = store.begin_run(run_id, snapshot, conditions, resume=resume)
    modules = tuple(a.module for a in plan.axes)
    current: dict | None = None
    attempt: int | None = None
    primary: BaseException | None = None
    cleanup_errors: list[str] = []
    cleanup_actions: list[dict] = []

    def emit(kind: str, payload: dict) -> None:
        context = {} if current is None else {
            "condition_id": current["condition_id"], "attempt_index": attempt}
        store.event(run_id, kind, {**context, **payload})

    try:
        if hasattr(station, "set_event_sink"):
            station.set_event_sink(emit)
        emit("run_started", {"resume": resume, "plan": snapshot})
        emit("preflight", station.open(modules))
        previous_indices: tuple | None = None
        for condition in conditions:
            if condition["condition_id"] in completed:
                continue
            current = condition
            attempt = store.begin_attempt(run_id, condition["condition_id"])
            emit("condition_started", condition)
            indices = (condition["repeat_index"], *(
                condition["axes"][module]["index"] for module in modules))
            # Index prefixes, not values: preserve duplicate points and inner resets.
            for depth, axis in enumerate(plan.axes):
                if previous_indices is None or indices[:depth + 2] != previous_indices[:depth + 2]:
                    point = axis.points[condition["axes"][axis.module]["index"]]
                    emit("axis_setting", {"module": axis.module, "requested": point.values})
                    emit("axis_ready", {"module": axis.module, **station.apply(axis.module, point)})
            qualification = station.qualify(modules)
            emit("condition_qualification", qualification)
            if qualification.get("ready") is not True:
                raise ValueError("Condition did not qualify")
            for sample_index in range(plan.samples_per_condition):
                start = utc_now()
                reads = []
                if hasattr(station, "begin_sample"):
                    station.begin_sample()
                # All settings/settling precede every fresh SMU/Lock-in read.
                # Per-instrument times describe sequential, not simultaneous, reads.
                read_error = None
                try:
                    for module in modules:
                        reading = station.read(module)
                        emit("raw_reading", {"sample_index": sample_index,
                                             "reading": _audit_value(reading)})
                        if reading.get("module") != module:
                            raise ValueError("Reading belongs to the wrong module")
                        reads.append(reading)
                        if reading.get("clean") is not True or reading.get("problems"):
                            # Preserve partial reads and stop later acquisition.
                            break
                except BaseException as exc:
                    read_error = exc
                    raise
                finally:
                    if hasattr(station, "end_sample"):
                        try:
                            station.end_sample(reads)
                        except BaseException as exc:
                            if read_error is None:
                                raise
                            read_error.add_note(f"Environmental after-read failed: {exc}")
                            try:
                                emit("environment_after_read_failed", {"error": str(exc)})
                            except BaseException as audit_error:
                                # A second failure must not replace the original
                                # instrument error or suppress global cleanup.
                                cleanup_errors.append(
                                    f"environment after-read: {exc}; audit: {audit_error}")
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
                clean = (finite and set(actual) == expected_keys and all(
                    r.get("clean") is True and not r.get("problems") for r in reads)
                    and expected_measured <= set(measurements) and fresh)
                if "magnetic" in modules and finite:
                    clean = clean and math.hypot(actual.get("field_x_t", math.inf),
                                                actual.get("field_z_t", math.inf)) <= 3
                if finite and set(actual) == expected_keys:
                    try:
                        CombinationPlan(tuple(ScanAxis(module, (AxisPoint({
                            key: value for key, value in actual.items() if key in MODULE_KEYS[module]
                        }),)) for module in modules)).validate()
                    except ValueError:
                        clean = False
                if not finite:
                    # Raw problematic values are already auditable via emit; JSON
                    # rejects non-finite numbers, so a malformed backend fails closed.
                    raise ValueError("Non-finite formal readback")
                sample = {
                    "started_at_utc": start, "finished_at_utc": finish,
                    "actual": actual, "measurements": measurements, "reads": reads,
                    "clean": clean, "simulated": snapshot["mode"] == "simulation",
                }
                store.sample(run_id, condition["condition_id"], attempt, sample_index, sample)
                emit("formal_sample", {"sample_index": sample_index, "clean": clean})
                if not clean:
                    raise ValueError("Formal sample is incomplete or unclean")
            store.finish_attempt(run_id, condition["condition_id"], attempt,
                                 expected_samples=plan.samples_per_condition)
            emit("condition_accepted", {})
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
        for module in ("lockin", "smu", "magnetic", "temperature"):
            if module not in modules:
                continue
            try:
                emit("cleanup_started", {"module": module})
            except BaseException as exc:
                cleanup_errors.append(f"cleanup audit: {exc}")
            try:
                action = _audit_value(station.cleanup(module, primary is not None or bool(cleanup_errors)))
                cleanup_actions.append(action)
                if action.get("clean") is not True:
                    cleanup_errors.append(f"{module}: cleanup not certified; review recorded actions")
                emit("cleanup_result", action)
            except BaseException as exc:
                cleanup_errors.append(f"{module}: {type(exc).__name__}: {exc}")
        try:
            station.close()
        except BaseException as exc:
            cleanup_errors.append(f"close: {exc}")
    communication_uncertain = isinstance(primary, OSError)
    cleanup = {"clean": not cleanup_errors and not communication_uncertain,
               "actions": cleanup_actions, "errors": cleanup_errors,
               "communication_uncertain": communication_uncertain,
               "manual_verification_required": bool(cleanup_errors) or communication_uncertain}
    status = ("interrupted" if isinstance(primary, KeyboardInterrupt) else
              "failed" if primary is not None or cleanup_errors else "completed")
    error = f"{type(primary).__name__}: {primary}" if primary else None
    store.finish_run(run_id, status, cleanup, error)
    return {"run_id": run_id, "status": status, "cleanup": cleanup, "error": error}
