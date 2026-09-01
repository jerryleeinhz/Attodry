"""Outer temperature / inner dual-SR830 excitation commissioning scan.

The module deliberately composes the already validated temperature-stability and
dual-SR830 excitation executors.  It does not introduce a second instrument
transport, a background polling thread, or an implicit hardware authorization.
"""

from __future__ import annotations

import argparse
import csv
from dataclasses import asdict, replace
import json
import math
from pathlib import Path
import sys
import time
from typing import Callable, Mapping, Sequence

from .attodry import (
    AttoDryAuthorizationError,
    AttoDryDriver,
    AttoDryError,
    load_attodry_dll,
)
from .config import (
    TemperatureExcitationOperationConfig,
    TemperatureRunConfig,
    load_temperature_excitation_operation_config,
)
from .lockin_test import (
    _execute_excitation_sweep_on_open_pair,
    _load_resource_manager_factory,
    _measurement_config_snapshot,
    _open_pair,
    _sweep_point_progress_snapshot,
    prepare_configured_excitation_sweep,
)
from .models import CryostatState
from .sr830 import Sr830Error
from .temperature_run import _close_with_note, _record_failure_diagnostic
from .temperature_scan import (
    SCHEMA_VERSION,
    _JsonlWriter,
    _git_commit,
    _measurement_config as _temperature_measurement_config,
    _utc_label,
    _validate_point_state,
    _validate_static_path,
    _wait_for_stable_point,
)


