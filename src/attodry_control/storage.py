from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from .models import LockinRole
from .records import (
    AttemptRecord,
    AttemptStatus,
    ExperimentCondition,
    RawStationSample,
    RawTransportReading,
)


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    created_at_utc TEXT NOT NULL,
    config_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'complete', 'failed', 'aborted'))
);

CREATE TABLE IF NOT EXISTS events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    condition_id TEXT,
    attempt_index INTEGER,
    created_at_utc TEXT NOT NULL,
    level TEXT NOT NULL,
    event_type TEXT NOT NULL,
    message TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conditions (
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    condition_id TEXT NOT NULL,
    scan_id TEXT NOT NULL,
    sequence_index INTEGER NOT NULL CHECK (sequence_index >= 0),
    temperature_k REAL NOT NULL CHECK (temperature_k > 0),
    bx_t REAL NOT NULL,
    bz_t REAL NOT NULL,
    excitation_v REAL NOT NULL CHECK (excitation_v >= 0),
    frequency_hz REAL NOT NULL CHECK (frequency_hz > 0),
    gate_top_v REAL NOT NULL,
    gate_bottom_v REAL NOT NULL,
    PRIMARY KEY (run_id, condition_id),
    UNIQUE (run_id, sequence_index),
    CHECK (ABS(bx_t) <= 3.0),
    CHECK (ABS(bz_t) <= 9.0),
    CHECK (bx_t * bx_t + bz_t * bz_t <= 9.000000000001)
);

