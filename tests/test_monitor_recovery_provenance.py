"""Recovery monitor uses the active segment while retaining accepted history."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from attodry_control.combination_scan import AxisPoint, CombinationPlan, ScanAxis
from attodry_control.combination_store import CombinationStore, run_snapshot, utc_now


class MonitorRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "recovery.sqlite"
        self.store = CombinationStore(self.path)
        self.addCleanup(self.store.connection.close)
        self.plan = CombinationPlan((ScanAxis("temperature", (
            AxisPoint({"temperature_k": 1.7}), AxisPoint({"temperature_k": 4.2}))),
            ScanAxis("lockin", (AxisPoint({"lockin_excitation_v_rms": .004,
                                          "lockin_frequency_hz": 200.}),))), samples_per_condition=1)
        self.original = self.plan.snapshot()
        self.original["hardware"] = {"effective_cryostat": {"temperature_stability": {
            "criteria": {"tolerance": .1, "stable_range": .05, "dwell_s": 30},
            "acceptance_mode": "target", "wait_timeout_s": 3600}}}
        self.store.begin_run("run", self.original, self.plan.conditions())
        self.execution("first", 0, self.original)
        self.cid, self.attempt = "condition-000000", self.store.begin_attempt("run", "condition-000000")
        self.emit("scan_phase", {"phase": "axis_apply", "module": "temperature"})
        self.store.sample("run", self.cid, self.attempt, 0, {
            "clean": True, "started_at_utc": utc_now(), "finished_at_utc": utc_now(),
            "reads": [{"module": "temperature", "captured_at_utc": utc_now(),
                       "actual": {"temperature_k": 1.7}, "measurements": {}, "clean": True}]})
        self.store.finish_attempt("run", self.cid, self.attempt, expected_samples=1)
        with self.store.connection:
            self.store.connection.execute("UPDATE combination_executions SET status='failed' WHERE execution_id='first'")
        self.current = deepcopy(self.original)
        self.current["hardware"]["effective_cryostat"]["temperature_stability"]["criteria"].update(
            stable_range=.2, dwell_s=60)
        self.execution("resumed", 1, self.current)

    def execution(self, identity, sequence, plan):
        with self.store.connection:
            self.store.connection.execute(
                "INSERT INTO combination_executions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("run", identity, sequence, "run" if sequence == 0 else "resume", "active",
                 json.dumps(plan), "{}", None, utc_now(), None, None, None))
        self.store.current_execution_id = identity

    def emit(self, kind, payload):
        self.store.event("run", kind, {"condition_id": self.cid, "attempt_index": self.attempt, **payload})

    def begin_current(self):
        self.cid = "condition-000001"
        self.attempt = self.store.begin_attempt("run", self.cid)
        self.emit("scan_phase", {"phase": "axis_apply", "module": "temperature"})
        self.emit("temperature_command_confirmed", {"target_k": 4.2})
        self.emit("temperature_qualification_started", {"target_k": 4.2,
            "axis_scope": {"repeat_index": 0, "axis_indices": [1], "condition_id": self.cid}})
        self.emit("scan_phase", {"phase": "axis_qualification", "module": "temperature"})

    def reading(self, **extra):
        self.emit("cryostat_state", {"captured_at_utc": utc_now(), "state": {
            "sample_temperature_k": 4.18, "field": {"bx_t": 0., "bz_t": 0.}}, **extra})

    def snapshot(self):
        return run_snapshot(self.path, "run")

    def test_new_execution_does_not_reuse_old_attempt_or_phase(self):
        snapshot = self.snapshot()
        self.assertIsNone(snapshot["current_attempt"])
        self.assertEqual(snapshot["phase"]["status"], "unavailable")
        self.assertEqual(snapshot["environment"]["temperature"]["status"], "unavailable")
        self.assertEqual(snapshot["accepted_conditions"], 1)

    def test_current_effective_criteria_not_original_run_toml(self):
        self.begin_current()
        self.reading()
        item = self.snapshot()["environment"]["temperature"]
        self.assertEqual(item["status"], "available")
        self.assertEqual(item["qualification"]["stable_range"], .2)
        self.assertEqual(item["qualification"]["dwell_s"], 60)
        self.assertEqual(item["source_condition_id"], "condition-000001")

    def test_accepted_history_keeps_its_own_execution(self):
        self.begin_current()
        self.reading()
        snapshot = self.snapshot()
        self.assertEqual(snapshot["current_execution_id"], "resumed")
        formal = snapshot["last_formal_measurement"]
        self.assertEqual(formal["execution_id"], "first")
        self.assertEqual(formal["condition_id"], "condition-000000")
        self.assertEqual(formal["modules"]["temperature"]["actual"]["temperature_k"], 1.7)

    def test_forged_current_event_cannot_borrow_previous_execution_attempt(self):
        self.begin_current()
        self.reading(condition_id="condition-000000", attempt_index=0)
        self.assertEqual(self.snapshot()["environment"]["temperature"]["status"], "unavailable")

    def test_unknown_newest_execution_read_does_not_fallback(self):
        self.begin_current()
        self.reading()
        self.store.current_execution_id = None
        self.reading()
        self.assertEqual(self.snapshot()["environment"]["temperature"]["status"], "unavailable")

    def test_cleanup_and_terminal_never_relabel_last_sample_as_current(self):
        self.begin_current()
        self.reading()
        self.emit("scan_phase", {"phase": "cleanup", "module": "temperature"})
        self.assertEqual(self.snapshot()["environment"]["temperature"]["status"], "unavailable")
        with self.store.connection:
            self.store.connection.execute("UPDATE combination_runs SET status='failed' WHERE run_id='run'")
        snapshot = self.snapshot()
        self.assertEqual(snapshot["phase"]["status"], "terminal")
        self.assertEqual(snapshot["last_formal_measurement"]["execution_id"], "first")

    def test_timing_summary_is_bound_to_its_execution(self):
        self.store.current_execution_id = "first"
        self.emit("measurement_timing_summary", {"elapsed_s": 120})
        self.assertIsNone(self.snapshot()["measurement_timing_summary"])
        self.store.current_execution_id = "resumed"
        self.emit("measurement_timing_summary", {"elapsed_s": 20, "scope": "partial final stages"})
        summary = self.snapshot()["measurement_timing_summary"]
        self.assertEqual(summary["execution_id"], "resumed")
        self.assertEqual(summary["elapsed_s"], 20)
        self.assertIn("recorded_at_utc", summary)


if __name__ == "__main__":
    unittest.main()
