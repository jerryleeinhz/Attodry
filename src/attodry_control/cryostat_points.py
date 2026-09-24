"""Single-owner attoDRY point session for combined scans (no I/O on import).

All access is synchronous. Formal-window samples bracket electrical reads; they
are not a claim of continuous monitoring while a VISA call is in progress.
"""
from __future__ import annotations

from dataclasses import asdict, replace
import math
import time

from .attodry import AttoDryDriver, AttoDryError
from .combination_store import utc_now
from .config import TemperatureStabilityMode
from .magnetic_field import execute_field_target
from .models import VectorField
from .safety import float32_field, validate_vector_field
from .temperature_scan import _validate_point_state
from .temperature_excitation_scan import _temperature_window_statistics


class _AuditedDriver(AttoDryDriver):
    def read_state(self):
        state = super().read_state()
        # Persist even an unsafe complete readback before rejecting it.
        self.owner.event("cryostat_state", {
            "captured_at_utc": utc_now(), "state": asdict(state)})
        self.owner.validate_state(state)
        return state


class CryostatPointSession:
    def __init__(self, config, temperature, magnetic, event, *, dll=None,
                 monotonic=None, sleep=None):
        self.config, self.temperature, self.magnetic = config, temperature, magnetic
        self.event, self.dll = event, dll
        self.monotonic = monotonic or time.monotonic
        self.sleep = sleep or time.sleep
        self.driver = None
        self.connection_owned = False
        self.temperature_target = self.field_target = None
        self.temperature_changed = False
        self.minimum_response = 0.0
        self.response_reference = None
        self.touched = set()
        self.cleaning = False
        self.window = None
        self.baseline_temperature = None
        self.temperature_ceiling = None

    @property
    def clock(self):
        return {"monotonic": self.monotonic, "sleeper": self.sleep}

    def validate_state(self, state):
        # Effective axis ceilings are both <=3 T, including pure Z. The same
        # limits are used inside the driver's float32/corner/ack checks.
        validate_vector_field(state.field, self.config.magnet.limits)
        validate_vector_field(state.field_setpoint, self.config.magnet.limits)
        if state.error_code:
            raise AttoDryError(f"attoDRY error code {state.error_code}")
        if not self.cleaning:
            if not (self.config.cryostat.temperature_min_k <= state.sample_temperature_k
                    <= self.config.cryostat.temperature_max_k):
                raise AttoDryError("Sample temperature outside configured cryostat limits")
            if self.temperature_target is not None:
                request = self.temperature.temperature_run
                if self.temperature_changed and self.temperature_ceiling is not None:
                    request = replace(request, max_overshoot_k=self.temperature_ceiling - self.temperature_target)
                _validate_point_state(state, self.temperature_target,
                                      request)

    def open(self):
        self.driver = _AuditedDriver.from_config(
            self.config, dll=self.dll, connection_authorized=True, writes_authorized=True)
        self.driver.owner = self
        self.driver.connect(**self.clock)
        self.connection_owned = True
        return asdict(self.driver.read_state())

    def apply(self, module, point):
        if module == "magnetic":
            target = VectorField(point.values["field_x_t"], point.values["field_z_t"])
            self.touched.add(module)  # Includes interrupted/uncertain command attempts.
            result = execute_field_target(
                self.driver, target, self.magnetic.run.max_step_t,
                transition_policy=self.magnetic.run.transition_policy,
                on_event=lambda payload: self.event("magnetic_point_event", payload), **self.clock)
            self.field_target = float32_field(target)
            return {"result": asdict(result), "segment": point.segment,
                    "direction": point.direction}
        target = point.values["temperature_k"]
        before = self.driver.read_state()
        request = self.temperature.temperature_run
        if abs(target - before.sample_temperature_k) > request.max_delta_k:
            raise ValueError("Temperature movement exceeds temperature_run.max_delta_k")
        changed = (self.temperature_target is not None and
                   not math.isclose(target, self.temperature_target, rel_tol=0, abs_tol=1e-4))
        self.response_reference = before.sample_temperature_k
        self.temperature_ceiling = max(target, before.sample_temperature_k) + request.max_overshoot_k
        if self.temperature_ceiling > self.config.cryostat.temperature_max_k:
            raise ValueError("Temperature transition ceiling exceeds configured maximum")
        stability = self.config.temperature_stability
        self.minimum_response = (stability.min_response_k if changed and
            stability.acceptance_mode is TemperatureStabilityMode.STABLE_READBACK else 0.0)
        self.temperature_target = None  # Old target must not reject the new acknowledgement.
        self.touched.add(module)
        self.event("temperature_command_attempt", {"target_k": target})
        state = self.driver.set_temperature_and_enable(target, **self.clock)
        self.temperature_target = target
        self.temperature_changed = True
        self.validate_state(state)
        self.event("temperature_command_confirmed", {"target_k": target, "state": asdict(state)})
        return {"state": asdict(state)}

    def qualify(self):
        if self.field_target is not None:
            self.driver.wait_for_field(self.field_target, **self.clock)
        if self.temperature_target is not None:
            # Always requalify AFTER every axis has been applied, including B
            # changes and inner-axis resets. A duplicate T point needs dwell,
            # but must not demand a new thermal response to the same target.
            samples = []
            self.event("temperature_qualification_started", {
                "target_k": self.temperature_target, "after_all_axis_settings": True})
            self.driver.wait_for_temperature(
                self.temperature_target,
                max_overshoot_k=(self.temperature_ceiling - self.temperature_target
                                if self.temperature_changed else self.temperature.temperature_run.max_overshoot_k),
                maximum_accepted_k=self.temperature_target + self.temperature.temperature_run.max_overshoot_k,
                minimum_response_k=self.minimum_response if self.temperature_changed else 0.0,
                response_reference_k=self.response_reference,
                on_sample=lambda state, elapsed: samples.append((elapsed, state.sample_temperature_k)),
                **self.clock)
            cutoff = samples[-1][0] - self.config.temperature_stability.criteria.dwell_s
            values = [value for elapsed, value in samples if elapsed >= cutoff]
            self.baseline_temperature = sum(values) / len(values)
            self.temperature_changed = False
            self.event("temperature_qualification_completed", {
                "target_k": self.temperature_target, "stable_mean_k": self.baseline_temperature,
                "samples": samples})
        self.capture("qualification")

    def capture(self, stage, harmonic=None, sample_index=None):
        state = self.driver.read_state()
        record = {"stage": stage, "harmonic": harmonic, "sample_index": sample_index,
                  "captured_at_utc": utc_now(), "captured_monotonic_s": self.monotonic(),
                  "state": asdict(state)}
        self.event("environment_sample", record)
        if self.window is not None:
            self.window.append(record)
        if self.field_target is not None:
            tolerance = self.config.magnet.stability.criteria.tolerance
            if not state.field_control_enabled or not self.driver._field_matches(
                    state.field_setpoint, self.field_target):
                raise AttoDryError("Magnetic control/setpoint changed during measurement")
            if (abs(state.field.bx_t - self.field_target.bx_t) > tolerance or
                    abs(state.field.bz_t - self.field_target.bz_t) > tolerance):
                raise AttoDryError("Magnetic readback outside target tolerance")
        if self.temperature_target is not None and self.baseline_temperature is not None:
            criteria = self.config.temperature_stability.criteria
            if abs(state.sample_temperature_k - self.baseline_temperature) > criteria.stable_range:
                raise AttoDryError("Temperature drifted from qualified baseline")
            if (self.config.temperature_stability.acceptance_mode is TemperatureStabilityMode.TARGET
                    and abs(state.sample_temperature_k - self.temperature_target) > criteria.tolerance):
                raise AttoDryError("Temperature readback outside target tolerance")
        return record

    def read(self, module):
        record = self.capture("formal_" + module)
        state = record["state"]
        actual = ({"temperature_k": state["sample_temperature_k"]} if module == "temperature" else
                  {"field_x_t": state["field"]["bx_t"], "field_z_t": state["field"]["bz_t"]})
        return {"module": module, "captured_at_utc": record["captured_at_utc"], "actual": actual,
                "measurements": {}, "clean": True, "problems": [], "status": record}

    def begin_sample(self):
        self.window = []
        self.capture("before_sample")

    def end_sample(self, reads):
        try:
            after = self.capture("after_sample")
            values = [r["state"]["sample_temperature_k"] for r in self.window]
            statistics = _temperature_window_statistics(self.window)
            summary = {"sampling": "synchronous_brackets_not_continuous", "samples": self.window,
                       "temperature_statistics": statistics,
                       "temperature_mean_k": statistics["mean_k"],
                       "temperature_min_k": min(values), "temperature_max_k": max(values)}
            field_means = {}
            field_range_ok = True
            for axis in ("x", "z"):
                fields = [r["state"]["field"]["b" + axis + "_t"] for r in self.window]
                field_means["field_" + axis + "_t"] = sum(fields) / len(fields)
                summary["field_" + axis + "_range_t"] = max(fields) - min(fields)
                field_range_ok &= max(fields) - min(fields) <= self.config.magnet.stability.criteria.stable_range
            summary["field_mean"] = field_means
            self.event("environment_formal_window", summary)
            if (self.temperature is not None and
                    max(values) - min(values) > self.config.temperature_stability.criteria.stable_range):
                raise AttoDryError("Temperature range exceeded during formal sample")
            if self.magnetic is not None and not field_range_ok:
                raise AttoDryError("Magnetic range exceeded during formal sample")
            for reading in reads:
                if reading["module"] == "temperature":
                    reading["actual"]["temperature_k"] = summary["temperature_mean_k"]
                if reading["module"] == "magnetic":
                    reading["actual"] = field_means
                if reading["module"] in {"temperature", "magnetic"}:
                    reading["status"] = summary
                    reading["captured_at_utc"] = after["captured_at_utc"]
        finally:
            self.window = None

    def cleanup(self, module, failed):
        self.cleaning = True
        if self.driver is None or module not in self.touched:
            return {"attempted": False, "verified": True, "reason": "no writes attempted"}
        if module == "temperature":
            if failed:
                self.event("temperature_disable_attempt", {})
                self.driver.ensure_temperature_control(False, **self.clock)
            state = self.driver.read_state()
            if failed:
                if state.temperature_control_enabled:
                    raise AttoDryError("Temperature disable not confirmed")
            else:
                _validate_point_state(state, self.temperature_target, self.temperature.temperature_run)
            policy = "disable" if failed else "hold"
        else:
            policy = "zero" if failed else self.magnetic.cleanup.normal_end_field_policy.value
            if policy == "zero":
                # Never toggle an unexpectedly disabled/unknown field controller
                # merely to claim successful cleanup. Driver fails closed.
                state = self.driver.request_zero_field(
                    on_command=lambda payload: self.event("magnetic_cleanup_command", payload), **self.clock)
            else:
                state = self.driver.read_state()
                if not state.field_control_enabled or self.field_target is None:
                    raise AttoDryError("Field hold not confirmed")
                if not self.driver._field_matches(state.field_setpoint, self.field_target):
                    raise AttoDryError("Field hold setpoint changed")
                tolerance = self.config.magnet.stability.criteria.tolerance
                if (abs(state.field.bx_t - self.field_target.bx_t) > tolerance or
                        abs(state.field.bz_t - self.field_target.bz_t) > tolerance):
                    raise AttoDryError("Field hold readback outside tolerance")
        return {"attempted": True, "verified": True, "policy": policy, "state": asdict(state)}

    def close(self):
        # connect() unwinds its own partial initialization on failure.
        if self.driver is not None and self.connection_owned:
            try:
                self.driver.close()
            finally:
                self.connection_owned = False
