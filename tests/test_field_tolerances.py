"""Operator-approved tolerances, with fake DLL/VISA transports only."""
from dataclasses import replace
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest

from attodry_control.attodry import AttoDryDriver, AttoDryError, AttoDryTimeout
from attodry_control.config import ConfigError, load_config
from attodry_control.magnetic_field import execute_ordered_field_points
from attodry_control.models import VectorField
from attodry_control.safety import FieldReadbackPolicy, FieldTransitionPolicy, SafetyViolation
from tests import test_attodry as fake
from tests import test_magnetic_field as magnetic_fixture
from tests import test_field_readback_policy as combination_fixture
from attodry_control.magnetic_field_cli import run as run_field_cli
from attodry_control.magnetic_field_monitor import read_progress_snapshot
from attodry_control.combination_analysis import load_combination_rows
from attodry_control.combination_hardware import load_hardware_combination
from attodry_control.combination_launch import launch_summary
from attodry_control.combination_terminal import launch_text


class FieldToleranceTests(unittest.TestCase):
    def driver(self):
        config = load_config("config/hardware.example.toml")
        criteria = replace(config.magnet.stability.criteria, tolerance=0.0015, dwell_s=2)
        stability = replace(config.magnet.stability, criteria=criteria, wait_timeout_s=8)
        config = replace(config, magnet=replace(config.magnet, stability=stability,
                                                readback_tolerance_t=0.0015))
        dll = fake.FakeAttoDryDll()
        driver = AttoDryDriver.from_config(config, dll=dll,
            connection_authorized=True, writes_authorized=True)
        driver.connect(monotonic=fake.StepClock(), sleeper=lambda _: None)
        dll.field_control = 1
        return driver, dll

    def test_controller_quantized_setpoint_is_acknowledged_without_rounding_command(self):
        config = load_config("config/hardware.example.toml")
        dll = fake.FakeAttoDryDll()
        exact_commands = []
        def set_z(value):
            exact_commands.append(value.value)
            dll.setpoint_z_t = round(value.value, 4)
            dll.bz_t = dll.setpoint_z_t
            return 0
        dll.AttoDRY_Interface_setUserMagneticFieldZ = set_z
        driver = AttoDryDriver.from_config(config, dll=dll,
            connection_authorized=True, writes_authorized=True)
        driver.connect(monotonic=fake.StepClock(), sleeper=lambda _: None)
        events = []
        result = execute_ordered_field_points(driver, (VectorField(0, -0.49444444444444446),),
            0.5, transition_policy=FieldTransitionPolicy.DIRECT, on_event=events.append,
            monotonic=fake.StepClock(), sleeper=lambda _: None)
        self.assertAlmostEqual(exact_commands[0], -0.49444442987442017)
        self.assertAlmostEqual(result[0].final_state.field_setpoint.bz_t, -0.4943999946117401)
        command = next(e for e in events if e["event"] == "field_command_result"
                       and e["command_kind"] == "set_field_component")
        self.assertEqual(command["setpoint_ack_tolerance_t"], 0.0001)

    def test_config_accepts_one_shared_readback_tolerance_and_rejects_conflicts(self):
        source = Path("config/hardware.example.toml").read_text(encoding="utf-8")
        # Works whether the public template has migrated or still uses the old key.
        import re
        source = re.sub(r"(?m)^(?:field|readback)_tolerance_t = .*", "readback_tolerance_t = 0.0015", source)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hardware.toml"
            (path.parent / "lockin_safety.toml").write_bytes(Path("config/lockin_safety.toml").read_bytes())
            path.write_text(source, encoding="utf-8")
            config = load_config(path)
            self.assertEqual(config.magnet.readback_tolerance_t, 0.0015)
            self.assertEqual(config.magnet.stability.criteria.tolerance, 0.0015)
            for bad in ("0", "0.0002", "nan", '"0.0001"'):
                path.write_text(source.replace("setpoint_ack_tolerance_t = 0.0001",
                    "setpoint_ack_tolerance_t = " + bad), encoding="utf-8")
                with self.subTest(ack=bad), self.assertRaises(ConfigError):
                    load_config(path)
            path.write_text(source.replace("readback_tolerance_t = 0.0015",
                "field_tolerance_t = 0.001"), encoding="utf-8")
            legacy = load_config(path)
            self.assertIsNone(legacy.magnet.readback_tolerance_t)
            self.assertEqual(legacy.magnet.stability.criteria.tolerance, 0.001)
            for bad in ("0", "0.0016", "nan", '"0.0015"'):
                path.write_text(source.replace("readback_tolerance_t = 0.0015",
                    "readback_tolerance_t = " + bad), encoding="utf-8")
                with self.subTest(bad=bad), self.assertRaises(ConfigError):
                    load_config(path)
            path.write_text(source.replace("readback_tolerance_t = 0.0015",
                "readback_tolerance_t = 0.0015\nfield_tolerance_t = 0.001"), encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path)

    def test_ack_setting_is_independent_and_bounded(self):
        driver, dll = self.driver()
        self.assertEqual(driver.setpoint_ack_tolerance_t, 0.0001)
        self.assertFalse(driver._field_matches(VectorField(0.0002, 0), VectorField(0, 0)))
        def wrong_setpoint(value):
            dll.setpoint_z_t = value.value + 0.0002
            dll.bz_t = dll.setpoint_z_t
            return 0
        dll.AttoDRY_Interface_setUserMagneticFieldZ = wrong_setpoint
        with self.assertRaisesRegex(AttoDryTimeout, "setpoint_z_ack"):
            execute_ordered_field_points(driver, (VectorField(0, 0.1),), 0.5,
                transition_policy=FieldTransitionPolicy.DIRECT,
                monotonic=fake.StepClock(), sleeper=lambda _: None)

    def test_wider_ack_does_not_skip_new_small_field_command(self):
        driver, dll = self.driver()
        execute_ordered_field_points(driver, (VectorField(0, 0.00005),), 0.5,
            transition_policy=FieldTransitionPolicy.DIRECT,
            monotonic=fake.StepClock(), sleeper=lambda _: None)
        self.assertIn("set_field_z", dll.events)
        self.assertAlmostEqual(dll.setpoint_z_t, 0.00005)

    def test_new_inactive_guard_and_legacy_archive_remain_distinct(self):
        old = FieldReadbackPolicy.from_targets((VectorField(0, 8.9),))
        new = FieldReadbackPolicy.from_targets((VectorField(0, 8.9),),
                                              readback_tolerance_t=0.0015)
        actual = VectorField(0.0007, 8.9)
        self.assertEqual(FieldReadbackPolicy.from_snapshot(old.snapshot()), old)
        with self.assertRaises(SafetyViolation):
            FieldReadbackPolicy.from_snapshot(old.snapshot()).validate_readback(actual)
        self.assertEqual(FieldReadbackPolicy.from_snapshot(new.snapshot()).validate_readback(actual), actual)
        for actual in (VectorField(0.00151, 8.9), VectorField(0, 9.0001)):
            with self.subTest(actual=actual), self.assertRaises(SafetyViolation):
                new.validate_readback(actual)
        with self.assertRaises(SafetyViolation):
            new.validate_target(VectorField(0, 9.0001))
        with self.assertRaises(ValueError):
            FieldReadbackPolicy.from_snapshot({**new.snapshot(), "zero_magnitude_tolerance_t": 0.002})

    def test_boundary_margin_and_vector_limit_are_not_expanded(self):
        for targets, actual in (
            ((VectorField(3, 0),), VectorField(3.0006, 0)),
            ((VectorField(1, 0), VectorField(0, 1)), VectorField(3.0006, 0)),
        ):
            policy = FieldReadbackPolicy.from_targets(targets, readback_tolerance_t=0.0015)
            with self.subTest(targets=targets), self.assertRaises(SafetyViolation):
                policy.validate_readback(actual)

    def test_zero_uses_shared_norm_tolerance_and_stable_dwell(self):
        driver, dll = self.driver()
        driver.field_readback_policy = FieldReadbackPolicy.from_targets(
            (VectorField(0, 8.9),), readback_tolerance_t=0.0015)
        def zero():
            dll.setpoint_x_t = dll.setpoint_z_t = 0
            dll.bx_t, dll.bz_t = 0.0007, 0.0012
            return 0
        dll.AttoDRY_Interface_sweepFieldToZero = zero
        clock = fake.StepClock()
        state = driver.request_zero_field(monotonic=clock, sleeper=lambda _: None)
        self.assertGreater(state.field.magnitude_t, 0.001)
        self.assertLess(state.field.magnitude_t, 0.0015)
        self.assertGreater(dll.events.count("get_field_x"), 2)

    def test_component_pass_does_not_certify_zero_when_norm_fails(self):
        driver, dll = self.driver()
        def zero():
            dll.setpoint_x_t = dll.setpoint_z_t = 0
            dll.bx_t = dll.bz_t = 0.0012
            return 0
        dll.AttoDRY_Interface_sweepFieldToZero = zero
        with self.assertRaisesRegex(AttoDryTimeout, "Field stability"):
            driver.request_zero_field(monotonic=fake.StepClock(), sleeper=lambda _: None)

    def test_zero_requires_independent_setpoint_ack(self):
        driver, dll = self.driver()
        def zero():
            dll.bx_t = dll.bz_t = 0
            dll.setpoint_x_t = 0.0002
            return 0
        dll.AttoDRY_Interface_sweepFieldToZero = zero
        with self.assertRaisesRegex(AttoDryTimeout, "zero_setpoint_ack"):
            driver.request_zero_field(monotonic=fake.StepClock(), sleeper=lambda _: None)

    def test_zero_communication_failure_preserves_last_confirmed_state(self):
        driver, dll = self.driver()
        dll.bz_t = dll.setpoint_z_t = 1
        previous = driver.read_state()
        dll.return_codes["get_field_z"] = 1
        with self.assertRaises(AttoDryError):
            driver.request_zero_field(monotonic=fake.StepClock(), sleeper=lambda _: None)
        self.assertEqual(driver.last_confirmed_state, previous)
        self.assertNotIn("sweep_zero", dll.events)


