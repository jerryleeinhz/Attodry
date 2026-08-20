import math
import unittest

from attodry_control.models import VectorField


class VectorFieldTests(unittest.TestCase):
    def test_vector_field_polar_round_trip(self) -> None:
        field = VectorField.from_polar(2.0, 30.0)

        self.assertAlmostEqual(field.magnitude_t, 2.0)
        self.assertAlmostEqual(field.angle_deg_from_z, 30.0)
        self.assertAlmostEqual(field.bx_t, 1.0)
        self.assertAlmostEqual(field.bz_t, math.sqrt(3.0))

    def test_vector_field_rejects_non_finite_components(self) -> None:
        with self.assertRaisesRegex(ValueError, "finite"):
            VectorField(float("nan"), 0.0)

    def test_polar_rejects_negative_magnitude(self) -> None:
        with self.assertRaisesRegex(ValueError, "non-negative"):
            VectorField.from_polar(-1.0, 0.0)


if __name__ == "__main__":
    unittest.main()
