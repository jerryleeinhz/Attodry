"""Offline-only combination plan/simulator and read-only SQLite monitor."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
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
    monitor = commands.add_parser("monitor", help="Read SQLite; never query instruments")
    monitor.add_argument("--database", type=Path, required=True)
    monitor.add_argument("--run-id", required=True)
    monitor.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
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
