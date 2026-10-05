"""Strict photonics field envelope; pure policy and injected fake DLL only."""
from dataclasses import replace
from types import SimpleNamespace
import unittest

from attodry_control.attodry import AttoDryError
from attodry_control.config import load_config
from attodry_control.cryostat_points import CryostatPointSession
from attodry_control.models import VectorField
from attodry_control.safety import (
    AxisWriteOrder, FieldReadbackPolicy, FieldScanMode, FieldTransitionPolicy,
    MagnetLimits, SafetyViolation, plan_field_transition,
)
from tests.test_attodry import FakeAttoDryDll, StepClock


class PhotonicsFieldPolicyTests(unittest.TestCase):
    def policy(self, point=(0, 3), limits=MagnetLimits(3, 3, 3)):
        return FieldReadbackPolicy.from_targets((VectorField(*point),), limits,
                                               readback_tolerance_t=0.0015,
                                               strict_resultant=True)

    def test_strict_pure_axis_targets_include_exact_boundary_only(self):
        for point in ((3, 0), (-3, 0), (0, 3), (0, -3)):
            policy = self.policy(point)
            self.assertEqual(policy.validate_target(VectorField(*point)), VectorField(*point))
        for point in ((0, 3.00001), (0, -3.00001), (0, 9)):
            with self.subTest(point=point), self.assertRaises(SafetyViolation):
                # Strict policy protects targets even if a caller supplies legacy ratings.
                self.policy(point, MagnetLimits())

    def test_strict_reduced_vector_limit_also_applies_to_pure_axis(self):
        policy = self.policy((0, 1), MagnetLimits(3, 3, 2))
        for point in ((0, 2.00001), (2.00001, 0)):
            with self.subTest(point=point), self.assertRaises(SafetyViolation):
                policy.validate_readback(VectorField(*point))
        with self.assertRaises(SafetyViolation):
            policy.validate_target(VectorField(0, 2.00001))

    def test_no_legacy_readback_margin_and_no_residual_norm_exception(self):
        for point, actual in (
            ((3, 0), (3.00001, 0)), ((0, 3), (0, 3.00001)),
            ((3, 0), (3, 0.0001)), ((0, 3), (0.0001, 3)),
        ):
            policy = self.policy(point)
            with self.subTest(point=point), self.assertRaises(SafetyViolation):
                policy.validate_readback(VectorField(*actual))
            self.assertFalse(policy.assessment(VectorField(*actual))["within_readback_policy"])
            self.assertTrue(policy.assessment(VectorField(*actual))["nominal_limit_exceeded"])

    def test_inactive_axis_guard_still_applies_inside_resultant_envelope(self):
        policy = self.policy((0, 2))
        policy.validate_readback(VectorField(0.001, 2))
        with self.assertRaises(SafetyViolation):
            policy.validate_readback(VectorField(0.002, 2))

    def test_strict_snapshot_is_explicit_and_round_trips(self):
        policy = self.policy()
        snapshot = policy.snapshot()
        self.assertEqual(snapshot["version"], "strict-resultant-readback-v1")
        self.assertEqual(snapshot["readback_margin_t"], 0)
        self.assertEqual(snapshot["vector_readback_limit_t"], 3)
        self.assertEqual(snapshot["axis_readback_limits_t"]["z"], 3)
        self.assertTrue(snapshot["strict_resultant"])
        self.assertEqual(FieldReadbackPolicy.from_snapshot(snapshot), policy)
        for key, value in (("readback_margin_t", 0.0005), ("strict_resultant", False),
                           ("vector_readback_limit_t", 3.0005), ("version", "planned-axis-readback-v3")):
            with self.subTest(key=key), self.assertRaises(ValueError):
                FieldReadbackPolicy.from_snapshot({**snapshot, key: value})

    def test_legacy_snapshots_keep_original_shape_and_behavior(self):
        for tolerance, version in ((None, "planned-axis-readback-v2"),
                                   (0.0015, "planned-axis-readback-v3")):
            policy = FieldReadbackPolicy.from_targets((VectorField(0, 8.9),),
                                                       readback_tolerance_t=tolerance)
            snapshot = policy.snapshot()
            self.assertNotIn("strict_resultant", snapshot)
            self.assertEqual(snapshot["version"], version)
            self.assertEqual(snapshot["axis_readback_limits_t"]["z"], 9)
            self.assertIsNone(snapshot["vector_readback_limit_t"])
            self.assertEqual(snapshot["readback_margin_t"], 0.0005)
            self.assertEqual(FieldReadbackPolicy.from_snapshot(snapshot), policy)
            policy.validate_readback(VectorField(0.0003, 8.9))
        legacy = FieldReadbackPolicy.from_targets((VectorField(3, 0),))
        legacy.validate_readback(VectorField(3.0004, 0))

    def test_strict_flag_requires_boolean(self):
        for invalid in (1, 0, "true", None):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                FieldReadbackPolicy(FieldScanMode.VECTOR, strict_resultant=invalid)

    def test_float32_rounding_cannot_cross_nominal_sphere(self):
        requested = VectorField(1.8, 2.4)
        self.assertEqual(requested.magnitude_t, 3)
        with self.assertRaises(SafetyViolation):
            plan_field_transition(VectorField(0, 0), requested, 0.5,
                                  FieldTransitionPolicy.DIRECT, MagnetLimits(3, 3, 3))

    def test_component_write_order_only_uses_safe_actual_corner(self):
        limits = MagnetLimits(3, 3, 3)
        plan = plan_field_transition(VectorField(2.5, 1), VectorField(1, 2.5), 3,
                                     FieldTransitionPolicy.DIRECT, limits)
        for waypoint in plan.waypoints:
            selected = (waypoint.x_then_z_mixed_corner
                        if waypoint.axis_order is AxisWriteOrder.X_THEN_Z
                        else waypoint.z_then_x_mixed_corner)
            self.assertLessEqual(selected.magnitude_t, 3)
            self.assertLessEqual(waypoint.command_field.magnitude_t, 3)


