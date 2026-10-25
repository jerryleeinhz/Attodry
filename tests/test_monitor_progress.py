"""Archived progress and phase evidence, with no instrument or TOML access."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from attodry_control.combination_scan import AxisPoint, CombinationPlan, ScanAxis
from attodry_control.combination_store import CombinationStore, open_readonly, run_snapshot


def plan(*, reversed_axes=False, repeats=1, duplicate_temperature=False, voltages=3):
    temperature = ScanAxis("temperature", tuple(AxisPoint({"temperature_k": t})
        for t in ((1.7, 1.7) if duplicate_temperature else (1.7, 4.2))))
    smu = ScanAxis("smu", tuple(AxisPoint({"gate_bottom_v": p * 1e-3})
        for p in (20, 25, 30, 35)[:voltages]))
    lockin = ScanAxis("lockin", (AxisPoint({"lockin_excitation_v_rms": 0.004,
                                           "lockin_frequency_hz": 8.0}),))
    axes = (smu, temperature, lockin) if reversed_axes else (temperature, smu, lockin)
    return CombinationPlan(axes, samples_per_condition=3, repeats=repeats)


class MonitorProgressTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "monitor.sqlite"
        self.store = CombinationStore(self.path)
        self.addCleanup(self.store.connection.close)

    def begin(self, scan_plan=None, run_id="run"):
        self.plan = scan_plan or plan()
        self.conditions = self.plan.conditions()
        self.store.begin_run(run_id, self.plan.snapshot(), self.conditions)

    def capture(self, index, *, samples=3, status="accepted", run_id="run"):
        condition_id = self.conditions[index]["condition_id"]
        attempt = self.store.begin_attempt(run_id, condition_id)
        for sample in range(samples):
            self.store.sample(run_id, condition_id, attempt, sample, {"clean": True})
        if status == "accepted":
            self.store.finish_attempt(run_id, condition_id, attempt, expected_samples=3)
        elif status == "rejected":
            self.store.finish_attempt(run_id, condition_id, attempt,
                                      expected_samples=3, error="retained rejected window")
        return condition_id, attempt

    def snapshot(self, run_id="run"):
        return run_snapshot(self.path, run_id)

    def test_temperature_and_voltage_progress_are_separate_from_conditions(self):
        self.begin()
        for i in range(4):
            self.capture(i)
        self.capture(4, samples=2, status="started")
        snapshot = self.snapshot()
        temperature, smu, lockin = snapshot["progress"]["axes"]
        self.assertEqual((temperature["current_point"], temperature["total_points"],
                          temperature["completed_points"]), (2, 2, 1))
        self.assertEqual((smu["current_point"], smu["total_points"],
                          smu["completed_points"]), (2, 3, 1))
        self.assertEqual(smu["scope"], {"temperature": 1})
        self.assertAlmostEqual(smu["requested"]["gate_bottom_v"], 25e-3)
        self.assertEqual(lockin["completed_points"], 0)
        self.assertEqual(snapshot["accepted_conditions"], 4)
        self.assertEqual(snapshot["progress"]["accepted_formal_samples"], 12)
        self.assertEqual(snapshot["progress"]["current_condition"]["formal_samples"], 2)
        self.assertEqual(snapshot["progress"]["current_condition"]["expected_formal_samples"], 3)

    def test_four_voltage_grid_automatically_has_404_conditions(self):
        old_plan = plan(voltages=4)
        scan_plan = CombinationPlan((ScanAxis("temperature", tuple(
            AxisPoint({"temperature_k": 1.7 + 2.5 * i}) for i in range(101))),
            *old_plan.axes[1:]), samples_per_condition=3)
        self.begin(scan_plan)
        self.capture(4, samples=0, status="started")
        snapshot = self.snapshot()
        self.assertEqual(snapshot["total_conditions"], 404)
        self.assertEqual([(a["current_point"], a["total_points"])
                          for a in snapshot["progress"]["axes"]], [(2, 101), (1, 4), (1, 1)])

    def test_duplicate_values_and_repeats_are_distinct_visits(self):
        self.begin(plan(duplicate_temperature=True, repeats=2))
        for i in range(6):
            self.capture(i)
        self.capture(6, samples=0, status="started")
        temperature, smu, _ = self.snapshot()["progress"]["axes"]
        self.assertEqual((temperature["repeat_index"], temperature["total_repeats"]), (1, 2))
        self.assertEqual((temperature["current_point"], temperature["completed_points"]), (1, 0))
        self.assertEqual(smu["completed_points"], 0)
        self.capture(7)
        self.capture(8)
        # First repeat's same numeric temperatures cannot complete repeat two.
        temperature = self.snapshot()["progress"]["axes"][0]
        self.assertEqual(temperature["completed_points"], 0)

    def test_outer_axis_scope_is_preserved_with_temperature_inside_smu(self):
        self.begin(plan(reversed_axes=True))
        self.capture(0)
        self.capture(1)
        self.capture(2, samples=0, status="started")
        smu, temperature, _ = self.snapshot()["progress"]["axes"]
        self.assertEqual((smu["current_point"], smu["completed_points"]), (2, 1))
        self.assertEqual((temperature["current_point"], temperature["completed_points"]), (1, 0))
        self.assertEqual(temperature["scope"], {"smu": 1})

    def test_rejected_partial_and_skipped_conditions_do_not_complete_axis(self):
        self.begin()
        self.capture(0)
        skipped_id, attempt = self.capture(1, samples=0, status="rejected")
        with self.store.connection:
            # Optional extension may exist in archived production DBs.
            self.store.connection.execute(
                "CREATE TABLE IF NOT EXISTS combination_skipped_conditions ("
                "run_id TEXT, condition_id TEXT, attempt_index INTEGER, reason TEXT, "
                "evidence_json TEXT, created_at_utc TEXT)")
            self.store.connection.execute(
                "INSERT INTO combination_skipped_conditions VALUES (?,?,?,?,?,?)",
                ("run", skipped_id, attempt, "target_unreachable", "{}", "2026-10-20T00:00:00+00:00"))
        self.capture(2, samples=2, status="rejected")
        self.capture(3, samples=0, status="started")
        snapshot = self.snapshot()
        self.assertEqual(snapshot["accepted_conditions"], 1)
        self.assertEqual(snapshot["progress"]["accepted_formal_samples"], 3)
        self.assertEqual(snapshot["progress"]["axes"][0]["completed_points"], 0)

    def test_corrupt_accepted_sample_count_cannot_complete_a_point(self):
        self.begin()
        for i in range(3):
            self.capture(i)
        with self.store.connection:
            self.store.connection.execute(
                "DELETE FROM combination_samples WHERE run_id='run' AND condition_id='condition-000000' "
                "AND sample_index=2")
        self.assertEqual(self.snapshot()["progress"]["axes"][0]["completed_points"], 0)

    def test_explicit_phase_survives_raw_events_and_retains_old_evidence_time(self):
        self.begin()
        condition_id, attempt = self.capture(0, samples=0, status="started")
        self.store.event("run", "scan_phase", {"phase": "axis_qualification", "module": "temperature",
            "condition_id": condition_id, "attempt_index": attempt})
        with self.store.connection:
            self.store.connection.execute("UPDATE combination_events SET created_at_utc=?",
                                          ("2026-10-01T00:00:00+00:00",))
        self.store.event("run", "raw_reading", {"reading": {"module": "temperature", "actual": {}}})
        snapshot = self.snapshot()
        self.assertEqual(snapshot["last_event"]["event_type"], "raw_reading")
        self.assertEqual(snapshot["phase"]["status"], "recorded")
        self.assertEqual(snapshot["phase"]["phase"], "axis_qualification")
        self.assertEqual(snapshot["phase"]["started_at_utc"], "2026-10-01T00:00:00+00:00")
        self.assertEqual(snapshot["phase"]["last_update_at_utc"], snapshot["last_event"]["created_at_utc"])
        # No changing clock-derived keys may force a new terminal print on each poll.
        self.assertEqual(self.snapshot(), snapshot)
        self.assertEqual(snapshot["process_liveness"], "unknown")

    def test_terminal_state_overrides_last_stored_active_phase(self):
        self.begin()
        self.store.event("run", "scan_phase", {"phase": "smu_tuning", "module": "smu"})
        self.store.finish_run("run", "interrupted", {"clean": False}, "KeyboardInterrupt")
        phase = self.snapshot()["phase"]
        self.assertEqual((phase["status"], phase["phase"]), ("terminal", "interrupted"))
        self.assertEqual(phase["last_recorded"]["phase"], "smu_tuning")

    def test_legacy_database_has_no_invented_current_phase_or_skip_schema_change(self):
        self.begin()
        self.store.event("run", "temperature_qualification_started", {})
        with self.store.connection:
            self.store.connection.execute("DROP TABLE IF EXISTS combination_skipped_conditions")
        snapshot = self.snapshot()
        self.assertEqual(snapshot["phase"]["status"], "unavailable")
        self.assertIsNone(snapshot["phase"]["phase"])
        self.assertEqual(snapshot["phase"]["last_known_event"]["event_type"],
                         "temperature_qualification_started")
        self.assertIsNone(snapshot["progress"]["current_condition"])
        self.assertIsNone(snapshot["progress"]["axes"][0]["current_point"])
        self.assertIsNone(self.store.connection.execute(
            "SELECT name FROM sqlite_master WHERE name='combination_skipped_conditions'").fetchone())

    def test_snapshot_is_run_specific_readonly_and_internally_consistent(self):
        self.begin()
        self.capture(0)
        self.store.begin_run("another", self.plan.snapshot(), self.conditions)
        self.store.event("another", "scan_phase", {"phase": "cleanup"})
        original_open = open_readonly
        observed = []
        changed = False

        def open_and_trace(path):
            connection = original_open(path)
            self.assertEqual(connection.execute("PRAGMA query_only").fetchone()[0], 1)

            def trace(sql):
                nonlocal changed
                observed.append(sql)
                if "COUNT(*) FROM combination_conditions" in sql and not changed:
                    changed = True
                    # Commit through the independent WAL writer after snapshot's first read.
                    self.capture(1)
            connection.set_trace_callback(trace)
            return connection

        with patch("attodry_control.combination_store.open_readonly", side_effect=open_and_trace):
            snapshot = self.snapshot()
        self.assertTrue(changed)
        self.assertEqual(snapshot["accepted_conditions"], 1)
        self.assertEqual(snapshot["progress"]["accepted_formal_samples"], 3)
        self.assertEqual(snapshot["phase"]["status"], "unavailable")
        self.assertEqual(self.snapshot()["accepted_conditions"], 2)
        self.assertFalse(any(sql.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER"))
                             for sql in observed))
        with closing(open_readonly(self.path)) as connection:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM combination_events")


if __name__ == "__main__":
    unittest.main()
