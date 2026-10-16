"""Pure modular grid, identities and safety tests: never open instruments."""
from copy import deepcopy
import json
from pathlib import Path
import re
import tempfile
import tomllib
import unittest

from attodry_control.combination_plan import AxisPoint, CombinationPlan, ScanAxis
from attodry_control.modular_config import load_modular_config, parse_modular_config
from attodry_control.nkt_config import NktError
from attodry_control.scan_grid import expand_grid, expand_ranges


def optical_document():
    return {"combination_scan": {"order": ["optics", "nanovoltmeter"],
                "samples_per_condition": 3},
            "optics": {"laser": "nkt", "use_pem": False, "use_power_meter": True,
                       "mode": "power_stabilized", "initial_source_level_pct": 32.},
            "optics_scan": {"order": ["wavelength", "bandwidth", "power"],
                "wavelength_ranges_nm": [{"min": 530., "max": 820., "scale": "linear", "points": 10}],
                "bandwidth_points_nm": [10.], "power_ranges_w": [
                    {"min": 10e-6, "max": 90e-6, "scale": "linear", "points": 9}],
                "dark_first": True},
            "nanovoltmeter": {"channel": 2, "voltage_range_v": 10., "nplc": 1.}}


class ScanGridTests(unittest.TestCase):
    def test_inclusive_step_and_named_segments_keep_duplicates(self):
        rows = expand_ranges([{"min": .1, "max": .3, "step": .1, "scale": "linear", "name": "first"},
                              {"min": .3, "max": .4, "points": 2, "scale": "linear"}], "grid")
        self.assertEqual([r.value for r in rows], [.1, .2, .3, .3, .4])
        self.assertEqual([r.segment_index for r in rows], [0, 0, 0, 1, 1])
        self.assertEqual(rows[0].segment, "first")

    def test_log_endpoints_are_exact(self):
        rows = expand_ranges([{"min": 1e-9, "max": 1e-6, "points": 4, "scale": "log"}], "grid")
        self.assertEqual(rows[0].value, 1e-9)
        self.assertEqual(rows[-1].value, 1e-6)
        self.assertAlmostEqual(rows[1].value, 1e-8)

    def test_explicit_order_and_duplicate_indices(self):
        rows = expand_grid({"p": [2., 1., 1., 2.]}, "p", "r", "scan")
        self.assertEqual([r.value for r in rows], [2., 1., 1., 2.])
        self.assertEqual([r.point_index for r in rows], list(range(4)))

    def test_invalid_ranges_and_conflicting_sources_fail(self):
        invalid = [
            {"min": 1, "max": 2, "scale": "linear", "step": .3},
            {"min": 1, "max": 2, "scale": "linear", "points": 2, "step": 1},
            {"min": 0, "max": 2, "scale": "log", "points": 2},
            {"min": 1, "max": 2, "scale": "log", "step": 1},
            {"min": 2, "max": 1, "scale": "linear", "points": 2},
            {"min": 1, "max": 2, "scale": "linear", "points": True},
            {"min": 1, "max": 2, "scale": "linear", "points": 10001},
            {"min": 1, "max": 2, "scale": "linear", "step": 1e-300},
        ]
        for segment in invalid:
            with self.subTest(segment=segment), self.assertRaises(ValueError):
                expand_ranges([segment], "grid")
        with self.assertRaises(NktError):
            expand_grid({"p": [1], "r": []}, "p", "r", "grid")


