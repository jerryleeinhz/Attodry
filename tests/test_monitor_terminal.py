"""English operator progress display; no instrument or mutable configuration access."""
import unittest

from attodry_control.combination_terminal import snapshot_text


class MonitorTerminalTests(unittest.TestCase):
    def test_timing_summary_labels_stored_exclusive_scope(self):
        value = self.snapshot()
        value["measurement_timing_summary"] = {
            "recorded_at_utc": "2030-01-01T00:00:00Z", "elapsed_s": 20,
            "exclusive_stage_duration_s": 15, "audit_duration_s": 3,
            "unattributed_duration_s": 2, "scope": "through the last completed stage",
            "stages": {"axis_apply": {"count": 2, "failed": 0,
                                       "exclusive_duration_s": 15, "inclusive_duration_s": 19}}}
        text = snapshot_text(value, 160)
        self.assertIn("Measurement timing (stored summary): 2030-01-01T00:00:00Z", text)
        self.assertIn("Exclusive stages: 00:00:15 | Audit: 00:00:03", text)
        self.assertIn("Nested inclusive stage times are not additive", text)
        self.assertIn("through the last completed stage", text)

    def snapshot(self):
        return {
            "run_id": "archived-plan", "status": "active", "accepted_conditions": 4,
            "total_conditions": 303, "process_liveness": "unknown",
            "progress": {"axes": [
                {"module": "temperature", "current_point": 2, "total_points": 101,
                 "completed_points": 1, "repeat_index": 0, "total_repeats": 1,
                 "scope": {}, "requested": {"temperature_k": 4.2}},
                {"module": "smu", "current_point": 2, "total_points": 3,
                 "completed_points": 1, "repeat_index": 0, "total_repeats": 1,
                 "scope": {"temperature": 1}, "requested": {"gate_bottom_v": .03}}],
                "current_condition": {"formal_samples": 2, "expected_formal_samples": 3,
                                      "status": "started"}},
            "phase": {"status": "recorded", "phase": "formal_sampling", "module": "lockin",
                      "sample_index": 1, "started_at_utc": "2026-10-20T09:00:00+00:00",
                      "last_update_at_utc": "2026-10-20T09:00:05+00:00"},
        }

    def test_temperature_and_power_progress_do_not_mean_accepted_conditions(self):
        text = snapshot_text(self.snapshot(), 160)
        self.assertIn("Accepted conditions: 4/303", text)
        self.assertIn("Temperature point: 2/101 | Fully completed in scope: 1/101 | target 4.2 K", text)
        self.assertIn("SMU point: 2/3 | Fully completed in scope: 1/3 | gate_bottom: 30 mV", text)
        self.assertIn("Point scope: temperature point 2", text)
        self.assertIn("Formal samples in attempt: 2/3 | started (provisional until accepted)", text)
        self.assertIn("Current phase (last recorded): lockin -> Formal sampling | sample 2", text)
        self.assertIn("Phase started:", text)
        self.assertIn("Last database event:", text)
        self.assertIn("Process liveness: unknown", text)

    def test_terminal_status_cannot_keep_an_old_active_phase(self):
        value = self.snapshot()
        value["status"] = "interrupted"
        value["phase"] = {"status": "terminal", "phase": "interrupted",
                          "last_recorded": {"phase": "cleanup", "module": "smu"}}
        text = snapshot_text(value, 160)
        self.assertIn("Current phase: interrupted (terminal)", text)
        self.assertIn("Last recorded phase: smu -> Cleanup", text)
        self.assertNotIn("Current phase (last recorded):", text)

    def test_legacy_unknown_phase_is_explicit(self):
        value = self.snapshot()
        value["phase"] = {"status": "unavailable", "phase": None}
        text = snapshot_text(value, 160)
        self.assertIn("Current phase: unavailable (no explicit phase record)", text)

    def test_repeat_display_is_once_and_narrow_lines_remain_readable(self):
        value = self.snapshot()
        for axis in value["progress"]["axes"]:
            axis.update(repeat_index=1, total_repeats=2)
        text = snapshot_text(value, 40)
        self.assertEqual(text.count("Repeat: 2/2"), 1)
        self.assertTrue(all(len(line) <= 40 for line in text.splitlines()))

    def test_four_power_points_follow_archived_counts(self):
        value = self.snapshot()
        value["total_conditions"] = 404
        value["progress"]["axes"][1]["total_points"] = 4
        text = snapshot_text(value, 160)
        self.assertIn("Accepted conditions: 4/404", text)
        self.assertIn("SMU point: 2/4", text)

    def test_preflight_has_no_invented_current_repeat(self):
        value = self.snapshot()
        for axis in value["progress"]["axes"]:
            axis.update(repeat_index=None, total_repeats=2, current_point=None, requested={}, scope={})
        value["progress"]["current_condition"] = None
        value["phase"] = {"status": "recorded", "phase": "preflight", "module": None}
        text = snapshot_text(value, 160)
        self.assertIn("Repeat: -/2", text)
        self.assertIn("Temperature point: -/101", text)
        self.assertIn("Startup checks", text)


if __name__ == "__main__":
    unittest.main()
