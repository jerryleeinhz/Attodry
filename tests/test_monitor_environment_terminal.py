"""Environment evidence presentation and text-only clocks, with no hardware."""
from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from attodry_control.combination_cli import monitor_output_due, run
from attodry_control.combination_terminal import snapshot_text


NOW = datetime(2026, 10, 20, 12, 6, 21, tzinfo=timezone.utc)


def snapshot():
    return {
        "run_id": "fixed", "status": "active", "accepted_conditions": 4,
        "total_conditions": 100, "process_liveness": "unknown",
        "progress": {"axes": [
            {"module": "temperature", "current_point": 2, "total_points": 25,
             "completed_points": 1, "requested": {"temperature_k": 4.2}},
            {"module": "smu", "current_point": 1, "total_points": 4,
             "completed_points": 0, "requested": {"gate_bottom_v": .02}}]},
        "phase": {"status": "recorded", "phase": "axis_qualification", "module": "temperature",
                  "started_at_utc": "2026-10-20T12:00:00Z",
                  "last_update_at_utc": "2026-10-20T12:06:19Z"},
        "environment": {"temperature": {
            "status": "available", "actual_temperature_k": 4.16732, "target_temperature_k": 4.2,
            "captured_at_utc": "2026-10-20T12:06:19Z", "source_condition_id": "condition-000004",
            "source_attempt_index": 0, "visit_started_at_utc": "2026-10-20T11:59:59Z",
            "qualification_started_at_utc": "2026-10-20T12:00:00Z",
            "qualification": {"acceptance_mode": "target", "tolerance": .1,
                              "stable_range": .05, "dwell_s": 120, "wait_timeout_s": 3600,
                              "stable_window_elapsed_s": None, "timeout_remaining_s": None}}},
        "last_formal_measurement": None,
    }


