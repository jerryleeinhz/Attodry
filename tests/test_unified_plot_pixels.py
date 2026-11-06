"""Offline checks of pixel geometry, provenance, legends and setup compatibility."""
from contextlib import redirect_stdout
from copy import deepcopy
import csv
import io
import json
import math
from pathlib import Path
import tempfile
import unittest

from attodry_control.unified_plotting import (
    UnifiedPlotDashboard, _sample_token, export_plot_bundle, render_plot,
)


X, Y, Z = "csv.x", "csv.y", "measured.lockin_xx_h1_x_v"


def sample(x=0., y=0., z=1., index=0, flag=0):
    return {X: x, Y: y, Z: z, "source_path": "synthetic.sqlite", "run_id": "synthetic",
            "condition_id": f"{x}/{y}", "sample_index": index, "attempt_index": 0,
            "repeat_index": 0, "accepted": True, "segment": "main", "direction": "forward",
            "status.lockin": {"samples": [{"harmonic": 1, "settings_verified": True,
                "selected_roles": ["xx"], "lockin_xx": {"lia_status": {"raw": flag}, "error_status": 0}}]}}


def map_spec(**kwargs):
    return dict(mode="xy_z", x=X, y=Y, z=Z, map_style="pixels", statistics="mean_sd", **kwargs)


