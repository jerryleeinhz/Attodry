"""Read-only terminal monitor for JSONL emitted by SR830 sweep processes."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .progress_monitor import JsonlProgressTail, resolve_progress_path


LOCKIN_PROGRESS_PATTERNS = (
    "*_lockin_*_progress.jsonl",
    "*_temperature_excitation_progress.jsonl",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Follow SR830 sweep progress JSONL using file reads only; this command "
            "never opens VISA, GPIB, or clears SR830 status latches."
        )
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--progress", type=Path, help="One progress JSONL file.")
    source.add_argument(
        "--directory",
        type=Path,
        help="Find and follow the newest lock-in progress JSONL below this directory.",
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


class LockinProgressView:
    """Condense the latest point/sample without changing any instrument state."""

    def __init__(self) -> None:
        self.scan: str | None = None
        self.phase = "waiting for events"
        self.outcome: str | None = None
        self.point: dict[str, object] = {}
        self.temperature_k: object | None = None
        self.requested_temperature_k: object | None = None
        self.sample: Mapping[str, Any] | None = None
        self.last_event = "none"

    def update(self, event: Mapping[str, Any]) -> None:
        event_name = event.get("event")
        if not isinstance(event_name, str):
            return
        self.last_event = event_name
        scan = event.get("scan")
        if isinstance(scan, str):
            self.scan = scan
        if "temperature_index" in event and "measurement_temperature_k" in event:
            self.temperature_k = event.get("measurement_temperature_k")
        if "requested_temperature_k" in event:
            self.requested_temperature_k = event.get("requested_temperature_k")
        point = event.get("sweep_point")
        if isinstance(point, Mapping):
            self.point = dict(point)
        else:
            self._update_flat_point(event)

        if event_name == "lockin_point_ready":
            self.phase = "point settled; waiting for formal sample"
            self.sample = None
        elif event_name == "lockin_formal_sample":
            self.phase = "formal sample"
            sample = event.get("sample")
            if isinstance(sample, Mapping):
                self.sample = sample
                sample_point = sample.get("sweep_point")
                if isinstance(sample_point, Mapping):
                    self.point = dict(sample_point)
                temperature = sample.get("measurement_temperature_k")
                if temperature is not None:
                    self.temperature_k = temperature
        elif event_name == "lockin_point_completed":
            self.phase = "point completed"
        elif event_name == "excitation_finished":
            self.phase = "lockin excitation finished"
            self.outcome = _optional_text(event.get("outcome"))
        elif event_name == "scan_finished":
            self.phase = "scan finished"
            self.outcome = _optional_text(event.get("outcome"))

    def _update_flat_point(self, event: Mapping[str, Any]) -> None:
        fields = (
            "point_index",
            "points_total",
            "frequency_index",
            "frequency_points_total",
            "excitation_index",
            "excitation_points_total",
            "target_frequency_hz",
            "actual_frequency_hz",
            "frequency_readback_hz",
            "source_v_rms",
            "source_readback_v_rms",
            "requested_nominal_current_a_rms",
            "nominal_current_a_rms",
        )
        updates = {field: event[field] for field in fields if field in event}
        if updates:
            self.point.update(updates)

    def format(self) -> str:
        parts = ["LOCKIN", self.phase]
        if self.scan:
            parts.append(f"scan={self.scan}")
        point_index = self.point.get("point_index")
        points_total = self.point.get("points_total")
        if point_index is not None:
            display_index = _display_index(point_index)
            parts.append(f"point={display_index}/{points_total if points_total is not None else '?'}")
        if self.requested_temperature_k is not None:
            parts.append(f"T-requested={_format_voltage(self.requested_temperature_k, ' K')}")
        if self.temperature_k is not None:
            parts.append(f"T-measured={_format_voltage(self.temperature_k, ' K')}")
        parts.extend(
            (
                f"SINE target={_format_voltage(self.point.get('source_v_rms'), ' V')}",
                f"readback={_format_voltage(self.point.get('source_readback_v_rms'), ' V')}",
                f"I-nominal={_format_current(self.point.get('nominal_current_a_rms'))}",
                f"frequency={_format_voltage(self.point.get('actual_frequency_hz'), ' Hz')}",
            )
        )
        if self.sample is not None:
            harmonic = self.sample.get("harmonic")
            sample_index = self.sample.get("sample_index")
            parts.append(f"h={harmonic if harmonic is not None else '?'} sample={_display_index(sample_index)}")
            parts.append(_format_role("Vxx", self.sample.get("lockin_xx")))
            parts.append(_format_role("Vxy", self.sample.get("lockin_xy")))
            problems = self.sample.get("problems")
            if isinstance(problems, list) and problems:
                parts.append("problems=" + "; ".join(str(item) for item in problems))
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
        patterns=LOCKIN_PROGRESS_PATTERNS,
    )
    tail = JsonlProgressTail(path)
    view = LockinProgressView()
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
        print("Stopped file-only lock-in monitor.", file=sys.stderr)
        raise SystemExit(130) from None
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


def _format_role(label: str, audited_sample: object) -> str:
    if not isinstance(audited_sample, Mapping):
        return f"{label}=--"
    reading = audited_sample.get("reading")
    if not isinstance(reading, Mapping):
        return f"{label}=--"
    status = "locked" if reading.get("locked") is True else "UNLOCKED"
    if reading.get("overload") is True:
        status += ",OVERLOAD"
    return (
        f"{label}: R={_format_voltage(reading.get('amplitude_v'), ' V')} "
        f"phase={_format_voltage(reading.get('phase_deg'), ' deg')} [{status}]"
    )


def _display_index(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, int):
        return "?"
    return str(value + 1)


def _format_voltage(value: object, suffix: str) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "--"
    return f"{float(value):.6g}{suffix}"


def _format_current(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "--"
    current = abs(float(value))
    if current < 1e-6:
        return f"{float(value) * 1e9:.6g} nA"
    if current < 1e-3:
        return f"{float(value) * 1e6:.6g} uA"
    return f"{float(value):.6g} A"


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


if __name__ == "__main__":
    main()
