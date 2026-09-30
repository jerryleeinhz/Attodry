from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
import math
import struct
from typing import Sequence

from .models import VectorField


# Original float32 path/step guard. Keep it separate from configurable register
# acknowledgement; widening ACK must not relax nominal endpoints or corners.
FIELD_SETPOINT_READBACK_TOLERANCE_T = 1e-5
# The legacy controller workflow and its commissioned project template both use
# 1 mT as the widest acceptable field-to-target (and zero-field) tolerance.
CONFIRMED_FIELD_TOLERANCE_MAX_T = 0.001
APPROVED_READBACK_TOLERANCE_MAX_T = 0.0015
# Operator-approved register ACK tolerance after the 44 uT quantization audit;
# not an actual-field arrival criterion or a manufacturer accuracy claim.
FIELD_SETPOINT_ACK_TOLERANCE_T = 0.0001
CONFIRMED_EXPERIMENT_VECTOR_MAX_T = 3.0
CONFIRMED_HARDWARE_X_MAX_T = 3.0
CONFIRMED_HARDWARE_Z_MAX_T = 9.0
FIELD_LIMIT_POLICY = "single-axis-hardware_combined-vector-v1"
# Operator-approved 2026-09-30 software readback acceptance, not a factory
# accuracy specification or permission to request fields beyond nominal limits.
FIELD_READBACK_POLICY = "planned-axis-readback-v2"
UNIFIED_FIELD_READBACK_POLICY = "planned-axis-readback-v3"
FIELD_READBACK_MARGIN_T = 0.0005
INACTIVE_AXIS_READBACK_MAX_T = 0.0005


class SafetyViolation(ValueError):
    """Raised before an unsafe target can reach a hardware driver."""


class FieldTransitionPolicy(StrEnum):
    """The explicitly configured path between adjacent ordered field points."""

    DIRECT = "direct"
    VIA_ZERO = "via_zero"


class FieldScanMode(StrEnum):
    SINGLE_X = "single_x"
    SINGLE_Z = "single_z"
    VECTOR = "vector"


class AxisWriteOrder(StrEnum):
    """One verified order for the vendor's separate X and Z setters."""

    X_THEN_Z = "x_then_z"
    Z_THEN_X = "z_then_x"


@dataclass(frozen=True, slots=True)
class FieldWaypoint:
    """One exact command endpoint and its verified component-write choices."""

    requested_field: VectorField
    command_field: VectorField
    x_then_z_mixed_corner: VectorField
    z_then_x_mixed_corner: VectorField
    axis_order: AxisWriteOrder


@dataclass(frozen=True, slots=True)
class FieldTransitionPlan:
    """A complete, prevalidated transition for one explicit requested target."""

    transition_policy: FieldTransitionPolicy
    requested_target: VectorField
    start_command: VectorField
    target_command: VectorField
    waypoints: tuple[FieldWaypoint, ...]


