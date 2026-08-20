from datetime import UTC, datetime
import json
from pathlib import Path
import tempfile
import unittest

from attodry_control.acquisition import RetryLimitExceeded, SimulationRunEngine
from attodry_control.config import FieldEndPolicy, load_config
from attodry_control.models import VectorField
from attodry_control.records import ExperimentCondition
from attodry_control.simulation import SimulationStation
from attodry_control.storage import RunMonitor, RunStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def condition(condition_id: str = "condition-0001") -> ExperimentCondition:
    return ExperimentCondition(
        condition_id=condition_id,
        sequence_index=0,
        temperature_k=2.0,
        field=VectorField(0.2, 0.3),
        excitation_v=0.004,
        frequency_hz=17.777,
        gate_top_v=0.1,
        gate_bottom_v=-0.1,
    )


class AcquisitionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.NamedTemporaryFile(
            dir=PROJECT_ROOT, suffix=".sqlite", delete=False
        )
        temporary.close()
        self.path = Path(temporary.name)
        self.addCleanup(self._cleanup_files)
        self.store = RunStore(self.path)
        self.addCleanup(self.store.close)
        self.config = load_config("config/simulation.toml")

    def _cleanup_files(self) -> None:
        for suffix in ("", "-shm", "-wal"):
            path = Path(str(self.path) + suffix)
            if path.exists():
                path.unlink()

    def test_rejected_raw_is_retained_then_retry_is_accepted(self) -> None:
        stations: list[SimulationStation] = []

        def factory() -> SimulationStation:
            station = SimulationStation.from_config(self.config)
            if not stations:
                station.lockin_xy.locked = False
            stations.append(station)
            return station

        engine = SimulationRunEngine(
            store=self.store,
            run_id="run-e2e",
            station_factory=factory,
            max_attempts_per_condition=2,
            normal_end_field_policy=FieldEndPolicy.HOLD,
            now=lambda: NOW,
        )
        summary = engine.start_new(
            (condition(),), config_snapshot={"mode": "simulation"}
        )

        self.assertEqual(summary.accepted_conditions, 1)
        self.assertEqual(summary.rejected_attempts, 1)
        self.assertEqual(len(self.store.load_transport_readings("run-e2e")), 6)
        self.assertEqual(
            len(
                self.store.load_transport_readings(
                    "run-e2e", accepted_only=False
                )
            ),
            8,
        )
        attempts = self.store.connection.execute(
            "SELECT status, accepted FROM attempts ORDER BY attempt_index"
        ).fetchall()
        self.assertEqual(
            [(row["status"], row["accepted"]) for row in attempts],
            [("rejected", 0), ("accepted", 1)],
        )
        self.assertEqual(self.store.pending_condition_ids("run-e2e"), ())
        station_rows = self.store.connection.execute(
            "SELECT accepted, safe_for_acceptance FROM station_samples "
            "ORDER BY attempt_index"
        ).fetchall()
        self.assertEqual(
            [(row["accepted"], row["safe_for_acceptance"]) for row in station_rows],
            [(0, 1), (1, 1)],
        )
        self.assertNotIn(
            "cryostat.request_zero_field",
            stations[-1].event_log,
        )
        self.assertEqual(stations[-1].lockin_xx.source_voltage_v, 0.004)
        self.assertFalse(stations[-1].gate_top.output_enabled)
        self.assertFalse(stations[-1].gate_bottom.output_enabled)
        event_payloads = [
            json.loads(row["payload_json"])
            for row in self.store.connection.execute(
                "SELECT payload_json FROM events "
                "WHERE run_id = ? AND event_type = 'attempt_completed' "
                "ORDER BY attempt_index",
                ("run-e2e",),
            ).fetchall()
        ]
        self.assertTrue(event_payloads[0]["field_zero_confirmed"])
        self.assertEqual(
            event_payloads[0]["last_confirmed_cryostat_state"]["bx_t"], 0.0
        )
        self.assertFalse(event_payloads[1]["field_zero_required"])
        self.assertEqual(
            event_payloads[1]["last_confirmed_cryostat_state"]["bx_t"], 0.2
        )
        self.assertTrue(event_payloads[1]["cleanup_events"])
        with RunMonitor(self.path) as monitor:
            monitored = monitor.summary("run-e2e")
            self.assertEqual(monitored.status, "complete")
            self.assertEqual(monitored.accepted_conditions, 1)
            self.assertEqual(monitored.rejected_attempts, 1)

    def test_zero_policy_confirms_zero_on_normal_completion(self) -> None:
        stations: list[SimulationStation] = []

        def factory() -> SimulationStation:
            station = SimulationStation.from_config(self.config)
            stations.append(station)
            return station

        engine = SimulationRunEngine(
            store=self.store,
            run_id="run-zero",
            station_factory=factory,
            normal_end_field_policy=FieldEndPolicy.ZERO,
            now=lambda: NOW,
        )
        engine.start_new((condition(),), config_snapshot={"mode": "simulation"})
        self.assertIn("cryostat.request_zero_field", stations[0].event_log)
        self.assertEqual(stations[0].cryostat.read_state().field, VectorField(0.0, 0.0))

    def test_retry_exhaustion_marks_run_failed_without_accepted_data(self) -> None:
        def factory() -> SimulationStation:
            station = SimulationStation.from_config(self.config)
            station.lockin_xx.overload = True
            return station

        engine = SimulationRunEngine(
            store=self.store,
            run_id="run-failed",
            station_factory=factory,
            max_attempts_per_condition=2,
            now=lambda: NOW,
        )
        with self.assertRaises(RetryLimitExceeded):
            engine.start_new(
                (condition(),), config_snapshot={"mode": "simulation"}
            )
        with RunMonitor(self.path) as monitor:
            summary = monitor.summary("run-failed")
        self.assertEqual(summary.status, "failed")
        self.assertEqual(summary.total_attempts, 2)
        self.assertEqual(summary.rejected_attempts, 2)
        self.assertEqual(self.store.load_transport_readings("run-failed"), ())

    def test_resume_rejects_incomplete_attempt_then_completes_pending(self) -> None:
        item = condition()
        self.store.create_run("run-resume", {"mode": "simulation"}, created_at_utc=NOW)
        self.store.register_condition("run-resume", item)
        self.store.start_attempt("run-resume", item.condition_id, started_at_utc=NOW)
        engine = SimulationRunEngine(
            store=self.store,
            run_id="run-resume",
            station_factory=lambda: SimulationStation.from_config(self.config),
            now=lambda: NOW,
        )
        result = engine.resume((item,))
        self.assertEqual(result.accepted_conditions, 1)
        attempts = self.store.connection.execute(
            "SELECT status FROM attempts ORDER BY attempt_index"
        ).fetchall()
        self.assertEqual([row["status"] for row in attempts], ["rejected", "accepted"])

    def test_interrupt_persists_partial_raw_and_last_confirmed_cleanup_state(self) -> None:
        def factory() -> SimulationStation:
            station = SimulationStation.from_config(self.config)

            def interrupt(_: int):
                raise KeyboardInterrupt()

            station.lockin_xy.read_harmonic = interrupt
            return station

        engine = SimulationRunEngine(
            store=self.store,
            run_id="run-interrupt",
            station_factory=factory,
            now=lambda: NOW,
        )
        with self.assertRaises(KeyboardInterrupt):
            engine.start_new(
                (condition(),), config_snapshot={"mode": "simulation"}
            )

        raw = self.store.load_transport_readings(
            "run-interrupt", accepted_only=False
        )
        self.assertEqual(len(raw), 1)
        self.assertFalse(raw[0].accepted)
        station_count = self.store.connection.execute(
            "SELECT COUNT(*) AS count FROM station_samples WHERE run_id = ?",
            ("run-interrupt",),
        ).fetchone()["count"]
        self.assertEqual(station_count, 1)
        payload = json.loads(
            self.store.connection.execute(
                "SELECT payload_json FROM events "
                "WHERE run_id = ? AND event_type = 'run_interrupted'",
                ("run-interrupt",),
            ).fetchone()["payload_json"]
        )
        self.assertTrue(payload["cleanup_succeeded"])
        self.assertTrue(payload["field_zero_confirmed"])
        self.assertEqual(payload["last_confirmed_cryostat_state"]["bx_t"], 0.0)


if __name__ == "__main__":
    unittest.main()
