"""Portable templates parse offline and never supply guessed station settings."""
from copy import deepcopy
from pathlib import Path
import shutil
import tempfile
import tomllib
import unittest

from tests.test_combination_photonics import toml_document
from attodry_control.combination_hardware import load_hardware_combination
from attodry_control.combination_launch import launch_summary


ROOT = Path(__file__).resolve().parents[1]


FAKE_VALUES = {
    "CHANGE_ME_XX_VISA_RESOURCE": "FAKE::XX", "CHANGE_ME_XY_VISA_RESOURCE": "FAKE::XY",
    "CHANGE_ME_REFERENCE_FREQUENCY_HZ": 100.0,
    "CHANGE_ME_XX_FULL_SCALE_V": 0.01, "CHANGE_ME_XY_FULL_SCALE_V": 0.01,
    "CHANGE_ME_CONFIRMED_SERIES_RESISTANCE_OHM": 100000.0,
    "CHANGE_ME_ESTIMATED_SAMPLE_RESISTANCE_OHM": 100.0,
    "CHANGE_ME_APPROVED_SAMPLE_CURRENT_A_RMS": 1e-4,
    "CHANGE_ME_APPROVED_SAMPLE_VOLTAGE_V_RMS": 0.1,
    "CHANGE_ME_APPROVED_NKT_SOURCE_MAX_PCT": 40.0,
    "CHANGE_ME_APPROVED_NKT_SOURCE_MIN_PCT": 8.0,
    "CHANGE_ME_APPROVED_NKT_STEP_PCT": 2.0,
    "CHANGE_ME_APPROVED_START_PCT": 8.0,
    "CHANGE_ME_WAVELENGTH_NM": 630.0, "CHANGE_ME_BANDWIDTH_NM": 10.0,
    "CHANGE_ME_NKT_SOURCE_MODULE_ADDRESS": 1, "CHANGE_ME_VARIA_MODULE_ADDRESS": 2,
    "CHANGE_ME_TARGET_POWER_W": 5e-6,
    "CHANGE_ME_APPROVED_PM_MAX_POWER_W": 0.01,
    "CHANGE_ME_APPROVED_FEEDBACK_MAX_POWER_W": 0.01,
    "CHANGE_ME_CONFIRMED_MINIMUM_VALID_ILLUMINATION_W": 1e-9,
    "CHANGE_ME_APPROVED_POWER_VARIATION_W": 1e-7,
    "CHANGE_ME_CONFIRMED_NOMINAL_DIODE_WAVELENGTH_NM": 730.0,
    "CHANGE_ME_APPROVED_START_CURRENT_A": 0.01,
    "CHANGE_ME_APPROVED_DIODE_MAX_CURRENT_A": 0.05,
    "CHANGE_ME_APPROVED_TEC_MAX_CURRENT_A": 0.5,
    "CHANGE_ME_APPROVED_DIODE_TEMPERATURE_MIN_C": 20.0,
    "CHANGE_ME_APPROVED_DIODE_TEMPERATURE_MAX_C": 30.0,
    "CHANGE_ME_DIODE_TEMPERATURE_TARGET_C": 25.0,
    "CHANGE_ME_APPROVED_TEMPERATURE_TOLERANCE_C": 0.1,
    "CHANGE_ME_CONFIRMED_CURRENT_READBACK_TOLERANCE_A": 1e-5,
    "CHANGE_ME_APPROVED_FEEDBACK_MIN_CURRENT_A": 0.001,
    "CHANGE_ME_APPROVED_FEEDBACK_CURRENT_STEP_A": 0.002,
    "CHANGE_ME_CONFIRMED_CHOPPER_MODEL": "MC2000B",
    "CHANGE_ME_CONFIRMED_INSTALLED_BLADE": "MC1F10",
    "CHANGE_ME_CONFIRMED_BEAM_TRACK": "single",
    "CHANGE_ME_EXACT_CHOPPER_ID_QUERY_RESPONSE": "Thorlabs MC2000B FAKE",
}


def substitute(value):
    if isinstance(value, dict):
        return {key: substitute(item) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute(item) for item in value]
    if isinstance(value, str) and value.startswith("CHANGE_ME"):
        return FAKE_VALUES.get(value, "FAKE_" + value.removeprefix("CHANGE_ME_"))
    return value


class PhotonicsChopperTemplateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.path = self.directory / "hardware.local.toml"
        shutil.copyfile(ROOT / "config/lockin_safety.toml", self.directory / "lockin_safety.toml")

    def document(self, source="nkt"):
        name = "photonics_cld.example.toml" if source == "cld" else "photonics_chopper.example.toml"
        return tomllib.loads((ROOT / "config" / name).read_text(encoding="utf-8"))

    def load(self, doc):
        self.path.write_text(toml_document(doc), encoding="utf-8")
        return load_hardware_combination(self.path)

    def test_templates_have_valid_toml_but_unfilled_values_fail_before_io(self):
        for source in ("nkt", "cld"):
            with self.subTest(source=source):
                doc = self.document(source)
                self.assertFalse(doc["chopper"]["enabled"])
                self.assertIn("CHANGE_ME", doc["lockin_xx"]["frequency_hz"])
                with self.assertRaises(ValueError):
                    self.load(doc)

    def test_both_filled_templates_load_and_preview_same_h1_roles_without_hardware(self):
        for source in ("nkt", "cld"):
            with self.subTest(source=source):
                config = self.load(substitute(self.document(source)))
                summary = launch_summary(config, self.directory / "unused.sqlite", "preview")
                self.assertEqual(summary["total_conditions"], 1)
                self.assertEqual(summary["lockin"]["harmonics_by_role"], {"xx": [1], "xy": [1]})
                self.assertFalse(config.lockin.lockin_xx.sine_output_connected)
                self.assertFalse(config.lockin.lockin_xy.sine_output_connected)
                self.assertFalse(config.optical.scan.use_pem)
                self.assertEqual(config.optical.scan.laser_source, source)
                self.assertFalse((self.directory / "unused.sqlite").exists())
                self.assertFalse(config.optical.background.enabled)

    def test_enabled_chopper_and_sample_excitation_declaration_preview(self):
        for source in ("nkt", "cld"):
            doc = substitute(self.document(source))
            doc["chopper"].update(enabled=True, manual_phase_lock_confirmed=True)
            doc["lockin_xx"]["sine_output_connected"] = True
            config = self.load(doc)
            summary = launch_summary(config, self.directory / "unused.sqlite", "preview")
            self.assertEqual(summary["optical"]["chopper"]["reference_track"], "single")
            self.assertEqual(config.optical.chopper.target_frequency(100), 100)
            self.assertTrue(config.snapshot["hardware"]["lockin"]["sample_excitation_connected"])

    def test_cld1015_980nm_preview_preserves_explicit_model_and_current_limits(self):
        doc = substitute(self.document("cld"))
        doc["cld1010"].update(expected_model="CLD1015", wavelength_nm=980.0)
        config = self.load(doc)
        self.assertEqual(config.optical.nkt.expected_model, "CLD1015")
        self.assertEqual(config.optical.nkt.points[0].wavelength_nm, 980)
        self.assertEqual(config.optical.nkt.max_current_a, doc["cld1010"]["max_current_a"])
        archived = config.snapshot["hardware"]["optical"]["nkt"]
        self.assertEqual(archived["expected_model"], "CLD1015")
        self.assertEqual(archived["points"][0]["wavelength_nm"], 980)
        self.assertFalse((self.directory / "unused.sqlite").exists())

    def test_nkt_spectral_arrays_ranges_power_grid_and_explicit_starts(self):
        doc = substitute(self.document())
        doc["nkt_run"]["points"] = [{"source_level_pct": 8.0,
            "wavelength_ranges": [{"min": 532.0, "max": 632.0, "scale": "linear", "step": 50.0}],
            "bandwidth_points_nm": [10.0, 20.0]}]
        doc["nkt_run"]["grid_order"] = ["bandwidth", "wavelength", "source"]
        feedback = doc["power_feedback"]
        feedback.pop("target_powers_w")
        feedback["target_power_ranges"] = [{"min": 50e-6, "max": 3e-3, "scale": "linear", "points": 6}]
        feedback.update(initial_current_mode="grid", initial_source_levels_pct=[[8.0] * 6 for _ in range(6)])
        config = self.load(doc)
        self.assertEqual(len(config.optical.nkt.points), 6)
        self.assertEqual(len(config.optical.points), 36)
        self.assertEqual(config.optical.nkt.points[3].bandwidth_nm, 20.0)
        self.assertEqual(config.optical.nkt.points[3].wavelength_nm, 532.0)
        self.assertEqual(len(config.plan.conditions()), 36)
        # These are alternative source-coordinate syntaxes, never simultaneous.
        doc["nkt_run"]["points"] = [{"source_level_ranges": [{"min": 8.0, "max": 10.0,
            "scale": "linear", "points": 2}], "wavelength_nm": 630.0,
            "bandwidth_ranges": [{"min": 10.0, "max": 20.0, "scale": "linear", "points": 2}]}]
        feedback.update(initial_source_levels_pct=[[8.0] * 6 for _ in range(4)])
        self.assertEqual(len(self.load(doc).optical.points), 24)

    def test_all_nkt_initial_modes_and_power_control_example_use_strict_loader(self):
        for mode in ("point", "per_target", "previous", "grid"):
            with self.subTest(mode=mode):
                doc = substitute(self.document())
                feedback = doc["power_feedback"]
                feedback.update(target_powers_w=[5e-6, 20e-6], initial_current_mode=mode)
                if mode == "per_target":
                    feedback["initial_source_levels_pct"] = [8.0, 12.0]
                elif mode == "grid":
                    feedback["initial_source_levels_pct"] = [[8.0, 12.0]]
                self.assertEqual(len(self.load(doc).optical.points), 2)
        doc = substitute(self.document())
        doc["nkt_source"]["control"] = "power"
        doc["power_feedback"]["actuator"] = "source_power"
        for suffix in ("min_pct", "max_pct", "step_pct"):
            doc["power_feedback"]["source_power_" + suffix] = doc["power_feedback"].pop("source_current_" + suffix)
        self.assertEqual(self.load(doc).optical.nkt.source_control, "power")

    def test_relative_window_retry_and_background_options_load_without_relaxing_limits(self):
        doc = substitute(self.document())
        doc["optical_scan"].update(max_power_retries=3, power_retry_timeout_s=60.0)
        doc["optical_background"].update(enabled=True, samples=2, max_age_s=20.0)
        doc["power_feedback"].update(tuning_tolerance_fraction=0.02)
        doc["power_feedback"]["window"].update(max_peak_to_peak_fraction=0.05,
            noise_floor_w=1e-9, max_drift_fraction_per_s=0.01)
        config = self.load(doc)
        self.assertEqual(config.optical.scan.max_power_retries, 3)
        self.assertEqual(config.optical.scan.feedback.max_power_w, 0.01)
        self.assertEqual(config.optical.scan.feedback.window.variation_limit_w(5e-6), 1e-7)
        self.assertTrue(config.optical.background.enabled)
        bad = deepcopy(doc)
        bad["chopper"].update(enabled=True, backend="simulation")
        with self.assertRaises(ValueError):
            self.load(bad)


if __name__ == "__main__":
    unittest.main()
