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
    tolerance: float | None
    stable_range: float
    dwell_s: float
    minimum_samples: int = 3

    def __post_init__(self) -> None:
        if self.tolerance is not None and (
            not math.isfinite(self.tolerance) or self.tolerance <= 0
        ):
            raise ValueError("tolerance must be finite and positive when provided.")
        if not math.isfinite(self.stable_range) or self.stable_range < 0:
            raise ValueError("stable_range must be finite and non-negative.")
        if not math.isfinite(self.dwell_s) or self.dwell_s <= 0:
            raise ValueError("dwell_s must be finite and positive.")
        if self.minimum_samples < 2:
            raise ValueError("minimum_samples must be at least 2.")


def _evaluate_window(
    samples: Sequence[TimedValue],
    criteria: StabilityCriteria,
    *,
    setpoint: float | None,
) -> bool:
    """Evaluate a sampled trailing dwell window without touching hardware."""

    if setpoint is not None and not math.isfinite(setpoint):
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
    preceding = [sample for sample in samples if sample.elapsed_s < cutoff]
    coverage_start = preceding[-1] if preceding else (window[0] if window else None)

    if len(window) < criteria.minimum_samples:
        return False
    if coverage_start is None or latest_time - coverage_start.elapsed_s < criteria.dwell_s:
        return False

    values = [sample.value for sample in window]
    if setpoint is not None:
        if criteria.tolerance is None:
            raise ValueError("target stability requires a temperature tolerance.")
        if any(abs(value - setpoint) > criteria.tolerance for value in values):
            return False
    return max(values) - min(values) <= criteria.stable_range


def evaluate_stability(
    samples: Sequence[TimedValue],
    setpoint: float,
    criteria: StabilityCriteria,
) -> bool:
    """Evaluate target tolerance plus range over a continuous dwell window."""

    return _evaluate_window(samples, criteria, setpoint=setpoint)


def evaluate_readback_stability(
    samples: Sequence[TimedValue], criteria: StabilityCriteria
) -> bool:
    """Evaluate stability of the actual readback, independent of setpoint error."""

    return _evaluate_window(samples, criteria, setpoint=None)