DEFAULT_CONFIG_PATH = Path("config/hardware.local.toml")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the configured ascending attoDRY temperature scan and a complete "
            "dual-SR830 excitation sweep at every stable temperature point."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Unified hardware TOML (default: {DEFAULT_CONFIG_PATH}).",
    )
    parser.add_argument(
        "--authorize-temperature-excitation-scan",
        action="store_true",
        help=(
            "Authorize attoDRY temperature-control writes and the configured "
            "dual-SR830 excitation sweep."
        ),
    )
    parser.add_argument(
        "--resume-progress",
        type=Path,
        help=(
            "Resume at the first incomplete temperature condition from a parent "
            "progress JSONL. Completed conditions are never rerun."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    try:
        exit_code = run(argv)
    except KeyboardInterrupt:
        print(
            "Interrupted. Lock-in cleanup/close and temperature-control shutdown "
            "were attempted; manually verify both instruments.",
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
    resource_manager_factory: Callable[[], object] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    wall_time: Callable[[], float] = time.time,
    confirmation: Callable[[str], str] | None = None,
) -> int:
    """Execute one explicitly authorized temperature/excitation scan.

    No DLL is loaded and no VISA resource is constructed until the strict TOML
    contract, static limits, and authorization flag all pass.
    """

    args = build_parser().parse_args(argv)
    config_path = args.config.resolve()
    config = load_temperature_excitation_operation_config(config_path)
    temperature_points_k = config.temperature_scan.points_k
    _validate_static_path(temperature_points_k, config.temperature_run)
    (
        lockin_args,
        lockin_settings,
        excitation_points_v,
        excitation_safety,
    ) = prepare_configured_excitation_sweep(config_path, config)

    if not args.authorize_temperature_excitation_scan:
        raise AttoDryAuthorizationError(
            "Temperature/excitation scan is not authorized. Re-run only after the "
            "operator approves this real combined write stage and include "
            "--authorize-temperature-excitation-scan."
        )

    cryostat = config.cryostat
    if (
        cryostat.com_port is None
        or cryostat.dll_path is None
        or cryostat.device_type is None
        or cryostat.connection_timeout_s is None
    ):
        raise ValueError("Hardware cryostat DLL path is missing.")

    combined_scan = config.temperature_excitation_scan
    output_directory = (config_path.parent / combined_scan.output_directory).resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    measurement_config = _measurement_config(
        config,
        temperature_points_k,
        lockin_args=lockin_args,
        lockin_settings=lockin_settings,
        excitation_safety=excitation_safety,
    )
    if args.resume_progress is None:
        progress_path = output_directory / (
            f"{_utc_label(wall_time())}_{combined_scan.run_name}_"
            "temperature_excitation_progress.jsonl"
        )
        summary_path, csv_path = _result_paths(progress_path)
        writer = _JsonlWriter(progress_path, create=True)
        completed_conditions: list[dict[str, object]] = []
        writer.append(
            {
                "event": "scan_started",
                "schema_version": SCHEMA_VERSION,
                "captured_unix_s": wall_time(),
                "command": "temperature-excitation-scan",
                "run_metadata": _run_metadata(config, config_path),
                "config_path": str(config_path),
                "measurement_config": measurement_config,
            }
        )
    else:
        progress_path = args.resume_progress.resolve()
        if progress_path.parent != output_directory:
            raise ValueError(
                "--resume-progress must be inside "
                "temperature_excitation_scan.output_directory."
            )
        summary_path, csv_path = _result_paths(progress_path)
        completed_conditions = _load_resume_progress(
            progress_path, measurement_config, temperature_points_k
        )
        writer = _JsonlWriter(progress_path, create=False)
        writer.append(
            {
                "event": "scan_resumed",
                "captured_unix_s": wall_time(),
                "resume_from_temperature_index": len(completed_conditions),
                "git_commit": _git_commit(config_path.parent.parent),
            }
        )

    summary: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "command": "temperature-excitation-scan",
        "completed": False,
        "outcome": "running",
        "captured_unix_s": wall_time(),
        "run_metadata": _run_metadata(config, config_path),
        "config_path": str(config_path),
        "measurement_config": measurement_config,
        "progress_jsonl": str(progress_path),
        "formal_samples_csv": str(csv_path),
        "temperature_conditions": completed_conditions,
        "interruptions": [],
        "recovery_actions": [],
    }
    # Resolve the Python VISA backend before loading the vendor DLL. Neither
    # action opens an instrument, but this ordering avoids a needless DLL load
    # when the required PyVISA dependency is absent.
    factory = (
        resource_manager_factory
        if resource_manager_factory is not None
        else _load_resource_manager_factory()
    )
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
        summary["preflight"] = {"initial_cryostat_state": asdict(initial_state)}
        if initial_state.error_code:
            raise AttoDryError(
                f"attoDRY preflight error code {initial_state.error_code}."
            )

        # The pair stays open for the full outer loop. Each inner sweep retains
        # its own verified preflight and cleanup record.
        with _open_pair(lockin_settings, factory) as (lockin_xx, lockin_xy):
            for temperature_index in range(
                len(completed_conditions), len(temperature_points_k)
            ):
                target_k = temperature_points_k[temperature_index]
                before_state = driver.read_state()
                _write_temperature_preflight(
                    writer,
                    before_state,
                    target_k=target_k,
                    temperature_index=temperature_index,
                    config=config,
                    wall_time=wall_time,
                )
                _validate_prewrite_temperature_move(
                    before_state, target_k, config.temperature_run
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
                        "temperature_index": temperature_index,
                        "requested_temperature_k": target_k,
                        "actual_setpoint_k": confirmed_state.user_temperature_k,
                        "force_reapply": force_reapply,
                        "state": asdict(confirmed_state),
                    }
                )

                stable_summary = _wait_for_stable_point(
                    driver,
                    replace(config.temperature_run, target_k=target_k),
                    point_index=temperature_index,
                    writer=writer,
                    interruptions=summary["interruptions"],
                    response_reference_k=before_state.sample_temperature_k,
                    monotonic=monotonic,
                    sleeper=sleeper,
                    wall_time=wall_time,
                    confirmation=confirmation,
                )
                writer.append(
                    {
                        "event": "temperature_stable",
                        "captured_unix_s": wall_time(),
                        "temperature_index": temperature_index,
                        "requested_temperature_k": target_k,
                        "stability": stable_summary,
                    }
                )

                condition: dict[str, object] = {
                    "temperature_index": temperature_index,
                    "requested_temperature_k": target_k,
                    "stability": stable_summary,
                    "stability_measurement_temperature_k": stable_summary[
                        "measurement_temperature_k"
                    ],
                    "excitation": None,
                    "measurement_temperature_k": None,
                    "measurement_window_temperature": None,
                }
                writer.append(
                    {
                        "event": "excitation_started",
                        "captured_unix_s": wall_time(),
                        "temperature_index": temperature_index,
                        "requested_temperature_k": target_k,
                        "excitation_points_v_rms": list(excitation_points_v),
                    }
                )

                measurement_context = _temperature_measurement_context(
                    driver,
                    request=config.temperature_run,
                    target_k=target_k,
                    temperature_index=temperature_index,
                    writer=writer,
                    monotonic=monotonic,
                    wall_time=wall_time,
                )
                on_formal_sample = _formal_sample_progress_callback(
                    writer,
                    temperature_index=temperature_index,
                    target_k=target_k,
                    wall_time=wall_time,
                )
                on_point_ready = _lockin_point_progress_callback(
                    writer,
                    event="lockin_point_ready",
                    temperature_index=temperature_index,
                    target_k=target_k,
                    wall_time=wall_time,
                )
                on_point_completed = _lockin_point_progress_callback(
                    writer,
                    event="lockin_point_completed",
                    temperature_index=temperature_index,
                    target_k=target_k,
                    wall_time=wall_time,
                )
                excitation_record, excitation_failure = (
                    _execute_excitation_sweep_on_open_pair(
                        lockin_xx,
                        lockin_xy,
                        args=lockin_args,
                        settings=lockin_settings,
                        points=excitation_points_v,
                        safety=excitation_safety,
                        measurement_context=measurement_context,
                        on_formal_sample_recorded=on_formal_sample,
                        on_point_ready=on_point_ready,
                        on_point_completed=on_point_completed,
                    )
                )
                condition["excitation"] = excitation_record
                condition_statistics = _enrich_excitation_temperature_records(
                    excitation_record
                )
                condition["measurement_temperature_k"] = condition_statistics[
                    "mean_k"
                ]
                condition["measurement_window_temperature"] = condition_statistics
                summary["partial_condition"] = condition
                writer.append(
                    {
                        "event": "excitation_finished",
                        "captured_unix_s": wall_time(),
                        "temperature_index": temperature_index,
                        "requested_temperature_k": target_k,
                        "completed": excitation_record["completed"],
                        "outcome": excitation_record["outcome"],
                        "measurement_window_temperature": condition_statistics,
                        "cleanup": excitation_record["cleanup"],
                        "error": excitation_record["error"],
                    }
                )
                if excitation_failure is not None:
                    raise excitation_failure
                cleanup = excitation_record["cleanup"]
                if not isinstance(cleanup, dict) or cleanup.get("verified") is not True:
                    raise Sr830Error(
                        "Excitation sweep completed with unsuccessful cleanup."
                    )

                post_excitation_state = driver.read_state()
                _validate_point_state(
                    post_excitation_state, target_k, config.temperature_run
                )
                condition["post_excitation_state"] = asdict(post_excitation_state)
                completed_conditions.append(condition)
                summary.pop("partial_condition", None)
                writer.append(
                    {
                        "event": "temperature_condition_completed",
                        "captured_unix_s": wall_time(),
                        "temperature_index": temperature_index,
                        "summary": condition,
                    }
                )

        final_state = driver.read_state()
        _validate_point_state(
            final_state, temperature_points_k[-1], config.temperature_run
        )
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


