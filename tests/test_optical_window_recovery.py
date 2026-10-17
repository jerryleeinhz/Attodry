"""Bounded full optical/electrical windows and matched darks with owned fakes."""
from contextlib import closing
from copy import deepcopy
import json
import math
import tomllib
import unittest
from unittest.mock import patch

from tests import test_internal_optical_combination as internal_helpers
from tests.test_three_smu_config import OPERATION_TEXT
from attodry_control.combination_analysis import load_combination_rows
from attodry_control.combination_hardware import HardwareCombinationStation
from attodry_control.combination_store import open_readonly
from attodry_control.lockin_points import LockinPointSession
from attodry_control.optical_points import OpticalPointSession


class OpticalWindowRecoveryTests(unittest.TestCase):
    setUp = internal_helpers.InternalOpticalCombinationTests.setUp
    write_config = internal_helpers.InternalOpticalCombinationTests.write_config
    factory = internal_helpers.InternalOpticalCombinationTests.factory
    events = internal_helpers.InternalOpticalCombinationTests.events
    write_document = internal_helpers.InternalOpticalCombinationTests.write_document
    backend_factory = internal_helpers.InternalOpticalCombinationTests.backend_factory
    meter_factory = internal_helpers.InternalOpticalCombinationTests.meter_factory
    chopper_factory = internal_helpers.InternalOpticalCombinationTests.chopper_factory
    enable_chopper = internal_helpers.InternalOpticalCombinationTests.enable_chopper
    execute = internal_helpers.InternalOpticalCombinationTests.execute
    assert_closed_laser_off = internal_helpers.InternalOpticalCombinationTests.assert_closed_laser_off

    def retries(self, *, maximum=1, timeout=2):
        self.document["optical_scan"].update(max_power_retries=maximum, power_retry_timeout_s=timeout)
        self.write_document()

    def after_guard_injection(self, action, *, every=False):
        original = OpticalPointSession._collect_guard_sample
        used = []
        def guarded(session, phase, **kwargs):
            if phase == "after_electrical" and (every or not used):
                used.append(phase)
                return action(original, session, phase, kwargs)
            return original(session, phase, **kwargs)
        return patch.object(OpticalPointSession, "_collect_guard_sample", guarded), used

    def target_deviation(self, original, session, phase, kwargs):
        resource = session.meter.resource
        provider = resource.power_provider
        resource.power_provider = lambda: session.feedback.target_power_w * 1.2
        try:
            return original(session, phase, **kwargs)
        finally:
            resource.power_provider = provider

    def stored_samples(self):
        with closing(open_readonly(self.database)) as db:
            return [json.loads(row[0]) for row in db.execute(
                "SELECT payload_json FROM combination_samples ORDER BY rowid")]

    def test_one_target_deviation_discards_whole_candidate_and_rereads_both_modules(self):
        self.retries()
        fault, injected = self.after_guard_injection(self.target_deviation)
        original = HardwareCombinationStation.read
        lockin_reads = []
        def numbered(station, module):
            result = original(station, module)
            if module == "lockin":
                lockin_reads.append(result)
                value = len(lockin_reads) * 1e-6
                for role in ("xx", "xy"):
                    prefix = f"lockin_{role}_h1_"
                    result["measurements"].update({prefix + "x_v": value, prefix + "y_v": 0,
                        prefix + "amplitude_v": value, prefix + "phase_deg": 0})
            return result
        with fault, patch.object(HardwareCombinationStation, "read", numbered):
            result = self.execute("one-power-retry")
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(len(injected), 1)
        samples = self.stored_samples()
        self.assertEqual(len(samples), 2)
        self.assertEqual([sample["measurements"]["lockin_xx_h1_x_v"] for sample in samples], [2e-6, 3e-6])
        self.assertTrue(all([read["module"] for read in sample["reads"]] == ["optical", "lockin"]
                            for sample in samples))
        events = self.events()
        raw = [payload for kind, payload in events if kind == "raw_reading"]
        self.assertEqual([(p["sample_index"], p["candidate_index"], p["reading"]["module"]) for p in raw],
            [(0, 1, "optical"), (0, 1, "lockin"), (0, 2, "optical"), (0, 2, "lockin"),
             (1, 1, "optical"), (1, 1, "lockin")])
        rejected = [payload for kind, payload in events if kind == "optical_window_rejected"]
        self.assertEqual(len(rejected), 1)
        self.assertFalse(rejected[0]["accepted"])
        self.assertFalse(rejected[0]["valid_for_analysis"])
        self.assertEqual(rejected[0]["evidence"]["power_sample"]["power_w"], 6e-6)
        self.assertEqual(sum(kind == "optical_window_retry" for kind, _ in events), 1)
        self.assertEqual(len(load_combination_rows(self.database)), 2)
        self.assert_closed_laser_off()

    def test_retry_deadline_is_shared_with_retuning_and_does_not_repeat_forever(self):
        self.retries(timeout=0.01)
        fault, _ = self.after_guard_injection(self.target_deviation)
        with fault:
            result = self.execute("short-retry-budget")
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("timeout", result["error"].lower())
        self.assertEqual(load_combination_rows(self.database), ())
        self.assertEqual(sum(kind == "optical_window_rejected" for kind, _ in self.events()), 1)
        self.assert_closed_laser_off()

    def test_one_retry_budget_preserves_both_failed_windows(self):
        self.retries()
        fault, injected = self.after_guard_injection(self.target_deviation, every=True)
        with fault:
            result = self.execute("retry-exhausted")
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(len(injected), 2)
        self.assertEqual(sum(kind == "optical_window_rejected" for kind, _ in self.events()), 2)
        self.assertEqual(sum(kind == "optical_window_retry" for kind, _ in self.events()), 1)
        self.assertEqual(load_combination_rows(self.database), ())
        self.assert_closed_laser_off()

    def test_late_last_read_cannot_promote_a_recovered_window_after_deadline(self):
        self.retries(timeout=2)
        original = OpticalPointSession._collect_guard_sample
        failed, deadlines = [], []
        def late_final_read(session, phase, **kwargs):
            if phase == "after_electrical" and not failed:
                failed.append(True)
                return self.target_deviation(original, session, phase, kwargs)
            if phase == "after_electrical" and session.retry_deadline is not None:
                resource = session.meter.resource
                provider = resource.power_provider
                deadlines.append(session.retry_deadline)
                def late_power():
                    power = provider()
                    self.clock.value = session.retry_deadline + 0.001
                    return power
                resource.power_provider = late_power
                try:
                    return original(session, phase, **kwargs)
                finally:
                    resource.power_provider = provider
            return original(session, phase, **kwargs)
        with patch.object(OpticalPointSession, "_collect_guard_sample", late_final_read):
            result = self.execute("late-recovered-final-read")
        self.assertEqual(result["status"], "failed", result)
        self.assertRegex(result["error"].lower(), "timeout|deadline")
        self.assertEqual(len(deadlines), 1)
        self.assertGreater(self.meter.last_sample["finished_monotonic_s"], deadlines[0])
        self.assertEqual(self.stored_samples(), [])
        self.assertEqual(load_combination_rows(self.database), ())
        self.assertEqual(sum(kind == "optical_window_retry" for kind, _ in self.events()), 1)
        audits = [payload for kind, payload in self.events() if kind == "optical_audit"]
        self.assertEqual(len(audits), 1)
        self.assertTrue(any(sample["finished_monotonic_s"] > deadlines[0]
            for point in audits[0]["points"] for sample in point["power_samples"]))
        self.assert_closed_laser_off()

    def hard_failure(self, kind):
        self.retries()
        if kind == "chopper":
            self.enable_chopper()
        fault_log_positions = []
        def fault(original, session, phase, kwargs):
            fault_log_positions.append((len(self.optical_log), len(session.backend.commands)))
            if kind == "power":
                session.meter.resource.power_provider = lambda: session.config.pm.max_power_w * 2
            elif kind == "source":
                with patch.object(session.backend, "read_source", side_effect=OSError("source response lost")):
                    return original(session, phase, **kwargs)
            elif kind == "settings":
                session.meter.resource.values["SENS:AVER:COUN?"] = "2"
            else:
                session.chopper.transport.values["nharmonic"] = 2
            return original(session, phase, **kwargs)
        injected, used = self.after_guard_injection(fault)
        with injected:
            result = self.execute("hard-" + kind)
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(len(used), 1)
        self.assertFalse(any(name in ("optical_window_retry", "optical_window_rejected") for name, _ in self.events()))
        optical_position, laser_position = fault_log_positions[0]
        self.assertFalse(any(item[:2] == ("emission", True) for item in self.optical_log[optical_position:]))
        self.assertFalse(any(item[0] == "configure" for item in self.backend.commands[laser_position:]))
        self.assertEqual(load_combination_rows(self.database), ())
        self.assert_closed_laser_off()

    def test_hard_pm_limit_does_not_retry_or_increase_output(self):
        self.hard_failure("power")

    def test_source_communication_fault_does_not_retry_or_increase_output(self):
        self.hard_failure("source")

    def test_settings_drift_does_not_retry_or_increase_output(self):
        self.hard_failure("settings")

    def test_chopper_drift_does_not_retry_or_increase_output(self):
        self.hard_failure("chopper")

    def test_ordinary_power_deviation_cannot_hide_hard_after_bracket_fault(self):
        self.retries()
        original = OpticalPointSession._collect_guard_sample
        failed = []
        def mixed_fault(session, phase, **kwargs):
            if phase == "formal" and not failed:
                failed.append(True)
                resource = session.meter.resource
                provider = resource.power_provider
                resource.power_provider = lambda: session.feedback.target_power_w * 1.2
                try:
                    return original(session, phase, **kwargs)
                finally:
                    resource.power_provider = provider
                    resource.values["SENS:AVER:COUN?"] = "2"
            return original(session, phase, **kwargs)
        with patch.object(OpticalPointSession, "_collect_guard_sample", mixed_fault):
            result = self.execute("mixed-power-and-settings-fault")
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("average/range settings mismatch", result["error"])
        self.assertFalse(any(kind in ("optical_window_retry", "optical_window_rejected") for kind, _ in self.events()))
        self.assertEqual(load_combination_rows(self.database), ())
        self.assert_closed_laser_off()

    def test_default_background_has_no_dark_calls_or_difference_columns(self):
        original = LockinPointSession.sample_point
        calls = []
        def counted(session, **kwargs):
            calls.append(session.sample_count)
            return original(session, **kwargs)
        with patch.object(LockinPointSession, "sample_point", counted):
            result = self.execute("background-disabled")
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(len(calls), 2)
        self.assertFalse(any(kind == "optical_dark_background" for kind, _ in self.events()))
        self.assertTrue(all(not any("delta_" in key for key in sample["measurements"])
                            for sample in self.stored_samples()))
        self.assert_closed_laser_off()

    def test_background_three_darks_and_two_lights_subtract_complex_components(self):
        self.document["optical_background"] = dict(enabled=True, samples=3, max_age_s=10)
        self.write_document()
        original = LockinPointSession.sample_point
        states = []
        def optical_response(session, **kwargs):
            result = original(session, **kwargs)
            light = self.backend.source["emission_state"] == 3
            states.append(light)
            for role in ("xx", "xy"):
                x, y = (4e-6, 6e-6) if light else (1e-6, 2e-6)
                prefix = f"lockin_{role}_h1_"
                result["measurements"].update({prefix + "x_v": x, prefix + "y_v": y,
                    prefix + "amplitude_v": math.hypot(x, y),
                    prefix + "phase_deg": math.degrees(math.atan2(y, x))})
            return result
        with patch.object(LockinPointSession, "sample_point", optical_response):
            result = self.execute("background-enabled")
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(states, [False, False, False, True, True])
        baselines = [payload for kind, payload in self.events() if kind == "optical_dark_background"]
        self.assertEqual(len(baselines), 1)
        self.assertEqual(len(baselines[0]["raw_readings"]), 3)
        self.assertTrue(baselines[0]["valid_for_analysis"])
        samples = self.stored_samples()
        self.assertEqual(len(samples), 2)
        for sample in samples:
            self.assertAlmostEqual(sample["measurements"]["lockin_xy_h1_delta_x_v"], 3e-6)
            self.assertAlmostEqual(sample["measurements"]["lockin_xy_h1_delta_y_v"], 4e-6)
            self.assertAlmostEqual(sample["measurements"]["lockin_xy_h1_delta_amplitude_v"], 5e-6)
            self.assertAlmostEqual(sample["measurements"]["lockin_xy_h1_delta_phase_deg"],
                                   math.degrees(math.atan2(4, 3)))
            self.assertEqual(sample["measurements"]["lockin_xy_h1_x_v"], 4e-6)
        self.assert_closed_laser_off()

    def test_background_reference_phase_context_change_rejects_light(self):
        self.document["optical_background"] = dict(enabled=True, samples=3, max_age_s=10)
        self.write_document()
        original = HardwareCombinationStation._background_context
        def changed_after_light(station):
            result = original(station)
            if self.backend.source["emission_state"] == 3:
                result["phase_shift_deg"]["xx"] += 1
            return result
        with patch.object(HardwareCombinationStation, "_background_context", changed_after_light):
            result = self.execute("background-phase-mismatch")
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("context mismatch", result["error"])
        self.assertEqual(load_combination_rows(self.database), ())
        self.assert_closed_laser_off()

    def test_gate_conditions_capture_distinct_matching_dark_baselines(self):
        smu = tomllib.loads(OPERATION_TEXT)
        smu.pop("smu_bias")
        smu.pop("gate_bottom")
        smu["three_smu_run"].update(delay_s=0.03, serpentine=False)
        smu["three_smu_run"]["smu_bias"] = {"role": "off", "bidirectional": False}
        smu["three_smu_run"]["gate_bottom"] = {"role": "off", "bidirectional": False}
        smu["three_smu_run"]["gate_top"] = dict(role="sweep", bidirectional=False,
            points=[-0.1, 0.1], zero_readback_tolerance_v=0.05)
        self.document.update(smu)
        self.document["optical_background"] = dict(enabled=True, samples=1, max_age_s=10)
        self.document["combination_scan"].update(order=["optical", "smu", "lockin"],
            illumination_policy="continuous_gate_scan", samples_per_condition=1)
        self.write_document()
        result = self.execute("background-gates")
        self.assertEqual(result["status"], "completed", result)
        baselines = [payload for kind, payload in self.events() if kind == "optical_dark_background"]
        self.assertEqual(len(baselines), 2)
        self.assertEqual([value["context"]["requested_conditions"]["smu"]["gate_top_v"]
                          for value in baselines], [-0.1, 0.1])
        self.assertNotEqual(baselines[0]["baseline_id"], baselines[1]["baseline_id"])
        samples = self.stored_samples()
        ids = [next(read for read in sample["reads"] if read["module"] == "lockin")["background"]["baseline_id"]
               for sample in samples]
        self.assertEqual(ids, [value["baseline_id"] for value in baselines])
        self.assert_closed_laser_off()


if __name__ == "__main__":
    unittest.main()