class ModularPlanTests(unittest.TestCase):
    def test_optics_generated_grid_dark_first_and_quantization_provenance(self):
        config = parse_modular_config(optical_document())
        plan = config.plan
        self.assertEqual(plan.condition_count, 100)
        self.assertEqual(plan.snapshot()["formal_sample_count"], 300)
        points = plan.axes[0].points
        for group in range(10):
            chunk = points[group*10:(group+1)*10]
            self.assertEqual(chunk[0].metadata["optical_state"], "dark")
            self.assertEqual(chunk[0].values["optical_target_power_w"], 0)
            self.assertEqual([p.metadata["power_index"] for p in chunk], list(range(10)))
            self.assertEqual({p.metadata["optical_group_index"] for p in chunk}, {group})
            self.assertTrue(all(p.metadata["optical_state"] == "light" for p in chunk[1:]))
        self.assertEqual(points[10].values["optical_wavelength_nm"], 562.2)
        self.assertAlmostEqual(points[10].metadata["ideal_wavelength_nm"], 562.2222222222222)
        self.assertEqual(config.document["optics_scan"]["wavelength_ranges_nm"][0]["max"], 820.)
        json.dumps(config.snapshot())

    def test_independent_temperature_field_optics_cartesian(self):
        doc = optical_document()
        doc["combination_scan"]["order"] = ["temperature", "magnetic", "optics", "nanovoltmeter"]
        doc["temperature_scan"] = {"points_k": [2., 3.]}
        doc["magnetic_scan"] = {"points": [{"bx_t": 0., "bz_t": z} for z in (0., .1, .2)]}
        plan = parse_modular_config(doc).plan
        self.assertEqual(plan.condition_count, 600)
        conditions = plan.conditions()
        self.assertEqual(len({plan.axis_visit(c, "temperature") for c in conditions}), 2)
        self.assertEqual(len({plan.axis_visit(c, "magnetic") for c in conditions}), 6)
        self.assertEqual(len({plan.axis_visit(c, "optics") for c in conditions}), 600)

    def test_no_optics_and_no_detector_modules_are_valid(self):
        for order, scans in ((["nanovoltmeter"], {}),
                (["current_source", "nanovoltmeter"], {"current_source_scan": {"points_a": [-1e-9, 0, 1e-9]}}),
                (["temperature"], {"temperature_scan": {"points_k": [2., 3.]}})):
            with self.subTest(order=order):
                doc = {"combination_scan": {"order": order}, **scans,
                       "optics": "unused-invalid-table", "nanovoltmeter": {}}
                config = parse_modular_config(doc)
                self.assertIsNone(config.optics)
                self.assertEqual(config.plan.order, tuple(order))

    def test_duplicate_coordinates_and_repeat_reenter_stability(self):
        plan = parse_modular_config({"combination_scan": {"order": ["temperature", "nanovoltmeter"], "repeats": 2},
                   "temperature_scan": {"points_k": [2., 2., 3.]}}).plan
        visits = [plan.axis_visit(c, "temperature") for c in plan.conditions()]
        self.assertEqual(len(visits), len(set(visits)))

    def test_temperature_wrap_check_counts_repeats_and_outer_axis(self):
        doc = {"combination_scan": {"order": ["temperature", "nanovoltmeter"]},
               "temperature_scan": {"points_k": [2., 3., 4.], "max_step_k": 1.1}}
        parse_modular_config(doc)
        doc["combination_scan"]["repeats"] = 2
        with self.assertRaisesRegex(NktError, "wrap-around"):
            parse_modular_config(doc)
        doc["combination_scan"] = {"order": ["magnetic", "temperature", "nanovoltmeter"]}
        doc["magnetic_scan"] = {"points": [{"bx_t": 0., "bz_t": 0.}, {"bx_t": 0., "bz_t": .1}]}
        with self.assertRaisesRegex(NktError, "wrap-around"):
            parse_modular_config(doc)

    def test_environment_must_be_outer_and_vector_invariant(self):
        with self.assertRaisesRegex(NktError, "precede"):
            parse_modular_config({"combination_scan": {"order": ["nanovoltmeter", "temperature"]},
                                 "temperature_scan": {"points_k": [2.]}})
        with self.assertRaisesRegex(ValueError, "project limit"):
            parse_modular_config({"combination_scan": {"order": ["magnetic"]},
                                 "magnetic_scan": {"points": [{"bx_t": 2.2, "bz_t": 2.2}]}})

    def test_temperature_bounds_and_sample_current_limit(self):
        with self.assertRaisesRegex(NktError, "bounds"):
            parse_modular_config({"combination_scan": {"order": ["temperature"]},
                "temperature_scan": {"points_k": [400.]},
                "cryostat": {"temperature_min_k": 1., "temperature_max_k": 300.}})
        with self.assertRaisesRegex(NktError, "sample current"):
            parse_modular_config({"combination_scan": {"order": ["current_source"]},
                "current_source_scan": {"points_a": [1e-7]}, "measurement": {"max_abs_current_a": 1e-8}})

    def test_magnetic_paired_and_cartesian_modes(self):
        doc = {"combination_scan": {"order": ["magnetic"]},
               "magnetic_scan": {"bx_points_t": [0, .1], "bz_points_t": [0, .2]}}
        self.assertEqual(parse_modular_config(doc).plan.condition_count, 4)
        doc["magnetic_scan"]["pattern"] = "paired"
        self.assertEqual(parse_modular_config(doc).plan.condition_count, 2)
        doc["magnetic_scan"]["bz_points_t"] = [0]
        self.assertEqual(parse_modular_config(doc).plan.condition_count, 2)
        doc["magnetic_scan"]["bz_points_t"] = [0, .1, .2]
        with self.assertRaisesRegex(NktError, "Paired"):
            parse_modular_config(doc)

    def test_smu_independent_coordinates_and_source_conflict(self):
        doc = {"combination_scan": {"order": ["smu"]},
               "smu_scan": {"gate_top_points_v": [-.1, 0, .1], "gate_bottom_points_v": [0., .1]}}
        self.assertEqual(parse_modular_config(doc).plan.condition_count, 6)
        doc["smu_scan"]["gate_top_points_a"] = [0.]
        with self.assertRaisesRegex(NktError, "voltage and current"):
            parse_modular_config(doc)

    def test_smu_reuses_existing_module_plan_without_a_second_grid(self):
        root = Path(__file__).resolve().parents[1]
        source = (root/"config/photoresponse_smu_nkt_simulation.toml").read_text(encoding="utf-8")
        source = re.sub(r"\[photoresponse_combination\][\s\S]*?(?=\n\[)",
            '[combination_scan]\norder=["smu"]\nbackend="simulation"\n', source, count=1)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"module.local.toml"
            path.write_text(source, encoding="utf-8")
            config = load_modular_config(path)
            points = config.plan.axes[0].points
            self.assertEqual(len(points), 2)
            self.assertEqual([p.values for p in points], [
                {"gate_top_v": 0., "gate_bottom_v": -.001},
                {"gate_top_v": 0., "gate_bottom_v": .001}])
            self.assertTrue(all(p.metadata["scan_source"] == "three_smu_run" for p in points))
            from attodry_control.modular_runtime import RuntimeConfig
            self.assertEqual(len(RuntimeConfig(config).smu.points), 2)
            changed = deepcopy(config.document)
            changed["gate_top"]["max_abs_voltage_v"] = .02
            with self.assertRaisesRegex(NktError, "differs"):
                parse_modular_config(changed, path)
            path.unlink()
            with self.assertRaisesRegex(NktError, "unchanged source"):
                parse_modular_config(config.document, path)

    def test_optics_duplicate_groups_preserve_identity(self):
        doc = optical_document()
        del doc["optics_scan"]["wavelength_ranges_nm"]
        doc["optics_scan"]["wavelength_points_nm"] = [630., 630.]
        plan = parse_modular_config(doc).plan
        self.assertEqual(plan.condition_count, 20)
        self.assertEqual(plan.axes[0].points[0].metadata["optical_group_index"], 0)
        self.assertEqual(plan.axes[0].points[10].metadata["optical_group_index"], 1)

    def test_direct_actuator_scan_has_one_dark_per_wave(self):
        doc = optical_document()
        doc["optics"].update(mode="direct", use_power_meter=False)
        doc["optics_scan"] = {"wavelength_points_nm": [630., 640.], "bandwidth_points_nm": [10.],
            "source_level_points_pct": [30., 40., 50.], "dark_first": True,
            "order": ["wavelength", "bandwidth", "source_level"]}
        points = parse_modular_config(doc).plan.axes[0].points
        self.assertEqual(len(points), 8)
        self.assertEqual([p.metadata["optical_state"] for p in points], ["dark", "light", "light", "light"]*2)
        self.assertEqual([p.values["optical_source_level_pct"] for p in points[1:4]], [30., 40., 50.])
        self.assertTrue(all("optical_target_power_w" not in p.values for p in points))

    def test_no_dark_power_outer_order_is_respected(self):
        doc = optical_document()
        doc["optics_scan"] = {"wavelength_points_nm": [630., 640.], "bandwidth_points_nm": [10.],
            "power_points_w": [10e-6, 20e-6], "order": ["power", "wavelength", "bandwidth"]}
        points = parse_modular_config(doc).plan.axes[0].points
        self.assertEqual([p.values["optical_target_power_w"] for p in points], [10e-6, 10e-6, 20e-6, 20e-6])
        doc["optics_scan"]["dark_first"] = True
        with self.assertRaisesRegex(NktError, "innermost"):
            parse_modular_config(doc)

    def test_optics_paired_wavelength_bandwidth_does_not_zip_power(self):
        doc = optical_document()
        doc["optics_scan"] = {"wavelength_points_nm": [630., 640.], "bandwidth_points_nm": [10., 20.],
            "power_points_w": [10e-6, 20e-6], "pattern": "paired", "dark_first": True,
            "order": ["wavelength", "bandwidth", "power"]}
        points = parse_modular_config(doc).plan.axes[0].points
        self.assertEqual(len(points), 6)
        self.assertEqual(points[3].values["optical_bandwidth_nm"], 20.)

    def test_cld_fixed_wavelength_and_current_seed(self):
        doc = optical_document()
        doc["optics"] = {"laser": "cld1010lp", "use_pem": False, "use_power_meter": True,
                         "mode": "power_stabilized", "initial_current_a": .01}
        doc["cld1010"] = {"wavelength_nm": 760., "max_current_a": .03}
        doc["optics_scan"] = {"wavelength_points_nm": [760.], "power_points_w": [10e-6, 20e-6],
                              "dark_first": True, "order": ["wavelength", "power"]}
        self.assertEqual(parse_modular_config(doc).plan.axes[0].points[0].values["optical_current_a"], .01)
        doc["optics_scan"] = {"power_points_w": [10e-6, 20e-6], "dark_first": True}
        self.assertEqual(parse_modular_config(doc).plan.condition_count, 3)
        doc["optics_scan"]["order"] = ["wavelength", "power"]
        doc["optics_scan"]["wavelength_points_nm"] = [760., 770.]
        with self.assertRaisesRegex(NktError, "exactly one"):
            parse_modular_config(doc)

    def test_requested_power_requires_feedback_and_explicit_seed(self):
        doc = optical_document()
        doc["optics"]["use_power_meter"] = False
        with self.assertRaisesRegex(NktError, "power meter"):
            parse_modular_config(doc)
        doc["optics"].update(mode="direct")
        with self.assertRaisesRegex(NktError, "Requested W"):
            parse_modular_config(doc)
        doc = optical_document()
        del doc["optics"]["initial_source_level_pct"]
        with self.assertRaisesRegex(NktError, "initial"):
            parse_modular_config(doc)

    def test_snapshot_is_detached_and_json_roundtrippable(self):
        source = optical_document()
        config = parse_modular_config(source)
        source["optics"]["laser"] = "bad"
        self.assertEqual(config.document["optics"]["laser"], "nkt")
        snapshot = config.snapshot()
        snapshot["original_config"]["optics"]["laser"] = "changed"
        self.assertEqual(config.document["optics"]["laser"], "nkt")
        json.dumps(snapshot)

    def test_unknown_duplicate_and_conflicting_configs_fail(self):
        for doc in ({"combination_scan": {"order": ["fake"]}},
                    {"combination_scan": {"order": ["optics", "optical"]}},
                    {"combination_scan": {"order": ["nanovoltmeter"]}, "unknown": {}},
                    {"combination_scan": {"order": ["nanovoltmeter"]}, "photoresponse_combination": {"order": []}}):
            with self.subTest(doc=doc), self.assertRaises(NktError):
                parse_modular_config(doc)

    def test_huge_cartesian_plan_is_rejected_before_materialization(self):
        with self.assertRaisesRegex(NktError, "conditions"):
            parse_modular_config({"combination_scan": {"order": ["current_source", "smu"]},
                "current_source_scan": {"points_a": [0.]*101},
                "smu_scan": {"gate_top_points_v": [0.]*101}})

    def test_loader_has_no_hardware_side_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"test.toml"
            path.write_text('[combination_scan]\norder=["nanovoltmeter"]\n', encoding="utf-8")
            config = load_modular_config(path)
            self.assertEqual(config.source_path, path.resolve())
            self.assertEqual(config.plan.condition_count, 1)

    def test_manual_plan_nanovoltmeter_is_singleton(self):
        with self.assertRaisesRegex(NktError, "singleton"):
            CombinationPlan((ScanAxis("nanovoltmeter", (AxisPoint({}), AxisPoint({}))),)).validate()


if __name__ == "__main__":
    unittest.main()
