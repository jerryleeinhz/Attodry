"""Optical CLI audit helpers; resources are opened only after explicit stage gates."""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
import time
from uuid import uuid4

from .nkt_config import NktError, keys, number, text, load_nkt_config
from .nkt_control import SimulatedNkt, utc_now
from .nkt_test import _git_revision
from .optical_config import (document, load_pem_config, load_pm_config, load_scan_config, WindowConfig)
from .pem import Pem, SerialTransport, SimulatedPemTransport
from .pm100d import Pm100d, SimulatedPmResource
from .power_feedback import PowerWindow
from .optical_scan import OpticalScan


def _device(module, config, simulation, args):
    if simulation:
        config = replace(config, backend="simulation")
    if module == "pem":
        transport = (SimulatedPemTransport() if simulation else
                     SerialTransport(config, authorize_connection=True, authorize_writes=args.authorize_settings))
        return Pem(config, transport, is_hardware=not simulation, authorize_writes=args.authorize_settings)
    if simulation:
        return Pm100d(config, SimulatedPmResource())
    return Pm100d.open(config, authorize_connection=True, authorize_settings=args.authorize_settings,
                      authorize_measurement=args.authorize_measurement)


def _run_table(module, path):
    table = document(path).get(module + "_run", {})
    common = {"output_directory", "run_name", "note", "wavelength_nm"}
    required = common | ({"peak_retardance_waves", "hold_s"} if module == "pem" else {"timeout_s", "window"})
    keys(table, module + "_run", required)
    number(table["wavelength_nm"], "wavelength_nm", 0.001, 1e6)
    for key in ("output_directory", "run_name"):
        text(table[key], key)
    text(table["note"], "note", empty=True)
    if module == "pem":
        number(table["hold_s"], "hold_s", 0, 86400)
    else:
        raw = table["window"]
        keys(raw, "pm100d_run.window", {"duration_s", "min_samples", "sample_interval_s", "max_peak_to_peak_w"})
        window = WindowConfig(**raw)
        window.validate()
        number(table["timeout_s"], "timeout_s", window.duration_s, 86400)
    return table


def _paths(path, run):
    directory = Path(run["output_directory"])
    if not directory.is_absolute():
        directory = Path(path).resolve().parent / directory
    directory.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.-]+", "_", run["run_name"])[:80]
    name = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + f"_{safe}_{uuid4().hex[:8]}"
    return directory / (name + ".json"), directory / (name + ".jsonl")


