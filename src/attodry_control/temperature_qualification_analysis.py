"""Offline replay of archived temperature windows; never a readiness certificate."""
from __future__ import annotations

import argparse
from bisect import bisect_left
from contextlib import closing
from dataclasses import replace
import json
import math
from pathlib import Path
import sqlite3

from .stability import (StabilityCriteria, TimedValue, evaluate_readback_stability,
                        evaluate_stability)


def qualification_wait_summary(payload, stability):
    """Compare arrival, target-only dwell and range-only dwell on the same samples.

    These overlapping thresholds are diagnostics, not additive causes. Replay
    excludes control/field/overshoot/response gates and cannot authorize a run.
    """
    mode = stability["acceptance_mode"]
    if mode not in {"target", "stable-readback"}:
        raise ValueError("Unknown archived temperature acceptance mode")
    target = payload["target_k"]
    if type(target) not in (int, float) or not math.isfinite(target):
        raise ValueError("Qualification target must be finite")
    samples = []
    for pair in payload["samples"]:
        if (len(pair) != 2 or any(type(v) not in (int, float) for v in pair)
                or pair[0] < 0):
            raise ValueError("Malformed qualification sample")
        samples.append(TimedValue(*pair))
    if not samples or any(b.elapsed_s < a.elapsed_s for a, b in zip(samples, samples[1:])):
        raise ValueError("Qualification samples must be nonempty and time ordered")
    criteria = StabilityCriteria(**stability["criteria"])
    values = [s.value for s in samples]
    times = [s.elapsed_s for s in samples]
    target_only = replace(criteria, stable_range=max(values) - min(values))
    first_band = None
    target_dwell = range_dwell = joint_dwell = None
    for i, sample in enumerate(samples):
        if (criteria.tolerance is not None and first_band is None
                and abs(sample.value - target) <= criteria.tolerance):
            first_band = sample.elapsed_s
        # Retain the real predecessor exactly as the acquisition evaluator does.
        start = max(0, bisect_left(times, sample.elapsed_s - criteria.dwell_s, 0, i + 1) - 1)
        window = samples[start:i + 1]
        range_ok = evaluate_readback_stability(window, criteria)
        target_ok = (evaluate_stability(window, target, target_only)
                     if criteria.tolerance is not None else None)
        joint_ok = (evaluate_stability(window, target, criteria)
                    if mode == "target" else range_ok)
        if target_ok and target_dwell is None:
            target_dwell = sample.elapsed_s
        if range_ok and range_dwell is None:
            range_dwell = sample.elapsed_s
        if joint_ok and joint_dwell is None:
            joint_dwell = sample.elapsed_s
    total = times[-1]
    return {
        "condition_id": payload.get("condition_id"),
        "attempt_index": payload.get("attempt_index"),
        "execution_id": payload.get("execution_id"),
        "target_k": target, "acceptance_mode": mode,
        "criteria": stability["criteria"], "sample_count": len(samples),
        "recorded_qualification_elapsed_s": total,
        "first_in_target_band_s": first_band,
        "first_target_only_dwell_s": target_dwell,
        "first_range_only_dwell_s": range_dwell,
        "first_joint_dwell_s": joint_dwell,
        "after_first_band_s": None if first_band is None else total - first_band,
        "replay_matches_recorded_completion": joint_dwell == total,
        "qualification_certified": False,
    }


def load_qualification_waits(database, run_id):
    """Read one explicitly selected run in a short read-only transaction."""
    if not isinstance(run_id, str) or not run_id.strip():
        raise ValueError("An explicit run ID is required")
    path = Path(database).resolve()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        run = connection.execute("SELECT status FROM combination_runs WHERE run_id=?", (run_id,)).fetchone()
        if run is None:
            raise ValueError("Run ID not found")
        plans = dict(connection.execute(
            "SELECT execution_id,plan_json FROM combination_executions WHERE run_id=?", (run_id,)))
        records = connection.execute(
            "SELECT event_id,payload_json FROM combination_events WHERE run_id=? "
            "AND event_type='temperature_qualification_completed' ORDER BY event_id", (run_id,)).fetchall()
    rows = []
    for event_id, raw in records:
        payload = json.loads(raw)
        plan = json.loads(plans[payload["execution_id"]])
        stability = plan["hardware"]["temperature"]["temperature_stability"]
        rows.append({"event_id": event_id, **qualification_wait_summary(payload, stability)})
    return {"run_id": run_id, "run_status": run[0], "source": str(path),
            "scope": "Completed qualification windows only; all attempts/executions kept separate",
            "interpretation": "Target/range times overlap; current evaluator replay is not a safety proof",
            "qualifications": rows}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    report = load_qualification_waits(args.database, args.run_id)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, allow_nan=False)
        stream.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
