"""Electrical coordinator and raw audit with fake instruments only."""
from contextlib import closing, redirect_stdout
from copy import deepcopy
from datetime import datetime, timezone
import io
import json
import re
import unittest
from unittest.mock import patch

from attodry_control.combination_cli import run as cli
from attodry_control.combination_hardware import load_hardware_combination
from attodry_control.combination_launch import launch_summary
from attodry_control.combination_store import (open_readonly, run_snapshot,
    validate_accepted_records, _validate_lockin_record)
from attodry_control.combination_terminal import launch_text, snapshot_text
from tests import test_combination_hardware as fixture


RULES = '''
[[combination_scan.lockin_range_rules]]
name = "first_visit"
point_indices = { smu = [0] }
lockin_xx_full_scale_v = 0.020
lockin_xy_full_scale_v = 0.050

[[combination_scan.lockin_range_rules]]
name = "other_visits"
point_indices = { smu = [1, 2] }
lockin_xx_full_scale_v = 0.010
lockin_xy_full_scale_v = 0.020
'''


class ElectricalRangeRulesTests(unittest.TestCase):
    setUp = fixture.ElectricalCombinationTests.setUp
    write_config = fixture.ElectricalCombinationTests.write_config
    factory = fixture.ElectricalCombinationTests.factory
    execute = fixture.ElectricalCombinationTests.execute
    events = fixture.ElectricalCombinationTests.events

    def configure(self, *, extra=RULES, order=("smu", "lockin"), source=None):
        self.write_config(order, extra, source)
        return load_hardware_combination(self.path)

    def test_full_condition_preview_keeps_original_grid_and_is_offline(self):
        original = self.configure(extra="").plan.conditions()
        config = self.configure()
        self.assertEqual(config.plan.conditions(), original)
        bindings = config.snapshot["lockin_range_rules"]["condition_bindings"]
        self.assertEqual(len(bindings), len(original))
        self.assertEqual([row["condition_id"] for row in bindings],
                         [row["condition_id"] for row in original])
        summary = launch_summary(config, self.database, "test")
        preview = launch_text(summary, width=150)
        self.assertIn("first_visit", preview)
        self.assertIn("condition-000005", preview)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli(["describe-hardware", "--config", str(self.path)]), 0)
        self.assertEqual(json.loads(output.getvalue())["plan"]["lockin_range_rules"],
                         config.snapshot["lockin_range_rules"])
        self.assertEqual(self.manager.opened, [])
        self.assertEqual(self.log, [])

    def test_rules_reject_inactive_or_inner_coordinates_and_bad_coverage(self):
        cases = [RULES.replace("[1, 2]", "[0, 1, 2]"),
                 RULES.replace("[1, 2]", "[1]"),
                 RULES.replace("point_indices = { smu = [0] }", "match = { temperature_k = [2] }"),
                 RULES.replace("point_indices = { smu = [0] }", "match = { lockin_excitation_v_rms = [0.004] }"),
                 RULES.replace("lockin_xy_full_scale_v = 0.050", "lockin_aux_full_scale_v = 0.050"),
                 RULES.replace("0.050", "50e-6"),
                 "lockin_range_rules = []"]
        for extra in cases:
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                self.configure(extra=extra)
        with self.assertRaisesRegex(ValueError, "before lockin|outside lockin"):
            self.configure(order=("lockin", "smu"))
        self.assertEqual(self.manager.opened, [])
        self.assertEqual(self.log, [])

    def test_rules_require_lockin_and_reject_harmonic_or_segment_overrides(self):
        with self.assertRaisesRegex(ValueError, "active lockin"):
            self.configure(order=("smu",))
        harmonic = self.base.replace("[lockin_sweep]", '''
[lockin_xy.harmonic_settings.h2]
sensitivity_mode = "fixed"
sensitivity_full_scale_v = 0.002
reserve_mode = "normal"

[lockin_sweep]''')
        with self.assertRaisesRegex(ValueError, "per-harmonic"):
            self.configure(source=harmonic)
        segment = self.base.replace("excitation_points_v_rms = [0.004, 0.008]", '''excitation_ranges = [
  { min = 0.004, max = 0.008, scale = "linear", points = 2, xx_full_scale_v = 0.020 },
]''')
        with self.assertRaisesRegex(ValueError, "segment"):
            self.configure(source=segment)

    def test_absent_rules_preserves_existing_path_and_snapshot(self):
        config = self.configure(extra="")
        self.assertIsNone(config.lockin_range_schedule)
        self.assertNotIn("lockin_range_rules", config.snapshot)
        result = self.execute()
        self.assertEqual(result["status"], "completed", result)
        self.assertFalse(any(kind.startswith("lockin_range_") for kind, _ in self.events()))
        self.assertNotIn("last_recorded_lockin_ranges", run_snapshot(self.database, "test"))

    def test_fake_scan_applies_profiles_before_point_and_raw_checks_every_role(self):
        config = self.configure()
        result = self.execute()
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["cleanup"]["clean"], result)
        events = self.events()
        selected = [payload for kind, payload in events if kind == "lockin_range_selected"]
        self.assertEqual(len(selected), 6)
        self.assertEqual([row["rule_name"] for row in selected],
                         ["first_visit"] * 2 + ["other_visits"] * 4)
        for index, (kind, payload) in enumerate(events):
            if kind == "lockin_point_qualification":
                condition_id = payload["condition_id"]
                self.assertTrue(any(k == "lockin_range_selected" and p["condition_id"] == condition_id
                                    for k, p in events[:index]))
        with closing(open_readonly(self.database)) as db:
            self.assertEqual(validate_accepted_records(db, "test")["accepted_conditions"], 6)
        monitor = run_snapshot(self.database, "test")
        self.assertEqual(monitor["last_recorded_lockin_ranges"]["rule_name"], "other_visits")
        self.assertTrue(monitor["last_recorded_lockin_ranges"]["superseded_by_cleanup"])
        self.assertIn("Last recorded Lock-in ranges", snapshot_text(monitor))
        self.assertIn("superseded by cleanup", snapshot_text(monitor))
        # Cleanup restores the immutable role baselines, not the last rule.
        from attodry_control.lockin_model_settings import sensitivity_code_for
        for role, resource in (("lockin_xx", self.xx), ("lockin_xy", self.xy)):
            device = getattr(config.lockin, role)
            self.assertEqual(int(resource.responses["SENS?"]),
                             sensitivity_code_for(device, device.sensitivity_full_scale_v))

    def test_frequency_and_joint_sweeps_keep_selected_profile(self):
        source = re.sub(r"(?ms)^frequency_ranges = \[.*?^\]",
                        "frequency_points_hz = [17.777, 100]", self.base)
        for mode in ("frequency", "frequency_excitation"):
            with self.subTest(mode=mode):
                self.configure(source=source, extra='lockin_mode = "' + mode + '"\n' + RULES)
                result = self.execute(mode)
                self.assertEqual(result["status"], "completed", result)
                with closing(open_readonly(self.database)) as db:
                    self.assertEqual(validate_accepted_records(db, mode)["accepted_conditions"],
                                     6 if mode == "frequency" else 12)

    def test_partial_profile_failure_stops_formal_sampling_and_preserves_evidence(self):
        self.configure()
        from attodry_control.lockin_points import LockinPointSession
        original = LockinPointSession.apply_range_profile
        def failing(session, profile):
            self.xy.fail_write = "SENS 22"  # 50 mV, after XX has changed.
            return original(session, profile)
        with patch.object(LockinPointSession, "apply_range_profile", failing):
            result = self.execute()
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(run_snapshot(self.database, "test")["accepted_conditions"], 0)
        events = self.events()
        self.assertTrue(any(kind == "lockin_range_rule_failed" for kind, _ in events))
        self.assertFalse(any(kind == "lockin_formal_pair" for kind, _ in events))
        self.assertIs(run_snapshot(self.database, "test")["last_recorded_lockin_ranges"]["verified"], False)

    def test_raw_profile_and_pair_tampering_are_rejected_independently(self):
        config = self.configure()
        self.assertEqual(self.execute()["status"], "completed")
        with closing(open_readonly(self.database)) as db:
            condition = json.loads(db.execute("SELECT context_json FROM combination_conditions "
                "WHERE run_id='test' ORDER BY sequence_index LIMIT 1").fetchone()[0])
            sample = json.loads(db.execute("SELECT payload_json FROM combination_samples "
                "WHERE run_id='test' ORDER BY condition_id LIMIT 1").fetchone()[0])
        reading = next(value for value in sample["reads"] if value["module"] == "lockin")
        start = datetime.fromisoformat(sample["started_at_utc"])
        finish = datetime.fromisoformat(sample["finished_at_utc"])
        _validate_lockin_record(reading, config.snapshot, condition, start, finish)
        for scope in ("status", "pair"):
            for field, value in (("verified", False), ("rule_name", "other_visits"),
                                 ("confirmed_full_scales_v", {"lockin_xx": .01, "lockin_xy": .05})):
                with self.subTest(scope=scope, field=field):
                    changed = deepcopy(reading)
                    target = changed["status"] if scope == "status" else changed["status"]["samples"][0]
                    target["lockin_range_rule"][field] = value
                    with self.assertRaisesRegex(ValueError, "range context"):
                        _validate_lockin_record(changed, config.snapshot, condition, start, finish)
        # h2's unselected physical XX still must remain at the requested range.
        changed = deepcopy(reading)
        pair = next(value for value in changed["status"]["samples"] if value["harmonic"] == 2)
        for key in ("settings_before", "settings_after"):
            pair[key]["roles"]["lockin_xx"].update(sensitivity_code=22, sensitivity_full_scale_v=.05)
        with self.assertRaisesRegex(ValueError, "compiled range rule"):
            _validate_lockin_record(changed, config.snapshot, condition, start, finish)
        changed_snapshot = deepcopy(config.snapshot)
        changed_snapshot["lockin_range_rules"]["condition_bindings"][0]["full_scales_v"]["lockin_xy"] = .01
        with self.assertRaisesRegex(ValueError, "bindings"):
            _validate_lockin_record(reading, changed_snapshot, condition, start, finish)

    def test_repeated_samples_keep_one_profile_without_extra_writes(self):
        self.write_config(extra=RULES)
        self.path.write_text(self.path.read_text(encoding="utf-8").replace(
            "samples_per_condition = 1", "samples_per_condition = 3"), encoding="utf-8")
        result = self.execute()
        self.assertEqual(result["status"], "completed", result["error"])
        selected = [p for k, p in self.events() if k == "lockin_range_selected"]
        writes = [p for k, p in self.events() if k == "lockin_range_rule_write_attempt"]
        self.assertEqual(len(selected), 6)
        self.assertEqual(len(writes), 4)  # two changed roles on two profile transitions.
        with closing(open_readonly(self.database)) as db:
            self.assertEqual(validate_accepted_records(db, "test")["accepted_samples"], 18)
