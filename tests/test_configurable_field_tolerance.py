"""Editable unified field tolerance; production code with fake DLL only."""
from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
import math
import re
from types import SimpleNamespace
import unittest

from attodry_control.attodry import AttoDryDriver, AttoDryError, AttoDryTimeout
from attodry_control.combination_cli import run as combination_cli
from attodry_control.combination_hardware import load_hardware_combination
from attodry_control.config import ConfigError
from attodry_control.cryostat_points import CryostatPointSession
from attodry_control.models import VectorField
from attodry_control.safety import FieldReadbackPolicy, MagnetLimits, SafetyViolation
from tests import test_combination_cryostat as fixtures
from tests.optical_helpers import Clock


class ConfigurableFieldToleranceTests(unittest.TestCase):
    setUp = fixtures.CryostatCombinationTests.setUp
    write_config = fixtures.CryostatCombinationTests.write_config

    def config(self, tolerance="0.005", target=3.0):
        source = re.sub(r"(?m)^(?:readback_tolerance_t|field_tolerance_t)\s*=.*$",
                        "readback_tolerance_t = " + tolerance, self.base)
        source = source.replace('{ bx_t = 0.0, bz_t = 0.0 },',
                                '{ bx_t = 0.0, bz_t = ' + str(target) + ' },')
        self.write_config(("magnetic",), source=source,
                          extra='magnetic_deviation_policy = "record_continue"\n')
        return load_hardware_combination(self.path)

    def session(self, tolerance="0.005", residual=0.003):
        config = self.config(tolerance)
        self.dll.setpoint_z_t = 3.0
        self.dll.bx_t = residual
        self.dll.bz_t = 3.0 + residual
        self.dll.field_control = 0
        clock, evidence = Clock(), []
        session = CryostatPointSession(config.cryostat, None, config.magnetic,
            lambda kind, payload: evidence.append((kind, payload)), dll=self.dll,
            monotonic=clock, sleep=clock.sleep)
        session.open()
        self.addCleanup(session.close)
        session.apply("magnetic", config.plan.axes[0].points[0])
        session.qualify(("magnetic",))
        return session, clock, evidence

    def test_loader_and_actual_cli_accept_values_above_previous_cap_without_io(self):
        for tolerance in ("0.005", "0.02"):
            with self.subTest(tolerance=tolerance):
                config = self.config(tolerance)
                value = float(tolerance)
                self.assertEqual(config.cryostat.magnet.readback_tolerance_t, value)
                self.assertEqual(config.cryostat.magnet.stability.criteria.tolerance, value)
                self.assertEqual(config.snapshot["field_readback_policy"]["zero_magnitude_tolerance_t"], value)
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(combination_cli(["describe-hardware", "--config", str(self.path)]), 0)
                preview = json.loads(out.getvalue())
                self.assertEqual(preview["plan"]["field_readback_policy"]["readback_tolerance_t"], value)
        self.assertEqual(self.dll.events, [])

    def test_invalid_config_policy_and_direct_driver_tolerances_are_rejected(self):
        for text in ("0", "-0.005", "nan", "inf", "true"):
            with self.subTest(config=text), self.assertRaises((ConfigError, ValueError)):
                self.config(text)
        valid = self.config()
        for value in (0, -0.005, math.nan, math.inf, True):
            with self.subTest(policy=value), self.assertRaises(ValueError):
                FieldReadbackPolicy.from_targets((VectorField(0, 3),), MagnetLimits(), value,
                                                 command_scoped=True)
            cfg = SimpleNamespace(**{**vars(valid.cryostat),
                "magnet": replace(valid.cryostat.magnet,
                    readback_tolerance_t=value)})
            with self.subTest(driver=value), self.assertRaises(ValueError):
                AttoDryDriver.from_config(cfg, dll=self.dll,
                    connection_authorized=True, writes_authorized=True)
        self.assertEqual(self.dll.events, [])

    def test_legacy_field_tolerance_still_has_its_historical_cap(self):
        source = re.sub(r"(?m)^(?:readback_tolerance_t|field_tolerance_t)\s*=.*$",
                        "field_tolerance_t = 0.002", self.base)
        self.write_config(("magnetic",), source=source)
        with self.assertRaisesRegex(ConfigError, "field_tolerance_t"):
            load_hardware_combination(self.path)
        self.assertEqual(self.dll.events, [])

    def test_fake_control_takeover_qualification_and_formal_readback_use_custom_tolerance(self):
        session, clock, _ = self.session()
        self.assertIn("toggle_field_control", self.dll.events)
        self.assertGreaterEqual(clock(), session.config.magnet.stability.criteria.dwell_s)
        self.assertIsNotNone(session.field_qualification)
        session.begin_sample()
        reading = session.read("magnetic")
        session.end_sample([reading])
        self.assertAlmostEqual(reading["actual"]["field_x_t"], 0.003, places=6)
        self.assertAlmostEqual(reading["actual"]["field_z_t"], 3.003, places=6)
        self.assertFalse(reading["status"]["magnetic_quality"]["warning"])
        self.assertTrue(reading["status"]["field_readback_assessment"]["within_readback_policy"])

    def test_fake_zero_confirmation_uses_configured_magnitude_tolerance_and_dwell(self):
        session, clock, _ = self.session()
        original = self.dll.AttoDRY_Interface_sweepFieldToZero
        def residual_zero():
            code = original()
            self.dll.bx_t = self.dll.bz_t = 0.003
            return code
        self.dll.AttoDRY_Interface_sweepFieldToZero = residual_zero
        started = clock()
        state = session.driver.request_zero_field(monotonic=clock, sleeper=clock.sleep)
        self.assertGreater(state.field.magnitude_t, 0.0015)
        self.assertLess(state.field.magnitude_t, 0.005)
        self.assertGreaterEqual(clock() - started, session.config.magnet.stability.criteria.dwell_s)

    def test_zero_requires_magnitude_not_individual_axes_below_tolerance(self):
        session, clock, _ = self.session()
        original = self.dll.AttoDRY_Interface_sweepFieldToZero
        def residual_zero():
            code = original()
            self.dll.bx_t = self.dll.bz_t = 0.004
            return code
        self.dll.AttoDRY_Interface_sweepFieldToZero = residual_zero
        with self.assertRaises(AttoDryTimeout):
            session.driver.request_zero_field(monotonic=clock, sleeper=clock.sleep)

    def test_larger_tolerance_keeps_axis_caps_dual_resultant_and_snapshot(self):
        policy = FieldReadbackPolicy.from_targets((VectorField(0, 3),),
            MagnetLimits(3, 9, 3), 0.02, command_scoped=True)
        policy.validate_readback(VectorField(0.01, 3), command=VectorField(0, 3))
        self.assertEqual(policy, FieldReadbackPolicy.from_snapshot(policy.snapshot()))
        self.assertEqual(policy.snapshot()["zero_magnitude_tolerance_t"], 0.02)
        for actual, command in ((VectorField(0, 9.01), VectorField(0, 9)),
                                (VectorField(3.01, 0), VectorField(3, 0)),
                                (VectorField(2.13, 2.13), VectorField(2, 2))):
            with self.subTest(actual=actual), self.assertRaises(SafetyViolation):
                policy.validate_readback(actual, command=command)
        reduced = FieldReadbackPolicy.from_targets((VectorField(0, 1),),
            MagnetLimits(0.001, 3, 3), 0.02, command_scoped=True)
        with self.assertRaises(SafetyViolation):
            reduced.validate_readback(VectorField(0.002, 1), command=VectorField(0, 1))

    def test_finite_large_tolerance_has_no_hidden_cap_or_float32_overflow(self):
        config = self.config("1e100")
        policy = config.plan.magnetic_readback_policy()
        self.assertEqual(config.cryostat.magnet.readback_tolerance_t, 1e100)
        self.assertEqual(policy.snapshot()["readback_tolerance_t"], 1e100)
        self.assertEqual(policy, FieldReadbackPolicy.from_snapshot(policy.snapshot()))
        self.assertLessEqual(policy.snapshot()["inactive_axis_readback_limits_t"]["x"], 3)
        self.assertLessEqual(policy.snapshot()["inactive_axis_readback_limits_t"]["z"], 9)
        policy.validate_readback(VectorField(0, 3), command=VectorField(0, 3))
        with self.assertRaises(SafetyViolation):
            policy.validate_readback(VectorField(0, 9.01), command=VectorField(0, 3))

    def test_custom_tolerance_never_suppresses_control_target_error_or_communication(self):
        session, _, _ = self.session()
        for changes in ({"field_control": 0}, {"setpoint_x_t": 0.001},
                        {"error_code": 35}, {"return_codes": {"get_field_x": 1}},
                        {"bx_t": math.nan}):
            with self.subTest(changes=changes):
                self.dll.field_control = 1
                self.dll.setpoint_x_t, self.dll.setpoint_z_t = 0, 3
                self.dll.bx_t, self.dll.bz_t = 0.003, 3.003
                self.dll.error_code = 0
                self.dll.return_codes = {}
                for key, value in changes.items():
                    setattr(self.dll, key, value)
                with self.assertRaises((AttoDryError, ValueError)):
                    session.capture("formal")

    def test_tolerance_change_keeps_healthy_getter_write_and_wait_schedule(self):
        traces = []
        for tolerance in ("0.0015", "0.005"):
            self.dll.events.clear()
            session, clock, _ = self.session(tolerance, residual=0)
            session.begin_sample()
            reading = session.read("magnetic")
            session.end_sample([reading])
            traces.append((tuple(self.dll.events), clock()))
            session.close()
        self.assertEqual(traces[0], traces[1])


if __name__ == "__main__":
    unittest.main()
