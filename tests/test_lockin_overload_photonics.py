"""Continued photonics overloads remain visible and invalidate only their role."""
import copy
from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest

from attodry_control.analysis_observations import channel_quality
from attodry_control.combination_analysis import load_combination_rows, select_series
from attodry_control.combination_scan import AxisPoint, CombinationPlan, ScanAxis
from attodry_control.combination_store import CombinationStore, open_readonly
from attodry_control.lockin_overload import (OverloadPolicy, PhotonicsStatusPolicy,
                                           overload_summary, reading_allows_continuation)


XX = "measured.lockin_xx_h1_amplitude_v"
XY = "measured.lockin_xy_h2_amplitude_v"


def formal_entry(bad="xy", *, bracket_only=False):
    readings, settings = {}, {}
    for role, model, harmonic in (("xx", "SR830", 1), ("xy", "SR865A", 2)):
        fault = role == bad and not bracket_only
        native = {"input_overload_latched": fault, "output_scale_overload_latched": False,
            "reference_unlock_latched": False, "filter_fault_latched": False,
            "configuration_changed_latched": False, "power_on_latched": False,
            "unknown_status_bits": 0, "consumed_status_latches": True,
            "current_status_raw": 0, "lia_status_raw": 16 if fault else 0}
        if role == "xx":
            native = [{"raw": 1 if fault else 0, "input_or_reserve_overload": fault,
                "filter_overload": False, "output_overload": False, "reference_unlocked": False,
                "frequency_range_changed": False, "time_constant_changed": False}, 0]
        readings[role] = {"role": role, "model": model, "harmonic": harmonic,
            "x_v": 0.0001, "y_v": 0.0002, "amplitude_v": 0.0002236068, "phase_deg": 63.4,
            "status": {"locked": True, "input_overload": False, "output_scale_overload": False,
                "instrument_error": False, "validity": not fault,
                "observation": "instantaneous_and_latched" if role == "xy" else "latched_interval",
                "native_status": native}}
        settings[role] = {"role": role, "model": model, "sensitivity_full_scale_v": 0.001}
    problems = [f"lockin_{bad} input/reserve overload"] if bad else []
    entry = {"selected_harmonics": {"xx": 1, "xy": 2}, "samples": readings,
        "settings_before": copy.deepcopy(settings), "settings_after": copy.deepcopy(settings),
        "problems": problems, "settings_verified": True,
        "valid_for_analysis_by_role": {"lockin_xx": bad != "xx", "lockin_xy": bad != "xy"}}
    OverloadPolicy("record_continue").apply(entry, problems)
    return entry


def lockin_reading(entry):
    problems = entry["problems"]
    return {"module": "lockin", "captured_at_utc": "2026-10-05T00:00:00+00:00",
        "clean": not problems, "problems": problems,
        "overload_continuation": bool(entry.get("continued_overload_problems")),
        "reference_unlock_continuation": bool(entry.get("continued_reference_unlock_problems")),
        "actual": {"lockin_excitation_v_rms": 0.004, "lockin_frequency_hz": 50027.0},
        "measurements": {f"lockin_{role}_h{entry['selected_harmonics'][role]}_{metric}": sample[metric]
            for role, sample in entry["samples"].items()
            for metric in ("x_v", "y_v", "amplitude_v", "phase_deg")},
        "status": {"schema_version": "photonics-lockin-v1", "samples": [entry],
            "overload_policy": entry.get("overload_policy", "abort"),
            "reference_unlock_policy": entry.get("reference_unlock_policy", "abort")}}


