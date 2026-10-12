from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import math
from pathlib import Path
import sys
import time
from uuid import uuid4
from typing import Callable, Sequence

from .attodry import AttoDryDriver, AttoDryError, load_attodry_dll
from .config import (
    TemperatureRunConfig,
    TemperatureInterruptPolicy,
    load_temperature_operation_config,
)
from .models import CryostatState
from .temperature_telemetry import TemperatureTelemetryRecorder


DEFAULT_CONFIG_PATH = Path("config/hardware.local.toml")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the configured daily temperature-conditioning sequence. "
            "Invoking this command connects to attoDRY and may enable temperature "
            "control and write the configured target."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"Unified hardware TOML (default: {DEFAULT_CONFIG_PATH}).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    try:
        exit_code = run(argv)
    except KeyboardInterrupt:
        print(
            "Interrupted. Temperature control shutdown and close were attempted; "
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
    config = load_temperature_operation_config(args.config)
    request = config.temperature_run
    cryostat = config.cryostat
    if (
        cryostat.com_port is None
        or cryostat.dll_path is None
        or cryostat.device_type is None
        or cryostat.connection_timeout_s is None
    ):
        raise ValueError("Hardware cryostat DLL path is missing.")

    dll = dll_loader(cryostat.dll_path)
    driver = AttoDryDriver(
        dll=dll,
        com_port=cryostat.com_port,
        device_type=cryostat.device_type,
        connection_timeout_s=cryostat.connection_timeout_s,
        temperature_min_k=cryostat.temperature_min_k,
        temperature_max_k=cryostat.temperature_max_k,
        limits=config.magnet.limits,
        field_stability=config.magnet.stability,
        readback_tolerance_t=config.magnet.readback_tolerance_t,
        setpoint_ack_tolerance_t=config.magnet.setpoint_ack_tolerance_t,
        temperature_stability=config.temperature_stability,
        connection_authorized=True,
        writes_authorized=True,
    )
    # Imported here because temperature_scan shares the interruption helpers below.
    from .temperature_scan import _JsonlWriter, _utc_label
    output_directory = (
        args.config.resolve().parent / config.temperature_scan.output_directory
        if config.temperature_scan is not None else
        args.config.resolve().parent.parent / "run_data/temperature_commissioning"
    ).resolve()
    progress_path = output_directory / (
        f"{_utc_label(wall_time())}_{uuid4().hex[:8]}_temperature_run_progress.jsonl"
    )
    writer = _JsonlWriter(progress_path, create=True)
    driver.temperature_telemetry = TemperatureTelemetryRecorder(
        driver, lambda kind, payload: writer.append({"event": kind, **payload})
    )
    driver.temperature_event_writer = writer
    record: dict[str, object] = {
        "completed": False,
        "measurement_ready": False,
        "config_path": str(args.config),
        "temperature_run": asdict(request),
        "command_actions": [],
        "temperature_samples": [],
        "interruptions": [],
        "recovery_actions": [],
        "progress_jsonl": str(progress_path),
    }
    writer.append({"event": "temperature_run_started", "captured_unix_s": wall_time(),
                   "config_path": str(args.config), "temperature_run": asdict(request)})
    connected = False
    mutation_attempted = False

    try:
        driver.connect(monotonic=monotonic, sleeper=sleeper)
        connected = True
        initial_state = driver.read_state()
        record["initial_state"] = asdict(initial_state)
        initial_sample = driver.temperature_telemetry.start(
            initial_state, phase="preflight", captured_unix_s=wall_time()
        )
        writer.append({"event": "temperature_snapshot", **initial_sample})
        if initial_state.error_code:
            raise AttoDryError(f"attoDRY preflight error code {initial_state.error_code}.")
        sample_target_delta_k = abs(
            request.target_k - initial_state.sample_temperature_k
        )
        record["prewrite_check"] = {
            "initial_sample_temperature_k": initial_state.sample_temperature_k,
            "sample_target_delta_k": sample_target_delta_k,
            "max_delta_k": request.max_delta_k,
            "passed": sample_target_delta_k <= request.max_delta_k,
        }
        if sample_target_delta_k > request.max_delta_k:
            raise ValueError(
                "Requested sample-temperature movement exceeds "
                "temperature_run.max_delta_k."
            )
        driver.temperature_telemetry.finish(initial_sample)

        mutation_attempted = True
        force_setpoint_reapply = not initial_state.temperature_control_enabled
        record["setpoint_force_reapply_requested"] = force_setpoint_reapply
        driver.set_temperature_and_enable(
            request.target_k,
            monotonic=monotonic,
            sleeper=sleeper,
        )
        record["command_actions"].extend([
            "temperature_target_confirmed_before_enable",
            "temperature_control_confirmed_enabled",
        ])
        record["command_actions"].append("temperature_setpoint_confirmed")

        measurement_state = _monitor_until_measurement(
            driver,
            request,
            samples=record["temperature_samples"],
            interruptions=record["interruptions"],
            monotonic=monotonic,
            sleeper=sleeper,
            wall_time=wall_time,
            confirmation=confirmation,
        )
        record["measurement_state"] = asdict(measurement_state)
        record["measurement_ready"] = True
        record["final_state"] = asdict(measurement_state)
        record["completed"] = True
    except BaseException as exc:
        record["error"] = str(exc)
        if connected:
            _record_failure_diagnostic(record, driver, wall_time=wall_time, read_heater_power=False)
        if connected and mutation_attempted:
            try:
                driver.ensure_temperature_control(
                    False, monotonic=monotonic, sleeper=sleeper
                )
                record["recovery_actions"].append(
                    "temperature_control_disabled_after_failure"
                )
                _record_failure_diagnostic(record, driver, wall_time=wall_time)
            except BaseException as recovery_error:
                record["recovery_error"] = str(recovery_error)
                exc.add_note(f"Temperature recovery also failed: {recovery_error}")
        if connected:
            try:
                record["final_state"] = asdict(driver.read_state())
            except BaseException as read_error:
                record["final_read_error"] = str(read_error)
                record["last_confirmed_state"] = (
                    None
                    if driver.last_confirmed_state is None
                    else asdict(driver.last_confirmed_state)
                )
        record["disconnected"] = _close_with_note(
            driver, connected=connected, primary_error=exc
        )
        try:
            writer.append({"event": "temperature_run_finished", "captured_unix_s": wall_time(),
                           "completed": False, "error": record["error"],
                           "final_state": record.get("final_state"),
                           "failure_diagnostic": record.get("failure_diagnostic"),
                           "disconnected": record["disconnected"]})
        except BaseException as progress_error:
            record["progress_write_error"] = str(progress_error)
            exc.add_note(f"Temperature progress write also failed: {progress_error}")
        print(json.dumps(record, ensure_ascii=False), flush=True)
        raise

    try:
        driver.close()
    except BaseException as exc:
        record["completed"] = False
        record["measurement_ready"] = False
        record["close_error"] = str(exc)
        record["disconnected"] = False
        writer.append({"event": "temperature_run_finished", "captured_unix_s": wall_time(),
                       "completed": False, "close_error": str(exc),
                       "final_state": record.get("final_state"), "disconnected": False})
        print(json.dumps(record, ensure_ascii=False), flush=True)
        raise
    record["disconnected"] = True
    writer.append({"event": "temperature_run_finished", "captured_unix_s": wall_time(),
                   "completed": True, "final_state": record["final_state"],
                   "disconnected": True})
    print(json.dumps(record, indent=2, ensure_ascii=False))
    return 0


def _monitor_until_measurement(
    driver: AttoDryDriver,
    request: TemperatureRunConfig,
    *,
    samples: object,
    interruptions: object,
    monotonic: Callable[[], float],
    sleeper: Callable[[float], None],
    wall_time: Callable[[], float],
    confirmation: Callable[[str], str] | None,
) -> CryostatState:
    if not isinstance(samples, list):
        raise TypeError("Temperature sample container must be a list.")
    if not isinstance(interruptions, list):
        raise TypeError("Temperature interruption container must be a list.")
    started = monotonic()
    overshoot_limit_k = request.target_k + request.max_overshoot_k
    recheck_started: float | None = None
    automatic_recoveries = 0
    while True:
        try:
            state = driver.read_state()
            elapsed_s = monotonic() - started
            sample = driver.temperature_telemetry.start(
                state, captured_unix_s=wall_time(), elapsed_s=elapsed_s,
                phase="conditioning", requested_temperature_k=request.target_k,
            )
            samples.append(sample)
            driver.temperature_event_writer.append({"event": "temperature_sample", **sample})
            if state.error_code:
                raise AttoDryError(
                    f"attoDRY error code {state.error_code} during temperature run."
                )
            if not state.temperature_control_enabled:
                raise AttoDryError(
                    "Temperature control became disabled during temperature run."
                )
            if not math.isclose(
                state.user_temperature_k, request.target_k, abs_tol=1e-4
            ):
                raise AttoDryError(
                    "Temperature setpoint changed during temperature run."
                )
            if state.sample_temperature_k >= overshoot_limit_k:
                raise AttoDryError(
                    "Sample temperature reached the configured overshoot limit: "
                    f"{state.sample_temperature_k:g} K >= {overshoot_limit_k:g} K."
                )
            samples[-1] = driver.temperature_telemetry.finish(sample)
        except KeyboardInterrupt:
            pause_started = monotonic()
            recheck_started = _handle_operator_interrupt(
                driver,
                request,
                interruptions=interruptions,
                monotonic=monotonic,
                wall_time=wall_time,
                confirmation=confirmation,
                automatic_recoveries=automatic_recoveries,
            )
            automatic_recoveries += 1
            started += monotonic() - pause_started
            continue
        if recheck_started is not None:
            recheck_elapsed_s = monotonic() - recheck_started
        else:
            recheck_elapsed_s = request.resume_recheck_s
        if recheck_elapsed_s < request.resume_recheck_s:
            try:
                sleeper(
                    min(
                        request.poll_interval_s,
                        request.resume_recheck_s - recheck_elapsed_s,
                    )
                )
            except KeyboardInterrupt:
                pause_started = monotonic()
                recheck_started = _handle_operator_interrupt(
                    driver,
                    request,
                    interruptions=interruptions,
                    monotonic=monotonic,
                    wall_time=wall_time,
                    confirmation=confirmation,
                    automatic_recoveries=automatic_recoveries,
                )
                automatic_recoveries += 1
                started += monotonic() - pause_started
            continue
        if elapsed_s >= request.pre_measure_wait_s:
            return state
        remaining_s = request.pre_measure_wait_s - elapsed_s
        try:
            sleeper(min(request.poll_interval_s, remaining_s))
        except KeyboardInterrupt:
            pause_started = monotonic()
            recheck_started = _handle_operator_interrupt(
                driver,
                request,
                interruptions=interruptions,
                monotonic=monotonic,
                wall_time=wall_time,
                confirmation=confirmation,
                automatic_recoveries=automatic_recoveries,
            )
            automatic_recoveries += 1
            started += monotonic() - pause_started


def _handle_operator_interrupt(
    driver: AttoDryDriver,
    request: TemperatureRunConfig,
    *,
    interruptions: list[object],
    monotonic: Callable[[], float],
    wall_time: Callable[[], float],
    confirmation: Callable[[str], str] | None,
    automatic_recoveries: int,
) -> float:
    """Validate a soft interruption and return the fresh recheck start time."""
    policy = request.interrupt_policy
    if policy is TemperatureInterruptPolicy.ABORT:
        _append_temperature_interruption(driver, interruptions,
            {
                "captured_unix_s": wall_time(),
                "policy": policy.value,
                "action": "abort",
                "reason": "operator interrupt",
            }
        )
        raise KeyboardInterrupt
    if (
        policy is TemperatureInterruptPolicy.CONTINUE
        and automatic_recoveries >= 1
    ):
        policy = TemperatureInterruptPolicy.WAIT_CONFIRMATION

    state = driver.read_state()
    recorder = getattr(driver, "temperature_telemetry", None)
    interruption_sample = None
    if recorder is not None:
        interruption_sample = recorder.start(
            state, phase="operator_interrupt_recheck", requested_temperature_k=request.target_k,
            captured_unix_s=wall_time(),
        )
        recorder.emit("temperature_snapshot", interruption_sample)
    if state.error_code:
        raise AttoDryError(
            "Cannot recover temperature interruption while attoDRY reports "
            f"error code {state.error_code}."
        )
    if not state.temperature_control_enabled:
        raise AttoDryError(
            "Cannot recover temperature interruption while temperature control "
            "is not confirmed enabled."
        )
    if not math.isclose(state.user_temperature_k, request.target_k, abs_tol=1e-4):
        raise AttoDryError(
            "Cannot recover temperature interruption after the temperature "
            "setpoint changed."
        )
    overshoot_limit_k = request.target_k + request.max_overshoot_k
    if state.sample_temperature_k >= overshoot_limit_k:
        raise AttoDryError(
            "Cannot recover temperature interruption after the sample reached "
            f"the overshoot limit: {state.sample_temperature_k:g} K >= "
            f"{overshoot_limit_k:g} K."
        )
    if interruption_sample is not None:
        recorder.finish(interruption_sample)

    action = "continue"
    if policy is TemperatureInterruptPolicy.WAIT_CONFIRMATION:
        answerer = confirmation or input
        answer = answerer(
            "Temperature control is confirmed safe. Continue this run? [y/N] "
        )
        if answer.strip().lower() not in {"y", "yes", "continue"}:
            _append_temperature_interruption(driver, interruptions,
                {
                    "captured_unix_s": wall_time(),
                    "policy": request.interrupt_policy.value,
                    "action": "abort",
                    "reason": "operator confirmation declined",
                    "state": asdict(state),
                }
            )
            raise KeyboardInterrupt
        action = "continue-after-confirmation"
    _append_temperature_interruption(driver, interruptions,
        {
            "captured_unix_s": wall_time(),
            "policy": request.interrupt_policy.value,
            "effective_policy": policy.value,
            "action": action,
            "reason": "operator interrupt",
            "state": asdict(state),
        }
    )
    return monotonic()


def _append_temperature_interruption(driver, interruptions, event):
    interruptions.append(event)
    writer = getattr(driver, "temperature_event_writer", None)
    if writer is not None:
        writer.append({"event": "operator_interruption", "details": event})


def _record_failure_diagnostic(
    record: dict[str, object],
    driver: AttoDryDriver,
    *,
    wall_time: Callable[[], float],
    read_heater_power: bool = True,
) -> None:
    diagnostic = record.get("failure_diagnostic")
    if not isinstance(diagnostic, dict):
        diagnostic = {
            "captured_unix_s": wall_time(),
            "trigger_state": (
                None
                if driver.last_confirmed_state is None
                else asdict(driver.last_confirmed_state)
            ),
        }
        record["failure_diagnostic"] = diagnostic
    if not read_heater_power:
        diagnostic["heater_power_skipped"] = "fault cleanup has priority"
        return
    diagnostic.pop("heater_power_skipped", None)
    diagnostic["heater_power_captured_unix_s"] = wall_time()
    sample = None
    recorder = getattr(driver, "temperature_telemetry", None)
    try:
        if recorder is not None and driver.last_confirmed_state is not None:
            sample = recorder.start(
                driver.last_confirmed_state, phase="cleanup_after_disable",
                captured_unix_s=wall_time(),
            )
            recorder.emit("temperature_snapshot", sample)
            telemetry = recorder.finish(sample)
            diagnostic["heater_power"] = telemetry["heater_power"]
            diagnostic["heater_power_state"] = telemetry["state"]
            diagnostic["heater_power_read_timing"] = telemetry["heater_power_read_timing"]
            diagnostic["heater_power_sample_id"] = telemetry["sample_id"]
        else:
            diagnostic["heater_power"] = asdict(driver.read_heater_powers())
    except BaseException as read_error:
        diagnostic["heater_power_read_error"] = str(read_error)
        # finish() persists a partial attempt before re-raising. Expose that
        # same attempt in the summary too; never copy an earlier sample.
        partial = recorder.last_record if recorder is not None else None
        if sample is not None and partial is not None and partial.get("sample_id") == sample["sample_id"]:
            diagnostic["heater_power"] = partial["heater_power"]
            diagnostic["heater_power_state"] = partial["state"]
            diagnostic["heater_power_read_timing"] = partial.get("heater_power_read_timing")
            diagnostic["heater_power_sample_id"] = partial["sample_id"]
    record["failure_diagnostic"] = diagnostic


def _close_with_note(
    driver: AttoDryDriver, *, connected: bool, primary_error: BaseException
) -> bool:
    if not connected:
        return True
    try:
        driver.close()
    except BaseException as close_error:
        primary_error.add_note(f"attoDRY close also failed: {close_error}")
        return False
    return True


if __name__ == "__main__":
    main()
