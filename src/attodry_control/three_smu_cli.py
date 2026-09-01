from __future__ import annotations

import argparse
from contextlib import ExitStack
from dataclasses import asdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from queue import SimpleQueue
import shutil
import time
from typing import Any, Callable, Sequence

from .keithley2400 import open_keithley2400_monitor
from .three_smu import ThreeSmuSample, ThreeSmuSession
from .three_smu_config import (
    FinishAction,
    SEMANTIC_ROLES,
    ScanPoint,
    ThreeSmuHardwareConfig,
    ThreeSmuOperationConfig,
    ThreeSmuScanPlan,
    active_smu_roles,
    load_three_smu_operation_config,
    validate_plan_targets,
)
from .three_smu_live import (
    ThreeSmuLiveSnapshot,
    format_live_three_smu_snapshot,
    monitor_problems,
)
from .three_smu_stream import LIVE_STREAM_URL, ThreeSmuLivePublisher


DEFAULT_CONFIG_PATH = Path("config/hardware.local.toml")
_WIDE_RUN_TABLE_MIN_COLUMNS = 96
_WIDE_RUN_TABLE_SEPARATOR = "─" * 94


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Three-SMU describe, query-only live monitor, and safe scans."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("describe", "run", "monitor-live"):
        child = subparsers.add_parser(command)
        child.add_argument(
            "--config",
            type=Path,
            help=(
                "Daily operation TOML (default: config/hardware.local.toml). "
                "The default local file is never committed."
            ),
        )
        if command == "monitor-live":
            child.add_argument(
                "--samples",
                type=_nonnegative_integer,
                default=0,
                help="Number of full three-SMU snapshots (0 means until Ctrl+C).",
            )
            child.add_argument(
                "--interval-s",
                type=_nonnegative_float,
                default=1.0,
                help="Delay between full snapshots in seconds (default: 1).",
            )
            child.add_argument(
                "--consume-status-queue",
                action="store_true",
                help=(
                    "Also query :SYST:ERR? for each SMU. This consumes one error-queue "
                    "entry per refresh and therefore remains opt-in."
                ),
            )
    return parser


def run(
    argv: Sequence[str] | None = None,
    *,
    input_fn: Callable[[str], str] = input,
    print_fn: Callable[..., None] = print,
    sleep: Callable[[float], None] = time.sleep,
    session_open: Callable[..., ThreeSmuSession] = ThreeSmuSession.open,
    monitor_resource_manager_factory: Callable[[], Any] | None = None,
    live_publisher_factory: Callable[[], ThreeSmuLivePublisher] = ThreeSmuLivePublisher,
) -> int:
    """Run the CLI while keeping connection paths injectable for fake tests."""

    args = build_parser().parse_args(argv)
    operation, hardware, plan, output_dir = _load_command_configuration(args)
    points = validate_plan_targets(hardware, plan)
    if args.command == "describe":
        _print_description(operation, hardware, plan, points, print_fn)
        return 0
    if args.command == "monitor-live":
        return _run_monitor_live(
            args,
            hardware,
            plan,
            monitor_resource_manager_factory=monitor_resource_manager_factory,
            print_fn=print_fn,
            sleep=sleep,
        )

    _print_scan_run_summary(
        operation,
        hardware,
        plan,
        points,
        output_dir,
        print_fn=print_fn,
    )
    _confirm_hold_outputs(plan, input_fn=input_fn)
    sample_queue: SimpleQueue[ThreeSmuSample] = SimpleQueue()
    displayed_samples = 0
    total_samples = len(points) * plan.samples_per_point
    publisher = live_publisher_factory()
    try:
        # This binds before QCoDeS/VISA opens. A local endpoint failure must never
        # leave a scan running without the requested live-plot data path.
        publisher.start(plan, total_samples=total_samples)
    except OSError as exc:
        raise SystemExit(
            f"Cannot start Three-SMU live stream at {LIVE_STREAM_URL}: {exc}. "
            "No QCoDeS/VISA resource was opened."
        ) from exc
    print_fn(f"Live Notebook endpoint: {publisher.endpoint} (memory samples; no extra queries)")

    def display_pending_samples() -> None:
        nonlocal displayed_samples
        while not sample_queue.empty():
            displayed_samples += 1
            _print_run_sample(
                sample_queue.get(),
                sample_number=displayed_samples,
                total_samples=total_samples,
                hardware=hardware,
                plan=plan,
                show_table_header=displayed_samples == 1,
                print_fn=print_fn,
            )

    def publish_formal_sample(sample: ThreeSmuSample) -> None:
        """Fan out one already-recorded sample without performing hardware I/O."""

        sample_queue.put(sample)
        publisher.publish_sample(sample)

    try:
        with session_open(
            hardware,
            plan,
            authorize_writes=True,
            authorize_status_consumption=True,
        ) as session:
            try:
                for _sample in session.run(
                    output_dir=output_dir,
                    run_name="" if operation is None else operation.run_name,
                    note="" if operation is None else operation.note,
                    config_path=None if operation is None else operation.config_path,
                    on_sample=publish_formal_sample,
                ):
                    display_pending_samples()
            finally:
                # The generator invokes the callback before raising for an unsafe
                # formal sample, so this also displays that retained problem sample.
                display_pending_samples()
        publisher.finish(status="completed")
        print_fn(session.last_run_dir)
    except BaseException as exc:
        publisher.finish(
            status="interrupted" if isinstance(exc, KeyboardInterrupt) else "rejected",
            error=f"{type(exc).__name__}: {exc}",
        )
        raise
    finally:
        publisher.close()
    return 0


