"""Describe, validate and simulate DC photoresponse. No real hardware command."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import subprocess
import sys
from uuid import uuid4

from .nkt_config import NktError
from .nkt_control import NktController, SimulatedNkt
from .photoresponse import PhotoresponseRunner, SimulatedElectrical
from .photoresponse_config import load_photoresponse_config


def audit_safe(value):
    """Preserve invalid raw numeric responses without emitting nonstandard JSON."""
    if isinstance(value, float) and not math.isfinite(value):
        return {"invalid_float": repr(value)}
    if isinstance(value, dict):
        return {k: audit_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [audit_safe(v) for v in value]
    return value


def provenance():
    root = Path(__file__).resolve().parents[2]
    result = {"python": sys.executable, "module": __file__, "git_commit": None, "git_dirty": None}
    try:
        result["git_commit"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True, stderr=subprocess.DEVNULL, timeout=5).strip()
        result["git_dirty"] = bool(subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=root, text=True, stderr=subprocess.DEVNULL, timeout=5).strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("describe", "validate-config", "simulate", "analyze"))
    parser.add_argument("--config", default="config/hardware.local.toml")
    parser.add_argument("--record", help="JSON record for read-only analysis")
    args = parser.parse_args(argv)
    try:
        if args.command == "describe":
            print(json.dumps({"stage": "offline simulation; hardware acquisition unavailable",
                              "modes": ["open_circuit_voltage", "dc_current_voltage", "voltage_bias_current"],
                              "light_source": "NKT simulation; CLD1010LP contract pending exact diode model",
                              "environment_control": "not connected", "power_w": "not measured"}, indent=2))
            return 0
        if args.command == "analyze":
            from .photoresponse_analysis import load_results
            if not args.record:
                raise NktError("analyze requires --record")
            print(json.dumps(load_results(args.record), indent=2, allow_nan=False))
            return 0
        config = load_photoresponse_config(args.config)
        if args.command == "validate-config":
            print(json.dumps(config.snapshot(), indent=2, allow_nan=False))
            return 0
        # Config rejects every non-simulation backend before constructing objects.
        directory = config.nkt.output_directory
        directory.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^\w.-]+", "_", config.nkt.run_name)[:80]
        stem = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + f"_{name}_{uuid4().hex[:8]}"
        journal_path = directory / f"{stem}.jsonl"
        with journal_path.open("x", encoding="utf-8") as journal:
            def event(value):
                journal.write(json.dumps(audit_safe(value), ensure_ascii=False, allow_nan=False) + "\n")
                journal.flush()
            software = provenance()
            event({"phase": "software", **software})
            laser = NktController(config.nkt, SimulatedNkt(config.nkt), on_event=event)
            electrical = SimulatedElectrical(config)
            runner = PhotoresponseRunner(config, laser, electrical, on_event=event)
            record = runner.run()
            record["software"] = software
            record["journal_path"] = str(journal_path)
            path = directory / f"{stem}_{record['outcome']}.json"
            # Write the final record before the redundant journal end event.
            with path.open("x", encoding="utf-8") as stream:
                json.dump(audit_safe(record), stream, indent=2, ensure_ascii=False, allow_nan=False)
            event({"phase": "run_end", "outcome": record["outcome"], "record_path": str(path)})
        print(f"SIMULATION {record['outcome']}: {path}")
        return 0 if record["completed"] else 1
    except (Exception, KeyboardInterrupt) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
