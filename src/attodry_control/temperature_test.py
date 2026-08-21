from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
import json
import math
from pathlib import Path
import sys
import time
import tomllib
from typing import Any, Callable, Mapping, Sequence

from .attodry import (
    AttoDryAuthorizationError,
    AttoDryDriver,
    load_attodry_dll,
)
from .config import StabilityConfig, load_config
from .models import CryostatState
from .stability import StabilityCriteria


@dataclass(frozen=True, slots=True)
class TemperatureCommissioningRequest:
    target_k: float
    max_delta_k: float
    tolerance_k: float
    stable_range_k: float
    dwell_s: float
    poll_interval_s: float
    timeout_s: float
    success_policy: str
    failure_policy: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Explicitly authorized smallest-temperature-movement tool. Use either "
            "--commissioning-config or every direct parameter option."
        )
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument(
        "--commissioning-config",
        type=Path,
        help="TOML file containing all per-attempt temperature parameters.",
    )
    parser.add_argument("--target-k", type=_positive_float)
    parser.add_argument("--max-delta-k", type=_positive_float)
    parser.add_argument("--tolerance-k", type=_positive_float)
    parser.add_argument("--stable-range-k", type=_nonnegative_float)
    parser.add_argument("--dwell-s", type=_positive_float)
    parser.add_argument("--poll-interval-s", type=_positive_float)
    parser.add_argument("--timeout-s", type=_positive_float)
    parser.add_argument(
        "--success-policy",
        choices=("hold-target", "restore-initial"),
    )
    parser.add_argument(
        "--failure-policy",
        choices=("disable-control", "restore-initial"),
    )
    parser.add_argument("--authorize-connection", action="store_true")
    parser.add_argument("--authorize-temperature-write", action="store_true")
    return parser


def _resolve_request(args: argparse.Namespace) -> TemperatureCommissioningRequest:
    direct_values = {
        "--target-k": args.target_k,
        "--max-delta-k": args.max_delta_k,
        "--tolerance-k": args.tolerance_k,
        "--stable-range-k": args.stable_range_k,
        "--dwell-s": args.dwell_s,
        "--poll-interval-s": args.poll_interval_s,
        "--timeout-s": args.timeout_s,
        "--success-policy": args.success_policy,
        "--failure-policy": args.failure_policy,
    }
    if args.commissioning_config is not None:
        provided = [name for name, value in direct_values.items() if value is not None]
        if provided:
            raise ValueError(
                "--commissioning-config cannot be combined with direct parameter "
                f"options: {', '.join(provided)}."
            )
        return _load_request_config(args.commissioning_config)

    missing = [name for name, value in direct_values.items() if value is None]
    if missing:
        raise ValueError(
            "Provide --commissioning-config or every direct parameter option; "
            f"missing: {', '.join(missing)}."
        )
    return TemperatureCommissioningRequest(
        target_k=args.target_k,
        max_delta_k=args.max_delta_k,
        tolerance_k=args.tolerance_k,
        stable_range_k=args.stable_range_k,
        dwell_s=args.dwell_s,
        poll_interval_s=args.poll_interval_s,
        timeout_s=args.timeout_s,
        success_policy=args.success_policy,
        failure_policy=args.failure_policy,
    )


