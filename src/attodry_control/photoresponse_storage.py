"""Photoresponse SQLite audit extension; existing SR830 storage tables are untouched."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from .nkt_config import NktError
from .nkt_control import utc_now


@contextmanager
def run_lock(path):
    """Process lock, automatically released after a crash; prevents concurrent resume."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        stream.seek(0, 2)
        if not stream.tell():
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        import os
        if os.name == "nt":
            import msvcrt
            try: msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc: raise NktError("Another photoresponse run owns this output/database") from exc
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try: yield
        finally:
            stream.seek(0)
            if os.name == "nt": msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else: fcntl.flock(stream, fcntl.LOCK_UN)


class PhotoStore:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS photo_runs (
                run_id TEXT PRIMARY KEY, series_id TEXT NOT NULL, attempt_index INTEGER NOT NULL,
                plan_hash TEXT NOT NULL, created_utc TEXT NOT NULL, outcome TEXT NOT NULL,
                config_json TEXT NOT NULL, record_json TEXT);
            CREATE TABLE IF NOT EXISTS photo_events (
                event_id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, event_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS photo_conditions (
                run_id TEXT NOT NULL, series_id TEXT NOT NULL, condition_id TEXT NOT NULL,
                attempt_index INTEGER NOT NULL, accepted INTEGER NOT NULL DEFAULT 0,
                condition_json TEXT NOT NULL, PRIMARY KEY(run_id, condition_id));
            CREATE UNIQUE INDEX IF NOT EXISTS photo_one_accepted
                ON photo_conditions(series_id, condition_id) WHERE accepted=1;
        """)
        self.run_id = None

    def start(self, config, *, resume=None):
        encoded = json.dumps(config.snapshot(), sort_keys=True, allow_nan=False)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        self.run_id = uuid4().hex
        self.series_id, self.attempt_index = self.run_id, 1
        with self.db:
            if resume:
                previous = self.db.execute("SELECT series_id,plan_hash,outcome FROM photo_runs WHERE run_id=?", (resume,)).fetchone()
                if previous is None: raise NktError("Resume run_id does not exist")
                if previous[1] != digest: raise NktError("Resume configuration differs from archived plan")
                self.series_id = previous[0]
                if self.db.execute("SELECT 1 FROM photo_runs WHERE series_id=? AND outcome='completed'", (self.series_id,)).fetchone():
                    raise NktError("This series already has a completed accepted attempt")
                self.attempt_index = self.db.execute("SELECT MAX(attempt_index)+1 FROM photo_runs WHERE series_id=?", (self.series_id,)).fetchone()[0]
                # Whole-run acceptance means every condition of a failed attempt is
                # retried; partial dark/light windows are never joined across runs.
                self.db.execute("UPDATE photo_runs SET outcome='interrupted' WHERE series_id=? AND outcome='running'", (self.series_id,))
            self.db.execute("INSERT INTO photo_runs VALUES (?,?,?,?,?,?,?,NULL)",
                (self.run_id,self.series_id,self.attempt_index,digest,utc_now(),"running",encoded))
        return self.run_id, self.attempt_index

    def event(self, event):
        # Import lazily to keep analysis free of hardware factories.
        from .photoresponse_cli import audit_safe
        encoded = json.dumps(audit_safe(event), allow_nan=False)
        with self.db:
            self.db.execute("INSERT INTO photo_events(run_id,event_json) VALUES (?,?)", (self.run_id,encoded))
            if event.get("phase") == "condition_complete":
                row = event["condition"]
                self.db.execute("INSERT INTO photo_conditions VALUES (?,?,?,?,0,?)",
                    (self.run_id,self.series_id,row["condition_id"],self.attempt_index,json.dumps(audit_safe(row),allow_nan=False)))

    def finalize(self, record):
        from .photoresponse_cli import audit_safe
        record["run_id"], record["series_id"] = self.run_id, self.series_id
        clean = (record.get("completed") is True and record.get("outcome") == "completed"
                 and record["cleanup"].get("laser_off_confirmed") is True
                 and record["cleanup"].get("electrical_safe_confirmed") is True
                 and not record["cleanup"].get("errors")
                 and (not record.get("measurement_config", {}).get("smu")
                      or record["cleanup"].get("smu_safe_confirmed") is True))
        if record.get("completed") and not clean: raise NktError("Unsafe accepted promotion")
        with self.db:
            for row in record.get("conditions", []):
                accepted = clean and row.get("accepted") is True
                self.db.execute("INSERT OR REPLACE INTO photo_conditions VALUES (?,?,?,?,?,?)",
                    (self.run_id,self.series_id,row["condition_id"],self.attempt_index,int(accepted),
                     json.dumps(audit_safe(row),allow_nan=False)))
            self.db.execute("UPDATE photo_runs SET outcome=?,record_json=? WHERE run_id=?",
                (record["outcome"],json.dumps(audit_safe(record),allow_nan=False),self.run_id))

    def close(self): self.db.close()