def _run_metadata(
    config: TemperatureExcitationOperationConfig, config_path: Path
) -> dict[str, str | None]:
    scan = config.temperature_excitation_scan
    return {
        "run_name": scan.run_name,
        "note": scan.note,
        "git_commit": _git_commit(config_path.parent.parent),
    }


def _measurement_config(
    config: TemperatureExcitationOperationConfig,
    temperature_points_k: tuple[float, ...],
    *,
    lockin_args: argparse.Namespace,
    lockin_settings: dict[str, object],
    excitation_safety: dict[str, float],
) -> dict[str, object]:
    """Make the resume contract without duplicating independent config tables."""

    temperature = _temperature_measurement_config(config, temperature_points_k)
    lockin = _measurement_config_snapshot(
        lockin_settings,
        lockin_args,
        scan="excitation",
        excitation_path={
            **excitation_safety,
            "external_50_ohm_termination": False,
        },
    )
    combined = config.temperature_excitation_scan
    return {
        "temperature": temperature,
        "lockin_excitation": lockin,
        "temperature_excitation_scan": {
            "run_name": combined.run_name,
            "note": combined.note,
            "output_directory": str(combined.output_directory),
            "traversal": "temperature_outer_ascending; excitation_inner_ascending",
            "resume_boundary": "completed-temperature-condition-only",
            "formal_temperature_sampling": (
                "synchronous-before-after-each-xx-xy-formal-sample; "
                "time-weighted-trapezoid-mean"
            ),
        },
    }


