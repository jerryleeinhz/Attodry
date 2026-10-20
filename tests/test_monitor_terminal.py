"""English operator progress display; no instrument or mutable configuration access."""
import unittest

from attodry_control.combination_terminal import snapshot_text


class MonitorTerminalTests(unittest.TestCase):
    def snapshot(self):
        return {
            "run_id": "archived-plan", "status": "active", "accepted_conditions": 4,
            "total_conditions": 303, "process_liveness": "unknown",
            "progress": {"axes": [
                {"module": "temperature", "current_point": 2, "total_points": 101,
                 "completed_points": 1, "repeat_index": 0, "total_repeats": 1,
                 "scope": {}, "requested": {"temperature_k": 4.2}},
                {"module": "optical", "current_point": 2, "total_points": 3,
                 "completed_points": 1, "repeat_index": 0, "total_repeats": 1,
                 "scope": {"temperature": 1}, "requested": {"optical_target_power_w": 30e-6}}],
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
        self.assertIn("Optical point: 2/3 | Fully completed in scope: 1/3 | power target: 30 uW", text)
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
                          "last_recorded": {"phase": "cleanup", "module": "optical"}}
        text = snapshot_text(value, 160)
        self.assertIn("Current phase: interrupted (terminal)", text)
        self.assertIn("Last recorded phase: optical -> Cleanup", text)
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
        self.assertIn("Optical point: 2/4", text)

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
