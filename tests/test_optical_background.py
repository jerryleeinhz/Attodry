"""Pure matched dark subtraction, context lifetime and quality regressions."""
from copy import deepcopy
import json
import math
import unittest

from optical_helpers import Clock
from attodry_control.nkt_config import NktError
from attodry_control.optical_background import (BackgroundConfig, MatchedLockinBackground,
                                                load_background_config)


def reading(x=1.0, y=0.0, *, clean=True, extra=False):
    values = {}
    channels = (("xx", 1), ("xy", 1)) + ((("xy", 2),) if extra else ())
    for role, harmonic in channels:
        prefix = f"lockin_{role}_h{harmonic}_"
        values.update({prefix + "x_v": x, prefix + "y_v": y,
            prefix + "amplitude_v": math.hypot(x, y),
            prefix + "phase_deg": math.degrees(math.atan2(y, x))})
    return {"module": "lockin", "actual": {"lockin_frequency_hz": 1000},
        "measurements": values, "clean": clean, "valid_for_analysis": clean,
        "problems": [] if clean else ["reference unlock"],
        "status": {"samples": [{"harmonic": 1,
            "valid_for_analysis_by_role": {"lockin_xx": clean, "lockin_xy": clean},
            "problems_by_role": {"lockin_xx": [], "lockin_xy": []}}]}}


