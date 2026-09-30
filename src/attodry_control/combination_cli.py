"""Combination simulation, gated hardware scans and file-only monitor."""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import time
import tomllib

from .combination_scan import AxisPoint, ScanAxis, CombinationPlan, run_simulated_combination
from .combination_store import CombinationStore, run_snapshot


def load_plan(path: str | Path) -> CombinationPlan:
    with Path(path).open("rb") as file:
        document = tomllib.load(file)
    if set(document) != {"combination_scan"}:
        raise ValueError("Offline plan must contain only [combination_scan]")
    table = document["combination_scan"]
    if not isinstance(table, dict):
        raise ValueError("combination_scan must be a table")
    allowed = {"backend", "axes", "samples_per_condition", "repeats", "run_name", "note"}
    if set(table) - allowed or table.get("backend") != "simulation":
        raise ValueError("Only backend='simulation' and documented scan keys are supported")
    axes = []
    raw_axes = table.get("axes", [])
    if not isinstance(raw_axes, list):
        raise ValueError("axes must be an array of tables")
    for axis in raw_axes:
        if not isinstance(axis, dict) or set(axis) != {"module", "points"}:
            raise ValueError("Each axis requires exactly module and points")
        if not isinstance(axis["module"], str) or not isinstance(axis["points"], list):
            raise ValueError("Axis requires a module name and point array")
        points = []
        for point in axis["points"]:
            if not isinstance(point, dict) or set(point) - {"values", "segment", "direction"}:
                raise ValueError("Point requires values and optional segment/direction")
            if not isinstance(point.get("values"), dict):
                raise ValueError("Point values must be a table")
            points.append(AxisPoint(point["values"], point.get("segment", "main"),
                                    point.get("direction", "ordered")))
        axes.append(ScanAxis(axis["module"], tuple(points)))
    plan = CombinationPlan(tuple(axes), table.get("samples_per_condition", 1),
                           table.get("repeats", 1), table.get("run_name", ""),
                           table.get("note", ""))
    plan.validate()
    return plan


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    describe = commands.add_parser("describe", help="Validate and show literal point order")
    describe.add_argument("--config", type=Path, required=True)
    simulate = commands.add_parser("simulate", help="Synthetic data only; no hardware entry point")
    simulate.add_argument("--config", type=Path, required=True)
    simulate.add_argument("--database", type=Path, required=True)
    simulate.add_argument("--run-id", required=True)
    simulate.add_argument("--resume", action="store_true")
    hardware_description = commands.add_parser(
        "describe-hardware", help="Offline validation of selected four-module hardware axes")
    hardware_description.add_argument("--config", type=Path, default=Path("config/hardware.local.toml"))
    hardware_run = commands.add_parser(
        "run", help="REAL selected-module writes and status consumption; separately authorized")
    hardware_run.add_argument("--config", type=Path, default=Path("config/hardware.local.toml"))
    hardware_run.add_argument("--database", type=Path, help="Override project.database_path")
    hardware_run.add_argument("--run-id", help="Override combination_scan.run_id (default: auto)")
    hardware_run.add_argument("--authorize-combination", "--authorize-electrical-combination",
                              dest="authorize_electrical_combination", action="store_true")
    hardware_run.add_argument("--authorize-cryostat", action="store_true",
                              help="Additionally authorize selected temperature/field axis writes")
    hardware_run.add_argument("--confirm-xy-sine-disconnected", action="store_true")
    monitor = commands.add_parser("monitor", help="Read SQLite; never query instruments")
    monitor.add_argument("--database", type=Path, required=True)
    monitor.add_argument("--run-id", required=True)
    monitor.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.command in {"describe-hardware", "run"}:
        from .combination_hardware import load_hardware_combination, run_hardware_combination
        config = load_hardware_combination(args.config)
        if args.command == "describe-hardware":
            print(json.dumps({"plan": config.snapshot, "conditions": config.plan.conditions()},
                             ensure_ascii=False, indent=2))
            return 0
        from .combination_launch import resolve_launch, check_run_destination, launch_summary
        database, run_id = resolve_launch(config, args.database, args.run_id)
        check_run_destination(database, run_id)
        summary = launch_summary(config, database, run_id)
        print("REAL combination scan - review requested settings:", flush=True)
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str), flush=True)
        explicit = (args.authorize_electrical_combination
                    and (config.lockin is None or args.confirm_xy_sine_disconnected)
                    and (config.cryostat is None or args.authorize_cryostat))
        if not explicit:
            supplied_flags = (args.authorize_electrical_combination or
                              args.confirm_xy_sine_disconnected or args.authorize_cryostat)
            if supplied_flags or not sys.stdin.isatty():
                missing = []
                if not args.authorize_electrical_combination:
                    missing.append("--authorize-combination")
                if config.lockin is not None and not args.confirm_xy_sine_disconnected:
                    missing.append("--confirm-xy-sine-disconnected")
                if config.cryostat is not None and not args.authorize_cryostat:
                    missing.append("--authorize-cryostat")
                raise ValueError("Explicit/non-interactive launch requires " + " ".join(missing))
            prompt = "Type RUN to authorize the displayed hardware writes/status reads"
            if config.lockin is not None:
                prompt += " and confirm XY SINE OUT is physically disconnected"
            try:
                confirmed = input(prompt + ": ") == "RUN"
            except EOFError:
                confirmed = False
            if not confirmed:
                print("Cancelled before database creation or hardware connection.")
                return 0
        # Confirmation applies to the displayed, already validated configuration.
        if hashlib.sha256(config.path.read_bytes()).hexdigest() != config.snapshot["config_sha256"]:
            raise ValueError("Configuration changed during confirmation; launch again to review it")
        if config.lockin is not None and hashlib.sha256(
                config.path.with_name("lockin_safety.toml").read_bytes()).hexdigest() != \
                config.snapshot["hardware"]["lockin"]["safety_sha256"]:
            raise ValueError("Lock-in safety configuration changed during confirmation")
        config = replace(config, snapshot={**config.snapshot, "launch": {
            "summary": summary, "authorization_method": "explicit_flags" if explicit else "interactive_RUN"}})
        database.parent.mkdir(parents=True, exist_ok=True)
        with CombinationStore(database) as store:
            result = run_hardware_combination(
                config, store, run_id, authorize_hardware=True,
                confirm_xy_sine_disconnected=config.lockin is not None,
                authorize_cryostat=config.cryostat is not None)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "completed" else 2
    if args.command == "describe":
        plan = load_plan(args.config)
        print(json.dumps({"plan": plan.snapshot(), "conditions": plan.conditions()}, indent=2))
        return 0
    if args.command == "simulate":
        plan = load_plan(args.config)  # Validation before database creation.
        with CombinationStore(args.database) as store:
            result = run_simulated_combination(plan, store, args.run_id, resume=args.resume)
        print(json.dumps(result, indent=2))
        return 0 if result["status"] == "completed" else 2
    while True:
        snapshot = run_snapshot(args.database, args.run_id)
        print(json.dumps(snapshot, ensure_ascii=False), flush=True)
        if args.once or snapshot["status"] != "active":
            return 0
        time.sleep(1)


def main() -> None:
    try:
        raise SystemExit(run())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except (OSError, ValueError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