@dataclass(frozen=True, slots=True)
class MagnetLimits:
    """Axis ceilings always apply; the resultant ceiling applies to dual-axis fields."""

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
        if (
            self.hardware_x_max_t > CONFIRMED_HARDWARE_X_MAX_T
            or self.hardware_z_max_t > CONFIRMED_HARDWARE_Z_MAX_T
        ):
            raise ValueError("Axis limits cannot exceed the confirmed X 3 T / Z 9 T ratings.")
        if self.experiment_vector_max_t > CONFIRMED_EXPERIMENT_VECTOR_MAX_T:
            raise ValueError(
                "The experiment vector limit cannot exceed the confirmed 3 T "
                "dual-axis project limit."
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
    # Strict command/legacy envelope: small nonzero components are never zeroed.
    # Approved scan readbacks use the separately declared FieldReadbackPolicy.
    if (
        target.bx_t != 0.0
        and target.bz_t != 0.0
        and target.magnitude_t > limits.experiment_vector_max_t
    ):
        raise SafetyViolation(
            f"Dual-axis |B|={target.magnitude_t:g} T exceeds the project limit "
            f"{limits.experiment_vector_max_t:g} T."
        )
    return target


@dataclass(frozen=True, slots=True)
class FieldReadbackPolicy:
    """Run-wide mode from targets; strict commands and bounded actual readbacks."""

    mode: FieldScanMode
    limits: MagnetLimits = MagnetLimits()
    readback_tolerance_t: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mode, FieldScanMode) or not isinstance(self.limits, MagnetLimits):
            raise ValueError("Field policy requires a validated mode and nominal limits.")
        if self.readback_tolerance_t is not None and (
            type(self.readback_tolerance_t) not in (float, int)
            or not math.isfinite(self.readback_tolerance_t)
            or not 0 < self.readback_tolerance_t <= APPROVED_READBACK_TOLERANCE_MAX_T
        ):
            raise ValueError("readback_tolerance_t must be positive and at most 0.0015 T.")

    @classmethod
    def from_targets(cls, targets: Sequence[VectorField],
                     limits: MagnetLimits = MagnetLimits(),
                     readback_tolerance_t: float | None = None) -> FieldReadbackPolicy:
        points = tuple(targets)
        if not points:
            raise ValueError("Field policy requires the complete nonempty target plan.")
        has_x = any(p.bx_t != 0 for p in points)
        has_z = any(p.bz_t != 0 for p in points)
        mode = (FieldScanMode.SINGLE_X if has_x and not has_z else
                FieldScanMode.SINGLE_Z if has_z and not has_x else FieldScanMode.VECTOR)
        policy = cls(mode, limits, readback_tolerance_t)
        for point in points:
            policy.validate_target(point)
        return policy

    def validate_target(self, target: VectorField) -> VectorField:
        validate_vector_field(target, self.limits)
        if ((self.mode is FieldScanMode.SINGLE_X and target.bz_t != 0) or
                (self.mode is FieldScanMode.SINGLE_Z and target.bx_t != 0)):
            raise SafetyViolation("Non-scanning axis setpoint must remain exactly zero.")
        if self.mode is FieldScanMode.VECTOR and target.magnitude_t > self.limits.experiment_vector_max_t:
            raise SafetyViolation("Vector scan target exceeds the nominal project limit.")
        return target

    def _axis_readback_limit(self, nominal: float) -> float:
        # This approval covers the 3 T boundary and reduced experiment limits.
        # The standalone Z 9 T ceiling is not increased.
        return nominal + (FIELD_READBACK_MARGIN_T if nominal <= 3.0 else 0.0)

    @property
    def inactive_axis_max_t(self) -> float:
        return (INACTIVE_AXIS_READBACK_MAX_T if self.readback_tolerance_t is None
                else self.readback_tolerance_t)

    def validate_readback(self, actual: VectorField) -> VectorField:
        checks = {
            "Bx": (actual.bx_t, self._axis_readback_limit(self.limits.hardware_x_max_t)),
            "Bz": (actual.bz_t, self._axis_readback_limit(self.limits.hardware_z_max_t)),
        }
        if self.mode is FieldScanMode.SINGLE_X:
            checks["Bz"] = (actual.bz_t, max(self.inactive_axis_max_t,
                float32_value(self.inactive_axis_max_t)))
        elif self.mode is FieldScanMode.SINGLE_Z:
            checks["Bx"] = (actual.bx_t, max(self.inactive_axis_max_t,
                float32_value(self.inactive_axis_max_t)))
        for axis, (value, limit) in checks.items():
            if not math.isfinite(value) or abs(value) > limit:
                raise SafetyViolation(
                    f"{self.mode.value} readback {axis}={value:.12g} T exceeds "
                    f"{limit:.12g} T "
                    f"(Bx={actual.bx_t:.12g}, Bz={actual.bz_t:.12g})."
                )
        if self.mode is FieldScanMode.VECTOR:
            limit = self.limits.experiment_vector_max_t + FIELD_READBACK_MARGIN_T
            if actual.magnitude_t > limit:
                raise SafetyViolation(
                    f"Vector readback |B|={actual.magnitude_t:.12g} T exceeds "
                    f"{limit:.12g} T by {actual.magnitude_t - limit:.12g} T "
                    f"(Bx={actual.bx_t:.12g}, Bz={actual.bz_t:.12g})."
                )
        return actual

    def assessment(self, actual: VectorField) -> dict[str, object]:
        problem = None
        try:
            self.validate_readback(actual)
        except SafetyViolation as exc:
            problem = str(exc)
        return {
            "policy": self.snapshot()["version"], "mode": self.mode.value,
            "actual_magnitude_t": actual.magnitude_t,
            "within_readback_policy": problem is None, "problem": problem,
            "inactive_axis_nonzero": (
                (self.mode is FieldScanMode.SINGLE_X and actual.bz_t != 0) or
                (self.mode is FieldScanMode.SINGLE_Z and actual.bx_t != 0)),
            "nominal_limit_exceeded": (
                abs(actual.bx_t) > self.limits.hardware_x_max_t or
                abs(actual.bz_t) > self.limits.hardware_z_max_t or
                (self.mode is FieldScanMode.VECTOR and
                 actual.magnitude_t > self.limits.experiment_vector_max_t)),
        }

    def snapshot(self) -> dict[str, object]:
        return {
            "version": (FIELD_READBACK_POLICY if self.readback_tolerance_t is None
                        else UNIFIED_FIELD_READBACK_POLICY), "mode": self.mode.value,
            "limits": asdict(self.limits), "readback_margin_t": FIELD_READBACK_MARGIN_T,
            "inactive_axis_max_abs_t": self.inactive_axis_max_t,
            "axis_readback_limits_t": {
                "x": (max(self.inactive_axis_max_t, float32_value(self.inactive_axis_max_t))
                      if self.mode is FieldScanMode.SINGLE_Z
                      else self._axis_readback_limit(self.limits.hardware_x_max_t)),
                "z": (max(self.inactive_axis_max_t, float32_value(self.inactive_axis_max_t))
                      if self.mode is FieldScanMode.SINGLE_X
                      else self._axis_readback_limit(self.limits.hardware_z_max_t)),
            },
            "vector_readback_limit_t": (
                self.limits.experiment_vector_max_t + FIELD_READBACK_MARGIN_T
                if self.mode is FieldScanMode.VECTOR else None),
            **({"readback_tolerance_t": self.readback_tolerance_t,
                "zero_magnitude_tolerance_t": self.readback_tolerance_t}
               if self.readback_tolerance_t is not None else {}),
        }

    @classmethod
    def from_snapshot(cls, snapshot: dict) -> FieldReadbackPolicy:
        policy = cls(FieldScanMode(snapshot["mode"]), MagnetLimits(**snapshot["limits"]),
                     snapshot.get("readback_tolerance_t"))
        if snapshot != policy.snapshot():
            raise ValueError("Invalid or unsupported archived field readback policy.")
        return policy


