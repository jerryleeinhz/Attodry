import unittest

from attodry_control.rotator_config import RotatorError, load_rotator_config


def document(two=False):
    result = {"rotator_1": {"enabled": True, "serial_number": "27000001",
                             "label": "λ/2波片／入射偏振"},
              "rotator_2": {"enabled": False, "label": ""},
              "rotator_scan": {"rotator_1_angles_deg": [0, 90, 360]}}
    if two:
        result["rotator_2"] = {"enabled": True, "serial_number": "27000002"}
        result["rotator_scan"]["rotator_2_angles_deg"] = [5, 15]
    return result


class RotatorConfigTests(unittest.TestCase):
    def test_first_enabled_second_minimal_off_and_uncalibrated_optical_zero(self):
        config = load_rotator_config(document())
        self.assertEqual(config.active_keys, ("rotator_1_angle_deg",))
        self.assertEqual(len(config.devices), 1)
        self.assertEqual(config.devices[0].label, "λ/2波片／入射偏振")
        self.assertIsNone(config.devices[0].optical_zero_deg)
        self.assertEqual(config.points[-1], {"rotator_1_angle_deg": 360.0})

    def test_absent_or_both_off_avoids_hardware_configuration(self):
        self.assertIsNone(load_rotator_config({}))
        self.assertIsNone(load_rotator_config({"rotator_1": {"enabled": False},
                                                "rotator_2": {"enabled": False}}))

    def test_off_fields_retained_without_parsing_but_typos_rejected(self):
        raw = document()
        raw["rotator_2"].update(serial_number="not available", stage_model=None)
        self.assertEqual(len(load_rotator_config(raw).devices), 1)
        raw["rotator_2"]["lable"] = "typo"
        with self.assertRaises(RotatorError):
            load_rotator_config(raw)

    def test_cartesian_first_outer_second_inner_and_ordered_repeats(self):
        raw = document(True)
        raw["rotator_scan"]["rotator_1_angles_deg"] = [90, 0, 90]
        config = load_rotator_config(raw)
        self.assertEqual(len(config.points), 6)
        self.assertEqual([tuple(p.values()) for p in config.points],
                         [(90, 5), (90, 15), (0, 5), (0, 15), (90, 5), (90, 15)])

    def test_second_only_is_supported(self):
        raw = document(True)
        raw["rotator_1"] = {"enabled": False}
        del raw["rotator_scan"]["rotator_1_angles_deg"]
        config = load_rotator_config(raw)
        self.assertEqual(config.active_keys, ("rotator_2_angle_deg",))

    def test_range_uses_shared_inclusive_grid(self):
        raw = document()
        raw["rotator_scan"] = {"rotator_1_ranges": [{"min": 0, "max": 360,
                                                         "scale": "linear", "step": 90}]}
        self.assertEqual([p["rotator_1_angle_deg"] for p in load_rotator_config(raw).points],
                         [0, 90, 180, 270, 360])

    def test_active_serial_required_and_no_model_or_identifier_discovery(self):
        for serial in (None, 27000001, "2700000", "83000001", "CHANGE_ME", "27abcdef"):
            with self.subTest(serial=serial):
                raw = document()
                raw["rotator_1"]["serial_number"] = serial
                with self.assertRaises(RotatorError):
                    load_rotator_config(raw)
        raw = document()
        del raw["rotator_1"]["serial_number"]
        with self.assertRaises(RotatorError):
            load_rotator_config(raw)

    def test_device_boundaries_are_strict_finite_and_bool_is_not_number(self):
        for changes in ({"enabled": 1}, {"controller": "other"}, {"stage_model": "PRM1/M"},
                        {"min_angle_deg": -1}, {"max_angle_deg": 361},
                        {"min_angle_deg": 360}, {"position_tolerance_deg": 0},
                        {"position_tolerance_deg": 2}, {"settle_s": -1},
                        {"home_policy": "never"}, {"move_timeout_s": 0},
                        {"optical_zero_deg": float("nan")}, {"label": "a\nb"},
                        {"settle_s": True}, {"poll_interval_s": float("inf")}):
            with self.subTest(changes=changes):
                raw = document()
                raw["rotator_1"].update(changes)
                with self.assertRaises(RotatorError):
                    load_rotator_config(raw)

    def test_grid_rejects_wrong_keys_disabled_and_missing_axes(self):
        for scan in ({}, {"rotator_1_angles_deg": []}, {"rotator_1_angles_deg": [361]},
                     {"rotator_1_angles_deg": [True]}, {"rotator_1_angles_deg": [float("nan")]},
                     {"rotator_1_angles_deg": [0], "rotator_2_angles_deg": [0]},
                     {"rotator_1_angles_deg": [0], "rotator_1_ranges": []},
                     {"rotator_1_angle_deg": [0]}):
            with self.subTest(scan=scan):
                raw = document()
                raw["rotator_scan"] = scan
                with self.assertRaises(RotatorError):
                    load_rotator_config(raw)

    def test_duplicate_serial_and_cartesian_budget(self):
        raw = document(True)
        raw["rotator_2"]["serial_number"] = "27000001"
        with self.assertRaises(RotatorError):
            load_rotator_config(raw)
        raw = document(True)
        raw["rotator_scan"] = {"rotator_1_angles_deg": [0] * 101,
                                 "rotator_2_angles_deg": [0] * 100}
        with self.assertRaises(RotatorError):
            load_rotator_config(raw)

    def test_snapshot_is_detached_and_has_archived_zero_and_label(self):
        config = load_rotator_config(document())
        snapshot = config.snapshot()
        snapshot["points"][0]["rotator_1_angle_deg"] = 99
        self.assertEqual(config.points[0]["rotator_1_angle_deg"], 0)
        self.assertIsNone(snapshot["devices"]["rotator_1"]["optical_zero_deg"])


if __name__ == "__main__":
    unittest.main()
