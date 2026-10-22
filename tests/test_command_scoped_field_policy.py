"""Command-scoped magnetic envelopes, using fake devices only."""
from dataclasses import replace
import math
import unittest

from attodry_control.attodry import AttoDryDriver
from attodry_control.config import load_config
from attodry_control.magnetic_field import execute_ordered_field_points
from attodry_control.models import CryostatState, VectorField
from attodry_control.safety import (
    FieldReadbackPolicy, FieldTransitionPolicy, MagnetLimits, SafetyViolation,
    float32_field, plan_field_transition,
)
from tests.test_attodry import FakeAttoDryDll, StepClock


class CommandScopedFieldPolicyTests(unittest.TestCase):
    def policy(self, points=((0, 4), (1, 0)), limits=None):
        return FieldReadbackPolicy.from_targets(
            tuple(VectorField(*p) for p in points), limits or MagnetLimits(),
            0.0015, command_scoped=True)

    def test_single_axis_targets_use_only_their_configured_axis_limits(self):
        policy = self.policy()
        policy.validate_target(VectorField(0, 9))
        policy.validate_target(VectorField(3, 0))
        for point in (VectorField(0, 9.00001), VectorField(3.00001, 0)):
            with self.assertRaises(SafetyViolation):
                policy.validate_target(point)
        with self.assertRaises(SafetyViolation):
            self.policy(((0, 4),), MagnetLimits(3, 3, 3))

    def test_dual_axis_commands_have_no_small_component_or_float32_exception(self):
        policy = self.policy()
        policy.validate_target(VectorField(2, 2))
        for point in (VectorField(2.5, 2.5), VectorField(1e-12, 4),
                      float32_field(VectorField(2, math.sqrt(5)))):
            with self.assertRaises(SafetyViolation):
                policy.validate_target(point)

    def test_actual_mode_is_explicit_command_not_actual_zero_or_complete_plan(self):
        policy = self.policy()
        policy.validate_readback(VectorField(0.001, 4), command=VectorField(0, 4))
        with self.assertRaises(SafetyViolation):
            policy.validate_readback(VectorField(0.002, 4), command=VectorField(0, 4))
        with self.assertRaises(SafetyViolation):
            policy.validate_readback(VectorField(0, 4), command=VectorField(1, 1))
        with self.assertRaisesRegex(SafetyViolation, "explicit"):
            policy.validate_readback(VectorField(0, 4))

    def test_new_policy_never_inherits_old_readback_limit_margins(self):
        policy = self.policy(((0, 3),), MagnetLimits(3, 3, 3))
        for actual in (VectorField(0, math.nextafter(3, math.inf)), VectorField(0, 3.0004)):
            with self.assertRaises(SafetyViolation):
                policy.validate_readback(actual, command=VectorField(0, 3))
        with self.assertRaises(SafetyViolation):
            self.policy().validate_readback(VectorField(2.1214, 2.1214), command=VectorField(2, 2))
        self.assertEqual(policy.snapshot()["readback_margin_t"], 0)

    def test_inactive_residual_guard_cannot_expand_a_smaller_configured_axis_cap(self):
        for command, actual, limits in (
            (VectorField(0, 1), VectorField(0.0014, 1), MagnetLimits(0.001, 3, 3)),
            (VectorField(1, 0), VectorField(1, 0.0014), MagnetLimits(3, 0.001, 3)),
        ):
            policy = self.policy(((command.bx_t, command.bz_t),), limits)
            with self.assertRaises(SafetyViolation):
                policy.validate_readback(actual, command=command)
            inactive_axis = "x" if command.bx_t == 0 else "z"
            self.assertEqual(policy.snapshot()["inactive_axis_readback_limits_t"][inactive_axis], 0.001)
            self.assertEqual(policy.snapshot()["axis_readback_limits_t"][inactive_axis], 0.001)

    def test_snapshot_versions_round_trip_without_reinterpreting_archives(self):
        new = self.policy()
        self.assertEqual(new, FieldReadbackPolicy.from_snapshot(new.snapshot()))
        self.assertEqual(new.snapshot()["version"], "command-scoped-readback-v1")
        for legacy in (
            FieldReadbackPolicy.from_targets((VectorField(0, 4),)),
            FieldReadbackPolicy.from_targets((VectorField(1, 1),), strict_resultant=True),
        ):
            self.assertEqual(legacy, FieldReadbackPolicy.from_snapshot(legacy.snapshot()))
            self.assertNotIn("command_scoped", legacy.snapshot())
        with self.assertRaises(ValueError):
            FieldReadbackPolicy.from_snapshot({**new.snapshot(), "readback_margin_t": 0.0005})

    def test_high_single_axis_transition_cannot_cross_an_unsafe_dual_axis_corner(self):
        with self.assertRaises(SafetyViolation):
            plan_field_transition(VectorField(0, 4), VectorField(1, 0), 0.05,
                                  FieldTransitionPolicy.DIRECT)
        plan = plan_field_transition(VectorField(0, 4), VectorField(1, 0), 0.05,
                                     FieldTransitionPolicy.VIA_ZERO)
        self.assertTrue(any(p.command_field.magnitude_t == 0 for p in plan.waypoints))


