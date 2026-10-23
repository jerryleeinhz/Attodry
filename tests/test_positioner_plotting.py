"""Position readback/offset labels, independent XY maps and audit exports."""
from contextlib import redirect_stdout
from copy import deepcopy
import csv
import io
import json

from attodry_control.combination_analysis import load_combination_rows
from attodry_control.unified_plotting import (
    UnifiedPlotDashboard, column_label, numeric_columns, render_plot, export_plot_bundle,
)
from tests.test_positioner_analysis import PositionerArchiveFixture, SIGNAL


X, Y, Z = ("actual.positioner_" + axis + "_um" for axis in "xyz")
ERROR = "measured.positioner_focus_error_um"


class PositionerPlottingTests(PositionerArchiveFixture):
    def setUp(self):
        super().setUp()
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        self.addCleanup(lambda: plt.close("all"))

    def rows(self, mode="relative"):
        return load_combination_rows(self.database, run_id=mode)

    def test_absolute_and_relative_labels_are_distinct_and_have_micrometre_units(self):
        rows = self.rows()
        options = {key: label for label, key in numeric_columns(rows)}
        for key in (X, Y, Z, "requested.positioner_x_offset_um", "requested.positioner_y_offset_um", ERROR):
            self.assertIn(key, options)
            self.assertIn("μm", options[key])
        self.assertIn("readback Positioner X absolute position", column_label(X))
        self.assertIn("offset from frozen origin", column_label("requested.positioner_x_offset_um"))
        self.assertIn("resolved absolute target", column_label("positioner_target.z_um"))
        self.assertIn("frozen origin", column_label("positioner_origin.x_um"))

    def test_each_position_axis_is_an_independent_condition_dimension(self):
        for mode in ("absolute", "relative"):
            rows = self.rows(mode)
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "Positioner Y"):
                render_plot(rows, {"x": X, "y": SIGNAL})
            _, grouped = render_plot(rows, {"x": X, "y": SIGNAL,
                "group_by": "requested.positioner_y_um" if mode == "absolute" else "requested.positioner_y_offset_um"})
            self.assertEqual(grouped["report"]["plotted_row_count"], 8)
            with self.assertRaisesRegex(ValueError, "Positioner X"):
                render_plot(rows, {"x": "elapsed_s", "y": SIGNAL, "group_by": Y})

    def test_xy_signal_actual_z_and_focus_error_maps_use_existing_readonly_renderer(self):
        before = self.database.read_bytes()
        rows = self.rows()
        for value in (SIGNAL, Z, ERROR):
            with self.subTest(value=value):
                _, data = render_plot(rows, {"mode": "xy_z", "x": X, "y": Y, "z": value})
                self.assertEqual(data["report"]["plotted_row_count"], 8)
        self.assertEqual(self.database.read_bytes(), before)

    def test_archived_focus_target_z_is_dependent_but_manual_or_inconsistent_z_is_not(self):
        rows = deepcopy(self.rows("absolute"))
        for row in rows:
            row["requested.positioner_z_um"] = row["status.positioner"]["target"]["positioner_z_um"]
        _, data = render_plot(rows, {"mode": "xy_z", "x": X, "y": Y, "z": SIGNAL})
        self.assertEqual(data["report"]["plotted_row_count"], 8)
        for change in ("manual", "mismatch"):
            altered = deepcopy(rows)
            for row in altered:
                if change == "manual":
                    row["status.positioner"]["z_mode"] = "manual"
                else:
                    row["status.positioner"]["target"]["positioner_z_um"] += 1.
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "Positioner Z"):
                render_plot(altered, {"mode": "xy_z", "x": X, "y": Y, "z": SIGNAL})

    def test_coordinate_frame_calibration_and_frozen_origin_cannot_silently_mix(self):
        original = self.rows()
        for key, value in (("positioner_context.coordinate_frame", "other-mount"),
                           ("positioner_context.focus_plane_id", "other-calibration"),
                           ("positioner_origin.x_um", 101.)):
            rows = deepcopy(original)
            rows[-1][key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "Positioner"):
                render_plot(rows, {"mode": "xy_z", "x": X, "y": Y, "z": SIGNAL})

    def test_widget_setup_and_exports_preserve_origin_frame_calibration_and_raw_status(self):
        before = self.database.read_bytes()
        with redirect_stdout(io.StringIO()):
            dashboard = UnifiedPlotDashboard(self.directory)
            dashboard.add_plot("xy_z", {"source_paths": [str(self.database)],
                "run_ids": {str(self.database): ["relative"]}, "x": X, "y": Y, "z": ERROR})
            card = dashboard.cards[0]
            self.assertTrue({X, Y, Z, ERROR} <= {value for _, value in card["z"].options})
            dashboard.configuration_path.value = str(self.directory / "positioner-setup.json")
            dashboard._save_setup()
            dashboard._load_setup()
            card = dashboard.cards[0]
            self.assertEqual((card["x"].value, card["y"].value, card["z"].value), (X, Y, ERROR))
            dashboard._render_card(card)
            self.assertIsNotNone(card["rendered"])
            destination = export_plot_bundle(self.directory / "export", [card["rendered"]])
        with (destination / "selected_samples.csv").open(encoding="utf-8") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 8)
        self.assertTrue(all(row["positioner_context.focus_plane_id"] == "plane-A" for row in rows))
        self.assertTrue(all(row["positioner_context.coordinate_frame"] == "mount-A" for row in rows))
        self.assertTrue(all(float(row["positioner_origin.x_um"]) == 100. for row in rows))
        self.assertTrue(all("coordinate_frame" in row["status.positioner"] for row in rows))
        manifest = json.loads((destination / "plot_manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest)
        self.assertEqual(self.database.read_bytes(), before)