class EnvironmentTerminalTests(unittest.TestCase):
    def render(self, value):
        return snapshot_text(value, 220, now=NOW)

    def test_temperature_evidence_clock_and_unknown_qualification_limits(self):
        text = self.render(snapshot())
        for line in (
            "Temperature point: 2/25", "temperature -> Axis qualification",
            "Latest temperature: 4.167320 K", "Target temperature: 4.200000 K (archived plan)",
            "Captured UTC: 2026-10-20T12:06:19Z | Age: 00:00:02",
            "Source condition: condition-000004 | Attempt: 0",
            "Qualification elapsed: 00:06:21 (wall-clock estimate)",
            "Stable window accumulated: unknown / required 00:02:00",
            "Qualification timeout remaining: unknown", "Qualification timeout limit: 01:00:00",
            "Automatic continuation: enabled",
            "process liveness is not established",
        ):
            self.assertIn(line, text)
        self.assertNotIn("Temperature error", text)
        self.assertNotIn("Field error", text)

    def test_missing_or_cross_visit_evidence_does_not_promote_old_formal_temperature(self):
        for reason in ("no cryostat_state in current visit", "record predates current temperature visit",
                       "condition context does not match current visit"):
            with self.subTest(reason=reason):
                value = snapshot()
                value["environment"]["temperature"].update(status="unavailable", reason=reason)
                value["last_recorded_readings"] = {"temperature": {
                    "captured_at_utc": "old", "actual": {"temperature_k": 1.7}}}
                text = self.render(value)
                self.assertIn("Latest temperature: unavailable", text)
                self.assertIn("Target temperature: 4.200000 K", text)
                self.assertIn(reason, text)
                self.assertNotIn('"temperature_k": 1.7', text)

    def test_future_or_invalid_capture_is_unavailable_without_clamping_age_to_zero(self):
        for captured in ("2026-10-20T12:06:22Z", "2026-10-20T12:00:00", "invalid", None):
            with self.subTest(captured=captured):
                value = snapshot()
                value["environment"]["temperature"]["captured_at_utc"] = captured
                text = self.render(value)
                self.assertIn("Latest temperature: unavailable", text)
                self.assertIn("Captured UTC: unavailable | Age: unknown", text)

    def test_valid_old_capture_retains_age_as_stored_evidence(self):
        value = snapshot()
        value["environment"]["temperature"]["captured_at_utc"] = "2026-10-20T12:00:01Z"
        text = self.render(value)
        self.assertIn("Age: 00:06:20", text)
        self.assertIn("stored; not a live query", text)

    def test_terminal_cannot_show_current_qualification_or_automatic_continuation(self):
        value = snapshot()
        value["status"] = "failed"
        value["phase"] = {"status": "terminal", "phase": "failed",
                          "last_recorded": {"phase": "cleanup", "module": "temperature"}}
        text = self.render(value)
        self.assertIn("Current phase: failed (terminal)", text)
        self.assertIn("Automatic continuation: disabled (terminal)", text)
        self.assertIn("Latest temperature: unavailable", text)
        self.assertNotIn("Qualification elapsed:", text)

    def test_unknown_run_status_and_no_smu_axis_do_not_claim_smu_activation(self):
        value = snapshot()
        value["progress"]["axes"] = value["progress"]["axes"][:1]
        text = self.render(value)
        value["status"] = "unknown"
        text = self.render(value)
        self.assertIn("Automatic continuation: unknown", text)
        self.assertNotIn("Latest temperature: 4.167320", text)
        self.assertNotIn("Qualification elapsed:", text)

    def test_magnetic_transition_stays_axis_apply_with_recorded_subwait_clock(self):
        value = snapshot()
        value["progress"]["axes"][0]["module"] = "magnetic"
        value["progress"]["axes"][0]["requested"] = {"field_x_t": .1, "field_z_t": 0}
        value["phase"].update(module="magnetic", phase="axis_apply")
        value["environment"] = {"magnetic": {
            "status": "available", "actual_field_t": {"bx_t": .0996, "bz_t": .0001},
            "target_field_t": {"bx_t": .1, "bz_t": 0},
            "commanded_waypoint_t": {"bx_t": .05, "bz_t": 0},
            "captured_at_utc": "2026-10-20T12:06:19Z", "source_condition_id": "condition-000004",
            "source_attempt_index": 0, "visit_started_at_utc": "2026-10-20T12:04:11Z",
            "qualification": {"dwell_s": 30},
            "subwait": {"phase": "final-target stability", "elapsed_s": 18,
                        "timeout_remaining_s": 1782, "captured_at_utc": "2026-10-20T12:06:19Z"}}}
        text = self.render(value)
        self.assertIn("magnetic -> Applying axis target", text)
        self.assertIn("Latest field: Bx=+0.099600 T | Bz=+0.000100 T", text)
        self.assertIn("Target field: Bx=+0.100000 T | Bz=+0.000000 T", text)
        self.assertIn("Commanded waypoint: Bx=+0.050000 T", text)
        self.assertIn("Axis entry elapsed: 00:02:10 (wall-clock estimate)", text)
        self.assertIn("Field subphase: final-target stability (recorded)", text)
        self.assertIn("Sub-wait elapsed at capture: 00:00:18", text)
        self.assertIn("Sub-wait timeout remaining at capture: 00:29:42", text)
        self.assertNotIn("Qualification elapsed:", text)
        self.assertNotIn("Field error", text)

    def test_formal_modules_keep_their_capture_and_condition_separate_from_environment(self):
        value = snapshot()
        value["last_formal_measurement"] = {
            "condition_id": "condition-000003", "attempt_index": 1, "attempt_status": "accepted",
            "sample_index": 2, "expected_samples": 3, "captured_at_utc": "2026-10-20T11:59:00Z",
            "modules": {"smu": {"captured_at_utc": "2026-10-20T11:58:58Z",
                "actual": {"smu_power_w": 35e-6}, "status": {}, "clean": True},
                "lockin": {"captured_at_utc": "2026-10-20T11:58:59Z",
                "measurements": {"lockin_xx_h1_x_v": 1e-6}, "status": {}, "clean": True}}}
        value["last_recorded_readings"] = {"smu": {"captured_at_utc": "raw-candidate",
                                                      "measurements": {"smu_power_w": .2}}}
        text = self.render(value)
        self.assertIn("Condition: condition-000003 | Attempt: 1 | Sample: 3/3 | Status: accepted", text)
        self.assertIn("2026-10-20T11:58:58Z", text)
        self.assertIn("2026-10-20T11:58:59Z", text)
        self.assertNotIn("raw-candidate", text)
        self.assertIn("Source condition: condition-000004 | Attempt: 0", text)
        self.assertLess(text.index("Latest environment"), text.index("Last formal measurement"))

    def test_rejected_formal_shape_and_legacy_raw_do_not_claim_accepted_measurement(self):
        value = snapshot()
        value["last_formal_measurement"] = {"attempt_status": "rejected", "modules": {
            "smu": {"measurements": {"raw_wrong": 1}}}}
        self.assertIn("Unavailable (no accepted formal measurement)", self.render(value))
        self.assertNotIn("raw_wrong", self.render(value))
        del value["last_formal_measurement"]
        value["last_recorded_readings"] = {"smu": {"measurements": {"legacy_candidate": 1}}}
        self.assertIn("Last recorded raw candidates (not formal or live)", self.render(value))
        self.assertIn("legacy_candidate", self.render(value))


