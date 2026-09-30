"""2026-09-30 operator-approved safety-policy change; offline regression only.

The user explicitly approved changing the previous exact-zero readback policy:
single-axis mode comes from the complete target plan, inactive-axis readback
remains independently limited to 0.5 mT, and the 3 T readback boundary gets a
0.5 mT margin. Requested/float32 targets keep their strict original limits.
This test uses only FakeAttoDryDll, not a loaded vendor DLL or instrument.
Real commissioning and deployment are separate, not authorized by this test.
"""
from dataclasses import replace
from contextlib import redirect_stdout
import io
import json
import math
from pathlib import Path
import tempfile
import unittest

from attodry_control.attodry import AttoDryDriver
from attodry_control.config import ConfigError, load_config, load_magnetic_field_operation_config
from attodry_control.combination_scan import (AxisPoint, ScanAxis, CombinationPlan,
    SimulatedCombinationStation, run_simulated_combination)
from attodry_control.combination_store import CombinationStore
from attodry_control.field_audit import read_jsonl_events
from attodry_control.magnetic_field import execute_ordered_field_points
from attodry_control.magnetic_field_cli import run as run_field_cli
from attodry_control.magnetic_field_monitor import read_progress_snapshot
from attodry_control.models import VectorField
from attodry_control.safety import (FieldReadbackPolicy, FieldScanMode, FieldTransitionPolicy,
    MagnetLimits, SafetyViolation, float32_value)
from tests.test_attodry import FakeAttoDryDll, StepClock
from tests.test_magnetic_field import _MagneticConfigFixture
from tests import test_combination_cryostat as hardware_fixture
from attodry_control.combination_analysis import load_combination_rows


class FieldReadbackRegressionTests(unittest.TestCase):
    def test_zero_target_cross_axis_does_not_abort_three_tesla_scan(self):
        dll = FakeAttoDryDll()
        dll.bx_t = dll.setpoint_x_t = -3.0
        dll.bz_t = 0.0003
        dll.field_control = 1
        config = load_config("config/hardware.example.toml")
        driver = AttoDryDriver.from_config(config, dll=dll,
            connection_authorized=True, writes_authorized=True)
        driver.field_stability = replace(driver.field_stability,
            wait_timeout_s=20, criteria=replace(driver.field_stability.criteria,
            dwell_s=2, minimum_samples=2))
        clock = StepClock()
        driver.connect(monotonic=clock, sleeper=lambda _: None)
        self.addCleanup(driver.close)
        events = []
        result = execute_ordered_field_points(driver,
            (VectorField(-3, 0), VectorField(-2.8, 0)), 0.5,
            transition_policy=FieldTransitionPolicy.DIRECT, on_event=events.append,
            monotonic=clock, sleeper=lambda _: None)
        self.assertEqual(len(result), 2)
        self.assertAlmostEqual(result[-1].final_state.field.bz_t, 0.0003)
        self.assertTrue(any(e.get("event") == "field_sample" and
            e["state"]["field"]["bz_t"] != 0 for e in events))


