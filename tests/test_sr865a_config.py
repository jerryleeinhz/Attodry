"""Strict electrical-role model configuration and offline launch previews."""
from copy import deepcopy
from pathlib import Path
import tomllib
import unittest
from unittest.mock import patch

from attodry_control.config import ConfigError, ReserveMode, load_config
from attodry_control.sr830_settings import SensitivityMode
from attodry_control.combination_launch import launch_summary
from attodry_control.combination_terminal import launch_text


ROOT = Path(__file__).resolve().parents[1]


class Sr865aConfigurationTests(unittest.TestCase):
    def document(self, model="SR865A"):
        document = tomllib.loads((ROOT / "config/simulation.toml").read_text(encoding="utf-8"))
        if model == "SR865A":
            xy = document["lockin_xy"]
            xy["model"] = model
            xy.pop("reserve_mode")
            xy["sr865a"] = {"input_range_v_peak": 0.3,
                "reference_input_impedance_ohm": 1_000_000.0,
                "current_status_supported": False, "sync_output_mode": "preserve"}
        return document

    def load(self, document):
        safety = tomllib.loads((ROOT / "config/lockin_safety.toml").read_text(encoding="utf-8"))
        with patch("attodry_control.config._load_document", side_effect=lambda path:
                   deepcopy(safety if Path(path).name == "lockin_safety.toml" else document)):
            return load_config("offline-model.toml")

    def test_legacy_dual_sr830_retains_reserve_and_needs_no_capability_table(self):
        config = self.load(self.document("SR830"))
        self.assertEqual(config.lockin_xx.model, "SR830")
        self.assertEqual(config.lockin_xy.model, "SR830")
        self.assertEqual(config.lockin_xy.reserve_mode, ReserveMode.NORMAL)
        self.assertIsNone(config.lockin_xy.sr865a)

    def test_sr865a_xy_preserves_distinct_input_range_and_output_full_scale(self):
        xy = self.load(self.document()).lockin_xy
        self.assertEqual(xy.model, "SR865A")
        self.assertEqual(xy.sr865a.input_range_v_peak, 0.3)
        self.assertEqual(xy.sensitivity_full_scale_v, 0.001)
        self.assertEqual(xy.sr865a.reference_input_impedance_ohm, 1_000_000.0)
        self.assertFalse(xy.sr865a.current_status_supported)
        self.assertEqual(xy.sr865a.sync_output_mode, "preserve")
        self.assertIsNone(xy.reserve_mode)

    def test_model_specific_time_constants_and_slopes_use_hardware_table(self):
        for value in (1e-6, 3e-6, 10e-6, 0.3, 30000.0):
            with self.subTest(time_constant_s=value):
                document = self.document()
                document["lockin_xy"].update(time_constant_s=value, filter_slope_db_oct=6)
                self.assertEqual(self.load(document).lockin_xy.time_constant_s, value)
        for field, value in (("time_constant_s", 2e-6), ("filter_slope_db_oct", 9)):
            with self.subTest(field=field):
                document = self.document()
                document["lockin_xy"][field] = value
                with self.assertRaises(ConfigError):
                    self.load(document)
        document = self.document("SR830")
        document["lockin_xy"]["time_constant_s"] = 1e-6
        with self.assertRaisesRegex(ConfigError, "SR830 OFLT"):
            self.load(document)

    def test_input_range_impedance_and_sync_reject_unknown_capabilities(self):
        for field, value in (("input_range_v_peak", 0.2), ("input_range_v_peak", True),
                             ("reference_input_impedance_ohm", 75.0),
                             ("current_status_supported", "false"),
                             ("sync_output_mode", "off")):
            with self.subTest(field=field, value=value):
                document = self.document()
                document["lockin_xy"]["sr865a"][field] = value
                with self.assertRaises(ConfigError):
                    self.load(document)

    def test_capability_flags_are_required_and_strict(self):
        for field in self.document()["lockin_xy"]["sr865a"]:
            with self.subTest(field=field):
                document = self.document()
                document["lockin_xy"]["sr865a"].pop(field)
                with self.assertRaisesRegex(ConfigError, field):
                    self.load(document)
        document = self.document()
        document["lockin_xy"]["sr865a"]["guess_current_status"] = True
        with self.assertRaisesRegex(ConfigError, "guess_current_status"):
            self.load(document)
        document["lockin_xy"].pop("sr865a")
        with self.assertRaisesRegex(ConfigError, "sr865a"):
            self.load(document)

    def test_xx_model_wiring_and_xy_source_capability_fail_closed(self):
        for role, field, value in (("lockin_xx", "model", "SR865A"),
                                  ("lockin_xy", "model", "SR865"),
                                  ("lockin_xy", "reference_source", "internal"),
                                  ("lockin_xy", "sine_output_connected", True),
                                  ("lockin_xy", "source_voltage_v", 3.0),
                                  ("lockin_xy", "frequency_hz", 100.0)):
            with self.subTest(role=role, field=field):
                document = self.document()
                document[role][field] = value
                with self.assertRaises(ConfigError):
                    self.load(document)

    def test_sr865a_rejects_reserve_at_baseline_and_harmonic_scope(self):
        document = self.document()
        document["lockin_xy"]["reserve_mode"] = "normal"
        with self.assertRaisesRegex(ConfigError, "reserve_mode"):
            self.load(document)
        document = self.document()
        document["lockin_xy"]["harmonic_settings"] = {"h2": {"reserve_mode": "normal"}}
        with self.assertRaisesRegex(ConfigError, "reserve_mode"):
            self.load(document)
        document = self.document("SR830")
        document["lockin_xy"]["sr865a"] = self.document()["lockin_xy"]["sr865a"]
        with self.assertRaisesRegex(ConfigError, "sr865a"):
            self.load(document)

    def test_full_scale_and_autorange_keep_existing_safety_allowlists(self):
        document = self.document()
        document["lockin_xy"]["sensitivity_full_scale_v"] = 0.5
        with self.assertRaisesRegex(ConfigError, "lockin_safety"):
            self.load(document)
        document = self.document()
        document["lockin_xy"].update(sensitivity_mode="bounded_auto",
            autorange_min_full_scale_v=.001, autorange_max_full_scale_v=.01,
            autorange_target_occupancy=.85, autorange_stable_samples=2)
        xy = self.load(document).lockin_xy
        self.assertEqual(xy.sensitivity_mode, SensitivityMode.BOUNDED_AUTO)
        self.assertEqual(xy.autorange_full_scales_v, (.001, .01))
        self.assertEqual(xy.sr865a.input_range_v_peak, .3)
        for field, value in (("autorange_max_full_scale_v", .1),
                             ("autorange_target_occupancy", .9),
                             ("autorange_stable_samples", 1)):
            bad = deepcopy(document)
            bad["lockin_xy"][field] = value
            with self.subTest(field=field), self.assertRaises(ConfigError):
                self.load(bad)

    def test_harmonic_fixed_and_auto_override_retain_model_semantics(self):
        document = self.document()
        document["lockin_xy"]["harmonic_settings"] = {
            "h1": {"sensitivity_full_scale_v": .01},
            "h2": {"sensitivity_mode": "bounded_auto", "sensitivity_full_scale_v": .002,
                "autorange_min_full_scale_v": .002, "autorange_max_full_scale_v": .01,
                "autorange_target_occupancy": .85, "autorange_stable_samples": 2}}
        xy = self.load(document).lockin_xy
        self.assertEqual([s.harmonic for s in xy.harmonic_settings], [1, 2])
        self.assertTrue(all(s.reserve_mode is None for s in xy.harmonic_settings))
        self.assertEqual(xy.harmonic_settings[1].autorange_full_scales_v, (.002, .01))
        document["lockin_xy"]["harmonic_settings"]["h1"]["sensitivity_full_scale_v"] = .5
        with self.assertRaisesRegex(ConfigError, "lockin_safety"):
            self.load(document)

    def test_segment_override_validates_approved_sr865a_full_scale(self):
        document = self.document()
        document["lockin_sweep"]["frequency_ranges"][0]["xy_full_scale_v"] = .002
        config = self.load(document)
        self.assertEqual(config.lockin_sweep.frequency_point_specs[0].xy_full_scale_v, .002)
        document["lockin_sweep"]["frequency_ranges"][0]["xy_full_scale_v"] = .5
        with self.assertRaisesRegex(ConfigError, "project-confirmed SR865A"):
            self.load(document)


