"""Versioned, additive SQLite audit for arbitrary module combinations.

No hardware imports. Old RunStore tables and standalone files are not migrated.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any

from .monitor_progress import phase_snapshot, read_progress


SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def open_readonly(path: str | Path) -> sqlite3.Connection:
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


class CombinationStore:
    """One writer; FULL-synchronous WAL. Each raw append precedes promotion."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS combination_runs (
                run_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL,
                plan_json TEXT NOT NULL, created_at_utc TEXT NOT NULL,
                status TEXT NOT NULL, cleanup_json TEXT, error TEXT
            );
            CREATE TABLE IF NOT EXISTS combination_conditions (
                run_id TEXT NOT NULL REFERENCES combination_runs(run_id),
                condition_id TEXT NOT NULL, sequence_index INTEGER NOT NULL,
                context_json TEXT NOT NULL,
                PRIMARY KEY (run_id, condition_id), UNIQUE(run_id, sequence_index)
            );
            CREATE TABLE IF NOT EXISTS combination_attempts (
                run_id TEXT NOT NULL, condition_id TEXT NOT NULL,
                attempt_index INTEGER NOT NULL, status TEXT NOT NULL
                    CHECK(status IN ('started', 'accepted', 'rejected')),
                error TEXT, started_at_utc TEXT NOT NULL, finished_at_utc TEXT,
                PRIMARY KEY(run_id, condition_id, attempt_index),
                FOREIGN KEY(run_id, condition_id)
                    REFERENCES combination_conditions(run_id, condition_id)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS combination_one_accepted
                ON combination_attempts(run_id, condition_id) WHERE status='accepted';
            CREATE TABLE IF NOT EXISTS combination_samples (
                run_id TEXT NOT NULL, condition_id TEXT NOT NULL,
                attempt_index INTEGER NOT NULL, sample_index INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY(run_id, condition_id, attempt_index, sample_index),
                FOREIGN KEY(run_id, condition_id, attempt_index)
                    REFERENCES combination_attempts(run_id, condition_id, attempt_index)
            );
            CREATE TABLE IF NOT EXISTS combination_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES combination_runs(run_id),
                created_at_utc TEXT NOT NULL, event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS combination_skipped_conditions (
                run_id TEXT NOT NULL, condition_id TEXT NOT NULL,
                attempt_index INTEGER NOT NULL, reason TEXT NOT NULL,
                evidence_json TEXT NOT NULL, created_at_utc TEXT NOT NULL,
                PRIMARY KEY (run_id, condition_id),
                FOREIGN KEY(run_id, condition_id, attempt_index)
                    REFERENCES combination_attempts(run_id, condition_id, attempt_index)
            );
        """)

    def __enter__(self) -> "CombinationStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.connection.close()

    def begin_run(self, run_id: str, plan: dict, conditions: list[dict], *,
                  resume: bool = False) -> set[str]:
        # Reserve the run atomically before any station operation.
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            prior = self.connection.execute(
                "SELECT * FROM combination_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if prior is None:
                if resume:
                    raise ValueError("Cannot resume an unknown run")
                self.connection.execute(
                    "INSERT INTO combination_runs VALUES (?, ?, ?, ?, 'active', NULL, NULL)",
                    (run_id, SCHEMA_VERSION, encode(plan), utc_now()),
                )
                for condition in conditions:
                    self.connection.execute(
                        "INSERT INTO combination_conditions VALUES (?, ?, ?, ?)",
                        (run_id, condition["condition_id"], condition["sequence_index"],
                         encode(condition)),
                    )
                accepted: set[str] = set()
            else:
                if not resume:
                    raise ValueError("Run already exists; never overwrite an audit")
                if prior["schema_version"] != SCHEMA_VERSION or prior["plan_json"] != encode(plan):
                    raise ValueError("Resume plan/schema differs from archived plan")
                if prior["status"] not in {"failed", "interrupted"}:
                    raise ValueError("Only a terminal failed/interrupted run can resume")
                if any(axis["module"] == "magnetic" for axis in plan["axes"]):
                    raise ValueError("Magnetic history requires an explicit recovery plan; no auto-resume")
                if not json.loads(prior["cleanup_json"] or "{}").get("clean", False):
                    raise ValueError("Unverified cleanup requires manual recovery")
                accepted = {row[0] for row in self.connection.execute(
                    "SELECT condition_id FROM combination_attempts "
                    "WHERE run_id=? AND status='accepted'", (run_id,)
                )}
                self.connection.execute(
                    "UPDATE combination_runs SET status='active', cleanup_json=NULL, error=NULL "
                    "WHERE run_id=?", (run_id,),
                )
            self.connection.commit()
            return accepted
        except BaseException:
            self.connection.rollback()
            raise

    def event(self, run_id: str, event_type: str, payload: dict) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO combination_events(run_id, created_at_utc, event_type, payload_json) "
                "VALUES (?, ?, ?, ?)", (run_id, utc_now(), event_type, encode(payload)),
            )

    def begin_attempt(self, run_id: str, condition_id: str) -> int:
        with self.connection:
            index = self.connection.execute(
                "SELECT COALESCE(MAX(attempt_index), -1)+1 FROM combination_attempts "
                "WHERE run_id=? AND condition_id=?", (run_id, condition_id),
            ).fetchone()[0]
            self.connection.execute(
                "INSERT INTO combination_attempts VALUES (?, ?, ?, 'started', NULL, ?, NULL)",
                (run_id, condition_id, index, utc_now()),
            )
        return index

    def sample(self, run_id: str, condition_id: str, attempt: int,
               sample_index: int, payload: dict) -> None:
        with self.connection:
            self.connection.execute(
                "INSERT INTO combination_samples VALUES (?, ?, ?, ?, ?)",
                (run_id, condition_id, attempt, sample_index, encode(payload)),
            )

    def finish_attempt(self, run_id: str, condition_id: str, attempt: int, *,
                       expected_samples: int, error: str | None = None) -> None:
        with self.connection:
            if error is None:
                samples = self.connection.execute(
                    "SELECT payload_json FROM combination_samples "
                    "WHERE run_id=? AND condition_id=? AND attempt_index=?",
                    (run_id, condition_id, attempt),
                ).fetchall()
                from .lockin_overload import reading_allows_continuation
                payloads = [json.loads(row[0]) for row in samples]
                if len(samples) != expected_samples or not all(
                    p.get("clean") is True or (
                        p.get("acquisition_accepted") is True and p.get("reads")
                        and all(reading_allows_continuation(r) for r in p["reads"]))
                    for p in payloads
                ):
                    raise ValueError("Cannot promote incomplete or unclean samples")
            cursor = self.connection.execute(
                "UPDATE combination_attempts SET status=?, error=?, finished_at_utc=? "
                "WHERE run_id=? AND condition_id=? AND attempt_index=? AND status='started'",
                ("accepted" if error is None else "rejected", error, utc_now(),
                 run_id, condition_id, attempt),
            )
            if cursor.rowcount != 1:
                raise ValueError("Attempt must exist and be started")

    def skip_condition(self, run_id, condition_id, attempt, evidence):
        """Archive a rejected, guarded skip; never promote it to accepted data."""
        plan = json.loads(self.connection.execute(
            "SELECT plan_json FROM combination_runs WHERE run_id=?", (run_id,)).fetchone()[0])
        context = json.loads(self.connection.execute(
            "SELECT context_json FROM combination_conditions WHERE run_id=? AND condition_id=?",
            (run_id, condition_id)).fetchone()[0])
        row = self.connection.execute(
            "SELECT status FROM combination_attempts WHERE run_id=? AND condition_id=? AND attempt_index=?",
            (run_id, condition_id, attempt)).fetchone()
        samples = self.connection.execute(
            "SELECT count(*) FROM combination_samples WHERE run_id=? AND condition_id=? AND attempt_index=?",
            (run_id, condition_id, attempt)).fetchone()[0]
        if (plan.get("unreachable_target_policy") != "skip_wavelength" or samples
                or row is None or row[0] != "rejected" or evidence.get("reason") != "target_unreachable"
                or any(evidence.get(k) is not True for k in
                       ("ready", "laser_off_confirmed", "reference_verified", "electrical_guard_verified"))
                or not evidence.get("target_evidence", {}).get("skip_eligible")
                or evidence.get("optical_point_index") != context["axes"]["optical"]["index"]
                or evidence.get("wavelength_nm") != context["requested"]["optical_wavelength_nm"]):
            raise ValueError("A skipped condition requires a rejected attempt and verified wavelength guards")
        with self.connection:
            self.connection.execute(
                "INSERT INTO combination_skipped_conditions VALUES (?,?,?,?,?,?)",
                (run_id, condition_id, attempt, "target_unreachable", encode(evidence), utc_now()))
            self.connection.execute(
                "INSERT INTO combination_events(run_id,created_at_utc,event_type,payload_json) VALUES (?,?,?,?)",
                (run_id, utc_now(), "condition_skipped", encode({"condition_id": condition_id,
                    "attempt_index": attempt, **evidence})))

    def finish_run(self, run_id: str, status: str, cleanup: dict,
                   error: str | None) -> None:
        if status not in {"completed", "failed", "interrupted"}:
            raise ValueError("Invalid terminal run status")
        if status == "completed" and (cleanup.get("clean") is not True or error):
            raise ValueError("Completed run requires verified cleanup")
        with self.connection:
            if status == "completed":
                pending = self.connection.execute(
                    "SELECT COUNT(*) FROM combination_conditions c WHERE c.run_id=? "
                    "AND NOT EXISTS (SELECT 1 FROM combination_attempts a WHERE "
                    "a.run_id=c.run_id AND a.condition_id=c.condition_id AND a.status='accepted') "
                    "AND NOT EXISTS (SELECT 1 FROM combination_skipped_conditions s WHERE "
                    "s.run_id=c.run_id AND s.condition_id=c.condition_id)",
                    (run_id,),
                ).fetchone()[0]
                if pending:
                    raise ValueError("Cannot complete a run with pending conditions")
            self.connection.execute(
                "UPDATE combination_runs SET status=?, cleanup_json=?, error=? WHERE run_id=?",
                (status, encode(cleanup), error, run_id),
            )
            self.connection.execute(
                "INSERT INTO combination_events(run_id, created_at_utc, event_type, payload_json) "
                "VALUES (?, ?, 'run_finished', ?)",
                (run_id, utc_now(), encode({"status": status, "cleanup": cleanup, "error": error})),
            )


def _last_formal_measurement(connection, run_id, plan):
    """Project the latest accepted formal sample, excluding communication payloads."""
    row = connection.execute(
        "SELECT s.condition_id,s.attempt_index,s.sample_index,a.status AS attempt_status,"
        "c.context_json,json_extract(s.payload_json,'$.started_at_utc') AS started_at_utc,"
        "json_extract(s.payload_json,'$.finished_at_utc') AS finished_at_utc "
        "FROM combination_samples s JOIN combination_attempts a ON a.run_id=s.run_id "
        "AND a.condition_id=s.condition_id AND a.attempt_index=s.attempt_index "
        "JOIN combination_conditions c ON c.run_id=s.run_id AND c.condition_id=s.condition_id "
        "WHERE s.run_id=? AND a.status='accepted' "
        "ORDER BY a.finished_at_utc DESC,c.sequence_index DESC,a.attempt_index DESC,s.sample_index DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    if row is None:
        return None
    readings = {}
    # JSON projection keeps optical audit/transcripts/raw out of the monitor snapshot.
    for reading in connection.execute(
        "SELECT json_extract(r.value,'$.module') AS module,"
        "json_extract(r.value,'$.captured_at_utc') AS captured_at_utc,"
        "json_extract(r.value,'$.actual') AS actual,"
        "json_extract(r.value,'$.measurements') AS measurements,"
        "json_extract(r.value,'$.clean') AS clean,"
        "json_extract(r.value,'$.status.power_target_w') AS power_target_w,"
        "json_extract(r.value,'$.status.power_target_assessment.target_in_tolerance') AS target_in_tolerance,"
        "json_extract(r.value,'$.status.power_target_assessment.target_deviation_w') AS target_deviation_w "
        "FROM combination_samples s,json_each(s.payload_json,'$.reads') r "
        "WHERE s.run_id=? AND s.condition_id=? AND s.attempt_index=? AND s.sample_index=? "
        "AND json_extract(r.value,'$.module') IN ('optical','lockin')",
        (run_id,row["condition_id"],row["attempt_index"],row["sample_index"]),
    ):
        status = {}
        if reading["power_target_w"] is not None:
            status["power_target_w"] = reading["power_target_w"]
        if reading["target_in_tolerance"] is not None:
            status["power_target_assessment"] = {
                "target_in_tolerance": bool(reading["target_in_tolerance"]),
                "target_deviation_w": reading["target_deviation_w"]}
        readings[reading["module"]] = {
            "module":reading["module"],"captured_at_utc":reading["captured_at_utc"],
            "actual":json.loads(reading["actual"] or "{}"),
            "measurements":json.loads(reading["measurements"] or "{}"),
            "clean":bool(reading["clean"]) if reading["clean"] is not None else None,"status":status}
    result = {key:row[key] for key in ("condition_id","attempt_index","sample_index",
              "attempt_status","started_at_utc","finished_at_utc")}
    return {**result,"captured_at_utc":row["finished_at_utc"],
            "context":json.loads(row["context_json"]),
            "expected_samples":plan.get("samples_per_condition"),"modules":readings}


def run_snapshot(path: str | Path, run_id: str) -> dict:
    """Read an internally consistent snapshot; active is not a liveness claim."""
    with closing(open_readonly(path)) as connection:
        connection.execute("BEGIN")
        row = connection.execute(
            "SELECT * FROM combination_runs WHERE run_id=?", (run_id,)
        ).fetchone()
        if row is None:
            raise ValueError("Unknown combination run")
        accepted = connection.execute(
            "SELECT COUNT(*) FROM combination_attempts WHERE run_id=? AND status='accepted'",
            (run_id,),
        ).fetchone()[0]
        total = connection.execute(
            "SELECT COUNT(*) FROM combination_conditions WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        has_skips = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='combination_skipped_conditions'").fetchone()
        skipped = (connection.execute("SELECT count(*) FROM combination_skipped_conditions WHERE run_id=?",
                                      (run_id,)).fetchone()[0] if has_skips else 0)
        event = connection.execute(
            "SELECT * FROM combination_events WHERE run_id=? ORDER BY event_id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        attempt = connection.execute(
            "SELECT condition_id, attempt_index, status FROM combination_attempts "
            "WHERE run_id=? ORDER BY started_at_utc DESC, rowid DESC LIMIT 1", (run_id,),
        ).fetchone()
        phase = connection.execute(
            "SELECT created_at_utc, payload_json FROM combination_events WHERE run_id=? "
            "AND event_type='scan_phase' ORDER BY event_id DESC LIMIT 1", (run_id,),
        ).fetchone()
        plan = json.loads(row["plan_json"])
        progress = read_progress(connection, run_id, plan,
                                 dict(attempt) if attempt else None)
        phase_evidence = phase_snapshot(dict(phase) if phase else None,row["status"],
                                        dict(event) if event else None)
        from .monitor_environment import read_environment
        environment = read_environment(connection,run_id,plan,progress,phase_evidence)
        recovery = connection.execute(
            "SELECT created_at_utc, event_type, payload_json FROM combination_events "
            "WHERE run_id=? AND event_type IN ('reference_recovery_started', "
            "'reference_recovery_poll', 'reference_recovery_recovered', "
            "'reference_recovery_failed') ORDER BY event_id DESC LIMIT 1", (run_id,),
        ).fetchone()
        background = connection.execute(
            "SELECT created_at_utc, payload_json FROM combination_events WHERE run_id=? "
            "AND event_type='optical_background_status' ORDER BY event_id DESC LIMIT 1", (run_id,)
        ).fetchone()
        last_confirmed = {}
        for raw in connection.execute(
            "SELECT payload_json FROM combination_events WHERE run_id=? "
            "AND event_type='raw_reading' ORDER BY event_id DESC LIMIT 4", (run_id,)
        ):
            reading = json.loads(raw[0])["reading"]
            last_confirmed.setdefault(reading["module"], reading)
        return {
            "run_id": run_id, "status": row["status"],
            "accepted_conditions": accepted, "total_conditions": total,
            "skipped_conditions": skipped,
            "current_attempt": dict(attempt) if attempt else None,
            "progress": progress,
            "phase": phase_evidence,
            "environment": environment,
            "has_optical_axis": any(axis.get("module") == "optical" for axis in plan.get("axes",[])),
            "last_formal_measurement": _last_formal_measurement(connection,run_id,plan),
            "cleanup": json.loads(row["cleanup_json"]) if row["cleanup_json"] else None,
            "error": row["error"], "process_liveness": "unknown",
            "last_recorded_readings": last_confirmed,
            "dark_background": ({"created_at_utc": background["created_at_utc"],
                                 **json.loads(background["payload_json"])} if background else None),
            "reference_recovery": ({"created_at_utc": recovery["created_at_utc"],
                "event_type": recovery["event_type"], "payload": json.loads(recovery["payload_json"])}
                if recovery else None),
            "last_event": {**dict(event), "payload": json.loads(event["payload_json"])}
                if event else None,
        }