class BackgroundTests(unittest.TestCase):
    def make(self, *, samples=2):
        clock = Clock()
        background = MatchedLockinBackground(BackgroundConfig(True, samples, 10), clock=clock)
        context = {"requested": {"gate_v": 2, "wavelength_nm": 630,
                                  "temperature_k": 4, "field_t": [0, 0]},
            "reference": {"topology": "internal_xx_xy", "frequency_hz": 1000,
                          "harmonics": {"xx": [1], "xy": [1]}, "phase_epoch": 1},
            "visit_index": 0}
        return background, clock, context

    def test_config_is_optional_but_strict_even_when_disabled(self):
        self.assertEqual(load_background_config({}), BackgroundConfig())
        self.assertEqual(load_background_config({"optical_background": {"enabled": True}}).samples, 3)
        for table in ({"enabld": True}, {"enabled": "true"}, {"samples": 0},
                      {"samples": True}, {"max_age_s": 0}, {"max_age_s": float("inf")}):
            with self.subTest(table=table), self.assertRaises(NktError):
                load_background_config({"optical_background": table})

    def test_difference_uses_complex_components_and_keeps_original_light(self):
        background, _, context = self.make()
        baseline = background.capture([reading(1, 0), reading(1, 0)], context)
        light = reading(0, 1)
        original = deepcopy(light)
        result = background.apply(light, context)
        self.assertEqual(light, original)
        self.assertEqual(result["measurements"]["lockin_xx_h1_amplitude_v"], 1)
        self.assertEqual(result["measurements"]["lockin_xx_h1_delta_x_v"], -1)
        self.assertEqual(result["measurements"]["lockin_xx_h1_delta_y_v"], 1)
        self.assertAlmostEqual(result["measurements"]["lockin_xx_h1_delta_amplitude_v"], math.sqrt(2))
        self.assertAlmostEqual(result["measurements"]["lockin_xx_h1_delta_phase_deg"], 135)
        self.assertEqual(result["background"]["baseline_id"], baseline["baseline_id"])
        self.assertTrue(result["background"]["valid_for_analysis"])

    def test_dark_averages_vectors_before_deriving_amplitude_and_phase(self):
        background, _, context = self.make()
        baseline = background.capture([reading(1, 0), reading(-1, 0)], context)
        self.assertEqual(baseline["channels"]["xx:h1"]["amplitude_v"], 0)
        self.assertIsNone(baseline["channels"]["xx:h1"]["phase_deg"])
        self.assertFalse(baseline["channels"]["xx:h1"]["phase_defined"])
        self.assertEqual(baseline["channels"]["xx:h1"]["samples"][0]["amplitude_v"], 1)
        self.assertEqual(len(baseline["raw_readings"]), 2)

    def test_exact_zero_difference_has_no_invented_phase_or_nan(self):
        background, _, context = self.make()
        background.capture([reading(), reading()], context)
        result = background.apply(reading(), context)
        self.assertEqual(result["measurements"]["lockin_xy_h1_delta_amplitude_v"], 0)
        self.assertNotIn("lockin_xy_h1_delta_phase_deg", result["measurements"])
        self.assertFalse(result["background"]["channels"]["xy:h1"]["phase_defined"])
        json.dumps(result, allow_nan=False)

    def test_gate_environment_frequency_phase_and_visit_changes_do_not_reuse(self):
        background, _, context = self.make()
        background.capture([reading(), reading()], context)
        changes = [deepcopy(context) for _ in range(5)]
        changes[0]["requested"]["gate_v"] = 3
        changes[1]["requested"]["temperature_k"] = 5
        changes[2]["reference"]["frequency_hz"] = 1001
        changes[3]["reference"]["phase_epoch"] = 2
        changes[4]["visit_index"] = 1
        for changed in changes:
            with self.subTest(context=changed), self.assertRaisesRegex(NktError, "context mismatch"):
                background.apply(reading(), changed)

    def test_age_boundary_and_clock_reversal(self):
        background, clock, context = self.make()
        background.capture([reading(), reading()], context)
        clock.value = 10
        self.assertEqual(background.apply(reading(), context)["background"]["age_s"], 10)
        for when in (10.001, -0.001):
            clock.value = when
            with self.subTest(when=when), self.assertRaisesRegex(NktError, "expired|reversed"):
                background.apply(reading(), context)

    def test_invalid_dark_or_light_cannot_promote_a_difference(self):
        for invalid_dark in (True, False):
            background, _, context = self.make()
            baseline = background.capture([reading(clean=not invalid_dark), reading()], context)
            result = background.apply(reading(clean=invalid_dark), context)
            self.assertEqual(result["clean"], invalid_dark)  # original light quality is unchanged
            self.assertEqual(result["valid_for_analysis"], invalid_dark)
            self.assertFalse(result["background"]["valid_for_analysis"])
            self.assertFalse(any("delta_" in key for key in result["measurements"]))
            self.assertEqual(baseline["raw_readings"][0]["valid_for_analysis"], not invalid_dark)

    def test_precise_role_invalidity_is_retained_even_if_global_flags_claim_clean(self):
        background, _, context = self.make()
        dark = reading()
        dark["status"]["samples"][0]["valid_for_analysis_by_role"]["lockin_xy"] = False
        dark["status"]["samples"][0]["problems_by_role"]["lockin_xy"] = ["input overload"]
        baseline = background.capture([dark, reading()], context)
        self.assertFalse(baseline["channels"]["xy:h1"]["valid_for_analysis"])
        self.assertTrue(baseline["channels"]["xx:h1"]["valid_for_analysis"])
        self.assertEqual(baseline["channels"]["xy:h1"]["samples"][0]["role_evidence"][0]["problems"],
                         ["input overload"])
        result = background.apply(reading(), context)
        self.assertTrue(result["valid_for_analysis"])
        self.assertIn("lockin_xx_h1_delta_x_v", result["measurements"])
        self.assertNotIn("lockin_xy_h1_delta_x_v", result["measurements"])

    def test_mixed_harmonic_raw_quality_uses_each_selected_role(self):
        background, _, context = self.make()
        dark = reading(extra=True)
        dark["status"]["samples"] = [{"selected_harmonics": {"xx": 1, "xy": 2},
            "valid_for_analysis_by_role": {"lockin_xx": True, "lockin_xy": False},
            "problems_by_role": {"lockin_xy": ["reference unlock"]}}]
        baseline = background.capture([dark, reading(extra=True)], context)
        self.assertFalse(baseline["channels"]["xy:h2"]["valid_for_analysis"])
        self.assertTrue(baseline["channels"]["xy:h1"]["valid_for_analysis"])

    def test_missing_channels_or_metrics_and_failed_capture_invalidate_old_baseline(self):
        background, _, context = self.make()
        background.capture([reading(), reading()], context)
        with self.assertRaisesRegex(NktError, "channel mismatch"):
            background.apply(reading(extra=True), context)
        missing = reading()
        del missing["measurements"]["lockin_xy_h1_y_v"]
        with self.assertRaisesRegex(NktError, "Missing"):
            background.capture([missing, reading()], context)
        self.assertIsNone(background.baseline)
        with self.assertRaisesRegex(NktError, "No matched dark"):
            background.apply(reading(), context)

    def test_raw_capture_context_and_return_values_are_independent_copies(self):
        background, _, context = self.make()
        dark = reading()
        result = background.capture([dark, reading()], context)
        result["channels"]["xx:h1"]["x_v"] = 99
        dark["measurements"]["lockin_xx_h1_x_v"] = 99
        context["requested"]["gate_v"] = 99
        self.assertEqual(background.baseline["channels"]["xx:h1"]["x_v"], 1)
        self.assertEqual(background.baseline["raw_readings"][0]["measurements"]["lockin_xx_h1_x_v"], 1)
        self.assertEqual(background.baseline["context"]["requested"]["gate_v"], 2)

    def test_disabled_mode_noops_and_reference_restart_can_invalidate(self):
        disabled = MatchedLockinBackground(BackgroundConfig())
        light = reading()
        self.assertEqual(disabled.apply(light, {}), light)
        with self.assertRaisesRegex(NktError, "disabled"):
            disabled.capture([light], {})
        background, _, context = self.make()
        background.capture([reading(), reading()], context)
        background.invalidate("chopper reference relocked")
        with self.assertRaisesRegex(NktError, "chopper reference relocked"):
            background.apply(reading(), context)

    def test_incomplete_nonfinite_and_already_corrected_inputs_fail_clearly(self):
        background, _, context = self.make()
        with self.assertRaisesRegex(NktError, "exactly"):
            background.capture([reading()], context)
        for bad_context in ({}, {"gate": float("nan")}, {"context": object()}):
            with self.subTest(context=bad_context), self.assertRaises(NktError):
                background.capture([reading(), reading()], bad_context)
        bad = reading()
        bad["measurements"]["lockin_xx_h1_x_v"] = float("nan")
        with self.assertRaisesRegex(NktError, "finite"):
            background.capture([bad, reading()], context)
        background.capture([reading(), reading()], context)
        corrected = background.apply(reading(), context)
        with self.assertRaisesRegex(NktError, "unsubtracted"):
            background.apply(corrected, context)


if __name__ == "__main__":
    unittest.main()
