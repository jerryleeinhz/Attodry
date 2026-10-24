"""Stored evidence only: no instrument, acquisition or live TOML imports."""
from datetime import datetime, timezone
import json
import sqlite3
import unittest

from attodry_control.monitor_environment import read_environment


class MonitorEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        self.connection.executescript("""
            CREATE TABLE combination_runs(run_id,status,created_at_utc);
            CREATE TABLE combination_conditions(run_id,condition_id,context_json);
            CREATE TABLE combination_attempts(run_id,condition_id,attempt_index,started_at_utc);
            CREATE TABLE combination_events(event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id,created_at_utc,event_type,payload_json);
        """)
        self.connection.execute("INSERT INTO combination_runs VALUES (?,?,?)",
            ("run", "active", self.stamp(0)))
        self.plan = {"axes": [
            {"module": "temperature", "points": [{"values": {"temperature_k": 1.7}},
                                                      {"values": {"temperature_k": 4.2}}]},
            {"module": "optical", "points": [{"values": {}}, {"values": {}}]}],
            "hardware": {"effective_cryostat": {"temperature_stability": {
                "acceptance_mode": "target", "criteria": {"tolerance": .1,
                "stable_range": .05, "dwell_s": 120}, "wait_timeout_s": 3600}}}}
        self.progress = {"current_condition": {"condition_id": "c0", "attempt_index": 0}}
        self.phase = {"status": "recorded", "phase": "axis_qualification",
                      "module": "temperature"}
        self.now = datetime(2030, 1, 1, 0, 10, tzinfo=timezone.utc)
        self.condition("c0", (0, 0))
        self.state = {"sample_temperature_k": 1.667, "field": {"bx_t": 0.0, "bz_t": 0.0},
                      "field_setpoint": {"bx_t": 0.0, "bz_t": 0.0}}

    @staticmethod
    def stamp(second):
        return f"2030-01-01T00:{second // 60:02d}:{second % 60:02d}+00:00"

    def condition(self, cid, indices, *, repeat=0, run_id="run", attempt=0):
        context = {"repeat_index": repeat, "axes": {a["module"]: {"index": i}
                   for a, i in zip(self.plan["axes"], indices)}}
        self.connection.execute("INSERT INTO combination_conditions VALUES (?,?,?)",
            (run_id, cid, json.dumps(context)))
        self.connection.execute("INSERT INTO combination_attempts VALUES (?,?,?,?)",
            (run_id, cid, attempt, self.stamp(1)))

    def event(self, kind, payload, second=5, *, cid="c0", run_id="run", attempt=0):
        payload = {"condition_id": cid, "attempt_index": attempt, **payload}
        cursor = self.connection.execute("INSERT INTO combination_events "
            "(run_id,created_at_utc,event_type,payload_json) VALUES (?,?,?,?)",
            (run_id, self.stamp(second), kind, json.dumps(payload)))
        return cursor.lastrowid

    def entry(self, module="temperature", *, cid="c0", second=2, repeat=0, prefix=None,
              target=1.7):
        self.event("scan_phase", {"phase": "axis_apply", "module": module}, second, cid=cid)
        if module == "temperature":
            self.event("temperature_command_confirmed", {"target_k": target}, second + 1, cid=cid)
            self.event("temperature_qualification_started", {"target_k": target,
                "axis_scope": {"repeat_index": repeat, "axis_indices": prefix or [0],
                               "condition_id": cid}}, second + 2, cid=cid)

    def reading(self, second=6, *, cid="c0", captured=None, recovery=None,
                run_id="run", attempt=0, state=None):
        return self.event("cryostat_state", {"captured_at_utc": captured or self.stamp(second),
            "state": self.state if state is None else state, "recovery_action": recovery},
            second, cid=cid, run_id=run_id, attempt=attempt)

    def read(self):
        return read_environment(self.connection, "run", self.plan, self.progress,
                                self.phase, now=self.now)

    def test_current_temperature_and_archive_requirements_without_error(self):
        self.entry()
        self.reading()
        item = self.read()["temperature"]
        self.assertEqual(item["status"], "available")
        self.assertEqual(item["actual_temperature_k"], 1.667)
        self.assertEqual(item["target_temperature_k"], 1.7)
        self.assertEqual(item["qualification_started_at_utc"], self.stamp(4))
        self.assertEqual(item["qualification"]["dwell_s"], 120)
        self.assertIsNone(item["qualification"]["stable_window_elapsed_s"])
        self.assertIsNone(item["qualification"]["timeout_remaining_s"])
        self.assertNotIn("error", json.dumps(item))
        self.assertNotIn("now", item)

    def test_missing_state_is_unavailable(self):
        self.entry()
        self.assertEqual(self.read()["temperature"]["status"], "unavailable")

    def test_corrupt_newest_state_does_not_fallback_to_previous_good_record(self):
        self.entry()
        self.reading()
        self.connection.execute("INSERT INTO combination_events "
            "(run_id,created_at_utc,event_type,payload_json) VALUES (?,?,?,?)",
            ("run", self.stamp(7), "cryostat_state", "not-json"))
        self.assertEqual(self.read()["temperature"]["status"], "unavailable")

    def test_preflight_read_cannot_become_axis_read(self):
        self.reading(second=1, cid=None)
        self.entry()
        self.assertEqual(self.read()["temperature"]["status"], "unavailable")

    def test_active_cleanup_hides_current_readback(self):
        self.entry()
        self.reading()
        self.phase["phase"] = "cleanup"
        self.assertEqual(self.read()["temperature"]["status"], "unavailable")

    def test_latest_cross_point_read_does_not_fallback_to_old_good_read(self):
        self.entry()
        self.reading()
        self.condition("old", (1, 0))
        self.reading(second=8, cid="old")
        self.assertEqual(self.read()["temperature"]["status"], "unavailable")

    def test_other_run_state_is_excluded(self):
        self.entry()
        self.reading()
        self.condition("c0", (1, 0), run_id="other")
        self.reading(second=8, run_id="other", state={"sample_temperature_k": 99})
        self.assertEqual(self.read()["temperature"]["actual_temperature_k"], 1.667)

    def test_duplicate_numeric_target_has_distinct_visit(self):
        self.plan["axes"][0]["points"][1]["values"]["temperature_k"] = 1.7
        self.entry()
        self.reading()
        self.condition("c1", (1, 0))
        self.progress["current_condition"]["condition_id"] = "c1"
        self.assertEqual(self.read()["temperature"]["status"], "unavailable")

    def test_repeat_cannot_reuse_prior_readback(self):
        self.entry()
        self.reading()
        self.condition("r1", (0, 0), repeat=1)
        self.progress["current_condition"]["condition_id"] = "r1"
        self.assertEqual(self.read()["temperature"]["status"], "unavailable")

    def test_same_visit_inner_condition_can_supply_latest_state(self):
        self.entry()
        self.condition("inner", (0, 1))
        self.progress["current_condition"]["condition_id"] = "inner"
        self.reading(cid="inner")
        item = self.read()["temperature"]
        self.assertEqual(item["status"], "available")
        self.assertEqual(item["source_condition_id"], "inner")

    def test_outer_axis_scope_prevents_reusing_inner_temperature(self):
        self.plan["axes"].reverse()
        self.connection.execute("DELETE FROM combination_conditions")
        self.condition("c0", (0, 0))
        self.entry(prefix=[0, 0])
        self.reading()
        self.condition("outer", (1, 0))
        self.progress["current_condition"]["condition_id"] = "outer"
        self.assertEqual(self.read()["temperature"]["status"], "unavailable")

    def test_timestamp_context_missing_or_future_never_current(self):
        self.entry()
        for captured in ("bad", "2030-01-01T00:00:06", self.stamp(0), self.stamp(7),
                         "2031-01-01T00:00:06Z"):
            with self.subTest(captured=captured):
                self.reading(captured=captured)
                self.assertEqual(self.read()["temperature"]["status"], "unavailable")
        self.reading(attempt=9)
        self.assertEqual(self.read()["temperature"]["status"], "unavailable")

    def test_future_event_timestamp_is_rejected(self):
        self.entry()
        self.reading(second=601)
        self.assertEqual(self.read()["temperature"]["status"], "unavailable")

    def test_finite_positive_required(self):
        self.entry()
        for value in (None, float("nan"), float("inf"), -1, True):
            with self.subTest(value=value):
                self.reading(state={"sample_temperature_k": value})
                self.assertEqual(self.read()["temperature"]["status"], "unavailable")

    def test_recovery_and_terminal_remain_unavailable(self):
        self.entry()
        self.reading(recovery="disable")
        self.assertEqual(self.read()["temperature"]["reason"], "recovery readback")
        self.reading(second=7)
        self.connection.execute("UPDATE combination_runs SET status='failed'")
        self.assertEqual(self.read()["temperature"]["reason"], "terminal run")

    def test_legacy_without_axis_entry_is_unavailable(self):
        self.reading()
        self.assertEqual(self.read()["temperature"]["reason"], "matching axis entry unavailable")

    def test_current_entry_precommand_read_is_unavailable(self):
        self.event("scan_phase", {"phase": "axis_apply", "module": "temperature"}, 2)
        self.reading()
        self.assertEqual(self.read()["temperature"]["status"], "unavailable")

    def test_qualification_scope_and_target_must_match(self):
        self.entry(prefix=[1])
        self.reading()
        self.assertNotIn("qualification_started_at_utc", self.read()["temperature"])

    def magnetic(self):
        self.plan["axes"][0] = {"module": "magnetic", "points": [
            {"values": {"field_x_t": .1, "field_z_t": .2}},
            {"values": {"field_x_t": .2, "field_z_t": .3}}]}
        self.plan["hardware"]["effective_cryostat"]["magnet"] = {"stability": {
            "criteria": {"tolerance": .001, "stable_range": .0001, "dwell_s": 30},
            "wait_timeout_s": 1800}}
        self.connection.execute("DELETE FROM combination_conditions")
        self.condition("c0", (0, 0))
        self.phase.update(phase="axis_apply", module="magnetic")
        self.entry(module="magnetic")

    def field_sample(self, second=6, *, phase="field_stability", elapsed=18, waypoint=None):
        self.event("magnetic_point_event", {"event": "field_sample", "state": self.state,
            "phase": phase, "waypoint_index": waypoint, "elapsed_s": elapsed}, second)

    def test_magnetic_actual_final_target_and_waypoint_are_distinct(self):
        self.magnetic()
        self.reading()
        self.field_sample(phase="waypoint_stability", waypoint=2)
        item = self.read()["magnetic"]
        self.assertEqual(item["actual_field_t"], {"bx_t": 0, "bz_t": 0})
        self.assertEqual(item["target_field_t"], {"bx_t": .1, "bz_t": .2})
        self.assertEqual(item["commanded_waypoint_t"], {"bx_t": 0, "bz_t": 0})
        self.assertEqual(item["subwait"]["timeout_remaining_s"], 1782)
        self.assertIsNone(item["qualification"]["stable_window_elapsed_s"])
        self.assertNotIn("error", json.dumps(item))

    def test_subwait_reset_uses_latest_phase_elapsed_not_whole_entry(self):
        self.magnetic()
        self.reading()
        self.field_sample(phase="planning_start_stability", elapsed=100)
        self.reading(second=8)
        self.field_sample(second=8, phase="waypoint_stability", waypoint=1, elapsed=2)
        self.assertEqual(self.read()["magnetic"]["subwait"]["timeout_remaining_s"], 1798)

    def test_ack_has_no_guessed_stability_timeout(self):
        self.magnetic()
        self.reading()
        self.field_sample(phase="setpoint_x_ack", elapsed=2, waypoint=0)
        self.assertIsNone(self.read()["magnetic"]["subwait"]["timeout_remaining_s"])

    def test_stale_field_sample_does_not_annotate_new_readback(self):
        self.magnetic()
        self.reading()
        self.field_sample()
        self.reading(second=9)
        self.assertNotIn("subwait", self.read()["magnetic"])

    def test_field_sample_must_match_exact_readback_condition_and_attempt(self):
        self.magnetic()
        self.reading()
        self.condition("inner", (0, 1))
        self.event("magnetic_point_event", {"event": "field_sample", "state": self.state,
            "phase": "field_stability", "waypoint_index": None, "elapsed_s": 18},
            cid="inner")
        self.assertNotIn("subwait", self.read()["magnetic"])

    def test_missing_field_component_is_unavailable_not_zero(self):
        self.magnetic()
        self.reading(state={"field": {"bx_t": 0}})
        self.assertEqual(self.read()["magnetic"]["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
