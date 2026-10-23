"""Power-window evidence and bounded actuator rules. No instrument dependencies."""
from __future__ import annotations

from collections import deque
import math
import statistics

from .nkt_config import NktError, number, tenths
from .optical_recovery import OpticalTargetUnreachable


def _checked_sample(sample, epoch_s, max_power_w, last_sequence, last_finished_s):
    if sample.get("valid") is not True or sample.get("unit") != "W":
        raise NktError("Window requires valid W samples")
    number(sample["power_w"], "window power W", 0, max_power_w)
    start, end = sample["started_monotonic_s"], sample["finished_monotonic_s"]
    number(start, "sample start", epoch_s, 1e15)
    number(end, "sample end", start, 1e15)
    sequence = sample["sequence"]
    if type(sequence) is not int or (last_sequence is not None and sequence <= last_sequence):
        raise NktError("Duplicate/out-of-order power acquisition")
    if last_finished_s is not None and start < last_finished_s:
        raise NktError("Stale/overlapping power acquisition")
    return start, end, sequence


class MeanPowerTarget:
    """One fresh instrument-averaged reading; never a temporal stability proof."""
    def __init__(self, *, epoch_s, max_power_w):
        self.epoch_s, self.max_power_w = epoch_s, max_power_w
        self.samples = deque(maxlen=1)
        self.last_sequence = self.last_finished_s = None

    def add(self, sample):
        start, end, sequence = _checked_sample(sample, self.epoch_s, self.max_power_w,
                                             self.last_sequence, self.last_finished_s)
        self.last_sequence, self.last_finished_s = sequence, end
        self.samples.append(sample)
        return {"mean_power_w": sample["power_w"], "count": 1,
                "started_monotonic_s": start, "finished_monotonic_s": end,
                "sequences": [sequence], "power_window_stable": False,
                "qualification": "mean_target_only",
                "reason": "single instrument-averaged reading; stability not assessed"}


def feedback_evidence(config, *, epoch_s):
    if config.qualification == "mean_target_only":
        return MeanPowerTarget(epoch_s=epoch_s, max_power_w=config.max_power_w)
    return PowerWindow(config.window, epoch_s=epoch_s, max_power_w=config.max_power_w,
                       target_power_w=config.target_power_w)


class PowerWindow:
    def __init__(self, config, *, epoch_s, max_power_w, target_power_w=None):
        config.validate()
        self.config = config
        self.epoch_s = epoch_s
        self.max_power_w = max_power_w
        self.target_power_w = target_power_w
        if (config.max_peak_to_peak_fraction is not None or config.max_drift_fraction_per_s is not None):
            number(target_power_w, "relative power-window target", 1e-15, max_power_w)
        self.samples = deque()
        self.last_sequence = None
        self.last_finished_s = None

    def add(self, sample):
        start, end, sequence = _checked_sample(sample, self.epoch_s, self.max_power_w,
                                             self.last_sequence, self.last_finished_s)
        if self.last_finished_s is not None and start - self.last_finished_s > self.config.duration_s:
            # A gap longer than the requested window cannot prove continuous
            # stability. Preserve raw samples in the caller, discard the proof.
            self.samples.clear()
        self.last_sequence, self.last_finished_s = sequence, end
        self.samples.append(sample)
        # Keep the sample just before the cutoff to prove full window coverage.
        while len(self.samples) > 1 and self.samples[1]["finished_monotonic_s"] <= end - self.config.duration_s:
            self.samples.popleft()
        return self.result()

    def result(self):
        values = [s["power_w"] for s in self.samples]
        if not values:
            return {"power_window_stable": False, "reason": "empty", "count": 0}
        start, end = self.samples[0]["finished_monotonic_s"], self.samples[-1]["finished_monotonic_s"]
        covered = end - start >= self.config.duration_s - 1e-9
        p2p = max(values) - min(values)
        enough = len(values) >= self.config.min_samples
        mean = statistics.mean(values)
        times = [s["finished_monotonic_s"] - start for s in self.samples]
        mean_time = statistics.mean(times)
        denominator = sum((t - mean_time)**2 for t in times)
        slope = sum((t - mean_time)*(p - mean) for t, p in zip(times, values)) / denominator if denominator else 0.0
        limit = self.config.variation_limit_w(self.target_power_w)
        drift_limit = (None if self.config.max_drift_fraction_per_s is None else
                       self.target_power_w * self.config.max_drift_fraction_per_s)
        reasons = []
        if not covered or not enough:
            reasons.append("insufficient duration/count")
        if p2p > limit + 1e-15:
            reasons.append("power variation")
        if drift_limit is not None and abs(slope) > drift_limit + 1e-15:
            reasons.append("power drift trend")
        stable = not reasons
        return {"mean_power_w": statistics.mean(values), "std_power_w": statistics.pstdev(values),
                "peak_to_peak_w": p2p, "count": len(values), "started_monotonic_s": start,
                "finished_monotonic_s": end, "power_window_stable": stable,
                "reason": "stable" if stable else "; ".join(reasons),
                "drift_w_per_s": slope, "max_drift_w_per_s": drift_limit,
                "max_peak_to_peak_w": limit,
                "sequences": [s["sequence"] for s in self.samples]}


