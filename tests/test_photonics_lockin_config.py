import copy
import unittest

from attodry_control.photonics_lockin_config import load_photonics_lockin_config
from attodry_control.photonics_lockin_points import PhotonicsLockinPointSession
from tests.photonics_lockin_helpers import fixture_document, safety_document


class PhotonicsConfigTests(unittest.TestCase):
    def load(self, document=None, safety=None):
        return load_photonics_lockin_config("fake.toml", document=fixture_document() if document is None else document,
                                           safety_document=safety_document() if safety is None else safety)

    def test_explicit_mixed_pair_physical_units_and_source_provenance(self):
        cfg = self.load()
        self.assertEqual((cfg.lockin_xx.model, cfg.lockin_xy.model), ("SR865A", "SR830"))
        self.assertEqual(cfg.lockin_xx.harmonics, (1, 3))
        self.assertEqual(cfg.lockin_xy.harmonics, (2,))
        self.assertEqual(cfg.source.wiring, "single_ended")
        self.assertEqual(cfg.source.load, "high_impedance")
        self.assertEqual(cfg.settle_s, 0.1)
        self.assertEqual(len(cfg.safety_sha256), 64)

    def test_dual_sr865a_uses_independent_harmonic_selection(self):
        doc = fixture_document()
        doc["lockin_xy"]["model"] = "SR865A"
        del doc["lockin_xy"]["sr830"]
        doc["lockin_xy"]["sr865a"] = copy.deepcopy(doc["lockin_xx"]["sr865a"])
        doc["lockin_xy"]["harmonics"] = [1, 2, 3]
        self.assertEqual(self.load(doc).lockin_xy.harmonics, (1, 2, 3))

    def test_pair_tolerance_above_old_reference_ratio_preserves_configured_hz(self):
        # The reference minimum is 49 kHz, so the removed ratio cap was 49 Hz.
        for tolerance in (100.0, 1000.0, 1000.25):
            doc = fixture_document()
            doc["photonics_lockin"]["pair_tolerance_hz"] = tolerance
            with self.subTest(tolerance=tolerance):
                cfg = self.load(doc)
                self.assertEqual(cfg.pair_tolerance_hz, tolerance)
                self.assertEqual((cfg.reference_min_hz, cfg.reference_max_hz),
                                 (49000.0, 51000.0))

    def test_pair_tolerance_still_requires_a_positive_finite_number(self):
        for tolerance in (0, -1, True, False, float("nan"), float("inf"),
                          -float("inf"), "100", None):
            doc = fixture_document()
            doc["photonics_lockin"]["pair_tolerance_hz"] = tolerance
            with self.subTest(tolerance=tolerance), self.assertRaisesRegex(
                    ValueError, "pair_tolerance_hz"):
                self.load(doc)

    def test_larger_configured_pair_tolerance_still_bounds_runtime_difference(self):
        for tolerance, xx in ((100.0, 50000.0), (1000.0, 49900.0)):
            doc = fixture_document()
            doc["photonics_lockin"]["pair_tolerance_hz"] = tolerance
            cfg = self.load(doc)
            session = PhotonicsLockinPointSession("fake.toml", cfg, lambda *_: None)
            with self.subTest(tolerance=tolerance):
                session._check_frequencies({"xx": xx, "xy": xx + tolerance})
                with self.assertRaisesRegex(ValueError, "external references disagree"):
                    session._check_frequencies({"xx": xx, "xy": xx + tolerance + 0.001})
                with self.assertRaisesRegex(ValueError, "approved interval"):
                    session._check_frequencies({"xx": 49000.0 - 0.001, "xy": 49000.0})

    def test_reference_lock_wait_is_optional_and_bounded(self):
        self.assertEqual(self.load().reference_lock_wait_s, 0.0)
        for seconds in (0, 45.0, 600):
            doc = fixture_document()
            doc["photonics_lockin"]["reference_lock_wait_s"] = seconds
            with self.subTest(seconds=seconds):
                self.assertEqual(self.load(doc).reference_lock_wait_s, seconds)

    def test_overload_policy_defaults_to_abort_and_resolves_explicit_choices(self):
        self.assertEqual(self.load().overload_policy, "abort")
        for policy in ("abort", "record_continue"):
            doc = fixture_document()
            doc["photonics_lockin"]["overload_policy"] = policy
            with self.subTest(policy=policy):
                self.assertEqual(self.load(doc).overload_policy, policy)

    def test_overload_policy_rejects_unknown_or_non_string_values(self):
        for policy in ("continue_unselected", "ignore", "", "RECORD_CONTINUE", True, 1, None, [], {}):
            doc = fixture_document()
            doc["photonics_lockin"]["overload_policy"] = policy
            with self.subTest(policy=policy), self.assertRaisesRegex(ValueError, "photonics_lockin.overload_policy"):
                self.load(doc)

    def test_reference_unlock_policy_defaults_to_abort_and_resolves_choices(self):
        self.assertEqual(self.load().reference_unlock_policy, "abort")
        for policy in ("abort", "record_continue"):
            doc = fixture_document()
            doc["photonics_lockin"]["reference_unlock_policy"] = policy
            with self.subTest(policy=policy):
                self.assertEqual(self.load(doc).reference_unlock_policy, policy)
                self.assertEqual(self.load(doc).overload_policy, "abort")

    def test_reference_unlock_policy_rejects_unknown_or_non_string_values(self):
        for policy in ("continue_unselected", "ignore", "", "RECORD_CONTINUE", True, 1, None, [], {}):
            doc = fixture_document()
            doc["photonics_lockin"]["reference_unlock_policy"] = policy
            with self.subTest(policy=policy), self.assertRaisesRegex(ValueError, "photonics_lockin.reference_unlock_policy"):
                self.load(doc)

    def test_reference_recovery_defaults_preserve_abort_and_resolve_explicit_choices(self):
        cfg = self.load()
        self.assertEqual(cfg.reference_transient_policy, "abort")
        self.assertEqual(cfg.reference_recovery_timeout_s, 45.0)
        self.assertEqual(cfg.reference_recovery_consecutive_good, 3)
        for policy in ("abort", "wait_stable"):
            doc = fixture_document()
            doc["photonics_lockin"].update(reference_transient_policy=policy,
                reference_recovery_timeout_s=12.5, reference_recovery_consecutive_good=5)
            cfg = self.load(doc)
            self.assertEqual(cfg.reference_transient_policy, policy)
            self.assertEqual(cfg.reference_recovery_timeout_s, 12.5)
            self.assertEqual(cfg.reference_recovery_consecutive_good, 5)
            self.assertEqual((cfg.overload_policy, cfg.reference_unlock_policy), ("abort", "abort"))

    def test_reference_transient_policy_accepts_only_explicit_known_choices(self):
        for policy in ("record_continue", "ignore", "", "WAIT_STABLE", True, 1, None, [], {}):
            doc = fixture_document()
            doc["photonics_lockin"]["reference_transient_policy"] = policy
            with self.subTest(policy=policy), self.assertRaisesRegex(ValueError, "reference_transient_policy"):
                self.load(doc)

    def test_recovery_timeout_is_positive_finite_and_bounded_even_under_abort(self):
        for timeout in (0.001, 45, 600):
            doc = fixture_document()
            doc["photonics_lockin"]["reference_recovery_timeout_s"] = timeout
            self.assertEqual(self.load(doc).reference_recovery_timeout_s, timeout)
        for timeout in (0, -1, 600.001, True, False, "45", None, float("nan"), float("inf")):
            doc = fixture_document()
            doc["photonics_lockin"]["reference_recovery_timeout_s"] = timeout
            with self.subTest(timeout=timeout), self.assertRaisesRegex(ValueError, "reference_recovery_timeout_s"):
                self.load(doc)

    def test_recovery_requires_bounded_exact_integer_consecutive_good(self):
        for good in (1, 3, 100):
            doc = fixture_document()
            doc["photonics_lockin"]["reference_recovery_consecutive_good"] = good
            self.assertEqual(self.load(doc).reference_recovery_consecutive_good, good)
        for good in (0, -1, 101, True, False, 3.0, "3", None, float("nan"), float("inf")):
            doc = fixture_document()
            doc["photonics_lockin"]["reference_recovery_consecutive_good"] = good
            with self.subTest(good=good), self.assertRaisesRegex(ValueError, "reference_recovery_consecutive_good"):
                self.load(doc)

    def test_reference_lock_wait_rejects_invalid_values(self):
        for seconds in (-0.001, 600.001, True, "45", float("nan"), float("inf")):
            doc = fixture_document()
            doc["photonics_lockin"]["reference_lock_wait_s"] = seconds
            with self.subTest(seconds=seconds), self.assertRaisesRegex(ValueError, "reference_lock_wait_s"):
                self.load(doc)

    def test_reference_timeout_resolves_new_key_and_rejects_alias_conflict(self):
        for seconds in (0, 45, 600):
            doc = fixture_document()
            doc["photonics_lockin"]["reference_lock_timeout_s"] = seconds
            self.assertEqual(self.load(doc).reference_lock_timeout_s, seconds)
            self.assertEqual(self.load(doc).reference_lock_wait_s, seconds)
        doc["photonics_lockin"]["reference_lock_wait_s"] = 45
        with self.assertRaisesRegex(ValueError, "Use only"):
            self.load(doc)
        for value in (-1, 601, True, "45", float("nan")):
            doc = fixture_document()
            doc["photonics_lockin"]["reference_lock_timeout_s"] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "reference_lock_timeout_s"):
                self.load(doc)

    def test_sr830_h3_is_rejected_at_plan_maximum_reference(self):
        doc = fixture_document()
        doc["lockin_xy"]["harmonics"] = [3]
        with self.assertRaises(ValueError):
            self.load(doc)

    def test_unsupported_model_parameters_never_silently_discarded(self):
        for role, key, value in (
            ("lockin_xx", "sr830", {"reserve_mode": "normal"}),
            ("lockin_xx", "frequency_hz", 50027),
            ("lockin_xy", "sr865a", {"input_range_v_peak": 0.1}),
            ("lockin_xy", "sensitivity_mode", "bounded_auto"),
        ):
            doc = fixture_document()
            doc[role][key] = value
            with self.subTest(role=role, key=key), self.assertRaises(ValueError):
                self.load(doc)

    def test_no_implicit_limits_wiring_or_source_dc(self):
        for key in ("wiring", "load", "amplitude_definition", "dc_mode", "dc_offset_v"):
            doc = fixture_document()
            del doc["photonics_lockin"]["source"][key]
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.load(doc)
        for key in ("minimum_source_voltage_v", "maximum_source_voltage_v", "cleanup_source_voltage_v"):
            policy = safety_document()
            del policy[key]
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.load(safety=policy)

    def test_nonzero_dc_and_unverified_current_status_rejected(self):
        doc = fixture_document()
        doc["photonics_lockin"]["source"]["dc_offset_v"] = 1e-9
        with self.assertRaises(ValueError):
            self.load(doc)
        doc = fixture_document()
        doc["lockin_xx"]["sr865a"]["current_status_supported"] = False
        with self.assertRaises(ValueError):
            self.load(doc)

    def test_hardware_and_explicit_safety_intersection(self):
        for target in (0, 0.02, True, float("nan"), float("inf")):
            doc = fixture_document()
            doc["lockin_sweep"]["excitation_points_v_rms"] = [target]
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.load(doc)
        policy = safety_document()
        policy["maximum_source_voltage_v"] = 5
        with self.assertRaises(ValueError):
            self.load(safety=policy)
        doc = fixture_document()
        doc["lockin_xx"]["sensitivity_full_scale_v"] = 1.0
        with self.assertRaises(ValueError):
            self.load(doc)

    def test_bool_parameters_are_not_numeric_settings(self):
        for role, key in (("photonics_lockin", "schema_version"), ("photonics_lockin", "reference_min_hz"),
                          ("lockin_xx", "time_constant_s"), ("lockin_xy", "sensitivity_full_scale_v"),
                          ("visa", "timeout_ms")):
            doc = fixture_document()
            doc[role][key] = True
            with self.subTest(role=role, key=key), self.assertRaises(ValueError):
                self.load(doc)

    def test_xy_connected_and_duplicate_address_reject(self):
        for change in ("source", "address"):
            doc = fixture_document()
            if change == "source":
                doc["lockin_xy"]["sine_output_connected"] = True
            else:
                doc["lockin_xy"]["address"] = doc["lockin_xx"]["address"]
            with self.assertRaises(ValueError):
                self.load(doc)

    def test_external_frequency_sweep_fields_reject(self):
        doc = fixture_document()
        doc["lockin_sweep"]["frequency_ranges"] = [{"min": 100, "max": 50000, "points": 3}]
        with self.assertRaises(ValueError):
            self.load(doc)

    def test_excitation_linear_ranges_include_both_endpoints(self):
        for spacing in ({"points": 3}, {"step": 0.002}):
            doc = fixture_document()
            doc["lockin_sweep"] = {"excitation_ranges": [
                {"min": 0.004, "max": 0.008, "scale": "linear", **spacing},
            ]}
            with self.subTest(spacing=spacing):
                self.assertEqual(self.load(doc).excitation_points_v_rms, (0.004, 0.006, 0.008))

    def test_excitation_log_range_and_disjoint_segments(self):
        doc = fixture_document()
        doc["lockin_sweep"] = {"excitation_ranges": [
            {"min": 0.004, "max": 0.006, "scale": "log", "points": 3},
            {"min": 0.007, "max": 0.009, "scale": "linear", "points": 2},
        ]}
        points = self.load(doc).excitation_points_v_rms
        self.assertEqual(points[0], 0.004)
        self.assertAlmostEqual(points[1], (0.004 * 0.006) ** 0.5)
        self.assertEqual(points[2:], (0.006, 0.007, 0.009))

    def test_explicit_excitation_points_keep_requested_order(self):
        doc = fixture_document()
        doc["lockin_sweep"]["excitation_points_v_rms"] = [0.006, 0.004, 0.005]
        self.assertEqual(self.load(doc).excitation_points_v_rms, (0.006, 0.004, 0.005))

    def test_excitation_point_list_and_ranges_are_strictly_mutually_exclusive(self):
        for sweep in ({}, {"excitation_points_v_rms": [0.004], "excitation_ranges": []}):
            doc = fixture_document()
            doc["lockin_sweep"] = sweep
            with self.subTest(sweep=sweep), self.assertRaisesRegex(ValueError, "exactly one"):
                self.load(doc)

    def test_excitation_range_malformed_numeric_spacing_and_keys_reject(self):
        cases = [
            {"min": True}, {"max": float("nan")}, {"max": float("inf")},
            {"min": 0.0}, {"max": 0.004}, {"points": True}, {"points": 2.5},
            {"points": 1}, {"step": 0.002}, {"scale": "invalid"},
            {"xx_full_scale_v": 0.001}, {"xy_full_scale_v": 0.001},
        ]
        for change in cases:
            doc = fixture_document()
            doc["lockin_sweep"] = {"excitation_ranges": [
                {"min": 0.004, "max": 0.008, "scale": "linear", "points": 3, **change},
            ]}
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.load(doc)
        for segment in (
            {"min": 0.004, "max": 0.008, "scale": "linear", "step": True},
            {"min": 0.004, "max": 0.008, "scale": "linear", "step": float("inf")},
            {"min": 0.004, "max": 0.008, "scale": "linear", "step": 0.003},
            {"min": 0.004, "max": 0.008, "scale": "linear", "step": 1e-320},
            {"min": 0.004, "max": 0.008, "scale": "log", "step": 0.002},
        ):
            doc = fixture_document()
            doc["lockin_sweep"] = {"excitation_ranges": [segment]}
            with self.subTest(segment=segment), self.assertRaises(ValueError):
                self.load(doc)

    def test_excitation_range_policy_cleanup_and_overlap_reject(self):
        for ranges in (
            [{"min": 0.0000001, "max": 0.008, "scale": "linear", "points": 3}],
            [{"min": 0.004, "max": 0.011, "scale": "linear", "points": 3}],
            [{"min": 0.003, "max": 0.008, "scale": "linear", "points": 3}],
            [{"min": 0.004, "max": 0.006, "scale": "linear", "points": 3},
             {"min": 0.006, "max": 0.008, "scale": "linear", "points": 3}],
        ):
            doc = fixture_document()
            doc["lockin_sweep"] = {"excitation_ranges": ranges}
            with self.subTest(ranges=ranges), self.assertRaises(ValueError):
                self.load(doc)

    def test_excitation_range_point_budget_checks_each_segment_and_total(self):
        for ranges in (
            [{"min": 0.004, "max": 0.008, "scale": "linear", "points": 100001}],
            [{"min": 0.004, "max": 0.008, "scale": "log", "points": 100001}],
            [{"min": 0.004, "max": 0.008, "scale": "linear", "step": 0.00000004}],
            [{"min": 0.004, "max": 0.006, "scale": "linear", "points": 60000},
             {"min": 0.007, "max": 0.009, "scale": "linear", "points": 60000}],
        ):
            doc = fixture_document()
            doc["lockin_sweep"] = {"excitation_ranges": ranges}
            with self.subTest(ranges=ranges), self.assertRaisesRegex(ValueError, "at most 100000"):
                self.load(doc)

    def test_excitation_range_accepts_exact_point_budget(self):
        doc = fixture_document()
        doc["lockin_sweep"] = {"excitation_ranges": [
            {"min": 0.004, "max": 0.008, "scale": "linear", "points": 100000},
        ]}
        points = self.load(doc).excitation_points_v_rms
        self.assertEqual(len(points), 100000)
        self.assertEqual((points[0], points[-1]), (0.004, 0.008))

    def test_four_pole_rc_filter_rejects_five_tau_settling(self):
        doc = fixture_document()
        doc["photonics_lockin"]["settle_time_constants"] = 5
        with self.assertRaises(ValueError):
            self.load(doc)


if __name__ == "__main__":
    unittest.main()