class UnifiedToleranceCliTests(magnetic_fixture._MagneticConfigFixture, unittest.TestCase):
    def test_quantized_ack_hold_and_versioned_file_monitor(self):
        path = self.write_config(((0, -0.49444444444444446),), transition_policy="direct")
        path.write_text(path.read_text().replace("field_tolerance_t = 0.001",
            "readback_tolerance_t = 0.0015"), encoding="utf-8")
        dll = fake.FakeAttoDryDll()
        def set_z(value):
            dll.setpoint_z_t = round(value.value, 4)
            dll.bz_t = dll.setpoint_z_t
            dll.bx_t = 0.0007
            return 0
        dll.AttoDRY_Interface_setUserMagneticFieldZ = set_z
        output = io.StringIO()
        with redirect_stdout(output):
            code = run_field_cli(["scan", "--config", str(path)], dll_loader=lambda _: dll,
                                 monotonic=fake.StepClock(), sleeper=lambda _: None)
        self.assertEqual(code, 0, output.getvalue())
        snapshot = read_progress_snapshot(Path(json.loads(output.getvalue())["progress_jsonl"]))
        self.assertEqual(snapshot["outcome"], "completed", snapshot)
        self.assertTrue(snapshot["audit_complete"])
        self.assertEqual(snapshot["field_readback_policy"]["version"], "planned-axis-readback-v3")