def float32_value(value: float) -> float:
    """Return the exact finite IEEE-754 binary32 value sent to the DLL."""

    try:
        return float(struct.unpack("<f", struct.pack("<f", value))[0])
    except (OverflowError, struct.error) as exc:
        raise SafetyViolation("Field command cannot be represented as float32.") from exc


def float32_bits_hex(value: float) -> str:
    """Return the authoritative binary32 bit pattern used in the command audit."""

    try:
        return struct.pack("<f", value).hex()
    except (OverflowError, struct.error) as exc:
        raise SafetyViolation("Field command cannot be represented as float32.") from exc


def float32_field(field: VectorField) -> VectorField:
    """Quantize both field components exactly as ``ctypes.c_float`` will."""

    return VectorField(float32_value(field.bx_t), float32_value(field.bz_t))


def serialize_float32_field(field: VectorField) -> dict[str, float | str]:
    """Serialize an exact binary32 vector without relying on JSON decimals alone."""

    command = float32_field(field)
    return {
        "bx_t": command.bx_t,
        "bz_t": command.bz_t,
        "bx_t_float32_bits_hex": float32_bits_hex(command.bx_t),
        "bz_t_float32_bits_hex": float32_bits_hex(command.bz_t),
    }


def plan_field_waypoint(
    previous_command: VectorField,
    requested_field: VectorField,
    max_step_t: float,
    limits: MagnetLimits = MagnetLimits(),
) -> FieldWaypoint:
    """Quantize and validate one endpoint plus both possible component corners.

    The vendor provides separate component setters, so a vector endpoint alone is
    not a sufficient safety proof.  Both X→Z and Z→X intermediate setpoints are
    checked using the exact binary32 values.  The lower-magnitude safe corner is
    chosen deterministically; an X→Z tie-break keeps audits reproducible.
    """

    previous = float32_field(validate_vector_field(previous_command, limits))
    validate_vector_field(previous, limits)
    requested = validate_vector_field(requested_field, limits)
    command = float32_field(requested)
    validate_vector_field(command, limits)
    if (
        math.hypot(
            command.bx_t - previous.bx_t,
            command.bz_t - previous.bz_t,
        )
        > max_step_t + FIELD_SETPOINT_READBACK_TOLERANCE_T
    ):
        raise SafetyViolation(
            "Float32 command quantization would exceed max_step_t beyond "
            "the setpoint-readback tolerance."
        )

    x_then_z = VectorField(command.bx_t, previous.bz_t)
    z_then_x = VectorField(previous.bx_t, command.bz_t)
    x_then_z_safe = _is_safe_vector(x_then_z, limits)
    z_then_x_safe = _is_safe_vector(z_then_x, limits)
    if not x_then_z_safe and not z_then_x_safe:
        raise SafetyViolation(
            "Neither X-then-Z nor Z-then-X float32 mixed corner is within "
            "the confirmed vector limit."
        )
    if x_then_z_safe and (
        not z_then_x_safe
        or x_then_z.magnitude_t <= z_then_x.magnitude_t
    ):
        axis_order = AxisWriteOrder.X_THEN_Z
    else:
        axis_order = AxisWriteOrder.Z_THEN_X
    return FieldWaypoint(
        requested_field=requested,
        command_field=command,
        x_then_z_mixed_corner=x_then_z,
        z_then_x_mixed_corner=z_then_x,
        axis_order=axis_order,
    )