def formal_unlock_entry(role="xy", *, current=False, bracket=None, mixed=False):
    entry = formal_entry("xx" if mixed else None)
    pair = entry["samples"]
    if bracket:
        pair = copy.deepcopy(pair)
        entry["bracket_samples"] = {bracket: pair}
    status = pair[role]["status"]
    status["validity"] = False
    status["locked"] = not current if role == "xy" else False
    if role == "xy":
        status["native_status"]["reference_unlock_latched"] = not current
    else:
        status["native_status"][0]["reference_unlocked"] = True
        status["native_status"][0]["raw"] |= 8
    entry["problems"].append(f"lockin_{role} reference unlocked")
    entry["valid_for_analysis_by_role"] = {"lockin_xx": False, "lockin_xy": False}
    PhotonicsStatusPolicy("record_continue" if mixed else "abort", "record_continue").apply(
        entry, entry["problems"])
    return entry


class PhotonicsReferenceStatusPolicyTests(unittest.TestCase):
    def test_policies_are_independent_and_standalone_unlock_remains_blocking(self):
        overload, unlock = "lockin_xy output-scale overload", "lockin_xy reference unlocked"
        self.assertEqual(OverloadPolicy("record_continue").partition([unlock]), ([unlock], []))
        self.assertEqual(PhotonicsStatusPolicy("record_continue").partition([overload, unlock]),
                         ([unlock], [overload]))
        policy = PhotonicsStatusPolicy("abort", "record_continue")
        self.assertEqual(policy.partition([overload, unlock]), ([overload], [unlock]))
        record = {}
        PhotonicsStatusPolicy("record_continue", "record_continue").apply(record, [overload, unlock])
        self.assertEqual(record["continued_overload_problems"], [overload])
        self.assertEqual(record["continued_reference_unlock_problems"], [unlock])
        with self.assertRaises(ValueError):
            PhotonicsStatusPolicy(reference_unlock_policy="continue_unselected")

    def test_only_exact_known_unlock_names_are_continuable(self):
        policy = PhotonicsStatusPolicy("record_continue", "record_continue")
        hard = ["lockin_xy reference is unlocked", "lockin_xy status validity unknown",
                "lockin_xy reference unlocked unknown", "lockin_xy filter fault", "communication failed"]
        self.assertEqual(policy.partition(hard), (hard, []))

    def test_current_latched_and_bracket_unlocks_accept_with_both_invalid(self):
        for role in ("xx", "xy"):
            for bracket in (None, "before", "after", "settled", "transition"):
                for current in (False, True):
                    with self.subTest(role=role, bracket=bracket, current=current):
                        reading = lockin_reading(formal_unlock_entry(role, current=current, bracket=bracket))
                        self.assertTrue(reading_allows_continuation(reading))
                        for companion in ("lockin_xx", "lockin_xy"):
                            bad = copy.deepcopy(reading)
                            bad["status"]["samples"][0]["valid_for_analysis_by_role"][companion] = True
                            self.assertFalse(reading_allows_continuation(bad))

    def test_unlock_exception_cannot_excuse_unknown_error_configuration_or_filter(self):
        original = lockin_reading(formal_unlock_entry())
        for key, value in (("locked", None), ("validity", None), ("validity", True),
                           ("instrument_error", True), ("input_overload", None)):
            reading = copy.deepcopy(original)
            reading["status"]["samples"][0]["samples"]["xy"]["status"][key] = value
            self.assertFalse(reading_allows_continuation(reading), key)
        for key in ("filter_fault_latched", "power_on_latched", "configuration_changed_latched",
                    "unknown_status_bits", "consumed_status_latches"):
            reading = copy.deepcopy(original)
            native = reading["status"]["samples"][0]["samples"]["xy"]["status"]["native_status"]
            native[key] = 1 if key == "unknown_status_bits" else False if key == "consumed_status_latches" else True
            self.assertFalse(reading_allows_continuation(reading), key)

    def test_archive_cannot_forge_unlock_authorization_with_only_outer_flag(self):
        original = lockin_reading(formal_unlock_entry())
        for field in ("reference_unlock_policy", "continued_reference_unlock_problems"):
            reading = copy.deepcopy(original)
            del reading["status"]["samples"][0][field]
            self.assertFalse(reading_allows_continuation(reading), field)
        reading = copy.deepcopy(original)
        reading["reference_unlock_continuation"] = False
        self.assertFalse(reading_allows_continuation(reading))
        reading = copy.deepcopy(original)
        reading["status"]["samples"][0]["settings_verified"] = False
        self.assertFalse(reading_allows_continuation(reading))
        reading = copy.deepcopy(original)
        reading["status"]["samples"][0]["problems"] = []
        self.assertFalse(reading_allows_continuation(reading))
        for value in (None, "abort"):
            reading = copy.deepcopy(original)
            if value is None:
                del reading["status"]["reference_unlock_policy"]
            else:
                reading["status"]["reference_unlock_policy"] = value
            self.assertFalse(reading_allows_continuation(reading))
        reading = lockin_reading(formal_entry())
        reading["reference_unlock_continuation"] = True
        reading["status"]["reference_unlock_policy"] = "record_continue"
        reading["status"]["samples"][0]["reference_unlock_policy"] = "record_continue"
        self.assertFalse(reading_allows_continuation(reading))
        reading = copy.deepcopy(original)
        entry = reading["status"]["samples"][0]
        entry["samples"] = formal_entry(bad=None)["samples"]
        self.assertFalse(reading_allows_continuation(reading))

    def test_real_fake_session_output_satisfies_archive_contract(self):
        from tests.test_photonics_reference_unlock import ReferenceUnlockContinuationTests
        for role, current in (("XX", False), ("XY", False), ("XY", True)):
            with self.subTest(role=role, current=current):
                helper = ReferenceUnlockContinuationTests()
                session, manager, _, _, _ = helper.ready(overload_policy="record_continue")
                self.addCleanup(helper.doCleanups)
                resource = manager.resources[f"FAKE::{role}"]
                resource.responses["CUROVLDSTAT?" if current else "LIAS?"] = "8"
                reading = session.sample_point()
                self.assertTrue(reading_allows_continuation(reading))

    def test_unlock_does_not_authorize_native_overload_when_overload_policy_aborts(self):
        entry = formal_unlock_entry()
        status = entry["samples"]["xx"]["status"]
        status["validity"] = False
        status["native_status"][0]["input_or_reserve_overload"] = True
        self.assertFalse(reading_allows_continuation(lockin_reading(entry)))

    def test_mixed_unlock_overload_summary_counts_separate_faults_once(self):
        entry = formal_unlock_entry(mixed=True)
        self.assertTrue(reading_allows_continuation(lockin_reading(entry)))
        summary = overload_summary({"schema_version": "photonics-lockin-v1", "samples": [entry]})
        self.assertEqual(summary["formal_pairs"], 1)
        self.assertEqual(summary["overloaded_pairs_by_role"], {"lockin_xx": 1, "lockin_xy": 0})
        self.assertEqual(summary["unlocked_pairs_by_role"], {"lockin_xx": 0, "lockin_xy": 1})
        self.assertEqual(summary["invalid_pairs_by_role"], {"lockin_xx": 1, "lockin_xy": 1})
        self.assertEqual(summary["continued_overload_pairs"], 1)
        self.assertEqual(summary["continued_reference_unlock_pairs"], 1)

    def test_companion_quality_detects_native_latch_even_without_aggregate_declaration(self):
        entry = formal_unlock_entry()
        entry.pop("continued_reference_unlock_problems")
        entry["valid_for_analysis_by_role"] = {"lockin_xx": True, "lockin_xy": True}
        row = {"status.lockin": {"schema_version": "photonics-lockin-v1", "samples": [entry]},
               XX: entry["samples"]["xx"]["amplitude_v"], XY: entry["samples"]["xy"]["amplitude_v"]}
        for column in (XX, XY):
            self.assertEqual(channel_quality(row, column)[0], "flagged")
            self.assertIn("reference_unlocked", channel_quality(row, column)[1])


