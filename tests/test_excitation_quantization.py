"""6221 source grid, readback formatting, provenance and protection regressions."""
from copy import deepcopy
from decimal import Decimal
import math
import struct
import unittest

from attodry_control.excitation_quantization import (
    SOURCE_GRIDS, quantize_amplitude, readback_tolerance_a, validate_quantized_amplitude)
from attodry_control.keithley6221_ac import Keithley6221Ac, Keithley6221Error, validate_active_state
from tests.test_keithley6221_ac import Fake6221, source_config


class Rounded6221(Fake6221):
    """Independent transport model: binary32 storage and seven-digit AMPL?."""
    def query(self, command):
        value = super().query(command)
        if command == "SOUR:WAVE:AMPL?":
            value = format(struct.unpack("f", struct.pack("f", float(value)))[0], ".6e")
        return value


class AmplitudeQuantizationTests(unittest.TestCase):
    def active(self, current=21e-9, **config_changes):
        resource = Rounded6221()
        config = source_config(current_rms_points_a=[current], frequency_points_hz=[25.], **config_changes)
        events = []
        driver = Keithley6221Ac(resource, config, lambda *e: events.append(e), authorize_writes=True,
                               sleep=lambda seconds: self.fail("Normal quantization must not wait"))
        driver.preflight(); driver.configure()
        state = driver.set_point(current, 25.)
        return resource, driver, state, events

    def test_recorded_21na_failure_is_serialization_not_source_step(self):
        requested_peak = 21e-9 * math.sqrt(2)
        returned = float(format(struct.unpack("f", struct.pack("f", requested_peak))[0], ".6e"))
        self.assertEqual(returned, 2.969849e-8)
        self.assertNotEqual(returned, float(format(requested_peak, ".6e")))
        self.assertLess(abs(returned-requested_peak), readback_tolerance_a(requested_peak))
        resource, driver, state, events = self.active()
        self.assertEqual(state["amplitude_quantization"]["programmed_current_peak_a"], 2.970e-8)
        self.assertTrue(validate_active_state(state, driver.config, 21e-9, 25., require_quantization=True))
        self.assertEqual(len([x for x in resource.commands if x.startswith("SOUR:WAVE:AMPL ")]), 1)
        self.assertEqual(len([e for e in events if e[0] == "excitation_amplitude_quantized"]), 1)

    def test_all_six_reviewed_points_complete_with_native_rounding(self):
        points = [1e-9, 11e-9, 21e-9, 31e-9, 41e-9, 51e-9]
        peaks = [1.4142e-9, 1.5556e-8, 2.970e-8, 4.384e-8, 5.798e-8, 7.212e-8]
        for current, peak in zip(points, peaks):
            resource, driver, state, _ = self.active(current)
            self.assertEqual(state["amplitude_quantization"]["programmed_current_peak_a"], peak)
            self.assertEqual(state["amplitude_quantization"]["requested_current_rms_a"], current)
            self.assertEqual(state["actual"]["excitation_current_rms_a"], state["current_peak_a"]/math.sqrt(2))
            self.assertTrue(validate_active_state(state, driver.config, current, 25., require_quantization=True))
            self.assertEqual((len(resource.queries), len(resource.commands)), (135, 18))
            self.assertTrue(driver.cleanup()["verified"])

    def test_documented_grids_across_all_ranges_are_distinct_from_text_precision(self):
        for nominal, step in SOURCE_GRIDS:
            peak = float(nominal * Decimal(".6"))
            q = quantize_amplitude(peak/math.sqrt(2), 0., .105)
            self.assertEqual(q["planning_source_range_a"], float(nominal))
            self.assertEqual(q["programming_step_a"], float(step))
            self.assertLess(q["readback_abs_tolerance_a"], float(step)/100)
            ratio = Decimal(str(q["programmed_current_peak_a"])) / step
            self.assertEqual(ratio, ratio.to_integral_value())

    def test_nearest_and_equal_distance_tie_and_cross_range_boundary(self):
        for peak, expected in ((1.41424e-9, 1.4142e-9), (1.41426e-9, 1.4143e-9),
                               (2.10001e-9, 2.1e-9), (2.1006e-9, 2.101e-9)):
            q = quantize_amplitude(peak/math.sqrt(2), 0, .001)
            self.assertEqual(q["programmed_current_peak_a"], expected)
        # Direct arithmetic produces this exact positive decimal half-step.
        q = quantize_amplitude(1.41425e-9/math.sqrt(2), 0, .001)
        self.assertEqual(q["programmed_current_peak_a"], 1.4142e-9)

    def test_upper_limit_rounds_down_and_counts_both_offset_signs(self):
        for offset in (1e-9, -1e-9):
            peak = 1.41428e-9
            limit = abs(offset) + peak
            q = quantize_amplitude(peak/math.sqrt(2), offset, limit)
            self.assertLessEqual(Decimal(str(q["programmed_current_peak_a"]))+abs(Decimal(str(offset))), Decimal(str(limit)))
            self.assertEqual(q["programmed_current_peak_a"], 1.414e-9)
        with self.assertRaises(ValueError):
            quantize_amplitude(21e-9, 1e-9, 20e-9)

    def test_endpoints_and_nonfinite_or_boolean_inputs_fail_closed(self):
        for peak in (2e-12, .105):
            q = quantize_amplitude(peak/math.sqrt(2), 0., .105)
            self.assertEqual(q["programmed_current_peak_a"], peak)
        for current in (0., -1e-9, True, float("nan"), float("inf"), .106):
            with self.subTest(current=current), self.assertRaises(ValueError):
                quantize_amplitude(current, 0., .105)

    def test_distinct_targets_can_share_setting_without_changing_coordinates(self):
        first = quantize_amplitude(21e-9, 0., .001)
        second = quantize_amplitude(21.00001e-9, 0., .001)
        self.assertEqual(first["programmed_current_peak_a"], second["programmed_current_peak_a"])
        self.assertNotEqual(first["requested_current_rms_a"], second["requested_current_rms_a"])

    def test_wrong_setting_and_malformed_provenance_are_not_accepted(self):
        _, driver, original, _ = self.active()
        for value in (original["current_peak_a"]*10, original["current_peak_a"]+1e-11, True, float("nan")):
            bad = deepcopy(original); bad["current_peak_a"] = value
            with self.assertRaises(ValueError):
                validate_active_state(bad, driver.config, 21e-9, 25., require_quantization=True)
        for field, value in (("rule", "unknown"), ("requested_current_rms_a", 31e-9),
                             ("readback_abs_tolerance_a", 1.), ("programmed_current_peak_a", True)):
            bad = deepcopy(original); bad["amplitude_quantization"][field] = value
            with self.assertRaises(ValueError):
                validate_active_state(bad, driver.config, 21e-9, 25., require_quantization=True)
        bad = deepcopy(original); del bad["amplitude_quantization"]
        with self.assertRaises(ValueError):
            validate_active_state(bad, driver.config, 21e-9, 25., require_quantization=True)

    def test_readback_tolerance_never_expands_total_peak_ceiling(self):
        q = quantize_amplitude(21e-9, 0., 21e-9*math.sqrt(2))
        peak = q["programmed_current_peak_a"]
        q = quantize_amplitude(peak/math.sqrt(2), 0., peak)
        state = {"amplitude_quantization": q, "offset_a": 0., "current_peak_a": peak+q["readback_abs_tolerance_a"]/2}
        with self.assertRaisesRegex(ValueError, "envelope"):
            validate_quantized_amplitude(state, peak/math.sqrt(2), 0., peak)

    def test_compliance_communication_and_failed_command_remain_fatal(self):
        for fault in ("condition", "event", "communication", "ignored_amplitude"):
            resource, driver, _, _ = self.active()
            if fault == "condition": resource.state["STAT:MEAS:COND"] = "8"
            if fault == "event": resource.state["STAT:MEAS:EVEN"] = "8"
            if fault == "communication": resource.fail_query = "SOUR:WAVE:AMPL?"
            if fault == "ignored_amplitude": resource.state["SOUR:WAVE:AMPL"] = "1e-9"
            with self.subTest(fault=fault), self.assertRaises((Keithley6221Error, OSError)):
                driver.readback()


if __name__ == "__main__":
    unittest.main()
