from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import math
from pathlib import Path
import struct
import sys
from typing import Sequence

from .field_audit import SCHEMA_VERSION, read_jsonl_events
from .field_segments import expand_field_segments
from .models import VectorField
from .safety import FIELD_LIMIT_POLICY, MagnetLimits, validate_vector_field


_FIELD_COMMAND_AUDIT_DECLARATION = {
    "protocol": "attempt-result-v1",
    "required": True,
    "float32_encoding": "ieee754-binary32-bits-hex",
}
_FIELD_TRANSITION_POLICIES = {"direct", "via_zero"}
_FIELD_AXIS_ORDERS = {"x_then_z", "z_then_x"}
_FIELD_COMMAND_SYMBOLS = {
    "toggle_field_control": "AttoDRY_Interface_toggleMagneticFieldControl",
    "sweep_field_to_zero": "AttoDRY_Interface_sweepFieldToZero",
}
_FIELD_COMPONENT_SYMBOLS = {
    "x": "AttoDRY_Interface_setUserMagneticFieldX",
    "z": "AttoDRY_Interface_setUserMagneticFieldZ",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read one magnetic-field progress JSONL without importing or "
            "connecting to the attoDRY DLL."
        )
    )
    parser.add_argument("--progress", type=Path, required=True)
    return parser


def read_progress_snapshot(path: str | Path) -> dict[str, object]:
    events, trailing_line_incomplete = read_jsonl_events(path)
    completed_points = sum(
        event.get("event") == "point_completed" for event in events
    )
    current_point_index: int | None = None
    last_confirmed_state: object = None
    terminal: dict[str, object] | None = None
    for event in events:
        point_index = event.get("point_index")
        if (
            event.get("event") in {"point_started", "point_completed"}
            and isinstance(point_index, int)
            and not isinstance(point_index, bool)
        ):
            current_point_index = point_index
        for key in ("last_confirmed_state", "final_state", "state"):
            candidate = event.get(key)
            if isinstance(candidate, dict):
                last_confirmed_state = candidate
        if event.get("event") == "run_finished":
            terminal = event

    integrity_errors = _integrity_errors(events, trailing_line_incomplete)

    if terminal is None or integrity_errors:
        # A file-only reader cannot know whether the producing process is still
        # alive.  Absence of a durable terminal record is therefore incomplete,
        # never evidence of a currently running operation.
        outcome: object = (
            "incomplete" if events or trailing_line_incomplete else "unknown"
        )
        zero_verified = False
        disconnected = False
        manual_verification_required = True
    else:
        outcome = terminal.get("outcome", "unknown")
        zero_verified = terminal.get("zero_verified") is True
        disconnected = terminal.get("disconnected") is True
        manual_verification_required = (
            terminal.get("manual_verification_required") is not False
        )
        if "last_confirmed_state" in terminal:
            last_confirmed_state = terminal["last_confirmed_state"]

    return {
        "progress_jsonl": str(Path(path).resolve()),
        "event_count": len(events),
        "completed_points": completed_points,
        "current_point_index": current_point_index,
        "outcome": outcome,
        "zero_verified": zero_verified,
        "disconnected": disconnected,
        "manual_verification_required": manual_verification_required,
        "last_confirmed_state": last_confirmed_state,
        "trailing_line_incomplete": trailing_line_incomplete,
        "audit_complete": terminal is not None and not integrity_errors,
        "integrity_errors": integrity_errors,
    }


