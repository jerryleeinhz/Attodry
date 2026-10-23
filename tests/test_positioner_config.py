"""Positioner coordinates and sequential path safety without instrument imports."""
import copy
import math
from pathlib import Path
import tomllib
import unittest

from attodry_control.positioner_config import ABSOLUTE_KEYS, PositionerError, load_positioner_config
from tests.test_positioner_focus_plane import focus_table


def positioner_document(*, focus=False, mode="absolute"):
    table = {"enabled": True, "controller_serial_number": "fixture-serial", "expected_firmware_raw": 123,
             "dll_path": "fixture-only/anc350v4.dll", "dll_sha256": "a" * 64,
             "coordinate_frame": "fixture-frame", "coordinate_frame_confirmed": True,
             "z_mode": "focus_plane" if focus else "manual", "move_order": ["x", "y", "z"],
             "settle_s": 0.2, "poll_interval_s": 0.05, "move_timeout_s": 3,
             "finish": "hold", "external_light_off_confirmed": True,
             "axes": {role: {"index": index, "min_position_um": -100, "max_position_um": 200,
                     "max_move_um": 150, "position_tolerance_um": 0.1,
                     "expected_actuator_name": "fixture-actuator", "expected_lut_name": "fixture-lut-" + role,
                     "amplitude_v": 20, "frequency_hz": 100} for index, role in enumerate(("x", "y", "z"))},
             "scan": {"mode": mode, "points": [{"x_um": 0, "y_um": 0}, {"x_um": 10, "y_um": 10}]}}
    if focus:
        table["focus_plane"] = focus_table()
        table["focus_tolerance_um"] = 0.15
    else:
        for point in table["scan"]["points"]:
            point["z_um"] = 0
    return {"positioner": table}


def xyz(x, y, z):
    return dict(zip(ABSOLUTE_KEYS, (x, y, z)))


