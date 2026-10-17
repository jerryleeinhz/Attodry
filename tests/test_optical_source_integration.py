"""CLD lifecycle and optical source-grid integration using only fake devices."""
from dataclasses import replace
import json
from pathlib import Path
import re
import tempfile
import unittest

from optical_helpers import Clock, EXAMPLE, ROOT
from attodry_control.nkt_config import NktError, load_nkt_config
from attodry_control.optical_config import FeedbackConfig, ScanConfig, WindowConfig, load_pm_config
from attodry_control.optical_points import OpticalPointConfig, OpticalPointSession, load_optical_point_config
from attodry_control.photoresponse_cld import CldConfig, CldPoint, SimulatedCld
from attodry_control.pm100d import Pm100d, SimulatedPmResource


def cld_config():
    return CldConfig(backend="simulation", points=(CldPoint(0.015, 730),),
        dll_path="", address="", expected_serial="SIMULATED", diode_model="FAKE-DIODE",
        diode_wiring_confirmed=False, max_current_a=0.08, max_tec_current_a=0.5,
        temperature_min_c=20, temperature_max_c=30, temperature_c=25,
        temperature_tolerance_c=0.1, stable_dwell_s=0.1, current_tolerance_a=1e-6,
        manual_route="SIMULATION detector only", timeout_s=2, poll_interval_s=0.02,
        output_directory=Path("simulation-only"), run_name="cld-fake", note="Fake response only")