def _integrity_errors(
    events: list[dict[str, object]], trailing_line_incomplete: bool
) -> list[str]:
    errors: list[str] = []
    if trailing_line_incomplete:
        errors.append("trailing JSONL record is incomplete")
    if not events:
        return errors

    run_id = events[0].get("run_id")
    if not isinstance(run_id, str) or not run_id:
        errors.append("first record has no run_id")
    terminal_positions: list[int] = []
    run_started_positions: list[int] = []
    for position, event in enumerate(events):
        schema_version = event.get("schema_version")
        if (
            isinstance(schema_version, bool)
            or not isinstance(schema_version, int)
            or schema_version != SCHEMA_VERSION
        ):
            errors.append(f"record {position} has an unsupported schema_version")
        if event.get("run_id") != run_id:
            errors.append(f"record {position} has a mismatched run_id")
        event_index = event.get("event_index")
        if (
            isinstance(event_index, bool)
            or not isinstance(event_index, int)
            or event_index != position
        ):
            errors.append(f"record {position} has a non-contiguous event_index")
        captured = event.get("captured_unix_s")
        if (
            isinstance(captured, bool)
            or not isinstance(captured, (int, float))
            or not math.isfinite(captured)
        ):
            errors.append(f"record {position} has an invalid captured_unix_s")
        if event.get("event") == "run_started":
            run_started_positions.append(position)
        if event.get("event") == "run_finished":
            terminal_positions.append(position)

    if run_started_positions != [0]:
        errors.append("run_started must occur exactly once as the first record")
    if "segment_plan" in events[0]:
        errors.extend(_segment_plan_errors(events))
    if len(terminal_positions) > 1:
        errors.append("run_finished occurs more than once")
    if terminal_positions and terminal_positions[-1] != len(events) - 1:
        errors.append("run_finished is not the final durable record")
    if len(terminal_positions) == 1:
        terminal = events[terminal_positions[0]]
        started = events[0]
        command = started.get("command")
        normal_end_field_policy = started.get("normal_end_field_policy")
        if command not in {"single-target", "scan"}:
            errors.append("run_started has an invalid command")
        if normal_end_field_policy not in {"hold", "zero"}:
            errors.append("run_started has an invalid normal-end field policy")
        ordered_points = started.get("ordered_points")
        if not isinstance(ordered_points, list):
            errors.append("run_started ordered_points is not a list")
            ordered_points = []
        completed_records = [
            event for event in events if event.get("event") == "point_completed"
        ]
        for point_index, completed_record in enumerate(completed_records):
            recorded_index = completed_record.get("point_index")
            if (
                isinstance(recorded_index, bool)
                or not isinstance(recorded_index, int)
                or recorded_index != point_index
            ):
                errors.append("point_completed indices are not a contiguous prefix")
                break
            if (
                point_index >= len(ordered_points)
                or completed_record.get("requested_field")
                != ordered_points[point_index]
            ):
                errors.append(
                    "point_completed requested fields do not match ordered_points"
                )
                break
        outcome = terminal.get("outcome")
        completed = terminal.get("completed")
        if outcome not in {"completed", "rejected", "interrupted"}:
            errors.append("run_finished has an invalid outcome")
        if not isinstance(completed, bool) or completed != (outcome == "completed"):
            errors.append("run_finished completed flag disagrees with outcome")
        completed_points = terminal.get("completed_points")
        requested_points = terminal.get("requested_points")
        observed_points = sum(
            event.get("event") == "point_completed" for event in events
        )
        if (
            isinstance(completed_points, bool)
            or not isinstance(completed_points, int)
            or completed_points != observed_points
        ):
            errors.append("run_finished completed_points disagrees with the stream")
        if (
            isinstance(requested_points, bool)
            or not isinstance(requested_points, int)
            or requested_points < observed_points
        ):
            errors.append("run_finished requested_points is invalid")
        elif requested_points != len(ordered_points):
            errors.append("run_finished requested_points disagrees with ordered_points")
        if outcome == "completed" and completed_points != requested_points:
            errors.append("completed outcome does not include every requested point")
        for flag in (
            "zero_verified",
            "manual_verification_required",
            "disconnected",
            "audit_complete",
        ):
            if not isinstance(terminal.get(flag), bool):
                errors.append(f"run_finished {flag} is not boolean")
        if terminal.get("audit_complete") is not True:
            errors.append("run_finished does not confirm a complete audit")
        if outcome == "completed" and (
            terminal.get("disconnected") is not True
            or terminal.get("manual_verification_required") is not False
        ):
            errors.append("completed outcome lacks safe terminal flags")
        if outcome == "completed" and not isinstance(
            terminal.get("last_confirmed_state"), dict
        ):
            errors.append("completed outcome lacks a last confirmed state")
        zero_required = (
            command == "single-target" or normal_end_field_policy == "zero"
        )
        if (
            outcome == "completed"
            and zero_required
            and terminal.get("zero_verified") is not True
        ):
            errors.append("completed zero-required run lacks verified zero")
        errors.extend(_field_command_audit_errors(events, started, terminal))
    return errors


