"""Offline auto-colorbar boundaries, provenance and export regressions."""
import copy
import csv
import json
from pathlib import Path
import tempfile
import unittest

from attodry_control.unified_plotting import (
    _sample_token, export_plot_bundle, render_plot,
)
from tests.test_unified_plotting import X, Y, observation


TEMPERATURE = "requested.temperature_k"


def temperature_rows(temperatures, *, points=3, all_channels=False):
    rows = []
    for trace, temperature in enumerate(temperatures):
        for point in range(points):
            row = observation(point, value=(trace + 1) * (point + 1) * .001)
            row.update({TEMPERATURE: float(temperature), "axes.temperature.index": trace,
                        "condition_id": f"condition-{trace:03d}-{point:03d}"})
            if all_channels:
                for role, factor in (("xx", 1.0), ("xy", .1)):
                    for harmonic in (1, 2):
                        row[f"measured.lockin_{role}_h{harmonic}_amplitude_v"] = (
                            (trace + 1) * (point + 1) * factor * harmonic)
                for sample in row["status.lockin"]["samples"]:
                    sample["selected_roles"] = ["xx", "xy"]
                    for role in ("xx", "xy"):
                        sample["lockin_" + role] = {
                            "lia_status": {"raw": 0}, "error_status": 0,
                            "reading": {"harmonic": sample["harmonic"]}}
            rows.append(row)
    return rows


def colorbar_axes(figure):
    return [axis for axis in figure.axes if axis.get_label() == "<colorbar>"]


def data_colors(axis):
    import matplotlib.colors as colors
    return [colors.to_rgba(container.lines[0].get_color())
            for container in axis.containers
            if getattr(container, "lines", ()) and container.lines[0] is not None]


