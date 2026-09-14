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
                if len(samples) != expected_samples or not all(
                    json.loads(row[0]).get("clean") is True for row in samples
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
                    "a.run_id=c.run_id AND a.condition_id=c.condition_id AND a.status='accepted')",
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
        event = connection.execute(
            "SELECT * FROM combination_events WHERE run_id=? ORDER BY event_id DESC LIMIT 1",
            (run_id,),
        ).fetchone()
        attempt = connection.execute(
            "SELECT condition_id, attempt_index, status FROM combination_attempts "
            "WHERE run_id=? ORDER BY started_at_utc DESC, rowid DESC LIMIT 1", (run_id,),
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
            "current_attempt": dict(attempt) if attempt else None,
            "cleanup": json.loads(row["cleanup_json"]) if row["cleanup_json"] else None,
            "error": row["error"], "process_liveness": "unknown",
            "last_recorded_readings": last_confirmed,
            "last_event": {**dict(event), "payload": json.loads(event["payload_json"])}
                if event else None,
        }
