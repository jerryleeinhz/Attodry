"""Stored environment evidence for a fixed run and archived axis visit.

This reader imports no acquisition or hardware modules. UTC observation times do
not prove the collector's monotonic stability window or time until illumination.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import sqlite3


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) else None


def _time(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.utcoffset() is not None else None
    except (ValueError, OverflowError):
        return None


def _json(value):
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _event(row):
    return None if row is None else {"event_id": row[0], "recorded_at_utc": row[1],
                                    "payload": _json(row[2])}


def _latest(connection, run_id, event_type, *, module=None, payload_event=None, execution_id=None):
    # Only selected small JSON documents are transferred, never raw transcripts
    # or whole histories. Each lookup returns a single most recent record.
    clauses = ["run_id=?", "event_type=?"]
    args = [run_id, event_type]
    if module is not None or payload_event is not None:
        clauses.append("json_valid(payload_json)")
    if module is not None:
        clauses.append("json_extract(payload_json, '$.module')=?")
        args.append(module)
    if payload_event is not None:
        clauses.append("json_extract(payload_json, '$.event')=?")
        args.append(payload_event)
    result = _event(connection.execute(
        "SELECT event_id, created_at_utc, payload_json FROM combination_events WHERE "
        + " AND ".join(clauses) + " ORDER BY event_id DESC LIMIT 1", args).fetchone())
    if execution_id is not None and result is not None and result["payload"].get("execution_id") != execution_id:
        return None
    return result


def _latest_apply(connection, run_id, module, execution_id=None):
    clauses = " AND json_extract(payload_json,'$.execution_id')=?" if execution_id is not None else ""
    args = (run_id,module,execution_id) if execution_id is not None else (run_id,module)
    return _event(connection.execute(
        "SELECT event_id,created_at_utc,payload_json FROM combination_events "
        "WHERE run_id=? AND event_type='scan_phase' AND json_valid(payload_json) "
        "AND json_extract(payload_json,'$.phase')='axis_apply' "
        "AND json_extract(payload_json,'$.module')=?" + clauses + " ORDER BY event_id DESC LIMIT 1",
        args).fetchone())


def _criteria(plan, module):
    hardware = plan.get("hardware", {})
    effective = hardware.get("effective_cryostat", {})
    if module == "temperature":
        config = effective.get("temperature_stability")
        if not isinstance(config, dict):
            config = hardware.get("temperature", {}).get("temperature_stability", {})
    else:
        config = effective.get("magnet", {}).get("stability")
        if not isinstance(config, dict):
            config = hardware.get("magnetic", {}).get("magnet", {}).get("stability", {})
    config = config if isinstance(config, dict) else {}
    criteria = config.get("criteria", {})
    criteria = criteria if isinstance(criteria, dict) else {}
    return {"acceptance_mode": config.get("acceptance_mode"),
            "tolerance": _number(criteria.get("tolerance")),
            "stable_range": _number(criteria.get("stable_range")),
            "dwell_s": _number(criteria.get("dwell_s")),
            "wait_timeout_s": _number(config.get("wait_timeout_s")),
            "stable_window_elapsed_s": None, "timeout_remaining_s": None}


def _field(value):
    if not isinstance(value, dict):
        return None
    values = {key: _number(value.get(key)) for key in ("bx_t", "bz_t")}
    return values if all(v is not None for v in values.values()) else None


def read_environment(connection: sqlite3.Connection, run_id: str, plan: dict,
                     progress: dict, phase: dict, *, now: datetime | None = None, execution_id: str | None = None) -> dict:
    """Read evidence in the caller's existing read-only transaction.

    ``now`` only rejects future records; it is never added to the returned stable
    snapshot. Display age and wall-clock elapsed are calculated by the renderer.
    Missing context is unknown, even when a prior sample has the same target.
    """
    axes = plan.get("axes", [])
    selected = [(depth, axis) for depth, axis in enumerate(axes)
                if axis.get("module") in ("temperature", "magnetic")]
    if not selected:
        return {}
    run = connection.execute(
        "SELECT status,created_at_utc FROM combination_runs WHERE run_id=?", (run_id,)).fetchone()
    if run is None:
        return {}
    reference = now or datetime.now(timezone.utc)
    if reference.utcoffset() is None:
        raise ValueError("Environment validation reference must include a timezone")
    reference = reference.astimezone(timezone.utc)
    created = _time(run[1])
    contexts = {}

    def source(payload):
        condition, attempt = payload.get("condition_id"), payload.get("attempt_index")
        if not isinstance(condition, str) or type(attempt) is not int or attempt < 0:
            return None
        key = condition, attempt
        if key not in contexts:
            if execution_id is not None:
                owner = connection.execute(
                    "SELECT execution_id FROM combination_attempt_executions "
                    "WHERE run_id=? AND condition_id=? AND attempt_index=?",
                    (run_id,condition,attempt)).fetchone()
                if owner is None or owner[0] != execution_id:
                    contexts[key] = None
                    return None
            row = connection.execute(
                "SELECT c.context_json,a.started_at_utc FROM combination_conditions c "
                "JOIN combination_attempts a ON a.run_id=c.run_id AND a.condition_id=c.condition_id "
                "WHERE c.run_id=? AND c.condition_id=? AND a.attempt_index=?",
                (run_id, condition, attempt)).fetchone()
            contexts[key] = (_json(row[0]), _time(row[1])) if row else None
        return contexts[key]

    def latest(kind, **kwargs):
        return _latest(connection, run_id, kind, execution_id=execution_id, **kwargs)

    current = progress.get("current_condition") or {}
    current_source = source(current)

    def visit_key(context, depth):
        repeat = context.get("repeat_index")
        if type(repeat) is not int or repeat < 0:
            return None
        result = [repeat]
        coordinates = context.get("axes", {})
        for axis in axes[:depth + 1]:
            index = coordinates.get(axis.get("module"), {}).get("index")
            if type(index) is not int or not 0 <= index < len(axis.get("points", [])):
                return None
            result.append(index)
        return tuple(result)

    latest_state = latest("cryostat_state")
    result = {}
    for depth, axis in selected:
        module = axis["module"]
        item = {"status": "unavailable", "reason": "current axis visit unavailable",
                "qualification": _criteria(plan, module)}
        result[module] = item
        key = visit_key(current_source[0], depth) if current_source else None
        if key is not None:
            values = axis["points"][key[-1]].get("values", {})
            if module == "temperature":
                item["target_temperature_k"] = _number(values.get("temperature_k"))
            else:
                item["target_field_t"] = _field({"bx_t": values.get("field_x_t"),
                                                  "bz_t": values.get("field_z_t")})
        if run[0] != "active" or phase.get("status") == "terminal":
            item["reason"] = "terminal run"
            continue
        if phase.get("status") != "recorded" or phase.get("phase") in (
                "preflight", "cleanup", "close", "cleanup_recovery"):
            item["reason"] = "no active acquisition phase"
            continue
        if key is None or created is None or created > reference:
            continue
        boundary = _latest_apply(connection, run_id, module, execution_id)
        boundary_source = source(boundary["payload"]) if boundary else None
        boundary_time = _time(boundary["recorded_at_utc"]) if boundary else None
        if (boundary_source is None or visit_key(boundary_source[0], depth) != key
                or boundary_time is None or not created <= boundary_time <= reference):
            item["reason"] = "matching axis entry unavailable"
            continue
        item["visit_started_at_utc"] = boundary["recorded_at_utc"]
        # Temperature has a separate authoritative start marker. Magnetic
        # arrival and dwell happen inside axis_apply, not qualify_axis.
        if module == "temperature":
            started = latest("temperature_qualification_started")
            start_source = source(started["payload"]) if started else None
            start_time = _time(started["recorded_at_utc"]) if started else None
            scope = started["payload"].get("axis_scope", {}) if started else {}
            if (started and started["event_id"] >= boundary["event_id"]
                    and start_source and visit_key(start_source[0], depth) == key
                    and start_time and boundary_time <= start_time <= reference
                    and isinstance(scope, dict)
                    and scope.get("repeat_index") == key[0]
                    and scope.get("axis_indices") == list(key[1:])
                    and started["payload"].get("target_k") == item.get("target_temperature_k")):
                item["qualification_started_at_utc"] = started["recorded_at_utc"]
        if latest_state is None:
            item["reason"] = "no cryostat_state record"
            continue
        payload = latest_state["payload"]
        state_source = source(payload)
        captured = _time(payload.get("captured_at_utc"))
        recorded = _time(latest_state["recorded_at_utc"])
        if (state_source is None or visit_key(state_source[0], depth) != key
                or latest_state["event_id"] < boundary["event_id"]):
            item["reason"] = "latest readback belongs to another visit or lacks context"
            continue
        if (captured is None or recorded is None or state_source[1] is None
                or not max(created, boundary_time, state_source[1]) <= captured <= recorded <= reference):
            item["reason"] = "invalid or future readback timestamp"
            continue
        if payload.get("recovery_action") is not None:
            item["reason"] = "recovery readback"
            continue
        if module == "temperature":
            command = latest("temperature_command_confirmed")
            command_source = source(command["payload"]) if command else None
            command_time = _time(command["recorded_at_utc"]) if command else None
            if (command is None or command_source is None
                    or visit_key(command_source[0], depth) != key
                    or command["event_id"] < boundary["event_id"]
                    or command["event_id"] > latest_state["event_id"]
                    or command_time is None or not boundary_time <= command_time <= captured
                    or command["payload"].get("target_k") != item.get("target_temperature_k")):
                item["reason"] = "readback after current temperature command unavailable"
                continue
        state = payload.get("state", {})
        state = state if isinstance(state, dict) else {}
        value = (_number(state.get("sample_temperature_k")) if module == "temperature"
                 else _field(state.get("field")))
        if value is None or (module == "temperature" and value <= 0):
            item["reason"] = "nonfinite or missing readback"
            continue
        item.update(status="available", event_id=latest_state["event_id"],
                    captured_at_utc=payload["captured_at_utc"],
                    recorded_at_utc=latest_state["recorded_at_utc"],
                    source_condition_id=payload["condition_id"],
                    source_attempt_index=payload["attempt_index"])
        item.pop("reason", None)
        item["actual_temperature_k" if module == "temperature" else "actual_field_t"] = value
        if module == "magnetic":
            _field_timing(connection, run_id, item, state, latest_state, source,
                          visit_key, depth, key, boundary, reference, execution_id)
    return result


def _field_timing(connection, run_id, item, state, latest_state, source,
                  visit_key, depth, key, boundary, reference, execution_id):
    sample = _latest(connection, run_id, "magnetic_point_event", payload_event="field_sample", execution_id=execution_id)
    if sample is None or sample["event_id"] < latest_state["event_id"]:
        return
    payload = sample["payload"]
    context = source(payload)
    recorded = _time(sample["recorded_at_utc"])
    if (context is None or visit_key(context[0], depth) != key
            or sample["event_id"] < boundary["event_id"]
            or payload.get("condition_id") != latest_state["payload"].get("condition_id")
            or payload.get("attempt_index") != latest_state["payload"].get("attempt_index")
            or payload.get("state") != state or recorded is None
            or not _time(latest_state["recorded_at_utc"]) <= recorded <= reference):
        return
    elapsed = _number(payload.get("elapsed_s"))
    phase = payload.get("phase")
    waypoint = payload.get("waypoint_index")
    if elapsed is None or elapsed < 0 or not isinstance(phase, str):
        return
    if waypoint is not None and (type(waypoint) is not int or waypoint < 0):
        return
    timeout = item["qualification"]["wait_timeout_s"]
    stability_wait = phase in ("planning_start_stability", "waypoint_stability", "field_stability")
    item["subwait"] = {"phase": phase, "waypoint_index": waypoint,
                       "elapsed_s": elapsed,
                       "timeout_remaining_s": (max(0.0, timeout - elapsed)
                           if stability_wait and timeout is not None and timeout >= 0 else None),
                       "captured_at_utc": item["captured_at_utc"],
                       "recorded_at_utc": sample["recorded_at_utc"]}
    commanded = _field(state.get("field_setpoint"))
    if commanded is not None and commanded != item.get("target_field_t"):
        item["commanded_waypoint_t"] = commanded