class AutoCurveColorbarTests(unittest.TestCase):
    def setUp(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        self.addCleanup(lambda: plt.close("all"))
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)

    def spec(self, **extra):
        return {"x": X, "y": Y, "group_by": TEMPERATURE,
                "curve_style": "line+points", **extra}

    def test_nine_curves_keep_legend_despite_many_observations(self):
        rows = temperature_rows(range(2, 47, 5), points=5)
        figure, data = render_plot(rows, self.spec())
        self.assertEqual(len(rows), 45)
        self.assertEqual(len(colorbar_axes(figure)), 0)
        self.assertEqual(len(figure.axes[0].get_legend().get_texts()), 9)
        self.assertEqual(data["report"]["curve_count"], 9)
        self.assertEqual(data["report"]["legend_mode"], "legend")

    def test_ten_curves_automatically_replace_full_legend_with_colorbar(self):
        figure, data = render_plot(temperature_rows(range(2, 52, 5)), self.spec())
        self.assertEqual(len(colorbar_axes(figure)), 1)
        self.assertIsNone(figure.axes[0].get_legend())
        self.assertEqual(data["report"]["curve_count"], 10)
        self.assertEqual(data["report"]["legend_mode"], "colorbar")
        encoding = data["report"]["color_encoding"]
        self.assertEqual(encoding["kind"], "numeric")
        self.assertEqual(encoding["field"], TEMPERATURE)
        self.assertIn("temperature", encoding["label"].lower())
        self.assertIn("K", encoding["label"])

    def test_uneven_temperatures_use_real_numeric_spacing(self):
        import matplotlib as mpl
        import matplotlib.colors as colors
        temperatures = [2, 2.5, 4, 9, 20, 22, 51, 88, 110, 175, 252]
        figure, data = render_plot(temperature_rows(temperatures), self.spec())
        encoding = data["report"]["color_encoding"]
        self.assertEqual((encoding["vmin"], encoding["vmax"]), (2, 252))
        cmap = mpl.colormaps[encoding["cmap"]]
        norm = colors.Normalize(2, 252)
        for actual, temperature in zip(data_colors(figure.axes[0]), temperatures):
            self.assertEqual(colors.to_hex(actual), colors.to_hex(cmap(norm(temperature))))
        self.assertNotEqual(data_colors(figure.axes[0])[5], cmap(.5))

    def test_fifty_one_temperatures_fit_without_long_legend_or_expanded_figure(self):
        figure, data = render_plot(temperature_rows(range(2, 253, 5)), self.spec())
        figure.canvas.draw()
        self.assertEqual(data["report"]["curve_count"], 51)
        self.assertEqual(len(colorbar_axes(figure)), 1)
        self.assertIsNone(figure.axes[0].get_legend())
        self.assertLess(figure.get_figwidth(), 15)
        self.assertGreater(figure.axes[0].get_window_extent().width, 300)
        bar = colorbar_axes(figure)[0]
        self.assertLessEqual(len(bar.get_yticks()), 9)

    def test_hidden_auto_colorbar_preserves_curves_statistics_and_inputs(self):
        rows = temperature_rows(range(2, 57, 5))
        before = copy.deepcopy(rows)
        shown, data = render_plot(rows, self.spec())
        hidden, no_key = render_plot(rows, self.spec(show_legend=False))
        self.assertEqual(len(colorbar_axes(shown)), 1)
        self.assertEqual(len(colorbar_axes(hidden)), 0)
        self.assertIsNone(hidden.axes[0].get_legend())
        self.assertEqual(no_key["report"]["legend_mode"], "hidden")
        self.assertEqual(data_colors(shown.axes[0]), data_colors(hidden.axes[0]))
        self.assertEqual(data["rows"], no_key["rows"])
        self.assertEqual(data["summary_rows"], no_key["summary_rows"])
        self.assertEqual(rows, before)

    def test_filters_and_quality_exclusions_count_only_visible_curves(self):
        temperatures = list(range(2, 57, 5))
        rows = temperature_rows(temperatures)
        for row in rows:
            if row[TEMPERATURE] in temperatures[-2:]:
                row["status.lockin"]["samples"][1]["lockin_xy"]["lia_status"]["raw"] = 4
        figure, data = render_plot(rows, self.spec())
        self.assertEqual(data["report"]["curve_count"], 9)
        self.assertEqual(len(colorbar_axes(figure)), 0)
        self.assertEqual(len(figure.axes[0].get_legend().get_texts()), 9)
        audit, audited = render_plot(rows, self.spec(quality_policy="include"))
        self.assertEqual(audited["report"]["curve_count"], 11)
        self.assertEqual(len(colorbar_axes(audit)), 1)
        filtered, selection = render_plot(rows, self.spec(
            quality_policy="include", filters={TEMPERATURE: temperatures[:9]}))
        self.assertEqual(selection["report"]["curve_count"], 9)
        self.assertEqual(len(colorbar_axes(filtered)), 0)

    def test_excluding_or_missing_entire_curves_reverts_to_nine_curve_legend(self):
        temperatures = list(range(2, 57, 5))
        rows = temperature_rows(temperatures)
        excluded = [_sample_token(row) for row in rows if row[TEMPERATURE] in temperatures[-2:]]
        for kind in ("manual", "missing"):
            selected = copy.deepcopy(rows)
            extra = {"excluded_sample_ids": excluded} if kind == "manual" else {}
            if kind == "missing":
                for row in selected:
                    if row[TEMPERATURE] in temperatures[-2:]:
                        row[Y] = None
            with self.subTest(kind=kind):
                figure, data = render_plot(selected, self.spec(**extra))
                self.assertEqual(data["report"]["curve_count"], 9)
                self.assertEqual(len(colorbar_axes(figure)), 0)
                self.assertEqual(len(figure.axes[0].get_legend().get_texts()), 9)

    def test_four_channel_plot_uses_one_shared_scale_and_one_colorbar(self):
        rows = temperature_rows(range(2, 57, 5), all_channels=True)
        figure, data = render_plot(rows, self.spec(mode="field"))
        axes = [axis for axis in figure.axes if axis not in colorbar_axes(figure)]
        self.assertEqual(len(axes), 4)
        self.assertEqual(len(colorbar_axes(figure)), 1)
        self.assertEqual(data["report"]["curve_count"], 11)
        self.assertTrue(all(data_colors(axis) == data_colors(axes[0]) for axis in axes))
        self.assertTrue(all(axis.get_legend() is None for axis in axes))

    def test_four_panels_do_not_multiply_the_curve_threshold(self):
        figure, data = render_plot(
            temperature_rows(range(2, 17, 5), all_channels=True), self.spec(mode="field"))
        self.assertEqual(len(colorbar_axes(figure)), 0)
        self.assertEqual(data["report"]["curve_count"], 3)
        self.assertTrue(all(len(axis.get_legend().get_texts()) == 3 for axis in figure.axes))

    def test_same_temperature_across_range_recovery_stays_separate_but_same_color(self):
        rows = temperature_rows([2 + 5 * i for i in range(10)] + [147])
        for row in rows:
            new = row[TEMPERATURE] == 147 and row[X] > 0
            row["configuration_revision"] = "range_5mV" if new else "range_500uV"
        figure, data = render_plot(rows, self.spec())
        encoding = data["report"]["color_encoding"]
        pairs = [item for item in encoding["mapping"] if item["group"][TEMPERATURE] == 147]
        self.assertEqual(len(pairs), 2)
        self.assertEqual(pairs[0]["color"], pairs[1]["color"])
        self.assertNotEqual(pairs[0]["group"], pairs[1]["group"])
        self.assertEqual(data["report"]["curve_count"], 12)
        lines = [container.lines[0] for container in figure.axes[0].containers]
        self.assertEqual(sorted(len(line.get_xdata()) for line in lines[-2:]), [1, 2])
        summary_groups = {json.dumps(p["group"], sort_keys=True) for p in data["summary_rows"]
                          if p["group"][TEMPERATURE] == 147}
        self.assertEqual(len(summary_groups), 2)

    def test_same_temperature_across_runs_keeps_distinct_curves_and_shared_color(self):
        rows = temperature_rows(range(2, 57, 5))
        second_run = [dict(row, run_id="second_run") for row in rows
                      if row[TEMPERATURE] == 2]
        figure, data = render_plot(rows + second_run, self.spec())
        encoding = data["report"]["color_encoding"]
        same_temperature = [item for item in encoding["mapping"]
                            if item["group"][TEMPERATURE] == 2]
        self.assertEqual(data["report"]["curve_count"], 12)
        self.assertEqual(len(same_temperature), 2)
        self.assertEqual({item["group"]["run_id"] for item in same_temperature},
                         {"test", "second_run"})
        self.assertEqual(len({item["color"] for item in same_temperature}), 1)
        self.assertEqual(len(data["rows"]), len(rows) + len(second_run))
        self.assertEqual(len(colorbar_axes(figure)), 1)

    def test_nonnumeric_groups_use_explicit_curve_index_mapping(self):
        rows = temperature_rows(range(2, 57, 5))
        for row in rows:
            row["source_kind"] = f"category-{int(row[TEMPERATURE]):03d}"
            row.pop(TEMPERATURE)
            row.pop("axes.temperature.index")
        figure, data = render_plot(rows, self.spec(group_by="source_kind"))
        encoding = data["report"]["color_encoding"]
        self.assertEqual(encoding["kind"], "curve_index")
        self.assertIsNone(encoding["field"])
        self.assertIn("curve", encoding["label"].lower())
        self.assertEqual(len(encoding["mapping"]), 11)
        self.assertEqual({m["index"] for m in encoding["mapping"]}, set(range(1, 12)))
        self.assertEqual({m["group"]["source_kind"] for m in encoding["mapping"]},
                         {row["source_kind"] for row in rows})
        self.assertEqual(len(colorbar_axes(figure)), 1)

    def test_multiple_non_temperature_numeric_dimensions_do_not_invent_one_scale(self):
        rows = []
        frequency = "requested.lockin_frequency_hz"
        excitation = "requested.lockin_excitation_v_rms"
        for index in range(11):
            for current in (1., 2.):
                for point in range(2):
                    row = observation(point, value=index + point + current)
                    row.update({frequency: 10. + index * 5, excitation: current,
                                "condition_id": f"{index}-{current}-{point}"})
                    rows.append(row)
        figure, data = render_plot(rows, self.spec(group_by=frequency,
            filters={excitation: [1., 2.]}, stack_filters=[excitation]))
        self.assertEqual(data["report"]["color_encoding"]["kind"], "curve_index")
        self.assertEqual(data["report"]["curve_count"], 22)
        self.assertEqual(len(colorbar_axes(figure)), 1)

    def test_export_keeps_numeric_color_mapping_and_exact_selected_samples(self):
        rows = temperature_rows(range(2, 57, 5))
        before = copy.deepcopy(rows)
        spec = self.spec()
        figure, data = render_plot(rows, spec)
        destination = export_plot_bundle(self.directory, [
            {"figure": figure, "data": data, "spec": spec}])
        self.assertTrue((destination / "plot_01.png").read_bytes().startswith(b"\x89PNG"))
        self.assertTrue((destination / "plot_01.pdf").read_bytes().startswith(b"%PDF"))
        svg = (destination / "plot_01.svg").read_text(encoding="utf-8")
        self.assertIn("temperature", svg.lower())
        manifest = json.loads((destination / "plot_manifest.json").read_text(encoding="utf-8"))
        report = manifest["plots"][0]["report"]
        self.assertEqual(report["legend_mode"], "colorbar")
        self.assertEqual(report["color_encoding"], data["report"]["color_encoding"])
        with (destination / "selected_samples.csv").open(encoding="utf-8", newline="") as f:
            selected = list(csv.DictReader(f))
        self.assertEqual(len(selected), len(rows))
        self.assertEqual({r["condition_id"] for r in selected}, {r["condition_id"] for r in rows})
        self.assertEqual({float(r[TEMPERATURE]) for r in selected}, {r[TEMPERATURE] for r in rows})
        self.assertEqual(rows, before)


if __name__ == "__main__":
    unittest.main()
