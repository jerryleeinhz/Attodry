from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import time
from typing import Callable, Sequence
from uuid import uuid4

from .attodry import (
    FIELD_COMMAND_ACK_TIMEOUT_S,
    FIELD_SETPOINT_READBACK_TOLERANCE_T,
    AttoDryAuthorizationError,
    AttoDryDriver,
    AttoDryError,
    load_attodry_dll,
)
from .config import FieldEndPolicy, load_magnetic_field_operation_config
from .field_audit import JsonlEventWriter
from .magnetic_field import execute_ordered_field_points
from .models import CryostatState, VectorField
from .safety import CONFIRMED_FIELD_TOLERANCE_MAX_T, validate_vector_field


DEFAULT_CONFIG_PATH = Path("config/hardware.local.toml")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run a standalone attoDRY X/Z magnetic-field operation. This command "
            "can connect to the controller and write field settings only when its "
            "explicit authorization flags are present."
        )
    )
    commands = parser.add_subparsers(dest="command", required=True)
    single = commands.add_parser(
        "single-target",
        help="Run exactly one configured target and then verify monitored zero.",
    )
    scan = commands.add_parser(
        "scan",
        help="Run the configured ordered point list, preserving duplicates.",
    )
    for command in (single, scan):
        command.add_argument(
            "--config",
            type=Path,
            default=DEFAULT_CONFIG_PATH,
            help=f"Unified hardware TOML (default: {DEFAULT_CONFIG_PATH}).",
        )
        command.add_argument(
            "--authorize-connection",
            action="store_true",
            help="Authorize one attoDRY begin/connect/read/disconnect session.",
        )
        command.add_argument(
            "--authorize-field-writes",
            action="store_true",
            help="Authorize field-control, component-setpoint, and cleanup writes.",
        )
    scan.add_argument(
        "--authorize-ordered-field-scan",
        action="store_true",
        help="Separately authorize every point in the configured ordered list.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    try:
        exit_code = run(argv)
    except KeyboardInterrupt:
        print(
            "Interrupted. Best-effort monitored zero and disconnect were attempted; "
            "follow the emitted manual-verification status.",
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
    config_path = args.config.resolve()
    config = load_magnetic_field_operation_config(config_path)
    points = config.run.points
    if args.command == "single-target" and len(points) != 1:
        raise ValueError(
            "single-target requires exactly one [magnetic_field_run].points entry."
        )
    if not args.authorize_connection:
        raise AttoDryAuthorizationError(
            "attoDRY connection is not authorized; add --authorize-connection only "
            "after the commissioning gate is approved."
        )
    if not args.authorize_field_writes:
        raise AttoDryAuthorizationError(
            "Magnetic-field writes are not authorized; add "
            "--authorize-field-writes only after the commissioning gate is approved."
        )
    if args.command == "scan" and not args.authorize_ordered_field_scan:
        raise AttoDryAuthorizationError(
            "The ordered field scan needs separate authorization; add "
            "--authorize-ordered-field-scan only after every listed point is approved."
        )

    cryostat = config.cryostat
    if (
        cryostat.com_port is None
        or cryostat.dll_path is None
        or cryostat.device_type is None
        or cryostat.connection_timeout_s is None
    ):
        raise ValueError("Hardware cryostat DLL configuration is incomplete.")

    output_directory = (config_path.parent / config.run.output_directory).resolve()
    output_directory.mkdir(parents=True, exist_ok=True)
    progress_path = output_directory / (
        f"{_utc_label(wall_time())}_{config.run.run_name}_{args.command}_"
        f"{uuid4().hex[:8]}.jsonl"
    )
    writer = JsonlEventWriter(progress_path, create=True, wall_time=wall_time)
    completed_points = 0
    audit_failure: BaseException | None = None

    def emit(event: dict[str, object]) -> None:
        nonlocal audit_failure, completed_points
        try:
            writer.append(event)
        except BaseException as exc:
            if audit_failure is None:
                audit_failure = exc
            raise
        if event.get("event") == "point_completed":
            completed_points += 1

    stability = config.magnet.stability
    try:
        emit(
            {
                "event": "run_started",
                "command": args.command,
                "config_path": str(config_path),
                "config_sha256": _sha256(config_path),
                "source": _source_provenance(),
                "run_metadata": {
                    "run_name": config.run.run_name,
                    "note": config.run.note,
                },
                "authorization_scope": {
                    "connection": bool(args.authorize_connection),
                    "field_writes": bool(args.authorize_field_writes),
                    "ordered_field_scan": bool(
                        getattr(args, "authorize_ordered_field_scan", False)
                    ),
                },
                "cryostat_interface": {
                    "backend": cryostat.backend,
                    "com_port": cryostat.com_port,
                    "dll_path": str(cryostat.dll_path),
                    "device_type": cryostat.device_type,
                    "connection_timeout_s": cryostat.connection_timeout_s,
                },
                "ordered_points": [asdict(point) for point in points],
                "max_step_t": config.run.max_step_t,
                "limits": asdict(config.magnet.limits),
                "field_stability": {
                    "tolerance_t": stability.criteria.tolerance,
                    "stable_range_t": stability.criteria.stable_range,
                    "stable_dwell_s": stability.criteria.dwell_s,
                    "minimum_samples": stability.criteria.minimum_samples,
                    "poll_interval_s": stability.poll_interval_s,
                    "wait_timeout_s": stability.wait_timeout_s,
                },
                "driver_field_protocol": {
                    "setpoint_readback_tolerance_t": (
                        FIELD_SETPOINT_READBACK_TOLERANCE_T
                    ),
                    "command_ack_timeout_s": FIELD_COMMAND_ACK_TIMEOUT_S,
                },
                "normal_end_field_policy": (
                    config.cleanup.normal_end_field_policy.value
                ),
                "exception_field_policy": config.cleanup.exception_field_policy.value,
            }
        )
    except BaseException:
        writer.close()
        raise

    driver: AttoDryDriver | None = None
    connection_attempted = False
    connected = False
    disconnected = True
    zero_attempted = False
    zero_verified = False
    normal_operation_finished = False
    communication_uncertain = False
    primary_error: BaseException | None = None
    outcome = "running"

    try:
        dll = dll_loader(cryostat.dll_path)
        # Temperature stability is unused by this standalone field-only path.
        driver = AttoDryDriver(
            dll=dll,
            com_port=cryostat.com_port,
            device_type=cryostat.device_type,
            connection_timeout_s=cryostat.connection_timeout_s,
            temperature_min_k=cryostat.temperature_min_k,
            temperature_max_k=cryostat.temperature_max_k,
            limits=config.magnet.limits,
            field_stability=config.magnet.stability,
            temperature_stability=config.magnet.stability,
            connection_authorized=True,
            writes_authorized=True,
        )
        connection_attempted = True
        disconnected = False
        driver.connect(monotonic=monotonic, sleeper=sleeper)
        connected = True
        emit({"event": "connection_confirmed"})

        execute_ordered_field_points(
            driver,
            points,
            config.run.max_step_t,
            on_event=emit,
            monotonic=monotonic,
            sleeper=sleeper,
        )

        normal_zero_required = (
            args.command == "single-target"
            or config.cleanup.normal_end_field_policy is FieldEndPolicy.ZERO
        )
        if normal_zero_required:
            zero_attempted = True
            zero_state = _request_monitored_zero(
                driver,
                emit=emit,
                monotonic=monotonic,
                sleeper=sleeper,
            )
            zero_verified = True
            _emit_best_effort(
                emit,
                {"event": "normal_zero_verified", "state": asdict(zero_state)},
            )
            if audit_failure is not None:
                raise audit_failure
        else:
            hold_state = driver.read_state()
            _validate_hold_state(driver, hold_state, points[-1])
            emit({"event": "normal_hold_verified", "state": asdict(hold_state)})
        normal_operation_finished = True
    except BaseException as exc:
        primary_error = exc
        outcome = "interrupted" if isinstance(exc, KeyboardInterrupt) else "rejected"
        communication_uncertain = (
            exc is not audit_failure and _communication_is_uncertain(exc)
        )
        _emit_best_effort(
            emit,
            {
                "event": "operation_failed",
                "outcome": outcome,
                "error_type": type(exc).__name__,
                "error": str(exc),
            },
        )

        # If normal zeroing itself was interrupted or failed before verification,
        # make one independent best-effort exception-cleanup attempt.  The nested
        # handler below bounds this to one retry.
        if connected and driver is not None and not zero_verified:
            zero_attempted = True
            try:
                zero_state = _request_monitored_zero(
                    driver,
                    emit=emit,
                    monotonic=monotonic,
                    sleeper=sleeper,
                )
                zero_verified = not communication_uncertain
                _emit_best_effort(
                    emit,
                    {
                        "event": "exception_zero_verified",
                        "credited_as_verified": zero_verified,
                        "state": asdict(zero_state),
                    },
                )
            except BaseException as cleanup_error:
                communication_uncertain = communication_uncertain or (
                    _communication_is_uncertain(cleanup_error)
                )
                zero_verified = False
                exc.add_note(f"Magnetic-field zero cleanup also failed: {cleanup_error}")
                _emit_best_effort(
                    emit,
                    {
                        "event": "exception_zero_failed",
                        "error_type": type(cleanup_error).__name__,
                        "error": str(cleanup_error),
                    },
                )
    finally:
        if connected and driver is not None:
            try:
                driver.close()
                disconnected = True
                _emit_best_effort(emit, {"event": "disconnect_confirmed"})
            except BaseException as close_error:
                disconnected = False
                communication_uncertain = communication_uncertain or (
                    _communication_is_uncertain(close_error)
                )
                if primary_error is None:
                    primary_error = close_error
                else:
                    primary_error.add_note(f"attoDRY close also failed: {close_error}")
                _emit_best_effort(
                    emit,
                    {
                        "event": "disconnect_failed",
                        "error_type": type(close_error).__name__,
                        "error": str(close_error),
                    },
                )
        elif connection_attempted:
            # A connection attempt that did not return cannot prove disconnect.
            disconnected = False

    if audit_failure is not None and primary_error is None:
        primary_error = audit_failure
    elif (
        audit_failure is not None
        and primary_error is not None
        and audit_failure is not primary_error
    ):
        primary_error.add_note(f"Magnetic-field audit also failed: {audit_failure}")

    if primary_error is None and normal_operation_finished and disconnected:
        outcome = "completed"
    elif outcome == "running":
        outcome = (
            "interrupted"
            if isinstance(primary_error, KeyboardInterrupt)
            else "rejected"
        )

    last_confirmed_state = (
        None
        if driver is None or driver.last_confirmed_state is None
        else asdict(driver.last_confirmed_state)
    )
    if communication_uncertain:
        # A later apparent readback cannot turn a communication-failure run
        # into a software-verified zero-field claim.
        zero_verified = False
    zero_required = (
        args.command == "single-target"
        or config.cleanup.normal_end_field_policy is FieldEndPolicy.ZERO
        or primary_error is not None
    )
    manual_verification_required = (
        communication_uncertain
        or audit_failure is not None
        or not disconnected
        or last_confirmed_state is None
        or (zero_required and not zero_verified)
    )
    summary: dict[str, object] = {
        "event": "run_finished",
        "command": args.command,
        "outcome": outcome,
        "completed": outcome == "completed",
        "completed_points": completed_points,
        "requested_points": len(points),
        "zero_attempted": zero_attempted,
        "zero_verified": zero_verified,
        "manual_verification_required": manual_verification_required,
        "disconnected": disconnected,
        "last_confirmed_state": last_confirmed_state,
        "progress_jsonl": str(progress_path),
        "audit_complete": audit_failure is None,
    }
    if primary_error is not None:
        summary["error_type"] = type(primary_error).__name__
        summary["error"] = str(primary_error)
    try:
        emit(summary)
    except BaseException as audit_error:
        if primary_error is None:
            primary_error = audit_error
            summary.update(
                {
                    "outcome": "rejected",
                    "completed": False,
                    "manual_verification_required": True,
                    "audit_complete": False,
                    "error_type": type(audit_error).__name__,
                    "error": str(audit_error),
                }
            )
        else:
            primary_error.add_note(f"Terminal audit append also failed: {audit_error}")
            summary["audit_complete"] = False
    finally:
        writer.close()
    print(json.dumps(summary, ensure_ascii=False), flush=True)
    if primary_error is not None:
        raise primary_error
    return 0


def _request_monitored_zero(
    driver: AttoDryDriver,
    *,
    emit: Callable[[dict[str, object]], None],
    monotonic: Callable[[], float],
    sleeper: Callable[[float], None],
) -> CryostatState:
    # Audit I/O must never interrupt a safety cleanup after it has begun.  The
    # caller's emitter latches the first failure so the run is still rejected.
    _emit_best_effort(emit, {"event": "zero_started"})

    def sample(
        state: CryostatState,
        elapsed_s: float,
        phase: str,
        waypoint_index: int | None,
    ) -> None:
        _emit_best_effort(
            emit,
            {
                "event": "field_sample",
                "phase": phase,
                "point_index": None,
                "waypoint_index": waypoint_index,
                "elapsed_s": elapsed_s,
                "state": asdict(state),
            },
        )

    driver.ensure_field_control(
        True,
        monotonic=monotonic,
        sleeper=sleeper,
        on_sample=sample,
    )
    state = driver.request_zero_field(
        monotonic=monotonic,
        sleeper=sleeper,
        on_sample=sample,
    )
    tolerance = driver.field_stability.criteria.tolerance
    if tolerance is None or tolerance > CONFIRMED_FIELD_TOLERANCE_MAX_T:
        raise AttoDryError(
            "Monitored zero requires a configured field tolerance no greater "
            f"than {CONFIRMED_FIELD_TOLERANCE_MAX_T:g} T."
        )
    if state.field.magnitude_t > tolerance:
        raise AttoDryError("Actual field is outside configured zero tolerance.")
    if state.field_setpoint.magnitude_t > tolerance:
        raise AttoDryError("Field setpoint is outside configured zero tolerance.")
    return state


def _validate_hold_state(
    driver: AttoDryDriver,
    state: CryostatState,
    expected: VectorField,
) -> None:
    validate_vector_field(state.field, driver.limits)
    validate_vector_field(state.field_setpoint, driver.limits)
    if state.error_code:
        raise AttoDryError(
            f"attoDRY reported error code {state.error_code} during hold verification."
        )
    if not state.field_control_enabled:
        raise AttoDryError("Field control is not confirmed during hold verification.")
    if not (
        math.isclose(
            state.field_setpoint.bx_t,
            expected.bx_t,
            rel_tol=0.0,
            abs_tol=FIELD_SETPOINT_READBACK_TOLERANCE_T,
        )
        and math.isclose(
            state.field_setpoint.bz_t,
            expected.bz_t,
            rel_tol=0.0,
            abs_tol=FIELD_SETPOINT_READBACK_TOLERANCE_T,
        )
    ):
        raise AttoDryError("Held field setpoint no longer matches the final target.")
    tolerance = driver.field_stability.criteria.tolerance
    if tolerance is None or (
        abs(state.field.bx_t - expected.bx_t) > tolerance
        or abs(state.field.bz_t - expected.bz_t) > tolerance
        or (
            expected.magnitude_t == 0.0
            and state.field.magnitude_t > tolerance
        )
    ):
        raise AttoDryError(
            "Actual field moved outside the configured target tolerance during "
            "hold verification."
        )


def _communication_is_uncertain(exc: BaseException) -> bool:
    # Driver errors include DLL failures, timeouts, malformed/non-finite
    # readbacks, device error flags, and unexpected state changes.  None permit
    # a later apparent readback to erase the uncertainty.
    return isinstance(exc, (AttoDryError, OSError))


def _emit_best_effort(
    emit: Callable[[dict[str, object]], None],
    event: dict[str, object],
) -> None:
    try:
        emit(event)
    except BaseException:
        pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_provenance() -> dict[str, object]:
    repository = Path(__file__).resolve().parents[2]
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repository,
            check=True,
            capture_output=True,
            text=True,
            timeout=5.0,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=normal"],
                cwd=repository,
                check=True,
                capture_output=True,
                text=True,
                timeout=5.0,
            ).stdout
        )
    except (OSError, subprocess.SubprocessError):
        return {"git_commit": None, "git_dirty": None}
    return {"git_commit": commit or None, "git_dirty": dirty}


def _utc_label(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y%m%dT%H%M%SZ")


if __name__ == "__main__":
    main()
