"""Exercise the shipped template through strict loaders, never real resources."""
import json
from pathlib import Path
import re
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from photonics_lockin_helpers import reversed_sine_document, reversed_sine_safety_document
from attodry_control.combination_hardware import load_hardware_combination


ROOT = Path(__file__).resolve().parents[1]


def synthetic_replacements():
    """Only test-fixture values; none are proposed sample or station settings."""
    lockin = reversed_sine_document()
    safety = reversed_sine_safety_document()
    optical = tomllib.loads((ROOT / "config/optical_source_current_simulation.toml").read_text(encoding="utf-8"))
    xx, xy = lockin["lockin_xx"], lockin["lockin_xy"]
    source = lockin["photonics_lockin"]["source"]
    reference = lockin["photonics_lockin"]["reference_output"]
    nkt, run, pem, pm = (optical[k] for k in ("nkt_source", "nkt_run", "pem", "pm100d"))
    scan, feedback = optical["optical_scan"], optical["power_feedback"]
    window = feedback["window"]
    return {
        "CHANGE_ME_EXPERIMENT_NAME": "synthetic_template_validation",
        "CHANGE_ME_SAMPLE_OPTICAL_ROUTE_AND_ACCEPTANCE_SCOPE": "Offline test only",
        "CHANGE_ME_CONFIRMED_SOURCE_WIRING": source["wiring"],
        "CHANGE_ME_CONFIRMED_SOURCE_LOAD": source["load"],
        "CHANGE_ME_CONFIRMED_REFERENCE_WIRING": reference["wiring"],
        "CHANGE_ME_READBACK_XY_REFERENCE_AMPLITUDE_V_RMS": reference["amplitude_v_rms"],
        "CHANGE_ME_READBACK_XY_REFERENCE_DC_MODE": reference["dc_mode"],
        "CHANGE_ME_XX_VISA_RESOURCE": xx["address"],
        "CHANGE_ME_XX_FULL_SCALE_V": xx["sensitivity_full_scale_v"],
        "CHANGE_ME_XX_PHASE_DEG": xx["phase_shift_deg"],
        "CHANGE_ME_XX_RESERVE_MODE": xx["sr830"]["reserve_mode"],
        "CHANGE_ME_XY_VISA_RESOURCE": xy["address"],
        "CHANGE_ME_XY_FULL_SCALE_V": xy["sensitivity_full_scale_v"],
        "CHANGE_ME_XY_PHASE_DEG": xy["phase_shift_deg"],
        "CHANGE_ME_XY_INPUT_RANGE_V_PEAK": xy["sr865a"]["input_range_v_peak"],
        "CHANGE_ME_XY_REF_INPUT_IMPEDANCE_OHM": xy["sr865a"]["reference_input_impedance_ohm"],
        "CHANGE_ME_APPROVED_SOURCE_V_RMS": lockin["lockin_sweep"]["excitation_points_v_rms"][0],
        "CHANGE_ME_GATE_BOTTOM_VISA_ADDRESS": "FAKE::GATE_BOTTOM",
        "CHANGE_ME_APPROVED_GATE_VOLTAGE_LIMIT_V": 50.0,
        "CHANGE_ME_APPROVED_GATE_CURRENT_LIMIT_A": 1e-5,
        "CHANGE_ME_APPROVED_SOURCE_CURRENT_MAX_PCT": nkt["max_level_pct"],
        "CHANGE_ME_VERIFIED_NKT_SDK_DLL_PATH": "FAKE_NOT_A_DLL.dll",
        "CHANGE_ME_NKT_PORT": "FAKE_NKT",
        "CHANGE_ME_CONFIRMED_SOURCE_MODULE_ADDRESS": 1,
        "CHANGE_ME_SOURCE_SERIAL": "SIMULATION",
        "CHANGE_ME_CONFIRMED_VARIA_MODULE_ADDRESS": 2,
        "CHANGE_ME_VARIA_SERIAL": "SIMULATION",
        "CHANGE_ME_CONFIRMED_OPTICAL_ROUTE": run["manual_route"],
        "CHANGE_ME_NKT_OPERATION_TIMEOUT_S": run["timeout_s"],
        "CHANGE_ME_NKT_POLL_INTERVAL_S": run["poll_interval_s"],
        "CHANGE_ME_QUALIFIED_ILLUMINATION_DWELL_S": run["dwell_s"],
        "CHANGE_ME_OPTICAL_RUN_NAME": run["run_name"],
        "CHANGE_ME_START_CURRENT_PCT": run["points"][0]["source_level_pct"],
        "CHANGE_ME_650NM_FIRST_CURRENT_PCT": run["points"][0]["source_level_pct"],
        "CHANGE_ME_650NM_P1_CURRENT_PCT": 10.0,
        "CHANGE_ME_650NM_P2_CURRENT_PCT": 20.0,
        "CHANGE_ME_650NM_P3_CURRENT_PCT": 30.0,
        "CHANGE_ME_650NM_P4_CURRENT_PCT": 38.0,
        "CHANGE_ME_650NM_P5_CURRENT_PCT": 39.0,
        "CHANGE_ME_650NM_P6_CURRENT_PCT": 40.0,
        "CHANGE_ME_LAMBDA_A_NM": run["points"][0]["wavelength_nm"],
        "CHANGE_ME_LAMBDA_B_NM": 650.0,
        "CHANGE_ME_BANDWIDTH_NM": run["points"][0]["bandwidth_nm"],
        "CHANGE_ME_PEM_PORT": "FAKE_PEM",
        "CHANGE_ME_EXACT_PEM_IDN": "Hinds PEM 200 controller SIMULATION",
        "CHANGE_ME_PEM_IO_TIMEOUT_S": pem["io_timeout_s"],
        "CHANGE_ME_PEM_SETTLE_TIMEOUT_S": pem["settle_timeout_s"],
        "CHANGE_ME_PEM_POLL_INTERVAL_S": pem["poll_interval_s"],
        "CHANGE_ME_CONFIRMED_PEM_WAVELENGTH_MIN_NM": pem["wavelength_min_nm"],
        "CHANGE_ME_CONFIRMED_PEM_WAVELENGTH_MAX_NM": pem["wavelength_max_nm"],
        "CHANGE_ME_ALLOWED_RETARDANCE_MIN_NM": pem["amplitude_min_nm"],
        "CHANGE_ME_ALLOWED_RETARDANCE_MAX_NM": pem["amplitude_max_nm"],
        "CHANGE_ME_RETARDANCE_READBACK_TOLERANCE_NM": pem["amplitude_tolerance_nm"],
        "CHANGE_ME_PM100D_VISA_RESOURCE": "FAKE::PM",
        "CHANGE_ME_PM100D_CONSOLE_SERIAL": "SIMULATION",
        "CHANGE_ME_SENSOR_SERIAL": "SIMULATION",
        "CHANGE_ME_PM_IO_TIMEOUT_S": pm["io_timeout_s"],
        "CHANGE_ME_PM_WAVELENGTH_TOLERANCE_NM": pm["wavelength_tolerance_nm"],
        "CHANGE_ME_PM_AVERAGE_COUNT": pm["average_count"],
        "CHANGE_ME_APPROVED_MEASURED_POWER_LIMIT_W": pm["max_power_w"],
        "CHANGE_ME_ACTUAL_POWER_MEASUREMENT_PLANE": pm["measurement_plane"],
        "CHANGE_ME_OPTICAL_SAMPLE_INTERVAL_S": scan["sample_interval_s"],
        "CHANGE_ME_REQUESTED_RETARDANCE_WAVES": scan["peak_retardance_waves"],
        "CHANGE_ME_P_A_W": feedback["target_powers_w"][0],
        "CHANGE_ME_P_B_W": feedback["target_powers_w"][1],
        "CHANGE_ME_POWER_FRACTIONAL_TOLERANCE": feedback["target_tolerance_fraction"],
        "CHANGE_ME_APPROVED_FEEDBACK_HARD_LIMIT_W": feedback["max_power_w"],
        "CHANGE_ME_TOTAL_FEEDBACK_TIMEOUT_S": feedback["timeout_s"],
        "CHANGE_ME_STABLE_POWER_HOLD_S": feedback["hold_s"],
        "CHANGE_ME_APPROVED_CURRENT_MIN_PCT": feedback["source_current_min_pct"],
        "CHANGE_ME_APPROVED_CURRENT_MAX_PCT": feedback["source_current_max_pct"],
        "CHANGE_ME_APPROVED_CURRENT_STEP_PCT": feedback["source_current_step_pct"],
        "CHANGE_ME_MINIMUM_VALID_ILLUMINATION_W": feedback["minimum_signal_power_w"],
        "CHANGE_ME_POWER_WINDOW_DURATION_S": window["duration_s"],
        "CHANGE_ME_POWER_WINDOW_MIN_SAMPLES": window["min_samples"],
        "CHANGE_ME_POWER_SAMPLE_INTERVAL_S": window["sample_interval_s"],
        "CHANGE_ME_POWER_WINDOW_PEAK_TO_PEAK_W": window["max_peak_to_peak_w"],
        "CHANGE_ME_APPROVED_MIN_SOURCE_V": safety["minimum_source_voltage_v"],
        "CHANGE_ME_APPROVED_MAX_SOURCE_V": safety["maximum_source_voltage_v"],
        "CHANGE_ME_APPROVED_PROTECTIVE_SOURCE_V": safety["cleanup_source_voltage_v"],
        "CHANGE_ME_APPROVED_XX_FULL_SCALE_V": xx["sensitivity_full_scale_v"],
        "CHANGE_ME_APPROVED_XY_FULL_SCALE_V": xy["sensitivity_full_scale_v"],
    }