class CldOpticalIntegrationTests(unittest.TestCase):
    def test_fixed_diode_rejects_multiple_nominal_wavelengths(self):
        config = cld_config()
        with self.assertRaisesRegex(NktError, "fixed nominal diode wavelength"):
            replace(config, points=(CldPoint(0.015, 730), CldPoint(0.02, 760))).validate()

    def make(self, *, feedback=False, initial_tec=False):
        config = cld_config()
        policy = None
        if feedback:
            policy = FeedbackConfig(None, None, 0.005, 5, 0, None, None, None, None,
                WindowConfig(0.02, 3, 0.01, 1e-6), target_powers_w=(0.001, 0.002),
                target_tolerance_fraction=0.1, actuator="cld_current", target_mapping="cartesian",
                current_min_a=0.005, current_max_a=0.08, current_step_a=0.005,
                minimum_signal_power_w=1e-8)
        scan = ScanConfig("power_stabilized" if feedback else "direct", False, True, 0.01,
                          feedback=policy, laser_source="cld")
        meter = replace(load_pm_config(EXAMPLE), max_power_w=0.005)
        point_config = OpticalPointConfig(config, scan, pm=meter)
        backend = SimulatedCld(config)
        backend.state["getTecOutputState"] = initial_tec
        writes = []
        original_set = backend.set
        def record_write(name, value):
            writes.append((name, value))
            original_set(name, value)
        backend.set = record_write
        clock = Clock()
        events = []
        session = OpticalPointSession(point_config, lambda kind, value: events.append((kind, value)),
            backend_factory=lambda _: backend, clock=clock, sleep=clock.sleep)
        self.addCleanup(session.cleanup)
        return session, backend, writes, clock, events

    def sample(self, session, index):
        session.set_point(index)
        self.assertFalse(session.backend.state["getLdOutputState"])
        session.qualify()
        session.begin_sample()
        result = session.read()
        session.end_sample([result])
        return result

    def test_direct_fixed_diode_reports_nominal_wavelength_and_native_current(self):
        session, backend, writes, _, _ = self.make()
        session.open()
        self.assertEqual(writes, [])
        session.configure()
        result = self.sample(session, 0)
        self.assertTrue(result["clean"])
        self.assertEqual(result["actual"]["optical_wavelength_nm"], 730)
        self.assertEqual(result["actual"]["optical_current_a"], 0.015)
        self.assertNotIn("optical_source_level_pct", result["actual"])
        self.assertAlmostEqual(result["measurements"]["optical_power_w"], 0.001)
        self.assertEqual(result["status"]["wavelength_definition"], "nominal_diode")
        self.assertNotIn("setWavelength", [name for name, value in writes])
        cleanup = session.cleanup()
        self.assertTrue(cleanup["verified"], cleanup)
        self.assertFalse(backend.state["getLdOutputState"])
        self.assertEqual(backend.state["getLdCurrSetpoint"], 0)
        self.assertFalse(backend.state["getTecOutputState"])

    def test_cartesian_two_targets_tune_current_before_freezing_formal_samples(self):
        session, backend, writes, _, _ = self.make(feedback=True)
        self.assertEqual(len(session.config.points), 2)
        session.open()
        session.configure()
        powers = []
        for index in range(2):
            if index:
                session.suspend()
            session.set_point(index)
            session.qualify()
            formal_start = len(writes)
            session.begin_sample()
            result = session.read()
            session.end_sample([result])
            self.assertEqual(writes[formal_start:], [])
            target = session.feedback.target_power_w
            powers.append(result["measurements"]["optical_power_w"])
            self.assertLessEqual(abs(powers[-1] - target), session.feedback.target_tolerance_w)
            self.assertEqual(result["actual"]["optical_target_power_w"], target)
            self.assertTrue(all(sample["requested_current_a"] <= session.config.nkt.max_current_a
                                for sample in session.row["power_samples"]))
        self.assertLess(powers[0], powers[1])
        self.assertTrue(session.row["cld_current_iterations"])
        self.assertTrue(session.cleanup()["off_confirmed"])

    def test_cleanup_restores_original_enabled_tec(self):
        session, backend, _, _, _ = self.make(initial_tec=True)
        session.open()
        session.configure()
        self.sample(session, 0)
        cleanup = session.cleanup()
        self.assertTrue(cleanup["verified"])
        self.assertTrue(backend.state["getTecOutputState"])
        self.assertFalse(backend.state["getLdOutputState"])
        self.assertEqual(backend.state["getLdCurrSetpoint"], 0)

    def test_driver_identity_failure_never_reaches_a_write_or_emission(self):
        session, backend, writes, _, _ = self.make()
        backend.snapshot = lambda: (_ for _ in ()).throw(NktError("CLD identity changed"))
        with self.assertRaisesRegex(NktError, "identity changed"):
            session.open()
        self.assertEqual(writes, [])
        self.assertFalse(backend.state["getLdOutputState"])

    def test_unknown_hardware_connection_blocks_before_ld_enable(self):
        session, backend, writes, clock, _ = self.make()
        hardware_config = replace(session.config.nkt, backend="visa", address="FAKE::CLD",
                                  diode_wiring_confirmed=True)
        backend.is_hardware = True
        backend.config = hardware_config
        backend.state["diagnostics"] = {"qualification": "unqualified", "ld_connection_raw": 1,
            "tec_connection_raw": 1, "measurement_condition_raw": 8192}
        meter_config = replace(session.config.pm, backend="visa", resource="FAKE::PM",
            expected_serial="SIMULATION", expected_sensor_serial="SIMULATION")
        config = replace(session.config, nkt=hardware_config, pm=meter_config)
        meter = Pm100d(meter_config, SimulatedPmResource(), is_hardware=True,
            authorize_settings=True, authorize_measurement=True, clock=clock)
        session = OpticalPointSession(config, backend_factory=lambda _: backend,
            meter_factory=lambda _: meter, clock=clock, sleep=clock.sleep)
        self.addCleanup(session.cleanup)
        session.open(authorize_writes=True, confirm_manual_route=True)
        session.configure()
        with self.assertRaisesRegex(NktError, "connection diagnostic unresolved"):
            session.set_point(0)
        self.assertNotIn(("switchLdOutput", True), writes)
        self.assertFalse(backend.state["getLdOutputState"])
        self.assertTrue(session.cleanup()["off_confirmed"])


def toml_value(value):
    if type(value) is dict:
        return "{" + ", ".join(name + " = " + toml_value(item) for name, item in value.items()) + "}"
    if type(value) is list:
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    return json.dumps(value)


