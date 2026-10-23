"""Offline power-reference plotting dispatch, persistence and export regressions."""
from contextlib import redirect_stdout
from copy import deepcopy
import csv
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from attodry_control.unified_plotting import (
    UnifiedPlotDashboard, _response_column, _sample_token, column_label,
    export_plot_bundle, render_plot,
)


POWER = "requested.optical_target_power_w"
SIGNAL = "measured.lockin_xx_h1_x_v"
DELTA = "measured.lockin_xx_h1_power_delta_x_v"


def light_row(power=1e-6, index=0, sample=0):
    row = {"source_path": "archive.sqlite", "run_id": "light", "condition_id": str(index),
           "attempt_index": 0, "sample_index": sample, "repeat_index": 0,
           "accepted": True, "observation_kind": "formal", POWER: power,
           "measured.optical_power_w": power, "axes.optical.index": index,
           "status.lockin": {"samples": []}}
    for harmonic in (1, 2):
        status = {"harmonic": harmonic, "settings_verified": True,
                  "selected_roles": ["xx", "xy", "aux"]}
        for role in ("xx", "xy", "aux"):
            status["lockin_" + role] = {"lia_status": {"raw": 0}, "error_status": 0,
                                        "reading": {"harmonic": harmonic}}
            stem = f"measured.lockin_{role}_h{harmonic}_"
            row.update({stem + "x_v": float(index + 1), stem + "y_v": .5,
                        stem + "amplitude_v": float(index + 1), stem + "phase_deg": 0.})
        row["status.lockin"]["samples"].append(status)
    return row


def power_result(rows, *, mode, reference_power_w=None):
    """Only emulate the engine's public row contract; numeric engine tests are separate."""
    output = []
    for item in rows:
        row = deepcopy(item)
        channels = {}
        for role in ("xx", "xy", "aux"):
            for harmonic in (1, 2):
                stem = f"measured.lockin_{role}_h{harmonic}_"
                for field in ("x_v", "y_v", "amplitude_v", "phase_deg"):
                    row[stem + "power_delta_" + field] = row[stem + field] - 1.
                channels[f"{role}:h{harmonic}"] = {"valid_for_analysis": True, "phase_defined": True,
                    "reference_sample_ids": ["reference-sample"], "reference_count": 1,
                    "reference_power_w": reference_power_w if reference_power_w is not None else 1e-6}
        row["status.lockin"]["power_reference"] = {"mode": mode, "channels": channels}
        output.append(row)
    return tuple(output)


