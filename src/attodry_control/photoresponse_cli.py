"""Plan, simulate, diagnose and explicitly authorize DC photoresponse acquisition."""
from __future__ import annotations

import argparse
import hashlib
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
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.rglob("*.py")):
        digest.update(path.relative_to(Path(__file__).parent).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    result["source_sha256"] = digest.hexdigest()
    try:
        result["git_commit"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True, stderr=subprocess.DEVNULL, timeout=5).strip()
        result["git_dirty"] = bool(subprocess.check_output(
            ["git", "status", "--porcelain"], cwd=root, text=True, stderr=subprocess.DEVNULL, timeout=5).strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return result


def _reject(record, message):
    record["completed"], record["outcome"] = False, "rejected"
    record.setdefault("cleanup", {}).setdefault("errors", []).append(message)
    for row in record.get("conditions", []):
        row["accepted"] = False
        for window in row["windows"]:
            for sample in window["samples"]: sample["accepted"] = False


def _run(config, args, event, attempt_index):
    config.validate()  # All source/domain/resource conflicts precede every factory.
    from contextlib import ExitStack
    from .photoresponse_cld import CldConfig, CldController, SimulatedCld, TL4000, VisaCld
    from .photoresponse_environment import open_environment
    from .photoresponse_hardware import HardwareElectrical
    from .photoresponse_transport import visa_resources
    from .photoresponse_power import open_photoresponse_power
    from .photoresponse_smu import PhotoresponseSmu
    hardware = config.nkt.backend != "simulation"
    record = None
    try:
        if hardware and config.smu:
            config.smu.require_hardware()
            if not getattr(args, "authorize_smu", False):
                raise NktError("Selected SMU hardware requires --authorize-smu")
        with ExitStack() as stack:
            smu = (PhotoresponseSmu(config, simulated=not hardware, on_event=event,
                    authorize_writes=getattr(args, "authorize_smu", False)) if config.smu else None)
            if smu: stack.callback(smu.close)
            if isinstance(config.nkt, CldConfig):
                if hardware:
                    factory = VisaCld if config.nkt.backend == "visa" else TL4000
                    backend = factory(config.nkt, authorize_connection=True, authorize_writes=True, on_event=event)
                else: backend = SimulatedCld(config.nkt)
                stack.callback(backend.close)
                laser = CldController(config.nkt, backend, on_event=event)
            else:
                if hardware:
                    from .nkt_sdk import NktSdk
                    backend = NktSdk(config.nkt, authorize_writes=True)
                    stack.callback(backend.close)
                    backend.open(authorize_connection=True)
                else:
                    backend = SimulatedNkt(config.nkt)
                    stack.callback(backend.close)
                laser = NktController(config.nkt, backend, on_event=event)
            if hardware:
                transports = stack.enter_context(visa_resources(config.hardware.addresses(),
                    timeout_ms=config.hardware.timeout_ms, authorize_connection=True,
                    authorize_writes=True, on_event=event))
                electrical = HardwareElectrical(config, transports)
            else: electrical = SimulatedElectrical(config)
            environment = (open_environment(config.environment, authorize_connection=True,
                authorize_writes=args.authorize_environment, on_event=event) if config.environment else None)
            if environment: stack.callback(environment.driver.close)
            power = open_photoresponse_power(config.power, laser, on_event=event,
                authorize_connection=hardware, authorize_writes=hardware) if config.power else None
            if power: stack.callback(power.close)
            runner = PhotoresponseRunner(config, laser, electrical, on_event=event,
                authorize_writes=hardware, confirm_manual_route=args.confirm_route,
                environment=environment, attempt_index=attempt_index, power=power, smu=smu)
            record = runner.run()
    except (Exception, KeyboardInterrupt) as exc:
        if record is None:
            record = {"schema_version":1, "measurement_type":"dc_photoresponse", "simulated":not hardware,
                "captured_at_utc":datetime.now(timezone.utc).isoformat(), "measurement_config":config.snapshot(),
                "conditions":[], "error":f"{type(exc).__name__}: {exc}", "cleanup":{
                "laser_off_confirmed":False,"electrical_safe_confirmed":False,"errors":[]}}
        _reject(record, f"session: {type(exc).__name__}: {exc}")
    return record


def _diagnose(config, event, authorize_connection):
    from .photoresponse_transport import visa_resources, Serial2182Transport
    from .photoresponse_keithley import Keithley2182Queries, Keithley6221Queries
    if config.hardware is None: raise NktError("diagnose requires hardware configuration")
    h = config.hardware
    result = {"command":"diagnose", "settings_changed":False, "readbacks":{}, "error":None}
    try:
        with visa_resources(h.addresses(), timeout_ms=h.timeout_ms,
                authorize_connection=authorize_connection, on_event=event) as resources:
            if h.source:
                result["readbacks"]["6221"] = Keithley6221Queries(resources["6221"],
                    expected_idn=h.source["expected_idn"]).snapshot(authorize_queries=True)
            transport = (Serial2182Transport(resources["6221"],timeout_s=h.timeout_ms/1000)
                         if h.detector_transport == "via_6221" else resources["detector"])
            if config.detector == "keithley2182a":
                result["readbacks"]["detector"] = Keithley2182Queries(transport,
                    expected_idn=h.detector["expected_idn"]).snapshot(authorize_queries=True)
            else:
                idn = transport.query("*IDN?")
                if idn != h.detector["expected_idn"]: raise NktError("2400 identity mismatch")
                result["readbacks"]["detector"] = {"identity":idn,
                    "output":transport.query("OUTP?"),"source_function":transport.query("SOUR:FUNC?")}
    except (Exception, KeyboardInterrupt) as exc: result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("describe", "validate-config", "simulate", "run", "diagnose", "test-6221", "analyze", "export"))
    parser.add_argument("--config", default="config/hardware.local.toml")
    parser.add_argument("--record", help="JSON record for read-only analysis")
    parser.add_argument("--output", help="Export CSV path")
    parser.add_argument("--resume-run", help="SQLite run_id to retry with a fresh preflight and new attempt")
    parser.add_argument("--authorize-connect", action="store_true")
    parser.add_argument("--authorize-writes", action="store_true", help="Includes source/laser/measurement settings, triggers and error-queue reads")
    parser.add_argument("--confirm-route", action="store_true")
    parser.add_argument("--authorize-environment", action="store_true")
    parser.add_argument("--authorize-smu", action="store_true", help="Explicitly authorize selected SMU connections, settings, fresh READs and consuming status queries")
    args = parser.parse_args(argv)
    try:
        if args.command == "describe":
            print(json.dumps({"stage":"offline-tested drivers; real commissioning is device-specific",
                "modes":["open_circuit_voltage","dc_current_voltage","voltage_bias_current", "smu_current_voltage", "smu_voltage_voltage"],
                "lasers":["NKT SDK EXTREME/VARIA","CLD1010LP USB/VISA SCPI or TL4000"],
                "detectors":["2182A direct VISA or 6221 serial relay","2400"],
                "storage":"JSON/JSONL, optional SQLite with explicit new-attempt retry",
                "environment":"optional paired attoDRY temperature/vector-field points",
                "combination":"ordered optical / target_power_w / bias / smu / environment / repeat axes",
                "smu":"opt-in pinned Three-SMU point engine; gate leakage separate from detector; one sample source",
                "power_w":"optional PM100D bounded feedback; actual W at configured measurement_plane"},indent=2))
            return 0
        if args.command in ("analyze","export"):
            from .photoresponse_analysis import load_results, export_results
            if not args.record: raise NktError("Analysis requires --record")
            if args.command == "export":
                if not args.output: raise NktError("export requires --output")
                export_results(args.record, args.output)
                print(args.output)
            else: print(json.dumps(load_results(args.record),indent=2,allow_nan=False))
            return 0
        if args.command == "test-6221":
            if not args.authorize_connect or not args.authorize_writes:
                raise NktError("6221 test needs --authorize-connect --authorize-writes")
            import tomllib
            with Path(args.config).open("rb") as stream: doc=tomllib.load(stream)
            directory = Path(doc["photoresponse_6221_test"]["output_directory"])
            if not directory.is_absolute(): directory=Path(args.config).resolve().parent/directory
            config=None
        else:
            config=load_photoresponse_config(args.config)
            if args.command == "validate-config":
                print(json.dumps(config.snapshot(),indent=2,allow_nan=False))
                return 0
            hardware = config.nkt.backend != "simulation"
            if args.command == "simulate" and hardware: raise NktError("simulate rejects hardware backends")
            if args.command == "run":
                if not hardware: raise NktError("Use simulate for simulation configuration")
                if not args.authorize_connect or not args.authorize_writes or not args.confirm_route:
                    raise NktError("run requires --authorize-connect --authorize-writes --confirm-route")
                if config.environment and not args.authorize_environment:
                    raise NktError("Environment writes require --authorize-environment")
                config.hardware.require_run(config)
                config.nkt.require_hardware(writes=True)
                if config.power:
                    config.power.meter.require_hardware(writes=True)
                if config.smu:
                    config.smu.require_hardware()
                    if not args.authorize_smu:
                        raise NktError("Selected SMU hardware requires --authorize-smu")
            if args.command == "diagnose" and not args.authorize_connect:
                raise NktError("diagnose requires --authorize-connect")
            if args.resume_run and (not config.database_path or args.command not in ("simulate","run")):
                raise NktError("Resume requires SQLite storage and simulate/run")
            directory=config.nkt.output_directory
        from .photoresponse_storage import PhotoStore, run_lock
        directory.mkdir(parents=True,exist_ok=True)
        name=re.sub(r"[^\w.-]+","_",config.nkt.run_name if config else "6221_test")[:80]
        stem=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")+f"_{name}_{uuid4().hex[:8]}"
        journal_path=directory/f"{stem}.jsonl"
        lock_path=Path(str(config.database_path)+".lock") if config and config.database_path else directory/".photoresponse.lock"
        with run_lock(lock_path), journal_path.open("x",encoding="utf-8") as journal:
            store=None
            try:
                attempt=1
                if config and config.database_path and args.command in ("simulate","run"):
                    store=PhotoStore(config.database_path)
                    run_id,attempt=store.start(config,resume=args.resume_run)
                def event(value):
                    journal.write(json.dumps(audit_safe(value),ensure_ascii=False,allow_nan=False)+"\n")
                    journal.flush()
                    if store: store.event(value)
                software=provenance()
                event({"phase":"software",**software})
                if args.command == "test-6221":
                    from .photoresponse_commission import test_6221
                    record=test_6221(args.config,authorize_connection=True,authorize_writes=True,on_event=event)
                elif args.command == "diagnose": record=_diagnose(config,event,True)
                else: record=_run(config,args,event,attempt)
                record["software"],record["journal_path"] = software,str(journal_path)
                if store: store.finalize(record)
                outcome=record.get("outcome", "rejected" if record.get("error") else "completed")
                path=directory/f"{stem}_{outcome}.json"
                with path.open("x",encoding="utf-8") as stream:
                    json.dump(audit_safe(record),stream,indent=2,ensure_ascii=False,allow_nan=False)
                event({"phase":"run_end","outcome":outcome,"record_path":str(path)})
            finally:
                if store: store.close()
        label="SIMULATION" if record.get("simulated") else args.command.upper()
        print(f"{label} {outcome}: {path}")
        if record.get("run_id"): print("run_id: "+record["run_id"])
        return 0 if outcome == "completed" else 1
    except (Exception, KeyboardInterrupt) as exc:
        print(f"{type(exc).__name__}: {exc}",file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
