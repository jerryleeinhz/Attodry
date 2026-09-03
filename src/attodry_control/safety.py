from __future__ import annotations

from dataclasses import dataclass
import math
import struct
from typing import Sequence

from .models import VectorField


# The vendor interface exposes field setpoints as 32-bit floats.  Smaller
# configured steps cannot be acknowledged independently by the driver.
FIELD_SETPOINT_READBACK_TOLERANCE_T = 1e-5
# The legacy controller workflow and the commissioned project template both use
# 1 mT as the widest acceptable field-to-target (and zero-field) tolerance.
CONFIRMED_FIELD_TOLERANCE_MAX_T = 0.001
CONFIRMED_EXPERIMENT_VECTOR_MAX_T = 3.0


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
        if self.experiment_vector_max_t > CONFIRMED_EXPERIMENT_VECTOR_MAX_T:
            raise ValueError(
                "The experiment vector limit cannot exceed the confirmed 3 T "
                "project invariant."
            )
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


def plan_zero_detour(
    start: VectorField,
    target: VectorField,
    max_step_t: float,
    limits: MagnetLimits = MagnetLimits(),
) -> tuple[VectorField, ...]:
    """Plan the existing conservative X-then-Z path through zero.

    The complete waypoint list and the mixed setpoint observed after each X write
    are validated before the caller can issue any hardware command.
    """

    validate_vector_field(start, limits)
    validate_vector_field(target, limits)
    if not math.isfinite(max_step_t) or max_step_t <= 0:
        raise ValueError("max_step_t must be finite and positive.")
    if start == target:
        return ()

    points: list[VectorField] = []
    if start.magnitude_t:
        steps = max(1, math.ceil(start.magnitude_t / max_step_t))
        for index in range(steps - 1, -1, -1):
            scale = index / steps
            points.append(VectorField(start.bx_t * scale, start.bz_t * scale))
    if target.magnitude_t:
        steps = max(1, math.ceil(target.magnitude_t / max_step_t))
        for index in range(1, steps + 1):
            scale = index / steps
            points.append(VectorField(target.bx_t * scale, target.bz_t * scale))

    previous = start
    previous_command = _float32_field(start)
    validate_vector_field(previous_command, limits)
    for point in points:
        validate_vector_field(point, limits)
        validate_vector_field(VectorField(point.bx_t, previous.bz_t), limits)
        # The DLL receives IEEE-754 float32 components.  Validate those exact
        # command values and their X-first mixed state before any caller can
        # issue a write; rounding at the 3 T boundary must not escape the disk.
        command = _float32_field(point)
        validate_vector_field(command, limits)
        validate_vector_field(
            VectorField(command.bx_t, previous_command.bz_t), limits
        )
        if (
            math.hypot(
                command.bx_t - previous_command.bx_t,
                command.bz_t - previous_command.bz_t,
            )
            > max_step_t + FIELD_SETPOINT_READBACK_TOLERANCE_T
        ):
            raise SafetyViolation(
                "Float32 command quantization would exceed max_step_t beyond "
                "the setpoint-readback tolerance."
            )
        previous = point
        previous_command = command
    return tuple(points)


def _float32_field(field: VectorField) -> VectorField:
    def convert(value: float) -> float:
        return float(struct.unpack("<f", struct.pack("<f", value))[0])

    return VectorField(convert(field.bx_t), convert(field.bz_t))


def plan_ordered_zero_detours(
    start: VectorField,
    targets: Sequence[VectorField],
    max_step_t: float,
    limits: MagnetLimits = MagnetLimits(),
) -> tuple[tuple[VectorField, ...], ...]:
    """Plan zero detours for an explicit ordered target list.

    Order and duplicate targets are retained.  No Cartesian expansion or axis
    generalization is performed.
    """

    checked_targets = tuple(
        validate_vector_field(target, limits) for target in targets
    )
    if not checked_targets:
        raise ValueError("At least one ordered magnetic-field point is required.")
    paths: list[tuple[VectorField, ...]] = []
    current = validate_vector_field(start, limits)
    for target in checked_targets:
        paths.append(plan_zero_detour(current, target, max_step_t, limits))
        current = target
    return tuple(paths)

