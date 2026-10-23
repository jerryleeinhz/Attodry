"""Archived waveplate coordinates, independent dimensions and read-only plots."""
from contextlib import redirect_stdout
from copy import deepcopy
import csv
import io
from pathlib import Path
import tempfile
import unittest

from attodry_control.combination_analysis import (
    load_combination_rows, load_combination_dark_rows, rotator_labels, select_series,
)
from attodry_control.combination_store import CombinationStore
from attodry_control.unified_plotting import (
    UnifiedPlotDashboard, column_label, numeric_columns, render_plot, export_plot_bundle,
)
from tests.test_photonics_analysis import observation


R1 = "actual.rotator_1_angle_deg"
R2 = "actual.rotator_2_angle_deg"
P1 = "requested.rotator_1_angle_deg"
P2 = "requested.rotator_2_angle_deg"
SIGNAL = "measured.lockin_xy_h2_x_v"
DARK = "dark.lockin_xy_h2_x_v"


class RotatorAnalysisTests(unittest.TestCase):
    def setUp(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        self.addCleanup(lambda: plt.close("all"))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.database = self.directory / "rotator.sqlite"
        self.make_run("one", "half-wave plate", zero=None)
        self.make_run("two", "旧接线／半波片", zero=13.5)
        self.make_run("legacy", None)

    def make_run(self, run, label, zero=None, *, second=True):
        config = {} if label is None else {"rotator": {
            "rotator_1": {"label": label, "optical_zero_deg": zero}}}
        if label is not None and second:
            config["rotator"]["rotator_2"] = {"label": "analyser", "optical_zero_deg": None}
        conditions = []
        for index, (a, b) in enumerate(((0., 0.), (90., 0.), (0., 45.), (90., 45.))):
            requested = {"rotator_1_angle_deg": a, "rotator_2_angle_deg": b} if label is not None else {}
            if not second:
                requested.pop("rotator_2_angle_deg", None)
            conditions.append({"condition_id": str(index), "sequence_index": index,
                "repeat_index": 0, "loop_order": ["rotator", "optical", "lockin"],
                "axes": {"rotator": {"index": index}} if requested else {}, "requested": requested})
        with CombinationStore(self.database) as store:
            store.begin_run(run, {"hardware": config}, conditions)
            with store.connection:
                store.connection.execute("UPDATE combination_runs SET created_at_utc=? WHERE run_id=?",
                                         ("2026-10-19T12:00:00Z", run))
            for condition in conditions:
                index = condition["sequence_index"]
                requested = condition["requested"]
                actual = {key: value + .01 for key, value in requested.items()}
                row = observation()
                reading = {"module": "lockin", "clean": True,
                    "captured_at_utc": f"2026-10-19T12:{index:02d}:10Z", "actual": {},
                    "status": row["status.lockin"],
                    "measurements": {key[9:]: value for key, value in row.items() if key.startswith("measured.")}}
                attempt = store.begin_attempt(run, condition["condition_id"])
                for sample in range(2):
                    store.sample(run, condition["condition_id"], attempt, sample, {
                        "started_at_utc": reading["captured_at_utc"], "finished_at_utc": reading["captured_at_utc"],
                        "simulated": False, "clean": True, "actual": actual,
                        "measurements": reading["measurements"], "reads": [reading]})
                store.finish_attempt(run, condition["condition_id"], attempt, expected_samples=2)
                baseline = {"baseline_id": run + "-" + str(index),
                    "context": {"requested_conditions": {"rotator": requested},
                        "actual_conditions": {"rotator": {**actual, "do_not_flatten": "private evidence"},
                                              "optical": {"optical_power_w": 999}},
                        "axis_visits": {"rotator": {"axis_indices": [index]}}},
                    "channels": {}, "raw_readings": [deepcopy(reading), deepcopy(reading)]}
                store.event(run, "optical_dark_background", baseline)
            store.finish_run(run, "completed", {"clean": True}, None)

    def rows(self, run="one"):
        return load_combination_rows(self.database, run_id=run)

    def test_archived_labels_zero_metadata_and_old_runs_without_rotator(self):
        before = self.database.read_bytes()
        one, two, legacy = self.rows(), self.rows("two"), self.rows("legacy")
        self.assertEqual(len(one), 8)
        self.assertEqual(one[0]["rotator_label.rotator_1"], "half-wave plate")
        self.assertFalse(one[0]["rotator_optical_zero_calibrated.rotator_1"])
        self.assertNotIn("rotator_optical_zero_deg.rotator_1", one[0])
        self.assertEqual(two[0]["rotator_label.rotator_1"], "旧接线／半波片")
        self.assertTrue(two[0]["rotator_optical_zero_calibrated.rotator_1"])
        self.assertEqual(two[0]["rotator_optical_zero_deg.rotator_1"], 13.5)
        self.assertEqual(two[0][R1], one[0][R1])  # No optical/polarization conversion.
        self.assertFalse(any(key.startswith("rotator_") for key in legacy[0]))
        self.assertEqual(self.database.read_bytes(), before)

    def test_dark_keeps_its_matching_rotator_readback_and_archived_labels(self):
        before = self.database.read_bytes()
        rows = load_combination_dark_rows(self.database, run_id="two")
        self.assertEqual(len(rows), 8)
        self.assertEqual(rows[0][P1], 0.)
        self.assertEqual(rows[0][R1], .01)
        self.assertEqual(rows[-1][R2], 45.01)
        self.assertEqual(rows[0]["rotator_label.rotator_1"], "旧接线／半波片")
        self.assertNotIn("actual.do_not_flatten", rows[0])
        self.assertNotIn("actual.optical_power_w", rows[0])
        self.assertEqual(self.database.read_bytes(), before)

    def test_only_first_rotator_has_no_second_axis_or_invented_metadata(self):
        self.make_run("first-only", "λ/2 waveplate", second=False)
        rows = self.rows("first-only")
        darks = load_combination_dark_rows(self.database, run_id="first-only")
        for row in (*rows, *darks):
            self.assertNotIn(R2, row)
            self.assertNotIn(P2, row)
            self.assertNotIn("rotator_label.rotator_2", row)
        _, data = render_plot(rows, {"x": R1, "y": SIGNAL})
        self.assertEqual(data["report"]["plotted_row_count"], 8)  # Preserve repeat visits.
        _, data = render_plot(darks, {"x": R1, "y": DARK})
        self.assertEqual(data["report"]["plotted_row_count"], 8)

    def test_both_target_readback_angles_have_xy_options_and_known_units(self):
        rows = self.rows()
        options = dict((key, label) for label, key in numeric_columns(rows))
        for key in (R1, R2, P1, P2):
            self.assertIn(key, options)
            self.assertIn("angle (deg)", options[key])
        self.assertIn("readback Rotator 1/half-wave plate", options[R1])
        self.assertIn("target Rotator 2/analyser", options[P2])
        self.assertEqual(column_label(R1), "readback Rotator 1 angle (deg)")

    def test_each_rotator_is_an_independent_condition_dimension(self):
        rows = self.rows()
        for axis in (R1, P1):
            with self.subTest(axis=axis), self.assertRaisesRegex(ValueError, "Rotator 2"):
                render_plot(rows, {"x": axis, "y": SIGNAL})
        figure, data = render_plot(rows, {"x": R1, "y": SIGNAL, "group_by": P2})
        self.assertEqual(data["report"]["plotted_row_count"], 8)
        self.assertEqual(len(figure.axes[0].get_legend_handles_labels()[1]), 4)  # Two sample traces per angle.
        _, filtered = render_plot(rows, {"x": R1, "y": SIGNAL, "filters": {P2: [0.]}})
        self.assertEqual(filtered["report"]["plotted_row_count"], 4)
        _, both = render_plot(rows, {"mode": "xy_z", "x": R1, "y": R2, "z": SIGNAL})
        self.assertEqual(both["report"]["plotted_row_count"], 8)

    def test_actual_axis_grouping_covers_only_its_own_target_coordinate(self):
        rows = self.rows()
        with self.assertRaisesRegex(ValueError, "Rotator 1"):
            render_plot(rows, {"x": "elapsed_s", "y": SIGNAL, "group_by": R2})
        _, data = render_plot(rows, {"x": R1, "y": SIGNAL, "group_by": R2})
        self.assertEqual(data["report"]["plotted_row_count"], 8)
        series = select_series(rows, x=R1, y=SIGNAL)
        self.assertEqual(sum(len(item.rows) for item in series), 8)
        self.assertTrue(all(P2 in dict(item.group) for item in series))

    def test_dark_time_and_angle_plots_do_not_merge_different_waveplate_positions(self):
        rows = load_combination_dark_rows(self.database, run_id="one")
        with self.assertRaisesRegex(ValueError, "Rotator"):
            render_plot(rows, {"x": "elapsed_s", "y": DARK})
        _, data = render_plot(rows, {"x": R1, "y": DARK, "group_by": P2})
        self.assertEqual(data["report"]["plotted_row_count"], 8)
        _, data = render_plot(rows, {"x": "elapsed_s", "y": DARK,
            "group_by": P1, "filters": {P2: [0.]}})
        self.assertEqual(data["report"]["plotted_row_count"], 4)

    def test_mixed_run_labels_and_unlabelled_are_not_misattributed(self):
        rows = [self.rows()[0], self.rows("two")[0], {**self.rows("legacy")[0], R1: 0., P1: 0., P2: 0.}]
        before = deepcopy(rows)
        caption = column_label(R1, rows)
        for name in ("half-wave plate", "旧接线／半波片", "unlabelled"):
            self.assertIn(name, caption)
        figure, data = render_plot(rows, {"x": R1, "y": SIGNAL})
        legends = figure.axes[0].get_legend_handles_labels()[1]
        self.assertTrue(any("one" in name and "half-wave plate" in name for name in legends), legends)
        self.assertTrue(any("two" in name and "旧接线／半波片" in name for name in legends), legends)
        self.assertTrue(any("legacy" in name and "half-wave plate" not in name for name in legends), legends)
        self.assertEqual(data["report"]["plotted_row_count"], 3)
        self.assertEqual(rows, before)

    def test_loaded_row_label_fallback_and_unknown_calibration_not_inferred(self):
        row = {"rotator_label.rotator_1": "λ/2", R1: 15.}
        self.assertEqual(rotator_labels(row), {"rotator_1": "λ/2"})
        self.assertIn("λ/2", column_label(R1, [row]))
        self.assertNotIn("rotator_optical_zero_deg.rotator_1", row)

    def test_widget_setup_reload_export_preserves_both_axes_and_archive_metadata(self):
        before = self.database.read_bytes()
        with redirect_stdout(io.StringIO()):
            dashboard = UnifiedPlotDashboard(self.directory)
            dashboard.add_plot("xy_z", {"source_paths": [str(self.database)],
                "run_ids": {str(self.database): ["one"]}, "x": R1, "y": R2, "z": SIGNAL})
            card = dashboard.cards[0]
            for axis in ("x", "y"):
                self.assertTrue({R1, R2, P1, P2} <= {value for label, value in card[axis].options})
            dashboard.configuration_path.value = str(self.directory / "rotator-setup.json")
            dashboard._save_setup()
            dashboard._load_setup()
            card = dashboard.cards[0]
            self.assertEqual((card["x"].value, card["y"].value), (R1, R2))
            dashboard._render_card(card)
            self.assertIsNotNone(card["rendered"])
            destination = export_plot_bundle(self.directory / "export", [card["rendered"]])
        with (destination / "selected_samples.csv").open(encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(row["rotator_label.rotator_1"] == "half-wave plate" for row in rows))
        self.assertTrue(all(row["rotator_optical_zero_calibrated.rotator_1"] == "False" for row in rows))
        self.assertTrue(all(key in rows[0] for key in (R1, R2, P1, P2)))
        self.assertEqual(self.database.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
