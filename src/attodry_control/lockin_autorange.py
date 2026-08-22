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
    maximum_adjustment_steps: int

    def __post_init__(self) -> None:
        try:
            sensitivity_code(self.minimum_full_scale_v)
            sensitivity_code(self.maximum_full_scale_v)
        except ValueError as exc:
            raise ValueError(
                "autorange bounds must use project-confirmed SR830 full scales"
            ) from exc
        if (self.minimum_full_scale_v, self.maximum_full_scale_v) not in {
            (0.001, 0.01),
            (0.01, 0.02),
        }:
            raise ValueError(
                "autorange bounds must be a confirmed adjacent pair: "
                "0.001-0.01 V or 0.01-0.02 V"
            )
        if not math.isclose(
            self.target_occupancy, 0.85, rel_tol=0.0, abs_tol=1e-12
        ):
            raise ValueError("target_occupancy must be the confirmed value 0.85")
        if self.stable_samples_before_narrowing != 2:
            raise ValueError(
                "stable_samples_before_narrowing must be the confirmed value 2"
            )
        if self.maximum_adjustment_steps != 1:
            raise ValueError("maximum_adjustment_steps must be the confirmed value 1")


@dataclass(frozen=True, slots=True)
class AutorangeState:
    current_full_scale_v: float
    adjustment_steps: int = 0
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
    overload: bool,
) -> AutorangeDecision:
    """Return one deterministic pre-sampling range decision without I/O."""

    if state.current_full_scale_v not in (
        policy.minimum_full_scale_v,
        policy.maximum_full_scale_v,
    ):
        raise ValueError("current_full_scale_v is outside the configured bounds")
    if not 0 <= state.adjustment_steps <= policy.maximum_adjustment_steps:
        raise ValueError("adjustment_steps is outside the configured limit")
    if state.stable_fit_samples < 0:
        raise ValueError("stable_fit_samples cannot be negative")
    if not math.isfinite(amplitude_v) or amplitude_v < 0:
        raise ValueError("amplitude_v must be finite and non-negative")

    occupancy = amplitude_v / state.current_full_scale_v
    must_widen = overload or occupancy >= policy.target_occupancy
    if must_widen:
        if (
            state.current_full_scale_v == policy.maximum_full_scale_v
            or state.adjustment_steps >= policy.maximum_adjustment_steps
        ):
            return AutorangeDecision(
                AutorangeAction.FAIL,
                AutorangeState(
                    state.current_full_scale_v,
                    state.adjustment_steps,
                    0,
                ),
                occupancy,
                "overload or target occupancy cannot be resolved within bounds",
            )
        return AutorangeDecision(
            AutorangeAction.WIDEN,
            AutorangeState(
                policy.maximum_full_scale_v,
                state.adjustment_steps + 1,
                0,
            ),
            occupancy,
            "overload" if overload else "target occupancy reached",
        )

    if state.current_full_scale_v == policy.minimum_full_scale_v:
        return AutorangeDecision(
            AutorangeAction.KEEP,
            AutorangeState(
                state.current_full_scale_v,
                state.adjustment_steps,
                0,
            ),
            occupancy,
            "minimum configured full scale is safe",
        )

    fits_narrower = (
        amplitude_v
        <= policy.target_occupancy * policy.minimum_full_scale_v
    )
    stable_fit_samples = state.stable_fit_samples + 1 if fits_narrower else 0
    if stable_fit_samples < policy.stable_samples_before_narrowing:
        return AutorangeDecision(
            AutorangeAction.KEEP,
            AutorangeState(
                state.current_full_scale_v,
                state.adjustment_steps,
                stable_fit_samples,
            ),
            occupancy,
            "waiting for consecutive samples that fit the narrower range",
        )
    if state.adjustment_steps >= policy.maximum_adjustment_steps:
        return AutorangeDecision(
            AutorangeAction.KEEP,
            AutorangeState(
                state.current_full_scale_v,
                state.adjustment_steps,
                stable_fit_samples,
            ),
            occupancy,
            "adjustment limit reached; current range remains safe",
        )
    return AutorangeDecision(
        AutorangeAction.NARROW,
        AutorangeState(
            policy.minimum_full_scale_v,
            state.adjustment_steps + 1,
            0,
        ),
        occupancy,
        "consecutive samples fit the narrower range",
    )
