"""Read-only power-reference arithmetic, matching and covariance contracts."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math
import unittest

from attodry_control.optical_power_reference import (
    subtract_power_reference, power_reference_mean_variance, power_reference_statistics,
)


def make_rows(powers=(1e-6, 6e-6), repeats=2, *, schema="legacy"):
    """Small normalized XX/XY/AUX fixture with complete archived visit evidence."""
    plan = {"loop_order": ["temperature", "optical", "lockin"],
            "feedback": {"actuator": "cld_current", "target_mapping": "paired"},
            "axes": [{"module": "optical", "points": [
                {"values": {"optical_target_power_w": p, "optical_current_a": .02 + i * .001,
                            "optical_wavelength_nm": 730.}, "segment": "main", "direction": "ordered"}
                for i, p in enumerate(powers)]}]}
    context = {"power_reference_plan": plan,
               "lockin": {"lockin_" + role: {"model": "SR830" if role != "aux" else "SR865A",
                    "reference_source": "internal" if role == "xx" else "external", "phase_shift_deg": 0.}
                    for role in ("xx", "xy", "aux")}, "optical": {"pem": None, "chopper": None}}
    rows = []
    for index, power in enumerate(powers):
        for sample_index in range(repeats):
            stamp = (datetime(2026, 10, 23, tzinfo=timezone.utc) + timedelta(seconds=10 * index + sample_index)).isoformat()
            row = {"source_path": "fixture.sqlite", "run_id": "power-run", "condition_id": f"condition-{index}",
                   "sample_index": sample_index, "attempt_index": 0, "repeat_index": 0, "accepted": True,
                   "simulated": False, "loop_order": plan["loop_order"], "analysis_context": context,
                   "axes.temperature.index": 0, "axes.temperature.segment": "main", "axes.temperature.direction": "ordered",
                   "axes.optical.index": index, "axes.optical.segment": "main", "axes.optical.direction": "ordered",
                   "axes.lockin.index": 0, "axes.lockin.segment": "main", "axes.lockin.direction": "ordered",
                   "requested.temperature_k": 2., "requested.optical_target_power_w": power,
                   "requested.optical_current_a": .02 + index * .001, "requested.optical_wavelength_nm": 730.,
                   "requested.lockin_frequency_hz": 8., "requested.lockin_excitation_v_rms": .004,
                   "measured.optical_power_w": power * 1.01, "finished_at_utc": stamp, "timestamps.lockin": stamp,
                   "status.optical": {"readback": {"nkt": {"source": {"getLdOutputState": True}}}},
                   "status.lockin": {"samples": []}}
            setting = {}
            for role in ("xx", "xy", "aux"):
                x, y = .01 * (index + 1) + sample_index * .002, .002 * index
                prefix = "measured.lockin_" + role + "_h1_"
                row.update({prefix + "x_v": x, prefix + "y_v": y, prefix + "amplitude_v": math.hypot(x, y),
                            prefix + "phase_deg": math.degrees(math.atan2(y, x))})
                setting[role] = {"role": role, "model": "SR830" if role != "aux" else "SR865A",
                    "identity": "fixture-" + role, "harmonic": 1, "time_constant_s": 1.,
                    "reference_source": "internal" if role == "xx" else "external",
                    "sensitivity_full_scale_v": .1 * (index + 1), "input_range_v_peak": .3 if role == "aux" else None,
                    "native_settings": {"phase_shift_deg": 0., "filter_slope": 3, "input_mode": 1, "input_coupling": 1,
                                        "sensitivity": index + 1, "sine_output_v": .004,
                                        "shield_grounding": 0, "reference_mode": 1 if role == "xx" else 0,
                                        "reference_slope": 1, "line_filter": 0, "reserve_mode": 1, "time_constant": 10,
                                        "x_v": x, "y_v": y, "phase_deg": sample_index * .1}}
                if role == "aux":
                    setting[role]["native_settings"] = {
                        "reference_source": "external", "external_reference_edge": "rising",
                        "reference_input_impedance_ohm": 1e6, "harmonic": 1, "time_constant_s": 1.,
                        "sensitivity_full_scale_v": .1 * (index + 1), "input_range_v_peak": .3,
                        "filter_slope_db_oct": 24, "advanced_filter": False, "synchronous_filter": False,
                        "input_mode": "a_minus_b", "input_coupling": "ac", "shield_grounding": "float",
                        "phase_shift_deg": 0., "sine_output_v": .004, "source_offset_v": 0.,
                        "source_dc_mode": "common", "sync_output_mode": "blazex"}
            if schema == "photonics":
                formal = {"selected_harmonics": {role: 1 for role in setting},
                          "settings_before": deepcopy(setting), "settings_after": deepcopy(setting), "settings_verified": True,
                          "reference_topology": "internal_xx_xy", "samples": {}}
                for role in setting:
                    prefix = "measured.lockin_" + role + "_h1_"
                    formal["samples"][role] = {"role": role, "harmonic": 1, "model": setting[role]["model"],
                        **{metric: row[prefix + metric] for metric in ("x_v", "y_v", "amplitude_v", "phase_deg")},
                        "status": {"validity": True, "locked": True, "input_overload": False,
                                   "output_scale_overload": False, "instrument_error": False,
                                   "observation": "instantaneous_and_latched"}}
                row["status.lockin"]["schema_version"] = "photonics-lockin-v1"
            else:
                formal = {"harmonic": 1, "selected_roles": list(setting), "settings_verified": True,
                          "settings_before": {"roles": {"lockin_" + k: deepcopy(v) for k, v in setting.items()}},
                          "settings_after": {"roles": {"lockin_" + k: deepcopy(v) for k, v in setting.items()}}}
                for role in setting:
                    formal["lockin_" + role] = {"reading": {"harmonic": 1}, "lia_status": {"raw": 0}, "error_status": 0}
            row["status.lockin"]["samples"] = [formal]
            rows.append(row)
    return rows


def channel(row, role="xx"):
    return row["status.lockin"]["power_reference"]["channels"][role + ":h1"]


def change_setting(row, key, value, role="xx"):
    sample = row["status.lockin"]["samples"][0]
    for stage in ("settings_before", "settings_after"):
        settings = sample[stage][role] if "selected_harmonics" in sample else sample[stage]["roles"]["lockin_" + role]
        settings["native_settings"][key] = value


class PowerReferenceTests(unittest.TestCase):
    def test_both_schemas_complex_subtraction_range_difference_and_no_mutation(self):
        for schema in ("legacy", "photonics"):
            with self.subTest(schema=schema):
                rows = make_rows(schema=schema)
                original = deepcopy(rows)
                output = subtract_power_reference(rows)
                self.assertEqual(rows, original)
                self.assertIs(output[0]["analysis_context"], rows[0]["analysis_context"])
                self.assertIsNot(output[0]["status.lockin"], rows[0]["status.lockin"])
                value = output[2]
                self.assertAlmostEqual(value["measured.lockin_xx_h1_power_delta_x_v"], .009)
                self.assertAlmostEqual(value["measured.lockin_xx_h1_power_delta_y_v"], .002)
                self.assertAlmostEqual(value["measured.lockin_xx_h1_power_delta_amplitude_v"], math.hypot(.009, .002))
                self.assertTrue(channel(value)["valid_for_analysis"])
                self.assertEqual(channel(value)["reference_count"], 2)
                self.assertEqual(channel(value)["target_full_scales_v"], [.2])
                self.assertEqual(channel(value)["reference_full_scales_v"][0]["full_scales_v"], [.1])

    def test_selected_power_and_minimum_never_uses_measured_signal(self):
        rows = make_rows()
        rows[0]["measured.optical_power_w"] = 99.
        out = subtract_power_reference(rows, mode="power_selected", reference_power_w=6e-6)
        self.assertAlmostEqual(out[0]["measured.lockin_xx_h1_power_delta_x_v"], -.011)
        self.assertEqual(channel(subtract_power_reference(rows)[-1])["reference_power_w"], 1e-6)

    def test_missing_lowest_does_not_fall_back_after_filtering(self):
        out = subtract_power_reference(make_rows()[2:])
        self.assertFalse(channel(out[0])["valid_for_analysis"])
        self.assertIn("No qualified reference", channel(out[0])["reason"])
        self.assertEqual(channel(out[0])["reference_power_w"], 1e-6)

    def test_missing_plan_requires_selected_and_legacy_visit_identity(self):
        rows = make_rows()
        for row in rows:
            row["analysis_context"] = deepcopy(row["analysis_context"])
            del row["analysis_context"]["power_reference_plan"]
            row["requested.optical_current_a"] = .02
        self.assertIn("archived optical plan", channel(subtract_power_reference(rows)[0])["reason"])
        self.assertTrue(channel(subtract_power_reference(rows, mode="power_selected", reference_power_w=1e-6)[2])["valid_for_analysis"])
        del rows[-1]["axes.temperature.index"]
        self.assertIn("visit index", channel(subtract_power_reference(rows, mode="power_selected", reference_power_w=1e-6)[-1])["reason"])

    def test_channel_failure_independent_and_manual_exclusion_audited(self):
        rows = make_rows(schema="photonics")
        rows[0]["status.lockin"]["samples"][0]["samples"]["xx"]["status"]["input_overload"] = True
        rows[1]["_manual_excluded"] = True
        out = subtract_power_reference(rows)
        self.assertFalse(channel(out[2])["valid_for_analysis"])
        self.assertTrue(channel(out[2], "xy")["valid_for_analysis"])
        self.assertEqual(channel(out[2], "xy")["reference_count"], 1)
        self.assertEqual(len(channel(out[2])["reference_rejections"]), 2)

    def test_missing_phase_settings_or_formal_setting_change_blocks_only_role(self):
        for change in ("phase", "changed", "unverified", "unknown_status"):
            rows = make_rows()
            if change == "phase":
                change_setting(rows[-1], "phase_shift_deg", 1.)
            elif change == "changed":
                rows[-1]["status.lockin"]["samples"][0]["settings_after"]["roles"]["lockin_xx"]["harmonic"] = 2
            elif change == "unverified":
                rows[-1]["status.lockin"]["samples"][0]["settings_verified"] = False
            else:
                rows[-1]["status.lockin"]["samples"][0]["lockin_xx"]["lia_status"] = {}
            out = subtract_power_reference(rows)
            self.assertFalse(channel(out[-1])["valid_for_analysis"], change)
            if change != "unverified":
                self.assertTrue(channel(out[-1], "xy")["valid_for_analysis"], change)

    def test_outer_coordinates_and_revisit_indices_never_mix(self):
        for key, value in (("source_path", "other.sqlite"), ("run_id", "other"),
                           ("requested.temperature_k", 20.), ("axes.temperature.index", 1),
                           ("repeat_index", 1), ("requested.lockin_frequency_hz", 9.)):
            rows = make_rows()
            for row in rows[2:]:
                row[key] = value
            self.assertFalse(channel(subtract_power_reference(rows)[2])["valid_for_analysis"], key)

    def test_reference_by_spectral_slice_and_both_loop_orders(self):
        rows = make_rows(powers=(1e-6, 6e-6, 2e-6, 8e-6))
        plan = rows[0]["analysis_context"]["power_reference_plan"]
        for point in plan["axes"][0]["points"][2:]:
            point["values"]["optical_wavelength_nm"] = 800.
        for row in rows[4:]:
            row["requested.optical_wavelength_nm"] = 800.
        plan["loop_order"] = ["optical", "temperature", "lockin"]
        self.assertEqual(channel(subtract_power_reference(rows)[-1])["reference_power_w"], 2e-6)

    def test_unknown_seed_control_not_exempted_and_actual_nd_kept(self):
        rows = make_rows()
        rows[0]["analysis_context"]["power_reference_plan"]["feedback"]["actuator"] = "unknown"
        self.assertFalse(channel(subtract_power_reference(rows)[-1])["valid_for_analysis"])
        rows = make_rows()
        for row in rows[2:]: row["requested.optical_nd_pct"] = 50.
        self.assertFalse(channel(subtract_power_reference(rows)[-1])["valid_for_analysis"])

    def test_duplicate_nominal_reference_visits_are_ambiguous(self):
        rows = make_rows(powers=(1e-6, 1e-6, 6e-6))
        out = subtract_power_reference(rows)
        self.assertIn("Ambiguous reference", channel(out[-1])["reason"])
        # A missing entire duplicate visit cannot silently disambiguate it.
        out = subtract_power_reference(rows[2:])
        self.assertIn("Ambiguous reference", channel(out[-1])["reason"])
        out = subtract_power_reference(rows[2:], mode="power_selected", reference_power_w=1e-6)
        self.assertIn("Ambiguous reference", channel(out[-1])["reason"])

    def test_duplicate_sample_identity_does_not_undercount_covariance(self):
        rows = make_rows()
        rows.insert(0, deepcopy(rows[0]))
        self.assertIn("Duplicate reference", channel(subtract_power_reference(rows)[-1])["reason"])

    def test_dark_rows_ignored_old_dark_delta_retained_and_stale_power_delta_removed(self):
        rows = make_rows()
        dark = deepcopy(rows[0]); dark["observation_kind"] = "dark"
        rows[-1]["measured.lockin_xx_h1_delta_x_v"] = 99.
        rows[-1]["measured.lockin_xx_h1_power_delta_x_v"] = 999.
        out = subtract_power_reference([dark, *rows])
        self.assertEqual(len(out), 4)
        self.assertEqual(out[-1]["measured.lockin_xx_h1_delta_x_v"], 99.)
        self.assertNotEqual(out[-1]["measured.lockin_xx_h1_power_delta_x_v"], 999.)

    def test_signed_time_distance_preserved_even_when_reference_is_later(self):
        out = subtract_power_reference(make_rows(), mode="power_selected", reference_power_w=6e-6)
        self.assertTrue(channel(out[0])["valid_for_analysis"])
        self.assertLess(channel(out[0])["reference_age_s"], 0)
        self.assertGreater(channel(out[0])["reference_max_distance_s"], 0)

    def test_bad_time_or_plan_mismatch_invalid_without_exception(self):
        for bad in (None, "bad", "2026-10-23T00:00:00"):
            rows = make_rows(); rows[0]["timestamps.lockin"] = bad
            self.assertFalse(channel(subtract_power_reference(rows)[-1])["valid_for_analysis"])
        rows = make_rows(); rows[-1]["requested.optical_current_a"] = .099
        self.assertIn("disagree", channel(subtract_power_reference(rows)[-1])["reason"])

    def test_missing_or_sparse_formal_evidence_never_qualifies(self):
        for change in (None, {}, "malformed", {"samples": [{"harmonic": 1, "settings_verified": True,
                "settings_before": {"roles": {"lockin_xx": {"harmonic": 1, "sensitivity_full_scale_v": .1}}},
                "settings_after": {"roles": {"lockin_xx": {"harmonic": 1, "sensitivity_full_scale_v": .1}}}}]}):
            rows = make_rows(); rows[-1]["status.lockin"] = change
            self.assertFalse(channel(subtract_power_reference(rows)[-1])["valid_for_analysis"])

    def test_compact_reference_raw_vectors_are_exportable_from_target_alone(self):
        output = subtract_power_reference(make_rows())
        references = channel(output[-1])["reference_samples"]
        self.assertEqual(references[0]["raw_x_v"], .01)
        self.assertIn("captured_at_utc", references[0])
        self.assertEqual(references[0]["confirmed_full_scales_v"], [.1])
        self.assertEqual(references[0]["sample_id"]["sample_index"], 0)

    def test_null_or_unknown_formal_configuration_is_not_evidence(self):
        for key in ("phase_shift_deg", "input_mode", "input_coupling", "filter_slope", "reference_slope",
                    "shield_grounding", "reserve_mode"):
            rows = make_rows(schema="photonics")
            for row in rows: change_setting(row, key, None)
            self.assertFalse(channel(subtract_power_reference(rows)[-1])["valid_for_analysis"], key)
        for key in ("input_mode", "external_reference_edge", "advanced_filter", "reference_input_impedance_ohm"):
            rows = make_rows(schema="photonics")
            for row in rows: change_setting(row, key, None, role="aux")
            self.assertFalse(channel(subtract_power_reference(rows)[-1], "aux")["valid_for_analysis"], key)
        rows = make_rows(schema="photonics")
        for row in rows:
            for stage in ("settings_before", "settings_after"):
                row["status.lockin"]["samples"][0][stage]["aux"]["input_range_v_peak"] = None
        self.assertFalse(channel(subtract_power_reference(rows)[-1], "aux")["valid_for_analysis"])

    def test_reference_self_full_and_partial_covariance_are_not_independent(self):
        out = subtract_power_reference(make_rows(repeats=3))
        key = "measured.lockin_xx_h1_power_delta_x_v"
        summary = power_reference_statistics(out[:3], key)
        self.assertEqual(summary["mean"], 0.)
        self.assertEqual(summary["sem"], 0.)
        self.assertGreater(summary["sd"], 0.)
        self.assertTrue(summary["reference_self_complete"])
        refvar = channel(out[0])["reference_covariance_mean_xy_v2"][0][0]
        partial = power_reference_statistics(out[:2], key)
        self.assertAlmostEqual(partial["sem"] ** 2, refvar * (3 / 2 - 1))
        self.assertIsNone(power_reference_mean_variance(out[:2], key))

    def test_reference_self_complex_mean_amplitude_zero_phase_undefined(self):
        out = subtract_power_reference(make_rows(repeats=3))
        prefix = "measured.lockin_xx_h1_power_delta_"
        amplitude = power_reference_statistics(out[:3], prefix + "amplitude_v")
        phase = power_reference_statistics(out[:3], prefix + "phase_deg")
        self.assertEqual(amplitude["mean"], 0.)
        self.assertEqual(amplitude["sem"], 0.)
        self.assertIsNone(phase["mean"])
        self.assertIsNone(phase["sem"])

    def test_shared_error_not_divided_by_target_n_and_complex_summary(self):
        out = subtract_power_reference(make_rows())
        key = "measured.lockin_xx_h1_power_delta_x_v"
        refvar = channel(out[-1])["reference_covariance_mean_xy_v2"][0][0]
        self.assertAlmostEqual(power_reference_mean_variance(out[2:], key), refvar)
        summary = power_reference_statistics(out[2:], key)
        self.assertAlmostEqual(summary["sem"] ** 2, refvar * 2)
        magnitude = power_reference_statistics(out[2:], key.replace("x_v", "amplitude_v"))
        self.assertAlmostEqual(magnitude["mean"], math.hypot(.01, .002))

    def test_one_repeat_unknown_error_except_exact_self_identity(self):
        out = subtract_power_reference(make_rows(repeats=1))
        key = "measured.lockin_xx_h1_power_delta_x_v"
        self.assertEqual(power_reference_statistics(out[:1], key)["sem"], 0.)
        self.assertIsNone(power_reference_statistics(out[1:], key)["sem"])

    def test_invalid_api_options(self):
        for kwargs in ({"mode": "raw"}, {"mode": "power_selected"},
                       {"mode": "power_selected", "reference_power_w": -1},
                       {"mode": "power_selected", "reference_power_w": True}):
            with self.assertRaises(ValueError):
                subtract_power_reference(make_rows(), **kwargs)


if __name__ == "__main__":
    unittest.main()
