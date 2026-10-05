"""NKT offline validation/simulation and separately gated commissioning CLI."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4

from .nkt_config import NktError, load_nkt_config
from .nkt_control import NktController, SimulatedNkt


def describe():
    return {
        "stage": "offline implementation; no real instrument commissioning",
        "broadband": "EXTREME emission/level; manually selected unfiltered route",
        "varia_bandpass": "400-840 nm, 10-100 nm bandwidth, ND percent",
        "lltf_swir": "1000-2300 nm simulation/interface; vendor SDK still required",
        "readback": "filter edge/ND/source setting readbacks are not measured spectra or watts",
        "power_meter": "optional PowerMeter.read_power_w(wavelength_nm) Python adapter",
        "finish": "emission off and verified; communication failure preserves last confirmed state",
    }


def _git_revision():
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True,
                                       stderr=subprocess.DEVNULL, timeout=5).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("describe", "validate-config", "simulate", "diagnose", "run"))
    parser.add_argument("--config", default="config/hardware.local.toml")
    parser.add_argument("--authorize-read", action="store_true")
    parser.add_argument("--authorize-writes", action="store_true")
    parser.add_argument("--confirm-manual-route", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "describe":
        print(json.dumps(describe(), indent=2, ensure_ascii=False))
        return 0
    try:
        config = load_nkt_config(args.config)
        if args.command == "validate-config":
            print(json.dumps(config.snapshot(), indent=2, ensure_ascii=False))
            return 0
        if args.command == "simulate":
            config = replace(config, backend="simulation")
            backend = SimulatedNkt(config)
        else:
            config.require_hardware(writes=args.command == "run")
            if args.command == "diagnose" and not args.authorize_read:
                raise NktError("diagnose requires --authorize-read")
            if args.command == "run" and not (args.authorize_writes and args.confirm_manual_route):
                raise NktError("run requires --authorize-writes and --confirm-manual-route")
            # Import has no I/O. open() below is the only DLL/port entry point.
            from .nkt_sdk import NktSdk
            backend = NktSdk(config, authorize_writes=args.command == "run")

        config.output_directory.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^\w.-]+", "_", config.run_name)[:80]
        name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + f"_{safe_name}_{uuid4().hex[:8]}"
        base = config.output_directory / name
        record_path = base.parent / f"{base.name}.json"
        journal_path = base.parent / f"{base.name}.jsonl"
        record = {"schema_version": 1, "command": args.command, "completed": False,
                  "outcome": "rejected", "git_commit": _git_revision(),
                  "measurement_config": config.snapshot(), "error": None}
        opened = False
        with journal_path.open("x", encoding="utf-8") as journal:
            def event(value):
                journal.write(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n")
                journal.flush()
            controller = NktController(config, backend, on_event=event)
            try:
                event({"phase": "run_start", **record})
                if backend.is_hardware:
                    backend.open(authorize_connection=True)
                    opened = True
                if args.command == "diagnose":
                    source, filt = controller.capture("diagnose")
                    record.update(readback={"source": source, "filter": filt},
                                  completed=True, outcome="completed")
                else:
                    record.update(controller.run(authorize_writes=args.authorize_writes,
                                                 confirm_manual_route=args.confirm_manual_route))
                    opened = False  # Controller owns close/cleanup and reports failures.
            except (Exception, KeyboardInterrupt) as exc:
                record.update(error=f"{type(exc).__name__}: {exc}", completed=False,
                              outcome="interrupted" if isinstance(exc, KeyboardInterrupt) else "rejected",
                              last_confirmed_state=controller.record["last_confirmed_state"],
                              state_current=False)
            finally:
                if opened:
                    try:
                        backend.close()  # Diagnose never writes, including on failure.
                    except Exception as exc:
                        record.update(completed=False, outcome="rejected", close_error=str(exc))
                record["partial_register_readback"] = getattr(backend, "last_partial_readback", {})
                record["sdk_runtime"] = getattr(backend, "runtime_metadata", None)
                record["sdk_write_attempts"] = getattr(backend, "command_log", [])
                event({"phase": "run_end", "record": record})
                with record_path.open("x", encoding="utf-8") as stream:
                    json.dump(record, stream, indent=2, ensure_ascii=False, allow_nan=False)
        print(f"{record['outcome']}: {record_path}")
        return 0 if record["completed"] else 1
    except (Exception, KeyboardInterrupt) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