def _write_temperature_preflight(
    writer: _JsonlWriter,
    state: CryostatState,
    *,
    target_k: float,
    temperature_index: int,
    config: TemperatureExcitationOperationConfig,
    wall_time: Callable[[], float],
) -> None:
    sample_target_delta_k = abs(target_k - state.sample_temperature_k)
    prewrite = {
        "initial_sample_temperature_k": state.sample_temperature_k,
        "sample_target_delta_k": sample_target_delta_k,
        "max_delta_k": config.temperature_run.max_delta_k,
        "passed": sample_target_delta_k <= config.temperature_run.max_delta_k,
    }
    writer.append(
        {
            "event": "temperature_point_preflight",
            "captured_unix_s": wall_time(),
            "temperature_index": temperature_index,
            "requested_temperature_k": target_k,
            "state": asdict(state),
            "prewrite_check": prewrite,
        }
    )


def _validate_prewrite_temperature_move(
    state: CryostatState, target_k: float, request: TemperatureRunConfig
) -> None:
    if state.error_code:
        raise AttoDryError(
            f"attoDRY error code {state.error_code} before temperature condition."
        )
    if abs(target_k - state.sample_temperature_k) > request.max_delta_k:
        raise ValueError(
            "Requested sample-temperature movement exceeds "
            "temperature_run.max_delta_k."
        )


def _temperature_measurement_context(
    driver: AttoDryDriver,
    *,
    request: TemperatureRunConfig,
    target_k: float,
    temperature_index: int,
    writer: _JsonlWriter,
    monotonic: Callable[[], float],
    wall_time: Callable[[], float],
) -> Callable[[str, int, int], Mapping[str, object]]:
    """Return a synchronous context reader for each formal XX/XY sample pair.

    A background DLL thread is intentionally avoided: vendor DLL thread safety is
    not established, while the before/after samples have an unambiguous interval.
    """

    def capture(stage: str, harmonic: int, sample_index: int) -> Mapping[str, object]:
        state = driver.read_state()
        _validate_point_state(state, target_k, request)
        sample = {
            "stage": stage,
            "harmonic": harmonic,
            "sample_index": sample_index,
            "captured_monotonic_s": monotonic(),
            "captured_unix_s": wall_time(),
            "state": asdict(state),
        }
        writer.append(
            {
                "event": "measurement_temperature_sample",
                "temperature_index": temperature_index,
                "requested_temperature_k": target_k,
                **sample,
            }
        )
        return sample

    return capture


