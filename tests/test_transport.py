import unittest

from attodry_control.transport import (
    LinearGateRelation,
    rms_current_from_known_series_path,
    signed_resistance_ohm,
)


class TransportTests(unittest.TestCase):
    def test_signed_resistance_preserves_voltage_sign(self) -> None:
        self.assertAlmostEqual(
            signed_resistance_ohm(voltage_v=-2e-6, current_a_rms=1e-9),
            -2000.0,
        )

    def test_current_requires_explicit_positive_total_series_resistance(self) -> None:
        self.assertEqual(
            rms_current_from_known_series_path(
                source_voltage_v_rms=0.004,
                total_series_resistance_ohm=4e6,
            ),
            1e-9,
        )
        with self.assertRaises(ValueError):
            rms_current_from_known_series_path(
                source_voltage_v_rms=0.004,
                total_series_resistance_ohm=0.0,
            )

    def test_linear_gate_relation_is_explicit_and_limit_checked(self) -> None:
        relation = LinearGateRelation(top_per_bottom=-0.5, top_intercept_v=0.1)
        self.assertEqual(
            relation.points((-1.0, 0.0, 1.0), top_limit_v=1.0),
            ((0.6, -1.0), (0.1, 0.0), (-0.4, 1.0)),
        )
        with self.assertRaisesRegex(ValueError, "exceeds"):
            relation.points((3.0,), top_limit_v=1.0)


if __name__ == "__main__":
    unittest.main()