class UnifiedToleranceCombinationTests(unittest.TestCase):
    setUp = combination_fixture.FieldPolicyHardwareCombinationTests.setUp
    write_config = combination_fixture.FieldPolicyHardwareCombinationTests.write_config
    factory = combination_fixture.FieldPolicyHardwareCombinationTests.factory
    events = combination_fixture.FieldPolicyHardwareCombinationTests.events
    execute = combination_fixture.FieldPolicyHardwareCombinationTests.execute
    points = combination_fixture.FieldPolicyHardwareCombinationTests.points

    def configure(self, points, order=("magnetic",)):
        self.base = self.base.replace("field_tolerance_t = 0.001", "readback_tolerance_t = 0.0015")
        self.points(points, order)

    def test_high_z_hold_and_formal_capture_keep_residual_and_tolerances(self):
        self.base = self.base.replace('normal_end_field_policy = "zero"', 'normal_end_field_policy = "hold"')
        self.configure([(0, 8.9)], ("magnetic", "lockin"))
        self.dll.bz_t = self.dll.setpoint_z_t = 8.9
        self.dll.bx_t = 0.0007
        self.dll.field_control = 1
        summary = launch_summary(load_hardware_combination(self.path), self.database, "offline")
        self.assertIn("Verified zero: |B| <= 1.5 mT", launch_text(summary, width=200))
        result = self.execute()
        self.assertEqual(result["status"], "completed", result)
        for row in load_combination_rows(self.database):
            self.assertAlmostEqual(row["actual.field_x_t"], 0.0007)
            self.assertEqual(row["status.magnetic"]["field_readback_policy"]["readback_tolerance_t"], 0.0015)

    def test_failed_electrical_run_can_verify_zero_above_old_one_mt(self):
        self.configure([(0, 8.9)], ("magnetic", "smu"))
        def modify(adapter):
            original = adapter.read
            def read():
                result = original()
                if adapter.read_count == 2:
                    self.dll.bx_t = 0.0007
                    raise RuntimeError("Injected electrical failure")
                return result
            adapter.read = read
        self.modify_adapter = modify
        def zero():
            self.dll.setpoint_x_t = self.dll.setpoint_z_t = 0
            self.dll.bx_t, self.dll.bz_t = 0.0007, 0.0012
            return 0
        self.dll.AttoDRY_Interface_sweepFieldToZero = zero
        result = self.execute()
        self.assertEqual(result["status"], "failed", result)
        action = next(a for a in result["cleanup"]["actions"] if a["module"] == "magnetic")
        self.assertTrue(action["result"]["verified"], action)
        self.assertTrue(result["cleanup"]["manual_verification_required"])

    def test_zero_target_formal_window_rejects_excess_norm(self):
        self.configure([(0, 0)], ("magnetic", "smu"))
        def modify(adapter):
            original = adapter.read
            def read():
                result = original()
                if adapter.read_count == 2:
                    self.dll.bx_t = self.dll.bz_t = 0.0012
                return result
            adapter.read = read
        self.modify_adapter = modify
        result = self.execute()
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("target tolerance", result["error"])
        self.assertEqual(load_combination_rows(self.database), ())


if __name__ == "__main__":
    unittest.main()