def _formal_sample_progress_callback(
    writer: _JsonlWriter,
    *,
    temperature_index: int,
    target_k: float,
    wall_time: Callable[[], float],
) -> Callable[[Mapping[str, object]], None]:
    def record(sample: Mapping[str, object]) -> None:
        statistics = _formal_sample_temperature_statistics(sample)
        if isinstance(sample, dict):
            sample["measurement_window_temperature"] = statistics
            sample["measurement_temperature_k"] = statistics["mean_k"]
        point = sample.get("sweep_point")
        writer.append(
            {
                "event": "lockin_formal_sample",
                "captured_unix_s": wall_time(),
                "scan": "excitation",
                "temperature_index": temperature_index,
                "requested_temperature_k": target_k,
                "sweep_point": (
                    dict(point) if isinstance(point, Mapping) else {}
                ),
                "sample": sample,
            }
        )

    return record


def _lockin_point_progress_callback(
    writer: _JsonlWriter,
    *,
    event: str,
    temperature_index: int,
    target_k: float,
    wall_time: Callable[[], float],
) -> Callable[[Mapping[str, object]], None]:
    """Archive the current, already-read-back SR830 point without a new query."""

    def record(point: Mapping[str, object]) -> None:
        writer.append(
            {
                "event": event,
                "captured_unix_s": wall_time(),
                "scan": "excitation",
                "temperature_index": temperature_index,
                "requested_temperature_k": target_k,
                "sweep_point": _sweep_point_progress_snapshot(point),
            }
        )

    return record


def _formal_sample_temperature_statistics(
    sample: Mapping[str, object]
) -> dict[str, object]:
    context = sample.get("measurement_context")
    if not isinstance(context, Mapping):
        return _temperature_window_statistics(())
    observations: list[Mapping[str, object]] = []
    for key in ("before", "after"):
        value = context.get(key)
        if isinstance(value, Mapping):
            observations.append(value)
    return _temperature_window_statistics(observations)


def _enrich_excitation_temperature_records(
    excitation_record: Mapping[str, object],
) -> dict[str, object]:
    """Attach formal-window temperatures at point level and condition level."""

    condition_windows: list[Mapping[str, object]] = []
    points = excitation_record.get("points")
    if not isinstance(points, list):
        return _combine_formal_window_statistics(())
    for point in points:
        if not isinstance(point, dict):
            continue
        point_windows: list[Mapping[str, object]] = []
        samples = point.get("samples")
        if not isinstance(samples, list):
            continue
        for sample in samples:
            if not isinstance(sample, dict):
                continue
            sample_statistics = _formal_sample_temperature_statistics(sample)
            sample["measurement_window_temperature"] = sample_statistics
            sample["measurement_temperature_k"] = sample_statistics["mean_k"]
            point_windows.append(sample_statistics)
            condition_windows.append(sample_statistics)
        point_statistics = _combine_formal_window_statistics(point_windows)
        point["measurement_window_temperature"] = point_statistics
        point["measurement_temperature_k"] = point_statistics["mean_k"]
    return _combine_formal_window_statistics(condition_windows)


