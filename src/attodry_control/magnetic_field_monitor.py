from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
from typing import Sequence

from .field_audit import SCHEMA_VERSION, read_jsonl_events


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
    return errors


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
