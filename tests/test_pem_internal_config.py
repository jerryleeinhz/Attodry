from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from attodry_control.pem_internal_config import load_diagnostic_config
from attodry_control.nkt_config import OTHER_TABLES
from attodry_control.config import OPTICAL_CONFIG_TABLES


EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "pem_internal_diagnostic.example.toml"


class PemInternalConfigTests(unittest.TestCase):
    def _load_text(self, content):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "diagnostic.toml"
            path.write_text(content, encoding="utf-8")
            return load_diagnostic_config(path)

    def test_standalone_example_reuses_station_without_loading_hardware(self):
        config = load_diagnostic_config(EXAMPLE)
        self.assertEqual(config.station_path, EXAMPLE.parent / "photonics.local.toml")
        self.assertEqual(config.internal_frequencies_hz, (50000.0,))
        self.assertEqual(config.harmonics, (1, 2))
        self.assertFalse(config.xx_excitation_disconnected)
        self.assertFalse(config.capture_buffer_available)
        self.assertEqual(config.snapshot()["station_path"], str(config.station_path))

    def test_unified_file_uses_itself_and_allowlists_accept_new_table(self):
        text = EXAMPLE.read_text(encoding="utf-8").replace('station_config = "photonics.local.toml"', '')
        config = self._load_text(text + '\n[project]\nname="offline"\n')
        self.assertEqual(config.station_path, config.config_path)
        self.assertIn("pem_internal_diagnostic", OTHER_TABLES)
        self.assertIn("pem_internal_diagnostic", OPTICAL_CONFIG_TABLES)

    def test_schema_unknown_missing_table_and_key_fail(self):
        source = EXAMPLE.read_text(encoding="utf-8")
        for text in (
            source.replace("schema_version = 1", "schema_version = 2"),
            source.replace("schema_version = 1", "schema_version = true"),
            source.replace("schema_version = 1", ""),
            source + "\nextra_unknown = true\n",
            source + "\n[unknown_instrument]\na=1\n",
            "[project]\nname='only'\n",
        ):
            with self.subTest(text=text[-100:]), self.assertRaises(ValueError):
                self._load_text(text)

    def test_arrays_reject_empty_duplicate_boolean_fractional_and_unmapped_tau(self):
        source = EXAMPLE.read_text(encoding="utf-8")
        for before, after in (
            ("harmonics = [1, 2]", "harmonics = []"),
            ("harmonics = [1, 2]", "harmonics = [1, 1]"),
            ("harmonics = [1, 2]", "harmonics = [true]"),
            ("harmonics = [1, 2]", "harmonics = [1.0]"),
            ("harmonics = [1, 2]", "harmonics = [100]"),
            ("optical_point_indices = [0]", "optical_point_indices = [-1]"),
            ("internal_frequencies_hz = [50000.0]", "internal_frequencies_hz = [nan]"),
            ("internal_frequencies_hz = [50000.0]", "internal_frequencies_hz = [true]"),
            ("time_constants_s = [0.001, 0.0001]", "time_constants_s = [0.002]"),
        ):
            with self.subTest(after=after), self.assertRaises(ValueError):
                self._load_text(source.replace(before, after))

    def test_numeric_windows_and_boolean_declarations_fail_closed(self):
        config = load_diagnostic_config(EXAMPLE)
        for change in (
            {"capture_duration_s": 0}, {"capture_duration_s": float("inf")},
            {"minimum_capture_rate_hz": True}, {"minimum_capture_rate_hz": 0.01},
            {"capture_timeout_s": 30}, {"monitor_interval_s": 31},
            {"pem_frequency_observation_s": -1}, {"pem_frequency_observation_s": 0.5},
            {"include_pem_frequency": 1}, {"xx_excitation_disconnected": "true"},
            {"capture_buffer_available": 1},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                replace(config, **change).validate()
        self.assertEqual(replace(config, pem_frequency_observation_s=0).validate().pem_frequency_observation_s, 0)

    def test_harmonic_detection_boundary_is_strict(self):
        config = load_diagnostic_config(EXAMPLE)
        replace(config, internal_frequencies_hz=(1999999.0,), harmonics=(2,)).validate()
        with self.assertRaises(ValueError):
            replace(config, internal_frequencies_hz=(2000000.0,), harmonics=(2,)).validate()


if __name__ == "__main__":
    unittest.main()
