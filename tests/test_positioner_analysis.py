"""Archived position/frame identity and optical reference isolation, file only."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from attodry_control.combination_analysis import (
    load_combination_rows, load_combination_dark_rows, positioner_reference_context,
    select_series,
)
from attodry_control.combination_store import CombinationStore
from attodry_control.optical_background_analysis import _context as dark_context
from attodry_control.optical_power_reference import subtract_power_reference
from tests.test_photonics_analysis import observation
from tests.test_optical_power_reference import make_rows, channel


POSITION = {axis: "positioner_" + axis + "_um" for axis in "xyz"}
SIGNAL = "measured.lockin_xy_h2_x_v"


def position_status(index=0, *, mode="absolute", frame="mount-A", plane="plane-A"):
    origin = {POSITION[axis]: value for axis, value in zip("xyz", (100., 200., 300.))}
    return {"verified": True, "coordinate_frame": frame, "mode": mode, "z_mode": "focus_plane",
            "focus_plane_id": plane, "origin": origin,
            "target": {POSITION[axis]: value for axis, value in zip("xyz", (100. + index, 200., 300. + index))}}


def attach_position(rows, index=0, *, mode="absolute"):
    rows = deepcopy(rows)
    for row in rows:
        row["analysis_context"]["power_reference_plan"]["loop_order"] = ["positioner", "temperature", "optical", "lockin"]
        row["loop_order"] = ["positioner", "temperature", "optical", "lockin"]
        row.update({"axes.positioner.index": index, "axes.positioner.segment": "path",
                    "axes.positioner.direction": "ordered", "status.positioner": position_status(index, mode=mode)})
        for axis in "xy":
            key = POSITION[axis] if mode == "absolute" else "positioner_" + axis + "_offset_um"
            row["requested." + key] = row["status.positioner"]["target"][POSITION[axis]] if mode == "absolute" else float(index)
        row.update({"actual." + key: value + .001 for key, value in row["status.positioner"]["target"].items()})
    return rows


class PositionerArchiveFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.database = self.directory / "positioner.sqlite"
        for mode in ("absolute", "relative", "legacy"):
            self.make_run(mode)

    def make_run(self, mode):
        config = {} if mode == "legacy" else {"positioner": {"coordinate_frame": "mount-A", "mode": mode,
                    "focus_plane": {"id": "plane-A", "points": [{"x_um": 100., "y_um": 200., "z_um": 300.}]}}}
        conditions = []
        for index, (x, y) in enumerate(((0., 0.), (1., 0.), (0., 1.), (1., 1.))):
            requested = {} if mode == "legacy" else {
                "positioner_x_um" if mode == "absolute" else "positioner_x_offset_um": 100. + x if mode == "absolute" else x,
                "positioner_y_um" if mode == "absolute" else "positioner_y_offset_um": 200. + y if mode == "absolute" else y}
            conditions.append({"condition_id": str(index), "sequence_index": index, "repeat_index": 0,
                "loop_order": ["positioner", "optical", "lockin"] if requested else ["optical", "lockin"],
                "axes": {"positioner": {"index": index, "segment": "outward", "direction": "ordered"}} if requested else {},
                "requested": requested})
        with CombinationStore(self.database) as store:
            store.begin_run(mode, {"hardware": config}, conditions)
            for condition in conditions:
                index = condition["sequence_index"]
                status = position_status(index, mode=mode) if mode != "legacy" else None
                if status:
                    target = status["target"]
                    target[POSITION["x"]] = condition["requested"].get(POSITION["x"], 100. + condition["requested"].get("positioner_x_offset_um", 0.))
                    target[POSITION["y"]] = condition["requested"].get(POSITION["y"], 200. + condition["requested"].get("positioner_y_offset_um", 0.))
                    target[POSITION["z"]] = 300. + index
                actual = {key: value + .01 for key, value in status["target"].items()} if status else {}
                row = observation()
                stamp = f"2026-10-23T12:{index:02d}:10+00:00"
                lockin = {"module": "lockin", "clean": True, "captured_at_utc": stamp, "actual": {},
                          "status": row["status.lockin"],
                          "measurements": {key[9:]: value for key, value in row.items() if key.startswith("measured.")}}
                reads = [lockin]
                measurements = dict(lockin["measurements"])
                if status:
                    measurements["positioner_focus_error_um"] = .001
                    reads.append({"module": "positioner", "clean": True, "captured_at_utc": stamp,
                                  "actual": actual, "measurements": {"positioner_focus_error_um": .001}, "status": status})
                attempt = store.begin_attempt(mode, str(index))
                for sample in range(2):
                    store.sample(mode, str(index), attempt, sample, {"started_at_utc": stamp, "finished_at_utc": stamp,
                        "simulated": False, "clean": True, "actual": actual, "measurements": measurements, "reads": reads})
                store.finish_attempt(mode, str(index), attempt, expected_samples=2)
                context = {"requested_conditions": {"positioner": condition["requested"]} if status else {},
                    "actual_conditions": {"positioner": {**{key: value - .02 for key, value in actual.items()}, "private_noise": 99.}} if status else {},
                    "axis_visits": {"positioner": {"axis_indices": [index]}} if status else {}}
                if status:
                    context["positioner"] = status
                dark_reading = deepcopy(lockin)
                if status:
                    dark_reading["actual"] = {key: value - .02 for key, value in actual.items()}
                    dark_reading["measurements"]["positioner_focus_error_um"] = -.002
                    dark_reading["status"]["positioner"] = status
                store.event(mode, "optical_dark_background", {"baseline_id": f"{mode}-{index}",
                    "context": context, "channels": {}, "raw_readings": [dark_reading]})
            store.finish_run(mode, "completed", {"clean": True}, None)


class PositionerAnalysisTests(PositionerArchiveFixture):
    def test_archive_keeps_absolute_actual_relative_requested_and_frozen_evidence(self):
        before = self.database.read_bytes()
        rows = load_combination_rows(self.database, run_id="relative")
        self.assertEqual(len(rows), 8)
        self.assertEqual(rows[0]["actual.positioner_x_um"], 100.01)
        self.assertEqual(rows[0]["requested.positioner_x_offset_um"], 0.)
        self.assertNotIn("requested.positioner_x_um", rows[0])
        self.assertEqual(rows[0]["positioner_origin.x_um"], 100.)
        self.assertEqual(rows[0]["positioner_target.x_um"], 100.)
        self.assertEqual(rows[0]["positioner_context.focus_plane_id"], "plane-A")
        self.assertEqual(rows[0]["measured.positioner_focus_error_um"], .001)
        self.assertEqual(rows[0]["analysis_context"]["positioner"]["coordinate_frame"], "mount-A")
        self.assertEqual(self.database.read_bytes(), before)

    def test_dark_uses_its_own_xyz_and_scope_not_later_light_or_unrelated_metadata(self):
        before = self.database.read_bytes()
        dark = load_combination_dark_rows(self.database, run_id="relative")
        self.assertAlmostEqual(dark[0]["actual.positioner_x_um"], 99.99)
        self.assertEqual(dark[0]["positioner_origin.x_um"], 100.)
        self.assertEqual(dark[0]["positioner_context.coordinate_frame"], "mount-A")
        self.assertNotIn("actual.private_noise", dark[0])
        self.assertEqual(dark[0]["measured.positioner_focus_error_um"], -.002)
        self.assertEqual(positioner_reference_context(dark[0])["visit"]["index"], 0)
        self.assertEqual(self.database.read_bytes(), before)

    def test_old_archive_has_no_invented_position_or_calibration(self):
        for row in (*load_combination_rows(self.database, run_id="legacy"),
                    *load_combination_dark_rows(self.database, run_id="legacy")):
            self.assertFalse(any("positioner_" in key or key == "status.positioner" for key in row))
            self.assertIsNone(positioner_reference_context(row))

    def test_dark_missing_actual_readback_does_not_use_target_as_a_position(self):
        with CombinationStore(self.database) as store:
            events = store.connection.execute("SELECT event_id,payload_json FROM combination_events WHERE run_id='relative' AND event_type='optical_dark_background'").fetchall()
            with store.connection:
                for event in events:
                    payload = json.loads(event["payload_json"])
                    for reading in payload["raw_readings"]:
                        reading["actual"] = {}
                    store.connection.execute("UPDATE combination_events SET payload_json=? WHERE event_id=?",
                                             (json.dumps(payload), event["event_id"]))
        rows = load_combination_dark_rows(self.database, run_id="relative")
        self.assertTrue(rows)
        self.assertTrue(all(not any(key.startswith("actual.positioner_") for key in row) for row in rows))
        with self.assertRaisesRegex(ValueError, "actual XYZ"):
            positioner_reference_context(rows[0])

    def test_select_series_protects_other_position_and_frame_for_absolute_and_relative(self):
        for mode in ("absolute", "relative"):
            rows = load_combination_rows(self.database, run_id=mode)
            series = select_series(rows, x="actual.positioner_x_um", y=SIGNAL)
            other = "requested.positioner_y_um" if mode == "absolute" else "requested.positioner_y_offset_um"
            self.assertTrue(all(other in dict(item.group) for item in series))
            self.assertTrue(all("positioner_context.coordinate_frame" in dict(item.group) for item in series))
            self.assertEqual(sum(len(item.rows) for item in series), 8)


class PositionerReferenceTests(unittest.TestCase):
    def test_power_reference_preserves_position_visits_even_same_requested_coordinates(self):
        first = attach_position(make_rows(), 0)
        second = attach_position(make_rows(), 1)
        for row in second:
            row["condition_id"] = "second-" + row["condition_id"]
            row["requested.positioner_x_um"] = 100.
            row["status.positioner"]["target"] = deepcopy(first[0]["status.positioner"]["target"])
        rows = first + second
        before = deepcopy(rows)
        out = subtract_power_reference(rows)
        self.assertTrue(channel(out[2])["valid_for_analysis"])
        self.assertTrue(channel(out[6])["valid_for_analysis"])
        self.assertAlmostEqual(out[2]["measured.lockin_xx_h1_power_delta_x_v"], .009)
        self.assertAlmostEqual(out[6]["measured.lockin_xx_h1_power_delta_x_v"], .009)
        self.assertNotEqual(channel(out[2])["reference_id"], channel(out[6])["reference_id"])
        self.assertEqual(rows, before)

    def test_power_reference_does_not_cross_frame_calibration_origin_or_target(self):
        for changed in ("coordinate_frame", "focus_plane_id", "origin", "target"):
            rows = attach_position(make_rows(), mode="relative")
            for row in rows[2:]:
                if changed in ("origin", "target"):
                    row["status.positioner"][changed][POSITION["x"]] += 1.
                else:
                    row["status.positioner"][changed] = "different"
            out = subtract_power_reference(rows)
            with self.subTest(changed=changed):
                self.assertFalse(channel(out[2])["valid_for_analysis"])
                self.assertNotIn("measured.lockin_xx_h1_power_delta_x_v", out[2])

    def test_missing_position_scope_does_not_match_unknown_to_unknown(self):
        rows = attach_position(make_rows())
        for row in rows:
            row["status.positioner"].pop("coordinate_frame")
        out = subtract_power_reference(rows)
        self.assertFalse(channel(out[2])["valid_for_analysis"])
        self.assertIn("frame", channel(out[2])["reason"].lower())

    def test_independent_dark_scope_contains_frozen_position_and_visit(self):
        row = attach_position(make_rows())[0]
        original = dark_context(row, "lockin_xx", 1)
        for changed in ("coordinate_frame", "focus_plane_id", "origin", "target", "visit"):
            other = deepcopy(row)
            if changed == "visit":
                other["axes.positioner.index"] += 1
            elif changed in ("origin", "target"):
                other["status.positioner"][changed][POSITION["x"]] += 1.
            else:
                other["status.positioner"][changed] = "different"
            with self.subTest(changed=changed):
                if changed == "target":
                    with self.assertRaisesRegex(ValueError, "disagree"):
                        dark_context(other, "lockin_xx", 1)
                else:
                    self.assertNotEqual(original, dark_context(other, "lockin_xx", 1))
        noisy = deepcopy(row)
        noisy["actual.positioner_x_um"] += .001
        self.assertEqual(original, dark_context(noisy, "lockin_xx", 1))


class PositionerCoordinatorAnalysisTests(unittest.TestCase):
    def test_real_coordinator_fake_archive_to_focus_map_and_power_reference(self):
        # Compose the fixture rather than inherit its full test suite. No real
        # DLL, VISA or serial transport is used by the production coordinator.
        from tests.test_positioner_combination import PositionerCombinationTests
        from tests.test_positioner_config import positioner_document
        from attodry_control.unified_plotting import render_plot
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        self.addCleanup(lambda: plt.close("all"))
        fixture = PositionerCombinationTests(methodName="runTest")
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.document.update(positioner_document(focus=True))
        fixture.document["power_feedback"].update(target_mapping="cartesian", target_powers_w=[5e-6, 8e-6])
        fixture.document["optical_background"] = dict(enabled=True, samples=2, max_age_s=86400)
        # Exercise the existing range-profile coordinator, whose archive carries
        # complete normalized native settings required by power subtraction.
        fixture.document["combination_scan"]["lockin_range_rules"] = [dict(name="position-all", point_indices={"positioner": [0, 1]},
            lockin_xx_full_scale_v=.01, lockin_xy_full_scale_v=.01)]
        fixture.write_document()

        def with_readback_offset(config):
            motor = fixture.motor_factory(config)
            read = motor.read_state
            def noisy():
                state = read()
                for axis in state["axes"].values():
                    axis["position_um"] += .01
                return state
            motor.read_state = noisy
            return motor

        result = fixture.execute(positioner_device_factory=with_readback_offset)
        self.assertEqual(result["status"], "completed", result)
        before = fixture.database.read_bytes()
        rows = load_combination_rows(fixture.database)
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(positioner_reference_context(row) is not None for row in rows))
        self.assertTrue(all(row["actual.positioner_x_um"] != row["positioner_target.x_um"] for row in rows))
        dark = load_combination_dark_rows(fixture.database)
        self.assertEqual(len(dark), 8)
        self.assertTrue(all(positioner_reference_context(row) is not None for row in dark))
        self.assertTrue(all(row["actual.positioner_x_um"] != row["positioner_target.x_um"] for row in dark))
        relative = subtract_power_reference(rows)
        self.assertTrue(all(channel(row)["valid_for_analysis"] for row in relative),
                        [channel(row) for row in relative])
        _, plotted = render_plot(rows, {"mode": "xy_z", "x": "actual.positioner_x_um",
            "y": "actual.positioner_y_um", "z": "measured.positioner_focus_error_um",
            "filters": {"requested.optical_target_power_w": [8e-6]}})
        self.assertEqual(plotted["report"]["plotted_row_count"], 4)
        self.assertEqual(fixture.database.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