class PixelPlotTests(unittest.TestCase):
    def setUp(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        self.addCleanup(lambda: plt.close("all"))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

    def test_regular_and_nonuniform_rectangles_match_export_cells(self):
        for xs in ((0., 1., 2.), (0., 1., 4.)):
            with self.subTest(xs=xs):
                rows = [sample(x, y, x-y) for x in xs for y in (0., 2.)]
                before = deepcopy(rows)
                fig, data = render_plot(rows, map_spec(show_legend=False))
                cells = data["pixel_cells"]
                self.assertEqual(len(cells), 6)
                self.assertEqual(data["report"]["blank_cell_count"], 0)
                self.assertEqual(len(fig.axes), 2)  # Legend toggle never hides colorbar.
                self.assertEqual(cells[2]["x_min"], .5)
                self.assertEqual(cells[2]["x_max"], (xs[1] + xs[2]) / 2)
                for cell, path in zip(cells, fig.axes[0].collections[0].get_paths()):
                    self.assertEqual(tuple(path.vertices.min(axis=0)), (cell["x_min"], cell["y_min"]))
                    self.assertEqual(tuple(path.vertices.max(axis=0)), (cell["x_max"], cell["y_max"]))
                self.assertEqual(rows, before)

    def test_missing_manual_and_flagged_cells_do_not_expand_neighbors(self):
        rows = [sample(x, y, flag=4 if x == 1 else 0) for x in (0., 1., 2.) for y in (0., 1.)]
        rows[-1][Z] = None
        spec = map_spec(excluded_sample_ids=[_sample_token(rows[1])])
        fig, data = render_plot(rows, spec)
        self.assertEqual([(c["x"], c["y"]) for c in data["pixel_cells"]], [(0., 0.), (2., 0.)])
        self.assertEqual(data["pixel_cells"][0]["x_max"], .5)
        self.assertEqual(data["pixel_cells"][1]["x_min"], 1.5)
        self.assertEqual(data["report"]["blank_cell_count"], 4)
        self.assertEqual(data["report"]["excluded_quality_count"], 2)
        self.assertEqual(sum(p["n"] == 0 for p in data["summary_rows"]), 4)
        self.assertEqual(tuple(fig.axes[0].get_ylim()), (-.5, 1.5))
        _, audit = render_plot(rows, {**spec, "quality_policy": "include"})
        self.assertEqual(len(audit["pixel_cells"]), 4)
        self.assertEqual(audit["report"]["flagged_row_count"], 2)

    def test_unmeasured_grid_intersection_stays_blank(self):
        rows = [sample(0., 0.), sample(1., 0.), sample(0., 1.)]
        fig, data = render_plot(rows, map_spec())
        self.assertEqual(len(fig.axes[0].collections[0].get_paths()), 3)
        self.assertEqual(data["report"]["blank_cell_count"], 1)

    def test_repeat_mean_n_and_raw_collision(self):
        rows = [sample(z=z, index=i) for i, z in enumerate((1., 2., 3.))]
        spec = map_spec(pixel_x_width=2., pixel_y_width=4.)
        _, data = render_plot(rows, spec)
        cell, = data["pixel_cells"]
        self.assertEqual((cell["z"], cell["n"], cell["sd"]), (2., 3, 1.))
        self.assertEqual(len(cell["sample_ids"]), 3)
        self.assertEqual((cell["x_min"], cell["y_max"]), (-1., 2.))
        with self.assertRaisesRegex(ValueError, "Multiple observations"):
            render_plot(rows, {**spec, "statistics": "raw"})

    def test_circular_phase_mean_and_undefined_mean_blank(self):
        phase = Z.replace("x_v", "phase_deg")
        rows = [{**sample(index=i), phase: z} for i, z in enumerate((179., -179.))]
        rows += [{**sample(1., index=i), phase: z} for i, z in enumerate((0., 180.))]
        _, data = render_plot(rows, {**map_spec(pixel_y_width=1.), "z": phase})
        cell, = data["pixel_cells"]
        self.assertAlmostEqual(abs(cell["z"]), 180.)
        self.assertAlmostEqual(cell["sd"], math.sqrt(2))
        self.assertEqual(data["report"]["blank_cell_count"], 1)
        self.assertEqual(len(data["rows"]), 2)

    def test_power_response_pixels_keep_complex_mean_and_reference_audit(self):
        from tests.test_optical_power_reference import make_rows
        rows = make_rows()
        power = "requested.optical_target_power_w"
        phase = Z.replace("x_v", "phase_deg")
        spec = {**map_spec(), "x": power, "y": "requested.temperature_k", "z": phase,
                "pixel_x_width": 1e-6, "pixel_y_width": 1., "photoresponse": "power_lowest",
                "filters": {power: [6e-6]}, "statistics": "mean_sem"}
        _, data = render_plot(rows, spec)
        cell, = data["pixel_cells"]
        self.assertEqual(cell["n"], 2)
        self.assertAlmostEqual(cell["z"], math.degrees(math.atan2(.002, .01)))
        self.assertIn("power_delta_phase_deg", cell["z_column"])
        self.assertIsNotNone(cell["sem"])
        self.assertEqual(len(data["audit_rows"]), 2)
        self.assertEqual(data["summary_rows"][0]["power_reference_statistics"],
                         "complex mean before magnitude/phase; shared reference covariance")

    def test_distinct_runs_segments_directions_and_visits_never_pool(self):
        base = sample()
        for key, value in (("run_id", "another"), ("source_path", "another.sqlite"),
                           ("condition_id", "revisit"), ("attempt_index", 1), ("repeat_index", 1),
                           ("segment", "return"), ("direction", None),
                           ("axes.smu.direction", "reverse")):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "Multiple observations"):
                render_plot([base, {**base, "sample_index": 1, key: value}],
                            map_spec(pixel_x_width=1., pixel_y_width=1.))

    def test_single_coordinate_axis_requires_explicit_width(self):
        for width in (None, 0., -1., float("nan")):
            with self.subTest(width=width), self.assertRaisesRegex(ValueError, "X cell width"):
                render_plot([sample()], map_spec(pixel_x_width=width, pixel_y_width=1.))
        with self.assertRaisesRegex(ValueError, "Y cell width"):
            render_plot([sample()], map_spec(pixel_x_width=1.))

    def test_actual_coordinate_jitter_is_not_rounded(self):
        rows = [sample(x=x, index=i) for i, x in enumerate((1., 1.00000001, 1.00000002))]
        _, data = render_plot(rows, map_spec(pixel_y_width=1.))
        self.assertEqual([c["x"] for c in data["pixel_cells"]], [r[X] for r in rows])
        self.assertTrue(all(c["n"] == 1 for c in data["pixel_cells"]))

    def test_log_boundaries_cannot_be_clipped(self):
        with self.assertRaisesRegex(ValueError, "positive pixel boundaries"):
            render_plot([sample(1.), sample(10.)], map_spec(x_scale="log", pixel_y_width=1.))

    def test_unidentified_csv_raw_pixels_and_legacy_scatter(self):
        rows = [{X: 0., Y: 0., Z: 1.}, {X: 1., Y: 1., Z: 2.}]
        _, data = render_plot(rows, {**map_spec(), "statistics": "raw"})
        self.assertEqual(len(data["pixel_cells"]), 2)
        with self.assertRaisesRegex(ValueError, "condition_id"):
            render_plot(rows, map_spec())
        _, data = render_plot([sample(), sample(index=1)], {"mode": "xy_z", "x": X, "y": Y, "z": Z})
        self.assertEqual(data["report"]["map_style"], "scatter")
        self.assertEqual(data["report"]["duplicate_xy_observation_count"], 1)

    def test_multi_run_export_keeps_raw_rows_and_exact_cell_provenance(self):
        rendered = []
        spec = map_spec(pixel_x_width=1., pixel_y_width=1.)
        for run in ("first", "second"):
            rows = [{**sample(z=i, index=i), "run_id": run, "actual.extra_readback": 1.23} for i in range(3)]
            fig, data = render_plot(rows, spec)
            rendered.append(dict(figure=fig, data=data, spec=spec))
        path = export_plot_bundle(self.directory, rendered)
        with (path / "pixel_cells.csv").open(encoding="utf-8") as stream:
            cells = list(csv.DictReader(stream))
        self.assertEqual([(c["plot_id"], c["run_id"]) for c in cells], [("plot_01", "first"), ("plot_02", "second")])
        self.assertTrue(all(c["n"] == "3" and c["z"] == "1.0" for c in cells))
        self.assertEqual(json.loads(cells[0]["sample_ids"]), rendered[0]["data"]["pixel_cells"][0]["sample_ids"])
        self.assertEqual(cells[0]["segment"], "main")
        self.assertEqual(float(cells[0]["x_min"]), -.5)
        with (path / "selected_samples.csv").open(encoding="utf-8") as stream:
            raw = list(csv.DictReader(stream))
        self.assertEqual(len(raw), 6)
        self.assertTrue(all(r["actual.extra_readback"] == "1.23" for r in raw))
        manifest = json.loads((path / "plot_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["pixel_cell_rows"], 2)
        self.assertEqual(manifest["interpolation"], "none")

    def test_single_many_and_multichannel_legends_toggle(self):
        rows = [{**sample(), "run_id": f"LegendOnlyRun{i}"} for i in range(13)]
        for count in (1, 13):
            for show in (True, False):
                fig, _ = render_plot(rows[:count], dict(x=X, y=Z, show_legend=show))
                legend = fig.axes[0].get_legend()
                self.assertEqual(legend is not None, show)
                if show:
                    self.assertEqual(len(legend.get_texts()), count)
        for mode, count in (("field", 4), ("three_channels", 3)):
            for show in (True, False):
                fig, _ = render_plot([sample()], dict(mode=mode, x=X, field_component="x_v", show_legend=show))
                self.assertEqual(len(fig.axes), count)
                self.assertTrue(all((ax.get_legend() is not None) == show for ax in fig.axes))

    def test_exported_svg_respects_legend_checkbox(self):
        rows = [{**sample(x=i), "run_id": f"LegendOnlyRun{i}"} for i in range(2)]
        for show in (True, False):
            spec = dict(x=X, y=Z, show_legend=show)
            fig, data = render_plot(rows, spec)
            path = export_plot_bundle(self.directory, [dict(figure=fig, data=data, spec=spec)])
            self.assertEqual("LegendOnlyRun0" in (path / "plot_01.svg").read_text(encoding="utf-8"), show)
            self.assertFalse((path / "pixel_cells.csv").exists())

    def test_controls_save_load_invalidation_and_legacy_map(self):
        path = self.directory / "data.csv"
        path.write_text("x,y,z\n0,0,1\n1,1,2\n", encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            dashboard = UnifiedPlotDashboard(self.directory)
            dashboard.add_plot("xy_z")
            fresh = dashboard.cards[-1]
            self.assertEqual(fresh["map_style"].value, "pixels")
            self.assertFalse(fresh["statistics"].disabled)
            dashboard.add_plot("xy_z", dict(source_paths=[str(path)], x=X, y=Y, z="csv.z"))
            legacy = dashboard.cards[-1]
            self.assertEqual(legacy["map_style"].value, "scatter")
            self.assertTrue(legacy["statistics"].disabled)
            legacy["map_style"].value = "pixels"
            legacy["pixel_x_width"].value = 2.
            legacy["pixel_y_width"].value = 3.
            legacy["show_legend"].value = False
            dashboard._render_card(legacy)
            self.assertIsNotNone(legacy["rendered"])
            legacy["map_style"].value = "scatter"
            self.assertIsNone(legacy["rendered"])
            self.assertFalse(legacy["image"].value)
            legacy["map_style"].value = "pixels"
            dashboard._remove_card(fresh)
            dashboard.configuration_path.value = str(self.directory / "setup.json")
            dashboard._save_setup()
            dashboard._load_setup()
            restored = dashboard.cards[0]
            spec = dashboard._card_spec(restored)
            self.assertEqual((spec["map_style"], spec["pixel_x_width"], spec["pixel_y_width"]), ("pixels", 2., 3.))
            self.assertFalse(spec["show_legend"])
            dashboard.add_plot("curve", dict(source_paths=[str(path)], x=X, y=Y))
            curve = dashboard.cards[-1]
            dashboard._render_card(curve)
            self.assertIsNotNone(curve["rendered"])
            curve["show_legend"].value = False
            self.assertIsNone(curve["rendered"])


if __name__ == "__main__":
    unittest.main()