def _load_command_configuration(
    args: argparse.Namespace,
) -> tuple[
    ThreeSmuOperationConfig | None,
    ThreeSmuHardwareConfig,
    ThreeSmuScanPlan,
    Path,
]:
    config_path = DEFAULT_CONFIG_PATH if args.config is None else args.config
    operation = load_three_smu_operation_config(config_path)
    return operation, operation.hardware, operation.plan, operation.output_directory


def _print_description(
    operation: ThreeSmuOperationConfig | None,
    hardware: ThreeSmuHardwareConfig,
    plan: ThreeSmuScanPlan,
    points: Sequence[ScanPoint],
    print_fn: Callable[..., None],
) -> None:
    print_fn(
        json.dumps(
            {
                "hardware": {
                    role: asdict(config) for role, config in hardware.by_role().items()
                },
                "active_roles": list(active_smu_roles(plan)),
                "off_roles": [
                    role for role in SEMANTIC_ROLES if role not in active_smu_roles(plan)
                ],
                "scan": asdict(plan),
                "generated_points": len(points),
                "formal_samples": len(points) * plan.samples_per_point,
                "hardware_opened": False,
                "config_path": None if operation is None else str(operation.config_path),
            },
            indent=2,
            default=lambda value: value.value,
        )
    )


def _print_scan_run_summary(
    operation: ThreeSmuOperationConfig | None,
    hardware: ThreeSmuHardwareConfig,
    plan: ThreeSmuScanPlan,
    points: Sequence[ScanPoint],
    output_dir: Path,
    *,
    print_fn: Callable[..., None],
) -> None:
    """Show the direct-run plan before the session opens any instrument."""

    print_fn("Three-SMU scan plan (no instrument has been opened):")
    print_fn(f"  config: {operation.config_path if operation else 'unknown'}")
    print_fn(f"  mode: {plan.mode.value}; points: {len(points)}; samples: {len(points) * plan.samples_per_point}")
    print_fn(f"  finish: {plan.finish_action.value}; output directory: {output_dir}")
    for role in SEMANTIC_ROLES:
        channel = plan.by_role()[role]
        if channel.role.value == "off":
            print_fn(f"  {role}: off; not connected; physical state remains unknown")
            continue
        config = hardware.require_role(role)
        print_fn(
            f"  {role}: {channel.role.value}; source={config.source_mode.value}; "
            f"max |V|={config.max_abs_voltage_v:g} V; "
            f"max |I|={config.max_abs_current_a:g} A"
        )
    print_fn(
        "This will connect the active SMUs, send setting writes, and consume their "
        ":SYST:ERR? error queues."
    )


def _confirm_hold_outputs(
    plan: ThreeSmuScanPlan,
    *,
    input_fn: Callable[[str], str],
) -> None:
    """Keep the separate confirmation for the exceptional hold-output cleanup."""

    if plan.finish_action is FinishAction.HOLD:
        hold_confirmation = _read_confirmation(
            input_fn,
            "finish_action=hold leaves outputs on. Type HOLD OUTPUTS to continue: ",
        )
        if hold_confirmation != "HOLD OUTPUTS":
            raise SystemExit(
                "Hold was not authorized; no QCoDeS/VISA resource was opened"
            )