class CommandScopedDriverTests(unittest.TestCase):
    def driver(self, dll, limits=MagnetLimits()):
        config = load_config("config/hardware.example.toml")
        driver = AttoDryDriver.from_config(config, dll=dll,
            connection_authorized=True, writes_authorized=True)
        driver.limits = limits
        driver.field_stability = replace(driver.field_stability,
            wait_timeout_s=40, criteria=replace(driver.field_stability.criteria,
            dwell_s=2, minimum_samples=2))
        driver.connect(monotonic=StepClock(), sleeper=lambda _: None)
        self.addCleanup(driver.close)
        return driver

    def test_zero_command_retains_known_single_axis_rundown_context(self):
        dll = FakeAttoDryDll()
        driver = self.driver(dll)
        driver.field_readback_policy = FieldReadbackPolicy.from_targets(
            (VectorField(0, 4), VectorField(1, 0)), driver.limits, 0.0015,
            command_scoped=True)
        state = CryostatState(2, 2, 2.1, VectorField(0.001, 4),
                             VectorField(0, 4), False, True, 0)
        driver._validate_field_state(state)
        zeroing = replace(state, field_setpoint=VectorField(0, 0))
        self.assertEqual(driver.field_readback_context(zeroing), VectorField(0, 4))
        driver._validate_field_state(zeroing)
        with self.assertRaises(SafetyViolation):
            driver._validate_field_state(replace(zeroing, field=VectorField(0.002, 4)))
        with self.assertRaises(SafetyViolation):
            driver._validate_field_actual(VectorField(0.05, 4), command=VectorField(0.05, 0))

    def test_unknown_zero_in_mixed_plan_does_not_assume_high_z_rundown(self):
        driver = self.driver(FakeAttoDryDll())
        driver.field_readback_policy = FieldReadbackPolicy.from_targets(
            (VectorField(0, 4), VectorField(1, 0)), driver.limits,
            0.0015, command_scoped=True)
        state = CryostatState(2, 2, 2.1, VectorField(0, 4),
                             VectorField(0, 0), False, True, 0)
        with self.assertRaises(SafetyViolation):
            driver._validate_field_state(state)

    def test_execute_mixed_high_z_via_zero_preserves_waits_and_safe_axis_write_order(self):
        dll = FakeAttoDryDll()
        dll.bz_t = dll.setpoint_z_t = 4
        dll.field_control = 1
        driver = self.driver(dll)
        points = (VectorField(0, 4), VectorField(1, 0))
        policy = FieldReadbackPolicy.from_targets(points, driver.limits, 0.0015,
                                                 command_scoped=True)
        events = []
        result = execute_ordered_field_points(driver, points, 0.5,
            transition_policy=FieldTransitionPolicy.VIA_ZERO,
            field_readback_policy=policy, on_event=events.append,
            monotonic=StepClock(), sleeper=lambda _: None)
        self.assertEqual(result[-1].final_state.field, VectorField(1, 0))
        self.assertTrue(any(e.get("phase") == "waypoint_stability" for e in events))
        self.assertTrue(any(e.get("event") == "setpoint_confirmed" for e in events))

    def test_new_boundary_does_not_change_same_scan_getter_write_or_wait_counts(self):
        traces = []
        for command_scoped in (False, True):
            dll = FakeAttoDryDll()
            dll.bz_t = dll.setpoint_z_t = 1
            dll.field_control = 1
            driver = self.driver(dll, MagnetLimits(3, 3, 3))
            points = (VectorField(0, 1.3),)
            policy = FieldReadbackPolicy.from_targets(points, driver.limits, 0.0015,
                strict_resultant=not command_scoped, command_scoped=command_scoped)
            sleeps = []
            dll.events.clear()
            execute_ordered_field_points(driver, points, 0.05,
                transition_policy=FieldTransitionPolicy.DIRECT,
                field_readback_policy=policy, monotonic=StepClock(), sleeper=sleeps.append)
            traces.append((list(dll.events), sleeps))
        self.assertEqual(traces[0], traces[1])
