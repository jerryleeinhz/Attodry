"""Whole-condition range-rule recovery through real engines and fake DLL/VISA."""
from contextlib import closing
from collections import Counter
import json
import unittest
from unittest.mock import patch

from attodry_control.combination_analysis import load_combination_rows
from attodry_control.combination_hardware import load_hardware_combination, run_hardware_combination
from attodry_control.combination_resume import build_recovery
from attodry_control.combination_store import (
    CombinationStore, hardware_recovery_state, open_readonly, validate_accepted_records,
)
from attodry_control.lockin_points import LockinPointSession
from tests import test_combination_cryostat as fixture
from tests import test_combination_range_rules as range_fixture


RUN = "range-recovery-hardware"


class RangeRuleHardwareRecoveryTests(unittest.TestCase):
    setUp = fixture.CryostatCombinationTests.setUp
    write_config = fixture.CryostatCombinationTests.write_config
    factory = fixture.CryostatCombinationTests.factory
    events = fixture.CryostatCombinationTests.events

    def configure(self, order=("smu", "lockin"), rules=range_fixture.RULES, source=None):
        source = (source or self.base).replace(
            "excitation_points_v_rms = [0.004, 0.008]", "excitation_points_v_rms = [0.004]")
        self.write_config(order, rules, source)
        self.path.write_text(self.path.read_text(encoding="utf-8").replace(
            "samples_per_condition = 1", "samples_per_condition = 2"), encoding="utf-8")
        return load_hardware_combination(self.path)

    def execute(self, config, recovery=None):
        clock = fixture.Clock()
        with CombinationStore(self.database) as store:
            return run_hardware_combination(config, store, RUN,
                authorize_hardware=True, authorize_cryostat=True,
                confirm_xy_sine_disconnected=True, dll=self.dll,
                monotonic=clock.monotonic, sleep=clock.sleep,
                smu_adapter_factory=self.factory, manager_factory=lambda: self.manager,
                recovery=recovery)

    def accepted_prefix(self):
        with closing(open_readonly(self.database)) as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM combination_samples WHERE run_id=? AND condition_id='condition-000000' "
                "ORDER BY sample_index", (RUN,))]

    def verify_interruption_and_resume(self, config, result):
        conditions = config.plan.conditions()
        self.assertEqual(result["status"], "interrupted", result)
        self.assertEqual(result["error"], "KeyboardInterrupt: ")
        state = hardware_recovery_state(self.database, RUN)
        self.assertEqual(set(state["accepted_condition_ids"]), {"condition-000000"})
        actions = {action["module"]: action for action in result["cleanup"]["actions"]}
        self.assertEqual(set(actions), {axis.module for axis in config.plan.axes})
        self.assertIs(result["cleanup"]["communication_uncertain"], False)
        for module, action in actions.items():
            cleanup = action["result"]
            if module == "smu":
                self.assertIs(cleanup["manual_verification_required"], False)
                self.assertFalse(cleanup.get("communication_uncertain", False))
                self.assertEqual(cleanup["cleanup_errors"], [])
                self.assertEqual(cleanup["result"], "confirmed_safe")
                self.assertTrue(all(item["zero_readback_recorded"] and item["output_off_confirmed"]
                                    for item in cleanup["actions"]))
            else:
                self.assertIs(cleanup["verified"], True, cleanup)
        # Preserve the original failure/manual flags; acknowledge the synthetic
        # cleanup evidence through the ordinary explicit recovery review.
        prefix = self.accepted_prefix()
        self.assertEqual(len(prefix), 2)
        before_events = len(self.events())
        with closing(open_readonly(self.database)) as db:
            original_conditions = [row[0] for row in db.execute(
                "SELECT context_json FROM combination_conditions WHERE run_id=? ORDER BY sequence_index", (RUN,))]
            validated = validate_accepted_records(db, RUN)
        self.assertEqual(validated, {"accepted_conditions": 1, "accepted_samples": 2})
        self.assertEqual([json.loads(value) for value in original_conditions], conditions)
        recovery = build_recovery(config, self.database, RUN,
            acknowledge_failure=True, manual_verification=True)
        self.assertIs(recovery["review"]["ready"], True)
        self.assertEqual(recovery["accepted_conditions"], 1)
        self.assertEqual(recovery["next_condition"]["condition_id"], "condition-000001")
        resumed = self.execute(config, recovery)
        self.assertEqual(resumed["status"], "completed", resumed)
        self.assertIs(resumed["cleanup"]["clean"], True)
        self.assertIs(resumed["cleanup"]["manual_verification_required"], False)
        self.assertEqual(self.accepted_prefix(), prefix)
        state = hardware_recovery_state(self.database, RUN)
        self.assertEqual(len(state["accepted_condition_ids"]), len(conditions))
        with closing(open_readonly(self.database)) as db:
            self.assertEqual(validate_accepted_records(db, RUN), {
                "accepted_conditions": len(conditions), "accepted_samples": len(conditions) * 2})
            self.assertEqual([row[0] for row in db.execute(
                "SELECT context_json FROM combination_conditions WHERE run_id=? ORDER BY sequence_index", (RUN,))],
                original_conditions)
            attempts = [dict(row) for row in db.execute(
                "SELECT * FROM combination_attempts WHERE run_id=? ORDER BY condition_id,attempt_index", (RUN,))]
        self.assertEqual([(row["attempt_index"], row["status"]) for row in attempts
                          if row["condition_id"] == "condition-000001"], [(0, "rejected"), (1, "accepted")])
        rows = load_combination_rows(self.database, run_id=RUN)
        self.assertEqual(len(rows), len(conditions) * 2)
        pending_rows = [row for row in rows if row["condition_id"] == "condition-000001"]
        self.assertEqual([row["attempt_index"] for row in pending_rows], [1, 1])
        self.assertEqual([row["sample_index"] for row in pending_rows], [0, 1])
        binding = config.snapshot["lockin_range_rules"]["condition_bindings"][1]
        new_events = self.events()[before_events:]
        applied = [(index, payload) for index, (kind, payload) in enumerate(new_events)
                   if kind == "lockin_range_rule_applied" and payload["condition_id"] == "condition-000001"]
        self.assertEqual(len(applied), 1)
        applied_index, context = applied[0]
        self.assertIs(context["verified"], True)
        self.assertEqual(context["attempt_index"], 1)
        self.assertEqual(context["rule_name"], binding["rule_name"])
        self.assertEqual(context["confirmed_full_scales_v"], binding["full_scales_v"])
        self.assertEqual(len(context["transitions"]), 2)
        qualified_index = next(index for index, (kind, payload) in enumerate(new_events)
                               if kind == "lockin_point_qualification"
                               and payload["condition_id"] == "condition-000001")
        self.assertLess(applied_index, qualified_index)
        for row in pending_rows:
            status = row["status.lockin"]
            self.assertEqual(status["lockin_range_rule"]["confirmed_full_scales_v"], binding["full_scales_v"])
            self.assertEqual(sorted((role, pair["harmonic"]) for pair in status["samples"]
                                    for role in pair["selected_roles"]),
                             sorted((role, harmonic) for role, harmonics in
                                    config.snapshot["hardware"]["lockin"]["harmonics_by_role"].items()
                                    for harmonic in harmonics))
            for pair in status["samples"]:
                self.assertEqual(pair["lockin_range_rule"]["rule_name"], binding["rule_name"])
                for role, scale in binding["full_scales_v"].items():
                    self.assertEqual(pair["settings_after"]["roles"][role]["sensitivity_full_scale_v"], scale)
        return new_events

    def test_interrupt_before_pending_profile_preserves_accepted_and_reapplies(self):
        config = self.configure()
        apply_profile = LockinPointSession.apply_range_profile
        calls = []
        def interrupt_pending(session, profile):
            calls.append(profile["name"])
            if len(calls) == 2:
                raise KeyboardInterrupt
            return apply_profile(session, profile)
        with patch.object(LockinPointSession, "apply_range_profile", interrupt_pending):
            result = self.execute(config)
        self.assertEqual(calls, ["first_visit", "other_visits"])
        self.assertFalse(any(kind == "lockin_formal_pair" and payload["condition_id"] == "condition-000001"
                             for kind, payload in self.events()))
        self.verify_interruption_and_resume(config, result)

    def test_joint_temperature_field_partial_window_is_remeasured_whole(self):
        source = self.base.replace("stop_k = 2.0", "stop_k = 2.1").replace(
            "{ bx_t = 0.0, bz_t = 0.0 },", "{ bx_t = 0.0, bz_t = 0.0 },\n{ bx_t = 0.0, bz_t = 0.01 },")
        rules = ""
        for t_index, temperature in enumerate((2., 2.1)):
            for b_index, bz in enumerate((0., .01)):
                rules += f'''
[[combination_scan.lockin_range_rules]]
name = "t{t_index}-b{b_index}"
match = {{ temperature_k = [{temperature}], field_z_t = [{bz}] }}
lockin_xx_full_scale_v = {0.020 if t_index == 0 else 0.010}
lockin_xy_full_scale_v = {0.050 if b_index == 0 else 0.020}
'''
        config = self.configure(("temperature", "magnetic", "lockin"), rules, source)
        self.assertEqual(len(config.plan.conditions()), 4)
        event = CombinationStore.event
        interrupted = []
        def interrupt_after_partial_pair(store, run_id, kind, payload):
            value = event(store, run_id, kind, payload)
            if (kind == "lockin_formal_pair" and payload["condition_id"] == "condition-000001"
                    and payload["harmonic"] == 1 and not interrupted):
                interrupted.append(payload)
                raise KeyboardInterrupt
            return value
        with patch.object(CombinationStore, "event", interrupt_after_partial_pair):
            result = self.execute(config)
        self.assertEqual(len(interrupted), 1)
        partial = [payload for kind, payload in self.events()
                   if kind == "lockin_formal_pair" and payload["condition_id"] == "condition-000001"]
        self.assertEqual([payload["harmonic"] for payload in partial], [1])
        self.assertEqual(partial[0]["attempt_index"], 0)
        new_events = self.verify_interruption_and_resume(config, result)
        repeated = [payload for kind, payload in new_events
                    if kind == "lockin_formal_pair" and payload["condition_id"] == "condition-000001"]
        self.assertEqual(Counter((role, payload["harmonic"]) for payload in repeated
                                 for role in payload["selected_roles"]),
                         Counter((role, harmonic) for _ in range(2) for role, harmonics in
                                 config.snapshot["hardware"]["lockin"]["harmonics_by_role"].items()
                                 for harmonic in harmonics))
        self.assertTrue(all(payload["attempt_index"] == 1 for payload in repeated))
        self.assertEqual(partial[0]["lockin_range_rule"]["rule_name"], "t0-b1")
        self.assertTrue(any(kind == "temperature_qualification_completed" for kind, _ in new_events))
        self.assertTrue(any(kind == "magnetic_point_event" for kind, _ in new_events))


if __name__ == "__main__":
    unittest.main()