class PhotonicsCryostatSafetyTests(unittest.TestCase):
    def make_session(self, *, magnetic=True, strict=True):
        source = load_config("config/hardware.example.toml")
        stability = replace(source.magnet.stability, wait_timeout_s=20,
                            criteria=replace(source.magnet.stability.criteria,
                                             dwell_s=2, minimum_samples=2))
        magnet = replace(source.magnet, limits=MagnetLimits(3, 3, 3), stability=stability)
        config = SimpleNamespace(project=source.project, cryostat=source.cryostat,
                                 magnet=magnet, temperature_stability=source.temperature_stability,
                                 strict_resultant=strict)
        run = SimpleNamespace(points=(VectorField(0, 0.1),), max_step_t=0.05,
                              transition_policy=FieldTransitionPolicy.DIRECT)
        operation = SimpleNamespace(run=run, cleanup=SimpleNamespace(
            normal_end_field_policy=SimpleNamespace(value="zero"))) if magnetic else None
        self.dll = FakeAttoDryDll()
        self.events = []
        session = CryostatPointSession(config, None, operation,
            lambda kind, value: self.events.append((kind, value)), dll=self.dll,
            monotonic=StepClock(), sleep=lambda _: None)
        self.addCleanup(session.close)
        return session

    def test_temperature_only_profile_still_checks_field_sphere(self):
        session = self.make_session(magnetic=False)
        self.assertEqual(session.field_policy.mode, FieldScanMode.VECTOR)
        self.assertTrue(session.field_policy.strict_resultant)
        session.open()
        self.dll.bx_t = 2.2
        self.dll.bz_t = 2.2
        with self.assertRaises(SafetyViolation):
            session.driver.read_state()
        self.assertNotIn("sweep_zero", self.dll.events)
        self.assertNotIn("toggle_field_control", self.dll.events)

    def test_uncapped_command_envelope_rejected_before_connection(self):
        session = self.make_session()
        session.config.magnet = replace(session.config.magnet, limits=MagnetLimits())
        with self.assertRaisesRegex(ValueError, "command axis limits"):
            CryostatPointSession(session.config, None, session.magnetic, lambda *_: None,
                                 dll=self.dll)
        self.assertEqual(self.dll.events, [])

    def test_strict_recovery_keeps_original_policy_and_limits(self):
        session = self.make_session()
        session.cleaning = True
        for action in ("zero", "disable"):
            with session._recovery(action):
                self.assertIs(session.active_field_policy, session.field_policy)
                self.assertIs(session.field_readback_limits, session.config.magnet.limits)
                self.assertEqual(session.active_field_policy.snapshot()["readback_margin_t"], 0)
        self.assertIsNone(session.recovery_action)

    def test_owned_within_limit_zero_is_verified(self):
        session = self.make_session()
        session.open()
        session.touched.add("magnetic")
        self.dll.field_control = 1
        self.dll.bz_t = self.dll.setpoint_z_t = 0.1
        result = session.cleanup("magnetic", True)
        self.assertTrue(result["verified"])
        self.assertEqual(result["state"]["field"], {"bx_t": 0, "bz_t": 0})
        self.assertEqual(self.dll.events.count("sweep_zero"), 1)

    def test_over_limit_cleanup_cannot_escalate_to_legacy_nine_tesla(self):
        session = self.make_session()
        session.open()
        session.touched.add("magnetic")
        self.dll.field_control = 1
        self.dll.bz_t = self.dll.setpoint_z_t = 0.1
        session.driver.read_state()
        self.dll.bz_t = 3.001
        with self.assertRaises(SafetyViolation):
            session.cleanup("magnetic", True)
        self.assertNotIn("sweep_zero", self.dll.events)
        self.assertIsNone(session.recovery_action)
        self.assertTrue(session.recovery_scan_limits_violated)
        # Complete but unsafe readback stays in raw audit, never a zero inference.
        self.assertGreater(session.driver.last_confirmed_state.field.bz_t, 3)

    def test_communication_failure_keeps_last_confirmed_nonzero_state(self):
        session = self.make_session()
        session.open()
        session.touched.add("magnetic")
        self.dll.field_control = 1
        self.dll.bz_t = self.dll.setpoint_z_t = 0.1
        last = session.driver.read_state()
        self.dll.return_codes["get_field_z"] = 1
        with self.assertRaises(AttoDryError):
            session.cleanup("magnetic", True)
        self.assertEqual(session.driver.last_confirmed_state, last)
        self.assertGreater(session.driver.last_confirmed_state.field.bz_t, 0)
        self.assertNotIn("sweep_zero", self.dll.events)
        self.assertIsNone(session.recovery_action)


if __name__ == "__main__":
    unittest.main()
