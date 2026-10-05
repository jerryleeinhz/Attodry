import copy
import unittest

from attodry_control.analysis_observations import channel_quality, qualify_observations, repeat_statistics


XX1 = "measured.lockin_xx_h1_amplitude_v"
XX3 = "measured.lockin_xx_h3_amplitude_v"
XY2 = "measured.lockin_xy_h2_amplitude_v"


def observation():
    def reading(role, harmonic, model):
        return {"role": role, "model": model, "harmonic": harmonic,
            "x_v": 0.0001, "y_v": 0.0002, "amplitude_v": 0.00022360679775,
            "phase_deg": 63.4349488, "status": {"locked": True, "input_overload": False,
                "output_scale_overload": False, "instrument_error": False,
                "validity": True, "observation": "instantaneous_and_latched",
                "native_status": {"lia_status_raw": 0}}}

    def entry(selected, xx_harmonic):
        readings = {"xx": reading("xx", xx_harmonic, "SR865A"), "xy": reading("xy", 2, "SR830")}
        settings = {role: {"role": role, "model": sample["model"], "sensitivity_full_scale_v": 0.001}
                    for role, sample in readings.items()}
        return {"selected_harmonics": selected, "samples": readings,
                "settings_before": copy.deepcopy(settings), "settings_after": copy.deepcopy(settings)}

    entries = [entry({"xx": 1, "xy": 2}, 1), entry({"xx": 3}, 3)]
    row = {"accepted": True, "condition_id": "point-0", "sample_index": 0,
           "actual.lockin_excitation_v_rms": 0.004,
           "status.lockin": {"schema_version": "photonics-lockin-v1", "samples": entries}}
    for item in entries:
        for role, harmonic in item["selected_harmonics"].items():
            for metric in ("x_v", "y_v", "amplitude_v", "phase_deg"):
                row[f"measured.lockin_{role}_h{harmonic}_{metric}"] = item["samples"][role][metric]
    return row