def _print_run_sample(
    sample: ThreeSmuSample,
    *,
    sample_number: int,
    total_samples: int,
    hardware: ThreeSmuHardwareConfig,
    plan: ThreeSmuScanPlan,
    show_table_header: bool,
    print_fn: Callable[..., None],
) -> None:
    """Render a formal sample without querying or changing an instrument."""

    terminal_columns = shutil.get_terminal_size(fallback=(120, 24)).columns
    if terminal_columns < _WIDE_RUN_TABLE_MIN_COLUMNS:
        _print_compact_run_sample(
            sample,
            sample_number=sample_number,
            total_samples=total_samples,
            hardware=hardware,
            plan=plan,
            show_table_header=show_table_header,
            print_fn=print_fn,
        )
    else:
        _print_wide_run_sample(
            sample,
            sample_number=sample_number,
            total_samples=total_samples,
            hardware=hardware,
            plan=plan,
            show_table_header=show_table_header,
            print_fn=print_fn,
        )
    if sample.clean:
        return
    print_fn("! PROBLEM details (recorded formal sample; cleanup follows):")
    print_fn("  status/error queue:")
    for role in active_smu_roles(plan):
        timed_reading = sample.readings.get(role)
        if timed_reading is None:
            print_fn(f"    {role}: formal reading unavailable")
            continue
        reading = timed_reading.reading
        status = (
            reading.status
            if reading.status_query_consumed and reading.status is not None
            else "not consumed or unavailable"
        )
        print_fn(f"    {role}: {status}")
    print_fn("  problems:")
    for problem in sample.problems:
        print_fn(f"    - {problem}")


def _print_wide_run_sample(
    sample: ThreeSmuSample,
    *,
    sample_number: int,
    total_samples: int,
    hardware: ThreeSmuHardwareConfig,
    plan: ThreeSmuScanPlan,
    show_table_header: bool,
    print_fn: Callable[..., None],
) -> None:
    if show_table_header:
        print_fn("Three-SMU formal sample readbacks (memory FIFO; no extra queries):")
        print_fn(
            "Role        │ Setpoint rb    │ Voltage        │ Current        │ "
            "Resistance     │ Output"
        )
        print_fn(_WIDE_RUN_TABLE_SEPARATOR)
    print_fn(_run_sample_summary(sample, sample_number, total_samples, plan))
    for role in active_smu_roles(plan):
        timed_reading = sample.readings.get(role)
        if timed_reading is None:
            print_fn(_wide_run_table_row(role, "n/a", "n/a", "n/a", "n/a", "n/a"))
            continue
        reading = timed_reading.reading
        source_unit = _source_unit(hardware, role)
        print_fn(
            _wide_run_table_row(
                role,
                _format_engineering(reading.source_setpoint, source_unit),
                _format_engineering(reading.voltage_v, "V"),
                _format_engineering(reading.current_a, "A"),
                _format_engineering(reading.resistance_ohm, "Ω"),
                "ON" if reading.output_enabled else "OFF",
            )
        )
    print_fn(_WIDE_RUN_TABLE_SEPARATOR)


def _print_compact_run_sample(
    sample: ThreeSmuSample,
    *,
    sample_number: int,
    total_samples: int,
    hardware: ThreeSmuHardwareConfig,
    plan: ThreeSmuScanPlan,
    show_table_header: bool,
    print_fn: Callable[..., None],
) -> None:
    if show_table_header:
        print_fn("Three-SMU formal sample readbacks (compact terminal view):")
    print_fn(_run_sample_summary(sample, sample_number, total_samples, plan))
    for role in active_smu_roles(plan):
        timed_reading = sample.readings.get(role)
        if timed_reading is None:
            print_fn(f"  {role}: formal reading unavailable")
            continue
        reading = timed_reading.reading
        source_unit = _source_unit(hardware, role)
        print_fn(
            f"  {role}: src={_format_engineering(reading.source_setpoint, source_unit)}; "
            f"V={_format_engineering(reading.voltage_v, 'V')}; "
            f"I={_format_engineering(reading.current_a, 'A')}; "
            f"R={_format_engineering(reading.resistance_ohm, 'Ω')}; "
            f"{'ON' if reading.output_enabled else 'OFF'}"
        )


def _run_sample_summary(
    sample: ThreeSmuSample,
    sample_number: int,
    total_samples: int,
    plan: ThreeSmuScanPlan,
) -> str:
    outcome = "CLEAN" if sample.clean else "PROBLEM"
    return (
        f"[{sample_number}/{total_samples}]  repeat {sample.repeat_index + 1}/"
        f"{plan.samples_per_point} · segment {sample.segment} · "
        f"elapsed {sample.elapsed_s:.3f} s · {outcome}"
    )