def _segment_plan_errors(events: list[dict[str, object]]) -> list[str]:
    started = events[0]
    archived = started["segment_plan"]
    if (
        not isinstance(archived, dict)
        or set(archived) != {"version", "axis", "segments", "point_segment_indices"}
        or type(archived.get("version")) is not int or archived["version"] != 1
        or started.get("field_limit_policy") != FIELD_LIMIT_POLICY
    ):
        return ["segment_plan declaration is invalid"]
    try:
        limits = MagnetLimits(**started["limits"])
        plan = expand_field_segments(archived["axis"], archived["segments"], limits)
    except (KeyError, TypeError, ValueError) as exc:
        return [f"segment_plan is invalid: {exc}"]
    indices = archived["point_segment_indices"]
    if (
        not isinstance(indices, list) or any(type(i) is not int for i in indices)
        or indices != list(plan.point_segment_indices)
        or started.get("ordered_points") != [asdict(point) for point in plan.points]
    ):
        return ["segment_plan expansion disagrees with archived ordered_points/indices"]
    errors: list[str] = []
    for event in events:
        if event.get("event") not in {"point_started", "point_completed"}:
            continue
        point_index = event.get("point_index")
        if type(point_index) is not int or not 0 <= point_index < len(plan.points):
            errors.append("segment_plan point index is invalid")
            continue
        segment_index = plan.point_segment_indices[point_index]
        if (
            type(event.get("segment_index")) is not int
            or event["segment_index"] != segment_index
            or event.get("sweep_direction") != plan.segments[segment_index].direction
        ):
            errors.append("segment_plan point metadata disagrees with its segment")
    return errors


def _field_command_audit_errors(
    events: list[dict[str, object]],
    started: dict[str, object],
    terminal: dict[str, object],
) -> list[str]:
    """Validate the opt-in M4 command transcript without changing v1 streams.

    The outer JSONL schema deliberately remains at version 1.  A producer opts
    into the stronger command-evidence contract by declaring it in ``run_started``;
    old M0--M2 files lack that declaration and must retain their original monitor
    semantics. The separate field_limit_policy declaration selects the new
    single-axis/dual-axis envelope without reinterpreting historical records.
    """

    errors: list[str] = []
    limits = None
    if "field_limit_policy" in started:
        if started["field_limit_policy"] != FIELD_LIMIT_POLICY:
            return ["run_started field_limit_policy is invalid"]
        recorded = started.get("limits")
        if (
            not isinstance(recorded, dict)
            or set(recorded) != {
                "hardware_x_max_t", "hardware_z_max_t", "experiment_vector_max_t"
            }
            or not all(_finite_number(value) for value in recorded.values())
        ):
            return ["run_started limits are invalid"]
        try:
            limits = MagnetLimits(**recorded)
        except ValueError as exc:
            return [f"run_started limits are invalid: {exc}"]
        if "field_command_audit" not in started:
            return ["field_limit_policy requires field_command_audit"]
        if terminal.get("outcome") == "completed":
            state = terminal.get("last_confirmed_state")
            for key in ("field", "field_setpoint"):
                _require_safe_field(
                    _finite_vector(
                        state.get(key) if isinstance(state, dict) else None,
                        f"run_finished {key}", errors,
                    ),
                    f"run_finished {key}", errors, limits,
                )
    if "field_command_audit" not in started:
        return errors

    declaration = started.get("field_command_audit")
    if declaration != _FIELD_COMMAND_AUDIT_DECLARATION:
        errors.append("run_started field_command_audit declaration is invalid")
        return errors

    transition_policy = started.get("transition_policy")
    if transition_policy not in _FIELD_TRANSITION_POLICIES:
        errors.append("run_started transition_policy is invalid")
        return errors

    planned_waypoints = _planned_waypoints(
        events,
        transition_policy,
        errors,
        limits,
    )
    executed_waypoints = _executed_waypoints(
        events,
        transition_policy,
        planned_waypoints,
        errors,
        limits,
    )
    if terminal.get("outcome") == "completed":
        for key in planned_waypoints:
            if key not in executed_waypoints:
                errors.append(
                    "point_started execution_plan waypoint has no "
                    "field_waypoint_execution record"
                )

    attempts: dict[int, tuple[int, dict[str, object]]] = {}
    results: dict[int, list[tuple[int, dict[str, object]]]] = {}
    attempt_count = 0
    result_count = 0
    expected_command_index = 0
    pending_command_index: int | None = None
    for position, event in enumerate(events):
        event_type = event.get("event")
        if event_type == "field_command_attempt":
            attempt_count += 1
            command_index = _nonnegative_int(
                event.get("command_index"),
                f"field_command_attempt at record {position} command_index",
                errors,
            )
            if command_index is None:
                continue
            if command_index != expected_command_index:
                errors.append("field command attempt indices are not contiguous")
            expected_command_index += 1
            if command_index in attempts:
                errors.append("field command attempt index occurs more than once")
                continue
            if pending_command_index is not None:
                errors.append(
                    "field command attempt occurs before the prior command result"
                )
            attempts[command_index] = (position, event)
            pending_command_index = command_index
            _validate_field_command_attempt(
                event,
                position,
                transition_policy,
                executed_waypoints,
                errors,
                limits,
            )
        elif event_type == "field_command_result":
            result_count += 1
            command_index = _nonnegative_int(
                event.get("command_index"),
                f"field_command_result at record {position} command_index",
                errors,
            )
            if command_index is None:
                continue
            results.setdefault(command_index, []).append((position, event))
            if pending_command_index != command_index:
                errors.append(
                    "field command result does not immediately resolve its attempt"
                )
            else:
                pending_command_index = None

    for command_index, (attempt_position, attempt) in attempts.items():
        matching_results = results.get(command_index, [])
        if not matching_results:
            errors.append("field command attempt has no result record")
            continue
        if len(matching_results) != 1:
            errors.append("field command attempt has more than one result record")
            continue
        result_position, result = matching_results[0]
        if result_position <= attempt_position:
            errors.append("field command result occurs before its attempt")
        _validate_field_command_result(
            attempt,
            result,
            command_index,
            errors,
        )
    for command_index in results:
        if command_index not in attempts:
            errors.append("field command result has no matching attempt record")

    _validate_terminal_command_counts(
        terminal,
        attempt_count,
        result_count,
        attempts,
        results,
        errors,
    )
    return errors


