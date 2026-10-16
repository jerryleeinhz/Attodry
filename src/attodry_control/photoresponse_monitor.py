"""Compact photoresponse progress and WAL snapshots; file access only."""
from contextlib import closing
from datetime import datetime, timezone
import json
import math
from pathlib import Path

from .combination_store import open_readonly


def _finite(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def initial_progress(config):
    plan = config.get("combination") or {}
    total = math.prod(plan["axis_lengths"].values()) if plan.get("axis_lengths") else None
    return {"phase": "registered", "total_conditions": total,
            "collected_conditions": 0, "accepted_conditions": 0,
            "last_recorded_readings": {}, "current_attempt": None,
            "last_event": None, "phase_started_at_utc": None}


def update_progress(progress, event):
    """Project a persisted event once; never retain large I/O transcripts here."""
    kind = event.get("phase", "unknown")
    stamp = event.get("captured_at_utc") or datetime.now(timezone.utc).isoformat()
    progress["last_event"] = {"created_at_utc": stamp, "event_type": kind}
    phase = event.get("operation_phase") if kind == "phase_start" else None
    if phase == "electrical_state":
        phase = None  # Nested guards do not replace the enclosing operator phase.
    if kind in {"run_start", "condition_start", "run_error", "cleanup_start", "cleanup_complete", "run_end"}:
        phase = kind
    if phase and phase != progress.get("phase"):
        progress.update(phase=phase, phase_started_at_utc=stamp)
    if kind == "condition_start":
        progress["current_attempt"] = {key: event.get(key) for key in (
            "condition_id", "attempt_index", "optical_state", "target_power_w", "axis_indices")}
        progress["current_attempt"]["status"] = "acquiring"
    if kind == "condition_complete":
        row = event["condition"]
        progress["collected_conditions"] += 1
        progress["current_attempt"] = {"condition_id": row["condition_id"],
            "attempt_index": row["attempt_index"], "status": "collected; final acceptance pending",
            "optical_state": row.get("optical_state"), "target_power_w": row.get("target_power_w")}
    if kind == "power_sample":
        evidence = event["evidence"]
        sample = evidence.get("sample") or {}
        power = _finite(sample.get("power_w"))
        progress["last_recorded_readings"]["pm100d"] = {
            "captured_at_utc": sample.get("captured_at_utc", stamp),
            "actual": {"power_w": power, "measurement_performed": sample.get("measurement_performed", power is not None)},
            "clean": evidence.get("valid"), "measurements": {}}
    if kind == "power_window_rejected":
        progress["power_rejection"] = event.get("assessment", {})
    if kind == "raw_sample":
        sample = event.get("sample", {})
        state = sample.get("electrical_after") or sample.get("electrical_before") or {}
        progress["last_recorded_readings"]["electrical"] = {
            "captured_at_utc": sample.get("captured_at_utc", stamp),
            "actual": {"excitation": state.get("excitation"), "source": state.get("source"),
                       "relay_host": state.get("relay_host_readback")},
            "measurements": {key: _finite(sample.get(key)) for key in ("voltage_v", "current_a")},
            "clean": sample.get("valid")}
    if kind in {"environment_readback", "environment_quality"}:
        state = event.get("state") or {}
        progress["last_recorded_readings"]["environment"] = {
            "captured_at_utc": state.get("captured_at_utc", stamp),
            "actual": {key: state.get(key) for key in ("measurement_temperature_k", "field", "quality")},
            "measurements": {}, "clean": None}
    if kind == "run_error":
        progress["error"] = event.get("error")
    if kind == "cleanup_complete":
        progress["cleanup"] = event.get("cleanup")
    return progress


def cleanup_snapshot(cleanup, config):
    keys = ["laser_off_confirmed", "electrical_safe_confirmed"]
    if config.get("smu"):
        keys.append("smu_safe_confirmed")
    actions = [{"module": key.removesuffix("_confirmed"), "result": {
        "verified": cleanup.get(key), "manual_verification_required": cleanup.get(key) is not True}}
        for key in keys]
    clean = all(cleanup.get(key) is True for key in keys) and not cleanup.get("errors")
    return {"clean": clean, "manual_verification_required": not clean,
            "actions": actions, "errors": cleanup.get("errors", [])}


def photo_run_snapshot(path, run_id):
    """Read one committed snapshot, including old databases without progress rows."""
    with closing(open_readonly(path)) as connection:
        connection.execute("BEGIN")
        run = connection.execute("SELECT * FROM photo_runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            raise ValueError("Unknown photoresponse run")
        config = json.loads(run["config_json"])
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        saved = (connection.execute("SELECT progress_json FROM photo_progress WHERE run_id=?", (run_id,)).fetchone()
                 if "photo_progress" in tables else None)
        progress = json.loads(saved[0]) if saved else initial_progress(config)
        if not saved:
            # Historical files: only bounded tail queries, never replay the whole journal.
            count = connection.execute("SELECT COUNT(*) FROM photo_conditions WHERE run_id=?", (run_id,)).fetchone()[0]
            progress["collected_conditions"] = count
            for raw in reversed(connection.execute(
                    "SELECT event_json FROM photo_events WHERE run_id=? ORDER BY event_id DESC LIMIT 12", (run_id,)).fetchall()):
                update_progress(progress, json.loads(raw[0]))
            progress["collected_conditions"] = count
        accepted = connection.execute("SELECT COUNT(*) FROM photo_conditions WHERE run_id=? AND accepted=1", (run_id,)).fetchone()[0]
        final = json.loads(run["record_json"]) if run["record_json"] else None
        if final:
            progress["collected_conditions"] = sum(c.get("result") is not None for c in final.get("conditions", []))
        cleanup = cleanup_snapshot(final["cleanup"], config) if final else None
        error = final.get("error") if final else progress.get("error")
        now = datetime.now(timezone.utc)
        def age(stamp):
            if not stamp:
                return None
            try:
                return max(0., (now - datetime.fromisoformat(stamp.replace("Z", "+00:00"))).total_seconds())
            except (TypeError, ValueError):
                return None
        return {**progress, "run_id": run_id, "measurement_type": "dc_photoresponse",
            "status": "active" if run["outcome"] == "running" else run["outcome"],
            "database": str(Path(path).resolve()), "accepted_conditions": accepted,
            "cleanup": cleanup, "error": error, "process_liveness": "unknown",
            "last_event_age_s": age((progress.get("last_event") or {}).get("created_at_utc")),
            "phase_elapsed_s": age(progress.get("phase_started_at_utc"))}
