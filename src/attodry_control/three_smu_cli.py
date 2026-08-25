from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
from typing import Sequence

from .three_smu import ThreeSmuSession
from .three_smu_config import (
    FinishAction,
    generate_scan_points,
    load_three_smu_hardware,
    load_three_smu_operation_config,
    load_three_smu_scan,
    validate_plan_targets,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline describe and explicitly authorized Three-SMU scans."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("describe", "run"):
        child = subparsers.add_parser(command)
        child.add_argument(
            "--config",
            type=Path,
            help="Single daily hardware.local.toml containing Three-SMU tables.",
        )
        child.add_argument("--hardware", type=Path, help=argparse.SUPPRESS)
        child.add_argument("--plan", type=Path, help=argparse.SUPPRESS)
        if command == "run":
            child.add_argument("--output-dir", type=Path, help=argparse.SUPPRESS)
            child.add_argument("--authorize-writes", action="store_true")
            child.add_argument(
                "--authorize-status-consumption",
                action="store_true",
                help=(
                    "Authorize reading the Keithley error queue; this query "
                    "consumes queued status entries."
                ),
            )
            child.add_argument(
                "--authorize-hold",
                action="store_true",
                help="Required in addition to TOML finish_action=hold.",
            )
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    operation = None
    if args.config is not None:
        if (
            args.hardware is not None
            or args.plan is not None
            or getattr(args, "output_dir", None) is not None
        ):
            raise SystemExit("--config cannot be combined with legacy --hardware/--plan/--output-dir")
        operation = load_three_smu_operation_config(args.config)
        hardware = operation.hardware
        plan = operation.plan
        output_dir = operation.output_directory
    else:
        if args.hardware is None or args.plan is None:
            raise SystemExit("Use --config, or both legacy --hardware and --plan")
        hardware = load_three_smu_hardware(args.hardware)
        plan = load_three_smu_scan(args.plan)
        output_dir = getattr(args, "output_dir", None)
        if args.command == "run" and output_dir is None:
            raise SystemExit("Legacy run requires --output-dir")
    points = validate_plan_targets(hardware, plan)
    if args.command == "describe":
        print(
            json.dumps(
                {
                    "hardware": {
                        role: asdict(config)
                        for role, config in hardware.by_role().items()
                    },
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
        return 0
    if plan.finish_action is FinishAction.HOLD and not args.authorize_hold:
        raise SystemExit(
            "finish_action=hold requires the additional --authorize-hold flag"
        )
    if not args.authorize_writes:
        raise SystemExit(
            "Real Three-SMU connection and writes require --authorize-writes"
        )
    if not args.authorize_status_consumption:
        raise SystemExit(
            "Real Three-SMU scans require --authorize-status-consumption because "
            "the Keithley error-queue query consumes status entries"
        )
    with ThreeSmuSession.open(
        hardware,
        plan,
        authorize_writes=True,
        authorize_status_consumption=True,
    ) as session:
        for _sample in session.run(
            output_dir=output_dir,
            run_name="" if operation is None else operation.run_name,
            note="" if operation is None else operation.note,
            config_path=None if operation is None else operation.config_path,
        ):
            pass
        print(session.last_run_dir)
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    raise SystemExit(run(argv))


if __name__ == "__main__":
    main()