class PureFieldReadbackPolicyTests(unittest.TestCase):
    def policy(self, points, limits=None):
        return FieldReadbackPolicy.from_targets(tuple(VectorField(*p) for p in points),
            limits or MagnetLimits())

    def test_mode_comes_from_complete_plan_and_fixed_nonzero_axis_is_vector(self):
        for points, mode in [
            ([(0, 0), (3, 0), (-3, 0)], FieldScanMode.SINGLE_X),
            ([(0, 0), (0, 4)], FieldScanMode.SINGLE_Z),
            ([(0, 1), (1, 1)], FieldScanMode.VECTOR),
            ([(1, 0), (0, 1)], FieldScanMode.VECTOR),
            ([(0, 0)], FieldScanMode.VECTOR),
        ]:
            with self.subTest(points=points):
                self.assertEqual(self.policy(points).mode, mode)

    def test_inactive_axis_is_recorded_and_independently_limited(self):
        policy = self.policy([(3, 0)])
        actual = VectorField(3, 0.0003)
        self.assertIs(policy.validate_readback(actual), actual)
        self.assertTrue(policy.assessment(actual)["inactive_axis_nonzero"])
        policy.validate_readback(VectorField(3, float32_value(0.0005)))
        limit = policy.snapshot()["axis_readback_limits_t"]["z"]
        for z in (math.nextafter(limit, math.inf), -0.0006, 0.05):
            with self.subTest(z=z), self.assertRaises(SafetyViolation):
                policy.validate_readback(VectorField(3, z))

    def test_three_tesla_readback_margin_does_not_authorize_targets(self):
        policy = self.policy([(3, 0)])
        for x in (3.0004, -3.0004, 3.0005):
            with self.subTest(x=x):
                self.assertTrue(policy.assessment(VectorField(x, 0))["nominal_limit_exceeded"])
        for x in (math.nextafter(3.0005, math.inf), -3.0006):
            with self.assertRaises(SafetyViolation):
                policy.validate_readback(VectorField(x, 0))
        for x in (3.0001, 3.0005):
            with self.assertRaises(SafetyViolation):
                policy.validate_target(VectorField(x, 0))
        with self.assertRaisesRegex(SafetyViolation, "setpoint"):
            policy.validate_target(VectorField(2, 0.0003))

    def test_vector_mode_does_not_change_when_actual_axis_reads_zero(self):
        policy = self.policy([(0, 1), (1, 1)])
        policy.validate_readback(VectorField(0, 3.0004))
        with self.assertRaisesRegex(SafetyViolation, "Vector readback"):
            policy.validate_readback(VectorField(0, 3.0006))
        with self.assertRaises(SafetyViolation):
            policy.validate_readback(VectorField(2.1218, 2.1218))

    def test_standalone_high_z_keeps_rating_and_cross_axis_guard(self):
        policy = self.policy([(0, 4), (0, 9)])
        policy.validate_readback(VectorField(0.0003, 4))
        policy.validate_readback(VectorField(0.0003, 9))
        with self.assertRaises(SafetyViolation):
            policy.validate_readback(VectorField(0, 9.0001))
        with self.assertRaises(SafetyViolation):
            policy.validate_readback(VectorField(0.0006, 4))
        with self.assertRaises(ValueError):
            self.policy([(0, 4)], MagnetLimits(3, 3, 3))

    def test_mixed_axis_high_z_plan_rejects_even_at_pure_z_point(self):
        with self.assertRaisesRegex(SafetyViolation, "Vector scan target"):
            self.policy([(0, 4), (1, 0)])

    def test_snapshot_is_exact_versioned_contract_not_an_arbitrary_limit(self):
        policy = self.policy([(3, 0)])
        snapshot = policy.snapshot()
        self.assertEqual(FieldReadbackPolicy.from_snapshot(snapshot), policy)
        for key, value in [("readback_margin_t", 0.05),
                           ("inactive_axis_max_abs_t", 0.05), ("version", "future")]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                FieldReadbackPolicy.from_snapshot({**snapshot, key: value})
        with self.assertRaises(ValueError):
            VectorField(float("nan"), 0)


class FieldPolicyCliTests(_MagneticConfigFixture, unittest.TestCase):
    def execute(self, point, dll):
        path = self.write_config((point,), transition_policy="direct")
        output = io.StringIO()
        with redirect_stdout(output):
            run_field_cli(["scan", "--config", str(path)], dll_loader=lambda _: dll,
                monotonic=StepClock(), sleeper=lambda _: None)
        return Path(json.loads(output.getvalue())["progress_jsonl"])

    def test_new_monitor_accepts_residual_but_old_policy_is_not_reinterpreted(self):
        dll = FakeAttoDryDll()
        dll.bx_t = dll.setpoint_x_t = 3
        dll.bz_t = 0.0003
        dll.field_control = 1
        path = self.execute((3, 0), dll)
        snapshot = read_progress_snapshot(path)
        self.assertEqual(snapshot["outcome"], "completed", snapshot)
        self.assertTrue(snapshot["audit_complete"])
        self.assertTrue(snapshot["last_field_readback_assessment"]["inactive_axis_nonzero"])
        events, _ = read_jsonl_events(path)
        events[0].pop("field_readback_policy")
        path.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
        legacy = read_progress_snapshot(path)
        self.assertEqual(legacy["outcome"], "incomplete")
        self.assertTrue(any("violates recorded field limits" in e for e in legacy["integrity_errors"]))

    def test_high_z_single_axis_records_cross_axis_without_vector_reclassification(self):
        dll = FakeAttoDryDll()
        dll.bz_t = dll.setpoint_z_t = 4
        dll.bx_t = 0.0003
        dll.field_control = 1
        snapshot = read_progress_snapshot(self.execute((0, 4), dll))
        self.assertEqual(snapshot["outcome"], "completed", snapshot)
        self.assertAlmostEqual(snapshot["last_confirmed_state"]["field"]["bx_t"], 0.0003)
        self.assertEqual(snapshot["field_readback_policy"]["mode"], "single_z")

    def test_mixed_high_z_plan_rejects_offline(self):
        path = self.write_config(((0, 4), (1, 0)), transition_policy="via_zero")
        with self.assertRaises(ConfigError):
            load_magnetic_field_operation_config(path)