def _planned_waypoints(
    events: list[dict[str, object]],
    transition_policy: object,
    errors: list[str],
    limits: MagnetLimits | None,
) -> dict[tuple[int, int], dict[str, object]]:
    planned: dict[tuple[int, int], dict[str, object]] = {}
    seen_points: set[int] = set()
    for position, event in enumerate(events):
        if event.get("event") != "point_started":
            continue
        point_index = _nonnegative_int(
            event.get("point_index"),
            f"point_started at record {position} point_index",
            errors,
        )
        if point_index is None:
            continue
        if point_index in seen_points:
            errors.append("point_started occurs more than once for a point index")
            continue
        seen_points.add(point_index)
        if event.get("transition_policy") != transition_policy:
            errors.append("point_started transition_policy disagrees with run_started")
        execution_plan = event.get("execution_plan")
        if not isinstance(execution_plan, dict):
            errors.append("point_started lacks an execution_plan")
            continue
        if execution_plan.get("transition_policy") != transition_policy:
            errors.append("execution_plan transition_policy disagrees with run_started")
        requested = _finite_vector(
            event.get("requested_field"),
            "point_started requested_field",
            errors,
        )
        planned_target = _finite_vector(
            execution_plan.get("requested_target"),
            "execution_plan requested_target",
            errors,
        )
        if requested is not None and planned_target is not None and requested != planned_target:
            errors.append("execution_plan requested_target disagrees with point_started")
        start_command = _exact_float32_field(
            execution_plan.get("start_command"),
            "execution_plan start_command",
            errors,
        )
        target_command = _exact_float32_field(
            execution_plan.get("target_command"),
            "execution_plan target_command",
            errors,
        )
        if limits is not None:
            for value, label in (
                (requested, "requested_field"), (planned_target, "requested_target"),
                (start_command, "start_command"), (target_command, "target_command"),
            ):
                _require_safe_field(value, f"execution_plan {label}", errors, limits)
        if planned_target is not None and target_command is not None:
            _require_float32_matches_requested(
                target_command,
                planned_target,
                "execution_plan target_command",
                errors,
            )
        waypoints = execution_plan.get("waypoints")
        if not isinstance(waypoints, list):
            errors.append("execution_plan waypoints is not a list")
            continue
        for waypoint_index, waypoint in enumerate(waypoints):
            label = f"execution_plan waypoint {point_index}:{waypoint_index}"
            if not isinstance(waypoint, dict):
                errors.append(f"{label} is not an object")
                continue
            requested_field = _finite_vector(
                waypoint.get("requested_field"),
                f"{label} requested_field",
                errors,
            )
            command_field = _exact_float32_field(
                waypoint.get("command_field"),
                f"{label} command_field",
                errors,
            )
            x_then_z = _exact_float32_field(
                waypoint.get("x_then_z_mixed_corner"),
                f"{label} x_then_z_mixed_corner",
                errors,
            )
            z_then_x = _exact_float32_field(
                waypoint.get("z_then_x_mixed_corner"),
                f"{label} z_then_x_mixed_corner",
                errors,
            )
            if waypoint.get("axis_order") not in _FIELD_AXIS_ORDERS:
                errors.append(f"{label} has an invalid axis_order")
            if requested_field is not None and command_field is not None:
                _require_float32_matches_requested(
                    command_field,
                    requested_field,
                    f"{label} command_field",
                    errors,
                )
            _require_safe_field(command_field, f"{label} command_field", errors, limits)
            # Both candidates are recorded, but only the chosen corner is sent.
            # Historical records retain their original all-corners check.
            if limits is None or waypoint.get("axis_order") == "x_then_z":
                _require_safe_field(x_then_z, f"{label} x_then_z_mixed_corner", errors, limits)
            if limits is None or waypoint.get("axis_order") == "z_then_x":
                _require_safe_field(z_then_x, f"{label} z_then_x_mixed_corner", errors, limits)
            planned[(point_index, waypoint_index)] = waypoint
    return planned