class Sr865aPreviewTests(unittest.TestCase):
    def test_launch_preview_exposes_model_and_independent_capabilities_offline(self):
        from tests.test_combination_hardware import ElectricalCombinationTests
        from attodry_control.combination_hardware import load_hardware_combination
        fixture = ElectricalCombinationTests("test_only_requested_modules_open")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        prefix, xy = fixture.base.split("[lockin_xy]", 1)
        xy, suffix = xy.split("[lockin_sweep]", 1)
        xy = xy.replace('model = "SR830"', 'model = "SR865A"')
        xy = xy.replace('reserve_mode = "normal"', '')
        source = prefix + "[lockin_xy]" + xy + """
[lockin_xy.sr865a]
input_range_v_peak = 0.3
reference_input_impedance_ohm = 1000000.0
current_status_supported = false
sync_output_mode = "preserve"
""" + "[lockin_sweep]" + suffix
        fixture.write_config(order=("lockin",), source=source)
        config = load_hardware_combination(fixture.path)
        summary = launch_summary(config, fixture.database, "model-preview")
        xy = summary["lockin"]["roles"]["lockin_xy"]
        self.assertEqual(xy["model"], "SR865A")
        self.assertIsNone(xy["reserve_mode"])
        self.assertEqual(xy["sr865a"]["input_range_v_peak"], .3)
        self.assertEqual(xy["sensitivity_full_scale_v"], .01)
        self.assertFalse(xy["sr865a"]["current_status_supported"])
        self.assertEqual(summary["lockin"]["roles"]["lockin_xx"]["model"], "SR830")
        for width in (40, 70, 160):
            text = launch_text(summary, width)
            self.assertTrue(all(len(line) <= width for line in text.splitlines()))
        normalized = "".join(launch_text(summary, 160).split())
        for expected in ("SR865A", "IRNG", "Vpeak", "unsupported", "preserve", "n/a"):
            self.assertIn(expected, normalized)
        self.assertIn("XXSENS/Reserve;XYSCAL", normalized)
        self.assertEqual(fixture.manager.opened, [])
        self.assertFalse(fixture.database.exists())
