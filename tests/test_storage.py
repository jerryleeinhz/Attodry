from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from attodry_control.models import (
    CryostatState,
    GateState,
    LockinReading,
    LockinRole,
    VectorField,
)
from attodry_control.records import (
    AttemptRecord,
    AttemptStatus,
    ExperimentCondition,
    RawStationSample,
    RawTransportReading,
)
from attodry_control.storage import RunMonitor, RunStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def condition(condition_id: str = "condition-0001", sequence_index: int = 0):
    return ExperimentCondition(
        condition_id=condition_id,
        sequence_index=sequence_index,
        temperature_k=2.0,
        field=VectorField(0.0, 1.0),
        excitation_v=0.004,
        frequency_hz=17.777,
        gate_top_v=0.1,
        gate_bottom_v=-0.1,
    )


def raw(
    role: LockinRole,
    harmonic: int,
    *,
    condition_id: str = "condition-0001",
    attempt_index: int = 0,
    locked: bool = True,
    overload: bool = False,
) -> RawTransportReading:
    x_v = (1e-6 if role is LockinRole.XX else 1e-7) / harmonic
    return RawTransportReading(
        condition_id=condition_id,
        attempt_index=attempt_index,
        captured_at_utc=NOW,
        reading=LockinReading(
            role=role,
            harmonic=harmonic,
            x_v=x_v,
            y_v=0.0,
            amplitude_v=abs(x_v),
            phase_deg=0.0,
            frequency_hz=17.777,
            locked=locked,
            overload=overload,
        ),
    )


def station_sample(
    *, attempt_index: int = 0, safe: bool = True
) -> RawStationSample:
    field = VectorField(0.0, 1.0)
    cryostat = CryostatState(
        sample_temperature_k=2.0,
        user_temperature_k=2.0,
        vti_temperature_k=2.0,
        field=field,
        field_setpoint=field,
        temperature_control_enabled=True,
        field_control_enabled=True,
        error_code=0,
    )
    return RawStationSample(
        condition_id="condition-0001",
        attempt_index=attempt_index,
        captured_at_utc=NOW,
        cryostat=cryostat,
        gate_top=GateState("top", 0.1, 0.1, 1e-10, 1e-8, safe),
        gate_bottom=GateState("bottom", -0.1, -0.1, 1e-10, 1e-8, True),
        gate_top_leakage_limit_a=5e-9,
        gate_bottom_leakage_limit_a=5e-9,
    )


class StorageTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.NamedTemporaryFile(
            dir=PROJECT_ROOT, suffix=".sqlite", delete=False
        )
        temporary.close()
        self.path = Path(temporary.name)
        self.addCleanup(self._cleanup_files)
        self.store = RunStore(self.path)
        self.addCleanup(self.store.close)
        self.store.create_run("run-001", {"mode": "simulation"}, created_at_utc=NOW)
        self.store.register_condition("run-001", condition())

    def _cleanup_files(self) -> None:
        for suffix in ("", "-shm", "-wal"):
            path = Path(str(self.path) + suffix)
            if path.exists():
                path.unlink()

    def append_complete_reading_set(
        self, *, attempt_index: int = 0, locked: bool = True
    ) -> None:
        self.store.append_station_sample(
            "run-001", station_sample(attempt_index=attempt_index)
        )
        for harmonic in (1, 2, 3):
            for role in (LockinRole.XX, LockinRole.XY):
                self.store.append_raw_reading(
                    "run-001",
                    raw(
                        role,
                        harmonic,
                        attempt_index=attempt_index,
                        locked=locked,
                    ),
                )

    def test_database_uses_wal_and_contains_required_tables(self) -> None:
        mode = self.store.connection.execute("PRAGMA journal_mode").fetchone()[0]
        tables = {
            row[0]
            for row in self.store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }

        self.assertEqual(mode.lower(), "wal")
        self.assertTrue(
            {
                "runs",
                "events",
                "conditions",
                "attempts",
                "raw_instrument_samples",
                "station_samples",
                "transport_readings",
                "checkpoints",
            }
            <= tables
        )

    def test_accepted_completion_promotes_only_its_six_readings(self) -> None:
        attempt_index = self.store.start_attempt(
            "run-001", "condition-0001", started_at_utc=NOW
        )
        self.append_complete_reading_set(attempt_index=attempt_index)
        self.store.complete_attempt(
            "run-001",
            AttemptRecord(
                condition_id="condition-0001",
                attempt_index=attempt_index,
                started_at_utc=NOW,
                completed_at_utc=NOW + timedelta(seconds=1),
                status=AttemptStatus.ACCEPTED,
            ),
        )

        accepted = self.store.load_transport_readings("run-001")
        self.assertEqual(len(accepted), 6)
        self.assertTrue(all(reading.accepted for reading in accepted))

    def test_rejected_attempt_raw_data_is_retained_but_excluded_by_default(self) -> None:
        attempt_index = self.store.start_attempt(
            "run-001", "condition-0001", started_at_utc=NOW
        )
        self.store.append_raw_reading(
            "run-001",
            raw(LockinRole.XY, 1, locked=False, attempt_index=attempt_index),
        )
        self.store.complete_attempt(
            "run-001",
            AttemptRecord(
                condition_id="condition-0001",
                attempt_index=attempt_index,
                started_at_utc=NOW,
                completed_at_utc=NOW,
                status=AttemptStatus.REJECTED,
                rejection_reason="reference unlocked",
            ),
        )

        self.assertEqual(self.store.load_transport_readings("run-001"), ())
        all_readings = self.store.load_transport_readings(
            "run-001", accepted_only=False
        )
        self.assertEqual(len(all_readings), 1)
        self.assertFalse(all_readings[0].accepted)

    def test_incomplete_or_unsafe_attempt_cannot_be_accepted(self) -> None:
        attempt_index = self.store.start_attempt(
            "run-001", "condition-0001", started_at_utc=NOW
        )
        self.store.append_raw_reading(
            "run-001",
            raw(LockinRole.XX, 1, attempt_index=attempt_index),
        )

        with self.assertRaisesRegex(ValueError, "six safe"):
            self.store.complete_attempt(
                "run-001",
                AttemptRecord(
                    condition_id="condition-0001",
                    attempt_index=attempt_index,
                    started_at_utc=NOW,
                    completed_at_utc=NOW,
                    status=AttemptStatus.ACCEPTED,
                ),
            )

    def test_unsafe_station_sample_cannot_be_accepted(self) -> None:
        attempt_index = self.store.start_attempt(
            "run-001", "condition-0001", started_at_utc=NOW
        )
        self.store.append_station_sample(
            "run-001", station_sample(attempt_index=attempt_index, safe=False)
        )
        for harmonic in (1, 2, 3):
            for role in (LockinRole.XX, LockinRole.XY):
                self.store.append_raw_reading(
                    "run-001", raw(role, harmonic, attempt_index=attempt_index)
                )
        with self.assertRaisesRegex(ValueError, "safe station sample"):
            self.store.complete_attempt(
                "run-001",
                AttemptRecord(
                    "condition-0001",
                    attempt_index,
                    NOW,
                    NOW,
                    AttemptStatus.ACCEPTED,
                ),
            )

    def test_retry_uses_next_attempt_and_pending_clears_only_after_acceptance(self) -> None:
        first = self.store.start_attempt(
            "run-001", "condition-0001", started_at_utc=NOW
        )
        self.store.complete_attempt(
            "run-001",
            AttemptRecord(
                condition_id="condition-0001",
                attempt_index=first,
                started_at_utc=NOW,
                completed_at_utc=NOW,
                status=AttemptStatus.REJECTED,
                rejection_reason="injected failure",
            ),
        )
        self.assertEqual(self.store.pending_condition_ids("run-001"), ("condition-0001",))

        second = self.store.start_attempt(
            "run-001", "condition-0001", started_at_utc=NOW
        )
        self.assertEqual(second, 1)
        self.append_complete_reading_set(attempt_index=second)
        self.store.complete_attempt(
            "run-001",
            AttemptRecord(
                condition_id="condition-0001",
                attempt_index=second,
                started_at_utc=NOW,
                completed_at_utc=NOW,
                status=AttemptStatus.ACCEPTED,
            ),
        )
        self.assertEqual(self.store.pending_condition_ids("run-001"), ())

    def test_second_accepted_attempt_for_same_condition_is_rejected_by_database(self) -> None:
        first = self.store.start_attempt(
            "run-001", "condition-0001", started_at_utc=NOW
        )
        self.append_complete_reading_set(attempt_index=first)
        self.store.complete_attempt(
            "run-001",
            AttemptRecord(
                "condition-0001",
                first,
                NOW,
                NOW,
                AttemptStatus.ACCEPTED,
            ),
        )
        second = self.store.start_attempt(
            "run-001", "condition-0001", started_at_utc=NOW
        )
        self.append_complete_reading_set(attempt_index=second)

        with self.assertRaises(sqlite3.IntegrityError):
            self.store.complete_attempt(
                "run-001",
                AttemptRecord(
                    "condition-0001",
                    second,
                    NOW,
                    NOW,
                    AttemptStatus.ACCEPTED,
                ),
            )

    def test_events_and_monotonic_checkpoint_are_auditable(self) -> None:
        self.store.append_event(
            "run-001",
            event_type="run_started",
            message="simulation run started",
            payload={"operator": "test"},
            created_at_utc=NOW,
        )
        self.store.save_checkpoint("run-001", next_sequence_index=1, updated_at_utc=NOW)

        event = self.store.connection.execute(
            "SELECT payload_json FROM events WHERE run_id = ?", ("run-001",)
        ).fetchone()
        self.assertEqual(json.loads(event[0]), {"operator": "test"})
        self.assertEqual(self.store.load_checkpoint("run-001"), 1)
        with self.assertRaisesRegex(ValueError, "backwards"):
            self.store.save_checkpoint(
                "run-001", next_sequence_index=0, updated_at_utc=NOW
            )

    def test_read_only_monitor_summarizes_wal_without_writes(self) -> None:
        self.store.register_condition(
            "run-001", condition("condition-0002", sequence_index=1)
        )
        self.store.connection.commit()

        with RunMonitor(self.path) as monitor:
            summary = monitor.summary("run-001")
            self.assertEqual(summary.total_conditions, 2)
            self.assertEqual(summary.pending_conditions, 2)
            with self.assertRaises(sqlite3.OperationalError):
                monitor.connection.execute(
                    "INSERT INTO events(run_id, created_at_utc, level, event_type, message, payload_json) "
                    "VALUES ('run-001', 'x', 'INFO', 'x', 'x', '{}')"
                )

    def test_resume_rejects_incomplete_attempt_without_deleting_raw_data(self) -> None:
        attempt_index = self.store.start_attempt(
            "run-001", "condition-0001", started_at_utc=NOW
        )
        self.store.append_raw_reading(
            "run-001", raw(LockinRole.XX, 1, attempt_index=attempt_index)
        )

        recovered = self.store.reject_incomplete_attempts(
            "run-001", completed_at_utc=NOW + timedelta(seconds=5)
        )

        self.assertEqual(recovered, 1)
        attempt = self.store.connection.execute(
            "SELECT status, accepted, rejection_reason FROM attempts"
        ).fetchone()
        self.assertEqual(attempt["status"], "rejected")
        self.assertEqual(attempt["accepted"], 0)
        self.assertIn("interrupted", attempt["rejection_reason"])
        self.assertEqual(
            len(self.store.load_transport_readings("run-001", accepted_only=False)),
            1,
        )
        retry = self.store.start_attempt(
            "run-001", "condition-0001", started_at_utc=NOW + timedelta(seconds=6)
        )
        self.assertEqual(retry, 1)