def _executed_waypoints(
    events: list[dict[str, object]],
    transition_policy: object,
    planned: dict[tuple[int, int], dict[str, object]],
    errors: list[str],
    limits: MagnetLimits | None,
) -> dict[tuple[int, int], dict[str, object]]:
    executed: dict[tuple[int, int], dict[str, object]] = {}
    for position, event in enumerate(events):
        if event.get("event") != "field_waypoint_execution":
            continue
        point_index = _nonnegative_int(
            event.get("point_index"),
            f"field_waypoint_execution at record {position} point_index",
            errors,
        )
        waypoint_index = _nonnegative_int(
            event.get("waypoint_index"),
            f"field_waypoint_execution at record {position} waypoint_index",
            errors,
        )
        if point_index is None or waypoint_index is None:
            continue
        key = (point_index, waypoint_index)
        if key in executed:
            errors.append("field_waypoint_execution occurs more than once for a waypoint")
            continue
        executed[key] = event
        if event.get("transition_policy") != transition_policy:
            errors.append(
                "field_waypoint_execution transition_policy disagrees with run_started"
            )
        if not _nonempty_string(event.get("phase")):
            errors.append("field_waypoint_execution has an invalid phase")
        plan = planned.get(key)
        if plan is None:
            errors.append(
                "field_waypoint_execution has no matching execution_plan waypoint"
            )
        else:
            if event.get("planned_requested_field") != plan.get("requested_field"):
                errors.append(
                    "field_waypoint_execution requested field disagrees with execution plan"
                )
            planned_command = _exact_float32_field(
                plan.get("command_field"),
                "execution_plan waypoint command_field",
                errors,
            )
            actual_command = _exact_float32_field(
                event.get("command_field"),
                "field_waypoint_execution command_field",
                errors,
            )
            if (
                planned_command is not None
                and actual_command is not None
                and not _same_float32_field(planned_command, actual_command)
            ):
                errors.append(
                    "field_waypoint_execution command field disagrees with execution plan"
                )
        predecessor = _exact_float32_field(
            event.get("confirmed_predecessor"),
            "field_waypoint_execution confirmed_predecessor",
            errors,
        )
        command = _exact_float32_field(
            event.get("command_field"),
            "field_waypoint_execution command_field",
            errors,
        )
        x_then_z = _exact_float32_field(
            event.get("x_then_z_mixed_corner"),
            "field_waypoint_execution x_then_z_mixed_corner",
            errors,
        )
        z_then_x = _exact_float32_field(
            event.get("z_then_x_mixed_corner"),
            "field_waypoint_execution z_then_x_mixed_corner",
            errors,
        )
        axis_order = event.get("axis_order")
        if axis_order not in _FIELD_AXIS_ORDERS:
            errors.append("field_waypoint_execution has an invalid axis_order")
        _require_safe_field(command, "field_waypoint_execution command_field", errors, limits)
        if limits is not None:
            _require_safe_field(predecessor, "field_waypoint_execution predecessor", errors, limits)
        if limits is None or axis_order == "x_then_z":
            _require_safe_field(
                x_then_z, "field_waypoint_execution x_then_z_mixed_corner", errors, limits,
            )
        if limits is None or axis_order == "z_then_x":
            _require_safe_field(
                z_then_x, "field_waypoint_execution z_then_x_mixed_corner", errors, limits,
            )
        if (
            predecessor is not None
            and command is not None
            and x_then_z is not None
            and z_then_x is not None
        ):
            if not (
                _same_float32_number(x_then_z["bx_t"], command["bx_t"])
                and _same_float32_number(x_then_z["bz_t"], predecessor["bz_t"])
                and _same_float32_number(z_then_x["bx_t"], predecessor["bx_t"])
                and _same_float32_number(z_then_x["bz_t"], command["bz_t"])
            ):
                errors.append(
                    "field_waypoint_execution mixed corners do not match its "
                    "predecessor and command field"
                )
    return executed


