"""Explicit OFF ZERO diagnostics; never produce formal/accepted scan samples."""
from dataclasses import asdict
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from uuid import uuid4

from .three_smu import ThreeSmuSafetyError
from .three_smu_config import ScanMode, active_smu_roles, validate_plan_targets
from .three_smu_live import ThreeSmuLiveSnapshot, format_live_three_smu_snapshot, monitor_problems


def run_off_zero_monitor(hardware, plan, *, output_dir, session_open, print_fn=print,
                         sleep=time.sleep, run_name="", note="", config_path=None):
    """Configure OFF ZERO once; inspect latest buffered values on the same adapters."""
    if plan.mode is not ScanMode.OFF_ZERO_MONITOR:
        raise ThreeSmuSafetyError("OFF ZERO diagnostics require off_zero_monitor mode")
    validate_plan_targets(hardware, plan)
    roles = active_smu_roles(plan)
    directory = Path(output_dir) / ("off_zero_monitor_" +
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid4().hex[:8])
    directory.mkdir(parents=True)
    metadata = {"schema": "three-smu-off-zero-monitor-v1", "status": "running",
                "record_kind": "monitor", "accepted": False, "formal_samples": 0,
                "actual_zero_verified": False, "measurement_freshness": "unknown",
                "run_name": run_name, "note": note,
                "config_path": str(config_path) if config_path is not None else None,
                "hardware": {role: asdict(hardware.require_role(role)) for role in roles},
                "plan": asdict(plan), "started_at_utc": _utc(), "observations": 0}
    metadata_path = directory / "monitor_metadata.json"
    def save_metadata():
        metadata_path.write_text(json.dumps(metadata, indent=2, allow_nan=False), encoding="utf-8")
    save_metadata()
    print_fn(f"OFF ZERO monitor records: {directory}")
    with (directory / "monitor_events.jsonl").open("x", encoding="utf-8") as events, \
            (directory / "monitor.jsonl").open("x", encoding="utf-8") as observations:
        class Recorder:
            def event(self, kind, payload):
                events.write(json.dumps({"type": kind, "received_at_utc": _utc(),
                                         "payload": payload}, allow_nan=False) + "\n")
                events.flush()
        recorder = Recorder()
        try:
            with session_open(hardware, plan, authorize_writes=True,
                              authorize_status_consumption=True) as session:
                session.prepare_off_zero_monitor(recorder)
                count = plan.point_count * plan.samples_per_point
                for index in range(count):
                    readings = {role: session.adapters[role].read_off_zero_monitor(
                        consume_status_queue=True) for role in roles}
                    problems = [p for role in roles for p in monitor_problems(
                        role, hardware.require_role(role), readings[role])]
                    identities = [reading.identity.strip() for reading in readings.values()]
                    if len(set(identities)) != len(identities):
                        problems.append("OFF ZERO monitor identities are not distinct")
                    for role, reading in readings.items():
                        if reading.output_enabled or reading.off_state != "zero":
                            problems.append(f"{role} did not remain OFF/ZERO")
                        if reading.voltage_v is None or reading.current_a is None:
                            problems.append(f"{role} has no OFF ZERO buffered V/I")
                    snapshot = ThreeSmuLiveSnapshot(index, datetime.now(timezone.utc), True,
                        {role: channel.role for role, channel in plan.by_role().items()},
                        readings, tuple(problems))
                    observations.write(json.dumps({"record_kind": "monitor", "accepted": False,
                        "actual_zero_verified": False, "received_at_utc": _utc(),
                        "snapshot": asdict(snapshot)}, allow_nan=False, default=str) + "\n")
                    observations.flush()
                    metadata["observations"] += 1
                    print_fn(format_live_three_smu_snapshot(snapshot), flush=True)
                    if problems:
                        raise ThreeSmuSafetyError("; ".join(problems))
                    if index + 1 < count and plan.delay_s:
                        sleep(plan.delay_s)
            # Transport closure must succeed before reporting completion.
            metadata["status"] = "completed"
            metadata["transport_closed"] = True
        except KeyboardInterrupt:
            metadata.update(status="interrupted", error="KeyboardInterrupt",
                            manual_verification_required=True)
            return 130
        except BaseException as exc:
            metadata.update(status="failed", error=f"{type(exc).__name__}: {exc}",
                            manual_verification_required=True)
            raise
        finally:
            metadata["finished_at_utc"] = _utc()
            save_metadata()
    return 0


def _utc():
    return datetime.now(timezone.utc).isoformat()