def standalone_main(module, argv=None):
    parser = argparse.ArgumentParser(description=f"{module} offline tests and explicitly gated commissioning")
    parser.add_argument("command", choices=("describe", "validate-config", "simulate", "diagnose", "run"))
    parser.add_argument("--config", default="config/hardware.local.toml")
    parser.add_argument("--authorize-read", action="store_true")
    parser.add_argument("--authorize-settings", action="store_true")
    parser.add_argument("--authorize-measurement", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "describe":
        print(json.dumps({"module": module, "stage": "hardware acceptance is recorded per station and configuration",
                          "PEM": "peak nm, fixed head resonance; output acknowledgement only",
                          "PM100D": "average W via READ?; no PEM harmonic demodulation",
                          "diagnose": "queries only; no error/event queue consumption",
                          "resource_close": "does not shut down laser/PEM modulation"}, indent=2))
        return 0
    try:
        config = load_pem_config(args.config) if module == "pem" else load_pm_config(args.config)
        run = _run_table(module, args.config)
        if module == "pem":
            config.target(run["wavelength_nm"], run["peak_retardance_waves"])
        if args.command == "validate-config":
            print(json.dumps({"device": asdict(config), "run": run}, indent=2))
            return 0
        simulation = args.command == "simulate"
        if not simulation:
            config.require_hardware(writes=args.command == "run" and args.authorize_settings)
            if args.command == "diagnose" and not args.authorize_read:
                raise NktError("diagnose requires --authorize-read")
            if args.command == "run":
                if module == "pem" and not args.authorize_settings:
                    raise NktError("PEM run requires --authorize-settings (includes activation/cleanup)")
                if module == "pm100d" and not args.authorize_measurement:
                    raise NktError("PM run requires --authorize-measurement; settings permission is separate")
            if args.command == "diagnose" and (args.authorize_settings or args.authorize_measurement):
                raise NktError("diagnose is query-only; do not pass setting/measurement permissions")
        record_path, journal_path = _paths(args.config, run)
        record = {"schema_version": 1, "module": module, "command": args.command,
                  "git_commit": _git_revision(), "simulated": simulation,
                  "measurement_config": {"device": asdict(config), "run": run},
                  "completed": False, "outcome": "rejected", "error": None,
                  "samples": [], "events": [], "cleanup": {"errors": []}}
        device = None
        with journal_path.open("x", encoding="utf-8") as journal:
            def event(phase, **values):
                item = {"phase": phase, "captured_at_utc": utc_now(), **values}
                record["events"].append(item)
                journal.write(json.dumps(item, allow_nan=False) + "\n")
                journal.flush()
            try:
                event("run_start")
                device = _device(module, config, simulation, args)
                if args.command == "diagnose":
                    record["readback"] = device.capture()
                elif module == "pem":
                    device.preflight()
                    record["preparation"] = device.prepare(run["wavelength_nm"], run["peak_retardance_waves"])
                    until = time.monotonic() + run["hold_s"]
                    while True:
                        evidence = device.verify_ready()
                        event("ready", evidence=evidence)
                        if time.monotonic() >= until:
                            break
                        time.sleep(min(config.poll_interval_s, until - time.monotonic()))
                else:
                    window = PowerWindow(WindowConfig(**run["window"]), epoch_s=time.monotonic(),
                                         max_power_w=config.max_power_w)
                    until = time.monotonic() + run["timeout_s"]
                    device.prepare(run["wavelength_nm"])
                    while True:
                        if time.monotonic() >= until:
                            raise NktError("PM stable-window timeout")
                        sample = device.read_sample(run["wavelength_nm"])
                        sample["accepted"] = False
                        record["samples"].append(sample)
                        event("sample", evidence=sample.copy())
                        record["stable_result"] = window.add(sample)
                        if time.monotonic() >= until:
                            raise NktError("PM acquisition exceeded stable-window deadline")
                        if record["stable_result"]["power_window_stable"]:
                            break
                        time.sleep(min(window.config.sample_interval_s, until - time.monotonic()))
                record["outcome"] = "completed"
            except (Exception, KeyboardInterrupt) as exc:
                record["error"] = f"{type(exc).__name__}: {exc}"
                record["error_notes"] = getattr(exc, "__notes__", [])
                record["outcome"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "rejected"
            finally:
                if device:
                    if module == "pem" and args.command != "diagnose":
                        record["cleanup"] = device.finish()
                    else:
                        try:
                            device.close()
                        except (Exception, KeyboardInterrupt) as exc:
                            record["cleanup"]["errors"].append(f"close: {exc}")
                    record["last_confirmed_state"] = device.last_confirmed_state
                    record["transcript"] = device.transcript
                record["state_current"] = False
                if record["cleanup"]["errors"]:
                    record["outcome"] = "rejected"
                record["completed"] = record["outcome"] == "completed"
                for sample in record["samples"]:
                    sample["accepted"] = record["completed"]
                event("run_end", outcome=record["outcome"], cleanup=record["cleanup"])
                with record_path.open("x", encoding="utf-8") as stream:
                    json.dump(record, stream, indent=2, allow_nan=False)
        print(f"{record['outcome']}: {record_path}")
        return 0 if record["completed"] else 1
    except (Exception, KeyboardInterrupt) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


def scan_main(argv=None):
    parser = argparse.ArgumentParser(description="Joint optical scan with optional PEM and PM100D")
    parser.add_argument("command", choices=("describe", "validate-config", "simulate", "run"))
    parser.add_argument("--config", default="config/hardware.local.toml")
    parser.add_argument("--authorize-writes", action="store_true")
    parser.add_argument("--confirm-manual-route", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "describe":
        print(json.dumps({"modes": ["direct", "power_stabilized"],
                          "devices": "explicit optional PEM/PM100D; NKT points are the sole wavelength grid",
                          "feedback": "explicit source_current or verified varia_nd; confirmed-off corrections, new windows, bounded steps/timeout",
                          "stage": "hardware acceptance is recorded per station and configuration"}, indent=2))
        return 0
    try:
        nkt = load_nkt_config(args.config)
        simulation = args.command == "simulate"
        if simulation:
            nkt = replace(nkt, backend="simulation")
        config, pem_config, pm_config = load_scan_config(args.config, nkt)
        if args.command == "validate-config":
            print(json.dumps({"nkt": nkt.snapshot(), "scan": asdict(config),
                              "pem": asdict(pem_config) if pem_config else None,
                              "pm100d": asdict(pm_config) if pm_config else None}, indent=2))
            return 0
        if not simulation:
            if not args.authorize_writes or not args.confirm_manual_route:
                raise NktError("Joint run requires --authorize-writes and --confirm-manual-route")
            nkt.require_hardware(writes=True)
            for device_config in (pem_config, pm_config):
                if device_config:
                    device_config.require_hardware(writes=True)
        run = {"output_directory": str(nkt.output_directory), "run_name": nkt.run_name}
        record_path, journal_path = _paths(args.config, run)
        backend = pem = meter = scanner = None
        record = {"git_commit": _git_revision(), "completed": False, "outcome": "rejected",
                  "error": None, "cleanup": {}, "points": []}
        with journal_path.open("x", encoding="utf-8") as journal:
            def event(value):
                journal.write(json.dumps(value, allow_nan=False) + "\n")
                journal.flush()
            try:
                event({"phase": "run_start", **record})
                if simulation:
                    backend = SimulatedNkt(nkt)
                else:
                    from .nkt_sdk import NktSdk
                    backend = NktSdk(nkt, authorize_writes=True)
                    backend.open(authorize_connection=True)
                permissions = argparse.Namespace(authorize_settings=True, authorize_measurement=True)
                if pem_config:
                    pem = _device("pem", pem_config, simulation, permissions)
                if pm_config:
                    meter = _device("pm100d", pm_config, simulation, permissions)
                    if simulation:
                        # An explicit nonlinear fake law with marked direction;
                        # this is not a VARIA transmission calibration.
                        def simulated_power():
                            if backend.source["emission_state"] != 3:
                                return 0.0
                            if config.feedback and config.feedback.actuator == "source_current":
                                return 2 * config.feedback.max_power_w * (backend.source["level_pct"] / nkt.max_level_pct) ** 2
                            increasing = config.feedback.increasing_nd_increases_power if config.feedback else False
                            return (0.002 * 10 ** ((backend.filter["nd_pct"] if increasing else -backend.filter["nd_pct"]) / 100)
                                    if nkt.mode == "varia_bandpass" else 0.002)
                        meter.resource.power_provider = simulated_power
                scanner = OpticalScan(nkt, config, backend, pem=pem, meter=meter, on_event=event)
                record.update(scanner.run(authorize_writes=args.authorize_writes,
                                          confirm_manual_route=args.confirm_manual_route))
            except (Exception, KeyboardInterrupt) as exc:
                record["error"] = f"{type(exc).__name__}: {exc}"
                record["error_notes"] = getattr(exc, "__notes__", [])
                record["outcome"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "rejected"
            finally:
                if scanner is None or not scanner.nkt.session_closed:
                    # Construction/preflight failures do not authorize a takeover.
                    if scanner:
                        record["cleanup"]["nkt"] = scanner.nkt.finish_session()
                    for name, resource in (("nkt", backend), ("pem", pem), ("pm100d", meter)):
                        if resource:
                            try:
                                if scanner and name == "nkt":
                                    continue
                                if name == "pem":
                                    record["cleanup"][name] = resource.finish()
                                else:
                                    resource.close()
                            except (Exception, KeyboardInterrupt) as exc:
                                record["cleanup"][name] = {"errors": [str(exc)]}
                if scanner:
                    record["sdk_write_attempts"] = getattr(backend, "command_log", [])
                    record["sdk_runtime"] = getattr(backend, "runtime_metadata", None)
                for name, resource in (("pem", pem), ("pm100d", meter)):
                    if resource:
                        record[f"{name}_transcript"] = resource.transcript
                        record[f"{name}_last_confirmed_state"] = resource.last_confirmed_state
                event({"phase": "final_record", "record": record})
                with record_path.open("x", encoding="utf-8") as stream:
                    json.dump(record, stream, indent=2, allow_nan=False)
        print(f"{record['outcome']}: {record_path}")
        return 0 if record["completed"] else 1
    except (Exception, KeyboardInterrupt) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
