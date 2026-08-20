from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Sequence


@dataclass(frozen=True, slots=True)
class TimedValue:
    elapsed_s: float
    value: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.elapsed_s) or not math.isfinite(self.value):
            raise ValueError("Stability samples must be finite.")


@dataclass(frozen=True, slots=True)
class StabilityCriteria:
    tolerance: float
    stable_range: float
    dwell_s: float
    minimum_samples: int = 3

    def __post_init__(self) -> None:
        if not math.isfinite(self.tolerance) or self.tolerance <= 0:
            raise ValueError("tolerance must be finite and positive.")
        if not math.isfinite(self.stable_range) or self.stable_range < 0:
            raise ValueError("stable_range must be finite and non-negative.")
        if not math.isfinite(self.dwell_s) or self.dwell_s <= 0:
            raise ValueError("dwell_s must be finite and positive.")
        if self.minimum_samples < 2:
            raise ValueError("minimum_samples must be at least 2.")


def evaluate_stability(
    samples: Sequence[TimedValue],
    setpoint: float,
    criteria: StabilityCriteria,
) -> bool:
    """Evaluate a continuous trailing dwell window without touching hardware."""

    if not math.isfinite(setpoint):
        raise ValueError("setpoint must be finite.")
    if not samples:
        return False

    previous_time = samples[0].elapsed_s
    for sample in samples[1:]:
        if sample.elapsed_s < previous_time:
            raise ValueError("Stability samples must be time ordered.")
        previous_time = sample.elapsed_s

    latest_time = samples[-1].elapsed_s
    cutoff = latest_time - criteria.dwell_s
    window = [sample for sample in samples if sample.elapsed_s >= cutoff]

    if len(window) < criteria.minimum_samples:
        return False
    if window[-1].elapsed_s - window[0].elapsed_s < criteria.dwell_s:
        return False

    values = [sample.value for sample in window]
    if any(abs(value - setpoint) > criteria.tolerance for value in values):
        return False
    return max(values) - min(values) <= criteria.stable_range