def _validate_field_command_attempt(
    event: dict[str, object],
    position: int,
    transition_policy: object,
    executed_waypoints: dict[tuple[int, int], dict[str, object]],
    errors: list[str],
    limits: MagnetLimits | None,
) -> None:
    label = f"field_command_attempt at record {position}"
    command_kind = event.get("command_kind")
    if command_kind not in {
        "toggle_field_control",
        "set_field_component",
        "sweep_field_to_zero",
    }:
        errors.append(f"{label} has an invalid command_kind")
        return
    if event.get("transition_policy") != transition_policy:
        errors.append(f"{label} transition_policy disagrees with run_started")
    if not _nonempty_string(event.get("phase")):
        errors.append(f"{label} has an invalid phase")
    if command_kind == "set_field_component":
        _validate_component_attempt(
            event,
            label,
            executed_waypoints,
            errors,
            limits,
        )
        return
    expected_symbol = _FIELD_COMMAND_SYMBOLS[command_kind]
    if event.get("dll_symbol") != expected_symbol:
        errors.append(f"{label} has an invalid DLL symbol")
    if command_kind == "toggle_field_control" and not isinstance(
        event.get("requested_enabled"), bool
    ):
        errors.append(f"{label} lacks boolean requested_enabled")


def _validate_component_attempt(
    event: dict[str, object],
    label: str,
    executed_waypoints: dict[tuple[int, int], dict[str, object]],
    errors: list[str],
    limits: MagnetLimits | None,
) -> None:
    axis = event.get("axis")
    if axis not in _FIELD_COMPONENT_SYMBOLS:
        errors.append(f"{label} has an invalid component axis")
        return
    if event.get("dll_symbol") != _FIELD_COMPONENT_SYMBOLS[axis]:
        errors.append(f"{label} has an invalid DLL symbol")
    point_index = _nonnegative_int(event.get("point_index"), f"{label} point_index", errors)
    waypoint_index = _nonnegative_int(
        event.get("waypoint_index"),
        f"{label} waypoint_index",
        errors,
    )
    axis_order_index = _nonnegative_int(
        event.get("axis_order_index"),
        f"{label} axis_order_index",
        errors,
    )
    axis_order = event.get("axis_order")
    if axis_order not in _FIELD_AXIS_ORDERS:
        errors.append(f"{label} has an invalid axis_order")
    elif axis_order_index is not None:
        expected_axis = (
            ("x", "z") if axis_order == "x_then_z" else ("z", "x")
        )
        if axis_order_index not in {0, 1} or axis != expected_axis[axis_order_index]:
            errors.append(f"{label} axis_order and axis_order_index disagree")
    value = event.get("float32_value_t")
    bits = event.get("float32_ieee754_bits_hex")
    exact_value = _exact_float32_value(value, bits, f"{label} float32 command", errors)
    previous = _exact_float32_field(
        event.get("previous_setpoint"),
        f"{label} previous_setpoint",
        errors,
    )
    expected = _exact_float32_field(
        event.get("expected_setpoint"),
        f"{label} expected_setpoint",
        errors,
    )
    _require_safe_field(expected, f"{label} expected_setpoint", errors, limits)
    if exact_value is not None and previous is not None and expected is not None:
        if axis == "x":
            valid_expected = (
                _same_float32_number(expected["bx_t"], exact_value)
                and _same_float32_number(expected["bz_t"], previous["bz_t"])
            )
        else:
            valid_expected = (
                _same_float32_number(expected["bx_t"], previous["bx_t"])
                and _same_float32_number(expected["bz_t"], exact_value)
            )
        if not valid_expected:
            errors.append(
                f"{label} expected_setpoint does not match its component command"
            )
    if point_index is None or waypoint_index is None:
        return
    execution = executed_waypoints.get((point_index, waypoint_index))
    if execution is None:
        errors.append(f"{label} has no matching field_waypoint_execution")
        return
    if event.get("axis_order") != execution.get("axis_order"):
        errors.append(f"{label} axis_order disagrees with field_waypoint_execution")
    command = _exact_float32_field(
        execution.get("command_field"),
        "field_waypoint_execution command_field",
        errors,
    )
    if exact_value is not None and command is not None:
        expected_value = command["bx_t"] if axis == "x" else command["bz_t"]
        if not _same_float32_number(exact_value, expected_value):
            errors.append(
                f"{label} float32 command does not match field_waypoint_execution"
            )