class MonitorRefreshTests(unittest.TestCase):
    def test_text_age_refresh_every_five_seconds_without_mutating_snapshot_or_json(self):
        value = snapshot()
        before = json.dumps(value, sort_keys=True)
        for elapsed, expected in ((0, False), (4.99, False), (5, True), (8, True)):
            self.assertEqual(monitor_output_due(value, changed=False, json_output=False,
                             last_rendered=10, now=10 + elapsed), expected)
            self.assertFalse(monitor_output_due(value, changed=False, json_output=True,
                            last_rendered=10, now=10 + elapsed))
        self.assertEqual(json.dumps(value, sort_keys=True), before)
        for module, phase in (("smu", "smu_tuning"), ("temperature", "cleanup")):
            value["phase"].update(module=module, phase=phase)
            self.assertFalse(monitor_output_due(value, changed=False, json_output=False,
                            last_rendered=10, now=100))

    def test_new_evidence_and_terminal_print_immediately_for_text_and_json(self):
        for json_output in (False, True):
            value = snapshot()
            self.assertTrue(monitor_output_due(value, changed=True, json_output=json_output,
                            last_rendered=10, now=10.1))
            value.update(status="completed", phase={"status": "terminal", "phase": "completed"})
            self.assertTrue(monitor_output_due(value, changed=True, json_output=json_output,
                            last_rendered=10, now=10.1))
            self.assertFalse(monitor_output_due(value, changed=False, json_output=json_output,
                             last_rendered=10, now=100))

    def test_cli_refreshes_same_text_but_json_only_when_evidence_changes(self):
        for json_output, expected in ((False, 3), (True, 2)):
            value = snapshot()
            terminal = {**value, "status": "completed", "phase": {"status": "terminal", "phase": "completed"}}
            output = io.StringIO()
            with patch("attodry_control.combination_registry.resolve_monitor", return_value=(Path("unused"), "fixed")), \
                 patch("attodry_control.combination_cli.run_snapshot", side_effect=[value, value, value, terminal]), \
                 patch("attodry_control.combination_cli.time.monotonic", side_effect=[0, 1, 5, 6]), \
                 patch("attodry_control.combination_cli.time.sleep"), redirect_stdout(output):
                run(["monitor", "--database", "unused", "--run-id", "fixed", *(["--json"] if json_output else [])])
            if json_output:
                self.assertEqual(len(output.getvalue().splitlines()), expected)
                self.assertEqual(json.loads(output.getvalue().splitlines()[0])["run_id"], "fixed")
            else:
                self.assertEqual(output.getvalue().count("Status:"), expected)


if __name__ == "__main__":
    unittest.main()
