import copy
import math
import unittest

import numpy as np

from attodry_control.pem_internal_analysis import rc_amplitude_retention, summarize_capture


def rotating_capture(frequency=54.0, amplitude=3e-6, rate=1024.0, size=4096):
    time = np.arange(size) / rate
    return {"x_v": amplitude * np.cos(2 * np.pi * frequency * time),
            "y_v": amplitude * np.sin(2 * np.pi * frequency * time),
            "sample_rate_hz": rate, "validity": True}


def summarize(capture, **changes):
    parameters = dict(internal_frequency_hz=50000.0, harmonic=2,
                      time_constant_s=0.001, pem_frequency_hz=50027.0)
    parameters.update(changes)
    return summarize_capture(capture, **parameters)


class PemInternalAnalysisTests(unittest.TestCase):
    def test_rotating_xy_retains_amplitude_even_when_complex_mean_cancels(self):
        capture = rotating_capture()
        before = copy.deepcopy(capture)
        result = summarize(capture)
        self.assertAlmostEqual(result["r_mean_v"], 3e-6, places=15)
        self.assertLess(result["complex_mean_magnitude_v"], 1e-12)
        self.assertAlmostEqual(result["r_sample_sd_v"], 0.0, places=15)
        self.assertEqual(result["fft_peak_absolute_hz"], 54.0)
        self.assertEqual(result["expected_absolute_beat_hz"], 54.0)
        self.assertTrue(result["valid_for_diagnostic_analysis"])
        self.assertFalse(result["formal_hall_eligible"])
        np.testing.assert_array_equal(capture["x_v"], before["x_v"])

    def test_negative_and_non_bin_centered_beat_peak_uses_actual_rate(self):
        result = summarize(rotating_capture(frequency=-53.91))
        self.assertLessEqual(abs(result["fft_peak_signed_hz"] + 53.91), result["fft_bin_width_hz"] / 2)
        self.assertEqual(result["sample_rate_hz"], 1024.0)
        self.assertEqual(result["time_axis_source"], "sample_index / hardware_sample_rate_hz")
        self.assertEqual(result["expected_beat_candidates_hz"], [-54.0, 54.0])

    def test_dc_and_zero_capture_do_not_invent_a_beat(self):
        result = summarize(rotating_capture(frequency=0), pem_frequency_hz=50000.0)
        self.assertEqual(result["fft_peak_signed_hz"], 0)
        self.assertEqual(result["ordinary_rc_amplitude_retention"], 1)
        result = summarize(rotating_capture(amplitude=0), pem_frequency_hz=None)
        self.assertIsNone(result["fft_peak_signed_hz"])
        self.assertIsNone(result["expected_absolute_beat_hz"])

    def test_noise_r_bias_is_disclosed_without_subtraction_or_filter_inverse(self):
        generator = np.random.default_rng(74)
        capture = {"x_v": generator.normal(0, 1e-6, 10000),
                   "y_v": generator.normal(0, 1e-6, 10000),
                   "sample_rate_hz": 1024.0, "validity": True}
        result = summarize(capture, time_constant_s=1.0)
        self.assertGreater(result["r_mean_v"], 1e-6)
        self.assertLess(result["complex_mean_magnitude_v"], 5e-8)
        self.assertTrue(result["r_has_positive_noise_bias"])
        self.assertFalse(result["filter_correction_applied"])
        self.assertLess(result["ordinary_rc_amplitude_retention"], 1e-9)

    def test_recorded_invalid_power_and_status_reasons_are_preserved(self):
        capture = rotating_capture()
        capture.update(validity=False, exclusion_reasons=["input_overload", "power_drift"], errors=["power_drift"])
        result = summarize(capture)
        self.assertEqual(result["exclusion_reasons"], ["recorded_invalid", "input_overload", "power_drift"])
        self.assertFalse(result["valid_for_diagnostic_analysis"])
        self.assertIsNotNone(result["r_mean_v"])
        capture.pop("validity")
        self.assertIn("hardware_quality_not_recorded", summarize(capture)["exclusion_reasons"])

    def test_missing_nonfinite_shapes_and_boolean_samples_do_not_qualify(self):
        for changed in (
            {"x_v": [1.0]}, {"x_v": [float("nan")] * 4096},
            {"y_v": [float("inf")] * 4096}, {"x_v": [[1.0]]},
            {"x_v": [True] * 4096}, {"y_v": None}, {"x_v": "voltage"},
            {"x_v": np.ones(4096, dtype=bool)}, {"x_v": np.ones(4096, dtype=complex)},
            {"x_v": ["1"] * 4096},
        ):
            with self.subTest(changed=str(changed)[:60]):
                capture = rotating_capture()
                capture.update(changed)
                result = summarize(capture)
                self.assertFalse(result["valid_for_diagnostic_analysis"])
                self.assertIn("missing_or_invalid_capture_arrays", result["exclusion_reasons"])

    def test_missing_bad_rate_and_nyquist_equality_are_excluded(self):
        for rate in (None, True, 0, -1, float("nan")):
            capture = rotating_capture()
            capture["sample_rate_hz"] = rate
            self.assertIn("missing_or_invalid_actual_sample_rate", summarize(capture)["exclusion_reasons"])
        result = summarize(rotating_capture(rate=108.0))
        self.assertIn("expected_beat_at_or_above_nyquist", result["exclusion_reasons"])
        self.assertFalse(result["valid_for_diagnostic_analysis"])

    def test_invalid_analysis_parameters_are_rejected(self):
        for change in ({"internal_frequency_hz": True}, {"harmonic": True}, {"harmonic": 100},
                       {"time_constant_s": 0}, {"pem_frequency_hz": float("nan")}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                summarize(rotating_capture(), **change)

    def test_finite_but_overflowing_statistics_are_not_json_nan(self):
        capture = rotating_capture()
        capture["x_v"] = np.full(4096, 1e308)
        result = summarize(capture)
        self.assertIn("capture_statistics_overflow", result["exclusion_reasons"])
        self.assertIsNone(result["r_mean_v"])
        self.assertFalse(result["valid_for_diagnostic_analysis"])

    def test_theory_four_rc_poles_and_extreme_argument(self):
        self.assertAlmostEqual(rc_amplitude_retention(27.0, 0.001), 0.9448332573452304)
        self.assertAlmostEqual(rc_amplitude_retention(-54.0, 0.001), (1 + (2 * math.pi * 54 * 0.001) ** 2) ** -2)
        self.assertEqual(rc_amplitude_retention(1e308, 1e308), 0)


if __name__ == "__main__":
    unittest.main()