class FieldPolicyCombinationTests(unittest.TestCase):
    def test_formal_sample_uses_run_mode_and_records_margin(self):
        class ResidualStation(SimulatedCombinationStation):
            def read(self, module):
                result = super().read(module)
                result["actual"].update(field_x_t=3.0004, field_z_t=0.0003)
                return result
        plan = CombinationPlan((ScanAxis("magnetic",
            (AxisPoint({"field_x_t": 3, "field_z_t": 0}),)),))
        with tempfile.TemporaryDirectory() as directory:
            with CombinationStore(Path(directory) / "scan.sqlite") as store:
                result = run_simulated_combination(plan, store, "margin", station=ResidualStation())
                self.assertEqual(result["status"], "completed", result)
                raw = store.connection.execute("SELECT payload_json FROM combination_samples").fetchone()[0]
                self.assertTrue(json.loads(raw)["field_readback_assessment"]["nominal_limit_exceeded"])

    def test_inactive_axis_growth_and_true_vector_overlimit_reject_formal_data(self):
        for points, actual in [
            ([(3, 0)], (3, 0.0006)),
            ([(1, 0), (0, 1)], (0, 3.0006)),
        ]:
            class Fault(SimulatedCombinationStation):
                def read(self, module):
                    result = super().read(module)
                    result["actual"].update(field_x_t=actual[0], field_z_t=actual[1])
                    return result
            plan = CombinationPlan((ScanAxis("magnetic", tuple(AxisPoint(
                {"field_x_t": x, "field_z_t": z}) for x, z in points)),))
            with self.subTest(points=points), tempfile.TemporaryDirectory() as directory:
                with CombinationStore(Path(directory) / "scan.sqlite") as store:
                    result = run_simulated_combination(plan, store, "fault", station=Fault())
                    self.assertEqual(result["status"], "failed", result)
                    self.assertEqual(store.connection.execute(
                        "SELECT COUNT(*) FROM combination_attempts WHERE status='accepted'").fetchone()[0], 0)


class FieldPolicyHardwareCombinationTests(unittest.TestCase):
    """Exercise the actual session/driver/cleanup path with fake transports only."""
    setUp = hardware_fixture.CryostatCombinationTests.setUp
    write_config = hardware_fixture.CryostatCombinationTests.write_config
    factory = hardware_fixture.CryostatCombinationTests.factory
    events = hardware_fixture.CryostatCombinationTests.events
    execute = hardware_fixture.CryostatCombinationTests.execute

    def points(self, points, order=("magnetic",)):
        import re
        source = re.sub(r"(?ms)^points = \[.*?^\]", "points = [\n" +
            "\n".join(f"{{ bx_t = {x}, bz_t = {z} }}," for x, z in points) +
            "\n]", self.base, count=1)
        self.write_config(order, source=source)

    def test_single_x_boundary_and_transition_preserve_raw_z_in_formal_windows(self):
        self.points([(-3, 0), (-2.8, 0)])
        self.dll.bx_t = self.dll.setpoint_x_t = -3
        self.dll.bz_t = 0.0003
        self.dll.field_control = 1
        result = self.execute()
        self.assertEqual(result["status"], "completed", result)
        rows = load_combination_rows(self.database)
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertAlmostEqual(row["actual.field_z_t"], 0.0003)
            self.assertEqual(row["status.magnetic"]["field_readback_policy"]["mode"], "single_x")
            self.assertTrue(row["status.magnetic"]["field_readback_assessment"]["inactive_axis_nonzero"])

    def test_complete_vector_mode_persists_across_pure_axis_leaf_points(self):
        self.points([(0, 0.1), (0.1, 0)])
        result = self.execute()
        self.assertEqual(result["status"], "completed", result)
        rows = load_combination_rows(self.database)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["status.magnetic"]["field_readback_policy"]["mode"] == "vector"
                            for row in rows))

    def test_inactive_axis_drift_rejects_and_existing_owned_cleanup_verifies_zero(self):
        self.points([(0.1, 0)], ("magnetic", "smu"))
        def modify(adapter):
            original = adapter.read
            def read():
                result = original()
                if adapter.read_count == 2:
                    self.dll.bz_t = 0.0006
                return result
            adapter.read = read
        self.modify_adapter = modify
        result = self.execute()
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("readback Bz", result["error"])
        self.assertEqual(load_combination_rows(self.database), ())
        self.assertIn("sweep_zero", self.dll.events)
        action = next(a for a in result["cleanup"]["actions"] if a["module"] == "magnetic")
        self.assertTrue(action["result"]["verified"])
        self.assertTrue(action["result"]["scan_limits_violated"])
        self.assertEqual(action["result"]["state"]["field"], {"bx_t": 0.0, "bz_t": 0.0})
        self.assertTrue(result["cleanup"]["manual_verification_required"])

    def test_single_axis_communication_failure_does_not_certify_cleanup(self):
        self.points([(0.1, 0)], ("magnetic", "smu"))
        def modify(adapter):
            original = adapter.read
            def read():
                result = original()
                if adapter.read_count == 2:
                    self.dll.return_codes["get_field_z"] = 1
                return result
            adapter.read = read
        self.modify_adapter = modify
        result = self.execute()
        self.assertEqual(result["status"], "failed", result)
        action = next(a for a in result["cleanup"]["actions"] if a["module"] == "magnetic")
        self.assertFalse(action["clean"])
        self.assertIn("last_confirmed_state_not_current", action["result"])
        self.assertTrue(result["cleanup"]["manual_verification_required"])
