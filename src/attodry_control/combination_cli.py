"""Common daily entry for DC combination scans and file-only monitoring."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time

from .combination_registry import DEFAULT_RUN_CONFIG, resolve_monitor
from .combination_store import run_snapshot
from .combination_terminal import snapshot_text


def run(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Acquisition remains the existing tested DC service. Monitor never imports it.
    if argv and argv[0] in {"run", "simulate", "validate-config", "describe", "describe-hardware", "analyze", "export"}:
        command = argv[0]
        from .photoresponse_cli import main as photoresponse_main
        if command in {"describe", "describe-hardware"}:
            argv[0] = "validate-config"
            if "--config" not in argv and not any(arg.startswith("--config=") for arg in argv):
                argv += ["--config", DEFAULT_RUN_CONFIG]
        return photoresponse_main(argv, entrypoint="combination_cli")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "simulate", "describe-hardware"):
        commands.add_parser(name, help="Use the selected DC photoresponse TOML (supports --config)")
    monitor = commands.add_parser("monitor", help="Read SQLite; never query instruments")
    monitor.add_argument("--config", type=Path, default=Path(DEFAULT_RUN_CONFIG))
    monitor.add_argument("--database", type=Path)
    monitor.add_argument("--run-id")
    monitor.add_argument("--json", action="store_true")
    monitor.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    database, run_id = resolve_monitor(args.config, args.database, args.run_id)
    if not args.json:
        print("Database: " + str(database), flush=True)
    previous = None
    while True:
        snapshot = run_snapshot(database, run_id)
        # Age changes alone do not justify decoding/rendering large records.
        comparable = {k:v for k,v in snapshot.items() if k not in ("last_event_age_s", "phase_elapsed_s")}
        encoded = (json.dumps(comparable, ensure_ascii=False), int(snapshot.get("last_event_age_s") or 0) // 15)
        if encoded != previous:
            print(json.dumps(snapshot, ensure_ascii=False) if args.json else snapshot_text(snapshot), flush=True)
            previous = encoded
        if args.once or snapshot["status"] != "active":
            return 0
        time.sleep(1)


def main(argv=None):
    try:
        return run(argv)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