def _validate_field_command_result(
    attempt: dict[str, object],
    result: dict[str, object],
    command_index: int,
    errors: list[str],
) -> None:
    label = f"field_command_result {command_index}"
    for key in (
        "command_kind",
        "dll_symbol",
        "transition_policy",
        "phase",
        "point_index",
        "waypoint_index",
        "axis_order",
        "axis_order_index",
        "axis",
        "requested_enabled",
        "requested_value_t",
        "float32_value_t",
        "float32_ieee754_bits_hex",
        "previous_setpoint",
        "expected_setpoint",
    ):
        if key in attempt and result.get(key) != attempt.get(key):
            errors.append(f"{label} does not preserve attempt {key}")
    success = result.get("success")
    if not isinstance(success, bool):
        errors.append(f"{label} success is not boolean")
        return
    acknowledgement = result.get("acknowledgement")
    return_code = result.get("dll_return_code")
    if success:
        if acknowledgement != "confirmed":
            errors.append(f"{label} successful command lacks confirmed acknowledgement")
        if isinstance(return_code, bool) or not isinstance(return_code, int) or return_code != 0:
            errors.append(f"{label} successful command lacks a zero DLL return code")
        if not isinstance(result.get("state"), dict):
            errors.append(f"{label} successful command lacks acknowledged state")
    else:
        if acknowledgement not in {"not_attempted", "failed"}:
            errors.append(f"{label} failed command has an invalid acknowledgement")
        if return_code is not None and (
            isinstance(return_code, bool) or not isinstance(return_code, int)
        ):
            errors.append(f"{label} failed command has an invalid DLL return code")
        if not _nonempty_string(result.get("error_type")):
            errors.append(f"{label} failed command lacks error_type")
        if not _nonempty_string(result.get("error")):
            errors.append(f"{label} failed command lacks error text")


