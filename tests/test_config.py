from pathlib import Path
import unittest
from unittest.mock import mock_open, patch

from attodry_control.config import (
    ConfigError,
    RunMode,
    load_config,
    load_temperature_operation_config,
)
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
        self.assertEqual(config.lockin_xx.sensitivity_mode, SensitivityMode.FIXED)
        self.assertEqual(config.lockin_xx.sensitivity_full_scale_v, 0.02)
        self.assertIsNone(config.lockin_xx.autorange_min_full_scale_v)
        self.assertIsNone(config.lockin_xx.autorange_max_full_scale_v)
        self.assertIsNone(config.lockin_xx.autorange_target_occupancy)
        self.assertIsNone(config.lockin_xx.autorange_stable_samples)
        self.assertIsNone(config.lockin_xx.autorange_max_steps)
        self.assertEqual(config.lockin_xy.sensitivity_mode, SensitivityMode.FIXED)
        self.assertEqual(config.lockin_xy.sensitivity_full_scale_v, 0.001)
        self.assertEqual(config.lockin_xx.settle_time_constants, 5.0)
        self.assertIsNone(config.lockin_xx.external_reference_edge)
        self.assertEqual(
            config.lockin_xy.external_reference_edge, ExternalReferenceEdge.RISING
        )
        self.assertEqual(config.magnet.limits.experiment_vector_max_t, 3.0)
        self.assertEqual(config.lockin_sweep.frequency_harmonics, (1, 2, 3))
        self.assertEqual(config.lockin_sweep.excitation_harmonics, (1, 2, 3))
        self.assertEqual(len(config.lockin_sweep.frequency_points_hz), 10)
        self.assertEqual(config.lockin_sweep.frequency_points_hz[-1], 100000.0)
        self.assertEqual(config.lockin_sweep.excitation_points_v_rms[-1], 0.4)
        self.assertEqual(config.lockin_sweep.run_name, "simulation")
        self.assertEqual(config.lockin_sweep.note, "Simulation fixture.")
        self.assertEqual(config.lockin_sweep.external_series_resistance_ohm, 100000.0)
        self.assertEqual(config.lockin_sweep.approximate_device_resistance_ohm, 500.0)
        self.assertEqual(config.lockin_sweep.maximum_device_resistance_ohm, 500.0)
        self.assertFalse(config.lockin_sweep.external_50_ohm_termination)
        self.assertEqual(
            config.lockin_sweep.output_directory,
            Path("../run_data/commissioning"),
        )
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
                "sensitivity_full_scale_v = 0.020",
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

    def test_xy_bounded_autorange_is_allowed_with_complete_policy(self) -> None:
        text = self.simulation_text().replace(
            'sensitivity_mode = "fixed"\nsensitivity_full_scale_v = 0.001',
            "\n".join(
                (
                    'sensitivity_mode = "bounded_auto"',
                    "sensitivity_full_scale_v = 0.001",
                    "autorange_min_full_scale_v = 0.001",
                    "autorange_max_full_scale_v = 0.01",
                    "autorange_target_occupancy = 0.85",
                    "autorange_stable_samples = 2",
                    "autorange_max_steps = 1",
                )
            ),
            1,
        )
        config = self.load_text(text)
        self.assertEqual(
            config.lockin_xy.sensitivity_mode, SensitivityMode.BOUNDED_AUTO
        )
        self.assertEqual(config.lockin_xy.autorange_min_full_scale_v, 0.001)
        self.assertEqual(config.lockin_xy.autorange_max_full_scale_v, 0.01)

    def test_xy_bounded_autorange_rejects_nonadjacent_pair(self) -> None:
        text = self.simulation_text().replace(
            'sensitivity_mode = "fixed"\nsensitivity_full_scale_v = 0.001',
            "\n".join(
                (
                    'sensitivity_mode = "bounded_auto"',
                    "sensitivity_full_scale_v = 0.001",
                    "autorange_min_full_scale_v = 0.001",
                    "autorange_max_full_scale_v = 0.02",
                    "autorange_target_occupancy = 0.85",
                    "autorange_stable_samples = 2",
                    "autorange_max_steps = 1",
                )
            ),
            1,
        )
        with self.assertRaisesRegex(ConfigError, "adjacent pair"):
            self.load_text(text)

    def test_bounded_autorange_rejects_role_swapped_bounds(self) -> None:
        xx_text = self.simulation_text().replace(
            'sensitivity_mode = "fixed"\nsensitivity_full_scale_v = 0.020',
            "\n".join(
                (
                    'sensitivity_mode = "bounded_auto"',
                    "sensitivity_full_scale_v = 0.001",
                    "autorange_min_full_scale_v = 0.001",
                    "autorange_max_full_scale_v = 0.01",
                    "autorange_target_occupancy = 0.85",
                    "autorange_stable_samples = 2",
                    "autorange_max_steps = 1",
                )
            ),
            1,
        )
        xy_text = self.simulation_text().replace(
            'sensitivity_mode = "fixed"\nsensitivity_full_scale_v = 0.001',
            "\n".join(
                (
                    'sensitivity_mode = "bounded_auto"',
                    "sensitivity_full_scale_v = 0.01",
                    "autorange_min_full_scale_v = 0.01",
                    "autorange_max_full_scale_v = 0.02",
                    "autorange_target_occupancy = 0.85",
                    "autorange_stable_samples = 2",
                    "autorange_max_steps = 1",
                )
            ),
            1,
        )
        with self.assertRaisesRegex(ConfigError, r"lockin_xx bounded_auto range"):
            self.load_text(xx_text)
        with self.assertRaisesRegex(ConfigError, r"lockin_xy bounded_auto range"):
            self.load_text(xy_text)

    def test_bounded_autorange_rejects_unconfirmed_policy_values(self) -> None:
        text = self.simulation_text().replace(
            'sensitivity_mode = "fixed"\nsensitivity_full_scale_v = 0.020',
            "\n".join(
                (
                    'sensitivity_mode = "bounded_auto"',
                    "sensitivity_full_scale_v = 0.01",
                    "autorange_min_full_scale_v = 0.01",
                    "autorange_max_full_scale_v = 0.02",
                    "autorange_target_occupancy = 0.85",
                    "autorange_stable_samples = 2",
                    "autorange_max_steps = 1",
                )
            ),
            1,
        )
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
                    self.load_text(text.replace(old, new, 1))

    def test_hardware_only_table_is_rejected_in_simulation(self) -> None:
        text = (
            self.simulation_text()
            + '\n[visa]\nbackend = "default"\ntimeout_ms = 5000\n'
        )

        with self.assertRaisesRegex(ConfigError, r"top level.*visa"):
            self.load_text(text)

    def test_hardware_temperature_run_rejects_unknown_field(self) -> None:
        text = HARDWARE_EXAMPLE_CONFIG.read_text(encoding="utf-8").replace(
            "target_k = 1.8",
            "target_k = 1.8\nunknown_option = true",
        )
        with self.assertRaisesRegex(ConfigError, r"temperature_run.*unknown_option"):
            with patch(
                "attodry_control.config.Path.open",
                mock_open(read_data=text.encode("utf-8")),
            ):
                load_temperature_operation_config("test.toml")

    def test_hardware_temperature_run_rejects_overshoot_above_limit(self) -> None:
        text = HARDWARE_EXAMPLE_CONFIG.read_text(encoding="utf-8").replace(
            "target_k = 1.8",
            "target_k = 299.9",
        )
        with self.assertRaisesRegex(ConfigError, "max_overshoot_k"):
            with patch(
                "attodry_control.config.Path.open",
                mock_open(read_data=text.encode("utf-8")),
            ):
                load_temperature_operation_config("test.toml")

    def test_loads_temperature_operation_without_parsing_lockin_fields(self) -> None:
        text = HARDWARE_EXAMPLE_CONFIG.read_text(encoding="utf-8")
        for field in (
            'input_mode = "a_minus_b"\n',
            'shield_grounding = "float"\n',
            'input_coupling = "ac"\n',
            "time_constant_s = 0.3\n",
            "filter_slope_db_oct = 24\n",
            'sensitivity_mode = "fixed"\n',
            'sensitivity_mode = "bounded_auto"\n',
            "sensitivity_full_scale_v = 0.001\n",
            "sensitivity_full_scale_v = 0.01\n",
            "autorange_min_full_scale_v = 0.01\n",
            "autorange_max_full_scale_v = 0.02\n",
            "autorange_target_occupancy = 0.85\n",
            "autorange_stable_samples = 2\n",
            "autorange_max_steps = 1\n",
            "settle_time_constants = 5.0\n",
            'external_reference_edge = "rising"\n',
        ):
            text = text.replace(field, "")
        with patch(
            "attodry_control.config.Path.open",
            mock_open(read_data=text.encode("utf-8")),
        ):
            config = load_temperature_operation_config("test.toml")
        self.assertEqual(config.temperature_run.target_k, 1.8)
        self.assertEqual(config.temperature_run.max_delta_k, 250.0)
        self.assertEqual(config.temperature_run.max_overshoot_k, 0.2)

    def test_malformed_toml_is_reported_as_configuration_error(self) -> None:
        with self.assertRaisesRegex(ConfigError, "Invalid TOML"):
            self.load_text('[project\nmode = "simulation"')

    def test_lockin_sweep_rejects_external_50_ohm_termination(self) -> None:
        text = self.simulation_text().replace(
            "external_50_ohm_termination = false",
            "external_50_ohm_termination = true",
        )
        with self.assertRaisesRegex(ConfigError, "must be false"):
            self.load_text(text)

    def test_lockin_sweep_rejects_resistance_upper_bound_below_approximation(self) -> None:
        text = self.simulation_text().replace(
            "maximum_device_resistance_ohm = 500.0",
            "maximum_device_resistance_ohm = 499.0",
        )
        with self.assertRaisesRegex(ConfigError, "maximum_device_resistance_ohm"):
            self.load_text(text)

    def test_lockin_sweep_accepts_independent_harmonic_combinations(self) -> None:
        text = self.simulation_text().replace(
            "frequency_harmonics = [1, 2, 3]",
            "frequency_harmonics = [1, 3]",
        ).replace(
            "excitation_harmonics = [1, 2, 3]",
            "excitation_harmonics = [2]",
        )

        config = self.load_text(text)

        self.assertEqual(config.lockin_sweep.frequency_harmonics, (1, 3))
        self.assertEqual(config.lockin_sweep.excitation_harmonics, (2,))

    def test_lockin_sweep_legacy_harmonics_apply_to_both_scan_types(self) -> None:
        text = self.simulation_text().replace(
            "frequency_harmonics = [1, 2, 3]\nexcitation_harmonics = [1, 2, 3]",
            "harmonics = [1, 3]",
        )

        config = self.load_text(text)

        self.assertEqual(config.lockin_sweep.frequency_harmonics, (1, 3))
        self.assertEqual(config.lockin_sweep.excitation_harmonics, (1, 3))

    def test_lockin_sweep_rejects_invalid_or_mixed_harmonic_lists(self) -> None:
        invalid_lists = ("[1, 1]", "[2, 1]", "[4]")
        for invalid in invalid_lists:
            with self.subTest(invalid=invalid):
                text = self.simulation_text().replace(
                    "frequency_harmonics = [1, 2, 3]",
                    f"frequency_harmonics = {invalid}",
                )
                with self.assertRaisesRegex(ConfigError, "combination of 1, 2, and 3"):
                    self.load_text(text)

        mixed = self.simulation_text().replace(
            "frequency_harmonics = [1, 2, 3]",
            "harmonics = [1]\nfrequency_harmonics = [1, 2, 3]",
        )
        with self.assertRaisesRegex(ConfigError, "cannot be combined"):
            self.load_text(mixed)

    def test_lockin_sweep_rejects_unsafe_run_name_or_blank_note(self) -> None:
        cases = (
            ('run_name = "simulation"', 'run_name = "sample/one"', "run_name"),
            ('run_name = "simulation"', 'run_name = " sample"', "run_name"),
            ('note = "Simulation fixture."', 'note = ""', "note"),
        )
        for old, new, expected in cases:
            with self.subTest(field=expected, value=new):
                with self.assertRaisesRegex(ConfigError, expected):
                    self.load_text(self.simulation_text().replace(old, new, 1))

    def test_lockin_sweep_rejects_rooted_output_directory(self) -> None:
        text = self.simulation_text().replace(
            'output_directory = "../run_data/commissioning"',
            'output_directory = "C:/outside"',
        )
        with self.assertRaisesRegex(ConfigError, "non-rooted relative directory"):
            self.load_text(text)

    def test_lockin_sweep_rejects_config_directory_as_output_directory(self) -> None:
        text = self.simulation_text().replace(
            'output_directory = "../run_data/commissioning"',
            'output_directory = "."',
        )
        with self.assertRaisesRegex(ConfigError, "non-rooted relative directory"):
            self.load_text(text)


if __name__ == "__main__":
    unittest.main()