class OpticalGridIntegrationTests(unittest.TestCase):
    def text(self, points, order=None, *, source_fixture=False):
        path = ROOT / "config/optical_source_current_simulation.toml" if source_fixture else EXAMPLE
        result = re.sub(r"(?ms)^points = \[.*?^\]", "points = " + toml_value(points), path.read_text(), count=1)
        if order is not None:
            result = result.replace('[nkt_run]\n', '[nkt_run]\ngrid_order = ' + toml_value(order) + '\n')
        return result

    def load(self, text, *, point_config=False):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fake.toml"
            path.write_text(text, encoding="utf-8")
            return load_optical_point_config(path) if point_config else load_nkt_config(path)

    def test_default_and_explicit_axis_order_preserve_duplicates(self):
        points = [{"source_levels_pct": [6.0, 8.0], "wavelength_points_nm": [633.0, 635.0],
                   "bandwidth_points_nm": [10.0, 12.0]}]
        default = self.load(self.text(points))
        self.assertEqual([(p.wavelength_nm, p.bandwidth_nm, p.source_level_pct) for p in default.points],
            [(wave, width, level) for wave in (633, 635) for width in (10, 12) for level in (6, 8)])
        reverse = self.load(self.text(points, ["source", "bandwidth", "wavelength"]))
        self.assertEqual([(p.source_level_pct, p.bandwidth_nm, p.wavelength_nm) for p in reverse.points],
            [(level, width, wave) for level in (6, 8) for width in (10, 12) for wave in (633, 635)])
        repeated = self.load(self.text([{"source_level_pct": 8.0, "wavelength_points_nm": [633.0, 633.0],
                                        "bandwidth_nm": 10.0}]))
        self.assertEqual(len(repeated.points), 2)
        self.assertEqual(repeated.points[0], repeated.points[1])

    def test_ranges_expand_all_three_axes_in_native_resolution(self):
        points = [{"source_level_ranges": [{"min": 6, "max": 8, "scale": "linear", "points": 3}],
            "wavelength_ranges": [{"min": 633, "max": 635, "scale": "linear", "step": 2}],
            "bandwidth_ranges": [{"min": 10, "max": 12, "scale": "linear", "points": 2}]}]
        config = self.load(self.text(points))
        self.assertEqual(len(config.points), 12)
        self.assertEqual([p.source_level_pct for p in config.points[:3]], [6, 7, 8])
        self.assertEqual({p.wavelength_nm for p in config.points}, {633, 635})

    def test_conflicts_unknown_order_quantization_and_product_budget_are_rejected(self):
        valid = {"source_level_pct": 8.0, "wavelength_nm": 633.0, "bandwidth_nm": 10.0}
        cases = [self.text([{**valid, "wavelength_points_nm": [633.0]}]),
            self.text([{**valid, "source_levels_pct": [8.0]}]),
            self.text([valid], ["wavelength", "wavelength", "source"]),
            self.text([{**valid, "source_level_pct": 7.05}]),
            self.text([{**valid, "wavelength_nm": 633.05}]),
            self.text([{"source_levels_pct": [8.0] * 101,
                "wavelength_points_nm": [633.0] * 100, "bandwidth_nm": 10}])]
        for text in cases:
            with self.subTest(text=text), self.assertRaises(NktError):
                self.load(text)

    def test_expanded_spectral_rows_match_initial_current_matrix_dimensions(self):
        text = self.text([{"source_level_pct": 8.0, "wavelength_points_nm": [633.0, 635.0],
                          "bandwidth_nm": 10.0}], source_fixture=True)
        text = text.replace('[power_feedback]\n', '[power_feedback]\ntarget_mapping = "cartesian"\n'
            'initial_current_mode = "grid"\ninitial_source_levels_pct = [[8, 10, 12], [14, 16, 18]]\n')
        config = self.load(text, point_config=True)
        self.assertEqual(len(config.nkt.points), 2)
        self.assertEqual(len(config.points), 6)
        self.assertEqual([point.source_level_pct for point in config.nkt_points], [8, 10, 12, 14, 16, 18])
        with self.assertRaisesRegex(NktError, "row count"):
            self.load(text.replace('[[8, 10, 12], [14, 16, 18]]', '[[8, 10, 12]]'), point_config=True)


if __name__ == "__main__":
    unittest.main()