def _validate_terminal_command_counts(
    terminal: dict[str, object],
    attempt_count: int,
    result_count: int,
    attempts: dict[int, tuple[int, dict[str, object]]],
    results: dict[int, list[tuple[int, dict[str, object]]]],
    errors: list[str],
) -> None:
    for key, observed in (
        ("field_command_attempt_count", attempt_count),
        ("field_command_result_count", result_count),
    ):
        value = terminal.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value != observed:
            errors.append(f"run_finished {key} disagrees with the stream")
    paired = (
        attempt_count == result_count
        and all(len(results.get(command_index, [])) == 1 for command_index in attempts)
        and all(command_index in attempts for command_index in results)
    )
    complete = terminal.get("field_command_audit_complete")
    if not isinstance(complete, bool) or complete != paired:
        errors.append("run_finished field_command_audit_complete disagrees with the stream")
    if complete is not True:
        errors.append("run_finished does not confirm a complete field command audit")


def _finite_vector(
    value: object,
    label: str,
    errors: list[str],
) -> dict[str, float] | None:
    if not isinstance(value, dict):
        errors.append(f"{label} is not an object")
        return None
    result: dict[str, float] = {}
    for component in ("bx_t", "bz_t"):
        numeric = value.get(component)
        if not _finite_number(numeric):
            errors.append(f"{label}.{component} is not finite")
            return None
        result[component] = float(numeric)
    return result


def _exact_float32_field(
    value: object,
    label: str,
    errors: list[str],
) -> dict[str, float] | None:
    if not isinstance(value, dict):
        errors.append(f"{label} is not an object")
        return None
    result: dict[str, float] = {}
    valid = True
    for component in ("bx_t", "bz_t"):
        numeric = _exact_float32_value(
            value.get(component),
            value.get(f"{component}_float32_bits_hex"),
            f"{label}.{component}",
            errors,
        )
        if numeric is None:
            valid = False
        else:
            result[component] = numeric
    return result if valid else None


def _exact_float32_value(
    numeric: object,
    bits: object,
    label: str,
    errors: list[str],
) -> float | None:
    if not _finite_number(numeric):
        errors.append(f"{label} is not a finite float32 value")
        return None
    if (
        not isinstance(bits, str)
        or len(bits) != 8
        or any(character not in "0123456789abcdef" for character in bits)
    ):
        errors.append(f"{label} has invalid IEEE-754 bits")
        return None
    decoded = struct.unpack("<f", bytes.fromhex(bits))[0]
    if not math.isfinite(decoded) or not _same_float32_number(float(numeric), decoded):
        errors.append(f"{label} is not an exact finite float32 value")
        return None
    return decoded


def _require_float32_matches_requested(
    command: dict[str, float],
    requested: dict[str, float],
    label: str,
    errors: list[str],
) -> None:
    for component in ("bx_t", "bz_t"):
        try:
            expected = struct.unpack(
                "<f",
                struct.pack("<f", requested[component]),
            )[0]
        except (OverflowError, struct.error):
            errors.append(f"{label} requested value cannot be represented as float32")
            return
        if not _same_float32_number(command[component], expected):
            errors.append(f"{label} does not match its requested float32 value")
            return


def _require_safe_field(
    field: dict[str, float] | None,
    label: str,
    errors: list[str],
    limits: MagnetLimits | None,
) -> None:
    if field is None:
        return
    if limits is not None:
        try:
            validate_vector_field(VectorField(field["bx_t"], field["bz_t"]), limits)
        except ValueError as exc:
            errors.append(f"{label} violates recorded field limits: {exc}")
    elif math.hypot(field["bx_t"], field["bz_t"]) > 3.0:
        errors.append(f"{label} exceeds the 3 T project vector limit")


def _same_float32_field(
    left: dict[str, float],
    right: dict[str, float],
) -> bool:
    return all(
        _same_float32_number(left[component], right[component])
        for component in ("bx_t", "bz_t")
    )


def _same_float32_number(left: float, right: float) -> bool:
    return left == right and (
        left != 0.0 or math.copysign(1.0, left) == math.copysign(1.0, right)
    )


def _finite_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _nonnegative_int(value: object, label: str, errors: list[str]) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        errors.append(f"{label} is not a non-negative integer")
        return None
    return value


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print(json.dumps(read_progress_snapshot(args.progress), ensure_ascii=False))
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    try:
        exit_code = run(argv)
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
