"""Owned, bounded temperature-only commissioning. No hardware I/O on import.

The normal temperature/combination executors are deliberately not modified.
Only this explicit entry can add heater telemetry or a reviewed target sequence.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import time

from .attodry import AttoDryDriver, AttoDryError, AttoDryTimeout
from .config import load_temperature_operation_config
from .hardware_lease import station_hardware_lease
from .stability import StabilityCriteria, TimedValue, evaluate_stability


CRITERIA = StabilityCriteria(tolerance=0.10, stable_range=0.05, dwell_s=120)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def finite(value, label, minimum, maximum):
    if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"Invalid {label}")
    return float(value)


def exact_keys(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        raise ValueError("Unknown or missing experiment fields")


@dataclass(frozen=True)
class Point:
    label: str
    target_k: float
    method: str = "fixed"
    max_wait_s: float = 3600
    hold_s: float = 600
    commands: tuple[tuple[float, float], ...] = ()

    @classmethod
    def parse(cls, row):
        exact_keys(row, ("label", "target_k"), ("method", "max_wait_s", "hold_s", "commands"))
        label = row["label"]
        if not isinstance(label, str) or not label or len(label) > 120:
            raise ValueError("Invalid point label")
        method = row.get("method", "fixed")
        if method not in {"fixed", "shadow", "sequence", "peak_capture_trial"}:
            raise ValueError("Unknown temperature strategy")
        commands = []
        raw = row.get("commands", [])
        if not isinstance(raw, list):
            raise ValueError("commands must be an array")
        for command in raw:
            exact_keys(command, ("after_s", "target_k"))
            commands.append((finite(command["after_s"], "command time", 0, 5000),
                             finite(command["target_k"], "command temperature", 1.7, 251.7)))
        target = finite(row["target_k"], "scientific target", 1.7, 251.7)
        wait = finite(row.get("max_wait_s", 3600), "point timeout", 120, 5400)
        hold = finite(row.get("hold_s", 600), "diagnostic hold", 0, 600)
        if method in {"sequence", "peak_capture_trial"}:
            if not 2 <= len(commands) <= 7 or commands[0][0] != 0 or commands[-1][1] != target:
                raise ValueError("Sequence must begin at zero and restore the final target")
            if any(b[0] - a[0] < 30 for a, b in zip(commands, commands[1:])):
                raise ValueError("Sequence writes must be at least 30 seconds apart")
            if commands[-1][0] + CRITERIA.dwell_s >= wait:
                raise ValueError("Sequence leaves no final qualification budget")
            if method == "peak_capture_trial":
                if (len(commands) != 2 or not 110 <= target <= 135 or wait != 3600 or hold != 600
                        or not 300 <= commands[1][0] <= 1800
                        or not .1 <= target-commands[0][1] <= .5):
                    raise ValueError("Peak-capture trial exceeds its two-command warm-small-step envelope")
        elif commands:
            raise ValueError("Only sequence mode may carry commands")
        return cls(label, target, method, wait, hold, tuple(commands))


def load_plan(path, *, now=None):
    path = Path(path).resolve()
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    exact_keys(raw, ("run_id", "station_config", "station_sha256", "root", "output_directory",
                     "deadline_utc", "points", "code_hashes"),
               ("heater_interval_s", "poll_interval_s", "cleanup_reserve_s", "candidate_evidence", "peak_capture_trial_evidence"))
    if not isinstance(raw["run_id"], str) or not raw["run_id"] or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in raw["run_id"]):
        raise ValueError("Invalid run ID")
    root = Path(raw["root"]).resolve(strict=True)
    station = Path(raw["station_config"]).resolve(strict=True)
    output = Path(raw["output_directory"]).resolve()
    if not station.is_relative_to(root) or not output.is_relative_to(root) or output == root:
        raise ValueError("Experiment paths must remain under the station root")
    if sha256(station) != raw["station_sha256"]:
        raise ValueError("Station configuration hash changed")
    if not isinstance(raw["code_hashes"], dict) or not raw["code_hashes"]:
        raise ValueError("Missing code hashes")
    for name, digest in raw["code_hashes"].items():
        source = (root / name).resolve(strict=True)
        if not source.is_relative_to(root) or sha256(source) != digest:
            raise ValueError("Experiment code hash changed")
    deadline = datetime.fromisoformat(raw["deadline_utc"])
    if deadline.utcoffset() is None:
        raise ValueError("Deadline must include a timezone")
    deadline_s = deadline.timestamp()
    if deadline_s <= (time.time() if now is None else now):
        raise ValueError("Experiment deadline has passed")
    if not isinstance(raw["points"], list) or not 1 <= len(raw["points"]) <= 20:
        raise ValueError("Invalid number of experiment points")
    points = tuple(Point.parse(row) for row in raw["points"])
    poll = finite(raw.get("poll_interval_s", 1.5), "poll interval", 1.5, 10)
    heater = raw.get("heater_interval_s", 5)
    if heater is not None:
        heater = finite(heater, "heater interval", 5, 60)
    reserve = finite(raw.get("cleanup_reserve_s", 600), "cleanup reserve", 60, 1800)
    config = load_temperature_operation_config(station)
    if config.cryostat.temperature_min_k < 1.6 or config.cryostat.temperature_max_k > 300:
        raise ValueError("Experiment cannot expand the recorded absolute thermal range")
    if config.temperature_run.max_delta_k > 260 or config.temperature_run.max_overshoot_k > 15:
        raise ValueError("Experiment cannot expand the recorded thermal boundaries")
    for point in points:
        if not config.cryostat.temperature_min_k <= point.target_k <= config.cryostat.temperature_max_k:
            raise ValueError("Scientific target outside station limits")
    if any(point.method == "sequence" for point in points):
        certificate = raw.get("candidate_evidence")
        exact_keys(certificate, ("path", "sha256"))
        evidence_path = Path(certificate["path"]).resolve(strict=True)
        if not evidence_path.is_relative_to(root) or sha256(evidence_path) != certificate["sha256"]:
            raise ValueError("Candidate evidence changed")
        evidence = json.loads(evidence_path.read_text(encoding="utf8"))
        if evidence.get("independent_validation_passed") is not True:
            raise ValueError("Candidate has no independent validation")
        expected = [asdict(point) for point in points if point.method == "sequence"]
        if json.loads(json.dumps(expected)) != evidence.get("approved_points"):
            raise ValueError("Candidate differs from reviewed evidence")
    trial_points = [point for point in points if point.method == "peak_capture_trial"]
    if trial_points:
        if len(trial_points) != 1:
            raise ValueError("Only one exploratory peak-capture point is authorized per run")
        certificate = raw.get("peak_capture_trial_evidence")
        exact_keys(certificate, ("path", "sha256"))
        evidence_path = Path(certificate["path"]).resolve(strict=True)
        if not evidence_path.is_relative_to(root) or sha256(evidence_path) != certificate["sha256"]:
            raise ValueError("Peak-capture trial evidence changed")
        evidence = json.loads(evidence_path.read_text(encoding="utf8"))
        if (evidence.get("kind") != "operator_authorized_exploratory_peak_capture"
                or evidence.get("operator_authorized") is not True
                or evidence.get("independent_validation_passed") is not False
                or evidence.get("controller_validation_passed") is not False
                or not isinstance(evidence.get("expected_initial_state"), dict)
                or evidence.get("run_id") != raw["run_id"]):
            raise ValueError("Missing explicit exploratory trial authorization; no validated-control claim")
        expected = json.loads(json.dumps([asdict(point) for point in trial_points]))
        if expected != evidence.get("approved_points"):
            raise ValueError("Peak-capture commands differ from the frozen operator trial")
        sources = evidence.get("source_evidence")
        if not isinstance(sources, list) or len(sources) < 2:
            raise ValueError("Peak-capture trial lacks frozen response and prediction evidence")
        for source in sources:
            exact_keys(source, ("path", "sha256"))
            source_path = Path(source["path"]).resolve(strict=True)
            if not source_path.is_relative_to(root) or sha256(source_path) != source["sha256"]:
                raise ValueError("Peak-capture source evidence changed")
    return raw, config, points, deadline_s, poll, heater, reserve


class Archive:
    def __init__(self, output, run_id):
        self.directory = Path(output)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.events = (self.directory / "events.jsonl").open("x", encoding="utf8", newline="\n")
        self.run_id = run_id
        self.index = 0

    def emit(self, event, **values):
        self.index += 1
        row = {"run_id": self.run_id, "event_index": self.index,
               "captured_at_utc": utc_now(), "event": event, **values}
        self.events.write(json.dumps(row, allow_nan=False) + "\n")
        self.events.flush()
        os.fsync(self.events.fileno())

    def snapshot(self, values):
        temporary = self.directory / "state.pending.json"
        with temporary.open("w", encoding="utf8") as stream:
            json.dump({"run_id": self.run_id, "captured_at_utc": utc_now(), **values}, stream, allow_nan=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        # Windows readers/virus scanners can briefly deny replacement. Retry
        # only this closed, fsynced file; never repeat an instrument operation.
        delays = (0, 0.02, 0.05, 0.10, 0.20)
        errors = []
        for attempt, delay in enumerate(delays, 1):
            if delay:
                time.sleep(delay)
            try:
                os.replace(temporary, self.directory / "state.json")
            except OSError as exc:
                if getattr(exc, "winerror", None) not in (5, 32, 33):
                    raise
                errors.append(exc.winerror)
                if attempt == len(delays):
                    self.emit("state_snapshot_replace_retry", recovered=False,
                              attempts=attempt, winerrors=errors,
                              requested_retry_wait_s=sum(delays))
                    raise
            else:
                if errors:
                    self.emit("state_snapshot_replace_retry", recovered=True,
                              attempts=attempt, winerrors=errors,
                              requested_retry_wait_s=sum(delays[:attempt]))
                return

    def close(self):
        self.events.close()


class Experiment:
    """Single synchronous session; the caller holds every station alias lease."""
    def __init__(self, config, archive, *, monotonic=time.monotonic, wall_time=time.time,
                 sleep=time.sleep, deadline_s=math.inf, poll=1.5, heater_interval=5,
                 cleanup_reserve=600, stop_requested=lambda: False):
        self.config, self.archive = config, archive
        self.monotonic, self.wall_time, self.sleep = monotonic, wall_time, sleep
        self.global_end = monotonic() + max(0, deadline_s - wall_time() - cleanup_reserve)
        self.deadline_s, self.reserve = deadline_s, cleanup_reserve
        self.poll, self.heater_interval = poll, heater_interval
        self.stop_requested = stop_requested
        self.driver = None
        self.expected = None
        self.allowed = None
        self.field_target = None
        self.field_control = None
        self.point = None
        self.visit = -1
        self.phase = "preflight"
        self.cleaning = False
        self.mutated = False
        self.point_end = math.inf
        self.temperature_ceiling = None
        self.last_heater = -math.inf
        self.audit_errors = []
        self.last_state = None
        self.last_state_utc = None
        self.started = monotonic()
        self.results = []

    def budget(self):
        if self.cleaning:
            return
        if self.stop_requested():
            raise KeyboardInterrupt("Owned stop request")
        if (self.monotonic() >= min(self.global_end, self.point_end)
                or self.wall_time() >= self.deadline_s - self.reserve):
            raise AttoDryTimeout("Temperature experiment shared deadline exhausted")

    def emit(self, event, **data):
        try:
            self.archive.emit(event, visit=self.visit, phase=self.phase,
                              monotonic_s=self.monotonic() - self.started, **data)
        except BaseException as exc:
            if not self.cleaning:
                raise
            self.audit_errors.append(f"{type(exc).__name__}: {exc}")

    def validate(self, state, *, allow_disabled=False):
        c = self.config.cryostat
        if type(state.temperature_control_enabled) is not bool or type(state.field_control_enabled) is not bool:
            raise AttoDryError("Unknown cryostat control flag")
        if state.error_code or not c.temperature_min_k <= state.sample_temperature_k <= c.temperature_max_k:
            raise AttoDryError("Invalid cryostat error/temperature state")
        if not c.temperature_min_k <= state.user_temperature_k <= c.temperature_max_k:
            raise AttoDryError("Cryostat setpoint outside absolute range")
        if state.field.magnitude_t > 3 or state.field_setpoint.magnitude_t > 3:
            raise AttoDryError("Strict resultant field exceeds 3 T")
        if self.field_target is not None:
            if (abs(state.field_setpoint.bx_t-self.field_target.bx_t) > 1e-5
                    or abs(state.field_setpoint.bz_t-self.field_target.bz_t) > 1e-5
                    or state.field_control_enabled != self.field_control):
                raise AttoDryError("Unowned field setting/control change")
        if self.temperature_ceiling is not None and state.sample_temperature_k >= self.temperature_ceiling:
            raise AttoDryError("Temperature overshoot hard boundary reached")
        if self.allowed is not None and not any(abs(state.user_temperature_k-t) <= 1e-4 for t in self.allowed):
            raise AttoDryError("Unowned temperature setpoint change")
        if self.expected is not None and self.allowed is None:
            if (abs(state.user_temperature_k-self.expected) > 1e-4
                    or (not allow_disabled and not state.temperature_control_enabled)):
                raise AttoDryError("Temperature control/target changed")

    def captured(self, state):
        self.last_state = state
        self.last_state_utc = utc_now()
        self.emit("cryostat_state", state=asdict(state), scientific_target_k=self.point.target_k if self.point else None,
                  commanded_target_k=self.expected)
        if not self.cleaning:
            self.validate(state)
            self.budget()

    def heater(self):
        if self.heater_interval is None or self.monotonic()-self.last_heater < self.heater_interval:
            return
        self.budget()
        started = self.monotonic()
        value = self.driver.read_heater_powers()
        if not all(math.isfinite(v) and v >= 0 for v in (value.sample_w, value.vti_w)):
            raise AttoDryError("Invalid heater power")
        self.last_heater = self.monotonic()
        self.emit("heater_power", values=asdict(value), query_duration_s=self.monotonic()-started)
        self.budget()

    def command(self, target, *, initial=False):
        self.budget()
        before = self.driver.read_state()
        if abs(target-before.sample_temperature_k) > self.config.temperature_run.max_delta_k:
            raise AttoDryError("Temperature command exceeds max_delta_k")
        self.allowed = (before.user_temperature_k, target)
        self.emit("temperature_command_attempt", target_k=target, scientific_target_k=self.point.target_k)
        self.mutated = True
        if initial:
            self.driver.set_temperature_and_enable(target, monotonic=self.monotonic, sleeper=self.sleep)
        else:
            self.driver.set_temperature(target, monotonic=self.monotonic, sleeper=self.sleep)
        self.expected = target
        self.allowed = None
        state = self.driver.read_state()
        self.emit("temperature_command_confirmed", target_k=target, state=asdict(state))
        self.budget()

    def run_point(self, point, index):
        # A new point only starts when its entire timeout, hold and cleanup fit.
        required = point.max_wait_s + point.hold_s
        if min(self.global_end-self.monotonic(), self.deadline_s-self.reserve-self.wall_time()) < required:
            self.emit("point_not_started", label=point.label, reason="insufficient_deadline_budget")
            return None
        self.point, self.visit, self.phase = point, index, "arrival"
        self.point_end = math.inf
        before = self.driver.read_state()
        start_k = before.sample_temperature_k
        if abs(point.target_k-start_k) > self.config.temperature_run.max_delta_k:
            raise AttoDryError("Scientific target exceeds max_delta_k")
        ceiling = max(start_k, point.target_k) + self.config.temperature_run.max_overshoot_k
        if ceiling > self.config.cryostat.temperature_max_k:
            raise AttoDryError("Insufficient high-temperature overshoot headroom")
        self.temperature_ceiling = ceiling
        commands = point.commands or ((0.0, point.target_k),)
        if point.method == "peak_capture_trial":
            if (not before.temperature_control_enabled or not 110 <= start_k <= 135
                    or not .5 <= commands[0][1]-start_k <= 2.5
                    or not .5 <= point.target_k-start_k <= 2.5):
                raise AttoDryError("Peak-capture actual initial state is outside the warm-small-step envelope")
            ceiling = min(ceiling, point.target_k+.75)
            self.temperature_ceiling = ceiling
        if any(not min(start_k, point.target_k) <= t <= max(start_k, point.target_k) for _, t in commands):
            raise AttoDryError("Temporary target outside initial/final temperature interval")
        entered = self.monotonic()
        self.point_end = entered + point.max_wait_s
        self.emit("point_started", point=asdict(point), initial_state=asdict(before), temperature_ceiling_k=ceiling)
        pending = 0
        last_command = -math.inf
        samples = []
        final_confirmed = None
        first_arrival = None
        peak, minimum = start_k, start_k
        qualified = None
        last_print = -math.inf
        while qualified is None:
            self.budget()
            elapsed = self.monotonic()-entered
            if (pending < len(commands) and elapsed >= commands[pending][0]
                    and self.monotonic()-last_command >= 30):
                self.command(commands[pending][1], initial=pending == 0)
                last_command = self.monotonic()
                pending += 1
                samples.clear()
                if pending == len(commands):
                    final_confirmed = self.monotonic()
                    self.phase = "qualification"
                    self.emit("final_target_restored", target_k=point.target_k)
            state = self.driver.read_state()
            observed = self.monotonic()
            self.heater()
            elapsed = observed-entered
            actual = state.sample_temperature_k
            peak, minimum = max(peak, actual), min(minimum, actual)
            if first_arrival is None and abs(actual-point.target_k) <= CRITERIA.tolerance:
                first_arrival = elapsed
                self.emit("first_target_arrival", elapsed_s=elapsed, actual_k=actual, is_qualification=False)
            if final_confirmed is not None:
                samples.append(TimedValue(observed-final_confirmed, actual))
                cutoff = samples[-1].elapsed_s-CRITERIA.dwell_s
                while len(samples) > 2 and samples[1].elapsed_s < cutoff:
                    samples.pop(0)
                if evaluate_stability(samples, point.target_k, CRITERIA):
                    self.budget()
                    qualified = elapsed
            if point.method == "shadow" and samples:
                recent = [s for s in samples if s.elapsed_s >= samples[-1].elapsed_s-60]
                slope = ((recent[-1].value-recent[0].value)/(recent[-1].elapsed_s-recent[0].elapsed_s)
                         if len(recent) > 1 and recent[-1].elapsed_s > recent[0].elapsed_s else None)
                self.emit("shadow_prediction", slope_k_per_s=slope, predicted_qualification_s=None,
                          actuated=False)
            self.archive.snapshot({"status": "running", "pid": os.getpid(), "visit": index,
                "label": point.label, "phase": self.phase, "target_k": point.target_k,
                "commanded_target_k": self.expected, "elapsed_s": elapsed,
                "timeout_remaining_s": max(0, self.point_end-observed), "state": asdict(state),
                "state_captured_at_utc": self.last_state_utc,
                "completed_points": len(self.results)})
            self.budget()
            if observed-last_print >= 30:
                print(f"Temperature point: {index+1} | Phase: {self.phase} | "
                      f"Latest temperature: {actual:.6f} K | Target: {point.target_k:g} K | "
                      f"Elapsed: {elapsed:.1f} s", flush=True)
                last_print = observed
            if qualified is None:
                self.sleep(min(self.poll, max(0, self.point_end-self.monotonic())))
        self.emit("point_qualified", elapsed_s=qualified, first_arrival_s=first_arrival,
                  final_state=asdict(state), criteria=asdict(CRITERIA))
        self.phase = "diagnostic_hold"
        self.point_end = self.monotonic()+point.hold_s+self.poll+30
        hold_started = self.monotonic()
        hold_values = [state.sample_temperature_k]
        while self.monotonic()-hold_started < point.hold_s:
            self.sleep(min(self.poll, point.hold_s-(self.monotonic()-hold_started)))
            state = self.driver.read_state()
            self.heater()
            hold_values.append(state.sample_temperature_k)
            self.archive.snapshot({"status": "running", "pid": os.getpid(), "visit": index,
                "label": point.label, "phase": self.phase, "target_k": point.target_k,
                "state": asdict(state), "state_captured_at_utc": self.last_state_utc,
                "completed_points": len(self.results)})
            self.budget()
        self.point_end = math.inf
        result = {"label": point.label, "target_k": point.target_k, "method": point.method,
            "initial_actual_k": start_k, "actual_step_k": point.target_k-start_k,
            "first_arrival_s": first_arrival, "qualification_s": qualified,
            "maximum_actual_k": peak, "minimum_actual_k": minimum,
            "overshoot_k": max(0, peak-point.target_k) if point.target_k >= start_k else max(0, point.target_k-minimum),
            "hold_min_k": min(hold_values), "hold_max_k": max(hold_values),
            "hold_peak_to_peak_k": max(hold_values)-min(hold_values), "hold_s": point.hold_s,
            "completed_at_utc": utc_now(), "final_state": asdict(state)}
        self.emit("point_completed", result=result)
        self.results.append(result)
        print(f"Temperature point: {index+1} | Target: {point.target_k:g} K | "
              f"First arrival: {first_arrival:.1f} s | Qualified: {qualified:.1f} s | "
              f"Hold range: {result['hold_peak_to_peak_k']:.6f} K", flush=True)
        return result


def execute(config, points, archive, *, driver_factory=None, readiness=False, expected_initial_state=None, **options):
    experiment = Experiment(config, archive, **options)

    class AuditedDriver(AttoDryDriver):
        def read_state(self):
            state = super().read_state()
            experiment.captured(state)
            return state

    connected = False
    driver = None
    primary = None
    cleanup = {"errors": [], "temperature_disabled": None, "closed": False}
    status = "failed"
    try:
        if driver_factory is None:
            c = config.cryostat
            from .attodry import load_attodry_dll
            driver = AuditedDriver(dll=load_attodry_dll(c.dll_path), com_port=c.com_port,
                device_type=c.device_type, connection_timeout_s=c.connection_timeout_s,
                temperature_min_k=c.temperature_min_k, temperature_max_k=c.temperature_max_k,
                limits=config.magnet.limits, field_stability=config.magnet.stability,
                temperature_stability=config.temperature_stability,
                readback_tolerance_t=config.magnet.readback_tolerance_t,
                setpoint_ack_tolerance_t=config.magnet.setpoint_ack_tolerance_t,
                connection_authorized=True, writes_authorized=not readiness)
        else:
            driver = driver_factory(experiment)
        experiment.driver = driver
        driver.connect(monotonic=experiment.monotonic, sleeper=experiment.sleep)
        connected = True
        initial = driver.read_state()
        experiment.validate(initial)
        if expected_initial_state is not None:
            expected = expected_initial_state
            if (initial.temperature_control_enabled is not True
                    or initial.temperature_control_enabled != expected["temperature_control_enabled"]
                    or initial.field_control_enabled != expected["field_control_enabled"]
                    or abs(initial.user_temperature_k-expected["user_temperature_k"]) > 1e-4
                    or any(abs(getattr(initial.field_setpoint, axis)-expected["field_setpoint"][axis]) > 1e-5
                           for axis in ("bx_t", "bz_t"))):
                raise AttoDryError("Initial control/setpoint changed since trial readiness")
        experiment.field_target, experiment.field_control = initial.field_setpoint, initial.field_control_enabled
        experiment.emit("preflight", state=asdict(initial), readiness_only=readiness)
        experiment.heater()
        if readiness:
            status = "readiness_complete"
        else:
            for index, point in enumerate(points):
                if experiment.run_point(point, index) is None:
                    break
            if len(experiment.results) == len(points):
                status = "completed"
            elif experiment.results:
                status = "budget_complete"
            else:
                raise AttoDryTimeout("No complete point fits the remaining budget")
            experiment.phase = "normal_finish"
            state = driver.read_state()
            experiment.validate(state)
    except BaseException as exc:
        primary = f"{type(exc).__name__}: {exc}"
        status = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
    finally:
        experiment.cleaning = True
        experiment.phase = "cleanup"
        if connected and primary is not None and experiment.mutated:
            try:
                driver.ensure_temperature_control(False, monotonic=experiment.monotonic, sleeper=experiment.sleep)
                state = driver.read_state()
                cleanup["temperature_disabled"] = not state.temperature_control_enabled
                if not cleanup["temperature_disabled"]:
                    raise AttoDryError("Temperature disable was not confirmed")
            except BaseException as exc:
                cleanup["errors"].append(f"disable: {type(exc).__name__}: {exc}")
        if driver is not None:
            try:
                if connected:
                    final = driver.read_state()
                    experiment.validate(final, allow_disabled=primary is not None and experiment.mutated)
            except BaseException as exc:
                cleanup["errors"].append(f"final read: {type(exc).__name__}: {exc}")
            try:
                driver.close()
                cleanup["closed"] = True
            except BaseException as exc:
                cleanup["errors"].append(f"close: {type(exc).__name__}: {exc}")
        cleanup["errors"].extend(experiment.audit_errors)
        if cleanup["errors"]:
            status = "failed"
        summary = {"status": status, "pid": os.getpid(), "primary_error": primary,
                   "completed_points": len(experiment.results), "points": experiment.results,
                   "cleanup": cleanup,
                   "final_state_captured_at_utc": experiment.last_state_utc,
                   "final_state": asdict(experiment.last_state) if experiment.last_state else None}
        try:
            archive.emit("experiment_terminal", **summary)
            archive.snapshot(summary)
        finally:
            archive.close()
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description="Bounded temperature-only commissioning")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--authorize-temperature-experiment", action="store_true")
    parser.add_argument("--authorize-peak-capture-trial", action="store_true")
    parser.add_argument("--readiness", action="store_true")
    parser.add_argument("--preview", action="store_true")
    args = parser.parse_args(argv)
    raw, config, points, deadline, poll, heater, reserve = load_plan(args.plan)
    if args.preview:
        print(json.dumps({"run_id": raw["run_id"], "points": [asdict(p) for p in points],
            "deadline_utc": raw["deadline_utc"], "criteria": asdict(CRITERIA), "hardware_io": False}, indent=2))
        return 0
    if not args.authorize_temperature_experiment:
        parser.error("Explicit temperature experiment authorization is required")
    if any(p.method == "peak_capture_trial" for p in points) and not args.authorize_peak_capture_trial:
        parser.error("Explicit exploratory peak-capture trial authorization is required")
    root = Path(raw["root"])
    aliases = {p.resolve() for p in root.rglob("*.hardware-owner.lock")}
    aliases.add(Path(raw["station_config"]).resolve().with_suffix(".hardware-owner.lock"))
    with ExitStack() as stack:
        for path in sorted(aliases):
            station_alias = path.with_name(path.name.removesuffix(".hardware-owner.lock")+".toml")
            stack.enter_context(station_hardware_lease(station_alias))
        if not {p.resolve() for p in root.rglob("*.hardware-owner.lock")} <= aliases:
            raise RuntimeError("Station alias inventory changed")
        # Recheck all pinned files after waiting for/acquiring ownership.
        raw, config, points, deadline, poll, heater, reserve = load_plan(args.plan)
        if any(p.method == "peak_capture_trial" for p in points) and not args.authorize_peak_capture_trial:
            raise ValueError("Peak-capture trial authorization missing after lease acquisition")
        archive = Archive(raw["output_directory"], raw["run_id"])
        archive.emit("experiment_started", plan=raw, plan_sha256=sha256(args.plan),
                     lease_count=len(aliases), command="temperature_experiment", readiness_only=args.readiness)
        expected_initial_state = None
        if any(p.method == "peak_capture_trial" for p in points):
            proof = json.loads(Path(raw["peak_capture_trial_evidence"]["path"]).read_text(encoding="utf8"))
            expected_initial_state = proof["expected_initial_state"]
        result = execute(config, points, archive, readiness=args.readiness, deadline_s=deadline,
            poll=poll, heater_interval=heater, cleanup_reserve=reserve,
            expected_initial_state=expected_initial_state,
            stop_requested=lambda: (Path(raw["output_directory"])/"stop-request").exists())
    print(json.dumps({k: result[k] for k in ("status", "completed_points", "primary_error", "cleanup", "final_state")}, indent=2))
    return 0 if result["status"] in {"completed", "budget_complete", "readiness_complete"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
