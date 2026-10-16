"""Daily modular Combination service using the existing audit/monitor contract."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from uuid import uuid4

from .audit_values import audit_safe
from .combination_registry import DEFAULT_RUN_CONFIG, register_run
from .modular_config import load_modular_config
from .nkt_config import NktError


def _record_stem(directory, run_name, *, now=None, token=None, max_path=None):
    """Keep every final outcome path within the Windows MAX_PATH budget.

    The full experiment name remains in the archived plan. Only the human part
    of the file name may be shortened; the UTC label and unique token remain.
    Count UTF-16 code units because a non-BMP character occupies two on Windows.
    """
    timestamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    token = token or uuid4().hex[:8]
    name = re.sub(r"[^\w.-]+", "_", run_name)[:80]
    if max_path is None and os.name == "nt":
        max_path = 259 # Excludes the terminating NUL of the 260-character API limit.
    if max_path is not None:
        def units(value):
            return len(str(value).encode("utf-16-le")) // 2
        # _interrupted.json is longer than _completed.json or _rejected.json,
        # and also covers the journal's shorter .jsonl extension.
        fixed = units(Path(directory).resolve()) + 1 + units(timestamp + "__" + token + "_interrupted.json")
        budget = max_path - fixed
        if budget < 1:
            raise NktError("Output directory is too long for a unique run record; choose a shorter output_directory")
        while name and units(name) > budget:
            name = name[:-1]
        if not name:
            raise NktError("Output directory leaves no room for the run name; choose a shorter output_directory")
    return timestamp + "_" + name + "_" + token


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "simulate", "validate-config", "describe", "describe-hardware"))
    parser.add_argument("--config", default=DEFAULT_RUN_CONFIG)
    parser.add_argument("--database", type=Path)
    parser.add_argument("--resume-run")
    parser.add_argument("--json", action="store_true")
    # Existing operator commands continue to work. Explicit run remains the
    # acquisition authorization; these legacy flags cannot bypass configuration.
    for option in ("authorize-connect", "authorize-writes", "confirm-route", "authorize-environment", "authorize-smu"):
        parser.add_argument("--" + option, action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv=None, *, entrypoint="combination_cli"):
    args = _parser().parse_args(argv)
    try:
        return _execute(args, entrypoint)
    except (Exception, KeyboardInterrupt) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 2


def _execute(args, entrypoint):
    from .modular_runtime import RuntimeConfig
    source = Path(args.config).resolve()
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    config = RuntimeConfig(load_modular_config(source))
    if args.database is not None:
        config.database_path = args.database.resolve()
    if hashlib.sha256(source.read_bytes()).hexdigest() != source_hash:
        raise NktError("Measurement TOML changed while loading; review and start a new run")
    if args.command in {"validate-config", "describe", "describe-hardware"}:
        print(json.dumps(config.snapshot(), ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    if args.command == "simulate" and config.hardware_mode:
        raise NktError("simulate rejects hardware backends")
    if args.command == "run":
        if not config.hardware_mode:
            raise NktError("Use simulate for simulation configuration")
        config.require_run()
    if args.resume_run and not config.database_path:
        raise NktError("Resume requires SQLite storage")
    from .photoresponse_storage import PhotoStore, run_lock
    from .photoresponse_cli import provenance
    from .combination_terminal import PhotoresponseProgressPrinter, launch_text, snapshot_text
    from .combination_store import run_snapshot
    from .modular_runner import run_modular

    directory = config.output_directory
    stem = _record_stem(directory, config.plan.run_name)
    directory.mkdir(parents=True, exist_ok=True)
    journal_path = directory / f"{stem}.jsonl"
    lock_path = Path(str(config.database_path) + ".lock")
    invocation = {"command": args.command, "entrypoint": entrypoint,
        "authorization_source": "run_command" if args.command == "run" else "simulation",
        "config_path": str(source), "config_sha256": source_hash,
        "selected_modules": list(config.plan.order)}
    store = None
    with run_lock(lock_path), journal_path.open("x", encoding="utf-8") as journal:
        try:
            store = PhotoStore(config.database_path)
            run_id, attempt = store.start(config, resume=args.resume_run)
            register_run(source, store, run_id)
            progress = PhotoresponseProgressPrinter(config.snapshot(), enabled=not args.json)
            if not args.json:
                print(launch_text({"measurement_type": "dc_photoresponse", "measurement_config": config.snapshot(),
                    "simulated": not config.hardware_mode, "run_id": run_id,
                    "database": str(store.path.resolve()), "config": str(source),
                    "config_sha256": source_hash}), flush=True)

            def event(value):
                value = {"captured_at_utc": datetime.now(timezone.utc).isoformat(), **value}
                journal.write(json.dumps(audit_safe(value), ensure_ascii=False, allow_nan=False) + "\n")
                journal.flush()
                store.event(value)
                progress(value)

            software = provenance()
            event({"phase": "software", **software})
            event({"phase": "invocation", **invocation})
            record = run_modular(config, event, attempt_index=attempt)
            record["software"], record["journal_path"] = software, str(journal_path)
            record["invocation"] = invocation
            store.finalize(record)
            outcome = record["outcome"]
            path = directory / f"{stem}_{outcome}.json"
            with path.open("x", encoding="utf-8") as stream:
                json.dump(audit_safe(record), stream, ensure_ascii=False, indent=2, allow_nan=False)
            event({"phase": "run_end", "outcome": outcome, "record_path": str(path)})
            if not args.json:
                print(snapshot_text(run_snapshot(store.path, run_id)), flush=True)
        finally:
            if store:
                store.close()
    if args.json:
        print(json.dumps(audit_safe(record), ensure_ascii=False, allow_nan=False))
    else:
        label = "RUN" if config.hardware_mode else "SIMULATION"
        print(f"{label} {outcome}: {path}", flush=True)
        print("run_id: " + record["run_id"], flush=True)
        if record.get("error"):
            print("Primary error: " + record["error"], file=sys.stderr, flush=True)
        for error in record.get("cleanup", {}).get("errors", []):
            print("Cleanup error: " + error, file=sys.stderr, flush=True)
    return 0 if outcome == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
