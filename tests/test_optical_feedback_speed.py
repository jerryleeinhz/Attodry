"""Offline command budgets and response-based tuning; no hardware imports."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from optical_helpers import EXAMPLE, Clock, scanner
from test_power_feedback import current_feedback
from attodry_control.nkt_config import NktError, load_nkt_config
from attodry_control.optical_config import WindowConfig, load_pm_config, load_scan_config
from attodry_control.pm100d import Pm100d, SimulatedPmResource
from attodry_control.power_feedback import (PowerWindow, adaptive_feedback_step, next_cld_current_a,
    next_source_current_pct, next_source_power_pct, target_in_tolerance, tuning_in_tolerance)


class PowerEvidenceBudgetTests(unittest.TestCase):
    def meter(self, resource=None):
        return Pm100d(load_pm_config(EXAMPLE), resource or SimulatedPmResource(), clock=Clock())

    def test_unchanged_prepare_uses_one_fresh_full_capture(self):
        meter = self.meter()
        before = meter.prepare(633)
        self.assertEqual(len(meter.transcript), 13)
        self.assertFalse(any(item["kind"] == "setting" for item in meter.transcript))
        self.assertEqual(before["questionable_condition"], 0)
        meter.transcript.clear()
        meter.prepare(635)
        self.assertEqual(sum(item["command"] == "*IDN?" for item in meter.transcript), 2)
        self.assertIn("SENS:CORR:WAV 635", [item["command"] for item in meter.transcript])

    def test_each_read_keeps_its_own_two_full_captures(self):
        meter = self.meter()
        first = meter.read_sample(633)
        self.assertEqual(len(meter.transcript), 27)
        meter.resource.values["SENS:POW:RANG?"] = "0.5"
        second = meter.read_sample(633)
        self.assertEqual(len(meter.transcript), 54)
        self.assertEqual([first["sequence"], second["sequence"]], [1, 2])
        self.assertEqual(first["after"]["range_w"], 1)
        self.assertEqual(second["before"]["range_w"], 0.5)
        self.assertEqual(sum(item["command"] == "READ?" for item in meter.transcript), 2)
        self.assertIn("this READ?", second["verification_scope"])

    def test_setting_change_inside_read_is_not_hidden_by_reuse(self):
        resource = SimulatedPmResource()
        meter = self.meter(resource)
        def changed_during_read():
            resource.values["SENS:AVER:COUN?"] = "2"
            return 0.001
        resource.power_provider = changed_during_read
        with self.assertRaisesRegex(NktError, "average/range settings mismatch"):
            meter.read_sample(633)
        self.assertIsNone(meter.last_sample)
        self.assertEqual(sum(item["command"] == "READ?" for item in meter.transcript), 1)
        self.assertEqual(sum(item["command"] == "STAT:QUES:COND?" for item in meter.transcript), 2)

    def test_joint_sample_reuses_pm_but_keeps_laser_and_pem_guards(self):
        scan = scanner()
        point = scan.nkt_config.points[0]
        scan.nkt.begin_session(authorize_writes=False, confirm_manual_route=False)
        scan.nkt.prepare_point(point)
        scan.pem.preflight()
        scan.pem.prepare(633, scan.config.peak_retardance_waves)
        scan.meter.prepare(633)
        scan.nkt.start_emission(point)
        start = len(scan.meter.transcript)
        laser_start = len(scan.nkt.backend.commands)
        pem_start = len(scan.pem.transcript)
        row = {"condition_id": "fake-0", "feedback_config": None, "power_samples": []}
        sample = scan._sample(row, point, 633, None, "formal")
        self.assertEqual(len(scan.meter.transcript) - start, 27)
        self.assertGreater(len(scan.nkt.backend.commands), laser_start)
        self.assertGreater(len(scan.pem.transcript), pem_start)
        state = scan.record["last_confirmed_state"]
        self.assertEqual(state["pm_sample_sequence"], sample["sequence"])
        self.assertEqual(state["pm100d"], sample["after"])
        self.assertIsNot(state["pm100d"], sample["after"])
        scan.nkt.finish_session()
        scan.pem.finish()
        scan.meter.close()


class PrecisionPolicyTests(unittest.TestCase):
    def sample(self, sequence, when, power):
        return {"sequence": sequence, "power_w": power, "valid": True, "unit": "W",
                "started_monotonic_s": when, "finished_monotonic_s": when}

    def test_relative_variation_is_capped_by_absolute_limit_and_noise_floor(self):
        cfg = WindowConfig(1, 3, 0.1, 1e-6, max_peak_to_peak_fraction=0.1, noise_floor_w=1e-7)
        self.assertAlmostEqual(cfg.variation_limit_w(5e-6), 5e-7)
        self.assertAlmostEqual(cfg.variation_limit_w(1e-8), 1e-7)
        self.assertAlmostEqual(cfg.variation_limit_w(1e-3), 1e-6)
        with self.assertRaises(NktError):
            PowerWindow(cfg, epoch_s=0, max_power_w=0.01)

    def test_drift_rejects_a_low_peak_to_peak_but_trending_window(self):
        cfg = WindowConfig(2, 3, 1, 1e-6, max_drift_fraction_per_s=0.005)
        window = PowerWindow(cfg, epoch_s=0, max_power_w=0.01, target_power_w=1e-5)
        for sequence, when in enumerate((0, 1, 2), 1):
            result = window.add(self.sample(sequence, when, 1e-5 + when * 0.2e-6))
        self.assertLess(result["peak_to_peak_w"], cfg.max_peak_to_peak_w)
        self.assertFalse(result["power_window_stable"])
        self.assertIn("drift trend", result["reason"])
        self.assertAlmostEqual(result["drift_w_per_s"], 0.2e-6)

    def test_narrow_tuning_is_explicit_and_never_widens_formal_acceptance(self):
        legacy = current_feedback()
        narrow = replace(legacy, tuning_tolerance_fraction=0.02)
        self.assertTrue(target_in_tolerance(9.5e-6, narrow))
        self.assertFalse(tuning_in_tolerance(9.5e-6, narrow))
        self.assertTrue(tuning_in_tolerance(9.5e-6, legacy))
        with self.assertRaisesRegex(NktError, "must not exceed"):
            replace(legacy, tuning_tolerance_fraction=0.2).validate()

    def test_positive_local_response_only_shrinks_the_approved_step(self):
        config = replace(current_feedback(), tuning_tolerance_fraction=0.02)
        step, evidence = adaptive_feedback_step(4, 9e-6, config, 1, [(3, 5e-6)])
        self.assertGreaterEqual(step, 0.1)
        self.assertLess(step, 0.4)
        self.assertGreater(evidence["local_slope_w_per_unit"], 0)
        ignored, evidence = adaptive_feedback_step(4, 9e-6, config, 1, [(3, 11e-6)])
        self.assertEqual(ignored, 0.4)
        self.assertIsNone(evidence["local_slope_w_per_unit"])
        legacy, _ = adaptive_feedback_step(4, 9e-6, current_feedback(), 1, [(3, 5e-6)])
        self.assertEqual(legacy, 1)

    def test_power_and_cld_actuators_preserve_their_native_units_and_bounds(self):
        base = current_feedback()
        power = replace(base, actuator="source_power", source_current_min_pct=None,
            source_current_max_pct=None, source_current_step_pct=None, source_power_min_pct=0,
            source_power_max_pct=8, source_power_step_pct=1)
        self.assertEqual(next_source_power_pct(4, 5e-6, power), 5)
        self.assertEqual(next_source_power_pct(7.8, 5e-6, power), 8)
        cld = replace(base, actuator="cld_current", source_current_min_pct=None,
            source_current_max_pct=None, source_current_step_pct=None,
            current_min_a=0.01, current_max_a=0.08, current_step_a=0.001)
        self.assertAlmostEqual(next_cld_current_a(0.04, 5e-6, cld), 0.041)
        self.assertAlmostEqual(next_cld_current_a(0.0795, 5e-6, cld), 0.08)
        for policy, method, current in ((power, next_source_power_pct, 4), (cld, next_cld_current_a, 0.04)):
            with self.subTest(actuator=policy.actuator), self.assertRaises(NktError):
                method(current, 0, policy)

    def test_new_fields_load_strictly_and_old_fields_remain_optional(self):
        original = EXAMPLE.read_text().replace('mode = "direct"', 'mode = "power_stabilized"')
        text = original.replace('[power_feedback]\n', '[power_feedback]\ntuning_tolerance_fraction = 0.01\n')
        text = text.replace('[power_feedback.window]\n', '[power_feedback.window]\n'
            'max_peak_to_peak_fraction = 0.05\nnoise_floor_w = 0.000000001\nmax_drift_fraction_per_s = 0.01\n')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fake.toml"
            path.write_text(text, encoding="utf-8")
            scan, _, _ = load_scan_config(path, load_nkt_config(path))
            self.assertEqual(scan.feedback.tuning_tolerance_fraction, 0.01)
            self.assertEqual(scan.feedback.window.max_peak_to_peak_fraction, 0.05)
            path.write_text(text.replace('noise_floor_w =', 'noise_flooor_w ='), encoding="utf-8")
            with self.assertRaisesRegex(NktError, "unknown"):
                load_scan_config(path, load_nkt_config(path))

    def test_power_range_expands_without_adding_a_second_target_source(self):
        original = EXAMPLE.read_text().replace('mode = "direct"', 'mode = "power_stabilized"')
        original = original.replace('target_power_w = 0.001', 'target_power_ranges = '
            '[{min = 0.001, max = 0.002, scale = "linear", points = 2}]')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fake.toml"
            path.write_text(original, encoding="utf-8")
            scan, _, _ = load_scan_config(path, load_nkt_config(path))
            self.assertEqual(scan.feedback.target_powers_w, (0.001, 0.002))
            path.write_text(original.replace('[power_feedback]\n',
                '[power_feedback]\ntarget_power_w = 0.001\n'), encoding="utf-8")
            with self.assertRaisesRegex(NktError, "exactly one"):
                load_scan_config(path, load_nkt_config(path))


if __name__ == "__main__":
    unittest.main()