class StorageMigrationTests(unittest.TestCase):
    def test_legacy_conditions_gain_explicit_noninferred_scan_id(self) -> None:
        temporary = tempfile.NamedTemporaryFile(
            dir=PROJECT_ROOT, suffix=".sqlite", delete=False
        )
        temporary.close()
        path = Path(temporary.name)
        self.addCleanup(
            lambda: [
                candidate.unlink()
                for candidate in (
                    path,
                    Path(str(path) + "-shm"),
                    Path(str(path) + "-wal"),
                )
                if candidate.exists()
            ]
        )
        connection = sqlite3.connect(path)
        connection.execute(
            """
            CREATE TABLE conditions (
                run_id TEXT NOT NULL,
                condition_id TEXT NOT NULL,
                sequence_index INTEGER NOT NULL,
                temperature_k REAL NOT NULL,
                bx_t REAL NOT NULL,
                bz_t REAL NOT NULL,
                excitation_v REAL NOT NULL,
                frequency_hz REAL NOT NULL,
                gate_top_v REAL NOT NULL,
                gate_bottom_v REAL NOT NULL,
                PRIMARY KEY (run_id, condition_id)
            )
            """
        )
        connection.execute(
            "INSERT INTO conditions VALUES "
            "('old-run','old-condition',0,2.0,0.0,0.0,0.004,17.777,0.0,0.0)"
        )
        connection.commit()
        connection.close()

        with RunStore(path) as store:
            migrated = store.connection.execute(
                "SELECT scan_id FROM conditions WHERE condition_id='old-condition'"
            ).fetchone()[0]
            version = store.connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(migrated, "legacy")
        self.assertEqual(version, 2)


if __name__ == "__main__":
    unittest.main()