def plan_field_transition(
    start: VectorField,
    target: VectorField,
    max_step_t: float,
    transition_policy: FieldTransitionPolicy,
    limits: MagnetLimits = MagnetLimits(),
) -> FieldTransitionPlan:
    """Build a complete exact-float32 transition for one requested target."""

    checked_start = validate_vector_field(start, limits)
    checked_target = validate_vector_field(target, limits)
    if not math.isfinite(max_step_t) or max_step_t <= 0:
        raise ValueError("max_step_t must be finite and positive.")
    try:
        policy = FieldTransitionPolicy(transition_policy)
    except ValueError as exc:
        raise ValueError("transition_policy must be 'direct' or 'via_zero'.") from exc

    start_command = float32_field(checked_start)
    target_command = float32_field(checked_target)
    validate_vector_field(start_command, limits)
    validate_vector_field(target_command, limits)
    if _same_float32_field(start_command, target_command):
        return FieldTransitionPlan(
            transition_policy=policy,
            requested_target=checked_target,
            start_command=start_command,
            target_command=target_command,
            waypoints=(),
        )

    if policy is FieldTransitionPolicy.DIRECT:
        requested_waypoints = _direct_waypoints(
            checked_start, checked_target, max_step_t
        )
    else:
        requested_waypoints = _via_zero_waypoints(
            checked_start, checked_target, max_step_t
        )

    previous_command = start_command
    waypoints: list[FieldWaypoint] = []
    for requested_waypoint in requested_waypoints:
        waypoint = plan_field_waypoint(
            previous_command,
            requested_waypoint,
            max_step_t,
            limits,
        )
        waypoints.append(waypoint)
        previous_command = waypoint.command_field
    if not _same_float32_field(previous_command, target_command):
        raise SafetyViolation(
            "The field transition did not end at the exact float32 target command."
        )
    return FieldTransitionPlan(
        transition_policy=policy,
        requested_target=checked_target,
        start_command=start_command,
        target_command=target_command,
        waypoints=tuple(waypoints),
    )


