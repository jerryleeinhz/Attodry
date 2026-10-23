"""Configurable register ACK; actual-field qualification remains independent."""
from dataclasses import replace
import math
from pathlib import Path
import re
import tempfile
import unittest

from attodry_control.attodry import AttoDryDriver, AttoDryTimeout
from attodry_control.config import ConfigError, load_config
from attodry_control.magnetic_field import execute_ordered_field_points
from attodry_control.models import VectorField
from attodry_control.safety import FieldTransitionPolicy, SafetyViolation
from tests import test_attodry as fake


REQUESTED_X = -1.1333333253860474
REGISTER_X = -1.1332000494003296


class FieldAckToleranceTests(unittest.TestCase):
    def driver(self, ack=0.0001, actual_tolerance=0.0015):
        config = load_config("config/hardware.example.toml")
        criteria = replace(config.magnet.stability.criteria,
                           tolerance=actual_tolerance, dwell_s=2)
        config = replace(config, magnet=replace(config.magnet,
            stability=replace(config.magnet.stability, criteria=criteria, wait_timeout_s=8),
            readback_tolerance_t=actual_tolerance, setpoint_ack_tolerance_t=ack))
        dll = fake.FakeAttoDryDll()
        driver = AttoDryDriver.from_config(config, dll=dll,
            connection_authorized=True, writes_authorized=True)
        driver.connect(monotonic=fake.StepClock(), sleeper=lambda _: None)
        self.addCleanup(driver.close)
        dll.field_control = 1
        return driver, dll

    def execute_quantized_x(self, driver, dll, actual_x=REGISTER_X):
        dll.setpoint_x_t = dll.bx_t = -1.2000000476837158
        commands, events = [], []

        def set_x(value):
            commands.append(value.value)
            dll.setpoint_x_t = REGISTER_X
            dll.bx_t = actual_x
            return 0

        dll.AttoDRY_Interface_setUserMagneticFieldX = set_x
        self.commands, self.events = commands, events
        return execute_ordered_field_points(driver, (VectorField(REQUESTED_X, 0),), 0.1,
            transition_policy=FieldTransitionPolicy.DIRECT, on_event=events.append,
            monotonic=fake.StepClock(), sleeper=lambda _: None)

    def test_archived_133ut_register_difference_still_times_out_with_default_ack(self):
        driver, dll = self.driver()
        self.assertGreater(abs(REGISTER_X - REQUESTED_X), driver.setpoint_ack_tolerance_t)
        with self.assertRaisesRegex(AttoDryTimeout, "setpoint_x_ack"):
            self.execute_quantized_x(driver, dll)
        self.assertEqual(self.commands, [REQUESTED_X])
        result = next(e for e in self.events if e["event"] == "field_command_result")
        self.assertEqual(result["acknowledgement"], "failed")
        self.assertEqual(result["dll_return_code"], 0)
        self.assertFalse(any(e["event"] == "point_completed" for e in self.events))

    def test_explicit_200ut_ack_confirms_register_then_requires_actual_dwell(self):
        driver, dll = self.driver(ack=0.0002)
        result = self.execute_quantized_x(driver, dll)
        self.assertEqual(self.commands, [REQUESTED_X])
        command = next(e for e in self.events if e["event"] == "field_command_result")
        self.assertEqual(command["acknowledgement"], "confirmed")
        self.assertEqual(command["setpoint_ack_tolerance_t"], 0.0002)
        self.assertEqual(command["readback_tolerance_t"], 0.0015)
        self.assertEqual(result[0].requested_field, VectorField(REQUESTED_X, 0))
        self.assertAlmostEqual(result[0].final_state.field_setpoint.bx_t, REGISTER_X)
        samples = [e for e in self.events
                   if e["event"] == "field_sample" and e["phase"] == "field_stability"]
        self.assertGreaterEqual(samples[-1]["elapsed_s"], 2)
        self.assertTrue(any(e["event"] == "point_completed" for e in self.events))

    def test_wider_ack_does_not_complete_a_field_that_never_arrives(self):
        driver, dll = self.driver(ack=0.0002)
        with self.assertRaisesRegex(AttoDryTimeout, "Field stability wait timed out"):
            self.execute_quantized_x(driver, dll, actual_x=-1.2)
        command = next(e for e in self.events if e["event"] == "field_command_result")
        self.assertEqual(command["acknowledgement"], "confirmed")
        self.assertFalse(any(e["event"] == "point_completed" for e in self.events))
        self.assertEqual(driver.field_stability.criteria.tolerance, 0.0015)

    def test_wider_ack_does_not_relax_actual_field_hard_limit(self):
        driver, dll = self.driver(ack=0.0002)
        with self.assertRaises(SafetyViolation):
            self.execute_quantized_x(driver, dll, actual_x=3.01)
        self.assertFalse(any(e["event"] == "point_completed" for e in self.events))

    def test_zero_still_requires_unchanged_actual_magnitude_criterion(self):
        driver, dll = self.driver(ack=0.0002)

        def zero():
            dll.setpoint_x_t = dll.setpoint_z_t = 0
            dll.bx_t, dll.bz_t = 0.0016, 0
            return 0

        dll.AttoDRY_Interface_sweepFieldToZero = zero
        with self.assertRaisesRegex(AttoDryTimeout, "Field stability wait timed out"):
            driver.request_zero_field(monotonic=fake.StepClock(), sleeper=lambda _: None)
        self.assertEqual(driver.field_stability.criteria.tolerance, 0.0015)
        self.assertGreater(driver.last_confirmed_state.field.magnitude_t, 0.0015)

    def test_parser_preserves_narrow_legacy_default_and_bounds_explicit_widening(self):
        source = Path("config/hardware.example.toml").read_text(encoding="utf-8")
        source = re.sub(r"(?m)^(?:field|readback)_tolerance_t\s*=.*$",
                        "readback_tolerance_t = ACTUAL", source)
        source = re.sub(r"(?m)^setpoint_ack_tolerance_t\s*=.*$",
                        "setpoint_ack_tolerance_t = ACK", source)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hardware.toml"
            (path.parent / "lockin_safety.toml").write_bytes(Path("config/lockin_safety.toml").read_bytes())
            for actual, ack, valid in ((0.0015, 0.0002, True), (0.0015, 0.0015, True),
                    (0.0015, 0.0016, False), (0.00005, 0.0001, True),
                    (0.00005, 0.00011, False)):
                path.write_text(source.replace("ACTUAL", str(actual)).replace("ACK", str(ack)), encoding="utf-8")
                with self.subTest(actual=actual, ack=ack):
                    if valid:
                        parsed = load_config(path)
                        self.assertEqual(parsed.magnet.setpoint_ack_tolerance_t, ack)
                        self.assertEqual(parsed.magnet.stability.criteria.tolerance, actual)
                    else:
                        with self.assertRaisesRegex(ConfigError, "setpoint_ack_tolerance_t"):
                            load_config(path)
            path.write_text(source.replace("ACTUAL", "0.00005").replace(
                "setpoint_ack_tolerance_t = ACK", ""), encoding="utf-8")
            self.assertEqual(load_config(path).magnet.setpoint_ack_tolerance_t, 0.0001)

    def test_direct_driver_enforces_same_positive_finite_compatible_bound(self):
        driver, _ = self.driver(ack=0.0001, actual_tolerance=0.00005)
        self.assertEqual(driver.setpoint_ack_tolerance_t, 0.0001)
        for ack in (0, -0.0002, math.nan, math.inf, True, "0.0002", 0.0016):
            with self.subTest(ack=ack), self.assertRaisesRegex(ValueError, "acknowledgement tolerance"):
                self.driver(ack=ack)


if __name__ == "__main__":
    unittest.main()
