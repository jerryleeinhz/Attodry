"""Planar focus mathematics, conservative coverage and provenance regressions."""
import copy
from dataclasses import replace
import math
import unittest

from attodry_control.positioner_focus_plane import PositionerError, fit_focus_plane


def focus_table():
    return {"model": "plane", "coordinate_frame": "fixture-frame", "max_residual_um": 0.1,
            "rms_residual_um": 0.1,
            "points": [{"x_um": x, "y_um": y, "z_um": 10 + 0.02 * x - 0.01 * y}
                       for x, y in ((0, 0), (100, 0), (0, 100), (100, 100))]}


class FocusPlaneTests(unittest.TestCase):
    def test_recovers_plane_and_offset_without_hardware(self):
        table = focus_table()
        table["z_offset_um"] = 0.25
        plane = fit_focus_plane(table)
        self.assertAlmostEqual(plane.slope_x, 0.02)
        self.assertAlmostEqual(plane.slope_y, -0.01)
        self.assertAlmostEqual(plane.evaluate(25, 80), 9.95)
        self.assertAlmostEqual(plane.focus_error(25, 80, 10), 0.05)
        self.assertEqual(plane.snapshot()["rank"], 3)
        self.assertTrue(plane.snapshot()["redundant_points"])

    def test_centered_fit_is_independent_of_large_coordinate_offsets(self):
        table = focus_table()
        for point in table["points"]:
            point["x_um"] += 1e12
            point["y_um"] -= 2e12
        plane = fit_focus_plane(table)
        self.assertAlmostEqual(plane.evaluate(1e12 + 50, -2e12 + 50), 10.5)
        self.assertAlmostEqual(plane.condition_number, 1.0)

    def test_three_points_have_no_redundant_accuracy_evidence(self):
        table = focus_table()
        table["points"].pop()
        plane = fit_focus_plane(table)
        self.assertFalse(plane.snapshot()["redundant_points"])
        self.assertIn("do not certify", plane.snapshot()["accuracy_note"])

    def test_uses_all_points_and_rejects_large_residuals(self):
        table = focus_table()
        table["points"][-1]["z_um"] += 1
        with self.assertRaisesRegex(PositionerError, "residual"):
            fit_focus_plane(table)
        table["max_residual_um"] = table["rms_residual_um"] = 1
        plane = fit_focus_plane(table)
        self.assertEqual(len(plane.residuals_um), 4)
        self.assertAlmostEqual(plane.snapshot()["rms_actual_residual_um"], 0.25)

    def test_convex_hull_is_not_a_bounding_rectangle(self):
        table = focus_table()
        table["points"].pop()
        plane = fit_focus_plane(table)
        self.assertAlmostEqual(plane.evaluate(50, 50), 10.5)
        for xy in ((80, 80), (-1e-5, 0), (101, 0)):
            with self.subTest(xy=xy), self.assertRaisesRegex(PositionerError, "convex hull"):
                plane.evaluate(*xy)
        for point in table["points"]:
            plane.evaluate(point["x_um"], point["y_um"])

    def test_rejects_collinear_and_nearly_collinear_points(self):
        for epsilon in (0, 1e-8):
            table = focus_table()
            table["points"] = [{"x_um": 0, "y_um": 0, "z_um": 1},
                               {"x_um": 1, "y_um": 1, "z_um": 1},
                               {"x_um": 2, "y_um": 2 + epsilon, "z_um": 1}]
            with self.subTest(epsilon=epsilon), self.assertRaisesRegex(PositionerError, "collinear|condition"):
                fit_focus_plane(table)

    def test_duplicate_xy_is_rejected_even_with_different_z(self):
        table = focus_table()
        table["points"].append({"x_um": 0, "y_um": 0, "z_um": 999})
        with self.assertRaisesRegex(PositionerError, "Duplicate"):
            fit_focus_plane(table)

    def test_invalid_points_and_thresholds_fail_closed(self):
        for value in (True, math.inf, math.nan, "1", 10 ** 1000):
            table = focus_table()
            table["points"][0]["z_um"] = value
            with self.subTest(value_type=type(value).__name__), self.assertRaises(PositionerError):
                fit_focus_plane(table)
        for key in ("max_residual_um", "rms_residual_um"):
            for value in (0, -1, True, math.inf):
                table = focus_table()
                table[key] = value
                with self.subTest(key=key, value=value), self.assertRaises(PositionerError):
                    fit_focus_plane(table)

    def test_coordinate_frame_and_schema_are_strict(self):
        table = focus_table()
        with self.assertRaisesRegex(PositionerError, "coordinate_frame"):
            fit_focus_plane(table, coordinate_frame="different")
        for key, value in (("model", "quadratic"), ("extrapolation_policy", "allow"), ("misspelling", 2)):
            table = focus_table()
            table[key] = value
            with self.subTest(key=key), self.assertRaises(PositionerError):
                fit_focus_plane(table)

    def test_temperature_is_explicit_and_does_not_change_fit(self):
        table = focus_table()
        table.update(calibration_temperature_k=20, temperature_tolerance_k=0.5)
        plane = fit_focus_plane(table)
        plane.validate_temperature(20.5)
        with self.assertRaisesRegex(PositionerError, "temperature"):
            plane.validate_temperature(20.51)
        with self.assertRaises(PositionerError):
            plane.validate_temperature(None)
        del table["temperature_tolerance_k"]
        with self.assertRaisesRegex(PositionerError, "together"):
            fit_focus_plane(table)

    def test_calibration_frozen_and_hash_covers_inputs_not_just_coefficients(self):
        table = focus_table()
        plane = fit_focus_plane(table)
        before = copy.deepcopy(table)
        table["points"][0]["z_um"] = -100
        self.assertAlmostEqual(plane.evaluate(0, 0), 10)
        self.assertEqual(plane.calibration_sha256, fit_focus_plane(before).calibration_sha256)
        before["calibration_conditions"] = "changed mount"
        self.assertNotEqual(plane.calibration_sha256, fit_focus_plane(before).calibration_sha256)
        exported = plane.snapshot()
        exported["points"][0]["z_um"] = 500
        self.assertEqual(plane.points[0].z_um, 10)

    def test_fit_diagnostics_record_each_residual_and_finite_failure(self):
        table = focus_table()
        table["points"][0]["z_um"] += 0.04
        plane = fit_focus_plane(table)
        self.assertEqual(len(plane.snapshot()["residuals_um"]), 4)
        self.assertAlmostEqual(plane.snapshot()["max_actual_residual_um"], 0.01)
        table["points"][0]["x_um"] = -1e308
        table["points"][1]["x_um"] = 1e308
        with self.assertRaises(PositionerError):
            fit_focus_plane(table)

    def test_frozen_fit_cannot_be_replaced_by_unverified_coefficients(self):
        plane = fit_focus_plane(focus_table())
        plane.validate()
        for altered in (replace(plane, slope_x=99), replace(plane, z_offset_um=3),
                        replace(plane, calibration_sha256="b" * 64)):
            with self.assertRaisesRegex(PositionerError, "frozen calibration"):
                altered.validate()


if __name__ == "__main__":
    unittest.main()
