"""Power-window evidence and bounded actuator rules. No instrument dependencies."""
from __future__ import annotations

from collections import deque
import math
import statistics

from .nkt_config import NktError, number, tenths


class PowerWindow:
    def __init__(self, config, *, epoch_s, max_power_w):
        config.validate()
        self.config = config
        self.epoch_s = epoch_s
        self.max_power_w = max_power_w
        self.samples = deque()
        self.last_sequence = None
        self.last_finished_s = None

    def add(self, sample):
        if sample.get("valid") is not True or sample.get("unit") != "W":
            raise NktError("Window requires valid W samples")
        number(sample["power_w"], "window power W", 0, self.max_power_w)
        start, end = sample["started_monotonic_s"], sample["finished_monotonic_s"]
        number(start, "sample start", self.epoch_s, 1e15)
        number(end, "sample end", start, 1e15)
        sequence = sample["sequence"]
        if type(sequence) is not int or (self.last_sequence is not None and sequence <= self.last_sequence):
            raise NktError("Duplicate/out-of-order power acquisition")
        if self.last_finished_s is not None and start < self.last_finished_s:
            raise NktError("Stale/overlapping power acquisition")
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
        stable = covered and enough and p2p <= self.config.max_peak_to_peak_w
        return {"mean_power_w": statistics.mean(values), "std_power_w": statistics.pstdev(values),
                "peak_to_peak_w": p2p, "count": len(values), "started_monotonic_s": start,
                "finished_monotonic_s": end, "power_window_stable": stable,
                "reason": "stable" if stable else "insufficient duration/count or excessive variation",
                "sequences": [s["sequence"] for s in self.samples]}


def target_in_tolerance(power_w, config):
    return abs(power_w - config.target_power_w) <= config.target_tolerance_w + 1e-15


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
        raise NktError("Target unreachable at configured source current boundary/resolution")
    return value
