import unittest

from attodry_control.models import VectorField
from attodry_control.safety import SafetyViolation
from attodry_control.scans import (
    field_grid,
    frequency_scan,
    gate_grid,
    paired_gate_scan,
    temperature_scan_points,
    temperature_field_grid,
    voltage_scan,
)


class ScanGenerationTests(unittest.TestCase):
    def test_voltage_scan_includes_both_endpoints_without_float_drift(self) -> None:
        self.assertEqual(voltage_scan(-0.2, 0.2, 0.1), (-0.2, -0.1, 0.0, 0.1, 0.2))

    def test_frequency_scan_rejects_nonpositive_values(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive"):
            frequency_scan(0.0, 1.0, 0.1)

    def test_temperature_scan_is_inclusive_without_float_drift(self) -> None:
        self.assertEqual(
            temperature_scan_points(1.7, 2.0, 0.1),
            (1.7, 1.8, 1.9, 2.0),
        )

    def test_temperature_scan_rejects_descending_path(self) -> None:
        with self.assertRaisesRegex(ValueError, "ascending"):
            temperature_scan_points(2.0, 1.7, -0.1)

    def test_descending_scan_requires_negative_step(self) -> None:
        with self.assertRaisesRegex(ValueError, "direction"):
            voltage_scan(1.0, 0.0, 0.1)

    def test_field_grid_validates_every_vector_before_returning(self) -> None:
        fields = field_grid(bx_values=(0.0, 1.8), bz_values=(0.0, 2.4))

        self.assertEqual(fields[-1], VectorField(1.8, 2.4))
        with self.assertRaises(SafetyViolation):
            field_grid(bx_values=(2.0,), bz_values=(2.5,))

    def test_temperature_field_grid_has_deterministic_temperature_major_order(self) -> None:
        points = temperature_field_grid(
            temperatures_k=(2.0, 3.0),
            fields=(VectorField(0.0, 0.0), VectorField(1.0, 0.0)),
        )

        self.assertEqual(
            [(point.temperature_k, point.field.bx_t) for point in points],
            [(2.0, 0.0), (2.0, 1.0), (3.0, 0.0), (3.0, 1.0)],
        )

    def test_gate_grid_supports_serpentine_bottom_gate_order(self) -> None:
        points = gate_grid(
            top_values_v=(0.0, 1.0),
            bottom_values_v=(-1.0, 0.0, 1.0),
            serpentine=True,
        )

        self.assertEqual(
            [(point.top_v, point.bottom_v) for point in points],
            [
                (0.0, -1.0),
                (0.0, 0.0),
                (0.0, 1.0),
                (1.0, 1.0),
                (1.0, 0.0),
                (1.0, -1.0),
            ],
        )

    def test_paired_gate_scan_requires_equal_lengths(self) -> None:
        with self.assertRaisesRegex(ValueError, "same number"):
            paired_gate_scan(top_values_v=(0.0, 1.0), bottom_values_v=(0.0,))


if __name__ == "__main__":
    unittest.main()