class PositionerConfigTests(unittest.TestCase):
    def test_absent_or_disabled_does_not_guess_hardware(self):
        self.assertIsNone(load_positioner_config({}))
        self.assertIsNone(load_positioner_config({"positioner": {"enabled": False}}))
        with self.assertRaisesRegex(PositionerError, "unknown"):
            load_positioner_config({"positioner": {"enabled": False, "typo": 1}})

    def test_absolute_points_preserve_repeat_visits_and_freeze(self):
        document = positioner_document()
        document["positioner"]["scan"]["points"] *= 2
        config = load_positioner_config(document)
        self.assertEqual(len(config.points), 4)
        self.assertEqual(config.resolve_all_points()[1], xyz(10, 10, 0))
        document["positioner"]["scan"]["points"][1]["x_um"] = 80
        self.assertEqual(config.points[1]["positioner_x_um"], 10)
        with self.assertRaises(TypeError):
            config.points[0]["positioner_x_um"] = 50

    def test_relative_origin_frozen_once_and_never_cumulative(self):
        config = load_positioner_config(positioner_document(mode="relative"))
        origin = xyz(50, 70, 15)
        targets = config.resolve_all_points(origin)
        self.assertEqual(targets, (xyz(50, 70, 15), xyz(60, 80, 15)))
        self.assertEqual(config.resolve_all_points(origin), targets)
        self.assertEqual(origin, xyz(50, 70, 15))
        with self.assertRaisesRegex(PositionerError, "origin"):
            config.resolve_all_points()

    def test_focus_absolute_z_is_computed_and_independent_z_forbidden(self):
        config = load_positioner_config(positioner_document(focus=True))
        self.assertEqual(config.resolve_all_points()[0], xyz(0, 0, 10))
        self.assertAlmostEqual(config.resolve_all_points()[1]["positioner_z_um"], 10.1)
        document = positioner_document(focus=True)
        document["positioner"]["scan"]["points"][0]["z_um"] = 11
        with self.assertRaisesRegex(PositionerError, "unknown"):
            load_positioner_config(document)

    def test_relative_focus_uses_absolute_xy_and_not_origin_z(self):
        config = load_positioner_config(positioner_document(focus=True, mode="relative"))
        self.assertEqual(len(config.active_keys), 2)
        targets = config.resolve_all_points(xyz(20, 30, 100))
        self.assertEqual(targets[0], xyz(20, 30, 10.1))
        self.assertAlmostEqual(targets[1]["positioner_z_um"], 10.2)

    def test_grid_serpentine_preserves_xy_and_order(self):
        document = positioner_document()
        document["positioner"]["scan"] = {"mode": "absolute", "x_values_um": [0, 10],
              "y_values_um": [0, 5, 10], "z_um": 3, "order": "xy", "serpentine": True}
        config = load_positioner_config(document)
        self.assertEqual(config.resolve_all_points(), (xyz(0, 0, 3), xyz(0, 5, 3), xyz(0, 10, 3),
                                                       xyz(10, 10, 3), xyz(10, 5, 3), xyz(10, 0, 3)))
        document["positioner"]["scan"]["order"] = "yx"
        config = load_positioner_config(document)
        self.assertEqual(config.resolve_all_points()[:4], (xyz(0, 0, 3), xyz(10, 0, 3), xyz(10, 5, 3), xyz(0, 5, 3)))

    def test_inclusive_range_grid(self):
        document = positioner_document()
        document["positioner"]["scan"] = {"mode": "absolute", "x_ranges_um": [{"min": 0, "max": 1, "scale": "linear", "points": 3}],
                                          "y_values_um": [0], "z_um": 0}
        config = load_positioner_config(document)
        self.assertEqual([p["positioner_x_um"] for p in config.points], [0, 0.5, 1])

    def test_waypoints_follow_explicit_axis_order(self):
        document = positioner_document()
        document["positioner"]["move_order"] = ["z", "y", "x"]
        config = load_positioner_config(document)
        self.assertEqual(config.validate_path(xyz(0, 0, 0), [xyz(10, 20, 30)]),
                         (xyz(0, 0, 30), xyz(0, 20, 30), xyz(10, 20, 30)))
        self.assertEqual(config.validate_path(xyz(0, 0, 0), [xyz(0, 0, 0)]), ())

    def test_max_move_and_start_limits_checked_without_automatic_splitting(self):
        config = load_positioner_config(positioner_document())
        with self.assertRaisesRegex(PositionerError, "max_move_um"):
            config.validate_path(xyz(-100, 0, 0), [xyz(100, 0, 0)])
        with self.assertRaisesRegex(PositionerError, "limits"):
            config.validate_path(xyz(-101, 0, 0), [])
        document = positioner_document()
        document["positioner"]["axes"]["x"]["max_move_um"] = 5
        with self.assertRaisesRegex(PositionerError, "max_move_um"):
            load_positioner_config(document)

    def test_focus_sequential_path_cannot_leave_hull_even_if_both_targets_inside(self):
        document = positioner_document(focus=True)
        document["positioner"]["focus_plane"]["points"].pop()
        document["positioner"]["scan"]["points"] = [{"x_um": 0, "y_um": 100}, {"x_um": 100, "y_um": 0}]
        with self.assertRaisesRegex(PositionerError, "convex hull"):
            load_positioner_config(document)
        document["positioner"]["move_order"] = ["y", "x", "z"]
        load_positioner_config(document)

    def test_relative_focus_out_of_calibration_rejected_after_origin_resolution(self):
        config = load_positioner_config(positioner_document(focus=True, mode="relative"))
        with self.assertRaisesRegex(PositionerError, "convex hull"):
            config.resolve_all_points(xyz(95, 95, 10))

    def test_all_axis_safety_fields_explicit_and_confirmed(self):
        document = positioner_document()
        del document["positioner"]["axes"]["x"]["max_move_um"]
        with self.assertRaisesRegex(PositionerError, "missing"):
            load_positioner_config(document)
        for key, value in (("coordinate_frame_confirmed", False), ("coordinate_frame_confirmed", 1),
                           ("expected_firmware_raw", True), ("dll_sha256", ""),
                           ("move_order", ["x", "y", "y"]), ("move_order", [[], "y", "z"])):
            document = positioner_document()
            document["positioner"][key] = value
            with self.subTest(key=key), self.assertRaises(PositionerError):
                load_positioner_config(document)

    def test_no_implicit_xyz_axis_remap(self):
        document = positioner_document()
        document["positioner"]["axes"]["z"]["index"] = 0
        with self.assertRaisesRegex(PositionerError, "XYZ map"):
            load_positioner_config(document)

    def test_bool_nonfinite_timeout_or_target_fail_closed(self):
        for key, value in (("settle_s", True), ("poll_interval_s", math.nan), ("move_timeout_s", 0),
                           ("poll_interval_s", 0.3), ("settle_s", 5)):
            document = positioner_document()
            document["positioner"][key] = value
            with self.subTest(key=key), self.assertRaises(PositionerError):
                load_positioner_config(document)
        document = positioner_document()
        document["positioner"]["scan"]["points"][0]["x_um"] = True
        with self.assertRaises(PositionerError):
            load_positioner_config(document)

    def test_manual_plane_and_scan_schema_conflicts_rejected(self):
        document = positioner_document()
        document["positioner"]["focus_plane"] = focus_table()
        with self.assertRaisesRegex(PositionerError, "conflicts"):
            load_positioner_config(document)
        document = positioner_document()
        document["positioner"]["scan"]["x_values_um"] = [1]
        with self.assertRaisesRegex(PositionerError, "conflict"):
            load_positioner_config(document)

    def test_snapshot_is_plain_data_and_keeps_focus_provenance(self):
        config = load_positioner_config(positioner_document(focus=True))
        snapshot = config.snapshot()
        self.assertEqual(snapshot["axes"]["z"]["index"], 2)
        self.assertEqual(snapshot["focus_plane"]["calibration_sha256"], config.focus_plane.calibration_sha256)
        snapshot["points"][0]["positioner_x_um"] = 99
        self.assertEqual(config.points[0]["positioner_x_um"], 0)

    def test_grid_budget_checked_before_materializing_cartesian_product(self):
        document = positioner_document()
        document["positioner"]["scan"] = {"mode": "absolute", "x_values_um": list(range(101)),
                                         "y_values_um": list(range(101)), "z_um": 0}
        with self.assertRaisesRegex(PositionerError, "grid exceeds"):
            load_positioner_config(document)

    def test_committed_example_disabled_and_only_synthetic_offline_geometry(self):
        path = Path(__file__).resolve().parents[1] / "config" / "positioner.example.toml"
        document = tomllib.loads(path.read_text(encoding="utf-8"))
        self.assertIsNone(load_positioner_config(document))
        document["positioner"]["enabled"] = True
        config = load_positioner_config(document)
        self.assertEqual(len(config.points), 9)
        self.assertIn("CHANGE_ME", config.controller_serial_number)
        self.assertEqual(config.dll_sha256, "0" * 64)
        for key, value in xyz(10, 10, 100.1).items():
            self.assertAlmostEqual(config.resolve_all_points()[0][key], value)


if __name__ == "__main__":
    unittest.main()
