from pathlib import Path
import unittest
from unittest.mock import mock_open, patch

from attodry_control.config import ConfigError, RunMode, load_config
from attodry_control.models import LockinRole
from attodry_control.sr830_settings import (
    ExternalReferenceEdge,
    InputCoupling,
    InputMode,
    SensitivityMode,
    ShieldGrounding,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SIMULATION_CONFIG = PROJECT_ROOT / "config" / "simulation.toml"
HARDWARE_EXAMPLE_CONFIG = PROJECT_ROOT / "config" / "hardware.example.toml"


class ConfigurationTests(unittest.TestCase):
    def simulation_text(self) -> str:
        return SIMULATION_CONFIG.read_text(encoding="utf-8")

    def load_text(self, text: str):
        with patch(
            "attodry_control.config.Path.open",
            mock_open(read_data=text.encode("utf-8")),
        ):
            return load_config("test.toml")

    def test_loads_complete_simulation_configuration(self) -> None:
        config = load_config(SIMULATION_CONFIG)

        self.assertEqual(config.project.mode, RunMode.SIMULATION)
        self.assertEqual(config.lockin_xx.role, LockinRole.XX)
        self.assertEqual(config.lockin_xy.role, LockinRole.XY)
        self.assertEqual(config.lockin_xx.input_mode, InputMode.A_MINUS_B)
        self.assertEqual(config.lockin_xx.shield_grounding, ShieldGrounding.FLOAT)
        self.assertEqual(config.lockin_xx.input_coupling, InputCoupling.AC)
        self.assertEqual(config.lockin_xx.time_constant_s, 0.3)
        self.assertEqual(config.lockin_xx.filter_slope_db_oct, 24)
        self.assertEqual(
            config.lockin_xx.sensitivity_mode, SensitivityMode.BOUNDED_AUTO
        )
        self.assertEqual(config.lockin_xx.sensitivity_full_scale_v, 0.01)
        self.assertEqual(config.lockin_xx.autorange_min_full_scale_v, 0.01)
        self.assertEqual(config.lockin_xx.autorange_max_full_scale_v, 0.02)
        self.assertEqual(config.lockin_xx.autorange_target_occupancy, 0.85)
        self.assertEqual(config.lockin_xx.autorange_stable_samples, 2)
        self.assertEqual(config.lockin_xx.autorange_max_steps, 1)
        self.assertEqual(config.lockin_xy.sensitivity_mode, SensitivityMode.FIXED)
        self.assertEqual(config.lockin_xy.sensitivity_full_scale_v, 0.001)
        self.assertEqual(config.lockin_xx.settle_time_constants, 5.0)
        self.assertIsNone(config.lockin_xx.external_reference_edge)
        self.assertEqual(
            config.lockin_xy.external_reference_edge, ExternalReferenceEdge.RISING
        )
        self.assertEqual(config.magnet.limits.experiment_vector_max_t, 3.0)
        self.assertIsNone(config.visa)

    def test_loads_hardware_template_without_opening_hardware(self) -> None:
        config = load_config(HARDWARE_EXAMPLE_CONFIG)

        self.assertEqual(config.project.mode, RunMode.HARDWARE)
        self.assertEqual(config.cryostat.backend, "legacy_dll")
        self.assertIsNotNone(config.visa)
        self.assertIsNone(config.gate_top.max_abs_voltage_v)
        with self.assertRaisesRegex(ConfigError, "Hardware configuration is not ready"):
            config.require_hardware_ready()

    def test_complete_hardware_values_pass_readiness_without_opening_hardware(self) -> None:
        text = HARDWARE_EXAMPLE_CONFIG.read_text(encoding="utf-8")
        replacements = {
            "CHANGE_ME_COM_PORT": "COM_TEST",
            "C:/CHANGE_ME/attoDRYxyz64bit.dll": "C:/vendor/attoDRYxyz64bit.dll",
            "CHANGE_ME_SR830_XX_VISA_ADDRESS": "GPIB0::8::INSTR",
            "CHANGE_ME_SR830_XY_VISA_ADDRESS": "GPIB0::9::INSTR",
            'model = "CHANGE_ME"': 'model = "TEST_SMU"',
            "CHANGE_ME_TOP_GATE_VISA_ADDRESS": "GPIB0::10::INSTR",
            "CHANGE_ME_BOTTOM_GATE_VISA_ADDRESS": "GPIB0::11::INSTR",
            'compliance_a = "CHANGE_ME"': "compliance_a = 1e-8",
            'leakage_limit_a = "CHANGE_ME"': "leakage_limit_a = 5e-9",
            'max_abs_voltage_v = "CHANGE_ME"': "max_abs_voltage_v = 1.0",
            'ramp_step_v = "CHANGE_ME"': "ramp_step_v = 0.05",
            'readback_tolerance_v = "CHANGE_ME"': "readback_tolerance_v = 0.001",
            'settle_s = "CHANGE_ME"': "settle_s = 0.1",
        }
        for old, new in replacements.items():
            text = text.replace(old, new)
        config = self.load_text(text)
        self.assertEqual(config.hardware_readiness_errors(), ())
        config.require_hardware_ready()

    def test_unknown_field_is_rejected(self) -> None:
        text = self.simulation_text().replace(
            'mode = "simulation"',
            'mode = "simulation"\nunknown_option = true',
        )

        with self.assertRaisesRegex(ConfigError, r"project.*unknown_option"):
            self.load_text(text)

    def test_missing_field_is_rejected(self) -> None:
        text = self.simulation_text().replace("stable_dwell_s = 30.0\n", "")

        with self.assertRaisesRegex(
            ConfigError, r"temperature_stability.*stable_dwell_s"
        ):
            self.load_text(text)

    def test_project_field_limit_cannot_be_raised_above_three_tesla(self) -> None:
        text = self.simulation_text().replace(
            "experiment_vector_max_t = 3.0",
            "experiment_vector_max_t = 3.1",
        )

        with self.assertRaisesRegex(ConfigError, "confirmed 3 T"):
            self.load_text(text)

    def test_lockin_roles_cannot_be_swapped(self) -> None:
        text = self.simulation_text().replace(
            'reference_source = "internal"',
            'reference_source = "external_ttl"',
            1,
        )

        with self.assertRaisesRegex(ConfigError, r"lockin_xx.*internal"):
            self.load_text(text)

    def test_sr830_source_cannot_be_configured_below_hardware_minimum(self) -> None:
        text = self.simulation_text().replace(
            "source_voltage_v = 0.004",
            "source_voltage_v = 0.002",
            1,
        )

        with self.assertRaisesRegex(ConfigError, "4 mVrms"):
            self.load_text(text)

    def test_lockin_rejects_unconfirmed_or_unsupported_fixed_settings(self) -> None:
        cases = (
            ('input_mode = "a_minus_b"', 'input_mode = "a"', "input_mode"),
            ('shield_grounding = "float"', 'shield_grounding = "ground"', "shield_grounding"),
            ('input_coupling = "ac"', 'input_coupling = "dc"', "input_coupling"),
            ("time_constant_s = 0.3", "time_constant_s = 1.0", "time_constant_s"),
            ("filter_slope_db_oct = 24", "filter_slope_db_oct = 12", "filter_slope"),
            (
                "sensitivity_full_scale_v = 0.01",
                "sensitivity_full_scale_v = 0.005",
                "sensitivity_full_scale_v",
            ),
        )
        for old, new, expected in cases:
            with self.subTest(field=expected):
                text = self.simulation_text().replace(old, new, 1)
                with self.assertRaisesRegex(ConfigError, expected):
                    self.load_text(text)

    def test_lockin_xy_requires_confirmed_ttl_rising_edge(self) -> None:
        text = self.simulation_text().replace(
            'external_reference_edge = "rising"',
            'external_reference_edge = "falling"',
        )
        with self.assertRaisesRegex(ConfigError, "external_reference_edge"):
            self.load_text(text)

    def test_lockin_rejects_settling_shorter_than_five_time_constants(self) -> None:
        text = self.simulation_text().replace(
            "settle_time_constants = 5.0", "settle_time_constants = 4.9", 1
        )
        with self.assertRaisesRegex(ConfigError, "at least 5.0"):
            self.load_text(text)

    def test_xy_bounded_autorange_is_rejected(self) -> None:
        text = self.simulation_text().replace(
            'sensitivity_mode = "fixed"', 'sensitivity_mode = "bounded_auto"', 1
        )
        with self.assertRaisesRegex(ConfigError, "lockin_xy"):
            self.load_text(text)

    def test_xx_autorange_rejects_unconfirmed_policy_values(self) -> None:
        cases = (
            ("autorange_min_full_scale_v = 0.01", "autorange_min_full_scale_v = 0.005"),
            ("autorange_max_full_scale_v = 0.02", "autorange_max_full_scale_v = 0.05"),
            ("autorange_target_occupancy = 0.85", "autorange_target_occupancy = 1.0"),
            ("autorange_stable_samples = 2", "autorange_stable_samples = 3"),
            ("autorange_max_steps = 1", "autorange_max_steps = 2"),
        )
        for old, new in cases:
            with self.subTest(field=old.split()[0]):
                with self.assertRaises(ConfigError):
                    self.load_text(self.simulation_text().replace(old, new, 1))

    def test_hardware_only_table_is_rejected_in_simulation(self) -> None:
        text = (
            self.simulation_text()
            + '\n[visa]\nbackend = "default"\ntimeout_ms = 5000\n'
        )

        with self.assertRaisesRegex(ConfigError, r"top level.*visa"):
            self.load_text(text)

    def test_malformed_toml_is_reported_as_configuration_error(self) -> None:
        with self.assertRaisesRegex(ConfigError, "Invalid TOML"):
            self.load_text('[project\nmode = "simulation"')


if __name__ == "__main__":
    unittest.main()