def target_in_tolerance(power_w, config):
    return abs(power_w - config.target_power_w) <= config.target_tolerance_w + 1e-15


def tuning_in_tolerance(power_w, config):
    """Initial center-seeking tolerance; formal acceptance remains independent."""
    return abs(power_w - config.target_power_w) <= config.tuning_tolerance_w() + 1e-15


def next_nd_pct(current, power_w, config, *, step_pct=None):
    config.validate()
    if config.actuator != "varia_nd":
        raise NktError("ND correction requires the varia_nd actuator")
    number(current, "current ND", config.nd_min_pct, config.nd_max_pct)
    number(power_w, "feedback power W", 0, config.max_power_w)
    if target_in_tolerance(power_w, config):
        return current
    if power_w == 0:
        raise NktError("Zero signal: do not increase output to search for light")
    step = config.nd_step_pct if step_pct is None else number(step_pct, "ND step", 0.1, config.nd_step_pct)
    want_more_power = power_w < config.target_power_w
    sign = 1 if want_more_power == config.increasing_nd_increases_power else -1
    value = round(min(config.nd_max_pct, max(config.nd_min_pct, current + sign * step)), 1)
    if math.isclose(value, current, abs_tol=0.01):
        raise NktError("Target unreachable at configured ND boundary/resolution")
    return value


def next_source_current_pct(current, power_w, config, *, step_pct=None):
    """Take one bounded current step; no linear power/current law is assumed."""
    config.validate()
    if config.actuator != "source_current":
        raise NktError("Source current correction requires the source_current actuator")
    number(current, "current source level", config.source_current_min_pct, config.source_current_max_pct)
    tenths(current)
    number(power_w, "feedback power W", 0, config.max_power_w)
    if power_w == 0:
        raise NktError("Zero signal: do not increase output to search for light")
    if power_w < config.minimum_signal_power_w:
        raise NktError("Signal below minimum_signal_power_w; source current feedback is disabled")
    if target_in_tolerance(power_w, config):
        return current
    step = config.source_current_step_pct if step_pct is None else number(
        step_pct, "Source current step", 0.1, config.source_current_step_pct)
    tenths(step)
    sign = 1 if power_w < config.target_power_w else -1
    value = round(min(config.source_current_max_pct,
                      max(config.source_current_min_pct, current + sign * step)), 1)
    if math.isclose(value, current, abs_tol=0.01):
        raise OpticalTargetUnreachable("Target unreachable at configured source current boundary/resolution")
    return value


