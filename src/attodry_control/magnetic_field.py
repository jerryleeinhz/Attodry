from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import time
from typing import Callable, Sequence

from .attodry import (
    FIELD_SETPOINT_READBACK_TOLERANCE_T,
    AttoDryDriver,
    AttoDryError,
)
from .models import CryostatState, VectorField
from .safety import (
    FieldTransitionPlan,
    FieldTransitionPolicy,
    FieldWaypoint,
    plan_ordered_field_transitions,
    serialize_float32_field,
    validate_vector_field,
)


FieldEventSink = Callable[[dict[str, object]], None]


@dataclass(frozen=True, slots=True)
class FieldPointResult:
    point_index: int
    requested_field: VectorField
    waypoint_count: int
    final_state: CryostatState


def execute_field_target(
    driver: AttoDryDriver,
    target: VectorField,
    max_step_t: float,
    *,
    point_index: int = 0,
    transition_policy: FieldTransitionPolicy = FieldTransitionPolicy.VIA_ZERO,
    on_event: FieldEventSink | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> FieldPointResult:
    """Execute one field target on an already-open driver.

    Connection ownership and failure cleanup deliberately remain with the caller.
    """

    results = execute_ordered_field_points(
        driver,
        (target,),
        max_step_t,
        first_point_index=point_index,
        transition_policy=transition_policy,
        on_event=on_event,
        monotonic=monotonic,
        sleeper=sleeper,
    )
    return results[0]


def execute_ordered_field_points(
    driver: AttoDryDriver,
    points: Sequence[VectorField],
    max_step_t: float,
    *,
    first_point_index: int = 0,
    transition_policy: FieldTransitionPolicy = FieldTransitionPolicy.VIA_ZERO,
    on_event: FieldEventSink | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[FieldPointResult, ...]:
    """Execute an explicit X/Z point list without sorting or deduplication."""

    targets = tuple(validate_vector_field(point, driver.limits) for point in points)
    if not targets:
        raise ValueError("At least one ordered magnetic-field point is required.")
    if (
        isinstance(first_point_index, bool)
        or not isinstance(first_point_index, int)
        or first_point_index < 0
    ):
        raise ValueError("first_point_index must be a non-negative integer.")

    try:
        policy = FieldTransitionPolicy(transition_policy)
    except ValueError as exc:
        raise ValueError("transition_policy must be 'direct' or 'via_zero'.") from exc

    initial_state = driver.read_state()
    _emit(
        on_event,
        "field_preflight_state",
        state=asdict(initial_state),
    )
    _validate_state(driver, initial_state)
    plans = plan_ordered_field_transitions(
        initial_state.field_setpoint,
        targets,
        max_step_t,
        policy,
        driver.limits,
    )
    _emit(
        on_event,
        "field_plan_validated",
        transition_policy=policy.value,
        transitions=_serialize_transition_plans(plans),
    )

    def control_sample(
        state: CryostatState,
        elapsed_s: float,
        phase: str,
        waypoint_index: int | None,
    ) -> None:
        _emit_state_sample(
            on_event,
            state,
            elapsed_s=elapsed_s,
            phase=phase,
            point_index=None,
            waypoint_index=waypoint_index,
        )
        _validate_state(driver, state)

    driver.ensure_field_control(
        True,
        monotonic=monotonic,
        sleeper=sleeper,
        on_sample=control_sample,
        on_command=on_event,
        command_context={
            "phase": "field_control_enable",
            "transition_policy": policy.value,
        },
    )
    control_state = driver.last_confirmed_state or driver.read_state()
    _validate_state(driver, control_state, require_control=True)
    _emit(on_event, "field_control_confirmed", state=asdict(control_state))

    # Revalidate the whole setpoint plan after the control acknowledgement and
    # before the first component setting write.
    plans = plan_ordered_field_transitions(
        control_state.field_setpoint,
        targets,
        max_step_t,
        policy,
        driver.limits,
    )
    _emit(
        on_event,
        "field_plan_revalidated",
        transition_policy=policy.value,
        transitions=_serialize_transition_plans(plans),
    )

    results: list[FieldPointResult] = []
    for offset, (target, plan) in enumerate(zip(targets, plans)):
        point_index = first_point_index + offset
        _emit(
            on_event,
            "point_started",
            point_index=point_index,
            requested_field=asdict(target),
            transition_policy=policy.value,
            execution_plan=_serialize_transition_plan(plan),
        )

        def setpoint_sample(
            state: CryostatState,
            elapsed_s: float,
            phase: str,
            waypoint_index: int | None,
            *,
            _point_index: int = point_index,
        ) -> None:
            _emit_state_sample(
                on_event,
                state,
                elapsed_s=elapsed_s,
                phase=phase,
                point_index=_point_index,
                waypoint_index=waypoint_index,
            )
            _validate_state(driver, state, require_control=True)

        setpoint_state = driver.set_vector_field(
            target,
            max_step_t=max_step_t,
            planning_start=plan.start_command,
            transition_plan=plan,
            transition_policy=policy,
            monotonic=monotonic,
            sleeper=sleeper,
            on_sample=setpoint_sample,
            on_command=on_event,
            command_context={
                "phase": "operation",
                "transition_policy": policy.value,
                "point_index": point_index,
            },
        )
        _validate_state(
            driver,
            setpoint_state,
            require_control=True,
            expected_setpoint=plan.target_command,
        )
        _emit(
            on_event,
            "setpoint_confirmed",
            point_index=point_index,
            requested_field=asdict(target),
            command_field=serialize_float32_field(plan.target_command),
            state=asdict(setpoint_state),
        )

        def stability_sample(
            state: CryostatState,
            elapsed_s: float,
            phase: str,
            waypoint_index: int | None,
            *,
            _point_index: int = point_index,
        ) -> None:
            _emit_state_sample(
                on_event,
                state,
                elapsed_s=elapsed_s,
                phase=phase,
                point_index=_point_index,
                waypoint_index=waypoint_index,
            )
            _validate_state(
                driver,
                state,
                expected_setpoint=plan.target_command,
            )

        final_state = driver.wait_for_field(
            plan.target_command,
            monotonic=monotonic,
            sleeper=sleeper,
            on_sample=stability_sample,
        )
        _validate_state(
            driver,
            final_state,
            require_control=True,
            expected_setpoint=plan.target_command,
        )
        result = FieldPointResult(
            point_index=point_index,
            requested_field=target,
            waypoint_count=len(plan.waypoints),
            final_state=final_state,
        )
        results.append(result)
        _emit(
            on_event,
            "point_completed",
            point_index=point_index,
            requested_field=asdict(target),
            command_field=serialize_float32_field(plan.target_command),
            waypoint_count=len(plan.waypoints),
            final_state=asdict(final_state),
        )
    return tuple(results)


def _validate_state(
    driver: AttoDryDriver,
    state: CryostatState,
    *,
    require_control: bool = False,
    expected_setpoint: VectorField | None = None,
) -> None:
    validate_vector_field(state.field, driver.limits)
    validate_vector_field(state.field_setpoint, driver.limits)
    if state.error_code:
        raise AttoDryError(f"attoDRY field operation reported error code {state.error_code}.")
    if require_control and not state.field_control_enabled:
        raise AttoDryError("Field control is not confirmed enabled.")
    if expected_setpoint is not None and not _field_matches(
        state.field_setpoint, expected_setpoint
    ):
        raise AttoDryError("Vector-field setpoint does not match the requested target.")


def _field_matches(actual: VectorField, expected: VectorField) -> bool:
    return math.isclose(
        actual.bx_t,
        expected.bx_t,
        rel_tol=0.0,
        abs_tol=FIELD_SETPOINT_READBACK_TOLERANCE_T,
    ) and math.isclose(
        actual.bz_t,
        expected.bz_t,
        rel_tol=0.0,
        abs_tol=FIELD_SETPOINT_READBACK_TOLERANCE_T,
    )


def _serialize_transition_plans(
    plans: tuple[FieldTransitionPlan, ...],
) -> list[dict[str, object]]:
    return [_serialize_transition_plan(plan) for plan in plans]


def _serialize_transition_plan(plan: FieldTransitionPlan) -> dict[str, object]:
    return {
        "transition_policy": plan.transition_policy.value,
        "requested_target": asdict(plan.requested_target),
        "start_command": serialize_float32_field(plan.start_command),
        "target_command": serialize_float32_field(plan.target_command),
        "waypoints": [_serialize_waypoint(waypoint) for waypoint in plan.waypoints],
    }


def _serialize_waypoint(waypoint: FieldWaypoint) -> dict[str, object]:
    return {
        "requested_field": asdict(waypoint.requested_field),
        "command_field": serialize_float32_field(waypoint.command_field),
        "x_then_z_mixed_corner": serialize_float32_field(
            waypoint.x_then_z_mixed_corner
        ),
        "z_then_x_mixed_corner": serialize_float32_field(
            waypoint.z_then_x_mixed_corner
        ),
        "axis_order": waypoint.axis_order.value,
    }


def _emit_state_sample(
    sink: FieldEventSink | None,
    state: CryostatState,
    *,
    elapsed_s: float,
    phase: str,
    point_index: int | None,
    waypoint_index: int | None,
) -> None:
    _emit(
        sink,
        "field_sample",
        phase=phase,
        point_index=point_index,
        waypoint_index=waypoint_index,
        elapsed_s=elapsed_s,
        state=asdict(state),
    )


def _emit(
    sink: FieldEventSink | None,
    event: str,
    **payload: object,
) -> None:
    if sink is not None:
        sink({"event": event, **payload})
