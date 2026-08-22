from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import math
from pathlib import Path
import sys
import time
from typing import Callable, Sequence
import uuid

from .attodry import AttoDryDriver, AttoDryError, load_attodry_dll
from .config import (
    TemperatureRunConfig,
    load_temperature_operation_config,
)
from .models import CryostatState


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
) -> int:
    args = build_parser().parse_args(argv)
    config = load_temperature_operation_config(args.config)
    request = config.temperature_run
    cryostat = config.cryostat
    live_log_path = (
        args.config.resolve().parent / request.live_log_path
    ).resolve()
    live_log_path.parent.mkdir(parents=True, exist_ok=True)
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
        temperature_stability=config.temperature_stability,
        connection_authorized=True,
        writes_authorized=True,
    )
    request_record = asdict(request)
    request_record["live_log_path"] = str(request.live_log_path)
    record: dict[str, object] = {
        "run_id": uuid.uuid4().hex,
        "completed": False,
        "measurement_ready": False,
        "config_path": str(args.config),
        "live_log_path": str(live_log_path),
        "temperature_run": request_record,
        "command_actions": [],
        "temperature_samples": [],
        "recovery_actions": [],
    }
    connected = False
    mutation_attempted = False

    try:
        driver.connect(monotonic=monotonic, sleeper=sleeper)
        connected = True
        initial_state = driver.read_state()
        record["initial_state"] = asdict(initial_state)
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

        mutation_attempted = True
        driver.ensure_temperature_control(
            True, monotonic=monotonic, sleeper=sleeper
        )
        record["command_actions"].append(
            "temperature_control_confirmed_enabled"
        )
        force_setpoint_reapply = not initial_state.temperature_control_enabled
        record["setpoint_force_reapply_requested"] = force_setpoint_reapply
        driver.set_temperature(
            request.target_k,
            force_write=force_setpoint_reapply,
            monotonic=monotonic,
            sleeper=sleeper,
        )
        record["command_actions"].append("temperature_setpoint_confirmed")

        measurement_state = _monitor_until_measurement(
            driver,
            request,
            samples=record["temperature_samples"],
            run_id=str(record["run_id"]),
            live_log_path=live_log_path,
            monotonic=monotonic,
            sleeper=sleeper,
            wall_time=wall_time,
        )
        record["measurement_state"] = asdict(measurement_state)
        record["measurement_ready"] = True
        record["final_state"] = asdict(measurement_state)
        record["completed"] = True
    except BaseException as exc:
        record["error"] = str(exc)
        if connected:
            _record_failure_diagnostic(record, driver, wall_time=wall_time)
        if connected and mutation_attempted:
            try:
                driver.ensure_temperature_control(
                    False, monotonic=monotonic, sleeper=sleeper
                )
                record["recovery_actions"].append(
                    "temperature_control_disabled_after_failure"
                )
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
        print(json.dumps(record, ensure_ascii=False), flush=True)
        raise

    try:
        driver.close()
    except BaseException as exc:
        record["completed"] = False
        record["measurement_ready"] = False
        record["close_error"] = str(exc)
        record["disconnected"] = False
        print(json.dumps(record, ensure_ascii=False), flush=True)
        raise
    record["disconnected"] = True
    print(json.dumps(record, indent=2, ensure_ascii=False))
    return 0


def _monitor_until_measurement(
    driver: AttoDryDriver,
    request: TemperatureRunConfig,
    *,
    samples: object,
    run_id: str,
    live_log_path: Path,
    monotonic: Callable[[], float],
    sleeper: Callable[[float], None],
    wall_time: Callable[[], float],
) -> CryostatState:
    if not isinstance(samples, list):
        raise TypeError("Temperature sample container must be a list.")
    started = monotonic()
    overshoot_limit_k = request.target_k + request.max_overshoot_k
    next_display_elapsed_s = 0.0
    next_heater_elapsed_s = 0.0
    last_heater_power: dict[str, object] | None = None
    while True:
        state = driver.read_state()
        elapsed_s = monotonic() - started
        sample: dict[str, object] = {
            "run_id": run_id,
            "captured_unix_s": wall_time(),
            "elapsed_s": elapsed_s,
            "state": asdict(state),
        }
        if elapsed_s >= next_heater_elapsed_s:
            try:
                last_heater_power = asdict(driver.read_heater_powers())
                sample["heater_power"] = last_heater_power
            except BaseException as read_error:
                sample["heater_power_read_error"] = str(read_error)
                samples.append(sample)
                _append_jsonl(live_log_path, sample)
                raise
            next_heater_elapsed_s = elapsed_s + request.heater_power_interval_s
        samples.append(sample)
        _append_jsonl(live_log_path, sample)
        if elapsed_s >= next_display_elapsed_s:
            _print_live_status(
                state,
                elapsed_s=elapsed_s,
                heater_power=last_heater_power,
            )
            next_display_elapsed_s = elapsed_s + request.display_interval_s
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
        if elapsed_s >= request.pre_measure_wait_s:
            return state
        remaining_s = request.pre_measure_wait_s - elapsed_s
        sleeper(min(request.poll_interval_s, remaining_s))


def _append_jsonl(path: Path, payload: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(payload, ensure_ascii=False) + "\n")
        output.flush()


def _print_live_status(
    state: CryostatState,
    *,
    elapsed_s: float,
    heater_power: dict[str, object] | None,
) -> None:
    sample_heater = (
        "n/a"
        if heater_power is None
        else f"{float(heater_power['sample_w']):.4f} W"
    )
    vti_heater = (
        "n/a"
        if heater_power is None
        else f"{float(heater_power['vti_w']):.4f} W"
    )
    print(
        "temperature "
        f"elapsed={elapsed_s:.1f} s "
        f"sample={state.sample_temperature_k:.4f} K "
        f"target={state.user_temperature_k:.4f} K "
        f"vti={state.vti_temperature_k:.4f} K "
        f"sample_heater={sample_heater} "
        f"vti_heater={vti_heater} "
        f"control={'on' if state.temperature_control_enabled else 'off'} "
        f"error={state.error_code}",
        file=sys.stderr,
        flush=True,
    )


def _record_failure_diagnostic(
    record: dict[str, object],
    driver: AttoDryDriver,
    *,
    wall_time: Callable[[], float],
) -> None:
    diagnostic: dict[str, object] = {
        "captured_unix_s": wall_time(),
        "trigger_state": (
            None
            if driver.last_confirmed_state is None
            else asdict(driver.last_confirmed_state)
        ),
    }
    try:
        diagnostic["heater_power"] = asdict(driver.read_heater_powers())
    except BaseException as read_error:
        diagnostic["heater_power_read_error"] = str(read_error)
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