def next_source_power_pct(current, power_w, config, *, step_pct=None):
    """Bounded constant-power setpoint estimate; measured watts still qualify it."""
    config.validate()
    if config.actuator != "source_power":
        raise NktError("Source power correction requires the source_power actuator")
    lower, upper, maximum_step = config.source_bounds()
    number(current, "current source power setpoint", lower, upper)
    tenths(current)
    number(power_w, "feedback power W", 0, config.max_power_w)
    if power_w == 0 or power_w < config.minimum_signal_power_w:
        raise NktError("Signal below minimum_signal_power_w; do not search for light")
    if target_in_tolerance(power_w, config):
        return current
    step = maximum_step if step_pct is None else number(step_pct, "Source power step", 0.1, maximum_step)
    tenths(step)
    direction = 1 if power_w < config.target_power_w else -1
    delta = direction * min(step, max(0.1, abs(current * config.target_power_w / power_w - current)))
    value = round(min(upper, max(lower, current + delta)), 1)
    if math.isclose(value, current, abs_tol=0.01):
        raise NktError("Target unreachable at configured source power boundary/resolution")
    return value


def next_cld_current_a(current, power_w, config, *, step_a=None):
    """Bounded current step in amperes; never infer optical watts from amperes."""
    config.validate()
    if config.actuator != "cld_current":
        raise NktError("CLD correction requires the cld_current actuator")
    number(current, "CLD current", config.current_min_a, config.current_max_a)
    number(power_w, "feedback power W", 0, config.max_power_w)
    if power_w < config.minimum_signal_power_w:
        raise NktError("Signal below minimum_signal_power_w; CLD feedback is disabled")
    if target_in_tolerance(power_w, config):
        return current
    step = config.current_step_a if step_a is None else number(step_a, "CLD current step", 1e-12,
                                                              config.current_step_a)
    direction = 1 if power_w < config.target_power_w else -1
    value = min(config.current_max_a, max(config.current_min_a, current + direction * step))
    if math.isclose(value, current, rel_tol=0, abs_tol=1e-13):
        raise NktError("Target unreachable at configured CLD current boundary/resolution")
    return value


def adaptive_feedback_step(current, power_w, config, step, history, *, minimum_step=None):
    """Choose a smaller approved step from local evidence, without actuator I/O.

    Legacy configurations keep their bounded fixed/reversal step. Explicit
    tuning precision enables early shrinking and source-current local slope.
    History belongs to one spectrum/actuator; callers clear it on changes/faults.
    """
    minimum = (0.1 if config.actuator != "cld_current" else 1e-12) if minimum_step is None else minimum_step
    if config.actuator == "cld_current":
        maximum = config.current_step_a
    elif config.actuator == "varia_nd":
        maximum = config.nd_step_pct
    else:
        maximum = config.source_bounds()[2]
    number(step, "adaptive step", minimum, maximum)
    evidence = {"base_step": step, "effective_step": step, "local_slope_w_per_unit": None,
                "relative_error": abs(power_w - config.target_power_w) / config.target_power_w,
                "center_seeking": config.tuning_tolerance_fraction is not None}
    if config.tuning_tolerance_fraction is None:
        return step, evidence
    effective = step
    if evidence["relative_error"] < 0.25:
        effective = min(effective, max(minimum, step * evidence["relative_error"] / 0.25))
    if config.actuator == "source_current":
        number(current, "adaptive source current", config.source_current_min_pct, config.source_current_max_pct)
        for old_current, old_power in reversed(history):
            if (abs(current - old_current) >= 0.1 and
                    (current - old_current) * (power_w - old_power) > 0):
                slope = (power_w - old_power) / (current - old_current)
                estimate = abs((config.target_power_w - power_w) / slope)
                effective = min(effective, max(minimum, estimate))
                evidence["local_slope_w_per_unit"] = slope
                break
    if config.actuator != "cld_current":
        effective = max(minimum, round(effective, 1))
    effective = min(step, effective)
    evidence["effective_step"] = effective
    return effective, evidence
