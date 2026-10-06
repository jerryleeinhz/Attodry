"""Synthetic offline checks for exact heatmap cells, identities and exports."""
import ast
import copy
import csv
import json
import math
from pathlib import Path
import tempfile
import unittest

from attodry_control.analysis_observations import observation_id
from attodry_control.plotting_heatmap import MISSING_COLOR, render_heatmap
from attodry_control.unified_plotting import _filter_rows, _sample_token, export_plot_bundle


X = "actual.field_x_t"
Y = "actual.lockin_excitation_v_rms"
Z = "measured.lockin_xy_h2_y_v"


def grid(xs=(-1., 1.), ys=(.001, .002), *, segment="outward", sample=0, offset=0.):
    rows = []
    for iy, y in enumerate(ys):
        for ix, x in enumerate(xs):
            rows.append({
                "source_path": "synthetic.sqlite", "run_id": "heatmap-test", "accepted": True,
                "condition_id": f"{segment}:{ix}:{iy}", "attempt_index": 0,
                "sample_index": sample, "repeat_index": 0, "simulated": True,
                "axes.magnetic.index": ix, "axes.lockin.index": iy,
                "axes.magnetic.segment": segment,
                "axes.magnetic.direction": "down" if "return" in segment else "up",
                "requested.field_x_t": x, "requested.field_z_t": 0.,
                "requested.lockin_excitation_v_rms": y, "requested.lockin_frequency_hz": 50000.,
                "requested.temperature_k": 2.1, X: x, Y: y, Z: x + iy + offset,
                "status.lockin": {"samples": [{"harmonic": 2, "settings_verified": True,
                    "selected_roles": ["xy"], "lockin_xy": {"lia_status": {"raw": 0},
                    "error_status": 0, "reading": {"harmonic": 2}}}]},
            })
    return rows


