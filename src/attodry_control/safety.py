from __future__ import annotations

from dataclasses import dataclass
import math

from .models import VectorField


class SafetyViolation(ValueError):
    """Raised before an unsafe target can reach a hardware driver."""


@dataclass(frozen=True, slots=True)
class MagnetLimits:
    hardware_x_max_t: float = 3.0
    hardware_z_max_t: float = 9.0
    experiment_vector_max_t: float = 3.0

    def __post_init__(self) -> None:
        values = (
            self.hardware_x_max_t,
            self.hardware_z_max_t,
            self.experiment_vector_max_t,
        )
        if any(not math.isfinite(value) or value <= 0 for value in values):
            raise ValueError("All magnet limits must be finite and positive.")
        if self.experiment_vector_max_t > math.hypot(
            self.hardware_x_max_t, self.hardware_z_max_t
        ):
            raise ValueError("The vector limit cannot exceed the combined hardware envelope.")


def validate_vector_field(
    target: VectorField,
    limits: MagnetLimits = MagnetLimits(),
) -> VectorField:
    """Return the target when it satisfies every confirmed magnet limit."""

    if abs(target.bx_t) > limits.hardware_x_max_t:
        raise SafetyViolation(
            f"|Bx|={abs(target.bx_t):g} T exceeds {limits.hardware_x_max_t:g} T."
        )
    if abs(target.bz_t) > limits.hardware_z_max_t:
        raise SafetyViolation(
            f"|Bz|={abs(target.bz_t):g} T exceeds {limits.hardware_z_max_t:g} T."
        )
    if target.magnitude_t > limits.experiment_vector_max_t:
        raise SafetyViolation(
            f"|B|={target.magnitude_t:g} T exceeds the project limit "
            f"{limits.experiment_vector_max_t:g} T."
        )
    return target

