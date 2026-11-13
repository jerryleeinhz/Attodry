"""One-command CLD LD ON/OFF using existing current and TEC settings."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import tomllib
import uuid

from .cld_output import enable_existing_ld, protected_ld_off
from .hardware_lease import station_hardware_lease
from .nkt_config import NktError
from .nkt_control import utc_now
from .photoresponse_cld import TL4000, VisaCld, load_cld_config


def _describe_error(exc):
    return f"{type(exc).__name__}: {exc}"


def run_control(action, config, *, record, persist, backend_factory=None,
                clock=time.monotonic, sleep=time.sleep):
    """Execute exactly one local command; injected backends support offline tests.

    The initial record must be durable before this function is called. Only
    this function owns the opened backend, including closure on failed ON/OFF.
    """
    if action not in ("on", "off"):
        raise NktError("Expected CLD action on or off")
    config.require_hardware(writes=True)
    record.update(action=action, status="connecting", started_at_utc=utc_now(),
                  identity_verified=False, transport_closed=False, errors=[],
                  manual_verification_required=True, transactions=0,
                  failed_transactions=[], state_current=False)
    persist(record)
    backend = None

    def audit(event):
        record["last_event"] = deepcopy(event)
        if "command" in event or "function" in event:
            record["transactions"] += 1
            if "error" in event or "audit_error" in event:
                record["failed_transactions"].append(deepcopy(event))
        if "cld" in event:
            record["last_confirmed_state"] = deepcopy(event["cld"])
            record["last_confirmed_at_utc"] = event["captured_at_utc"]
        persist(record)

    try:
        factory = backend_factory or (VisaCld if config.backend == "visa" else TL4000)
        backend = factory(config, authorize_connection=True, authorize_writes=True, on_event=audit)
        record["status"] = "connected"
        persist(record)
        if action == "off":
            record["off"] = {}
            protected_ld_off(config, backend, record=record["off"], phase="cld_manual_off", on_event=audit)
            record["identity_verified"] = record["off"]["identity_verified"]
        else:
            record["identity"] = backend.identify()
            record["identity_verified"] = True
            record["identity_captured_at_utc"] = utc_now()
            record["status"] = "qualifying"
            record["on"] = {"on_attempted": False, "on_confirmed": False}
            persist(record)
            enable_existing_ld(config, backend, record=record["on"], on_event=audit,
                               clock=clock, sleep=sleep)
        record["status"] = "verified"
        record["manual_verification_required"] = False
        persist(record)
    except (Exception, KeyboardInterrupt) as exc:
        record["status"] = "failed"
        record["errors"].append(_describe_error(exc))
        record["manual_verification_required"] = True
        if action == "on" and backend is not None and record["identity_verified"]:
            record["failed_on_off"] = {}
            try:
                protected_ld_off(config, backend, record=record["failed_on_off"],
                                 phase="cld_failed_on_off", on_event=audit,
                                 verified_identity=record)
            except (Exception, KeyboardInterrupt) as cleanup_exc:
                record["errors"].append("protective OFF: " + _describe_error(cleanup_exc))
    finally:
        if backend is not None:
            try:
                backend.close()
                record["transport_closed"] = True
            except (Exception, KeyboardInterrupt) as exc:
                record["errors"].append("close: " + _describe_error(exc))
                record["status"] = "failed"
                record["manual_verification_required"] = True
        if action == "off" and "off" in record:
            record["identity_verified"] = record["off"]["identity_verified"]
        record["finished_at_utc"] = utc_now()
        # Readbacks are historical after close, never a live output guarantee.
        record["state_current"] = False
        try:
            persist(record)
        except (Exception, KeyboardInterrupt) as exc:
            # No re-open/retry after closure. In particular, successful ON may
            # still be ON when only its final audit write fails.
            record["errors"].append("final audit: " + _describe_error(exc))
            record["status"] = "failed"
            record["manual_verification_required"] = True
            record["final_audit_persist_failed"] = True
            raise NktError("; ".join(record["errors"])) from exc
    if record["errors"]:
        raise NktError("; ".join(record["errors"]))
    return record


class _RecordWriter:
    def __init__(self, path, initial):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(initial, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

    def __call__(self, record):
        temporary = self.path.with_name(self.path.name + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(record, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for action in ("on", "off"):
        command = commands.add_parser(action, help=f"Set LD {action.upper()}; preserve current and TEC")
        command.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    writer = None
    record = {"schema_version": 1, "action": args.action, "status": "prepared",
              "prepared_at_utc": utc_now(), "config": str(args.config.resolve())}
    try:
        raw = args.config.read_bytes()
        config = load_cld_config(tomllib.loads(raw.decode("utf-8-sig")), args.config)
        config.require_hardware(writes=True)
        record["config_sha256"] = hashlib.sha256(raw).hexdigest()
        record["cld_config"] = config.snapshot()
        label = utc_now().replace(":", "").replace("-", "").replace("+", "_")
        path = config.output_directory / f"cld-{args.action}-{label}-{uuid.uuid4().hex[:8]}.json"
        writer = _RecordWriter(path, record)
        # Station ownership plus the backend's common serial-based CLD lease.
        with station_hardware_lease(args.config):
            run_control(args.action, config, record=record, persist=writer)
        print(f"CLD LD {args.action.upper()} confirmed; existing current and TEC settings preserved.")
        print(f"Record: {path}")
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        if writer is not None:
            record["status"] = "failed"
            errors = record.setdefault("errors", [])
            detail = _describe_error(exc)
            if detail not in errors:
                errors.append(detail)
            try:
                writer(record)
            except (Exception, KeyboardInterrupt) as audit_exc:
                print("Record write failed: " + _describe_error(audit_exc), file=sys.stderr)
            print(f"Record: {writer.path}", file=sys.stderr)
        print("ERROR: " + _describe_error(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
