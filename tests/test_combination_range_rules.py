"""Range-rule loading and coordinator contracts with owned fake devices only."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from attodry_control.combination_hardware import load_hardware_combination
from attodry_control.combination_scan import (
    AxisPoint, CombinationPlan, ScanAxis, SimulatedCombinationStation,
    run_simulated_combination,
)
from attodry_control.combination_store import CombinationStore, open_readonly
from attodry_control.internal_lockin_group import InternalLockinGroupSession
from tests import test_internal_optical_combination as internal
from tests import test_combination_photonics as pem
from tests import test_combination_continuous_gate as continuous
from tests import test_combination_cryostat as cryostat
from tests.test_three_lockin import AUX, POLICY


def range_rule(name, power, *, xx=.01, xy=.01, **extra):
    return dict(name=name, match={"optical_target_power_w": power},
                lockin_xx_full_scale_v=xx, lockin_xy_full_scale_v=xy, **extra)


class InternalRangeRuleIntegrationTests(unittest.TestCase):
    write_config = internal.InternalOpticalCombinationTests.write_config
    write_document = internal.InternalOpticalCombinationTests.write_document
    factory = internal.InternalOpticalCombinationTests.factory
    events = internal.InternalOpticalCombinationTests.events
    execute = internal.InternalOpticalCombinationTests.execute
    backend_factory = internal.InternalOpticalCombinationTests.backend_factory
    meter_factory = internal.InternalOpticalCombinationTests.meter_factory
    chopper_factory = internal.InternalOpticalCombinationTests.chopper_factory

    def setUp(self):
        internal.InternalOpticalCombinationTests.setUp(self)
        self.document["power_feedback"].update(target_mapping="cartesian",
                                              target_powers_w=[5e-6, 10e-6])
        self.document["lockin_sweep"]["excitation_points_v_rms"] = [.004, .008]
        self.rules = [range_rule("low", [5e-6]), range_rule("high", [10e-6], xx=.02, xy=.02)]
        self.document["combination_scan"]["lockin_range_rules"] = deepcopy(self.rules)
        self.write_document()

    def load(self):
        with patch("attodry_control.internal_lockin_group.InternalLockinGroupSession.open",
                   side_effect=AssertionError("Parsing cannot open a lock-in")), \
             patch("attodry_control.optical_points.OpticalPointSession.open",
                   side_effect=AssertionError("Parsing cannot open the optical source")):
            result = load_hardware_combination(self.path)
        self.assertEqual(self.manager.opened, [])
        return result

    def test_dual_loader_compiles_profiles_without_altering_grid(self):
        cfg = self.load()
        self.assertIsNone(cfg.lockin.lockin_aux)
        self.assertEqual(len(cfg.plan.conditions()), 4)
        self.assertEqual([row["rule_name"] for row in cfg.lockin_range_schedule.preview()], ["low", "high"])
        self.assertEqual(cfg.snapshot["lockin_range_rules"]["contract"], "outer-condition-lockin-range-v1")

    def test_three_role_loader_requires_aux_scale_and_its_own_station_allowlist(self):
        self.document["lockin_aux"] = tomllib.loads(AUX)["lockin_aux"]
        with (self.directory / "lockin_safety.toml").open("a") as handle:
            handle.write(POLICY)
        self.write_document()
        with self.assertRaisesRegex(ValueError, "every active"):
            self.load()
        for index, rule in enumerate(self.document["combination_scan"]["lockin_range_rules"]):
            rule["lockin_aux_full_scale_v"] = .001 if index == 0 else .0002
        self.write_document()
        cfg = self.load()
        self.assertEqual(cfg.lockin_range_schedule.resolve((1,)).full_scales_v["lockin_aux"], .0002)
        self.document["combination_scan"]["lockin_range_rules"][1]["lockin_aux_full_scale_v"] = .01
        self.write_document()
        with self.assertRaisesRegex(ValueError, "supported and allowed"):
            self.load()

    def test_model_supported_but_station_disallowed_sensitivity_rejects_offline(self):
        self.document["combination_scan"]["lockin_range_rules"][0]["lockin_xx_full_scale_v"] = 50e-6
        self.write_document()
        with self.assertRaisesRegex(ValueError, "supported and allowed"):
            self.load()

    def test_missing_and_ambiguous_rules_reject_before_hardware(self):
        for rules, error in ((self.rules[:1], "unmatched"),
                             (self.rules + [range_rule("overlap", [5e-6])], "ambiguous")):
            with self.subTest(error=error):
                self.document["combination_scan"]["lockin_range_rules"] = deepcopy(rules)
                self.write_document()
                with self.assertRaisesRegex(ValueError, error):
                    self.load()

    def test_rule_coordinates_must_be_outside_lockin(self):
        self.document["combination_scan"]["order"] = ["lockin", "optical"]
        self.write_document()
        with self.assertRaisesRegex(ValueError, "outside lockin"):
            self.load()

    def test_rules_reject_frequency_mode_harmonic_and_segment_range_conflicts(self):
        base = deepcopy(self.document)
        variants = []
        mode = deepcopy(base)
        mode["combination_scan"]["lockin_mode"] = "frequency"
        variants.append(mode)
        harmonic = deepcopy(base)
        harmonic["lockin_xy"]["harmonic_settings"] = {
            "h1": dict(sensitivity_mode="fixed", sensitivity_full_scale_v=.01, reserve_mode="normal")}
        variants.append(harmonic)
        segment = deepcopy(base)
        segment["lockin_sweep"].pop("excitation_points_v_rms")
        segment["lockin_sweep"]["excitation_ranges"] = [
            dict(min=.004, max=.008, scale="linear", points=2, xx_full_scale_v=.02)]
        variants.append(segment)
        auto = deepcopy(base)
        auto["lockin_xx"].update(sensitivity_mode="bounded_auto", sensitivity_full_scale_v=.01,
            autorange_min_full_scale_v=.01, autorange_max_full_scale_v=.02,
            autorange_target_occupancy=.85, autorange_stable_samples=2)
        variants.append(auto)
        for index, variant in enumerate(variants):
            with self.subTest(variant=index):
                self.document = variant
                self.write_document()
                with self.assertRaises(ValueError):
                    self.load()

    def test_unconverted_dual_profile_is_rejected_explicitly_before_io(self):
        self.document["lockin_xy"]["filter_slope_db_oct"] = 12
        self.write_document()
        with self.assertRaisesRegex(ValueError, "24 dB"):
            self.load()

    def test_variable_once_background_rejects_constant_profile_is_allowed(self):
        self.document["optical_background"] = dict(enabled=True, samples=1,
            max_age_s=600, capture_mode="once_before_scan")
        self.write_document()
        with self.assertRaisesRegex(ValueError, "per_condition"):
            self.load()
        self.document["combination_scan"]["lockin_range_rules"][1].update(
            lockin_xx_full_scale_v=.01, lockin_xy_full_scale_v=.01)
        self.write_document()
        self.assertIsNotNone(self.load().lockin_range_schedule)

    def test_absent_rules_keeps_existing_dual_session_and_no_profile_events(self):
        self.document["combination_scan"].pop("lockin_range_rules")
        self.write_document()
        cfg = self.load()
        self.assertIsNone(cfg.lockin_range_schedule)
        self.assertNotIn("lockin_range_rules", cfg.snapshot)
        with patch.object(InternalLockinGroupSession, "open", side_effect=AssertionError("No rule must preserve dual session")):
            result = self.execute("legacy")
        self.assertEqual(result["status"], "completed", result)
        self.assertFalse(any(kind.startswith("lockin_range_") for kind, _ in self.events()))

    def test_complete_fake_run_records_each_condition_profile_and_matching_background(self):
        self.document["optical_background"] = dict(enabled=True, samples=1,
            max_age_s=600, capture_mode="per_condition")
        self.write_document()
        result = self.execute("profile-background")
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["cleanup"]["clean"], result)
        events = self.events()
        selected = [payload for kind, payload in events if kind == "lockin_range_selected"]
        self.assertEqual([p["rule_name"] for p in selected], ["low", "low", "high", "high"])
        darks = [payload for kind, payload in events if kind == "optical_dark_background"]
        self.assertEqual(len(darks), 4)
        self.assertEqual([p["context"]["lockin_full_scales_v"]["lockin_xx"] for p in darks],
                         [.01, .01, .02, .02])
        with closing(open_readonly(self.database)) as db:
            samples = [json.loads(row[0]) for row in db.execute("SELECT payload_json FROM combination_samples ORDER BY rowid")]
        self.assertEqual(len(samples), 8)
        for index, sample in enumerate(samples):
            reading = next(item for item in sample["reads"] if item["module"] == "lockin")
            profile = reading["status"]["lockin_range_profile"]
            self.assertTrue(profile["verified"])
            self.assertEqual(profile["rule_name"], "low" if index < 4 else "high")
            self.assertEqual(profile["confirmed_full_scales_v"]["lockin_xx"], .01 if index < 4 else .02)
            self.assertTrue(reading["status"]["background"]["context_matches"])

    def test_failed_range_write_keeps_partial_evidence_and_runs_independent_cleanup(self):
        self.document["combination_scan"]["lockin_range_rules"][0]["lockin_xy_full_scale_v"] = .02
        self.write_document()
        self.xy.fail_write = "SENS 21"
        result = self.execute("failed-range")
        self.assertEqual(result["status"], "failed", result)
        self.assertFalse(result["cleanup"]["clean"], result)
        self.assertTrue(result["cleanup"]["manual_verification_required"], result)
        events = self.events()
        failure = next(payload for kind, payload in events if kind == "lockin_range_profile_failed")
        self.assertEqual(failure["confirmed_full_scales_v"]["lockin_xx"], .01)
        self.assertIsNone(failure["confirmed_full_scales_v"]["lockin_xy"])
        self.assertFalse(failure["verified"])
        self.assertFalse(any(kind == "lockin_range_selected" for kind, _ in events))
        self.assertTrue(self.xx.closed and self.xy.closed and self.manager.closed)
        self.assertEqual(self.backend.source["emission_state"], 0)
        with closing(open_readonly(self.database)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM combination_samples").fetchone()[0], 0)


class PemRangeRuleLoadingTests(unittest.TestCase):
    write_config = pem.PhotonicsCombinationTests.write_config
    on_lockin_query = pem.PhotonicsCombinationTests.on_lockin_query
    backend_factory = pem.PhotonicsCombinationTests.backend_factory
    pem_factory = pem.PhotonicsCombinationTests.pem_factory
    meter_factory = pem.PhotonicsCombinationTests.meter_factory
    execute = pem.PhotonicsCombinationTests.execute
    events = pem.PhotonicsCombinationTests.events

    def setUp(self):
        pem.PhotonicsCombinationTests.setUp(self)
        self.document = tomllib.loads(self.path.read_text())
        self.document["combination_scan"]["lockin_range_rules"] = [
            range_rule("all-powers", self.document["power_feedback"]["target_powers_w"])]

    def test_pem_mixed_model_rules_compile_with_actual_policy(self):
        self.path.write_text(pem.toml_document(self.document))
        cfg = load_hardware_combination(self.path)
        self.assertEqual(cfg.lockin.lockin_xx.model, "SR865A")
        self.assertEqual(cfg.lockin.lockin_xy.model, "SR830")
        self.assertEqual(len(cfg.plan.conditions()), 6)
        self.assertEqual(len(cfg.lockin_range_schedule.preview()), 3)
        self.assertEqual(self.manager.opened, [])

    def test_pem_supported_range_outside_its_safety_policy_is_rejected(self):
        self.document["combination_scan"]["lockin_range_rules"][0]["lockin_xx_full_scale_v"] = .02
        self.path.write_text(pem.toml_document(self.document))
        with self.assertRaisesRegex(ValueError, "supported and allowed"):
            load_hardware_combination(self.path)
        self.assertEqual(self.manager.opened, [])

    def test_pem_range_application_precedes_reference_transitions_and_formal_sampling(self):
        self.path.write_text(pem.toml_document(self.document))
        result = self.execute("pem-ranges")
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["cleanup"]["clean"], result)
        events = self.events()
        selections = [payload for kind, payload in events if kind == "lockin_range_selected"]
        self.assertEqual(len(selections), 6)
        self.assertTrue(all(payload["verified"] for payload in selections))
        with closing(open_readonly(self.database)) as db:
            samples = [json.loads(row[0]) for row in db.execute("SELECT payload_json FROM combination_samples")]
        self.assertEqual(len(samples), 12)
        for sample in samples:
            reading = next(item for item in sample["reads"] if item["module"] == "lockin")
            self.assertEqual(reading["status"]["lockin_range_profile"]["confirmed_full_scales_v"],
                             {"lockin_xx": .01, "lockin_xy": .01})


class CoordinatorRangePreparationTests(unittest.TestCase):
    def test_each_condition_prepares_before_optical_or_lockin_even_unchanged_prefix(self):
        class Station(SimulatedCombinationStation):
            def prepare_lockin_ranges(self, condition):
                self.operations.append(("prepare_ranges", condition["condition_id"]))
        plan = CombinationPlan((
            ScanAxis("temperature", (AxisPoint({"temperature_k": 2}),)),
            ScanAxis("optical", (AxisPoint({"optical_current_a": .01}),)),
            ScanAxis("lockin", tuple(AxisPoint({"lockin_excitation_v_rms": value,
                                               "lockin_frequency_hz": 17}) for value in (.004, .008))),
        ))
        station = Station()
        with tempfile.TemporaryDirectory() as directory, CombinationStore(Path(directory) / "scan.sqlite") as store:
            result = run_simulated_combination(plan, store, "fake", station=station)
        self.assertEqual(result["status"], "completed", result)
        operations = station.operations
        prepares = [index for index, op in enumerate(operations) if op[0] == "prepare_ranges"]
        optical = next(index for index, op in enumerate(operations) if op[:2] == ("apply", "optical"))
        lockins = [index for index, op in enumerate(operations) if op[:2] == ("apply", "lockin")]
        temperature_qualified = next(index for index, op in enumerate(operations) if op[:2] == ("qualify_axis", "temperature"))
        self.assertEqual(len(prepares), 2)
        self.assertLess(temperature_qualified, prepares[0])
        self.assertLess(prepares[0], optical)
        self.assertLess(prepares[0], lockins[0])
        self.assertLess(prepares[1], lockins[1])


class CryostatRangeRuleIntegrationTests(unittest.TestCase):
    setUp = cryostat.CryostatCombinationTests.setUp
    write_config = cryostat.CryostatCombinationTests.write_config
    factory = cryostat.CryostatCombinationTests.factory
    events = cryostat.CryostatCombinationTests.events
    execute = cryostat.CryostatCombinationTests.execute

    def install(self, module):
        document = tomllib.loads(self.base)
        if module == "temperature":
            document["temperature_scan"]["stop_k"] = 2.1
            key, first, second = "temperature_k", 2.0, 2.1
        else:
            document["magnetic_field_run"]["points"] = [
                dict(bx_t=0, bz_t=0), dict(bx_t=0, bz_t=.01)]
            key, first, second = "field_z_t", 0, .01
        rules = [dict(name="first", match={key: [first]}, lockin_xx_full_scale_v=.01,
                      lockin_xy_full_scale_v=.01),
                 dict(name="second", match={key: [second]}, lockin_xx_full_scale_v=.02,
                      lockin_xy_full_scale_v=.02)]
        document["combination_scan"] = dict(backend="hardware", order=[module, "lockin"],
            samples_per_condition=1, repeats=1, run_name="fake-cryo-ranges", note="Owned fake DLL",
            lockin_range_rules=rules)
        self.path.write_text(pem.toml_document(document))

    def check_run(self, module):
        self.install(module)
        cfg = load_hardware_combination(self.path)
        self.assertIsNone(cfg.optical)
        self.assertEqual(len(cfg.plan.conditions()), 4)
        result = self.execute(module + "-ranges")
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["cleanup"]["clean"], result)
        self.assertEqual(self.dll.events.count("connect"), 1)
        self.assertEqual(self.dll.events.count("disconnect"), 1)
        selections = [payload for kind, payload in self.events() if kind == "lockin_range_selected"]
        self.assertEqual([payload["rule_name"] for payload in selections],
                         ["first", "first", "second", "second"])

    def test_temperature_outer_switches_ranges_without_optical_module(self):
        self.check_run("temperature")

    def test_magnetic_outer_switches_ranges_without_optical_module(self):
        self.check_run("magnetic")


class ContinuousRangeRuleTests(unittest.TestCase):
    def make(self):
        return continuous.ContinuousGateCombinationTests.make(self)

    def test_constant_ranges_keep_illumination_across_inner_gate_visits(self):
        case = self.make()
        document = tomllib.loads(case.path.read_text())
        powers = document["power_feedback"]["target_powers_w"]
        # Rule names can change without a physical change in sensitivity.
        document["combination_scan"]["lockin_range_rules"] = [
            range_rule("negative-gate", powers), range_rule("nonnegative-gate", powers)]
        document["combination_scan"]["lockin_range_rules"][0]["match"]["gate_top_v"] = [-.1]
        document["combination_scan"]["lockin_range_rules"][1]["match"]["gate_top_v"] = [0, .1]
        case.path.write_text(pem.toml_document(document))
        result = continuous.ContinuousGateCombinationTests.execute(self, case, "continuous-ranges")
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["cleanup"]["clean"], result)
        groups = [payload for kind, payload in case.events() if kind == "continuous_optical_group_qualified"]
        self.assertEqual(len(groups), 2)
        # Source-current feedback may reassert ON during initial group tuning.
        # The contract is no OFF after qualification across inner gate visits.
        for group in groups:
            power = group["requested"]["optical_target_power_w"]
            start = next(i for i, entry in enumerate(case.log)
                         if entry[:2] == ("event", "continuous_optical_group_qualified")
                         and entry[2]["requested"]["optical_target_power_w"] == power)
            last_selection = max(i for i, entry in enumerate(case.log)
                                 if entry[:2] == ("event", "lockin_range_selected")
                                 and entry[2]["requested"]["optical_target_power_w"] == power)
            self.assertFalse(any(entry[:2] == ("laser", False)
                                 for entry in case.log[start:last_selection + 1]))
        samples = continuous.ContinuousGateCombinationTests.samples(self, case)
        self.assertEqual(len(samples), 12)
        for sample in samples:
            reading = next(item for item in sample["reads"] if item["module"] == "lockin")
            self.assertTrue(reading["status"]["lockin_range_profile"]["verified"])

    def test_ranges_changing_within_continuously_illuminated_gate_group_reject_offline(self):
        case = self.make()
        document = tomllib.loads(case.path.read_text())
        powers = document["power_feedback"]["target_powers_w"]
        low = range_rule("negative-gate", powers)
        high = range_rule("nonnegative-gate", powers, xx=.001)
        low["match"]["gate_top_v"] = [-.1]
        high["match"]["gate_top_v"] = [0, .1]
        document["combination_scan"]["lockin_range_rules"] = [low, high]
        case.path.write_text(pem.toml_document(document))
        with self.assertRaisesRegex(ValueError, "continuous_gate_scan"):
            load_hardware_combination(case.path)
        self.assertEqual(case.manager.opened, [])


if __name__ == "__main__":
    unittest.main()