def replace_settings(text, settings):
    """Override only scalar fixture settings in a temporary TOML copy."""
    section = ""
    lines = []
    for line in text.splitlines():
        header = re.fullmatch(r"\[([^\]]+)\]", line.strip())
        if header:
            section = header[1]
        assignment = re.match(r"^([a-z_]+)\s*=", line)
        if assignment and assignment[1] in settings.get(section, {}):
            line = assignment[1] + " = " + json.dumps(settings[section][assignment[1]])
        lines.append(line)
    return "\n".join(lines) + "\n"


class PhotonicsTemplateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "hardware.local.toml"
        self.safety_path = self.path.with_name("photonics_lockin_safety.local.toml")
        self.template = (ROOT / "config/photonics.example.toml").read_text(encoding="utf-8")
        self.safety = (ROOT / "config/photonics_lockin_safety.example.toml").read_text(encoding="utf-8")

    def write_filled(self, *, current_status=True):
        replacements = synthetic_replacements()
        pattern = r'"(CHANGE_ME_[A-Z0-9_]+)"'
        required = set(re.findall(pattern, self.template + self.safety))
        self.assertLessEqual(required, set(replacements), "Every remaining template placeholder needs a synthetic value")
        fill = lambda text: re.sub(pattern, lambda match: json.dumps(replacements[match[1]]), text)
        text = fill(self.template)
        lockin = reversed_sine_document()
        # The target example may already contain queried station settings. Keep
        # this test's ranges, time constants and all resource names synthetic,
        # while leaving shipped topology/model/filter/harmonic constraints intact.
        settings = {}
        for role in ("lockin_xx", "lockin_xy"):
            settings[role] = {key: lockin[role][key] for key in
                ("address", "sensitivity_full_scale_v", "phase_shift_deg", "shield_grounding")}
            settings[role]["time_constant_s"] = 1.0
        settings["lockin_xx.sr830"] = {"reserve_mode": lockin["lockin_xx"]["sr830"]["reserve_mode"]}
        settings["lockin_xy.sr865a"] = {
            "input_range_v_peak": lockin["lockin_xy"]["sr865a"]["input_range_v_peak"],
            "reference_input_impedance_ohm": lockin["lockin_xy"]["sr865a"]["reference_input_impedance_ohm"],
            "current_status_supported": current_status,
        }
        settings["photonics_lockin.source"] = {key: lockin["photonics_lockin"]["source"][key]
            for key in ("wiring", "load")}
        settings["photonics_lockin.reference_output"] = {key: lockin["photonics_lockin"]["reference_output"][key]
            for key in ("wiring", "load", "amplitude_v_rms", "dc_mode")}
        settings["nkt_source"] = {"dll_path": "FAKE_NOT_A_DLL.dll", "port": "FAKE_NKT",
            "address": 1, "expected_serial": "SIMULATION"}
        settings["nkt_varia"] = {"address": 2, "expected_serial": "SIMULATION"}
        settings["pem"] = {"port": "FAKE_PEM", "expected_idn": "Hinds PEM 200 controller SIMULATION"}
        settings["pm100d"] = {"resource": "FAKE::PM", "expected_serial": "SIMULATION",
            "expected_sensor_serial": "SIMULATION"}
        text = replace_settings(text, settings)
        points = lockin["lockin_sweep"]["excitation_points_v_rms"]
        text = re.sub(r"^excitation_points_v_rms = .+$", "excitation_points_v_rms = " + json.dumps(points), text, flags=re.M)
        self.path.write_text(text, encoding="utf-8")
        self.safety_path.write_text(fill(self.safety), encoding="utf-8")

    def test_unconfigured_template_fails_strict_loading_with_no_connection(self):
        self.path.write_text(self.template, encoding="utf-8")
        self.safety_path.write_text(self.safety, encoding="utf-8")
        with patch("attodry_control.combination_hardware.HardwareCombinationStation.open") as opened:
            with self.assertRaises(ValueError):
                load_hardware_combination(self.path)
        opened.assert_not_called()

    def test_filled_template_loads_two_wavelengths_two_powers_two_excitations(self):
        self.write_filled()
        with patch("attodry_control.combination_hardware.HardwareCombinationStation.open") as opened:
            config = load_hardware_combination(self.path)
        opened.assert_not_called()
        self.assertEqual(config.reference_topology, "pem_xy_xx_sine")
        self.assertEqual(config.lockin.lockin_xx.model, "SR830")
        self.assertEqual(config.lockin.lockin_xy.model, "SR865A")
        self.assertEqual(config.lockin.lockin_xx.harmonics, (1,))
        self.assertEqual(config.lockin.lockin_xy.harmonics, (2,))
        self.assertEqual(config.lockin.lockin_xx.reference_source, "external_sine")
        self.assertEqual(config.lockin.lockin_xx.external_reference_edge, "sine_zero_crossing")
        self.assertTrue(config.lockin.lockin_xy.sine_output_connected)
        self.assertFalse(config.lockin.reference_output.sample_connected)
        self.assertEqual(config.lockin.lockin_xy.sync_output_mode, "preserve")
        self.assertEqual(config.lockin.settle_s,
            tomllib.loads(self.template)["photonics_lockin"]["settle_time_constants"])
        self.assertTrue(config.plan.strict_resultant)
        conditions = config.plan.conditions()
        self.assertEqual(len(conditions), 8)
        points = [row["requested"] for row in conditions]
        self.assertEqual({row["optical_wavelength_nm"] for row in points}, {633.0, 650.0})
        self.assertEqual({row["optical_target_power_w"] for row in points}, {0.000005, 0.0000225})
        self.assertEqual({row["lockin_excitation_v_rms"] for row in points}, {0.004, 0.006})
        combinations = {(row["optical_wavelength_nm"], row["optical_target_power_w"],
                         row["lockin_excitation_v_rms"]) for row in points}
        self.assertEqual(len(combinations), 8)
        self.assertEqual([row["axes"]["optical"]["index"] for row in conditions], [0, 0, 1, 1, 2, 2, 3, 3])
        self.assertEqual([row["axes"]["lockin"]["index"] for row in conditions], [0, 1] * 4)
        self.assertTrue(all(point.nd_pct is None and point.pulse_picker_ratio is None
                            for point in config.optical.nkt.points))
        self.assertEqual(config.optical.scan.feedback.actuator, "source_current")

    def test_station_filled_example_uses_only_synthetic_test_settings(self):
        self.template = replace_settings(self.template, {
            "photonics_lockin.source": {"load": "high_impedance"},
            "photonics_lockin.reference_output": {"wiring": "single_ended", "amplitude_v_rms": 0.4,
                "dc_mode": "difference"},
            "lockin_xx": {"address": "STATION::XX", "sensitivity_full_scale_v": 1.0},
            "lockin_xx.sr830": {"reserve_mode": "low_noise"},
            "lockin_xy": {"address": "STATION::XY", "time_constant_s": 0.3,
                "sensitivity_full_scale_v": 0.002, "phase_shift_deg": -134.6477133,
                "shield_grounding": "ground"},
            "lockin_xy.sr865a": {"input_range_v_peak": 1.0, "current_status_supported": True},
        })
        self.write_filled()
        with patch("attodry_control.combination_hardware.HardwareCombinationStation.open") as opened:
            config = load_hardware_combination(self.path)
        opened.assert_not_called()
        self.assertEqual(config.lockin.lockin_xx.address, "FAKE::XX")
        self.assertEqual(config.lockin.lockin_xy.address, "FAKE::XY")
        self.assertEqual(config.lockin.lockin_xy.sensitivity_full_scale_v, 0.001)
        self.assertEqual(config.lockin.reference_output.amplitude_v_rms, 0.2)
        self.assertEqual(len(config.plan.conditions()), 8)

    def test_filled_template_still_requires_verified_current_status_capability(self):
        self.write_filled(current_status=False)
        with self.assertRaisesRegex(ValueError, "current-status"):
            load_hardware_combination(self.path)

    def test_commented_continuous_gate_example_loads_with_complete_explicit_ramp(self):
        # Enable the shipped block itself so omissions or stale example fields
        # fail this test; all replacement values remain synthetic.
        self.assertEqual(self.template.count("# [gate_bottom]"), 1)
        prefix, block = self.template.split("# [gate_bottom]", 1)
        block = re.sub(r"(?m)^# (?=(?:\[[^\]]+\]|[a-z_]+\s*=))", "", block)
        # Select the documented optional ramp: direct zero tolerance is exclusive.
        block = re.sub(r"(?m)^zero_readback_tolerance_v\s*=.*(?:\n|$)", "", block)
        self.template = prefix + "[gate_bottom]" + block
        self.write_filled()
        text = replace_settings(self.path.read_text(encoding="utf-8"), {
            "combination_scan": {"order": ["optical", "smu", "lockin"],
                "illumination_policy": "continuous_gate_scan"},
            "lockin_sweep": {"excitation_points_v_rms": [.006]},
        })
        self.path.write_text(text, encoding="utf-8")
        with patch("attodry_control.combination_hardware.HardwareCombinationStation.open") as opened:
            config = load_hardware_combination(self.path)
        opened.assert_not_called()
        self.assertEqual(config.illumination_policy, "continuous_gate_scan")
        self.assertEqual([axis.module for axis in config.plan.axes], ["optical", "smu", "lockin"])
        self.assertEqual(set(config.smu.hardware.by_role()), {"gate_bottom"})
        self.assertEqual(config.smu.hardware.gate_bottom.address, "FAKE::GATE_BOTTOM")
        self.assertEqual(config.smu.hardware.gate_bottom.max_abs_voltage_v, 50.0)
        self.assertEqual(config.smu.hardware.gate_bottom.max_abs_current_a, 1e-5)
        ramp = config.smu.plan.gate_bottom.ramp
        self.assertEqual((ramp.max_step_v, ramp.step_interval_s,
                          ramp.readback_tolerance_v, ramp.timeout_s), (.5, .2, .05, 600.0))
        conditions = config.plan.conditions()
        self.assertEqual(len(conditions), 4 * 21)
        self.assertEqual({row["requested"]["lockin_excitation_v_rms"] for row in conditions}, {.006})
        self.assertEqual({row["requested"]["gate_bottom_v"] for row in conditions},
                         {-35.0 + 3.5 * index for index in range(21)})
        # The public example's timeout is mandatory; enabling only part of the
        # ramp table must reject before a resource could be opened.
        prefix, block = text.rsplit("[three_smu_run.gate_bottom.ramp]", 1)
        block = re.sub(r"(?m)^timeout_s\s*=.*(?:\n|$)", "", block)
        text = prefix + "[three_smu_run.gate_bottom.ramp]" + block
        self.path.write_text(text, encoding="utf-8")
        with self.assertRaises(ValueError):
            load_hardware_combination(self.path)

        # Select direct from the same shipped gate block, retaining its full grid.
        direct_text = prefix + "zero_readback_tolerance_v = 0.05\n"
        self.path.write_text(direct_text, encoding="utf-8")
        with patch("attodry_control.combination_hardware.HardwareCombinationStation.open") as opened:
            direct = load_hardware_combination(self.path)
        opened.assert_not_called()
        self.assertIsNone(direct.smu.plan.gate_bottom.ramp)
        self.assertEqual(direct.smu.plan.gate_bottom.zero_readback_tolerance_v, .05)
        self.assertEqual(len(direct.plan.conditions()), 4 * 21)


if __name__ == "__main__":
    unittest.main()
