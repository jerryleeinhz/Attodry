"""Optional PM100D qualification and frozen formal-window power guards.

Uses the PEM module's PM100D, PowerWindow and bounded NKT policies. Reads are
sequential brackets, never a claim of continuous or hardware-synchronous power.
"""
from copy import deepcopy
import statistics
import time
from dataclasses import replace

from .nkt_config import NktError, number
from .nkt_control import utc_now
from .power_feedback import (PowerWindow, next_nd_pct, next_source_current_pct,
                             next_source_power_pct, next_cld_current_a, target_in_tolerance, tuning_in_tolerance)


class PowerDeviation(NktError):
    """Recoverable optical deviation, after all hardware/status guards passed."""


class PowerRecoveryExhausted(NktError):
    """The explicit point attempt/time budget was exhausted; stop the run."""


class PhotoresponsePower:
    def __init__(self, config, laser, meter, *, clock=time.perf_counter, sleep=time.sleep, on_event=None):
        config.validate(laser.config)
        if meter.config != config.meter or bool(meter.is_hardware) != bool(laser.backend.is_hardware):
            raise NktError("Power controller/meter configuration or hardware mode mismatch")
        if meter.is_hardware and (not meter.authorize_settings or not meter.authorize_measurement):
            raise NktError("Hardware power qualification requires PM setting and measurement authorization")
        self.config, self.laser, self.meter = config, laser, meter
        self.clock, self.sleep, self.on_event = clock, sleep, on_event
        self.row = self.feedback = self.wavelength = None
        self.formal_values = []
        self.point_deadline = None
        self._history_key, self._response_history = None, []

    def _event(self, phase, **values):
        if self.on_event:
            self.on_event({"phase": phase, "captured_at_utc": utc_now(), **deepcopy(values)})

    def prepare(self, condition, point):
        dark = self.config.power_points is not None and condition["power_index"] == 0
        self.feedback = self.config.for_target(1 if dark else condition["power_index"])
        self.row = {"samples": [], "adjustments": [], "qualification_windows": [],
                    "target_power_w": None if dark else self.feedback.target_power_w,
                    "optical_state": "dark" if dark else "light",
                    "target_tolerance_w": self.feedback.target_tolerance_w,
                    "tuning_tolerance_w": self.feedback.tuning_tolerance_w(),
                    "measurement_plane": self.config.meter.measurement_plane,
                    "qualified": False, "formal_mean_power_w": None}
        condition["power"] = self.row
        optical = self.laser.verify_point(emitting=False, phase="power_prepare_off")
        filt = optical["filter"]
        self.wavelength = ((filt["lower_edge_nm"] + filt["upper_edge_nm"]) / 2
                           if filt and filt.get("kind") == "varia" else point.wavelength_nm)
        self.row["wavelength_nm"] = self.wavelength
        history_key = (self.wavelength, filt.get("upper_edge_nm") if filt else None,
                       filt.get("lower_edge_nm") if filt else None)
        if history_key != self._history_key:
            self._response_history = []
        self._history_key = history_key
        self.row["wavelength_kind"] = "nominal_diode" if hasattr(point, "current_a") else "filter_readback"
        self.row["pm_preparation"] = self.meter.prepare(self.wavelength)
        self.formal_values = []

    def _sample(self, phase, illuminated, guard=None):
        # Append before I/O: incomplete/invalid READ replies remain attached to
        # the exact attempt, even when no valid PM last_sample was produced.
        entry = {"phase": phase, "captured_at_utc": utc_now(), "sample": None, "valid": False,
                 "started_monotonic_s": self.clock()}
        self.row["samples"].append(entry)
        start = len(self.meter.transcript)
        try:
            self.check_point_deadline()
            entry["optical_before"] = self.laser.verify_point(emitting=illuminated, phase=phase + "_power_before")
            if guard:
                entry["modules_before"] = guard()
            if illuminated:
                sample = self.meter.read_sample(self.wavelength)
            else:
                # Dark noise can be signed; do not turn a light-only feedback
                # reading into a dark-power acceptance gate or search for light.
                sample = {"power_w": None, "measurement_performed": False,
                          "state": self.meter.verify_ready(self.wavelength),
                          "measurement_plane": self.config.meter.measurement_plane,
                          "freshness": "settings/status queries only; dark power not measured"}
            entry["sample"] = deepcopy(sample)
            if illuminated:
                number(sample["power_w"], "feedback measured power W", 0, self.feedback.max_power_w)
                if self.feedback.minimum_signal_power_w is not None and sample["power_w"] < self.feedback.minimum_signal_power_w:
                    raise NktError("Signal below minimum_signal_power_w; no search for light")
                if self.feedback.reduce_above_power_w is not None and sample["power_w"] > self.feedback.reduce_above_power_w:
                    raise NktError("Power exceeds reduction threshold")
            entry["optical_after"] = self.laser.verify_point(emitting=illuminated, phase=phase + "_power_after")
            if guard:
                entry["modules_after"] = guard()
            entry["valid"] = True
            self.check_point_deadline()
            return sample
        except (Exception, KeyboardInterrupt) as exc:
            entry["valid"] = False
            entry["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            entry["finished_monotonic_s"] = self.clock()
            entry["duration_s"] = entry["finished_monotonic_s"] - entry["started_monotonic_s"]
            entry["pm_transcript"] = deepcopy(self.meter.transcript[start:])
            self._event("power_sample", evidence=entry, target_power_w=self.row["target_power_w"],
                        measurement_plane=self.config.meter.measurement_plane)

    def check_point_deadline(self):
        if self.point_deadline is not None and self.clock() >= self.point_deadline:
            raise PowerRecoveryExhausted("Power point total time budget exhausted")

    def _deadline(self, deadline):
        self.check_point_deadline()
        if self.clock() > deadline:
            raise NktError("Power qualification timed out")

    def qualify(self, guard, *, on_emission_changed=None):
        """May tune only before the formal light window; resets proof per step."""
        f = self.feedback
        self.row["qualified"] = False
        deadline = self.clock() + f.timeout_s
        window = PowerWindow(f.window, epoch_s=self.clock(), max_power_w=f.max_power_w, target_power_w=f.target_power_w)
        stable_since = None
        previous_direction = None
        step = {"varia_nd": f.nd_step_pct, "source_current": f.source_current_step_pct,
                "source_power": f.source_power_step_pct, "cld_current": f.current_step_a}[f.actuator]
        while True:
            self._deadline(deadline)
            sample = self._sample("qualification", True, guard)
            self._deadline(deadline)  # A slow READ cannot overrun the total budget.
            result = window.add(sample)
            self.row["qualification_windows"].append(deepcopy(result))
            if f.reduce_above_power_w is not None and sample["power_w"] > f.reduce_above_power_w:
                raise NktError("Power exceeds reduction threshold during qualification")
            if f.minimum_signal_power_w is not None and sample["power_w"] < f.minimum_signal_power_w:
                raise NktError("Signal below minimum_signal_power_w; no search for light")
            in_target = all(tuning_in_tolerance(s["power_w"], f) for s in window.samples)
            self._event("power_stability", result=result, target_in_tuning_range=in_target,
                        target_power_w=f.target_power_w, measured_power_w=sample["power_w"],
                        reason=result["reason"] if not result["power_window_stable"] else
                               "stable" if in_target else "target deviation",
                        control_phase="waiting_stability")
            if result["power_window_stable"] and in_target:
                if stable_since is None:
                    stable_since = self.clock()
                if self.clock() - stable_since + 1e-9 >= f.hold_s:
                    self.row.update(qualified=True, qualification_result=deepcopy(result))
                    self.row.update(qualified_monotonic_s=self.clock(), qualified_at_utc=utc_now(),
                                    first_formal_power_gap_s=None)
                    self.formal_values = []
                    self._reference_values = [s["power_w"] for s in window.samples]
                    self._formal_window = window
                    if f.actuator == "source_current" and f.tuning_tolerance_fraction is not None:
                        optical = self.laser.verify_point(emitting=True, phase="qualified_response_history")
                        self._response_history.append((optical["source"]["level_pct"], result["mean_power_w"]))
                        del self._response_history[:-16]
                    return result
            elif result["power_window_stable"] and not tuning_in_tolerance(result["mean_power_w"], f):
                direction = 1 if result["mean_power_w"] < f.target_power_w else -1
                reversed_direction = previous_direction is not None and direction != previous_direction
                if reversed_direction:
                    minimum = .1
                    if f.actuator == "cld_current":
                        minimum = min(f.current_step_a, max(self.laser.config.current_tolerance_a * 2, 1e-12))
                    step = max(minimum, step / 2)
                    if f.actuator != "cld_current":
                        step = round(step, 1)
                previous_direction = direction
                optical = self.laser.verify_point(emitting=True, phase="feedback_before_adjustment")
                # Legacy policies retain the original bounded step. Explicit
                # narrow tuning enables center-seeking and early fine steps.
                tuning = replace(f, target_tolerance_w=f.tuning_tolerance_w())
                effective_step = step
                relative_error = abs(result["mean_power_w"] - f.target_power_w) / f.target_power_w
                if f.tuning_tolerance_fraction is not None and relative_error < .25:
                    effective_step = min(step, max(.1 if f.actuator != "cld_current" else
                        min(step, self.laser.config.current_tolerance_a * 2), step * relative_error / .25))
                    if f.actuator != "cld_current":
                        effective_step = max(.1, round(effective_step, 1))
                if f.actuator == "cld_current":
                    current = optical["source"]["getLdCurrSetpoint"]
                    value = next_cld_current_a(current, result["mean_power_w"], tuning, step_a=effective_step)
                elif f.actuator == "source_current":
                    current = optical["source"]["level_pct"]
                    history = self._response_history
                    if f.tuning_tolerance_fraction is not None:
                        history.append((current, result["mean_power_w"]))
                        for old_current, old_power in reversed(history[:-1]):
                            if abs(current - old_current) >= .1 and (current-old_current)*(result["mean_power_w"]-old_power) > 0:
                                slope = (result["mean_power_w"] - old_power) / (current - old_current)
                                estimate = abs((f.target_power_w - result["mean_power_w"]) / slope)
                                effective_step = max(.1, round(min(effective_step, estimate), 1))
                                break
                        del history[:-16]
                    value = next_source_current_pct(current, result["mean_power_w"], tuning, step_pct=effective_step)
                elif f.actuator == "source_power":
                    current = optical["source"]["level_pct"]
                    value = next_source_power_pct(current, result["mean_power_w"], tuning, step_pct=effective_step)
                else:
                    current = optical["filter"]["nd_pct"]
                    value = next_nd_pct(current, result["mean_power_w"], tuning, step_pct=effective_step)
                adjustment = {"actuator": f.actuator, "before": current, "requested": value,
                              "control_phase": "fine_adjustment" if effective_step < step or reversed_direction else "coarse_adjustment",
                              "direction_reversed": reversed_direction,
                              "effective_step": effective_step,
                              "power_window": deepcopy(result), "finished": False}
                self.row["adjustments"].append(adjustment)
                try:
                    adjustment["modules_before"] = guard()
                    self.laser.set_emission(False)
                    adjustment["readback"] = self.laser.adjust_power(value, self.config)
                    adjustment["modules_after"] = guard()
                    self._deadline(deadline)
                    self.laser.set_emission(True)
                    if on_emission_changed:
                        on_emission_changed()
                    self._deadline(deadline)
                    adjustment["finished"] = True
                except (Exception, KeyboardInterrupt) as exc:
                    adjustment["error"] = f"{type(exc).__name__}: {exc}"
                    raise
                finally:
                    self._event("power_adjustment", evidence=adjustment)
                window = PowerWindow(f.window, epoch_s=self.clock(), max_power_w=f.max_power_w, target_power_w=f.target_power_w)
                stable_since = None
            else:
                stable_since = None
            self.sleep(f.window.sample_interval_s)

    def preacquisition_check(self, guard):
        """Fresh post-electrical evidence; no excitation or tuning inside READ."""
        sample = self.formal_sample("preacquisition", illuminated=True, guard=guard)
        self.row.setdefault("preacquisition_checks", []).append({"power_w": sample["power_w"],
            "checked_monotonic_s": self.clock(), "after_qualification_s": self.clock() - self.row["qualified_monotonic_s"]})
        if not tuning_in_tolerance(sample["power_w"], self.feedback):
            self.row["qualified"] = False
            self.row["samples"][-1].update(window_valid=False, rejection="preacquisition target deviation")
            self._event("power_window_rejected", evidence=self.row["samples"][-1], assessment={
                "reason": "preacquisition target deviation", "target_power_w": self.feedback.target_power_w,
                "measured_power_w": sample["power_w"], "tuning_tolerance_w": self.feedback.tuning_tolerance_w()})
            raise PowerDeviation("Preacquisition power left the tuning range")
        self.formal_values = []
        self.row["formal_mean_power_w"] = None
        return sample

    def formal_sample(self, phase, *, illuminated, guard):
        sample = self._sample(phase, illuminated, guard)
        entry = self.row["samples"][-1]
        entry["target_in_tolerance"] = target_in_tolerance(sample["power_w"], self.feedback) if illuminated else None
        entry["window_valid"] = True
        if illuminated:
            if not self.row["qualified"]:
                raise NktError("Formal light sample requires qualified power")
            if phase != "preacquisition" and self.row.get("first_formal_power_gap_s") is None:
                self.row["first_formal_power_gap_s"] = sample["started_monotonic_s"] - self.row["qualified_monotonic_s"]
            self.formal_values.append(sample["power_w"])
            self.row["formal_mean_power_w"] = statistics.mean(self.formal_values)
            evidence = self._reference_values + self.formal_values
            spread = max(evidence) - min(evidence)
            self.row["formal_peak_to_peak_w"] = spread
            limit = self.feedback.window.variation_limit_w(self.feedback.target_power_w)
            trend = self._formal_window.add(sample)
            drift_failed = ("power drift trend" in trend["reason"] and
                            trend["count"] >= self.feedback.window.min_samples and
                            trend["finished_monotonic_s"] - trend["started_monotonic_s"] >= self.feedback.window.duration_s - 1e-9)
            entry["stability"] = trend
            if (not target_in_tolerance(sample["power_w"], self.feedback)
                    or spread > limit or drift_failed):
                self.row["qualified"] = False
                entry["window_valid"] = False
                entry["rejection"] = ("target deviation" if not entry["target_in_tolerance"] else
                                       "drift trend" if drift_failed else "power variation")
                assessment = {"target_power_w": self.feedback.target_power_w,
                    "measured_power_w": sample["power_w"], "target_tolerance_w": self.feedback.target_tolerance_w,
                    "peak_to_peak_w": spread, "max_peak_to_peak_w": limit,
                    "target_in_tolerance": entry["target_in_tolerance"], "reason": entry["rejection"],
                    "stability": trend}
                self._event("power_window_rejected", evidence=entry, assessment=assessment)
                raise PowerDeviation("Formal power drift: " +
                    f"target={self.feedback.target_power_w:g} W, measured={sample['power_w']:g} W, " +
                    f"peak-to-peak={spread:g} W (limit {limit:g} W); reject whole acquisition window")
        return sample

    def close(self):
        try:
            self.meter.close()
        finally:
            self.audit = {"transcript": deepcopy(self.meter.transcript),
                          "last_confirmed_state": deepcopy(self.meter.last_confirmed_state),
                          "state_current": False}


def open_photoresponse_power(config, laser, *, clock=time.perf_counter, sleep=time.sleep, on_event=None,
                             authorize_connection=False, authorize_writes=False):
    """Explicitly gated resource factory; caller owns unconditional close."""
    from .pm100d import Pm100d, SimulatedPmResource
    config.validate(laser.config)
    if bool(laser.backend.is_hardware) != (config.meter.backend != "simulation"):
        raise NktError("Power meter and injected laser backend mode mismatch")
    if config.meter.backend == "visa":
        if not authorize_connection or not authorize_writes:
            raise NktError("PM power loop requires explicit connection, settings and acquisition authorization")
        meter = Pm100d.open(config.meter, authorize_connection=authorize_connection,
                           authorize_settings=authorize_writes, authorize_measurement=authorize_writes, clock=clock)
    else:
        def power():
            # Explicit synthetic, monotone model only; never used for real conversion.
            backend, f = laser.backend, config.feedback
            if f.actuator == "cld_current":
                return (0.002 * backend.state["getLdCurrSetpoint"] / laser.config.max_current_a
                        if backend.state["getLdOutputState"] else 0.)
            if backend.source["emission_state"] != 3:
                return 0.
            base = 0.002 * backend.source["level_pct"] / laser.config.max_level_pct
            if f.actuator == "varia_nd":
                sign = 1 if f.increasing_nd_increases_power else -1
                return base * 10 ** (sign * backend.filter["nd_pct"] / 100)
            return base
        meter = Pm100d(config.meter, SimulatedPmResource(power), clock=clock)
    try:
        return PhotoresponsePower(config, laser, meter, clock=clock, sleep=sleep, on_event=on_event)
    except BaseException as exc:
        try:
            meter.close()
        except BaseException as cleanup_exc:
            exc.add_note(f"PM100D factory cleanup: {cleanup_exc}")
        raise