def _wide_run_table_row(
    role: str,
    setpoint: str,
    voltage: str,
    current: str,
    resistance: str,
    output: str,
) -> str:
    return (
        f"{role:<11} │ {setpoint:>14} │ {voltage:>14} │ {current:>14} │ "
        f"{resistance:>14} │ {output:^6}"
    )


def _source_unit(hardware: ThreeSmuHardwareConfig, role: str) -> str:
    return "V" if hardware.require_role(role).source_mode.value == "voltage" else "A"


def _format_engineering(value: float | None, unit: str) -> str:
    """Format a finite readback in a compact, copy-friendly engineering unit."""

    if value is None:
        return "n/a"
    if not math.isfinite(value):
        return str(value)
    if value == 0:
        return f"0 {unit}"
    for scale, prefix in (
        (1e9, "G"),
        (1e6, "M"),
        (1e3, "k"),
        (1.0, ""),
        (1e-3, "m"),
        (1e-6, "µ"),
        (1e-9, "n"),
        (1e-12, "p"),
    ):
        if abs(value) >= scale:
            return f"{value / scale:.5g} {prefix}{unit}"
    return f"{value:.3e} {unit}"


def _read_confirmation(input_fn: Callable[[str], str], prompt: str) -> str:
    try:
        return input_fn(prompt).strip()
    except EOFError:
        return ""


def _run_monitor_live(
    args: argparse.Namespace,
    hardware: ThreeSmuHardwareConfig,
    plan: ThreeSmuScanPlan,
    *,
    monitor_resource_manager_factory: Callable[[], Any] | None,
    print_fn: Callable[..., None],
    sleep: Callable[[float], None],
) -> int:
    """Display sequential query-only snapshots; never configure or clean up SMUs."""

    if args.samples == 0 and args.interval_s <= 0:
        raise SystemExit("continuous monitor-live requires --interval-s greater than zero")
    manager_factory = (
        _open_visa_resource_manager
        if monitor_resource_manager_factory is None
        else monitor_resource_manager_factory
    )
    active_roles = active_smu_roles(plan)
    if not active_roles:
        raise SystemExit("monitor-live requires at least one fixed or sweep SMU role")
    manager = manager_factory()
    had_problem = False
    try:
        with ExitStack() as stack:
            monitors: dict[str, Any] = {}
            for role in active_roles:
                monitor = open_keithley2400_monitor(
                    role, hardware.require_role(role), manager
                )
                stack.callback(monitor.close)
                monitors[role] = monitor
            sample_index = 0
            while args.samples == 0 or sample_index < args.samples:
                readings = {
                    role: monitors[role].read_monitor(
                        consume_status_queue=args.consume_status_queue
                    )
                    for role in active_roles
                }
                problems = [
                    problem
                    for role in active_roles
                    for problem in monitor_problems(
                        role, hardware.require_role(role), readings[role]
                    )
                ]
                problems.extend(_duplicate_identity_problems(readings))
                snapshot = ThreeSmuLiveSnapshot(
                    sample_index=sample_index,
                    captured_at_utc=datetime.now(timezone.utc),
                    status_queue_consumed=args.consume_status_queue,
                    plan_roles={role: plan.by_role()[role].role for role in SEMANTIC_ROLES},
                    readings=readings,
                    problems=tuple(problems),
                )
                if sample_index:
                    print_fn("")
                print_fn(format_live_three_smu_snapshot(snapshot), flush=True)
                had_problem = had_problem or bool(problems)
                sample_index += 1
                if args.samples == 0 or sample_index < args.samples:
                    sleep(args.interval_s)
    except KeyboardInterrupt:
        print_fn("\nThree-SMU live monitor stopped.")
        return 130
    finally:
        manager.close()
    return 1 if had_problem else 0


def _duplicate_identity_problems(readings: dict[str, Any]) -> tuple[str, ...]:
    identities: dict[str, list[str]] = {}
    for role, reading in readings.items():
        identities.setdefault(reading.identity.strip(), []).append(role)
    return tuple(
        "identical *IDN? returned by " + ", ".join(roles)
        for identity, roles in identities.items()
        if identity and len(roles) > 1
    )


def _open_visa_resource_manager() -> Any:
    try:
        import pyvisa
    except ImportError as exc:
        raise SystemExit(
            "monitor-live requires PyVISA in the target hardware environment"
        ) from exc
    return pyvisa.ResourceManager()


def _nonnegative_integer(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if value < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return value


def _nonnegative_float(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be numeric") from exc
    if value < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return value


def main(argv: Sequence[str] | None = None) -> None:
    raise SystemExit(run(argv))


if __name__ == "__main__":
    main()