class PhotonicsOverloadTests(unittest.TestCase):
    def test_exact_output_scale_fault_continues_but_other_faults_do_not(self):
        policy = OverloadPolicy("record_continue")
        for role in ("xx", "xy"):
            allowed = f"lockin_{role} output-scale overload"
            self.assertEqual(policy.partition([allowed]), ([], [allowed]))
        hard = ["lockin_xy filter fault", "lockin_xy reference is unlocked",
                "lockin_xy output scale overload", "unknown output-scale overload"]
        self.assertEqual(policy.partition(hard), (hard, []))

    def test_latched_only_and_bracket_only_summary_are_counted_once(self):
        for bracket in (False, True):
            entry = formal_entry(bracket_only=bracket)
            value = {"status": {"schema_version": "photonics-lockin-v1", "samples": [entry],
                "transition_probes": [{"samples": [entry]}]}}
            summary = overload_summary(value)
            self.assertEqual(summary["formal_pairs"], 1)
            self.assertEqual(summary["overloaded_pairs_by_role"], {"lockin_xx": 0, "lockin_xy": 1})
            self.assertEqual(summary["invalid_pairs_by_role"], {"lockin_xx": 0, "lockin_xy": 1})
            self.assertEqual(summary["continued_overload_pairs"], 1)

    def test_native_raw_words_are_not_reinterpreted(self):
        entry = formal_entry(bad=None)
        entry["samples"]["xy"]["status"]["native_status"].update(lia_status_raw=65535)
        summary = overload_summary({"schema_version": "photonics-lockin-v1", "samples": [entry]})
        self.assertEqual(summary["data_quality"], "no_overload_recorded")

    def test_continuation_cannot_excuse_nonoverload_or_unverified_settings(self):
        reading = lockin_reading(formal_entry())
        self.assertTrue(reading_allows_continuation(reading))
        for problem in ("lockin_xy reference is unlocked", "lockin_xy filter fault", "communication failed"):
            changed = copy.deepcopy(reading)
            changed["problems"].append(problem)
            self.assertFalse(reading_allows_continuation(changed))
        changed = copy.deepcopy(reading)
        changed["status"]["samples"][0]["settings_verified"] = False
        self.assertFalse(reading_allows_continuation(changed))

    def test_normalized_unlock_unknown_and_native_faults_block_despite_declared_overload(self):
        for role in ("xx", "xy"):
            for key, value in (("locked", False), ("locked", None), ("instrument_error", True),
                               ("input_overload", None), ("validity", None)):
                with self.subTest(role=role, key=key, value=value):
                    reading = lockin_reading(formal_entry())
                    reading["status"]["samples"][0]["samples"][role]["status"][key] = value
                    self.assertFalse(reading_allows_continuation(reading))
        for field in ("reference_unlock_latched", "filter_fault_latched", "power_on_latched",
                      "configuration_changed_latched", "unknown_status_bits"):
            reading = lockin_reading(formal_entry())
            native = reading["status"]["samples"][0]["samples"]["xy"]["status"]["native_status"]
            native[field] = 1 if field == "unknown_status_bits" else True
            self.assertFalse(reading_allows_continuation(reading), field)
        for stage in ("before", "after", "settled"):
            reading = lockin_reading(formal_entry())
            entry = reading["status"]["samples"][0]
            entry["bracket_samples"] = {stage: copy.deepcopy(entry["samples"])}
            entry["bracket_samples"][stage]["xy"]["status"]["locked"] = False
            self.assertFalse(reading_allows_continuation(reading), stage)

    def test_expected_harmonic_transition_history_does_not_hide_formal_overload(self):
        reading = lockin_reading(formal_entry())
        entry = reading["status"]["samples"][0]
        entry["bracket_samples"] = {"transition": copy.deepcopy(entry["samples"])}
        transition = entry["bracket_samples"]["transition"]["xy"]["status"]
        transition["locked"] = False
        transition["native_status"]["reference_unlock_latched"] = True
        transition["native_status"]["configuration_changed_latched"] = True
        self.assertTrue(reading_allows_continuation(reading))

    def test_sr830_transition_only_settings_history_may_have_unknown_validity(self):
        reading = lockin_reading(formal_entry())
        entry = reading["status"]["samples"][0]
        entry["bracket_samples"] = {"transition": copy.deepcopy(entry["samples"])}
        transition = entry["bracket_samples"]["transition"]["xx"]["status"]
        transition["validity"] = None
        transition["native_status"][0]["frequency_range_changed"] = True
        self.assertTrue(reading_allows_continuation(reading))
        transition["native_status"][0]["raw"] = 128
        self.assertFalse(reading_allows_continuation(reading))
        transition["native_status"][0]["raw"] = 0
        transition["native_status"][0]["frequency_range_changed"] = False
        self.assertFalse(reading_allows_continuation(reading))
        for field in ("lockin_xx", "lockin_xy"):
            changed = copy.deepcopy(reading)
            del changed["status"]["samples"][0]["valid_for_analysis_by_role"][field]
            self.assertFalse(reading_allows_continuation(changed))
        changed = copy.deepcopy(reading)
        changed["status"]["samples"][0]["valid_for_analysis_by_role"]["lockin_xy"] = True
        self.assertFalse(reading_allows_continuation(changed))

    def test_bracket_fault_role_is_flagged_with_clean_formal_status(self):
        entry = formal_entry(bracket_only=True)
        row = {"status.lockin": {"schema_version": "photonics-lockin-v1", "samples": [entry]},
               XX: entry["samples"]["xx"]["amplitude_v"], XY: entry["samples"]["xy"]["amplitude_v"]}
        self.assertEqual(channel_quality(row, XX), ("clear", ()))
        self.assertIn("recorded_invalid_for_analysis", channel_quality(row, XY)[1])


class PhotonicsCombinationAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "scan.sqlite"

    def store_sample(self, entry, *, cleanup_clean=True, status="completed", other_fault=False):
        plan = CombinationPlan((ScanAxis("lockin", (AxisPoint({
            "lockin_excitation_v_rms": 0.004, "lockin_frequency_hz": 50027.0}),)),))
        condition, = plan.conditions()
        reading = lockin_reading(entry)
        reads = [reading]
        if other_fault:
            reads.append({"module": "optical", "clean": False, "problems": ["power limit"],
                          "captured_at_utc": reading["captured_at_utc"]})
        payload = {"clean": not entry["problems"], "acquisition_accepted": True,
            "simulated": False, "started_at_utc": reading["captured_at_utc"],
            "finished_at_utc": reading["captured_at_utc"], "actual": reading["actual"],
            "measurements": reading["measurements"], "reads": reads}
        with CombinationStore(self.path) as store:
            store.begin_run("test", plan.snapshot(), [condition])
            index = store.begin_attempt("test", condition["condition_id"])
            store.sample("test", condition["condition_id"], index, 0, payload)
            # Deliberately permit malformed archived evidence to exercise loader guards.
            with store.connection:
                store.connection.execute("UPDATE combination_attempts SET status='accepted'")
            store.finish_run("test", "completed", {"clean": True}, None)
            with store.connection:
                store.connection.execute("UPDATE combination_runs SET status=?, cleanup_json=?",
                                         (status, json.dumps({"clean": cleanup_clean})))
        return payload

    def test_default_keeps_companion_and_audit_retains_invalid_values_and_raw(self):
        payload = self.store_sample(formal_entry())
        raw, = load_combination_rows(self.path, audit=True)
        row, = load_combination_rows(self.path)
        self.assertTrue(row["accepted"])
        self.assertTrue(row["acquisition_accepted"])
        self.assertFalse(row["clean"])
        self.assertIn(XX, row)
        self.assertNotIn(XY, row)
        self.assertEqual(raw[XY], payload["measurements"][XY.removeprefix("measured.")])
        self.assertFalse(raw["valid_for_analysis_by_role"]["lockin_xy"])
        self.assertFalse(raw["valid_for_analysis_by_channel"][XY])
        self.assertTrue(select_series([row], x="actual.lockin_excitation_v_rms", y=XX))
        with closing(open_readonly(self.path)) as connection:
            stored = json.loads(connection.execute("SELECT payload_json FROM combination_samples").fetchone()[0])
        self.assertEqual(stored, payload)

    def test_bracket_only_fault_is_excluded_even_with_clean_current_reading(self):
        self.store_sample(formal_entry(bracket_only=True))
        row, = load_combination_rows(self.path)
        self.assertIn(XX, row)
        self.assertNotIn(XY, row)

    def test_failed_run_and_unverified_cleanup_cannot_be_salvaged(self):
        for status, clean in (("failed", True), ("completed", False)):
            with self.subTest(status=status, cleanup=clean):
                self.store_sample(formal_entry(), status=status, cleanup_clean=clean)
                self.assertEqual(load_combination_rows(self.path), ())
                self.path.unlink()

    def test_other_module_fault_cannot_be_salvaged(self):
        self.store_sample(formal_entry(), other_fault=True)
        self.assertEqual(load_combination_rows(self.path), ())

    def test_invalid_or_missing_role_evidence_cannot_be_salvaged(self):
        entry = formal_entry()
        del entry["valid_for_analysis_by_role"]["lockin_xy"]
        self.store_sample(entry)
        self.assertEqual(load_combination_rows(self.path), ())

    def test_declared_overload_with_normalized_unlock_cannot_be_salvaged(self):
        entry = formal_entry()
        entry["samples"]["xy"]["status"]["locked"] = False
        self.store_sample(entry)
        self.assertEqual(load_combination_rows(self.path), ())

    def test_both_invalid_roles_remain_in_audit_but_no_default_lockin_values(self):
        entry = formal_entry()
        entry["valid_for_analysis_by_role"]["lockin_xx"] = False
        entry["problems"].append("lockin_xx filter overload")
        OverloadPolicy("record_continue").apply(entry, entry["problems"])
        self.store_sample(entry)
        row, = load_combination_rows(self.path)
        self.assertNotIn(XX, row)
        self.assertNotIn(XY, row)
        raw, = load_combination_rows(self.path, audit=True)
        self.assertIn(XX, raw)
        self.assertIn(XY, raw)

    def test_mixed_unlock_with_overload_cannot_be_salvaged(self):
        entry = formal_entry()
        entry["problems"].append("lockin_xy reference is unlocked")
        self.store_sample(entry)
        self.assertEqual(load_combination_rows(self.path), ())

    def test_explicit_unlock_keeps_raw_pair_but_excludes_both_default_channels(self):
        for stage in (None, "after", "transition"):
            with self.subTest(stage=stage):
                payload = self.store_sample(formal_unlock_entry(bracket=stage))
                row, = load_combination_rows(self.path)
                self.assertTrue(row["acquisition_accepted"])
                self.assertFalse(row["clean"])
                self.assertNotIn(XX, row)
                self.assertNotIn(XY, row)
                raw, = load_combination_rows(self.path, audit=True)
                self.assertEqual(raw[XX], payload["measurements"][XX.removeprefix("measured.")])
                self.assertEqual(raw[XY], payload["measurements"][XY.removeprefix("measured.")])
                self.assertFalse(raw["valid_for_analysis_by_channel"][XX])
                self.assertFalse(raw["valid_for_analysis_by_channel"][XY])
                self.path.unlink()

    def test_unapproved_unlock_archive_is_audit_only_even_if_accepted_in_sqlite(self):
        entry = formal_unlock_entry()
        del entry["reference_unlock_policy"]
        self.store_sample(entry)
        self.assertEqual(load_combination_rows(self.path), ())
        raw, = load_combination_rows(self.path, audit=True)
        self.assertFalse(raw["accepted"])
        self.assertIn(XX, raw)
        self.assertIn(XY, raw)


if __name__ == "__main__":
    unittest.main()
