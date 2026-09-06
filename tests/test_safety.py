import math
import unittest

from attodry_control.models import VectorField
from attodry_control.safety import (
    AxisWriteOrder,
    FIELD_SETPOINT_READBACK_TOLERANCE_T,
    FieldTransitionPolicy,
    MagnetLimits,
    SafetyViolation,
    float32_bits_hex,
    plan_field_transition,
    plan_ordered_field_transitions,
    validate_vector_field,
)


class MagnetSafetyTests(unittest.TestCase):
    def test_boundary_vector_is_allowed(self) -> None:
        target = VectorField(1.8, 2.4)

        self.assertIs(validate_vector_field(target), target)
        self.assertAlmostEqual(target.magnitude_t, 3.0)

    def test_resultant_field_above_three_tesla_is_rejected(self) -> None:
        with self.assertRaisesRegex(SafetyViolation, "project limit"):
            validate_vector_field(VectorField(2.0, 2.5))

    def test_pure_z_is_still_limited_to_three_tesla_by_project_policy(self) -> None:
        with self.assertRaisesRegex(SafetyViolation, "project limit"):
            validate_vector_field(VectorField(0.0, 3.1))

    def test_hardware_axis_limits_are_checked_independently(self) -> None:
        limits = MagnetLimits(
            hardware_x_max_t=1.0,
            hardware_z_max_t=9.0,
            experiment_vector_max_t=3.0,
        )

        with self.assertRaisesRegex(SafetyViolation, "Bx"):
            validate_vector_field(VectorField(1.1, 0.0), limits)

    def test_direct_limits_cannot_raise_project_invariant_above_three_tesla(
        self,
    ) -> None:
        with self.assertRaisesRegex(ValueError, "confirmed 3 T"):
            MagnetLimits(
                hardware_x_max_t=3.0,
                hardware_z_max_t=9.0,
                experiment_vector_max_t=4.0,
            )


class FieldTransitionPlanningTests(unittest.TestCase):
    def test_direct_and_via_zero_are_distinct_explicit_policies(self) -> None:
        start = VectorField(1.0, 0.0)
        target = VectorField(0.0, 1.0)

        direct = plan_field_transition(
            start,
            target,
            0.5,
            FieldTransitionPolicy.DIRECT,
        )
        via_zero = plan_field_transition(
            start,
            target,
            0.5,
            FieldTransitionPolicy.VIA_ZERO,
        )

        self.assertEqual(direct.transition_policy, FieldTransitionPolicy.DIRECT)
        self.assertEqual(via_zero.transition_policy, FieldTransitionPolicy.VIA_ZERO)
        self.assertNotIn(
            VectorField(0.0, 0.0),
            tuple(waypoint.command_field for waypoint in direct.waypoints),
        )
        self.assertIn(
            VectorField(0.0, 0.0),
            tuple(waypoint.command_field for waypoint in via_zero.waypoints),
        )

        previous = direct.start_command
        for waypoint in direct.waypoints:
            self.assertLessEqual(
                math.hypot(
                    waypoint.command_field.bx_t - previous.bx_t,
                    waypoint.command_field.bz_t - previous.bz_t,
                ),
                0.5 + FIELD_SETPOINT_READBACK_TOLERANCE_T,
            )
            previous = waypoint.command_field
        self.assertEqual(previous, direct.target_command)

    def test_ordered_direct_plan_preserves_reverse_and_duplicate_targets(self) -> None:
        targets = (
            VectorField(0.5, 0.0),
            VectorField(0.0, 0.5),
            VectorField(0.5, 0.0),
            VectorField(0.5, 0.0),
        )

        plans = plan_ordered_field_transitions(
            VectorField(0.0, 0.0),
            targets,
            0.5,
            FieldTransitionPolicy.DIRECT,
        )

        self.assertEqual(len(plans), len(targets))
        self.assertEqual(tuple(plan.requested_target for plan in plans), targets)
        self.assertEqual(plans[1].start_command, plans[0].target_command)
        self.assertEqual(plans[2].start_command, plans[1].target_command)
        self.assertEqual(plans[-1].waypoints, ())

    def test_three_tesla_boundary_selects_the_only_safe_float32_axis_order(self) -> None:
        plan = plan_field_transition(
            VectorField(3.0, 0.0),
            VectorField(0.0, 3.0),
            5.0,
            FieldTransitionPolicy.DIRECT,
        )

        self.assertEqual(len(plan.waypoints), 1)
        waypoint = plan.waypoints[0]
        self.assertEqual(waypoint.axis_order, AxisWriteOrder.X_THEN_Z)
        self.assertEqual(waypoint.command_field, VectorField(0.0, 3.0))
        self.assertEqual(waypoint.x_then_z_mixed_corner, VectorField(0.0, 0.0))
        self.assertEqual(waypoint.z_then_x_mixed_corner, VectorField(3.0, 3.0))
        self.assertEqual(float32_bits_hex(waypoint.command_field.bx_t), "00000000")
        self.assertEqual(float32_bits_hex(waypoint.command_field.bz_t), "00004040")
        self.assertIs(
            validate_vector_field(waypoint.x_then_z_mixed_corner),
            waypoint.x_then_z_mixed_corner,
        )
        with self.assertRaisesRegex(SafetyViolation, "project limit"):
            validate_vector_field(waypoint.z_then_x_mixed_corner)

        reverse = plan_field_transition(
            VectorField(0.0, 3.0),
            VectorField(3.0, 0.0),
            5.0,
            FieldTransitionPolicy.DIRECT,
        )
        self.assertEqual(reverse.waypoints[0].axis_order, AxisWriteOrder.Z_THEN_X)


if __name__ == "__main__":
    unittest.main()
