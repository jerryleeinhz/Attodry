"""Read-only progress from the archived plan and committed acquisition records.

No instrument access or live configuration is needed. Axis positions are visits
identified by repeat and index prefixes, rather than distinct numeric values.
"""
from __future__ import annotations

import json
import math
import sqlite3


def read_progress(connection: sqlite3.Connection, run_id: str, plan: dict,
                  current_attempt: dict | None) -> dict:
    """Summarize one existing read transaction without loading sample payloads."""
    axes = plan.get("axes", [])
    expected = plan.get("samples_per_condition")
    if not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
        expected = None
    current = None
    if current_attempt is not None:
        row = connection.execute(
            "SELECT context_json FROM combination_conditions WHERE run_id=? AND condition_id=?",
            (run_id, current_attempt["condition_id"]),
        ).fetchone()
        if row is not None:
            current = json.loads(row[0])
    current_samples = (connection.execute(
        "SELECT count(*) FROM combination_samples WHERE run_id=? AND condition_id=? AND attempt_index=?",
        (run_id, current_attempt["condition_id"], current_attempt["attempt_index"]),
    ).fetchone()[0] if current_attempt else 0)
    condition = ({**current_attempt, "formal_samples": current_samples,
                  "expected_formal_samples": expected} if current_attempt else None)
    axis_progress = []
    counts = []
    for depth, axis in enumerate(axes):
        module = axis["module"]
        points = axis.get("points", [])
        index = (current or {}).get("axes", {}).get(module, {}).get("index")
        valid_index = type(index) is int and 0 <= index < len(points)
        outer = [a["module"] for a in axes[:depth]]
        scope = {m: current["axes"][m]["index"] for m in outer} if current else {}
        axis_progress.append({
            "module": module, "current_point": index + 1 if valid_index else None,
            "total_points": len(points), "completed_points": 0,
            "repeat_index": current.get("repeat_index", 0) if current else None,
            "total_repeats": plan.get("repeats", 1), "scope": scope,
            "requested": dict(points[index].get("values", {})) if valid_index else {},
        })
        counts.append({})
    accepted_samples = 0
    # Aggregate sample counts once; never transfer raw sample JSON to monitor.
    rows = connection.execute(
        "SELECT c.context_json, a.attempt_index, COALESCE(s.n, 0) AS n "
        "FROM combination_conditions c LEFT JOIN combination_attempts a "
        "ON a.run_id=c.run_id AND a.condition_id=c.condition_id AND a.status='accepted' "
        "LEFT JOIN (SELECT condition_id, attempt_index, count(*) AS n FROM combination_samples "
        "WHERE run_id=? GROUP BY condition_id, attempt_index) s "
        "ON s.condition_id=a.condition_id AND s.attempt_index=a.attempt_index WHERE c.run_id=?",
        (run_id, run_id),
    )
    for row in rows:
        context = json.loads(row[0])
        accepted = row[1] is not None
        accepted_samples += row[2] if accepted else 0
        complete = accepted and expected is not None and row[2] == expected
        if current is None or context.get("repeat_index") != current.get("repeat_index"):
            continue
        for depth, axis in enumerate(axis_progress):
            coordinate = context.get("axes", {})
            if any(coordinate.get(m, {}).get("index") != i for m, i in axis["scope"].items()):
                continue
            index = coordinate.get(axis["module"], {}).get("index")
            if type(index) is not int or not 0 <= index < axis["total_points"]:
                continue
            seen, completed = counts[depth].get(index, (0, 0))
            counts[depth][index] = (seen + 1, completed + int(complete))
    for depth, axis in enumerate(axis_progress):
        inner_count = math.prod(len(a.get("points", [])) for a in axes[depth + 1:])
        axis["completed_points"] = sum(
            seen == inner_count and completed == inner_count
            for seen, completed in counts[depth].values()
        )
    return {"axes": axis_progress, "current_condition": condition,
            "accepted_formal_samples": accepted_samples}


def phase_snapshot(event: dict | None, run_status: str,
                   last_event: dict | None) -> dict:
    """Keep explicit phase evidence separate from events and terminal status.

    Timestamps stay stable between polls. Rendering can calculate evidence age;
    neither a long phase nor an old event proves whether the process is alive.
    """
    recorded = None
    if event is not None:
        try:
            payload = json.loads(event["payload_json"])
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            recorded = {key: payload.get(key) for key in
                        ("phase", "module", "sample_index", "condition_id", "attempt_index", "execution_id")}
            recorded["started_at_utc"] = event["created_at_utc"]
    last_update = last_event["created_at_utc"] if last_event else None
    if run_status != "active":
        return {"status": "terminal", "phase": run_status,
                "started_at_utc": last_update, "last_update_at_utc": last_update,
                "last_recorded": recorded}
    if recorded is not None and isinstance(recorded.get("phase"), str):
        return {"status": "recorded", **recorded, "last_update_at_utc": last_update}
    return {"status": "unavailable", "phase": None, "started_at_utc": None,
            "last_update_at_utc": last_update,
            "last_known_event": ({"event_type": last_event["event_type"],
                                  "created_at_utc": last_update} if last_event else None)}