class HeatmapTests(unittest.TestCase):
    def setUp(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        self.addCleanup(lambda: plt.close("all"))
        self.spec = dict(mode="heatmap", x=X, y=Y, z=Z)

    def render(self, rows, **changes):
        return render_heatmap(rows, {**self.spec, **changes})

    def test_four_hysteresis_segments_and_duplicate_endpoints_are_separate(self):
        rows = []
        for index, segment in enumerate(("positive-outward", "positive-return", "negative-outward", "negative-return")):
            rows.extend(grid(segment=segment, offset=index))
        figure, data = self.render(rows)
        report = data["report"]
        self.assertEqual(report["panel_count"], 4)
        self.assertEqual(report["displayed_cell_count"], 16)
        self.assertEqual(len(data["rows"]), 16)
        self.assertIn("axes.magnetic.segment", report["facet_keys"])
        self.assertIn("axes.magnetic.direction", report["facet_keys"])
        self.assertNotIn("axes.magnetic.index", report["facet_keys"])
        self.assertEqual({p["facets"]["axes.magnetic.segment"] for p in report["panels"]},
                         {"positive-outward", "positive-return", "negative-outward", "negative-return"})
        meshes = [axis.collections[0] for axis in figure.axes if axis.collections and axis.get_xlabel()]
        self.assertEqual(len(meshes), 4)
        self.assertTrue(all(mesh.norm is meshes[0].norm for mesh in meshes))

    def test_missing_manual_quality_and_nonfinite_cells_remain_gray_gaps(self):
        import matplotlib.colors as colors
        import numpy as np
        rows = grid(xs=(-1., 0., 1.), ys=(.001, .002, .003))
        missing = rows.pop(4)
        manual, flagged, nonfinite = rows[0], rows[1], rows[-1]
        flagged["status.lockin"]["samples"][0]["lockin_xy"]["lia_status"]["raw"] = 4
        nonfinite[Z] = None
        before = copy.deepcopy(rows)
        figure, data = self.render(rows, excluded_sample_ids=[_sample_token(manual)])
        report = data["report"]
        self.assertEqual(report["panels"][0]["shape"], [3, 3])
        self.assertEqual((report["plotted_row_count"], report["masked_cell_count"]), (5, 4))
        self.assertEqual(report["manual_excluded_count"], 1)
        self.assertEqual(report["excluded_quality_count"], 1)
        self.assertEqual(report["omitted_missing_or_nonfinite_count"], 1)
        cells = {(p["x"], p["y"]): p for p in data["summary_rows"]}
        for row, reason in ((missing, "missing"), (manual, "manual_excluded"),
                            (flagged, "quality_excluded"), (nonfinite, "missing_or_nonfinite")):
            cell = cells[row[X], row[Y]]
            self.assertTrue(cell["masked"])
            self.assertIsNone(cell["z"])
            self.assertIn(reason, cell["mask_reasons"])
        mesh = figure.axes[0].collections[0]
        self.assertEqual(np.ma.getmaskarray(mesh.get_array()).sum(), 4)
        figure.canvas.draw()
        facecolors = mesh.get_facecolors()
        self.assertEqual(sum(np.allclose(color, colors.to_rgba(MISSING_COLOR)) for color in facecolors), 4)
        self.assertEqual(figure.legends[0].get_texts()[0].get_text(), "Missing/excluded")
        self.assertEqual(rows, before)

    def test_entire_excluded_column_keeps_its_grid_coordinates(self):
        rows = grid()
        for row in rows:
            if row[X] == 1.:
                row["_manual_excluded"] = True
        _, data = self.render(rows)
        self.assertEqual(data["report"]["panels"][0]["x_coordinates"], [-1., 1.])
        self.assertEqual(data["report"]["panels"][0]["masked_cells"], [[False, True], [False, True]])
        self.assertEqual(len(data["report"]["manual_excluded_samples"]), 2)

    def test_source_run_repeat_simulation_role_and_harmonic_are_automatic_facets(self):
        for key, value in (("source_path", "other.sqlite"), ("run_id", "another-run"),
                           ("repeat_index", 1), ("simulated", False), ("role", "lockin_xy"), ("harmonic", 2)):
            with self.subTest(key=key):
                rows = grid()
                rows.extend({**row, key: value} for row in grid())
                _, data = self.render(rows, statistics="mean_sd")
                self.assertEqual(data["report"]["panel_count"], 2)
                self.assertIn(key, data["report"]["facet_keys"])
                self.assertEqual(data["report"]["displayed_cell_count"], 8)

    def test_different_conditions_attempts_and_axis_indices_cannot_average(self):
        for key, value in (("condition_id", "another-visit"), ("attempt_index", 1), ("axes.magnetic.index", 99)):
            with self.subTest(key=key):
                rows = grid()
                rows.append({**copy.deepcopy(rows[0]), key: value, "sample_index": 1})
                with self.assertRaisesRegex(ValueError, "Conflicting recorded condition/attempt/axis-index"):
                    self.render(rows, statistics="mean_sd")

    def test_conflicting_excluded_visit_is_not_silently_merged(self):
        rows = grid()
        rows.append({**copy.deepcopy(rows[0]), "condition_id": "other", "_manual_excluded": True})
        with self.assertRaisesRegex(ValueError, "different visits cannot be averaged"):
            self.render(rows, statistics="mean_sd")

    def test_formal_repeats_have_mean_sd_sem_and_exact_sample_ids(self):
        rows = []
        for sample, value in enumerate((1., 2., 3.)):
            repeated = grid(sample=sample)
            for row in repeated:
                row[Z] = value
            rows.extend(repeated)
        _, data = self.render(rows, statistics="mean_sem")
        self.assertEqual(len(data["rows"]), 12)
        self.assertEqual(data["report"]["displayed_cell_count"], 4)
        for point in data["summary_rows"]:
            self.assertEqual((point["z"], point["mean"], point["n"], point["sd"]), (2., 2., 3, 1.))
            self.assertAlmostEqual(point["sem"], 1 / math.sqrt(3))
            self.assertEqual(point["error"], point["sem"])
            self.assertEqual({i["sample_index"] for i in point["sample_ids"]}, {0, 1, 2})
        self.assertEqual(data["report"]["color_value"], "within-condition mean")

    def test_raw_repeat_cells_require_explicit_statistics_choice(self):
        rows = grid() + grid(sample=1)
        with self.assertRaisesRegex(ValueError, "one qualified sample per cell"):
            self.render(rows)

    def test_phase_mean_is_circular_and_antipodal_cell_is_masked(self):
        phase = Z.replace("y_v", "phase_deg")
        rows = grid() + grid(sample=1)
        for row in rows:
            row[phase] = 179. if row["sample_index"] == 0 else -179.
        _, data = self.render(rows, z=phase, statistics="mean_sd")
        self.assertTrue(all(abs(point["mean"]) == 180. for point in data["summary_rows"]))
        self.assertTrue(all(abs(point["sd"] - math.sqrt(2)) < 1e-8 for point in data["summary_rows"]))
        rows[0][phase], rows[4][phase] = 0., 180.
        _, data = self.render(rows, z=phase, statistics="mean_sd")
        point = data["summary_rows"][0]
        self.assertTrue(point["masked"])
        self.assertEqual(point["n"], 2)
        self.assertEqual(len(point["sample_ids"]), 2)
        self.assertEqual(point["mask_reasons"], ["undefined_phase_mean"])

    def test_noisy_actual_axes_use_requested_grid_without_altering_readbacks(self):
        rows = grid()
        for index, row in enumerate(rows):
            row[X] += (index + 1) * .0001
            row[Y] += (index + 1) * .000001
        before = copy.deepcopy(rows)
        figure, data = self.render(rows)
        report = data["report"]
        self.assertEqual(report["panels"][0]["x_coordinates"], [-1., 1.])
        self.assertEqual(report["panels"][0]["y_coordinates"], [.001, .002])
        self.assertEqual(report["coordinate_mappings"]["x"]["grid_column"], "requested.field_x_t")
        self.assertEqual(report["coordinate_mappings"]["y"]["grid_column"], "requested.lockin_excitation_v_rms")
        self.assertTrue(figure.axes[0].get_xlabel().startswith("Requested "))
        self.assertTrue(figure.axes[0].get_ylabel().startswith("Requested "))
        self.assertEqual(list(data["rows"]), before)
        self.assertEqual(rows, before)

    def test_missing_requested_coordinate_never_falls_back_to_actual(self):
        rows = grid()
        rows[0].pop("requested.field_x_t")
        rows[0][X] = -.9999
        _, data = self.render(rows)
        self.assertEqual(data["report"]["coordinate_mappings"]["x"]["missing_grid_coordinate_count"], 1)
        self.assertEqual(data["report"]["panels"][0]["x_coordinates"], [-1., 1.])
        self.assertEqual(data["report"]["masked_cell_count"], 1)
        self.assertEqual(data["report"]["missing_coordinate_count"], 1)
        self.assertEqual(len(data["rows"]), 3)

    def test_absent_requested_counterpart_preserves_exact_selected_coordinates(self):
        rows = grid()
        for index, row in enumerate(rows):
            row.pop("requested.field_x_t")
            row[X] += (index + 1) * .0001
        _, data = self.render(rows)
        report = data["report"]
        self.assertEqual(report["coordinate_mappings"]["x"]["grid_column"], X)
        self.assertEqual(report["panels"][0]["x_coordinates"], sorted({r[X] for r in rows}))
        self.assertEqual(report["masked_cell_count"], 4)
        self.assertEqual(len(data["rows"]), 4)

    def test_nonuniform_grid_uses_midpoint_edges_without_interpolation(self):
        import numpy as np
        rows = grid(xs=(0., 1., 3.), ys=(.001, .003, .009))
        figure, data = self.render(rows)
        panel = data["report"]["panels"][0]
        self.assertEqual(panel["x_edges"], [-.5, .5, 2., 4.])
        np.testing.assert_allclose(panel["y_edges"], [0., .002, .006, .012])
        self.assertEqual(data["report"]["interpolation"], "none")
        mesh = figure.axes[0].collections[0]
        np.testing.assert_allclose(mesh.get_coordinates()[0, :, 0], panel["x_edges"])
        np.testing.assert_allclose(mesh.get_coordinates()[:, 0, 1], panel["y_edges"])
        self.assertEqual(mesh.get_array().size, 9)

    def test_signed_zero_center_and_common_colorbar_units(self):
        import matplotlib.colors as colors
        rows = grid()
        for index, row in enumerate(rows):
            row[Z] = index + 1.
        figure, data = self.render(rows)
        norm = figure.axes[0].collections[0].norm
        self.assertIsInstance(norm, colors.TwoSlopeNorm)
        self.assertEqual(norm(0.), .5)
        self.assertEqual((norm.vmin, norm.vmax), (-4., 4.))
        self.assertEqual(data["report"]["colormap"], "RdBu_r")
        self.assertIn("Lock-in XY h2 Y (V)", figure.axes[-1].get_ylabel())

    def test_positive_amplitude_uses_sequential_color(self):
        amplitude = Z.replace("y_v", "amplitude_v")
        rows = grid()
        for index, row in enumerate(rows):
            row[amplitude] = index + 1.
        _, data = self.render(rows, z=amplitude)
        self.assertEqual(data["report"]["colormap"], "viridis")
        self.assertIsNone(data["report"]["color_normalization"]["vcenter"])

    def test_unresolved_other_coordinate_requires_fixed_filter(self):
        rows = grid()
        rows[0]["requested.temperature_k"] = 3.
        with self.assertRaisesRegex(ValueError, "fixed-condition filter"):
            self.render(rows)
        spec = {**self.spec, "filters": {"requested.temperature_k": [2.1]}}
        _, data = render_heatmap(_filter_rows(rows, spec["filters"]), spec)
        self.assertEqual(data["report"]["masked_cell_count"], 1)

    def test_one_dimensional_selection_recommends_scatter(self):
        for rows in (grid(xs=(0.,)), grid(ys=(.001,))):
            with self.assertRaisesRegex(ValueError, "use scatter"):
                self.render(rows)

    def test_unidentified_csv_requires_raw_for_statistics(self):
        rows = [{"csv.x": x, "csv.y": y, "csv.z": x + y} for x in (0., 1.) for y in (1., 2.)]
        figure, data = render_heatmap(rows, dict(x="csv.x", y="csv.y", z="csv.z"))
        self.assertEqual(data["report"]["statistics"], "raw")
        self.assertEqual(len(data["rows"]), 4)
        self.assertIn("unit unspecified", figure.axes[-1].get_ylabel())
        with self.assertRaisesRegex(ValueError, "condition_id"):
            render_heatmap(rows, dict(x="csv.x", y="csv.y", z="csv.z", statistics="mean_sd"))

    def test_all_quality_excluded_can_display_geometry_without_colorbar(self):
        rows = grid()
        for row in rows:
            row["status.lockin"]["samples"][0]["lockin_xy"]["lia_status"]["raw"] = 4
        figure, data = self.render(rows)
        self.assertEqual((data["report"]["plotted_row_count"], data["report"]["masked_cell_count"]), (0, 4))
        self.assertTrue(data["report"]["color_normalization"]["empty"])
        self.assertEqual(len(figure.axes), 1)

    def test_audit_quality_include_retains_flagged_ids_and_visible_warning(self):
        rows = grid()
        rows[0]["accepted"] = False
        rows[0]["status.lockin"]["samples"][0]["lockin_xy"]["lia_status"]["raw"] = 4
        figure, data = self.render(rows, quality_policy="include")
        self.assertEqual((len(data["rows"]), data["report"]["flagged_row_count"]), (4, 1))
        self.assertEqual(data["report"]["excluded_quality_count"], 0)
        self.assertEqual(data["report"]["flagged_samples"][0]["sample"], observation_id(rows[0]))
        title = figure._suptitle.get_text()
        self.assertIn("flagged samples included", title)
        self.assertIn("AUDIT: rejected", title)
        self.assertIn("SIMULATED", title)

    def test_export_preserves_original_rows_cell_statistics_and_masks(self):
        rows = grid() + grid(sample=1, offset=2.)
        rows[0]["_manual_excluded"] = True
        spec = {**self.spec, "statistics": "mean_sd"}
        figure, data = render_heatmap(rows, spec)
        with tempfile.TemporaryDirectory() as tmp:
            destination = export_plot_bundle(tmp, [{"figure": figure, "data": data, "spec": spec}])
            manifest = json.loads((destination / "plot_manifest.json").read_text(encoding="utf-8"))
            report = manifest["plots"][0]["report"]
            self.assertEqual(manifest["selected_sample_rows"], 7)
            self.assertEqual(report["manual_excluded_count"], 1)
            self.assertEqual(report["coordinate_mappings"]["x"]["grid_column"], "requested.field_x_t")
            self.assertEqual(report["interpolation"], "none")
            self.assertEqual(report["panels"][0]["x_edges"], [-2., 0., 2.])
            with (destination / "plotted_statistics.csv").open(encoding="utf-8") as stream:
                summary = list(csv.DictReader(stream))
            self.assertEqual(len(summary), 4)
            self.assertEqual(summary[0]["n"], "1")
            self.assertEqual(json.loads(summary[0]["sample_ids"]), [observation_id(rows[4])])
            self.assertEqual(json.loads(summary[0]["excluded_samples"])[0]["reason"], "manual_excluded")
            self.assertTrue(all((destination / ("plot_01." + ext)).is_file() for ext in ("png", "pdf", "svg")))

    def test_backend_imports_no_instrument_drivers(self):
        import attodry_control.plotting_heatmap as module
        forbidden = {"sr830", "sr865a", "attodry", "pyvisa", "qcodes", "serial", "keithley2400"}
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = [alias.name for alias in node.names] if isinstance(node, ast.Import) else [node.module] if isinstance(node, ast.ImportFrom) else []
            self.assertFalse(any(name and name.split(".")[-1] in forbidden for name in names))


if __name__ == "__main__":
    unittest.main()