def plan_ordered_field_transitions(
    start: VectorField,
    targets: Sequence[VectorField],
    max_step_t: float,
    transition_policy: FieldTransitionPolicy,
    limits: MagnetLimits = MagnetLimits(),
) -> tuple[FieldTransitionPlan, ...]:
    """Plan each literal ordered target without sorting or deduplicating it."""

    checked_targets = tuple(
        validate_vector_field(target, limits) for target in targets
    )
    if not checked_targets:
        raise ValueError("At least one ordered magnetic-field point is required.")
    current = float32_field(validate_vector_field(start, limits))
    plans: list[FieldTransitionPlan] = []
    for target in checked_targets:
        plan = plan_field_transition(
            current,
            target,
            max_step_t,
            transition_policy,
            limits,
        )
        plans.append(plan)
        current = plan.target_command
    return tuple(plans)


def plan_zero_detour(
    start: VectorField,
    target: VectorField,
    max_step_t: float,
    limits: MagnetLimits = MagnetLimits(),
) -> tuple[VectorField, ...]:
    """Compatibility wrapper for the explicitly selected ``via_zero`` policy."""

    plan = plan_field_transition(
        start,
        target,
        max_step_t,
        FieldTransitionPolicy.VIA_ZERO,
        limits,
    )
    return tuple(waypoint.requested_field for waypoint in plan.waypoints)


def plan_ordered_zero_detours(
    start: VectorField,
    targets: Sequence[VectorField],
    max_step_t: float,
    limits: MagnetLimits = MagnetLimits(),
) -> tuple[tuple[VectorField, ...], ...]:
    """Compatibility wrapper retaining the former explicit zero-detour output."""

    plans = plan_ordered_field_transitions(
        start,
        targets,
        max_step_t,
        FieldTransitionPolicy.VIA_ZERO,
        limits,
    )
    return tuple(
        tuple(waypoint.requested_field for waypoint in plan.waypoints)
        for plan in plans
    )


def _direct_waypoints(
    start: VectorField,
    target: VectorField,
    max_step_t: float,
) -> tuple[VectorField, ...]:
    distance = math.hypot(target.bx_t - start.bx_t, target.bz_t - start.bz_t)
    steps = max(1, math.ceil(distance / max_step_t))
    values = tuple(
        VectorField(
            start.bx_t + (target.bx_t - start.bx_t) * index / steps,
            start.bz_t + (target.bz_t - start.bz_t) * index / steps,
        )
        for index in range(1, steps + 1)
    )
    return (*values[:-1], target)


def _via_zero_waypoints(
    start: VectorField,
    target: VectorField,
    max_step_t: float,
) -> tuple[VectorField, ...]:
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
    return tuple(points)


def _is_safe_vector(target: VectorField, limits: MagnetLimits) -> bool:
    try:
        validate_vector_field(target, limits)
    except SafetyViolation:
        return False
    return True


def _same_float32_field(left: VectorField, right: VectorField) -> bool:
    return (
        float32_bits_hex(left.bx_t) == float32_bits_hex(right.bx_t)
        and float32_bits_hex(left.bz_t) == float32_bits_hex(right.bz_t)
    )

