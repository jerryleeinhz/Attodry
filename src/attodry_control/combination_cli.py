"""Combination simulation, gated hardware scans and file-only monitor."""
from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
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
        "describe-hardware", help="Offline validation of selected electrical, environment and optical axes")
    hardware_description.add_argument("--config", type=Path, default=Path("config/hardware.local.toml"))
    hardware_run = commands.add_parser(
        "run", help="Authorize and run REAL selected-module writes/status reads using the TOML")
    hardware_run.add_argument("--config", type=Path, default=Path("config/hardware.local.toml"))
    hardware_run.add_argument("--database", type=Path, help="Override project.database_path")
    hardware_run.add_argument("--run-id", help="Override combination_scan.run_id (default: auto)")
    hardware_run.add_argument("--authorize-combination", "--authorize-electrical-combination",
                              dest="authorize_electrical_combination", action="store_true")
    hardware_run.add_argument("--authorize-cryostat", action="store_true",
                              help="Legacy authorization flag; run already authorizes selected axes")
    hardware_run.add_argument("--confirm-xy-sine-disconnected", action="store_true")
    hardware_run.add_argument("--authorize-optical", action="store_true",
                              help="Authorize optical connections, settings and configured emission")
    hardware_run.add_argument("--confirm-optical-route", action="store_true",
                              help="Confirm the configured manual optical route and sample limits")
    hardware_run.add_argument("--json", action="store_true", help="Full structured summary/result")
    monitor = commands.add_parser("monitor", help="Read SQLite; never query instruments")
    monitor.add_argument("--config", type=Path, default=Path("config/hardware.local.toml"))
    monitor.add_argument("--database", type=Path, help="Override the latest registered database")
    monitor.add_argument("--run-id", help="Override the latest registered run ID")
    monitor.add_argument("--json", action="store_true", help="Full structured snapshots")
    monitor.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    if args.command in {"describe-hardware", "run"}:
        from .combination_hardware import load_hardware_combination, run_hardware_combination
        config = load_hardware_combination(args.config)
        if args.command == "describe-hardware":
            print(json.dumps({"plan": config.snapshot, "conditions": config.plan.conditions()},
                             ensure_ascii=False, indent=2))
            return 0
        if config.optical is not None and not (args.authorize_optical and args.confirm_optical_route):
            raise ValueError("Optical runs require --authorize-optical and --confirm-optical-route")
        from .combination_launch import resolve_launch, check_run_destination, launch_summary
        database, run_id = resolve_launch(config, args.database, args.run_id)
        check_run_destination(database, run_id)
        summary = launch_summary(config, database, run_id)
        from .combination_terminal import launch_text, snapshot_text
        from .combination_registry import register_run
        print(json.dumps(summary, ensure_ascii=False, indent=2, default=str)
              if args.json else launch_text(summary), flush=True)
        # The approved daily run command itself authorizes the selected modules.
        # Legacy flags remain accepted; backend guards and TOML wiring stay strict.
        explicit = (args.authorize_electrical_combination
                    and (config.lockin is None or config.reference_topology == "pem_xy_xx_sine"
                         or args.confirm_xy_sine_disconnected)
                    and (config.cryostat is None or args.authorize_cryostat))
        # Recheck the displayed, validated files immediately before connection.
        if hashlib.sha256(config.path.read_bytes()).hexdigest() != config.snapshot["config_sha256"]:
            raise ValueError("Configuration changed during launch; launch again to review it")
        from .photonics_lockin_config import PHOTONICS_REFERENCE_TOPOLOGIES
        safety_path = (Path(config.lockin.safety_path) if config.reference_topology in PHOTONICS_REFERENCE_TOPOLOGIES
                       else config.path.with_name("lockin_safety.toml"))
        if config.lockin is not None and hashlib.sha256(
                safety_path.read_bytes()).hexdigest() != \
                config.snapshot["hardware"]["lockin"]["safety_sha256"]:
            raise ValueError("Lock-in safety configuration changed during launch")
        config = replace(config, snapshot={**config.snapshot, "launch": {
            "summary": summary, "authorization_method": "explicit_flags" if explicit else "run_command"}})
        database.parent.mkdir(parents=True, exist_ok=True)
        from .hardware_lease import station_hardware_lease
        with station_hardware_lease(config.path), CombinationStore(database) as store:
            result = run_hardware_combination(
                config, store, run_id, authorize_hardware=True,
                confirm_xy_sine_disconnected=(config.lockin is not None
                    and not config.lockin.lockin_xy.sine_output_connected),
                authorize_cryostat=config.cryostat is not None,
                on_registered=lambda store, selected: register_run(config.path, store, selected),
                authorize_optical=args.authorize_optical,
                confirm_optical_route=args.confirm_optical_route)
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        else:
            snapshot = run_snapshot(database, run_id)
            print(snapshot_text({**snapshot, **result}), flush=True)
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
    from .combination_registry import resolve_monitor
    from .combination_terminal import snapshot_text
    database, run_id = resolve_monitor(args.config, args.database, args.run_id)
    # Resolve once. Do not silently switch when a newer run registers.
    if not args.json:
        print("Database: " + str(database), flush=True)
    previous = None
    while True:
        snapshot = {**run_snapshot(database, run_id), "database": str(database)}
        encoded = json.dumps(snapshot, ensure_ascii=False)
        if encoded != previous:
            print(encoded if args.json else snapshot_text(snapshot), flush=True)
            previous = encoded
        if args.once or snapshot["status"] != "active":
            return 0
        time.sleep(1)


def main() -> None:
    try:
        raise SystemExit(run())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except (OSError, ValueError, sqlite3.Error) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
