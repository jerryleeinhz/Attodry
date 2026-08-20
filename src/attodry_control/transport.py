from __future__ import annotations

from dataclasses import dataclass
import math


def signed_resistance_ohm(*, voltage_v: float, current_a_rms: float) -> float:
    """Return signed V/I; the caller must supply independently established RMS current."""

    if not math.isfinite(voltage_v):
        raise ValueError("voltage_v must be finite.")
    if not math.isfinite(current_a_rms) or current_a_rms == 0:
        raise ValueError("current_a_rms must be finite and non-zero.")
    return voltage_v / current_a_rms


def rms_current_from_known_series_path(
    *, source_voltage_v_rms: float, total_series_resistance_ohm: float
) -> float:
    """Calculate drive current only when the complete series-path resistance is known."""

    if not math.isfinite(source_voltage_v_rms):
        raise ValueError("source_voltage_v_rms must be finite.")
    if (
        not math.isfinite(total_series_resistance_ohm)
        or total_series_resistance_ohm <= 0
    ):
        raise ValueError("total_series_resistance_ohm must be finite and positive.")
    return source_voltage_v_rms / total_series_resistance_ohm


@dataclass(frozen=True, slots=True)
class LinearGateRelation:
    """Explicit user/calibration supplied relation: top = slope*bottom + intercept."""

    top_per_bottom: float
    top_intercept_v: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.top_per_bottom):
            raise ValueError("top_per_bottom must be finite.")
        if not math.isfinite(self.top_intercept_v):
            raise ValueError("top_intercept_v must be finite.")

    def top_voltage_v(self, bottom_voltage_v: float) -> float:
        if not math.isfinite(bottom_voltage_v):
            raise ValueError("bottom_voltage_v must be finite.")
        return self.top_per_bottom * bottom_voltage_v + self.top_intercept_v

    def points(
        self,
        bottom_voltages_v: tuple[float, ...],
        *,
        top_limit_v: float,
    ) -> tuple[tuple[float, float], ...]:
        if not math.isfinite(top_limit_v) or top_limit_v <= 0:
            raise ValueError("top_limit_v must be finite and positive.")
        result: list[tuple[float, float]] = []
        for bottom in bottom_voltages_v:
            top = self.top_voltage_v(bottom)
            if abs(top) > top_limit_v:
                raise ValueError(
                    f"Calculated top-gate voltage {top:g} V exceeds "
                    f"the explicit limit {top_limit_v:g} V."
                )
            result.append((top, bottom))
        return tuple(result)
