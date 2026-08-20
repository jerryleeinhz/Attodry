import unittest

from attodry_control.models import VectorField
from attodry_control.safety import MagnetLimits, SafetyViolation, validate_vector_field


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


if __name__ == "__main__":
    unittest.main()