def _combine_formal_window_statistics(
    windows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Combine only formal measurement intervals, never the gaps between them."""

    valid = [
        window
        for window in windows
        if isinstance(window.get("mean_k"), (int, float))
        and isinstance(window.get("duration_s"), (int, float))
    ]
    if not valid:
        return _temperature_window_statistics(())
    durations = [max(0.0, float(window["duration_s"])) for window in valid]
    total_duration_s = sum(durations)
    means = [float(window["mean_k"]) for window in valid]
    if total_duration_s > 0:
        mean_k = sum(
            mean * duration for mean, duration in zip(means, durations)
        ) / total_duration_s
        method = "time-weighted-over-formal-windows"
    else:
        mean_k = sum(means) / len(means)
        method = "arithmetic-mean-zero-duration-formal-windows"
    minimums = [
        float(window["minimum_k"])
        for window in valid
        if isinstance(window.get("minimum_k"), (int, float))
    ]
    maximums = [
        float(window["maximum_k"])
        for window in valid
        if isinstance(window.get("maximum_k"), (int, float))
    ]
    sample_count = sum(
        int(window["sample_count"])
        for window in valid
        if isinstance(window.get("sample_count"), int)
    )
    return {
        "method": method,
        "mean_k": mean_k,
        "minimum_k": None if not minimums else min(minimums),
        "maximum_k": None if not maximums else max(maximums),
        "sample_count": sample_count,
        "duration_s": total_duration_s,
        "formal_window_count": len(valid),
    }


def _temperature_window_statistics(
    observations: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Return a time-weighted temperature mean from formal sampling endpoints."""

    timed_values: list[tuple[float, float]] = []
    for observation in observations:
        timestamp = observation.get("captured_monotonic_s")
        state = observation.get("state")
        if not isinstance(timestamp, (int, float)) or not isinstance(state, Mapping):
            continue
        temperature_k = state.get("sample_temperature_k")
        if not isinstance(temperature_k, (int, float)):
            continue
        timed_values.append((float(timestamp), float(temperature_k)))
    timed_values.sort(key=lambda item: item[0])
    if not timed_values:
        return {
            "method": "unavailable",
            "mean_k": None,
            "minimum_k": None,
            "maximum_k": None,
            "sample_count": 0,
            "duration_s": 0.0,
        }

    values = [value for _, value in timed_values]
    duration_s = timed_values[-1][0] - timed_values[0][0]
    if duration_s > 0:
        integral = sum(
            (previous_temperature + next_temperature)
            * (next_time - previous_time)
            / 2.0
            for (previous_time, previous_temperature), (
                next_time,
                next_temperature,
            ) in zip(timed_values, timed_values[1:])
        )
        mean_k = integral / duration_s
        method = "time-weighted-trapezoid"
    else:
        mean_k = sum(values) / len(values)
        method = "arithmetic-mean-zero-duration"
    return {
        "method": method,
        "mean_k": mean_k,
        "minimum_k": min(values),
        "maximum_k": max(values),
        "sample_count": len(values),
        "duration_s": duration_s,
    }


def _load_resume_progress(
    path: Path,
    measurement_config: dict[str, object],
    temperature_points_k: tuple[float, ...],
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
            raise ValueError(f"Progress JSONL line {line_number} is not an object.")
        events.append(event)
    if events[0].get("event") != "scan_started":
        raise ValueError("Progress JSONL does not start with scan_started.")
    if events[0].get("command") != "temperature-excitation-scan":
        raise ValueError("Progress JSONL is not a temperature/excitation scan.")
    if events[0].get("measurement_config") != measurement_config:
        raise ValueError("Current TOML does not match the archived scan contract.")
    if any(
        event.get("event") == "scan_finished" and event.get("completed") is True
        for event in events
    ):
        raise ValueError("The selected progress JSONL already completed.")

    completed: list[dict[str, object]] = []
    for event in events:
        if event.get("event") != "temperature_condition_completed":
            continue
        condition = event.get("summary")
        if not isinstance(condition, dict):
            raise ValueError(
                "A temperature_condition_completed event has no summary object."
            )
        expected_index = len(completed)
        if condition.get("temperature_index") != expected_index:
            raise ValueError("Completed temperature conditions are not contiguous.")
        requested = condition.get("requested_temperature_k")
        if not isinstance(requested, (int, float)) or not math.isclose(
            float(requested), temperature_points_k[expected_index], abs_tol=1e-9
        ):
            raise ValueError(
                "Completed temperature condition does not match the scan grid."
            )
        completed.append(condition)
    if len(completed) >= len(temperature_points_k):
        raise ValueError("All configured temperature conditions are already complete.")
    return completed


def _write_results(summary_path: Path, csv_path: Path, summary: dict[str, object]) -> None:
    temporary = summary_path.with_suffix(summary_path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    temporary.replace(summary_path)
    _write_formal_samples_csv(csv_path, summary)


def _write_formal_samples_csv(csv_path: Path, summary: Mapping[str, object]) -> None:
    """Flatten only complete temperature conditions for default analysis."""

    fields = (
        "temperature_index",
        "requested_temperature_k",
        "stability_measurement_temperature_k",
        "condition_measurement_temperature_k",
        "source_v_rms",
        "source_readback_v_rms",
        "nominal_current_a_rms",
        "harmonic",
        "sample_index",
        "role",
        "measurement_temperature_k",
        "measurement_window_duration_s",
        "x_v",
        "y_v",
        "amplitude_v",
        "phase_deg",
        "frequency_hz",
        "lia_status_raw",
        "error_status",
    )
    with csv_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        conditions = summary.get("temperature_conditions")
        if not isinstance(conditions, list):
            return
        for condition in conditions:
            if not isinstance(condition, Mapping):
                continue
            excitation = condition.get("excitation")
            if not isinstance(excitation, Mapping):
                continue
            points = excitation.get("points")
            if not isinstance(points, list):
                continue
            for point in points:
                if not isinstance(point, Mapping):
                    continue
                samples = point.get("samples")
                if not isinstance(samples, list):
                    continue
                for sample in samples:
                    if not isinstance(sample, Mapping):
                        continue
                    window = sample.get("measurement_window_temperature")
                    window = window if isinstance(window, Mapping) else {}
                    selected = sample.get("selected_roles")
                    roles = (
                        tuple(
                            role
                            for role in selected
                            if role in {"lockin_xx", "lockin_xy"}
                        )
                        if isinstance(selected, list)
                        else ("lockin_xx", "lockin_xy")
                    )
                    for role in roles:
                        audited = sample.get(role)
                        if not isinstance(audited, Mapping):
                            continue
                        reading = audited.get("reading")
                        if not isinstance(reading, Mapping):
                            continue
                        status = audited.get("lia_status")
                        if not isinstance(status, Mapping):
                            # Support hand-built/offline fixture records as well
                            # as the normal audited Sr830HarmonicSample shape.
                            status = reading.get("lia_status")
                        status = status if isinstance(status, Mapping) else {}
                        yield_row = {
                            "temperature_index": condition.get("temperature_index"),
                            "requested_temperature_k": condition.get(
                                "requested_temperature_k"
                            ),
                            "stability_measurement_temperature_k": condition.get(
                                "stability_measurement_temperature_k"
                            ),
                            "condition_measurement_temperature_k": condition.get(
                                "measurement_temperature_k"
                            ),
                            "source_v_rms": point.get("source_v_rms"),
                            "source_readback_v_rms": point.get(
                                "source_readback_v_rms"
                            ),
                            "nominal_current_a_rms": point.get(
                                "nominal_current_a_rms"
                            ),
                            "harmonic": sample.get("harmonic"),
                            "sample_index": sample.get("sample_index"),
                            "role": role,
                            "measurement_temperature_k": sample.get(
                                "measurement_temperature_k"
                            ),
                            "measurement_window_duration_s": window.get(
                                "duration_s"
                            ),
                            "x_v": reading.get("x_v"),
                            "y_v": reading.get("y_v"),
                            "amplitude_v": reading.get("amplitude_v"),
                            "phase_deg": reading.get("phase_deg"),
                            "frequency_hz": reading.get("frequency_hz"),
                            "lia_status_raw": status.get("raw"),
                            "error_status": audited.get(
                                "error_status", reading.get("error_status")
                            ),
                        }
                        writer.writerow(yield_row)


def _result_paths(progress_path: Path) -> tuple[Path, Path]:
    suffix = "_progress.jsonl"
    if not progress_path.name.endswith(suffix):
        raise ValueError("Progress filename must end with _progress.jsonl.")
    base = progress_path.name[: -len(suffix)]
    return (
        progress_path.with_name(base + "_summary.json"),
        progress_path.with_name(base + "_formal_samples.csv"),
    )


if __name__ == "__main__":
    main()