def _load_request_config(path: Path) -> TemperatureCommissioningRequest:
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Cannot read commissioning config {path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Invalid commissioning TOML {path}: {exc}") from exc
    if set(document) != {"temperature_commissioning"}:
        raise ValueError(
            "Commissioning config must contain exactly [temperature_commissioning]."
        )
    table = document["temperature_commissioning"]
    if not isinstance(table, Mapping):
        raise ValueError("temperature_commissioning must be a TOML table.")
    expected = {
        "target_k",
        "max_delta_k",
        "tolerance_k",
        "stable_range_k",
        "dwell_s",
        "poll_interval_s",
        "timeout_s",
        "success_policy",
        "failure_policy",
    }
    missing = sorted(expected - set(table))
    unexpected = sorted(set(table) - expected)
    if missing or unexpected:
        details = []
        if missing:
            details.append(f"missing: {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected: {', '.join(unexpected)}")
        raise ValueError(
            "temperature_commissioning fields must match the template exactly "
            f"({'; '.join(details)})."
        )
    return TemperatureCommissioningRequest(
        target_k=_config_number(table["target_k"], "target_k", positive=True),
        max_delta_k=_config_number(
            table["max_delta_k"], "max_delta_k", positive=True
        ),
        tolerance_k=_config_number(
            table["tolerance_k"], "tolerance_k", positive=True
        ),
        stable_range_k=_config_number(
            table["stable_range_k"], "stable_range_k", positive=False
        ),
        dwell_s=_config_number(table["dwell_s"], "dwell_s", positive=True),
        poll_interval_s=_config_number(
            table["poll_interval_s"], "poll_interval_s", positive=True
        ),
        timeout_s=_config_number(table["timeout_s"], "timeout_s", positive=True),
        success_policy=_config_policy(
            table["success_policy"],
            "success_policy",
            ("hold-target", "restore-initial"),
        ),
        failure_policy=_config_policy(
            table["failure_policy"],
            "failure_policy",
            ("disable-control", "restore-initial"),
        ),
    )


def _config_number(value: Any, name: str, *, positive: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"temperature_commissioning.{name} must be a number.")
    converted = float(value)
    if not math.isfinite(converted) or (converted <= 0 if positive else converted < 0):
        qualifier = "finite and positive" if positive else "finite and non-negative"
        raise ValueError(f"temperature_commissioning.{name} must be {qualifier}.")
    return converted


def _config_policy(value: Any, name: str, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        choices = ", ".join(allowed)
        raise ValueError(
            f"temperature_commissioning.{name} must be one of: {choices}."
        )
    return str(value)


def main(argv: Sequence[str] | None = None) -> None:
    try:
        exit_code = run(argv)
    except KeyboardInterrupt:
        print(
            "Interrupted. Configured temperature recovery and close were attempted; "
            "manually verify the attoDRY temperature setpoint and control state.",
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
    _require_authorizations(args)
    request = _resolve_request(args)
    if request.timeout_s < request.dwell_s:
        raise ValueError("timeout-s must cover dwell-s.")

    config = load_config(args.config)
    if config.cryostat.dll_path is None:
        raise ValueError("Hardware cryostat DLL path is missing.")
    if not (
        config.cryostat.temperature_min_k
        <= request.target_k
        <= config.cryostat.temperature_max_k
    ):
        raise ValueError("Temperature target is outside configured limits.")
    temperature_stability = StabilityConfig(
        criteria=StabilityCriteria(
            tolerance=request.tolerance_k,
            stable_range=request.stable_range_k,
            dwell_s=request.dwell_s,
        ),
        poll_interval_s=request.poll_interval_s,
        wait_timeout_s=request.timeout_s,
    )
    config = replace(config, temperature_stability=temperature_stability)
    dll = dll_loader(config.cryostat.dll_path)
    driver = AttoDryDriver.from_config(
        config,
        dll=dll,
        connection_authorized=True,
        writes_authorized=True,
    )
    record: dict[str, object] = {
        "completed": False,
        "writes_authorized": True,
        "request": {
            "target_k": request.target_k,
            "max_delta_k": request.max_delta_k,
            "tolerance_k": request.tolerance_k,
            "stable_range_k": request.stable_range_k,
            "dwell_s": request.dwell_s,
            "poll_interval_s": request.poll_interval_s,
            "timeout_s": request.timeout_s,
            "success_policy": request.success_policy,
            "failure_policy": request.failure_policy,
        },
        "target_samples": [],
        "restore_samples": [],
        "recovery_actions": [],
    }
    connected = False
    mutation_attempted = False
    restoration_started = False
    initial_state: CryostatState | None = None

    try:
        driver.connect(monotonic=monotonic, sleeper=sleeper)
        connected = True
        initial_state = driver.read_state()
        record["initial_state"] = asdict(initial_state)
        sample_target_delta_k = abs(
            request.target_k - initial_state.sample_temperature_k
        )
        user_setpoint_target_delta_k = abs(
            request.target_k - initial_state.user_temperature_k
        )
        record["prewrite_check"] = {
            "initial_sample_temperature_k": initial_state.sample_temperature_k,
            "initial_user_temperature_k": initial_state.user_temperature_k,
            "sample_target_delta_k": sample_target_delta_k,
            "user_setpoint_target_delta_k": user_setpoint_target_delta_k,
            "max_delta_k": request.max_delta_k,
            "passed": sample_target_delta_k <= request.max_delta_k,
        }
        if sample_target_delta_k > request.max_delta_k:
            raise ValueError(
                "Requested sample-temperature movement exceeds the explicitly "
                "authorized max-delta-k."
            )

        mutation_attempted = True
        driver.set_temperature(
            request.target_k, monotonic=monotonic, sleeper=sleeper
        )
        driver.ensure_temperature_control(
            True, monotonic=monotonic, sleeper=sleeper
        )
        target_state = driver.wait_for_temperature(
            request.target_k,
            monotonic=monotonic,
            sleeper=sleeper,
            on_sample=_sample_recorder(
                record["target_samples"], wall_time=wall_time
            ),
        )
        record["target_stable_state"] = asdict(target_state)

        if request.success_policy == "restore-initial":
            restoration_started = True
            _restore_initial(
                driver,
                initial_state,
                wait_for_stability=True,
                samples=record["restore_samples"],
                actions=record["recovery_actions"],
                monotonic=monotonic,
                sleeper=sleeper,
                wall_time=wall_time,
            )
        record["final_state"] = asdict(driver.read_state())
        record["completed"] = True
    except BaseException as exc:
        record["error"] = str(exc)
        if connected:
            _record_failure_diagnostic(
                record,
                driver,
                wall_time=wall_time,
            )
        if (
            connected
            and mutation_attempted
            and not restoration_started
            and initial_state is not None
        ):
            try:
                if request.failure_policy == "disable-control":
                    driver.ensure_temperature_control(
                        False,
                        monotonic=monotonic,
                        sleeper=sleeper,
                    )
                    record["recovery_actions"].append(
                        "temperature_control_disabled_after_failure"
                    )
                else:
                    _restore_initial(
                        driver,
                        initial_state,
                        wait_for_stability=False,
                        samples=record["restore_samples"],
                        actions=record["recovery_actions"],
                        monotonic=monotonic,
                        sleeper=sleeper,
                        wall_time=wall_time,
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
        record["close_error"] = str(exc)
        record["disconnected"] = False
        print(json.dumps(record, ensure_ascii=False), flush=True)
        raise
    record["disconnected"] = True
    print(json.dumps(record, indent=2, ensure_ascii=False))
    return 0


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


def _restore_initial(
    driver: AttoDryDriver,
    initial_state: CryostatState,
    *,
    wait_for_stability: bool,
    samples: object,
    actions: object,
    monotonic: Callable[[], float],
    sleeper: Callable[[float], None],
    wall_time: Callable[[], float],
) -> None:
    if not isinstance(samples, list) or not isinstance(actions, list):
        raise TypeError("Temperature audit containers must be lists.")
    if not initial_state.temperature_control_enabled:
        driver.ensure_temperature_control(
            False, monotonic=monotonic, sleeper=sleeper
        )
        actions.append("temperature_control_restored_disabled")
    driver.set_temperature(
        initial_state.user_temperature_k,
        monotonic=monotonic,
        sleeper=sleeper,
    )
    actions.append("temperature_setpoint_restored")
    if initial_state.temperature_control_enabled:
        driver.ensure_temperature_control(
            True, monotonic=monotonic, sleeper=sleeper
        )
        actions.append("temperature_control_restored_enabled")
        if wait_for_stability:
            driver.wait_for_temperature(
                initial_state.user_temperature_k,
                monotonic=monotonic,
                sleeper=sleeper,
                on_sample=_sample_recorder(samples, wall_time=wall_time),
            )


def _sample_recorder(
    samples: object, *, wall_time: Callable[[], float]
) -> Callable[[CryostatState, float], None]:
    if not isinstance(samples, list):
        raise TypeError("Temperature sample container must be a list.")

    def record(state: CryostatState, elapsed_s: float) -> None:
        samples.append(
            {
                "captured_unix_s": wall_time(),
                "elapsed_s": elapsed_s,
                "state": asdict(state),
            }
        )

    return record


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


def _require_authorizations(args: argparse.Namespace) -> None:
    if not args.authorize_connection:
        raise AttoDryAuthorizationError(
            "attoDRY connection was not explicitly authorized."
        )
    if not args.authorize_temperature_write:
        raise AttoDryAuthorizationError(
            "attoDRY temperature setting writes were not explicitly authorized."
        )


def _positive_float(value: str) -> float:
    converted = float(value)
    if not math.isfinite(converted) or converted <= 0:
        raise argparse.ArgumentTypeError("must be finite and positive")
    return converted


def _nonnegative_float(value: str) -> float:
    converted = float(value)
    if not math.isfinite(converted) or converted < 0:
        raise argparse.ArgumentTypeError("must be finite and non-negative")
    return converted


if __name__ == "__main__":
    main()
