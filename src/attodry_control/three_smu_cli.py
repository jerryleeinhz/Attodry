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
        child.add_argument("--hardware", required=True, type=Path)
        child.add_argument("--plan", required=True, type=Path)
        if command == "run":
            child.add_argument("--output-dir", required=True, type=Path)
            child.add_argument("--authorize-writes", action="store_true")
            child.add_argument(
                "--authorize-hold",
                action="store_true",
                help="Required in addition to TOML finish_action=hold.",
            )
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    hardware = load_three_smu_hardware(args.hardware)
    plan = load_three_smu_scan(args.plan)
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
    with ThreeSmuSession.open(
        hardware,
        plan,
        authorize_writes=True,
    ) as session:
        for _sample in session.run(output_dir=args.output_dir):
            pass
        print(session.last_run_dir)
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    raise SystemExit(run(argv))


if __name__ == "__main__":
    main()
