from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math

from .sr830_settings import sensitivity_code


class AutorangeAction(StrEnum):
    KEEP = "keep"
    WIDEN = "widen"
    NARROW = "narrow"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class AutorangePolicy:
    minimum_full_scale_v: float
    maximum_full_scale_v: float
    target_occupancy: float
    stable_samples_before_narrowing: int
    configured_full_scales_v: tuple[float, ...] | None = None

    def __post_init__(self) -> None:
        full_scales = self.full_scales_v
        if len(full_scales) < 2 or any(
            not math.isfinite(value) or value <= 0
            for value in full_scales
        ):
            raise ValueError("autorange ladder must contain at least two finite positive full scales")
        if tuple(sorted(set(full_scales))) != full_scales:
            raise ValueError("autorange ladder must be strictly increasing")
        try:
            for full_scale in full_scales:
                sensitivity_code(full_scale)
        except ValueError as exc:
            raise ValueError(
                "autorange bounds must use SR830 voltage-input full scales"
            ) from exc
        if not math.isclose(
            self.minimum_full_scale_v, full_scales[0], rel_tol=0.0, abs_tol=1e-15
        ) or not math.isclose(
            self.maximum_full_scale_v, full_scales[-1], rel_tol=0.0, abs_tol=1e-15
        ):
            raise ValueError("autorange bounds must match the configured ladder endpoints")
        if not 0.0 < self.target_occupancy < 1.0:
            raise ValueError("target_occupancy must be between 0 and 1")
        if self.stable_samples_before_narrowing < 1:
            raise ValueError("stable_samples_before_narrowing must be positive")

    @property
    def full_scales_v(self) -> tuple[float, ...]:
        if self.configured_full_scales_v is not None:
            return self.configured_full_scales_v
        if (self.minimum_full_scale_v, self.maximum_full_scale_v) == (0.01, 0.05):
            return (0.01, 0.02, 0.05)
        return (self.minimum_full_scale_v, self.maximum_full_scale_v)


@dataclass(frozen=True, slots=True)
class AutorangeState:
    current_full_scale_v: float
    stable_fit_samples: int = 0


@dataclass(frozen=True, slots=True)
class AutorangeDecision:
    action: AutorangeAction
    state: AutorangeState
    occupancy: float
    reason: str


def decide_autorange(
    policy: AutorangePolicy,
    state: AutorangeState,
    *,
    amplitude_v: float,
    overload: bool = False,
) -> AutorangeDecision:
    """Return one deterministic adjacent-range decision without I/O.

    ``overload`` is retained as a compatibility hook for callers that have an
    independently verified input overload.  Sweep code must not pass SR830
    ``LIAS`` bit 2 (the CH1/CH2 output-overload latch) here: that latch is
    recorded for audit only and is intentionally ignored by the sweep policy.
    """

    if state.current_full_scale_v not in policy.full_scales_v:
        raise ValueError("current_full_scale_v is outside the configured bounds")
    if state.stable_fit_samples < 0:
        raise ValueError("stable_fit_samples cannot be negative")
    if not math.isfinite(amplitude_v) or amplitude_v < 0:
        raise ValueError("amplitude_v must be finite and non-negative")

    occupancy = amplitude_v / state.current_full_scale_v
    current_index = policy.full_scales_v.index(state.current_full_scale_v)
    must_widen = overload or occupancy >= policy.target_occupancy
    if must_widen:
        if current_index == len(policy.full_scales_v) - 1:
            return AutorangeDecision(
                AutorangeAction.FAIL,
                AutorangeState(state.current_full_scale_v, 0),
                occupancy,
                "overload or target occupancy cannot be resolved within bounds",
            )
        return AutorangeDecision(
            AutorangeAction.WIDEN,
            AutorangeState(policy.full_scales_v[current_index + 1], 0),
            occupancy,
            "overload" if overload else "target occupancy reached",
        )

    if current_index == 0:
        return AutorangeDecision(
            AutorangeAction.KEEP,
            AutorangeState(state.current_full_scale_v, 0),
            occupancy,
            "minimum configured full scale is safe",
        )

    fits_narrower = (
        amplitude_v
        <= policy.target_occupancy * policy.full_scales_v[current_index - 1]
    )
    stable_fit_samples = state.stable_fit_samples + 1 if fits_narrower else 0
    if stable_fit_samples < policy.stable_samples_before_narrowing:
        return AutorangeDecision(
            AutorangeAction.KEEP,
            AutorangeState(state.current_full_scale_v, stable_fit_samples),
            occupancy,
            "waiting for consecutive samples that fit the narrower range",
        )
    return AutorangeDecision(
        AutorangeAction.NARROW,
        AutorangeState(policy.full_scales_v[current_index - 1], 0),
        occupancy,
        "consecutive samples fit the narrower range",
    )