CREATE TABLE IF NOT EXISTS attempts (
    run_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    attempt_index INTEGER NOT NULL CHECK (attempt_index >= 0),
    started_at_utc TEXT NOT NULL,
    completed_at_utc TEXT,
    status TEXT NOT NULL CHECK (status IN ('started', 'accepted', 'rejected')),
    accepted INTEGER NOT NULL DEFAULT 0 CHECK (accepted IN (0, 1)),
    rejection_reason TEXT,
    PRIMARY KEY (run_id, condition_id, attempt_index),
    FOREIGN KEY (run_id, condition_id)
        REFERENCES conditions(run_id, condition_id),
    CHECK (
        (status = 'accepted' AND accepted = 1 AND completed_at_utc IS NOT NULL
            AND rejection_reason IS NULL)
        OR
        (status = 'rejected' AND accepted = 0 AND completed_at_utc IS NOT NULL
            AND rejection_reason IS NOT NULL)
        OR
        (status = 'started' AND accepted = 0 AND completed_at_utc IS NULL
            AND rejection_reason IS NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS one_accepted_attempt_per_condition
ON attempts(run_id, condition_id) WHERE accepted = 1;

CREATE TABLE IF NOT EXISTS raw_instrument_samples (
    sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    attempt_index INTEGER NOT NULL,
    captured_at_utc TEXT NOT NULL,
    instrument_role TEXT NOT NULL CHECK (instrument_role IN ('xx', 'xy')),
    sample_kind TEXT NOT NULL,
    accepted INTEGER NOT NULL DEFAULT 0 CHECK (accepted IN (0, 1)),
    payload_json TEXT NOT NULL,
    FOREIGN KEY (run_id, condition_id, attempt_index)
        REFERENCES attempts(run_id, condition_id, attempt_index)
);

CREATE TABLE IF NOT EXISTS transport_readings (
    reading_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    attempt_index INTEGER NOT NULL,
    captured_at_utc TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('xx', 'xy')),
    harmonic INTEGER NOT NULL CHECK (harmonic IN (1, 2, 3)),
    x_v REAL NOT NULL,
    y_v REAL NOT NULL,
    amplitude_v REAL NOT NULL CHECK (amplitude_v >= 0),
    phase_deg REAL NOT NULL,
    phase_shift_deg REAL NOT NULL,
    frequency_hz REAL NOT NULL CHECK (frequency_hz > 0),
    locked INTEGER NOT NULL CHECK (locked IN (0, 1)),
    overload INTEGER NOT NULL CHECK (overload IN (0, 1)),
    accepted INTEGER NOT NULL DEFAULT 0 CHECK (accepted IN (0, 1)),
    UNIQUE (run_id, condition_id, attempt_index, role, harmonic),
    FOREIGN KEY (run_id, condition_id, attempt_index)
        REFERENCES attempts(run_id, condition_id, attempt_index)
);

CREATE TABLE IF NOT EXISTS station_samples (
    sample_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    condition_id TEXT NOT NULL,
    attempt_index INTEGER NOT NULL,
    captured_at_utc TEXT NOT NULL,
    accepted INTEGER NOT NULL DEFAULT 0 CHECK (accepted IN (0, 1)),
    safe_for_acceptance INTEGER NOT NULL CHECK (safe_for_acceptance IN (0, 1)),
    sample_temperature_k REAL NOT NULL CHECK (sample_temperature_k > 0),
    user_temperature_k REAL NOT NULL CHECK (user_temperature_k > 0),
    vti_temperature_k REAL NOT NULL CHECK (vti_temperature_k > 0),
    bx_t REAL NOT NULL,
    bz_t REAL NOT NULL,
    bx_setpoint_t REAL NOT NULL,
    bz_setpoint_t REAL NOT NULL,
    temperature_control_enabled INTEGER NOT NULL
        CHECK (temperature_control_enabled IN (0, 1)),
    field_control_enabled INTEGER NOT NULL CHECK (field_control_enabled IN (0, 1)),
    cryostat_error_code INTEGER NOT NULL,
    cryostat_error_message TEXT NOT NULL,
    gate_top_set_v REAL NOT NULL,
    gate_top_read_v REAL NOT NULL,
    gate_top_leakage_a REAL,
    gate_top_compliance_a REAL NOT NULL CHECK (gate_top_compliance_a > 0),
    gate_top_leakage_limit_a REAL NOT NULL CHECK (gate_top_leakage_limit_a > 0),
    gate_top_output_enabled INTEGER NOT NULL CHECK (gate_top_output_enabled IN (0, 1)),
    gate_bottom_set_v REAL NOT NULL,
    gate_bottom_read_v REAL NOT NULL,
    gate_bottom_leakage_a REAL,
    gate_bottom_compliance_a REAL NOT NULL CHECK (gate_bottom_compliance_a > 0),
    gate_bottom_leakage_limit_a REAL NOT NULL
        CHECK (gate_bottom_leakage_limit_a > 0),
    gate_bottom_output_enabled INTEGER NOT NULL
        CHECK (gate_bottom_output_enabled IN (0, 1)),
    UNIQUE (run_id, condition_id, attempt_index),
    FOREIGN KEY (run_id, condition_id, attempt_index)
        REFERENCES attempts(run_id, condition_id, attempt_index)
);

CREATE TABLE IF NOT EXISTS checkpoints (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
    next_sequence_index INTEGER NOT NULL CHECK (next_sequence_index >= 0),
    updated_at_utc TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS transport_accept_requires_accepted_attempt_insert
BEFORE INSERT ON transport_readings
WHEN NEW.accepted = 1 AND NOT EXISTS (
    SELECT 1 FROM attempts
    WHERE run_id = NEW.run_id
      AND condition_id = NEW.condition_id
      AND attempt_index = NEW.attempt_index
      AND accepted = 1
)
BEGIN
    SELECT RAISE(ABORT, 'accepted reading requires accepted attempt');
END;

CREATE TRIGGER IF NOT EXISTS transport_accept_requires_accepted_attempt_update
BEFORE UPDATE OF accepted ON transport_readings
WHEN NEW.accepted = 1 AND NOT EXISTS (
    SELECT 1 FROM attempts
    WHERE run_id = NEW.run_id
      AND condition_id = NEW.condition_id
      AND attempt_index = NEW.attempt_index
      AND accepted = 1
)
BEGIN
    SELECT RAISE(ABORT, 'accepted reading requires accepted attempt');
END;

CREATE TRIGGER IF NOT EXISTS raw_accept_requires_accepted_attempt_update
BEFORE UPDATE OF accepted ON raw_instrument_samples
WHEN NEW.accepted = 1 AND NOT EXISTS (
    SELECT 1 FROM attempts
    WHERE run_id = NEW.run_id
      AND condition_id = NEW.condition_id
      AND attempt_index = NEW.attempt_index
      AND accepted = 1
)
BEGIN
    SELECT RAISE(ABORT, 'accepted raw sample requires accepted attempt');
END;

CREATE TRIGGER IF NOT EXISTS station_accept_requires_accepted_attempt_update
BEFORE UPDATE OF accepted ON station_samples
WHEN NEW.accepted = 1 AND NOT EXISTS (
    SELECT 1 FROM attempts
    WHERE run_id = NEW.run_id
      AND condition_id = NEW.condition_id
      AND attempt_index = NEW.attempt_index
      AND accepted = 1
)
BEGIN
    SELECT RAISE(ABORT, 'accepted station sample requires accepted attempt');
END;
"""


@dataclass(frozen=True, slots=True)
class StoredTransportReading:
    condition_id: str
    attempt_index: int
    captured_at_utc: datetime
    role: LockinRole
    harmonic: int
    x_v: float
    y_v: float
    amplitude_v: float
    phase_deg: float
    phase_shift_deg: float
    frequency_hz: float
    locked: bool
    overload: bool
    accepted: bool


@dataclass(frozen=True, slots=True)
class RunSummary:
    run_id: str
    status: str
    total_conditions: int
    accepted_conditions: int
    pending_conditions: int
    total_attempts: int
    rejected_attempts: int


class RunStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.connection = sqlite3.connect(self.path, timeout=5.0)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA synchronous = FULL")
        self.connection.executescript(SCHEMA)
        self._migrate_schema()

    def __enter__(self) -> RunStore:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def _migrate_schema(self) -> None:
        condition_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(conditions)")
        }
        with self.connection:
            if "scan_id" not in condition_columns:
                self.connection.execute(
                    "ALTER TABLE conditions ADD COLUMN scan_id TEXT NOT NULL "
                    "DEFAULT 'legacy'"
                )
            transport_columns = {
                row["name"]
                for row in self.connection.execute(
                    "PRAGMA table_info(transport_readings)"
                )
            }
            if "phase_shift_deg" not in transport_columns:
                self.connection.execute(
                    "ALTER TABLE transport_readings ADD COLUMN "
                    "phase_shift_deg REAL NOT NULL DEFAULT 0.0"
                )
            self.connection.execute("PRAGMA user_version = 3")

    def create_run(
        self,
        run_id: str,
        config_snapshot: Mapping[str, Any],
        *,
        created_at_utc: datetime,
    ) -> None:
        _nonempty(run_id, "run_id")
        with self.connection:
            self.connection.execute(
                "INSERT INTO runs(run_id, created_at_utc, config_json) VALUES (?, ?, ?)",
                (
                    run_id,
                    _timestamp_text(created_at_utc),
                    _json(config_snapshot),
                ),
            )

    def register_condition(self, run_id: str, condition: ExperimentCondition) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO conditions(
                    run_id, condition_id, scan_id, sequence_index, temperature_k,
                    bx_t, bz_t, excitation_v, frequency_hz,
                    gate_top_v, gate_bottom_v
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    condition.condition_id,
                    condition.scan_id,
                    condition.sequence_index,
                    condition.temperature_k,
                    condition.field.bx_t,
                    condition.field.bz_t,
                    condition.excitation_v,
                    condition.frequency_hz,
                    condition.gate_top_v,
                    condition.gate_bottom_v,
                ),
            )

    def start_attempt(
        self,
        run_id: str,
        condition_id: str,
        *,
        started_at_utc: datetime,
    ) -> int:
        started = _timestamp_text(started_at_utc)
        with self.connection:
            row = self.connection.execute(
                """
                SELECT COALESCE(MAX(attempt_index), -1) + 1 AS next_index
                FROM attempts WHERE run_id = ? AND condition_id = ?
                """,
                (run_id, condition_id),
            ).fetchone()
            attempt_index = int(row["next_index"])
            self.connection.execute(
                """
                INSERT INTO attempts(
                    run_id, condition_id, attempt_index, started_at_utc,
                    status, accepted
                ) VALUES (?, ?, ?, ?, 'started', 0)
                """,
                (run_id, condition_id, attempt_index, started),
            )
        return attempt_index

    def append_raw_reading(self, run_id: str, raw: RawTransportReading) -> None:
        reading = raw.reading
        payload = asdict(reading)
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO raw_instrument_samples(
                    run_id, condition_id, attempt_index, captured_at_utc,
                    instrument_role, sample_kind, accepted, payload_json
                ) VALUES (?, ?, ?, ?, ?, 'lockin_transport', 0, ?)
                """,
                (
                    run_id,
                    raw.condition_id,
                    raw.attempt_index,
                    _timestamp_text(raw.captured_at_utc),
                    reading.role.value,
                    _json(payload),
                ),
            )
            self.connection.execute(
                """
                INSERT INTO transport_readings(
                    run_id, condition_id, attempt_index, captured_at_utc,
                    role, harmonic, x_v, y_v, amplitude_v, phase_deg,
                    phase_shift_deg, frequency_hz, locked, overload, accepted
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
                """,
                (
                    run_id,
                    raw.condition_id,
                    raw.attempt_index,
                    _timestamp_text(raw.captured_at_utc),
                    reading.role.value,
                    reading.harmonic,
                    reading.x_v,
                    reading.y_v,
                    reading.amplitude_v,
                    reading.phase_deg,
                    reading.phase_shift_deg,
                    reading.frequency_hz,
                    int(reading.locked),
                    int(reading.overload),
                ),
            )

    def append_station_sample(self, run_id: str, sample: RawStationSample) -> None:
        cryostat = sample.cryostat
        top = sample.gate_top
        bottom = sample.gate_bottom
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO station_samples(
                    run_id, condition_id, attempt_index, captured_at_utc,
                    accepted, safe_for_acceptance,
                    sample_temperature_k, user_temperature_k, vti_temperature_k,
                    bx_t, bz_t, bx_setpoint_t, bz_setpoint_t,
                    temperature_control_enabled, field_control_enabled,
                    cryostat_error_code, cryostat_error_message,
                    gate_top_set_v, gate_top_read_v, gate_top_leakage_a,
                    gate_top_compliance_a, gate_top_leakage_limit_a,
                    gate_top_output_enabled,
                    gate_bottom_set_v, gate_bottom_read_v, gate_bottom_leakage_a,
                    gate_bottom_compliance_a, gate_bottom_leakage_limit_a,
                    gate_bottom_output_enabled
                ) VALUES (
                    ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    run_id,
                    sample.condition_id,
                    sample.attempt_index,
                    _timestamp_text(sample.captured_at_utc),
                    int(sample.safe_for_acceptance),
                    cryostat.sample_temperature_k,
                    cryostat.user_temperature_k,
                    cryostat.vti_temperature_k,
                    cryostat.field.bx_t,
                    cryostat.field.bz_t,
                    cryostat.field_setpoint.bx_t,
                    cryostat.field_setpoint.bz_t,
                    int(cryostat.temperature_control_enabled),
                    int(cryostat.field_control_enabled),
                    cryostat.error_code,
                    cryostat.error_message,
                    top.voltage_set_v,
                    top.voltage_read_v,
                    top.leakage_current_a,
                    top.compliance_a,
                    sample.gate_top_leakage_limit_a,
                    int(top.output_enabled),
                    bottom.voltage_set_v,
                    bottom.voltage_read_v,
                    bottom.leakage_current_a,
                    bottom.compliance_a,
                    sample.gate_bottom_leakage_limit_a,
                    int(bottom.output_enabled),
                ),
            )

    def complete_attempt(self, run_id: str, attempt: AttemptRecord) -> None:
        if attempt.status is AttemptStatus.STARTED:
            raise ValueError("complete_attempt requires accepted or rejected status.")
        with self.connection:
            existing = self.connection.execute(
                """
                SELECT started_at_utc FROM attempts
                WHERE run_id = ? AND condition_id = ? AND attempt_index = ?
                """,
                (run_id, attempt.condition_id, attempt.attempt_index),
            ).fetchone()
            if existing is None:
                raise ValueError("Attempt was not started in this run.")
            if datetime.fromisoformat(existing["started_at_utc"]) != attempt.started_at_utc:
                raise ValueError("Attempt start timestamp does not match stored audit data.")
            if attempt.accepted:
                aggregate = self.connection.execute(
                    """
                    SELECT COUNT(*) AS reading_count,
                           SUM(locked) AS locked_count,
                           SUM(overload) AS overload_count
                    FROM transport_readings
                    WHERE run_id = ? AND condition_id = ? AND attempt_index = ?
                    """,
                    (run_id, attempt.condition_id, attempt.attempt_index),
                ).fetchone()
                if (
                    aggregate["reading_count"] != 6
                    or aggregate["locked_count"] != 6
                    or aggregate["overload_count"] != 0
                ):
                    raise ValueError(
                        "An accepted attempt requires exactly six safe xx/xy "
                        "harmonic readings."
                    )
                station = self.connection.execute(
                    """
                    SELECT COUNT(*) AS sample_count,
                           SUM(safe_for_acceptance) AS safe_count
                    FROM station_samples
                    WHERE run_id = ? AND condition_id = ? AND attempt_index = ?
                    """,
                    (run_id, attempt.condition_id, attempt.attempt_index),
                ).fetchone()
                if station["sample_count"] != 1 or station["safe_count"] != 1:
                    raise ValueError(
                        "An accepted attempt requires exactly one safe station sample."
                    )
            cursor = self.connection.execute(
                """
                UPDATE attempts
                SET completed_at_utc = ?, status = ?, accepted = ?, rejection_reason = ?
                WHERE run_id = ? AND condition_id = ? AND attempt_index = ?
                  AND status = 'started'
                """,
                (
                    _timestamp_text(attempt.completed_at_utc),
                    attempt.status.value,
                    int(attempt.accepted),
                    attempt.rejection_reason,
                    run_id,
                    attempt.condition_id,
                    attempt.attempt_index,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("Attempt was already completed.")
            if attempt.accepted:
                key = (run_id, attempt.condition_id, attempt.attempt_index)
                self.connection.execute(
                    """
                    UPDATE raw_instrument_samples SET accepted = 1
                    WHERE run_id = ? AND condition_id = ? AND attempt_index = ?
                    """,
                    key,
                )
                self.connection.execute(
                    """
                    UPDATE station_samples SET accepted = 1
                    WHERE run_id = ? AND condition_id = ? AND attempt_index = ?
                    """,
                    key,
                )
                self.connection.execute(
                    """
                    UPDATE transport_readings SET accepted = 1
                    WHERE run_id = ? AND condition_id = ? AND attempt_index = ?
                    """,
                    key,
                )

    def load_transport_readings(
        self, run_id: str, *, accepted_only: bool = True
    ) -> tuple[StoredTransportReading, ...]:
        predicate = "AND tr.accepted = 1" if accepted_only else ""
        rows = self.connection.execute(
            f"""
            SELECT tr.*
            FROM transport_readings AS tr
            JOIN conditions AS c
              ON c.run_id = tr.run_id AND c.condition_id = tr.condition_id
            WHERE tr.run_id = ? {predicate}
            ORDER BY c.sequence_index, tr.attempt_index, tr.harmonic,
                     CASE tr.role WHEN 'xx' THEN 0 ELSE 1 END
            """,
            (run_id,),
        ).fetchall()
        return tuple(
            StoredTransportReading(
                condition_id=row["condition_id"],
                attempt_index=row["attempt_index"],
                captured_at_utc=datetime.fromisoformat(row["captured_at_utc"]),
                role=LockinRole(row["role"]),
                harmonic=row["harmonic"],
                x_v=row["x_v"],
                y_v=row["y_v"],
                amplitude_v=row["amplitude_v"],
                phase_deg=row["phase_deg"],
                phase_shift_deg=row["phase_shift_deg"],
                frequency_hz=row["frequency_hz"],
                locked=bool(row["locked"]),
                overload=bool(row["overload"]),
                accepted=bool(row["accepted"]),
            )
            for row in rows
        )

    def pending_condition_ids(self, run_id: str) -> tuple[str, ...]:
        rows = self.connection.execute(
            """
            SELECT c.condition_id
            FROM conditions AS c
            WHERE c.run_id = ?
              AND NOT EXISTS (
                  SELECT 1 FROM attempts AS a
                  WHERE a.run_id = c.run_id
                    AND a.condition_id = c.condition_id
                    AND a.accepted = 1
              )
            ORDER BY c.sequence_index
            """,
            (run_id,),
        ).fetchall()
        return tuple(row["condition_id"] for row in rows)

    def reject_incomplete_attempts(
        self,
        run_id: str,
        *,
        completed_at_utc: datetime,
        reason: str = "interrupted before completion; rejected during resume",
    ) -> int:
        _nonempty(reason, "reason")
        with self.connection:
            cursor = self.connection.execute(
                """
                UPDATE attempts
                SET completed_at_utc = ?, status = 'rejected', accepted = 0,
                    rejection_reason = ?
                WHERE run_id = ? AND status = 'started'
                """,
                (_timestamp_text(completed_at_utc), reason, run_id),
            )
        return cursor.rowcount

    def set_run_status(self, run_id: str, status: str) -> None:
        if status not in {"active", "complete", "failed", "aborted"}:
            raise ValueError("Invalid run status.")
        with self.connection:
            cursor = self.connection.execute(
                "UPDATE runs SET status = ? WHERE run_id = ?", (status, run_id)
            )
            if cursor.rowcount != 1:
                raise ValueError(f"Unknown run_id {run_id!r}.")

    def append_event(
        self,
        run_id: str,
        *,
        event_type: str,
        message: str,
        payload: Mapping[str, Any] | None = None,
        level: str = "INFO",
        condition_id: str | None = None,
        attempt_index: int | None = None,
        created_at_utc: datetime,
    ) -> None:
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO events(
                    run_id, condition_id, attempt_index, created_at_utc,
                    level, event_type, message, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    condition_id,
                    attempt_index,
                    _timestamp_text(created_at_utc),
                    level,
                    event_type,
                    message,
                    _json(payload or {}),
                ),
            )

    def save_checkpoint(
        self,
        run_id: str,
        *,
        next_sequence_index: int,
        updated_at_utc: datetime,
    ) -> None:
        if isinstance(next_sequence_index, bool) or next_sequence_index < 0:
            raise ValueError("next_sequence_index must be non-negative.")
        current = self.load_checkpoint(run_id)
        if current is not None and next_sequence_index < current:
            raise ValueError("Checkpoint cannot move backwards.")
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO checkpoints(run_id, next_sequence_index, updated_at_utc)
                VALUES (?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    next_sequence_index = excluded.next_sequence_index,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (run_id, next_sequence_index, _timestamp_text(updated_at_utc)),
            )

    def load_checkpoint(self, run_id: str) -> int | None:
        row = self.connection.execute(
            "SELECT next_sequence_index FROM checkpoints WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        return None if row is None else int(row["next_sequence_index"])


class RunMonitor:
    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        uri = self.path.as_uri() + "?mode=ro"
        self.connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA query_only = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")

    def __enter__(self) -> RunMonitor:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def summary(self, run_id: str) -> RunSummary:
        run = self.connection.execute(
            "SELECT status FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if run is None:
            raise ValueError(f"Unknown run_id {run_id!r}.")
        condition_counts = self.connection.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN EXISTS (
                       SELECT 1 FROM attempts AS a
                       WHERE a.run_id = c.run_id
                         AND a.condition_id = c.condition_id
                         AND a.accepted = 1
                   ) THEN 1 ELSE 0 END) AS accepted
            FROM conditions AS c WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        attempt_counts = self.connection.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) AS rejected
            FROM attempts WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()
        total_conditions = int(condition_counts["total"] or 0)
        accepted_conditions = int(condition_counts["accepted"] or 0)
        return RunSummary(
            run_id=run_id,
            status=run["status"],
            total_conditions=total_conditions,
            accepted_conditions=accepted_conditions,
            pending_conditions=total_conditions - accepted_conditions,
            total_attempts=int(attempt_counts["total"] or 0),
            rejected_attempts=int(attempt_counts["rejected"] or 0),
        )


def _timestamp_text(value: datetime | None) -> str:
    if value is None:
        raise ValueError("A completed timestamp is required.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamps must be timezone-aware UTC.")
    if value.utcoffset() != timedelta(0):
        raise ValueError("Timestamps must use UTC.")
    return value.isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _nonempty(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string.")
