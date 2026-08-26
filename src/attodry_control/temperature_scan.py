from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Callable, Sequence

from .attodry import (
    AttoDryAuthorizationError,
    AttoDryDriver,
    AttoDryError,
    load_attodry_dll,
)
from .config import (
    TemperatureOperationConfig,
    TemperatureRunConfig,
    TemperatureStabilityMode,
    load_temperature_operation_config,
)
from .models import CryostatState
from .scans import temperature_scan_points
from .temperature_run import (
    _close_with_note,
    _handle_operator_interrupt,
    _record_failure_diagnostic,
)


DEFAULT_CONFIG_PATH = Path("config/hardware.local.toml")
SCHEMA_VERSION = 1


class _JsonlWriter:
    def __init__(self, path: Path, *, create: bool) -> None:
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.open("x", encoding="utf-8").close()
        self.path = path

    def append(self, event: dict[str, object]) -> None:
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the configured ascending attoDRY temperature-stability scan. "
            "This is a commissioning write path and remains explicitly gated."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Unified hardware TOML (default: {DEFAULT_CONFIG_PATH}).",
    )
    parser.add_argument(
        "--authorize-temperature-scan",
        action="store_true",
        help="Authorize connection, temperature-control enable, and scan setpoints.",
    )
    parser.add_argument(
        "--resume-progress",
        type=Path,
        help=(
            "Resume the first incomplete point from an existing progress JSONL. "
            "The current TOML must match its archived scan contract."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    try:
        exit_code = run(argv)
    except KeyboardInterrupt:
        print(
            "Interrupted. Temperature-control shutdown and close were attempted; "
            "manually verify the attoDRY temperature state.",
            file=sys.stderr,
        )
        raise SystemExit(130) from None
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from None
    raise SystemExit(exit_code)


def run(
    argv: Sequence[str] | None = None,
    *,
    dll_loader: Callable[[str | Path], object] = load_attodry_dll,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    wall_time: Callable[[], float] = time.time,
    confirmation: Callable[[str], str] | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    config_path = args.config.resolve()
    config = load_temperature_operation_config(config_path)
    scan = config.temperature_scan
    if scan is None:
        raise ValueError("hardware TOML is missing the [temperature_scan] table.")
    points_k = temperature_scan_points(scan.start_k, scan.stop_k, scan.step_k)
    _validate_static_path(points_k, config.temperature_run)
    if not args.authorize_temperature_scan:
        raise AttoDryAuthorizationError(
            "Temperature scan is not authorized. Re-run only after the operator "
            "approves this real multi-point write stage and include "
            "--authorize-temperature-scan."
        )

    cryostat = config.cryostat
    if (
        cryostat.com_port is None
        or cryostat.dll_path is None
        or cryostat.device_type is None
        or cryostat.connection_timeout_s is None
    ):
        raise ValueError("Hardware cryostat DLL path is missing.")

    output_directory = (config_path.parent / scan.output_directory).resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    measurement_config = _measurement_config(config, points_k)
    if args.resume_progress is None:
        progress_path = output_directory / (
            f"{_utc_label(wall_time())}_{scan.run_name}_temperature_scan_progress.jsonl"
        )
        summary_path, csv_path = _result_paths(progress_path)
        writer = _JsonlWriter(progress_path, create=True)
        completed_points: list[dict[str, object]] = []
        writer.append(
            {
                "event": "scan_started",
                "schema_version": SCHEMA_VERSION,
                "captured_unix_s": wall_time(),
                "command": "temperature-scan",
                "run_metadata": {
                    "run_name": scan.run_name,
                    "note": scan.note,
                    "git_commit": _git_commit(config_path.parent.parent),
                },
                "config_path": str(config_path),
                "measurement_config": measurement_config,
            }
        )
    else:
        progress_path = args.resume_progress.resolve()
        if progress_path.parent != output_directory:
            raise ValueError(
                "--resume-progress must be inside temperature_scan.output_directory."
            )
        summary_path, csv_path = _result_paths(progress_path)
        completed_points = _load_resume_progress(
            progress_path, measurement_config, points_k
        )
        writer = _JsonlWriter(progress_path, create=False)
        writer.append(
            {
                "event": "scan_resumed",
                "captured_unix_s": wall_time(),
                "resume_from_point_index": len(completed_points),
                "git_commit": _git_commit(config_path.parent.parent),
            }
        )

    summary: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "command": "temperature-scan",
        "completed": False,
        "outcome": "running",
        "captured_unix_s": wall_time(),
        "run_metadata": {
            "run_name": scan.run_name,
            "note": scan.note,
            "git_commit": _git_commit(config_path.parent.parent),
        },
        "config_path": str(config_path),
        "measurement_config": measurement_config,
        "progress_jsonl": str(progress_path),
        "stable_times_csv": str(csv_path),
        "points": completed_points,
        "interruptions": [],
        "recovery_actions": [],
    }
    driver = AttoDryDriver(
        dll=dll_loader(cryostat.dll_path),
        com_port=cryostat.com_port,
        device_type=cryostat.device_type,
        connection_timeout_s=cryostat.connection_timeout_s,
        temperature_min_k=cryostat.temperature_min_k,
        temperature_max_k=cryostat.temperature_max_k,
        limits=config.magnet.limits,
        field_stability=config.magnet.stability,
        temperature_stability=config.temperature_stability,
        connection_authorized=True,
        writes_authorized=True,
    )
    connected = False
    mutation_attempted = False

    try:
        driver.connect(monotonic=monotonic, sleeper=sleeper)
        connected = True
        initial_state = driver.read_state()
        summary["preflight"] = {"initial_state": asdict(initial_state)}
        if initial_state.error_code:
            raise AttoDryError(
                f"attoDRY preflight error code {initial_state.error_code}."
            )

        for point_index in range(len(completed_points), len(points_k)):
            target_k = points_k[point_index]
            before_state = driver.read_state()
            sample_target_delta_k = abs(
                target_k - before_state.sample_temperature_k
            )
            prewrite = {
                "initial_sample_temperature_k": before_state.sample_temperature_k,
                "sample_target_delta_k": sample_target_delta_k,
                "max_delta_k": config.temperature_run.max_delta_k,
                "passed": sample_target_delta_k <= config.temperature_run.max_delta_k,
            }
            writer.append(
                {
                    "event": "point_preflight",
                    "captured_unix_s": wall_time(),
                    "point_index": point_index,
                    "requested_temperature_k": target_k,
                    "state": asdict(before_state),
                    "prewrite_check": prewrite,
                }
            )
            if before_state.error_code:
                raise AttoDryError(
                    f"attoDRY error code {before_state.error_code} before point "
                    f"{point_index}."
                )
            if sample_target_delta_k > config.temperature_run.max_delta_k:
                raise ValueError(
                    "Requested sample-temperature movement exceeds "
                    "temperature_run.max_delta_k."
                )

            mutation_attempted = True
            driver.ensure_temperature_control(
                True, monotonic=monotonic, sleeper=sleeper
            )
            force_reapply = not before_state.temperature_control_enabled
            driver.set_temperature(
                target_k,
                force_write=force_reapply,
                monotonic=monotonic,
                sleeper=sleeper,
            )
            confirmed_state = driver.read_state()
            _validate_point_state(
                confirmed_state, target_k, config.temperature_run
            )
            writer.append(
                {
                    "event": "setpoint_confirmed",
                    "captured_unix_s": wall_time(),
                    "point_index": point_index,
                    "requested_temperature_k": target_k,
                    "actual_setpoint_k": confirmed_state.user_temperature_k,
                    "force_reapply": force_reapply,
                    "state": asdict(confirmed_state),
                }
            )

            point_summary = _wait_for_stable_point(
                driver,
                replace(config.temperature_run, target_k=target_k),
                point_index=point_index,
                writer=writer,
                interruptions=summary["interruptions"],
                response_reference_k=before_state.sample_temperature_k,
                monotonic=monotonic,
                sleeper=sleeper,
                wall_time=wall_time,
                confirmation=confirmation,
            )
            completed_points.append(point_summary)
            writer.append(
                {
                    "event": "point_completed",
                    "captured_unix_s": wall_time(),
                    "point_index": point_index,
                    "summary": point_summary,
                }
            )

        final_state = driver.read_state()
        _validate_point_state(final_state, points_k[-1], config.temperature_run)
        summary["final_state"] = asdict(final_state)
        driver.close()
        connected = False
        summary["disconnected"] = True
        summary["completed"] = True
        summary["outcome"] = "completed"
        writer.append(
            {
                "event": "scan_finished",
                "captured_unix_s": wall_time(),
                "completed": True,
                "outcome": "completed",
                "final_state": summary["final_state"],
            }
        )
    except BaseException as exc:
        summary["error"] = str(exc)
        summary["outcome"] = (
            "interrupted" if isinstance(exc, KeyboardInterrupt) else "rejected"
        )
        if connected:
            _record_failure_diagnostic(summary, driver, wall_time=wall_time)
        if connected and mutation_attempted:
            try:
                driver.ensure_temperature_control(
                    False, monotonic=monotonic, sleeper=sleeper
                )
                summary["recovery_actions"].append(
                    "temperature_control_disabled_after_failure"
                )
            except BaseException as recovery_error:
                summary["recovery_error"] = str(recovery_error)
                exc.add_note(f"Temperature recovery also failed: {recovery_error}")
        if connected:
            try:
                summary["final_state"] = asdict(driver.read_state())
            except BaseException as read_error:
                summary["final_read_error"] = str(read_error)
                summary["last_confirmed_state"] = (
                    None
                    if driver.last_confirmed_state is None
                    else asdict(driver.last_confirmed_state)
                )
        summary["disconnected"] = _close_with_note(
            driver, connected=connected, primary_error=exc
        )
        try:
            writer.append(
                {
                    "event": "scan_finished",
                    "captured_unix_s": wall_time(),
                    "completed": False,
                    "outcome": summary["outcome"],
                    "error": summary["error"],
                    "last_confirmed_state": summary.get(
                        "final_state", summary.get("last_confirmed_state")
                    ),
                }
            )
        except BaseException as progress_error:
            summary["progress_write_error"] = str(progress_error)
            exc.add_note(f"Progress audit write also failed: {progress_error}")
        try:
            _write_results(summary_path, csv_path, summary)
        except BaseException as result_error:
            summary["result_write_error"] = str(result_error)
            exc.add_note(f"Final result write also failed: {result_error}")
        print(json.dumps(summary, indent=2, ensure_ascii=False), flush=True)
        raise

    _write_results(summary_path, csv_path, summary)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


def _validate_static_path(
    points_k: tuple[float, ...], request: TemperatureRunConfig
) -> None:
    for current, following in zip(points_k, points_k[1:]):
        if following - current > request.max_delta_k:
            raise ValueError(
                "A temperature_scan step exceeds temperature_run.max_delta_k."
            )


def _validate_point_state(
    state: CryostatState, target_k: float, request: TemperatureRunConfig
) -> None:
    if state.error_code:
        raise AttoDryError(
            f"attoDRY error code {state.error_code} during temperature scan."
        )
    if not state.temperature_control_enabled:
        raise AttoDryError("Temperature control became disabled during temperature scan.")
    if not math.isclose(state.user_temperature_k, target_k, abs_tol=1e-4):
        raise AttoDryError("Temperature setpoint changed during temperature scan.")
    overshoot_limit_k = target_k + request.max_overshoot_k
    if state.sample_temperature_k >= overshoot_limit_k:
        raise AttoDryError(
            "Sample temperature reached the configured overshoot limit: "
            f"{state.sample_temperature_k:g} K >= {overshoot_limit_k:g} K."
        )


def _wait_for_stable_point(
    driver: AttoDryDriver,
    request: TemperatureRunConfig,
    *,
    point_index: int,
    writer: _JsonlWriter,
    interruptions: object,
    response_reference_k: float,
    monotonic: Callable[[], float],
    sleeper: Callable[[float], None],
    wall_time: Callable[[], float],
    confirmation: Callable[[str], str] | None,
) -> dict[str, object]:
    if not isinstance(interruptions, list):
        raise TypeError("Temperature interruption container must be a list.")
    point_started = monotonic()
    first_tolerance_s: float | None = None
    automatic_recoveries = 0
    attempt_index = 0
    needs_recheck = False
    stability = driver.temperature_stability
    stable_readback = (
        stability.acceptance_mode is TemperatureStabilityMode.STABLE_READBACK
    )
    minimum_response_k = (
        stability.min_response_k if point_index > 0 and stable_readback else 0.0
    )
    first_response_s: float | None = None

    while True:
        attempt_samples: list[tuple[float, float]] = []
        attempt_offset_s = monotonic() - point_started

        def on_sample(state: CryostatState, elapsed_s: float) -> None:
            nonlocal first_response_s, first_tolerance_s
            writer.append(
                {
                    "event": "temperature_sample",
                    "captured_unix_s": wall_time(),
                    "point_index": point_index,
                    "attempt_index": attempt_index,
                    "phase": "stability",
                    "requested_temperature_k": request.target_k,
                    "elapsed_s": elapsed_s,
                    "state": asdict(state),
                }
            )
            _validate_point_state(state, request.target_k, request)
            attempt_samples.append((elapsed_s, state.sample_temperature_k))
            if (
                first_response_s is None
                and minimum_response_k > 0
                and abs(state.sample_temperature_k - response_reference_k)
                >= minimum_response_k
            ):
                first_response_s = attempt_offset_s + elapsed_s
            if (
                first_tolerance_s is None
                and not stable_readback
                and stability.criteria.tolerance is not None
                and abs(state.sample_temperature_k - request.target_k)
                <= stability.criteria.tolerance
            ):
                first_tolerance_s = attempt_offset_s + elapsed_s

        try:
            if needs_recheck:
                _wait_resume_recheck(
                    driver,
                    request,
                    point_index=point_index,
                    attempt_index=attempt_index,
                    writer=writer,
                    monotonic=monotonic,
                    sleeper=sleeper,
                    wall_time=wall_time,
                )
                needs_recheck = False
                attempt_offset_s = monotonic() - point_started
            final_state = driver.wait_for_temperature(
                request.target_k,
                max_overshoot_k=request.max_overshoot_k,
                minimum_response_k=minimum_response_k,
                response_reference_k=response_reference_k,
                monotonic=monotonic,
                sleeper=sleeper,
                on_sample=on_sample,
            )
        except KeyboardInterrupt:
            before_count = len(interruptions)
            try:
                _handle_operator_interrupt(
                    driver,
                    request,
                    interruptions=interruptions,
                    monotonic=monotonic,
                    wall_time=wall_time,
                    confirmation=confirmation,
                    automatic_recoveries=automatic_recoveries,
                )
            finally:
                for item in interruptions[before_count:]:
                    writer.append(
                        {
                            "event": "operator_interruption",
                            "point_index": point_index,
                            "attempt_index": attempt_index,
                            "details": item,
                        }
                    )
            automatic_recoveries += 1
            attempt_index += 1
            needs_recheck = True
            continue

        stable_elapsed_s = attempt_samples[-1][0]
        cutoff_s = stable_elapsed_s - driver.temperature_stability.criteria.dwell_s
        stable_values = [
            value for elapsed, value in attempt_samples if elapsed >= cutoff_s
        ]
        measurement_temperature_k = sum(stable_values) / len(stable_values)
        stable_mean_k = measurement_temperature_k
        stable_std_k = math.sqrt(
            sum((value - stable_mean_k) ** 2 for value in stable_values)
            / len(stable_values)
        )
        return {
            "point_index": point_index,
            "requested_temperature_k": request.target_k,
            "actual_setpoint_k": final_state.user_temperature_k,
            "actual_sample_temperature_k": final_state.sample_temperature_k,
            "measurement_temperature_k": measurement_temperature_k,
            "time_to_response_s": first_response_s,
            "time_to_first_tolerance_s": first_tolerance_s,
            "time_to_stable_s": attempt_offset_s + stable_elapsed_s,
            "successful_attempt_index": attempt_index,
            "stable_window": {
                "sample_count": len(stable_values),
                "minimum_k": min(stable_values),
                "maximum_k": max(stable_values),
                "peak_to_peak_k": max(stable_values) - min(stable_values),
                "mean_k": stable_mean_k,
                "standard_deviation_k": stable_std_k,
                "required_dwell_s": driver.temperature_stability.criteria.dwell_s,
            },
            "final_state": asdict(final_state),
        }


def _wait_resume_recheck(
    driver: AttoDryDriver,
    request: TemperatureRunConfig,
    *,
    point_index: int,
    attempt_index: int,
    writer: _JsonlWriter,
    monotonic: Callable[[], float],
    sleeper: Callable[[float], None],
    wall_time: Callable[[], float],
) -> None:
    started = monotonic()
    while True:
        state = driver.read_state()
        elapsed_s = monotonic() - started
        writer.append(
            {
                "event": "temperature_sample",
                "captured_unix_s": wall_time(),
                "point_index": point_index,
                "attempt_index": attempt_index,
                "phase": "resume_recheck",
                "requested_temperature_k": request.target_k,
                "elapsed_s": elapsed_s,
                "state": asdict(state),
            }
        )
        _validate_point_state(state, request.target_k, request)
        if elapsed_s >= request.resume_recheck_s:
            return
        sleeper(min(request.poll_interval_s, request.resume_recheck_s - elapsed_s))


def _measurement_config(
    config: TemperatureOperationConfig, points_k: tuple[float, ...]
) -> dict[str, object]:
    scan = config.temperature_scan
    if scan is None:
        raise ValueError("Temperature scan configuration is missing.")
    stability = config.temperature_stability
    run = config.temperature_run
    cryostat = config.cryostat
    return {
        "cryostat": {
            "backend": cryostat.backend,
            "com_port": cryostat.com_port,
            "dll_path": None if cryostat.dll_path is None else str(cryostat.dll_path),
            "device_type": cryostat.device_type,
            "connection_timeout_s": cryostat.connection_timeout_s,
            "temperature_min_k": cryostat.temperature_min_k,
            "temperature_max_k": cryostat.temperature_max_k,
        },
        "temperature_stability": {
            "acceptance_mode": stability.acceptance_mode.value,
            "tolerance_k": stability.criteria.tolerance,
            "stable_range_k": stability.criteria.stable_range,
            "stable_dwell_s": stability.criteria.dwell_s,
            "minimum_samples": stability.criteria.minimum_samples,
            "min_response_k": stability.min_response_k,
            "poll_interval_s": stability.poll_interval_s,
            "wait_timeout_s": stability.wait_timeout_s,
        },
        "temperature_run_safety": {
            "max_delta_k": run.max_delta_k,
            "max_overshoot_k": run.max_overshoot_k,
            "interrupt_policy": run.interrupt_policy.value,
            "resume_recheck_s": run.resume_recheck_s,
        },
        "temperature_scan": {
            "start_k": scan.start_k,
            "stop_k": scan.stop_k,
            "step_k": scan.step_k,
            "points_k": list(points_k),
            "run_name": scan.run_name,
            "note": scan.note,
            "output_directory": str(scan.output_directory),
            "normal_end_state": "hold-final-target-control-enabled",
            "failure_state": "disable-temperature-control-hold-setpoint",
        },
    }


def _load_resume_progress(
    path: Path,
    measurement_config: dict[str, object],
    points_k: tuple[float, ...],
) -> list[dict[str, object]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"Could not read progress JSONL {path}: {exc}") from exc
    if not lines:
        raise ValueError("Progress JSONL is empty.")
    events: list[dict[str, object]] = []
    for line_number, line in enumerate(lines, start=1):
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Progress JSONL is malformed at line {line_number}."
            ) from exc
        if not isinstance(event, dict):
            raise ValueError(
                f"Progress JSONL line {line_number} is not an object."
            )
        events.append(event)
    if not events or events[0].get("event") != "scan_started":
        raise ValueError("Progress JSONL does not start with scan_started.")
    if events[0].get("measurement_config") != measurement_config:
        raise ValueError("Current TOML does not match the archived scan contract.")
    if any(
        event.get("event") == "scan_finished" and event.get("completed") is True
        for event in events
    ):
        raise ValueError("The selected progress JSONL already completed.")

    completed: list[dict[str, object]] = []
    for event in events:
        if event.get("event") != "point_completed":
            continue
        summary = event.get("summary")
        if not isinstance(summary, dict):
            raise ValueError("A point_completed event has no summary object.")
        expected_index = len(completed)
        if summary.get("point_index") != expected_index:
            raise ValueError("Completed temperature points are not contiguous.")
        requested = summary.get("requested_temperature_k")
        if not isinstance(requested, (int, float)) or not math.isclose(
            float(requested), points_k[expected_index], abs_tol=1e-9
        ):
            raise ValueError("Completed temperature point does not match the scan grid.")
        completed.append(summary)
    if len(completed) >= len(points_k):
        raise ValueError("All configured temperature points are already complete.")
    return completed


def _write_results(
    summary_path: Path, csv_path: Path, summary: dict[str, object]
) -> None:
    temporary = summary_path.with_suffix(summary_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    temporary.replace(summary_path)
    points = summary.get("points")
    if not isinstance(points, list):
        raise TypeError("Temperature point summary container must be a list.")
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=(
                "point_index",
                "requested_temperature_k",
                "actual_setpoint_k",
                "actual_sample_temperature_k",
                "measurement_temperature_k",
                "time_to_response_s",
                "time_to_first_tolerance_s",
                "time_to_stable_s",
                "stable_minimum_k",
                "stable_maximum_k",
                "stable_peak_to_peak_k",
                "stable_mean_k",
                "stable_standard_deviation_k",
                "stable_sample_count",
            ),
        )
        writer.writeheader()
        for point in points:
            if not isinstance(point, dict):
                continue
            window = point.get("stable_window")
            window = window if isinstance(window, dict) else {}
            writer.writerow(
                {
                    "point_index": point.get("point_index"),
                    "requested_temperature_k": point.get(
                        "requested_temperature_k"
                    ),
                    "actual_setpoint_k": point.get("actual_setpoint_k"),
                    "actual_sample_temperature_k": point.get(
                        "actual_sample_temperature_k"
                    ),
                    "measurement_temperature_k": point.get("measurement_temperature_k"),
                    "time_to_response_s": point.get("time_to_response_s"),
                    "time_to_first_tolerance_s": point.get(
                        "time_to_first_tolerance_s"
                    ),
                    "time_to_stable_s": point.get("time_to_stable_s"),
                    "stable_minimum_k": window.get("minimum_k"),
                    "stable_maximum_k": window.get("maximum_k"),
                    "stable_peak_to_peak_k": window.get("peak_to_peak_k"),
                    "stable_mean_k": window.get("mean_k"),
                    "stable_standard_deviation_k": window.get(
                        "standard_deviation_k"
                    ),
                    "stable_sample_count": window.get("sample_count"),
                }
            )


def _result_paths(progress_path: Path) -> tuple[Path, Path]:
    suffix = "_progress.jsonl"
    stem = progress_path.name
    if not stem.endswith(suffix):
        raise ValueError("Progress filename must end with _progress.jsonl.")
    base = stem[: -len(suffix)]
    return (
        progress_path.with_name(base + "_summary.json"),
        progress_path.with_name(base + "_stable_times.csv"),
    )


def _utc_label(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _git_commit(repository: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository,
            capture_output=True,
            check=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


if __name__ == "__main__":
    main()
