"""Single-owner, fixed-range point acquisition with PEM-owned external frequency."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import Enum
from functools import wraps
import math
import time

from .lockin_backend import create_lockin_backend, capabilities_for
from .lockin_overload import PhotonicsStatusPolicy, overload_summary
from .models import LockinRole
from .reference_transients import ReferenceTransientError


def _now():
    return datetime.now(UTC).isoformat()


def _plain(value):
    if hasattr(value, "__dataclass_fields__"):
        return _plain(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


def _reference_operation(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        return self.run_reference_operation(method.__name__, lambda: method(self, *args, **kwargs))
    return guarded


@dataclass(frozen=True, slots=True)
class PhotonicsLockinGridPoint:
    source_v_rms: float
    frequency_hz: float
    excitation_index: int
    frequency_index: int = 0
    excitation_segment: int | None = None
    frequency_segment: int | None = None
    pem_reference_harmonic: int = 1

    def metadata(self):
        return {**asdict(self), "frequency_coordinate_source": "expected_external_reference"}


class PhotonicsLockinPointSession:
    def __init__(self, config_path, config, event, *, manager_factory=None, sleep=None, clock=None, mode="excitation"):
        if mode != "excitation":
            raise ValueError("PEM owns frequency; this profile only scans excitation amplitude.")
        self.config = config
        self.status_policy = PhotonicsStatusPolicy(config.overload_policy,
            reference_unlock_policy=config.reference_unlock_policy)
        self.event = event
        self.manager_factory = manager_factory
        self.sleep = time.sleep if sleep is None else sleep
        self.clock = time.monotonic if clock is None else clock
        self.grid = tuple(PhotonicsLockinGridPoint(v, config.reference_expected_hz, i,
                          pem_reference_harmonic=config.pem_reference_harmonic)
                          for i, v in enumerate(config.excitation_points_v_rms))
        self.roles = (config.lockin_xx, config.lockin_xy)
        self.harmonics_by_role = {c.role.value: c.harmonics for c in self.roles}
        self.point_harmonics = tuple(sorted(set(h for c in self.roles for h in c.harmonics)))
        self.skipped_by_frequency = {}
        self.manager = None
        self.resources = []
        self.pair = None
        self.preflight = None
        self.identities = {}
        self.current_harmonics = {c.role.value: c.harmonics[0] for c in self.roles}
        self.audit_cursors = {"xx": 0, "xy": 0}
        self.writes_started = False
        self.configured = False
        self.failed = False
        self.closed = False
        self.cleanup_result = None
        self.point_record = None
        self.target_source = None
        self.sample_count = 0
        self.reference_transition_pending = False
        self.reference_transition_target = None
        self.reference_wait_pending = False
        self.reference_recovery_guard = None
        self._reference_operation_context = None
        self._configuration_settings_applied = False

    def _fail(self, error):
        # A typed reference transient reaches the operation boundary before
        # poisoning the owned session. Every other error remains terminal.
        if not (isinstance(error, ReferenceTransientError)
                and self.config.reference_transient_policy == "wait_stable"
                and self._reference_operation_context is not None):
            self.failed = True

    def _recovery_deadline_check(self):
        context = self._reference_operation_context
        if context is not None and context["deadline"] is not None:
            if self.clock() >= context["deadline"]:
                raise TimeoutError("Reference recovery exhausted the shared operation deadline.")

    def _recovery_guard(self):
        self._recovery_deadline_check()
        context = self._reference_operation_context
        if self.reference_recovery_guard is not None and context is not None and context["deadline"] is not None:
            self.reference_recovery_guard(context["deadline"])
        self._recovery_deadline_check()

    def _wait(self, seconds):
        context = self._reference_operation_context
        if context is None or context["deadline"] is None:
            self.sleep(seconds)
            return
        until = self.clock() + seconds
        while self.clock() < until:
            self._recovery_guard()
            remaining = until - self.clock()
            if remaining > 0:
                self.sleep(min(1.0, remaining))
            self._recovery_deadline_check()

    def run_reference_operation(self, stage, operation, *, before_recovery=None, after_recovery=None):
        """Retry a complete operation within one first-fault recovery deadline.

        Combination acquisition supplies the outer boundary covering optical
        before/after brackets and all module reads. Nested lock-in methods do
        not retry a narrower window or reuse half of a rejected formal pair.
        Recovery changes no output; an optional station guard verifies optics,
        PEM and power during each poll and each second of filter settling.
        """
        if self._reference_operation_context is not None:
            self._recovery_deadline_check()
            result = operation()
            self._recovery_deadline_check()
            return result
        context = {"stage": stage, "deadline": None, "started": None, "sample_count": self.sample_count,
                   "formal_candidates": [], "retries": 0, "fully_recovered": False}
        self._reference_operation_context = context
        try:
            while True:
                self._recovery_deadline_check()
                try:
                    result = operation()
                    self._recovery_deadline_check()
                    return result
                except ReferenceTransientError as error:
                    if self.config.reference_transient_policy != "wait_stable":
                        raise
                    if context["deadline"] is None:
                        context["started"] = self.clock()
                        context["deadline"] = context["started"] + self.config.reference_recovery_timeout_s
                    self.sample_count = context["sample_count"]
                    rejected = _plain(context["formal_candidates"])
                    for candidate in rejected:
                        candidate.update(clean=False, valid_for_analysis=False,
                                         rejected_reference_candidate=True)
                        for entry in candidate.get("status", {}).get("samples", ()):
                            entry["valid_for_analysis_by_role"] = {"lockin_xx": False, "lockin_xy": False}
                    self.event("lockin_reference_operation_rejected", {
                        "stage": stage, "kind": error.kind, "evidence": _plain(error.evidence),
                        "rejected_formal_candidates": rejected, "valid_for_analysis": False,
                        "deadline_monotonic": context["deadline"], "retry": context["retries"]})
                    context["formal_candidates"] = []
                    context["fully_recovered"] = False
                    if before_recovery is not None:
                        before_recovery()
                    self._recover_reference(error)
                    context["fully_recovered"] = True
                    if after_recovery is not None:
                        after_recovery()
                    self._recovery_deadline_check()
                    context["retries"] += 1
        except BaseException as error:
            # Preserve harmless method precondition failures (for example a
            # formal read requested while a known PEM transition is pending).
            # Hardware-path failures already mark the session inside the method.
            if context["deadline"] is not None or isinstance(error, ReferenceTransientError):
                self.failed = True
            if context["deadline"] is not None:
                self.event("reference_recovery_failed", {"stage": stage,
                    "error_type": type(error).__name__, "error": str(error),
                    "elapsed_s": self.clock() - context["started"],
                    "remaining_s": max(0.0, context["deadline"] - self.clock()),
                    "deadline_monotonic": context["deadline"]})
            raise
        finally:
            self._reference_operation_context = None

    def _recover_reference(self, error):
        context = self._reference_operation_context
        self.event("reference_recovery_started", {
            "stage": context["stage"], "kind": error.kind, "evidence": _plain(error.evidence),
            "timeout_s": self.config.reference_recovery_timeout_s,
            "deadline_monotonic": context["deadline"],
            "required_good": self.config.reference_recovery_consecutive_good,
            "settle_s": self.config.settle_s, "output_policy": "preserve_and_verify"})
        consecutive = 0
        settle_until = None
        poll = 0
        while True:
            poll_started = self.clock()
            stable = False
            transient = None
            frequencies = None
            try:
                stable, frequencies = self._reference_recovery_probe(poll)
            except ReferenceTransientError as next_error:
                transient = {"kind": next_error.kind, "evidence": _plain(next_error.evidence)}
            finally:
                self._emit_audit("reference_recovery_poll")
            consecutive = consecutive + 1 if stable else 0
            self.event("reference_recovery_poll", {
                "poll": poll, "reference_hz": frequencies, "stable": stable,
                "consecutive_good": consecutive, "transient": transient,
                "required_good": self.config.reference_recovery_consecutive_good,
                "elapsed_s": self.clock() - context["started"],
                "remaining_s": max(0.0, context["deadline"] - self.clock()),
                "stage": context["stage"], "deadline_monotonic": context["deadline"],
                "settle_remaining_s": None if settle_until is None else max(0.0, settle_until - self.clock())})
            if stable and settle_until is not None and self.clock() >= settle_until:
                self.event("reference_recovery_recovered", {
                    "stage": context["stage"], "polls": poll + 1,
                    "reference_hz": frequencies, "locked": True, "settle_s": self.config.settle_s,
                    "elapsed_s": self.clock() - context["started"],
                    "remaining_s": max(0.0, context["deadline"] - self.clock()),
                    "deadline_monotonic": context["deadline"]})
                return
            if not stable:
                settle_until = None
            elif consecutive >= self.config.reference_recovery_consecutive_good and settle_until is None:
                settle_until = self.clock() + self.config.settle_s
            interval = max(0.0, 1.0 - (self.clock() - poll_started))
            if interval:
                self._recovery_deadline_check()
                self.sleep(interval)
                self._recovery_deadline_check()
            poll += 1

    def _reference_recovery_probe(self, poll):
        self._recovery_guard()
        self._verify_settings()
        source = self.pair[0].read_source_state()
        self._check_source(source, expected=self.target_source)
        self._verify_reference_output()
        stable = True
        range_evidence = None
        for cfg, device in zip(self.roles, self.pair):
            if device.read_identity() != self.identities[cfg.role.value]:
                raise ValueError("Instrument identity changed during reference recovery.")
            status = device.read_reference_status(current_status_supported=cfg.current_status_supported)
            problems = self._status_problems(cfg, status)
            observation = {"role": "lockin_" + cfg.role.value, "status": asdict(status),
                           "problems": problems, "poll": poll,
                           "deadline_monotonic": self._reference_operation_context["deadline"]}
            blocking = self.status_policy.apply(observation, problems)
            unlocks = self._reference_unlock_problems(cfg, status)
            # Recovery waits for actual locked evidence even when formal
            # unlock continuation is allowed. Its wait is not formal data.
            observation["waiting_reference_unlock_problems"] = unlocks
            range_changed = self._range_transient(cfg, status, problems)
            if range_changed:
                observation.update(valid_for_analysis=False, reference_transient_kind="frequency_range_changed")
            self.event("lockin_reference_recovery_status", observation)
            if range_changed:
                range_evidence = observation
                stable = False
            elif any(problem not in unlocks for problem in blocking):
                raise ValueError("Faulted/unknown recovery status: " + "; ".join(blocking))
            if status.locked is not True or unlocks:
                stable = False
            self._recovery_deadline_check()
        frequencies = {device.role.value: device.read_reference_frequency() for device in self.pair}
        self._check_frequencies(frequencies)
        pair = self._pair_samples(require_clean=True, allow_reference_unlock=True)
        if any(pair[cfg.role.value].status.locked is not True
               or self._reference_unlock_problems(cfg, pair[cfg.role.value].status) for cfg in self.roles):
            stable = False
        self._recovery_deadline_check()
        if range_evidence is not None:
            raise ReferenceTransientError("SR830 reference frequency range changed during recovery.",
                kind="frequency_range_changed", evidence=range_evidence)
        return stable, frequencies

    def _range_transient(self, cfg, status, problems):
        if cfg.model != "SR830":
            return False
        lia, errors = status.native_status
        if (lia is None or not lia.frequency_range_changed or lia.time_constant_changed
                or lia.triggered or lia.raw & 0x80 or errors != 0):
            return False
        blocking, _ = self.status_policy.partition(problems)
        range_only = {f"lockin_{cfg.role.value} unexpected settings change",
                      f"lockin_{cfg.role.value} reference unlocked"}
        return not any(problem not in range_only for problem in blocking)

    def _emit_audit(self, stage, *, suppress=False):
        errors = []
        if self.pair:
            for device in self.pair:
                role = device.role.value
                raw = device.audit[self.audit_cursors[role]:]
                if raw:
                    try:
                        self.event("lockin_model_io", {"stage": stage, "role": "lockin_" + role,
                            "model": device.capabilities.model.value, "raw": [asdict(item) for item in raw]})
                        self.audit_cursors[role] = len(device.audit)
                    except BaseException as exc:
                        if not suppress:
                            self.failed = True
                            raise
                        errors.append(f"audit {role}: {exc}")
        return errors

    def _require_open(self):
        if self.pair is None or self.closed:
            raise RuntimeError("Lock-in session is not open.")

    def _require_ready(self):
        self._require_open()
        if not self.configured or self.failed or self.cleanup_result is not None:
            raise RuntimeError("Lock-in session is not configured or has failed/finished.")

    def open(self):
        if self.manager is not None or self.closed:
            raise RuntimeError("A lock-in session can only be opened once.")
        factory = self.manager_factory
        if factory is None:
            def factory():
                import pyvisa
                return pyvisa.ResourceManager() if self.config.visa.backend == "default" else pyvisa.ResourceManager(self.config.visa.backend)
        try:
            self.manager = factory()
            devices = []
            for cfg in self.roles:
                resource = self.manager.open_resource(cfg.address)
                self.resources.append(resource)
                resource.timeout = self.config.visa.timeout_ms
                resource.read_termination = "\n"
                resource.write_termination = "\n"
                devices.append(create_lockin_backend(model=cfg.model, role=cfg.role, resource=resource))
            self.pair = tuple(devices)
            # Both identities are verified before any configuration is attempted.
            self.identities = {device.role.value: device.read_identity() for device in self.pair}
            if len(set(self.identities.values())) != 2:
                raise ValueError("Both roles returned the same physical instrument identity.")
            self.preflight = {device.role.value: asdict(device.read_settings()) for device in self.pair}
            self.current_harmonics = {role: settings["harmonic"] for role, settings in self.preflight.items()}
            source = self.pair[0].read_source_state()
            self._check_source(source, expected=None)
            reference_output = self._verify_reference_output()
            self.event("lockin_preflight", {"roles": self.preflight, "source": source,
                "source_wiring": asdict(self.config.source), "reference_topology": self.config.reference_topology,
                "reference_output": reference_output})
            self._emit_audit("open")
            return {"roles": self.preflight, "source": source, "source_wiring": asdict(self.config.source),
                    "reference_output": reference_output}
        except BaseException as exc:
            self.failed = True
            self._emit_audit("open_failed", suppress=True)
            try:
                self.close()
            except BaseException as close_error:
                exc.add_note(f"Resource cleanup also failed: {close_error}")
            raise

    def _check_source(self, source, *, expected):
        value = source["source_voltage_v"]
        if not self.config.minimum_source_voltage_v <= value <= self.config.maximum_source_voltage_v:
            raise ValueError("Actual XX source amplitude exceeds the approved policy.")
        if source["dc_offset_v"] != 0 or source["dc_mode"] != self.config.source.dc_mode:
            raise ValueError("XX source zero-DC/mode readback is not confirmed.")
        if expected is not None and not math.isclose(value, expected, rel_tol=1e-6, abs_tol=1e-12):
            raise ValueError("XX source amplitude differs from the selected point.")

    def _source(self, value, *, protect=False):
        if self.pair[0].read_identity() != self.identities["xx"]:
            raise ValueError("XX identity changed; no excitation write is allowed.")
        if not protect:
            self._verify_reference_output()
        if self._reference_operation_context is not None:
            self._reference_operation_context["fully_recovered"] = False
        return self.pair[0].set_source_amplitude(value,
            minimum_v=self.config.minimum_source_voltage_v,
            maximum_v=self.config.maximum_source_voltage_v,
            expected_dc_mode=self.config.source.dc_mode,
            authorize_writes=True, protect=protect)

    def _verify_reference_output(self):
        """Preserve XY SINE amplitude/DC; it is a clock, never sample excitation."""
        cfg = self.config.reference_output
        if cfg is None:
            return None
        if self.pair[1].read_identity() != self.identities["xy"]:
            raise ValueError("XY reference identity changed.")
        state = self.pair[1].read_source_state()
        if (not math.isclose(state["source_voltage_v"], cfg.amplitude_v_rms, rel_tol=1e-6, abs_tol=1e-12)
                or state["dc_offset_v"] != cfg.dc_offset_v or state["dc_mode"] != cfg.dc_mode):
            raise ValueError("XY SINE reference amplitude/DC differs from its preserved configuration.")
        return {"configuration": asdict(cfg), "readback": state, "policy": "preserve_read_only"}

    def protect_source(self):
        """Confirm the approved low AC output before a PEM frequency transition."""
        self._require_open()
        if self.failed or self.cleanup_result is not None:
            raise RuntimeError("Cannot begin source protection after session failure/cleanup.")
        try:
            self.writes_started = True
            source = self._source(self.config.cleanup_source_voltage_v, protect=True)
            self._check_source(source, expected=None)
            if source["source_voltage_v"] > self.config.cleanup_source_voltage_v + 1e-12:
                raise ValueError("Source protection target is not confirmed.")
            result = {"verified": True, "source": source, "source_wiring": asdict(self.config.source)}
            self.event("lockin_source_protected", result)
            return result
        except BaseException:
            self.failed = True
            raise
        finally:
            self._emit_audit("protect_source", suppress=self.failed)

    @_reference_operation
    def prepare_reference_transition(self):
        """Hold approved low AC while PEM settings can disturb the external clock."""
        self._require_ready()
        if self.reference_transition_pending:
            raise RuntimeError("A PEM reference transition is already pending.")
        try:
            self._verify_settings()
            self._coordinates()
            self._pair_samples(require_clean=True)
            desired = self.target_source
            protection = self.protect_source()
            self.reference_transition_target = desired
            self.target_source = protection["source"]["source_voltage_v"]
            self.reference_transition_pending = True
            self.reference_wait_pending = True
            return {**protection, "restore_source_voltage_v": desired}
        except BaseException as error:
            self._fail(error)
            raise
        finally:
            self._emit_audit("prepare_reference_transition", suppress=self.failed)

    @_reference_operation
    def restore_source_after_reference_transition(self):
        """Requalify the new reference before restoring the selected excitation."""
        self._require_ready()
        if not self.reference_transition_pending:
            return self.read_coordinates()
        try:
            self._verify_settings()
            self._coordinates()
            context = self._reference_operation_context
            if (context is not None and context["stage"] == "restore_source_after_reference_transition"
                    and context["retries"] > 0 and context["fully_recovered"]):
                # Recovery has already observed the protected clock through
                # the complete filter dwell. This reentry has made no write;
                # retain fresh strict probes instead of waiting a second dwell.
                pair = self._pair_samples(require_clean=True)
                settings = self._verify_settings()
                actual, raw = self._coordinates()
                self.event("lockin_reference_settling_reused", {
                    "stage": context["stage"], "settle_s": self.config.settle_s,
                    "deadline_monotonic": context["deadline"], "source_settings_written": False,
                    "samples": _plain(pair), "settings": _plain(settings), "actual": actual, "raw": raw})
            else:
                self._transition(allow_reference_unlock=True)
            target = self.reference_transition_target
            if target is not None:
                self._source(target)
                self.target_source = target
                self._transition()
            self.reference_transition_pending = False
            self.reference_transition_target = None
            result = self._coordinates()[0]
            self.event("lockin_reference_transition_completed", {"actual": result})
            return result
        except BaseException as error:
            self._fail(error)
            raise
        finally:
            self._emit_audit("restore_after_reference_transition", suppress=self.failed)

    def _check_frequencies(self, frequencies):
        for cfg in self.roles:
            value = frequencies[cfg.role.value]
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Actual external reference must be positive and finite.")
            for harmonic in cfg.harmonics:
                capabilities_for(cfg.model).validate_detection(value, harmonic)
        for cfg in self.roles:
            value = frequencies[cfg.role.value]
            if not self.config.reference_min_hz <= value <= self.config.reference_max_hz:
                raise ReferenceTransientError("Actual external reference left the approved interval.",
                    kind="reference_interval", evidence={"role": cfg.role.value, "reference_hz": value,
                        "reference_min_hz": self.config.reference_min_hz,
                        "reference_max_hz": self.config.reference_max_hz})
        if abs(frequencies["xx"] - frequencies["xy"]) > self.config.pair_tolerance_hz:
            raise ReferenceTransientError("XX and XY external references disagree.",
                kind="pair_frequency_mismatch", evidence={"reference_hz": dict(frequencies),
                    "pair_tolerance_hz": self.config.pair_tolerance_hz})

    @_reference_operation
    def wait_for_reference_lock(self):
        """Poll at one-second cadence, retaining explicitly continued unlocks."""
        self._require_open()
        if self.failed or self.cleanup_result is not None:
            raise RuntimeError("Cannot wait for reference after session failure/cleanup.")
        if not self.reference_wait_pending:
            return
        try:
            timeout_s = self.config.reference_lock_timeout_s
            if timeout_s:
                started = self.clock()
                deadline = started + timeout_s
                context = self._reference_operation_context
                if context is not None and context["deadline"] is not None:
                    deadline = min(deadline, context["deadline"])
                payload = {"timeout_s": timeout_s, "poll_interval_s": 1.0,
                           "reference_topology": self.config.reference_topology,
                           "reference_unlock_policy": self.config.reference_unlock_policy}
                self.event("lockin_reference_wait_started", payload)
                poll_index = 0
                ready = False
                last_observations = {}
                last_statuses = {}

                def check_deadline():
                    self._recovery_deadline_check()
                    if self.clock() >= deadline:
                        raise TimeoutError("External references did not lock before reference_lock_timeout_s.")

                while True:
                    # Only the scheduled wait may reach the lock deadline and
                    # continue. Slow/failed instrument or audit I/O still fails.
                    self._recovery_deadline_check()
                    if self.clock() >= deadline:
                        break
                    poll_started = self.clock()
                    if context is not None and context["deadline"] is not None:
                        self._recovery_guard()
                    ready = True
                    for cfg, device in zip(self.roles, self.pair):
                        check_deadline()
                        if device.read_identity() != self.identities[cfg.role.value]:
                            raise ValueError("Lock-in identity changed during reference polling.")
                        check_deadline()
                        status = device.read_reference_status(current_status_supported=cfg.current_status_supported)
                        observation = {"role": "lockin_" + cfg.role.value,
                            "poll_index": poll_index, "elapsed_s": self.clock() - started,
                            "status": asdict(status), "problems": self._status_problems(
                                cfg, status, allow_reference_unlock=True,
                                allow_settings_change=not (self.configured
                                    and self.config.reference_transient_policy == "wait_stable"))}
                        self.status_policy.apply(observation, observation["problems"])
                        self.event("lockin_reference_status", observation)
                        last_observations[cfg.role.value] = observation
                        last_statuses[cfg.role.value] = status
                        # A consuming query is never silently discarded, even if it
                        # returns a clean status after the timeout.
                        check_deadline()
                        ready = self._reference_status_ready(cfg, status) and ready
                    self._emit_audit("reference_poll")
                    if ready:
                        frequencies = {device.role.value: device.read_reference_frequency() for device in self.pair}
                        self._check_frequencies(frequencies)
                        check_deadline()
                        self.event("lockin_reference_wait_finished", {**payload,
                            "elapsed_s": self.clock() - started, "polls": poll_index + 1,
                            "reference_hz": frequencies, "locked": True, "timed_out": False})
                        check_deadline()
                        break
                    check_deadline()
                    remaining = deadline - self.clock()
                    interval_remaining = max(0.0, 1.0 - (self.clock() - poll_started))
                    if interval_remaining:
                        self.sleep(min(interval_remaining, remaining))
                        self._recovery_deadline_check()
                    poll_index += 1
                if not ready:
                    if self.config.reference_unlock_policy != "record_continue":
                        raise TimeoutError("External references did not lock before reference_lock_timeout_s.")
                    # A persistent configuration transition cannot be treated
                    # as an unlock exception merely because the clock expired.
                    for cfg in self.roles:
                        status = last_statuses[cfg.role.value]
                        native = status.native_status
                        changed = ((native[0].frequency_range_changed or native[0].time_constant_changed)
                                   if cfg.model == "SR830" else native.configuration_changed_latched)
                        if changed:
                            raise ValueError("Reference polling timed out with an unresolved settings change.")
                    remaining = list(dict.fromkeys(problem for cfg in self.roles
                        for problem in self._reference_unlock_problems(cfg, last_statuses[cfg.role.value])))
                    if not remaining:
                        raise ValueError("Reference polling timed out without known reference-unlock evidence.")
                    outcome = {**payload, "elapsed_s": self.clock() - started,
                        "polls": poll_index, "locked": False, "timed_out": True,
                        "remaining_status_by_role": last_observations, "problems": remaining}
                    self.status_policy.apply(outcome, remaining)
                    self.event("lockin_reference_wait_timed_out", outcome)
                    frequencies = {device.role.value: device.read_reference_frequency() for device in self.pair}
                    self._check_frequencies(frequencies)
                    self.event("lockin_reference_wait_finished", {**outcome,
                        "elapsed_s": self.clock() - started, "reference_hz": frequencies})
            self.reference_wait_pending = False
        except BaseException as error:
            self._fail(error)
            raise
        finally:
            self._emit_audit("reference_wait", suppress=self.failed)

    def _reference_status_ready(self, cfg, status):
        problems = self._status_problems(
            cfg, status, allow_reference_unlock=True,
            allow_settings_change=not (self.configured and self.config.reference_transient_policy == "wait_stable"))
        blocking, _ = self.status_policy.partition(problems)
        if (self.config.reference_transient_policy == "wait_stable"
                and self._range_transient(cfg, status, problems)):
            raise ReferenceTransientError("SR830 reference frequency range changed during reference polling.",
                kind="frequency_range_changed", evidence={"role": cfg.role.value, "status": _plain(status)})
        if blocking:
            raise ValueError("Faulted/unknown reference polling status: " + "; ".join(blocking))
        native = status.native_status
        if cfg.model == "SR830":
            lia, _ = native
            return (status.locked is True and not lia.frequency_range_changed
                    and not lia.time_constant_changed)
        return (status.locked is True and (not native.reference_unlock_latched
                or self.config.reference_unlock_policy == "record_continue")
                and not native.configuration_changed_latched)

    def _reference_unlock_problems(self, cfg, status):
        unlocked = status.locked is False
        if cfg.model == "SR830":
            lia, _ = status.native_status
            unlocked |= lia is not None and lia.reference_unlocked
        else:
            unlocked |= status.native_status.reference_unlock_latched is True
        return [f"lockin_{cfg.role.value} reference unlocked"] if unlocked else []

    def _overload_problems(self, cfg, status):
        """Keep model-specific current and latched overload evidence distinct."""
        role = "lockin_" + cfg.role.value
        if cfg.model == "SR830":
            lia, _ = status.native_status
            if lia is None:
                return []
            flags = ((lia.input_or_reserve_overload, "input/reserve overload"),
                     (lia.filter_overload, "filter overload"),
                     (lia.output_overload, "output-scale overload"))
        else:
            native = status.native_status
            flags = ((status.input_overload is True or native.input_overload_latched is True,
                      "input/reserve overload"),
                     (status.output_scale_overload is True or native.output_scale_overload_latched is True,
                      "output-scale overload"))
        return [f"{role} {name}" for flagged, name in flags if flagged]

    def _status_problems(self, cfg, status, *, allow_reference_unlock=False, allow_settings_change=False):
        role = "lockin_" + cfg.role.value
        problems = self._overload_problems(cfg, status)
        if any(value is None for value in (status.locked, status.input_overload,
                                          status.output_scale_overload, status.instrument_error)):
            problems.append(f"{role} unknown status")
        if status.instrument_error is True:
            problems.append(f"{role} instrument error")
        unlocked = status.locked is False
        if cfg.model == "SR830":
            lia, errors = status.native_status
            if lia is None or errors is None or (lia is not None and lia.raw & 0x80):
                problems.append(f"{role} unknown status")
            if errors not in (None, 0):
                problems.append(f"{role} instrument error")
            if lia is not None:
                unlocked |= lia.reference_unlocked
                changed = lia.frequency_range_changed or lia.time_constant_changed
                if lia.triggered:
                    problems.append(f"{role} unexpected trigger event")
            else:
                changed = False
        else:
            native = status.native_status
            if (native.unknown_status_bits or not native.consumed_status_latches
                    or any(value is None for value in (native.reference_unlock_latched,
                        native.input_overload_latched, native.output_scale_overload_latched,
                        native.filter_fault_latched, native.configuration_changed_latched, native.power_on_latched))):
                problems.append(f"{role} unknown status")
            if native.filter_fault_latched:
                problems.append(f"{role} synchronous filter fault")
            if native.power_on_latched:
                problems.append(f"{role} power-on event")
            unlocked |= native.reference_unlock_latched is True
            changed = native.configuration_changed_latched
        if unlocked and (not allow_reference_unlock
                or self.config.reference_unlock_policy == "record_continue"):
            problems.append(f"{role} reference unlocked")
        if changed and not allow_settings_change:
            problems.append(f"{role} unexpected settings change")
        if (status.clean is not True and not problems
                and not (unlocked and allow_reference_unlock or changed and allow_settings_change)):
            problems.append(f"{role} unknown status")
        return list(dict.fromkeys(problems))

    def _coordinates(self, *, wait_for_lock=True):
        if wait_for_lock:
            self.wait_for_reference_lock()
        source = self.pair[0].read_source_state()
        self._check_source(source, expected=self.target_source)
        reference_output = self._verify_reference_output()
        frequencies = {device.role.value: device.read_reference_frequency() for device in self.pair}
        self._check_frequencies(frequencies)
        return {"lockin_excitation_v_rms": source["source_voltage_v"],
                "lockin_frequency_hz": frequencies["xx"]}, {"source": source, "reference_hz": frequencies,
                                                           "reference_output": reference_output}

    @_reference_operation
    def read_coordinates(self):
        """Guarded public readback for cross-checking the PEM reference owner."""
        self._require_ready()
        try:
            self._verify_settings()
            return self._coordinates()[0]
        except BaseException as error:
            self._fail(error)
            raise
        finally:
            self._emit_audit("read_coordinates", suppress=self.failed)

    @_reference_operation
    def read_reference_frequencies(self, *, wait_for_lock=True):
        """Read and validate both role references for comparison with the PEM."""
        self._require_ready()
        try:
            self._verify_settings()
            return self._coordinates(wait_for_lock=wait_for_lock)[1]["reference_hz"]
        except BaseException as error:
            self._fail(error)
            raise
        finally:
            self._emit_audit("read_reference_frequencies", suppress=self.failed)

    def _verify_settings(self):
        result = {}
        for cfg, device in zip(self.roles, self.pair):
            actual = device.read_settings()
            if actual.identity != self.identities[cfg.role.value]:
                raise ValueError("Instrument identity changed inside the owned session.")
            if (actual.reference_source != "external" or actual.time_constant_s != cfg.time_constant_s
                    or actual.sensitivity_full_scale_v != cfg.sensitivity_full_scale_v
                    or actual.harmonic != self.current_harmonics[cfg.role.value]):
                raise ValueError("Lock-in reference/filter/range/harmonic settings changed unexpectedly.")
            native = actual.native_settings
            if cfg.model == "SR830":
                expected_edge = {"sine_zero_crossing": 0, "rising": 1, "falling": 2}[cfg.external_reference_edge]
                reserve = {"high_reserve": 0, "normal": 1, "low_noise": 2}[cfg.reserve_mode]
                correct = (native.reference_slope == expected_edge and native.input_mode == 1
                    and native.shield_grounding == (0 if cfg.shield_grounding == "float" else 1) and native.input_coupling == 0
                    and native.line_filter == 0 and native.filter_slope == 3 and native.reserve_mode == reserve)
            else:
                correct = (native.external_reference_edge == cfg.external_reference_edge
                    and native.reference_input_impedance_ohm == cfg.reference_input_impedance_ohm
                    and native.input_range_v_peak == cfg.input_range_v_peak
                    and not native.advanced_filter and not native.synchronous_filter
                    and native.filter_slope_db_oct == 24 and native.input_mode == "a_minus_b"
                    and native.input_coupling == "ac" and native.shield_grounding == cfg.shield_grounding
                    and native.sync_output_mode == (self.preflight[cfg.role.value]["native_settings"]["sync_output_mode"]
                        if cfg.sync_output_mode == "preserve" else cfg.sync_output_mode))
            if not correct or not math.isclose(native.phase_shift_deg, cfg.phase_shift_deg, abs_tol=0.001, rel_tol=0):
                raise ValueError("Lock-in model-specific settings differ from the configured profile.")
            result[cfg.role.value] = asdict(actual)
        return result

    def _pair_samples(self, *, require_clean, allow_reference_unlock=False):
        result = {}
        for cfg, device in zip(self.roles, self.pair):
            frequency_check = ({"reference_frequency_tolerance_hz": self.config.pair_tolerance_hz}
                               if cfg.model == "SR865A" else {})
            try:
                sample = device.read_sample(consume_status_latches=True,
                                           current_status_supported=cfg.current_status_supported,
                                           **frequency_check)
            except ReferenceTransientError as error:
                error.evidence["partial_samples_by_role"] = _plain(result)
                raise
            result[cfg.role.value] = sample
            problems = self._status_problems(cfg, sample.status,
                allow_reference_unlock=allow_reference_unlock, allow_settings_change=not require_clean)
            observation = {"role": "lockin_" + cfg.role.value,
                "formal_candidate": require_clean, **frequency_check, "sample": asdict(sample),
                "pem_reference_harmonic": self.config.pem_reference_harmonic,
                "pem_harmonic": self.config.pem_reference_harmonic * sample.harmonic,
                "problems": problems, "valid_for_analysis": sample.status.clean is True}
            blocking = self.status_policy.apply(observation, problems)
            range_changed = require_clean and self._range_transient(cfg, sample.status, problems)
            if range_changed:
                observation.update(valid_for_analysis=False, reference_transient_kind="frequency_range_changed")
            # Emit each role immediately so a companion failure retains this sample.
            self.event("lockin_raw_role", observation)
            if sample.harmonic != self.current_harmonics[cfg.role.value]:
                raise ValueError("Unexpected harmonic during lock-in acquisition.")
            if range_changed:
                raise ReferenceTransientError("SR830 reference frequency range changed.",
                    kind="frequency_range_changed", evidence={"role": cfg.role.value,
                        "sample": _plain(sample), "partial_samples_by_role": _plain(result)})
            if blocking:
                raise ValueError("Faulted/unknown lock-in status: " + "; ".join(blocking))
        self._check_frequencies({role: sample.reference_frequency_hz for role, sample in result.items()})
        return result

    def _transition(self, *, allow_reference_unlock=False):
        # Transition samples retain the old latch window and are never accepted.
        before = self._pair_samples(require_clean=False, allow_reference_unlock=allow_reference_unlock)
        self._wait(self.config.settle_s)
        settled = self._pair_samples(require_clean=True)
        self._verify_settings()
        self._coordinates()
        return before, settled

    @_reference_operation
    def configure(self):
        self._require_open()
        if self.configured or self.failed or self.cleanup_result is not None:
            raise RuntimeError("Configuration cannot be repeated or resumed after failure.")
        try:
            for device in self.pair:
                if device.read_identity() != self.identities[device.role.value]:
                    raise ValueError("Instrument identity changed before configuration.")
            source = self.protect_source()["source"]
            if not self._configuration_settings_applied:
                for cfg, device in zip(self.roles, self.pair):
                    device.configure_external_reference(edge=cfg.external_reference_edge,
                        input_impedance_ohm=cfg.reference_input_impedance_ohm,
                        sync_output_mode=cfg.sync_output_mode, reference_source=cfg.reference_source,
                        authorize_writes=True)
                    device.configure_fixed_measurement(cfg, authorize_writes=True)
                self._configuration_settings_applied = True
                if self.config.reference_lock_timeout_s == 0:
                    # With the legacy zero wait, consume the owned settings
                    # writes before a frequency fault enters recovery. This
                    # audited baseline cannot hide later TC/config events.
                    self._verify_settings()
                    for cfg, device in zip(self.roles, self.pair):
                        status = device.read_reference_status(current_status_supported=cfg.current_status_supported)
                        problems = self._status_problems(cfg, status,
                            allow_reference_unlock=True, allow_settings_change=True)
                        observation = {"role": "lockin_" + cfg.role.value,
                            "status": asdict(status), "problems": problems,
                            "stage": "owned_configuration_writes", "valid_for_analysis": False}
                        blocking = self.status_policy.apply(observation, problems)
                        self.event("lockin_configuration_status_baseline", observation)
                        if blocking:
                            raise ValueError("Faulted/unknown configuration baseline: " + "; ".join(blocking))
            self.reference_wait_pending = True
            self.wait_for_reference_lock()
            # Validate every role before the first harmonic-setting write.
            frequencies = {device.role.value: device.read_reference_frequency() for device in self.pair}
            self._check_frequencies(frequencies)
            for cfg, device in zip(self.roles, self.pair):
                if self.current_harmonics[cfg.role.value] != cfg.harmonics[0]:
                    device.set_harmonic(cfg.harmonics[0], authorize_writes=True)
                    self.current_harmonics[cfg.role.value] = cfg.harmonics[0]
            self._transition(allow_reference_unlock=True)
            self.configured = True
            self.event("lockin_configured", {"reference_topology": self.config.reference_topology,
                "harmonics_by_role": self.harmonics_by_role, "source": source,
                "overload_policy": self.config.overload_policy,
                "reference_unlock_policy": self.config.reference_unlock_policy})
        except BaseException as error:
            self._fail(error)
            raise
        finally:
            self._emit_audit("configure", suppress=self.failed)

    @_reference_operation
    def set_point(self, source_v, frequency_hz=None, *, point_index=None):
        self._require_ready()
        frequency = self.config.reference_expected_hz if frequency_hz is None else frequency_hz
        if point_index is None:
            point_index = next((i for i, p in enumerate(self.grid)
                if (p.source_v_rms, p.frequency_hz) == (source_v, frequency)), -1)
        if type(point_index) is not int or not 0 <= point_index < len(self.grid):
            raise ValueError("Point is outside the validated photonics lock-in grid.")
        point = self.grid[point_index]
        if isinstance(source_v, bool) or (point.source_v_rms, point.frequency_hz) != (source_v, frequency):
            raise ValueError("Point coordinates do not match the configured grid; external frequency cannot be swept.")
        try:
            if self.reference_transition_pending:
                # An outer optical axis may move before the inner excitation
                # axis is selected. Keep AC protected until PEM requalification.
                self.reference_transition_target = source_v
                self.point_record = {"point_index": point_index, **point.metadata(), "samples": [],
                                     "source_wiring": asdict(self.config.source)}
                self.sample_count = 0
                self.event("lockin_excitation_deferred", {"target_source_voltage_v": source_v,
                    "reason": "PEM_reference_transition", "point_index": point_index})
                return self.read_coordinates()
            self._verify_settings()
            self._coordinates()
            self._pair_samples(require_clean=True)
            self._source(source_v)
            self.target_source = source_v
            self.point_record = {"point_index": point_index, **point.metadata(), "samples": [],
                                 "source_wiring": asdict(self.config.source)}
            self._transition()
            self.sample_count = 0
            return self._coordinates()[0]
        except BaseException as error:
            self._fail(error)
            raise
        finally:
            self._emit_audit("set_point", suppress=self.failed)

    @_reference_operation
    def qualify(self):
        self._require_ready()
        if self.reference_transition_pending:
            raise RuntimeError("PEM reference must be requalified before restoring excitation.")
        if self.point_record is None:
            raise RuntimeError("A source point must be selected before qualification.")
        try:
            self._wait(self.config.settle_s)
            self._verify_settings()
            actual, raw = self._coordinates()
            self._pair_samples(require_clean=True)
            self.event("lockin_point_qualification", {"actual": actual, "raw": raw})
            self.sample_count = 0
            return actual
        except BaseException as error:
            self._fail(error)
            raise
        finally:
            self._emit_audit("qualify", suppress=self.failed)

    @_reference_operation
    def sample_point(self, *, measurement_context=None):
        self._require_ready()
        if self.reference_transition_pending:
            raise RuntimeError("No formal sample is allowed during a PEM reference transition.")
        if self.point_record is None:
            raise RuntimeError("A source point must be selected before sampling.")
        record = {**self.point_record, "schema_version": "photonics-lockin-v1", "samples": [],
                  "identities": dict(self.identities), "sequential_role_reads": True,
                  "overload_policy": self.config.overload_policy,
                  "reference_unlock_policy": self.config.reference_unlock_policy}
        measured = {}
        try:
            if self.sample_count:
                self._wait(self.config.sample_interval_s)
            for index in range(max(len(cfg.harmonics) for cfg in self.roles)):
                selected = {cfg.role.value: cfg.harmonics[index] for cfg in self.roles if index < len(cfg.harmonics)}
                candidate = {"selected_harmonics": selected, "bracket_samples": {},
                             "valid_for_analysis": False}
                record["candidate_in_progress"] = candidate
                self._verify_settings()
                self._coordinates()
                bracket = {"before": self._pair_samples(require_clean=True)}
                candidate["bracket_samples"]["before"] = _plain(bracket["before"])
                changed = any(self.current_harmonics[role] != harmonic for role, harmonic in selected.items())
                if changed:
                    for cfg, device in zip(self.roles, self.pair):
                        if cfg.role.value in selected:
                            harmonic = selected[cfg.role.value]
                            device.set_harmonic(harmonic, authorize_writes=True)
                            self.current_harmonics[cfg.role.value] = harmonic
                    bracket["transition"], bracket["settled"] = self._transition(allow_reference_unlock=True)
                    candidate["bracket_samples"].update(
                        {stage: _plain(bracket[stage]) for stage in ("transition", "settled")})
                settings_before = self._verify_settings()
                candidate["settings_before"] = _plain(settings_before)
                actual, coordinate_before = self._coordinates()
                candidate["coordinates_before"] = _plain(coordinate_before)
                before = None if measurement_context is None else measurement_context("before", None, self.sample_count)
                pair = self._pair_samples(require_clean=True)
                candidate["samples"] = _plain(pair)
                after = None if measurement_context is None else measurement_context("after", None, self.sample_count)
                actual, coordinate_after = self._coordinates()
                candidate["coordinates_after"] = _plain(coordinate_after)
                settings_after = self._verify_settings()
                candidate["settings_after"] = _plain(settings_after)
                bracket["after"] = self._pair_samples(require_clean=True)
                candidate["bracket_samples"]["after"] = _plain(bracket["after"])
                entry = {"selected_harmonics": selected, "samples": {r: asdict(v) for r, v in pair.items()},
                    "harmonic_metadata": {role: {
                        "pem_reference_harmonic": self.config.pem_reference_harmonic,
                        "pem_harmonic": self.config.pem_reference_harmonic * harmonic,
                        "reference_frequency_hz": pair[role].reference_frequency_hz,
                        "detection_frequency_hz": pair[role].detection_frequency_hz,
                    } for role, harmonic in selected.items()},
                    "bracket_samples": {stage: {r: asdict(v) for r, v in probe.items()}
                                        for stage, probe in bracket.items()},
                    "coordinates_before": coordinate_before, "coordinates_after": coordinate_after,
                    "settings_before": settings_before, "settings_after": settings_after,
                    "settings_verified": True, "measurement_context": {"before": before, "after": after}}
                by_role = {"lockin_" + cfg.role.value: list(dict.fromkeys(
                    problem for probe in (*bracket.values(), pair)
                    for problem in (*self._overload_problems(cfg, probe[cfg.role.value].status),
                        *(self._reference_unlock_problems(cfg, probe[cfg.role.value].status)
                          if self.config.reference_unlock_policy == "record_continue" else ()))))
                    for cfg in self.roles}
                entry["problems_by_role"] = by_role
                entry["problems"] = [problem for problems in by_role.values() for problem in problems]
                self.status_policy.apply(entry, entry["problems"])
                reference_invalid = bool(entry["continued_reference_unlock_problems"])
                # Both channels depend on the same cascaded external clock.
                # A known unlock in either bracket invalidates both roles.
                entry["valid_for_analysis_by_role"] = {role: not problems and not reference_invalid
                    for role, problems in by_role.items()}
                record["samples"].append(entry)
                record.pop("candidate_in_progress", None)
                self.event("lockin_formal_pair", entry)
                for role, harmonic in selected.items():
                    reading = pair[role]
                    for metric in ("x_v", "y_v", "amplitude_v", "phase_deg"):
                        value = getattr(reading, metric)
                        if value is not None:
                            measured[f"lockin_{role}_h{harmonic}_{metric}"] = value
            self.sample_count += 1
            problems = list(dict.fromkeys(p for entry in record["samples"] for p in entry["problems"]))
            reading = {"module": "lockin", "captured_at_utc": _now(), "actual": actual,
                "measurements": measured, "clean": not problems, "valid_for_analysis": not problems,
                "overload_continuation": any(entry["continued_overload_problems"] for entry in record["samples"]),
                "reference_unlock_continuation": any(entry["continued_reference_unlock_problems"]
                    for entry in record["samples"]), "overload_summary": overload_summary(record),
                "problems": problems, "status": record}
            if self._reference_operation_context is not None:
                self._reference_operation_context["formal_candidates"].append(reading)
            return reading
        except ReferenceTransientError as error:
            record.update(rejected_reference_candidate=True, valid_for_analysis=False,
                          transient_kind=error.kind, transient_evidence=_plain(error.evidence))
            for entry in record["samples"]:
                entry["valid_for_analysis_by_role"] = {"lockin_xx": False, "lockin_xy": False}
            self.event("lockin_rejected_reference_candidate", record)
            self._fail(error)
            raise
        except BaseException as error:
            self._fail(error)
            raise
        finally:
            try:
                self.event("lockin_point_samples", record)
            except BaseException:
                self.failed = True
                raise
            finally:
                self._emit_audit("sample_point", suppress=self.failed)

    def cleanup(self):
        if self.cleanup_result is not None:
            return self.cleanup_result
        if not self.writes_started:
            return {"attempted": False, "verified": True, "source_protection_verified": False,
                    "errors": []}
        result = {"attempted": True, "verified": False, "errors": [],
                  "source_protection_verified": False,
                  "reference_policy": "preserve_external", "source_wiring": asdict(self.config.source)}
        try:
            source = self._source(self.config.cleanup_source_voltage_v, protect=True)
            result["source"] = source
            if source["source_voltage_v"] > self.config.cleanup_source_voltage_v + 1e-12:
                raise ValueError("Cleanup source amplitude is not confirmed below the explicit target.")
            if source["dc_offset_v"] != 0 or source["dc_mode"] != self.config.source.dc_mode:
                raise ValueError("Cleanup AC protection does not establish safe source DC state.")
            result["source_protection_verified"] = True
        except BaseException as exc:
            result["errors"].append(f"source: {type(exc).__name__}: {exc}")
        if self.config.reference_output is not None:
            result["reference_output_preserved"] = False
            try:
                result["reference_output"] = self._verify_reference_output()
                result["reference_output_preserved"] = True
            except BaseException as exc:
                result["errors"].append(f"reference output: {type(exc).__name__}: {exc}")
        for device in self.pair or ():
            try:
                state = device.read_settings()
                result["lockin_" + device.role.value] = asdict(state)
                if state.reference_source != "external":
                    raise ValueError("External-reference ownership was not preserved.")
                if self.config.reference_topology == "pem_xy_xx_sine":
                    native = state.native_settings
                    if device.role is LockinRole.XX:
                        if native.reference_slope != 0:
                            raise ValueError("XX SINE reference trigger was not preserved.")
                    elif (native.external_reference_edge != self.config.lockin_xy.external_reference_edge
                            or native.reference_input_impedance_ohm != self.config.lockin_xy.reference_input_impedance_ohm
                            or native.sync_output_mode != self.preflight["xy"]["native_settings"]["sync_output_mode"]):
                        raise ValueError("XY external reference or preserved BlazeX state changed.")
            except BaseException as exc:
                result["errors"].append(f"{device.role.value}: {type(exc).__name__}: {exc}")
        result["errors"].extend(self._emit_audit("cleanup", suppress=True))
        result["verified"] = not result["errors"]
        result["manual_verification_required"] = not result["verified"]
        self.cleanup_result = result
        self.configured = False
        return result

    def close(self):
        errors = []
        for resource in reversed(self.resources):
            try:
                resource.close()
            except BaseException as exc:
                errors.append(exc)
        self.resources = []
        if self.manager is not None:
            try:
                self.manager.close()
            except BaseException as exc:
                errors.append(exc)
        self.manager = None
        self.closed = True
        if errors:
            raise RuntimeError("Lock-in resource close failed: " + "; ".join(str(e) for e in errors))
