"""Read-only terminal monitor for temperature and temperature--excitation JSONL."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .progress_monitor import JsonlProgressTail, resolve_progress_path


TEMPERATURE_PROGRESS_PATTERNS = (
    "*_temperature_scan_progress.jsonl",
    "*_temperature_excitation_progress.jsonl",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Follow a temperature progress JSONL using file reads only; this command "
            "never opens the attoDRY DLL or COM port."
        )
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--progress", type=Path, help="One progress JSONL file.")
    source.add_argument(
        "--directory",
        type=Path,
        help="Find and follow the newest temperature progress JSONL below this directory.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Print the current snapshot and exit instead of following new events.",
    )
    parser.add_argument(
        "--poll-interval-s",
        type=float,
        default=1.0,
        help="File polling interval in seconds (default: 1.0).",
    )
    return parser


class TemperatureProgressView:
    """Condense the newest temperature event into one terminal-readable state."""

    def __init__(self) -> None:
        self.command: str | None = None
        self.phase = "waiting for events"
        self.outcome: str | None = None
        self.temperature_index: object | None = None
        self.requested_temperature_k: object | None = None
        self.actual_setpoint_k: object | None = None
        self.sample_temperature_k: object | None = None
        self.vti_temperature_k: object | None = None
        self.control_enabled: object | None = None
        self.error_code: object | None = None
        self.measurement_temperature_k: object | None = None
        self.last_event = "none"

    def update(self, event: Mapping[str, Any]) -> None:
        event_name = event.get("event")
        if not isinstance(event_name, str):
            return
        self.last_event = event_name
        command = event.get("command")
        if isinstance(command, str):
            self.command = command
        if "temperature_index" in event:
            self.temperature_index = event["temperature_index"]
        if "point_index" in event and event_name in {
            "temperature_sample",
            "point_preflight",
            "setpoint_confirmed",
            "point_completed",
        }:
            self.temperature_index = event["point_index"]
        if "requested_temperature_k" in event:
            self.requested_temperature_k = event["requested_temperature_k"]
        if "actual_setpoint_k" in event:
            self.actual_setpoint_k = event["actual_setpoint_k"]
        state = event.get("state")
        if isinstance(state, Mapping):
            self._update_state(state)

        if event_name == "temperature_sample":
            self.phase = str(event.get("phase", "stability"))
        elif event_name == "measurement_temperature_sample":
            self.phase = "lockin measurement"
        elif event_name == "temperature_stable":
            self.phase = "temperature stable; starting lock-in sweep"
            stability = event.get("stability")
            if isinstance(stability, Mapping):
                self.measurement_temperature_k = stability.get(
                    "measurement_temperature_k"
                )
        elif event_name == "excitation_started":
            self.phase = "lockin excitation sweep"
        elif event_name == "excitation_finished":
            self.phase = "lockin excitation finished"
            self.outcome = _optional_text(event.get("outcome"))
        elif event_name in {"temperature_condition_completed", "point_completed"}:
            self.phase = "temperature condition completed"
        elif event_name == "scan_finished":
            self.phase = "scan finished"
            self.outcome = _optional_text(event.get("outcome"))

    def _update_state(self, state: Mapping[str, Any]) -> None:
        for attribute, field in (
            ("sample_temperature_k", "sample_temperature_k"),
            ("actual_setpoint_k", "user_temperature_k"),
            ("vti_temperature_k", "vti_temperature_k"),
            ("control_enabled", "temperature_control_enabled"),
            ("error_code", "error_code"),
        ):
            if field in state:
                setattr(self, attribute, state[field])

    def format(self) -> str:
        parts = ["TEMPERATURE", self.phase]
        if self.command:
            parts.append(f"command={self.command}")
        if self.temperature_index is not None:
            parts.append(f"point={_display_index(self.temperature_index)}")
        parts.extend(
            (
                f"target={_format_k(self.requested_temperature_k)}",
                f"setpoint={_format_k(self.actual_setpoint_k)}",
                f"sample={_format_k(self.sample_temperature_k)}",
                f"vti={_format_k(self.vti_temperature_k)}",
            )
        )
        if self.measurement_temperature_k is not None:
            parts.append(f"measurement-mean={_format_k(self.measurement_temperature_k)}")
        if self.control_enabled is not None:
            parts.append(f"control={self.control_enabled}")
        if self.error_code is not None:
            parts.append(f"error_code={self.error_code}")
        if self.outcome:
            parts.append(f"outcome={self.outcome}")
        parts.append(f"event={self.last_event}")
        return " | ".join(parts)


def run(
    argv: Sequence[str] | None = None,
    *,
    sleep: Callable[[float], None] = time.sleep,
    output: Callable[[str], None] = print,
) -> int:
    args = build_parser().parse_args(argv)
    if args.poll_interval_s <= 0.0:
        raise ValueError("--poll-interval-s must be positive.")
    path = resolve_progress_path(
        progress=args.progress,
        directory=args.directory,
        patterns=TEMPERATURE_PROGRESS_PATTERNS,
    )
    tail = JsonlProgressTail(path)
    view = TemperatureProgressView()
    for event in tail.read_new_events():
        view.update(event)
    output(f"Monitoring file only: {path}")
    output(view.format())
    if args.once:
        return 0
    while True:
        sleep(args.poll_interval_s)
        for event in tail.read_new_events():
            view.update(event)
            output(view.format())


def main(argv: Sequence[str] | None = None) -> None:
    try:
        raise SystemExit(run(argv))
    except KeyboardInterrupt:
        print("Stopped file-only temperature monitor.", file=sys.stderr)
        raise SystemExit(130) from None
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


def _format_k(value: object) -> str:
    return _format_number(value, suffix=" K")


def _format_number(value: object, *, suffix: str = "") -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "--"
    return f"{float(value):.6g}{suffix}"


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _display_index(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, int):
        return str(value)
    return str(value + 1)


if __name__ == "__main__":
    main()