class PhotonicsAnalysisTests(unittest.TestCase):
    def test_mixed_model_selected_harmonic_is_clear_without_mutating_record(self):
        row = observation()
        before = copy.deepcopy(row)
        for column in (XX1, XX3, XY2):
            self.assertEqual(channel_quality(row, column), ("clear", ()))
        self.assertEqual(row, before)

    def test_unselected_companion_and_transition_status_do_not_qualify_channel(self):
        row = observation()
        row["status.lockin"]["samples"][1]["samples"]["xy"]["status"].update(
            input_overload=True, validity=False)
        row["status.lockin"]["transition_probes"] = [{"samples": {"xy": {"status": {"validity": False}}}}]
        self.assertEqual(channel_quality(row, XY2), ("clear", ()))
        row["status.lockin"]["samples"][0]["selected_harmonics"].pop("xy")
        self.assertEqual(channel_quality(row, XY2), ("flagged", ("not_selected_channel",)))

    def test_role_local_normalized_flags_do_not_reinterpret_vendor_bits(self):
        row = observation()
        status = row["status.lockin"]["samples"][0]["samples"]["xx"]["status"]
        # Vendor words have already been decoded when this schema was written.
        status["native_status"] = {"lia_status_raw": 255, "current_status_raw": 255, "raw": 255}
        self.assertEqual(channel_quality(row, XX1), ("clear", ()))
        for field in ("input_overload", "output_scale_overload", "instrument_error"):
            changed = copy.deepcopy(row)
            changed["status.lockin"]["samples"][0]["samples"]["xx"]["status"][field] = True
            self.assertIn(field, channel_quality(changed, XX1)[1])
            self.assertEqual(channel_quality(changed, XY2), ("clear", ()))

    def test_lock_loss_and_recorded_invalid_do_not_become_clear(self):
        row = observation()
        state = row["status.lockin"]["samples"][0]["samples"]["xx"]["status"]
        state.update(locked=False, validity=False)
        self.assertEqual(set(channel_quality(row, XX1)[1]), {"reference_unlocked", "recorded_invalid_for_analysis"})

    def test_unknown_validity_or_missing_boolean_stays_unknown(self):
        for field in ("validity", "locked", "input_overload", "output_scale_overload", "instrument_error"):
            row = observation()
            row["status.lockin"]["samples"][0]["samples"]["xx"]["status"][field] = None
            self.assertEqual(channel_quality(row, XX1), ("unknown", ()))
            kept, report = qualify_observations([row], [XX1])
            self.assertEqual(len(kept), 1)
            self.assertEqual(report["unknown_status_count"], 1)

    def test_full_scale_is_physical_voltage_and_checks_both_brackets(self):
        for stage in ("settings_before", "settings_after"):
            row = observation()
            row["status.lockin"]["samples"][0][stage]["xx"]["sensitivity_full_scale_v"] = 0.0002
            quality, issues = channel_quality(row, XX1)
            self.assertEqual(quality, "flagged")
            self.assertIn("exceeds_full_scale", issues)
            self.assertIn("settings_changed", issues)
            self.assertEqual(channel_quality(row, XY2), ("clear", ()))

    def test_irng_input_peak_does_not_replace_output_full_scale(self):
        row = observation()
        for stage in ("settings_before", "settings_after"):
            row["status.lockin"]["samples"][0][stage]["xx"]["input_range_v_peak"] = 1e-6
        self.assertEqual(channel_quality(row, XX1), ("clear", ()))

    def test_mismatched_harmonic_or_settings_identity_is_flagged(self):
        row = observation()
        row["status.lockin"]["samples"][0]["samples"]["xx"]["harmonic"] = 3
        self.assertIn("harmonic_readback_mismatch", channel_quality(row, XX1)[1])
        row = observation()
        row["status.lockin"]["samples"][0]["settings_after"]["xx"]["model"] = "SR830"
        self.assertIn("settings_identity_mismatch", channel_quality(row, XX1)[1])

    def test_zero_xy_has_valid_voltage_and_unknown_missing_phase(self):
        row = observation()
        reading = row["status.lockin"]["samples"][0]["samples"]["xx"]
        for field in ("x_v", "y_v", "amplitude_v"):
            reading[field] = 0.0
            row[f"measured.lockin_xx_h1_{field}"] = 0.0
        reading["phase_deg"] = None
        phase_column = "measured.lockin_xx_h1_phase_deg"
        row.pop(phase_column)
        self.assertEqual(channel_quality(row, XX1), ("clear", ()))
        self.assertEqual(channel_quality(row, phase_column), ("unknown", ()))
        result, = repeat_statistics([row], x="actual.lockin_excitation_v_rms", y=phase_column)
        self.assertIsNone(result["y"])
        self.assertEqual(result["n"], 0)
        row[phase_column] = 0.0
        self.assertIn("undefined_phase", channel_quality(row, phase_column)[1])

    def test_missing_bracket_is_unknown_and_cannot_borrow_other_harmonic(self):
        row = observation()
        del row["status.lockin"]["samples"][0]["settings_after"]
        self.assertEqual(channel_quality(row, XX1), ("unknown", ()))
        self.assertEqual(channel_quality(row, XX3), ("clear", ()))

    def test_channel_filter_reports_exclusion_without_changing_outer_acceptance(self):
        row = observation()
        row["accepted"] = False
        self.assertEqual(channel_quality(row, XX1), ("clear", ()))
        self.assertFalse(row["accepted"])
        row["status.lockin"]["samples"][0]["samples"]["xx"]["status"]["input_overload"] = True
        kept, report = qualify_observations([row], [XX1])
        self.assertEqual(kept, ())
        self.assertEqual(report["excluded_quality_count"], 1)
        self.assertFalse(row["accepted"])

    def test_unknown_model_metadata_and_boolean_harmonic_are_not_clear(self):
        row = observation()
        row["status.lockin"]["samples"][0]["samples"]["xx"]["model"] = None
        self.assertNotEqual(channel_quality(row, XX1)[0], "clear")
        row = observation()
        row["status.lockin"]["samples"][0]["selected_harmonics"]["xx"] = True
        self.assertIn("invalid_harmonic_selection", channel_quality(row, XX1)[1])


if __name__ == "__main__":
    unittest.main()