class PowerReferencePlotTests(unittest.TestCase):
    def setUp(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        self.addCleanup(lambda: plt.close("all"))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)

    def test_full_run_reference_candidates_precede_filters_and_manual_exclusions(self):
        rows = [light_row(), light_row(6e-6, 1),
                {**light_row(), "observation_kind": "dark"}]
        before = deepcopy(rows)
        spec = {"x": POWER, "y": SIGNAL, "photoresponse": "power_lowest",
                "filters": {POWER: [6e-6]}, "excluded_sample_ids": [_sample_token(rows[0])]}
        with patch("attodry_control.optical_power_reference.subtract_power_reference", side_effect=power_result) as subtract:
            figure, data = render_plot(rows, spec)
        candidates = subtract.call_args.args[0]
        self.assertEqual(len(candidates), 2)
        self.assertTrue(candidates[0]["_manual_excluded"])
        self.assertFalse(candidates[1]["_manual_excluded"])
        self.assertEqual(data["rows"][0][DELTA], 1.)
        self.assertEqual(data["report"]["photoresponse"], "power_lowest")
        self.assertIn("lowest planned-power", figure.axes[0].get_title())
        self.assertNotIn("dark", data["report"]["uncertainty"])
        self.assertEqual(rows, before)
        self.assertEqual(spec["y"], SIGNAL)

    def test_selected_reference_dispatch_uses_watts_and_separate_delta_columns(self):
        spec = {"x": POWER, "y": SIGNAL, "photoresponse": "power_selected", "reference_power_w": 6e-6}
        with patch("attodry_control.optical_power_reference.subtract_power_reference", side_effect=power_result) as subtract:
            figure, data = render_plot([light_row(), light_row(6e-6, 1)], spec)
        self.assertEqual(subtract.call_args.kwargs, {"mode": "power_selected", "reference_power_w": 6e-6})
        self.assertIn(DELTA, data["rows"][0])
        self.assertNotIn(SIGNAL.replace("_x_v", "_delta_x_v"), data["rows"][0])
        self.assertIn("6 μW", figure.axes[0].get_title())
        self.assertIn("Power-reference ΔX", figure.axes[0].get_ylabel())

    def test_field_and_three_channels_accept_all_power_components(self):
        for mode in ("field", "three_channels"):
            for component in ("x_v", "y_v", "amplitude_v", "phase_deg"):
                with self.subTest(mode=mode, component=component), patch(
                        "attodry_control.optical_power_reference.subtract_power_reference", side_effect=power_result):
                    _, data = render_plot([light_row(), light_row(6e-6, 1)],
                        {"mode": mode, "x": POWER, "field_component": component,
                         "photoresponse": "power_lowest", "statistics": "raw"})
                    keys = data["report"]["channels"]
                    self.assertEqual(len(keys), 3 if mode == "three_channels" else 4)
                    self.assertTrue(all(key.endswith("_power_delta_" + component) for key in keys))
                    self.assertTrue(all("dark" not in panel["uncertainty"] for panel in data["report"]["channels"].values()))

    def test_xy_z_transforms_signal_axes_and_preserves_response_report(self):
        with patch("attodry_control.optical_power_reference.subtract_power_reference", side_effect=power_result):
            figure, data = render_plot([light_row(), light_row(6e-6, 1)],
                {"mode": "xy_z", "x": POWER, "y": SIGNAL,
                 "z": SIGNAL.replace("x_v", "y_v"), "photoresponse": "power_lowest"})
        self.assertEqual(data["report"]["photoresponse"], "power_lowest")
        self.assertIn("Power-reference ΔX", figure.axes[0].get_ylabel())
        self.assertIn("Power-reference ΔY", figure.axes[1].get_ylabel())
        self.assertIn("lowest planned-power", figure.axes[0].get_title())

    def test_response_switch_replaces_existing_delta_prefix_once(self):
        raw = SIGNAL
        for prefix in ("", "delta_", "power_delta_"):
            candidate = raw.replace("h1_", "h1_" + prefix)
            self.assertEqual(_response_column(candidate, "power_delta_"), DELTA)
            self.assertEqual(_response_column(candidate, "delta_"), raw.replace("h1_", "h1_delta_"))
        self.assertIn("Power-reference ΔR", column_label(DELTA.replace("x_v", "amplitude_v")))

    def test_power_response_rejects_dark_axes_without_calling_engine(self):
        with patch("attodry_control.optical_power_reference.subtract_power_reference") as subtract:
            with self.assertRaisesRegex(ValueError, "Dark axes"):
                render_plot([{**light_row(), "observation_kind": "dark"}],
                    {"x": POWER, "y": "dark.lockin_xx_h1_x_v", "photoresponse": "power_lowest"})
            subtract.assert_not_called()

    def test_export_keeps_power_evidence_raw_values_and_manual_audit_rows(self):
        rows = [light_row(), light_row(6e-6, 1)]
        spec = {"x": POWER, "y": SIGNAL, "photoresponse": "power_selected", "reference_power_w": 1e-6,
                "excluded_sample_ids": [_sample_token(rows[0])]}
        with patch("attodry_control.optical_power_reference.subtract_power_reference", side_effect=power_result):
            figure, data = render_plot(rows, spec)
        with patch("attodry_control.unified_plotting.export_publication_figure_set", return_value=[]):
            folder = export_plot_bundle(self.directory, [{"figure": figure, "data": data, "spec": spec}])
        manifest = json.loads((folder / "plot_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["selected_sample_rows"], 1)
        self.assertEqual(manifest["analysis_audit_sample_rows"], 2)
        self.assertEqual(len(manifest["power_reference_source_sha256"]), 64)
        self.assertEqual(manifest["plots"][0]["spec"]["reference_power_w"], 1e-6)
        with (folder / "selected_samples.csv").open(encoding="utf-8", newline="") as stream:
            selected, = csv.DictReader(stream)
        self.assertEqual(float(selected[SIGNAL]), 2.)
        self.assertEqual(float(selected[DELTA]), 1.)
        channel = json.loads(selected["status.lockin"])["power_reference"]["channels"]["xx:h1"]
        self.assertEqual(channel["reference_sample_ids"], ["reference-sample"])
        with (folder / "analysis_audit_samples.csv").open(encoding="utf-8", newline="") as stream:
            audit = list(csv.DictReader(stream))
        self.assertEqual(audit[0]["_manual_excluded"], "True")

    def test_widget_reference_units_visibility_and_saved_setup_roundtrip(self):
        source = self.directory / "data.csv"
        source.write_text("U,V\n1,2\n2,4\n", encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            dashboard = UnifiedPlotDashboard(self.directory)
            dashboard.add_plot("curve", {"source_paths": [str(source)], "x": "csv.U", "y": "csv.V",
                                         "photoresponse": "power_selected", "reference_power_w": 6e-6})
            card = dashboard.cards[0]
            self.assertEqual(card["reference_power"].value, 6.)
            self.assertEqual(card["reference_power"].layout.display, "")
            self.assertEqual(card["dark_run"].layout.display, "none")
            card["reference_power"].value = 15.
            self.assertAlmostEqual(dashboard._card_spec(card)["reference_power_w"], 15e-6)
            dashboard.configuration_path.value = str(self.directory / "saved.json")
            dashboard._save_setup()
            dashboard._load_setup()
            restored = dashboard.cards[0]
            self.assertEqual(restored["photoresponse"].value, "power_selected")
            self.assertAlmostEqual(restored["reference_power"].value, 15.)
            restored["photoresponse"].value = "power_lowest"
            self.assertEqual(restored["reference_power"].layout.display, "none")
            restored["photoresponse"].value = "separate"
            self.assertEqual(restored["dark_run"].layout.display, "")

    def test_zero_selected_power_survives_setup(self):
        with redirect_stdout(io.StringIO()):
            dashboard = UnifiedPlotDashboard(self.directory)
            dashboard.add_plot("curve", {"photoresponse": "power_selected", "reference_power_w": 0.})
        self.assertEqual(dashboard.cards[0]["reference_power"].value, 0.)

    @staticmethod
    def real_rows():
        from tests.test_optical_power_reference import make_rows
        rows = make_rows()
        for row in rows:
            row["status.optical"].update(power_target_w=row[POWER], feedback_actuator="cld_current")
        return rows

    def test_real_engine_mean_sem_render_and_export_keep_shared_error(self):
        rows = self.real_rows()
        spec = {"x": POWER, "y": SIGNAL, "photoresponse": "power_lowest", "statistics": "mean_sem"}
        figure, data = render_plot(rows, spec)
        points = data["summary_rows"]
        self.assertEqual([point["n"] for point in points], [2, 2])
        self.assertAlmostEqual(points[0]["y"], 0.)
        self.assertAlmostEqual(points[0]["sem"], 0.)
        self.assertAlmostEqual(points[1]["y"], .01)
        self.assertAlmostEqual(points[1]["sem"], math.sqrt(2e-6))
        self.assertIn("shared power-reference", figure.axes[0].get_title())
        with patch("attodry_control.unified_plotting.export_publication_figure_set", return_value=[]):
            folder = export_plot_bundle(self.directory, [{"figure": figure, "data": data, "spec": spec}])
        with (folder / "plotted_statistics.csv").open(encoding="utf-8", newline="") as stream:
            exported = list(csv.DictReader(stream))
        self.assertAlmostEqual(float(exported[1]["sem"]), math.sqrt(2e-6))
        self.assertEqual(len(json.loads(exported[1]["sample_ids"])), 2)

    def test_real_engine_complex_mean_phase_includes_zero_vectors_and_exclusions(self):
        rows = self.real_rows()
        for row, x, y in ((rows[2], .011, 0.), (rows[3], .015, .004)):
            stem = "measured.lockin_xx_h1_"
            row.update({stem + "x_v": x, stem + "y_v": y,
                        stem + "amplitude_v": math.hypot(x, y),
                        stem + "phase_deg": math.degrees(math.atan2(y, x))})
        spec = {"x": POWER, "y": SIGNAL.replace("x_v", "phase_deg"),
                "photoresponse": "power_lowest", "statistics": "mean_sem"}
        _, data = render_plot(rows, spec)
        reference, target = data["summary_rows"]
        self.assertIsNone(reference["y"])
        self.assertEqual(target["n"], 2)
        self.assertAlmostEqual(target["y"], 45.)
        _, excluded = render_plot(rows, {**spec, "excluded_sample_ids": [_sample_token(rows[3])]})
        self.assertEqual(excluded["summary_rows"][1]["n"], 1)
        self.assertIsNone(excluded["summary_rows"][1]["y"])

    def test_real_engine_missing_reference_is_exported_as_invalid_audit(self):
        rows = self.real_rows()
        spec = {"x": POWER, "y": SIGNAL, "photoresponse": "power_lowest", "statistics": "mean_sem",
                "excluded_sample_ids": [_sample_token(row) for row in rows[:2]]}
        figure, data = render_plot(rows, spec)
        self.assertFalse(data["rows"])
        self.assertEqual(data["report"]["excluded_quality_count"], 2)
        self.assertEqual(len(data["audit_rows"]), 4)
        channel = data["audit_rows"][-1]["status.lockin"]["power_reference"]["channels"]["xx:h1"]
        self.assertFalse(channel["valid_for_analysis"])
        self.assertEqual(channel["reference_power_w"], 1e-6)
        self.assertIn("No qualified reference", channel["reason"])
        with patch("attodry_control.unified_plotting.export_publication_figure_set", return_value=[]):
            folder = export_plot_bundle(self.directory, [{"figure": figure, "data": data, "spec": spec}])
        with (folder / "analysis_audit_samples.csv").open(encoding="utf-8", newline="") as stream:
            self.assertEqual(len(list(csv.DictReader(stream))), 4)


if __name__ == "__main__":
    unittest.main()
