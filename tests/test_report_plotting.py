import ast
import json
import math
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from attodry_control.commissioning_analysis import (
    CommissioningSample,
    ExcitationPathResistance,
    HarmonicScalingPoint,
    ScalarHarmonicScalingModel,
    fit_harmonic_scaling,
    plot_role_harmonic_sweep,
)
from attodry_control.report_plotting import (
    condensed_iv_report_manifest,
    plot_condensed_iv_report,
    experiment_fit_report_manifest,
    plot_experiment_fit_report,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = PROJECT_ROOT / "notebooks" / "sr830_commissioning_sweeps.ipynb"


class ReportPlottingTests(unittest.TestCase):
    def test_amplitude_only_draws_one_final_scalar_curve_per_channel(self) -> None:
        try:
            import matplotlib.pyplot as plt
        except ImportError as exc:
            self.skipTest(f"matplotlib unavailable: {exc}")
        fit = self._fit("xx", 1)

        figure = plot_condensed_iv_report(
            {("xx", 1): fit},
            amplitude_channels=(("xx", 1),),
            phase_mode="none",
        )
        self.addCleanup(plt.close, figure)

        self.assertEqual(len(figure.axes), 1)
        self.assertEqual(figure.axes[0].get_xscale(), "log")
        self.assertEqual(figure.axes[0].get_yscale(), "log")
        legend_labels = [
            text.get_text() for text in figure.axes[0].get_legend().get_texts()
        ]
        self.assertEqual(len(legend_labels), 1)
        self.assertIn("Vxx h1", legend_labels[0])
        self.assertIn("p=1.2", legend_labels[0])
        self.assertIn("R(I) =", legend_labels[0])
        fit_line = next(
            line
            for line in figure.axes[0].lines
            if line.get_label().startswith("Vxx h1")
        )
        self.assertAlmostEqual(min(fit_line.get_xdata()), 1e-6)
        self.assertAlmostEqual(max(fit_line.get_xdata()), 4e-6)

    def test_optional_phase_uses_a_right_axis_and_combined_legend(self) -> None:
        try:
            import matplotlib.pyplot as plt
        except ImportError as exc:
            self.skipTest(f"matplotlib unavailable: {exc}")
        fits = {
            ("xx", 1): self._fit("xx", 1),
            ("xy", 2): self._fit("xy", 2),
        }

        figure = plot_condensed_iv_report(
            fits,
            amplitude_channels=(("xx", 1), ("xy", 2)),
            phase_channels=(("xy", 2),),
            phase_mode="right",
        )
        self.addCleanup(plt.close, figure)

        self.assertEqual(len(figure.axes), 2)
        self.assertEqual(figure.axes[1].get_ylabel(), "Unwrapped phase (degree)")
        legend_labels = [
            text.get_text() for text in figure.axes[0].get_legend().get_texts()
        ]
        self.assertEqual(len(legend_labels), 3)
        self.assertEqual(legend_labels[-1], "Vxy h2 phase")
        self.assertEqual(
            list(figure.axes[1].lines[0].get_ydata()),
            [179.0, 181.0, 185.0],
        )

    def test_manifest_records_selected_final_models_and_sources(self) -> None:
        fit = self._fit("xy", 3)

        manifest = condensed_iv_report_manifest(
            {("xy", 3): fit},
            amplitude_channels=(("xy", 3),),
            phase_channels=(("xy", 3),),
            phase_mode="right",
            source_paths=(Path("completed.json"),),
        )

        self.assertEqual(manifest["scalar_curve"], "scalar_selected_free_model")
        self.assertEqual(manifest["source_files"], ["completed.json"])
        self.assertEqual(manifest["models"]["xy_h3"]["exponent"], 1.2)
        self.assertEqual(
            manifest["phase_points"]["xy_h3"]["qualified_point_count"], 3
        )

    def test_phase_selection_requires_the_explicit_right_axis(self) -> None:
        fit = self._fit("xx", 1)
        with self.assertRaisesRegex(ValueError, "phase_channels must be empty"):
            plot_condensed_iv_report(
                {("xx", 1): fit},
                amplitude_channels=(("xx", 1),),
                phase_channels=(("xx", 1),),
                phase_mode="none",
            )

    def test_notebook_has_an_independent_configurable_report_cell(self) -> None:
        document = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
        code = "\n\n".join(
            "".join(cell.get("source", ()))
            for cell in document["cells"]
            if cell["cell_type"] == "code"
        )

        ast.parse(code)
        self.assertIn("plot_experiment_fit_report", code)
        self.assertIn("REPORT_AVAILABLE_CHANNELS", code)
        self.assertIn("REPORT_AMPLITUDE_CHANNELS", code)
        self.assertIn("REPORT_FIT_METHODS = SCALING_PLOT_METHODS", code)
        self.assertIn("harmonic_scaling_rows_snapshot", code)
        self.assertIn("REPORT_OUTPUT_STEM = None", code)
        self.assertIn("experiment_fit_report_manifest", code)

    def test_overlay_preserves_all_experimental_points_and_phase_rules(self) -> None:
        import matplotlib.pyplot as plt
        rows = self._rows()
        path = ExcitationPathResistance(100_000.0, 50.0, 100.0)
        original = plot_role_harmonic_sweep(rows, role="xy", harmonic=2,
                                           excitation_path=path, phase_minimum_amplitude_v=2e-5)
        report = plot_experiment_fit_report(rows, role="xy", harmonic=2,
                                           excitation_path=path, phase_minimum_amplitude_v=2e-5)
        self.addCleanup(plt.close, original)
        self.addCleanup(plt.close, report)
        import numpy as np
        for before, after in zip(original.axes, report.axes):
            for old_line, new_line in zip(before.lines, after.lines):
                np.testing.assert_allclose(np.asarray(old_line.get_xdata(), dtype=float),
                                           np.asarray(new_line.get_xdata(), dtype=float))
                np.testing.assert_allclose(np.asarray(old_line.get_ydata(), dtype=float),
                                           np.asarray(new_line.get_ydata(), dtype=float), equal_nan=True)
            self.assertEqual(before.get_xscale(), after.get_xscale())
            self.assertEqual(before.get_yscale(), after.get_yscale())
            self.assertTrue(all(text.get_text().startswith("experiment")
                                for text in after.get_legend().get_texts()))
        self.assertEqual(len(report.axes[0].containers[0].lines[0].get_xdata()), 8)
        self.assertIn("No available fit", report.axes[0].texts[0].get_text())

    def test_every_available_method_draws_fixed_and_free_curves_without_refitting(self) -> None:
        import matplotlib.pyplot as plt
        rows = self._rows()
        path = ExcitationPathResistance(100_000.0, 50.0, 100.0)
        fit = fit_harmonic_scaling(rows, role="xy", harmonic=2, excitation_path=path)
        before = fit.as_dict()
        report = plot_experiment_fit_report(rows, role="xy", harmonic=2, fit=fit, excitation_path=path)
        self.addCleanup(plt.close, report)
        fitting = [line for line in report.axes[0].lines if line.get_label().startswith("fitting")]
        self.assertEqual(len(fitting), 6)
        for method in ("log", "scalar", "complex"):
            self.assertEqual(sum(f"· {method} ·" in line.get_label() for line in fitting), 2)
        included = [point for point in fit.points if point.included]
        self.assertAlmostEqual(min(fitting[0].get_xdata()), min(point.current_a_rms for point in included))
        self.assertAlmostEqual(max(fitting[0].get_xdata()), max(point.current_a_rms for point in included))
        self.assertEqual(fit.as_dict(), before)
        manifest = experiment_fit_report_manifest(rows, role="xy", harmonic=2, fit=fit)
        self.assertEqual(len(manifest["curves"]), 6)
        self.assertEqual(manifest["fit"], before)

    def test_voltage_coordinates_and_predictions_use_the_frozen_excitation_path(self) -> None:
        import matplotlib.pyplot as plt
        rows = self._rows()
        path = ExcitationPathResistance(100_000.0, 50.0, 100.0)
        fit = fit_harmonic_scaling(rows, role="xy", harmonic=2, excitation_path=path)
        report = plot_experiment_fit_report(rows, role="xy", harmonic=2, fit=fit,
                                           methods=("scalar",), excitation_path=path,
                                           excitation_x_axis="sine_output_v_rms")
        self.addCleanup(plt.close, report)
        lines = [line for line in report.axes[0].lines if line.get_label().startswith("fitting")]
        self.assertEqual(len(lines), 2)
        model = next(model for model in fit.scalar_models if model.name == fit.scalar_selected_free_model)
        for voltage, prediction in zip(lines[1].get_xdata(), lines[1].get_ydata()):
            current = voltage / path.total_resistance_ohm
            expected = model.background_v + model.response_v_at_reference_current * (current / model.current_reference_a_rms) ** model.exponent
            self.assertAlmostEqual(prediction, expected)
        self.assertAlmostEqual(max(lines[1].get_xdata()), max(row.sine_output_v_rms for row in rows))
        with self.assertRaisesRegex(ValueError, "fit's excitation path"):
            plot_experiment_fit_report(rows, role="xy", harmonic=2, fit=fit, methods=("scalar",),
                                       excitation_x_axis="sine_output_v_rms")

    def test_missing_scalar_models_keep_zero_and_low_signal_experiments(self) -> None:
        import matplotlib.pyplot as plt
        rows = self._rows()
        fit = fit_harmonic_scaling(rows, role="xy", harmonic=2)
        fit = replace(fit, scalar_models=(), scalar_selected_model=None, scalar_selected_free_model=None)
        rows = tuple(replace(row, x_v=0.0, y_v=0.0, amplitude_v=0.0)
                     if row.point_index == 0 else row for row in rows)
        report = plot_experiment_fit_report(rows, role="xy", harmonic=2, fit=fit, methods=("scalar",))
        self.addCleanup(plt.close, report)
        self.assertEqual(min(report.axes[0].containers[0].lines[0].get_ydata()), 0.0)
        self.assertFalse(any(line.get_label().startswith("fitting") for line in report.axes[0].lines))
        self.assertEqual(report.axes[0].get_yscale(), "linear")

    def test_frequency_report_has_experiment_only_and_rejects_wrong_or_pooled_fit(self) -> None:
        import matplotlib.pyplot as plt
        rows = self._rows()
        frequency = tuple(replace(row, scan_type="frequency", actual_frequency_hz=100.0 * (row.point_index + 1)) for row in rows)
        report = plot_experiment_fit_report(frequency, role="xy", harmonic=2)
        self.addCleanup(plt.close, report)
        self.assertEqual(report.axes[0].get_xscale(), "log")
        self.assertEqual(len(report.axes[0].get_legend().get_texts()), 1)
        fit = fit_harmonic_scaling(rows, role="xy", harmonic=2)
        with self.assertRaisesRegex(ValueError, "match the excitation report channel"):
            plot_experiment_fit_report(frequency, role="xy", harmonic=2, fit=fit)
        with self.assertRaisesRegex(ValueError, "match the excitation report channel"):
            plot_experiment_fit_report(rows, role="xy", harmonic=1, fit=fit)
        with self.assertRaisesRegex(ValueError, "one run at a time"):
            plot_experiment_fit_report((*rows, replace(rows[0], source_path="other.json")), role="xy", harmonic=2)

    def test_notebook_report_reuses_loaded_rows_and_keeps_runs_separate(self) -> None:
        import matplotlib.pyplot as plt
        first = self._rows()
        second = tuple(replace(row, source_path="other.json", x_v=row.x_v * 2,
                               y_v=row.y_v * 2, amplitude_v=row.amplitude_v * 2) for row in first)
        frequency = tuple(replace(row, source_path="frequency.json", scan_type="frequency",
                                  actual_frequency_hz=100.0 * (row.point_index + 1)) for row in first)
        rows = (*first, *second)
        fit = fit_harmonic_scaling(first, role="xy", harmonic=2)
        env = dict(Path=Path, json=json, plt=plt, display=lambda *args: None,
                   frequency_rows=frequency, frequency_excitation_path=None,
                   excitation_rows=rows, excitation_excitation_path=None,
                   harmonic_scaling_rows_snapshot=rows,
                   harmonic_scaling_results_by_run={first[0].source_path: {("xy", 2): fit}},
                   harmonic_scaling_excitation_paths_by_run={}, SCALING_PLOT_METHODS=("scalar",),
                   excitation_x_axis_widget=SimpleNamespace(value="sine_output_current_a_rms"),
                   PHASE_MINIMUM_AMPLITUDE_V=0.0, PHASE_MAXIMUM_STANDARD_DEVIATION_DEG=None)
        document = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
        code = next("".join(cell["source"]) for cell in document["cells"]
                    if cell["cell_type"] == "code" and "REPORT_AMPLITUDE_CHANNELS =" in "".join(cell["source"]))
        exec(compile(code, "report-cell", "exec"), env)
        self.assertEqual(len(env["report_figures"]), 3)
        first_figure = env["report_figures"][(first[0].source_path, "xy", 2)]
        other_figure = env["report_figures"][("other.json", "xy", 2)]
        self.assertTrue(any(line.get_label().startswith("fitting") for line in first_figure.axes[0].lines))
        self.assertFalse(any(line.get_label().startswith("fitting") for line in other_figure.axes[0].lines))
        self.assertEqual(env["report_manifests"][("other.json", "xy", 2)]["experimental_sample_count"], len(second))
        env["excitation_rows"] = first
        with self.assertRaisesRegex(RuntimeError, "selection changed"):
            exec(compile(code, "report-cell", "exec"), env)

    def test_notebook_retains_six_channel_merged_report_alongside_channel_reports(self) -> None:
        import matplotlib.pyplot as plt
        channels = tuple((role, harmonic) for role in ("xx", "xy") for harmonic in (1, 2, 3))
        rows = tuple(replace(row, role=role, harmonic=harmonic,
                             x_v=row.x_v * (index + 1), y_v=row.y_v * (index + 1),
                             amplitude_v=row.amplitude_v * (index + 1))
                     for index, (role, harmonic) in enumerate(channels) for row in self._rows())
        fits = {key: fit_harmonic_scaling(rows, role=key[0], harmonic=key[1]) for key in channels}
        missing_rows = tuple(replace(row, source_path="missing_scalar.json") for row in self._rows())
        loaded = (*rows, *missing_rows)
        env = dict(Path=Path, json=json, plt=plt, display=lambda *args: None,
                   frequency_rows=(), frequency_excitation_path=None,
                   excitation_rows=loaded, excitation_excitation_path=None,
                   harmonic_scaling_rows_snapshot=loaded,
                   harmonic_scaling_results_by_run={rows[0].source_path: fits},
                   harmonic_scaling_excitation_paths_by_run={}, SCALING_PLOT_METHODS=("scalar",),
                   excitation_x_axis_widget=SimpleNamespace(value="sine_output_current_a_rms"),
                   PHASE_MINIMUM_AMPLITUDE_V=0.0, PHASE_MAXIMUM_STANDARD_DEVIATION_DEG=None)
        document = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
        code = next("".join(cell["source"]) for cell in document["cells"]
                    if cell["cell_type"] == "code" and "REPORT_AMPLITUDE_CHANNELS =" in "".join(cell["source"]))
        exec(compile(code, "retained-merged-report", "exec"), env)
        self.assertEqual(len(env["report_figures"]), 7)
        self.assertEqual(set(env["merged_report_figures"]), {rows[0].source_path})
        merged = env["merged_report_figures"][rows[0].source_path]
        self.assertEqual(len(merged.axes), 1)
        self.assertEqual(len(merged.axes[0].containers), 6)
        self.assertEqual(merged.axes[0].get_xscale(), "log")
        self.assertEqual(merged.axes[0].get_yscale(), "log")
        labels = [text.get_text() for text in merged.axes[0].get_legend().get_texts()]
        self.assertEqual(len(labels), 6)
        for role, harmonic in channels:
            self.assertTrue(any(f"V{role} h{harmonic}" in label for label in labels))
        manifest = env["merged_report_manifests"][rows[0].source_path]
        self.assertEqual(manifest["amplitude_channels"], [list(channel) for channel in channels])
        self.assertEqual(manifest["source_files"], [rows[0].source_path])
        self.assertEqual(manifest["scalar_curve"], "scalar_selected_free_model")
        self.assertEqual(env["merged_report_omissions"]["missing_scalar.json"], [["xy", 2]])
        self.assertIn(("missing_scalar.json", "xy", 2), env["report_figures"])
        self.assertFalse(plt.fignum_exists(merged.number))
        # Preserve the old explicit optional right-phase-axis controls.
        phase_code = code.replace('REPORT_PHASE_MODE = "none"', 'REPORT_PHASE_MODE = "right"')
        phase_code = phase_code.replace('REPORT_PHASE_CHANNELS = ()', 'REPORT_PHASE_CHANNELS = (("xy", 2),)')
        exec(compile(phase_code, "retained-merged-report-phase", "exec"), env)
        self.assertEqual(len(env["merged_report_figures"][rows[0].source_path].axes), 2)
        self.assertEqual(env["merged_report_manifests"][rows[0].source_path]["phase_mode"], "right")

    @staticmethod
    def _rows():
        rows = []
        for index in range(8):
            voltage = 0.004 * 10 ** (3 * index / 7)
            x, y = 2e-6 + 0.01 * voltage ** 2, -1e-6 + 0.003 * voltage ** 2
            for sample, noise in enumerate((-1e-8, 0.0, 1e-8)):
                xx, yy = x + noise, y - noise
                rows.append(CommissioningSample(
                    source_path="completed.json", record_status="completed", scan_type="excitation",
                    point_index=index, sample_index=sample, target_frequency_hz=5000.0,
                    actual_frequency_hz=5000.0, source_v_rms=voltage, sine_output_v_rms=voltage,
                    nominal_current_a_rms=None, recorded_external_series_resistance_ohm=100_000.0,
                    recorded_sr830_output_resistance_ohm=50.0, recorded_approximate_device_resistance_ohm=100.0,
                    role="xy", harmonic=2, x_v=xx, y_v=yy, amplitude_v=math.hypot(xx, yy),
                    phase_deg=math.degrees(math.atan2(yy, xx)), reference_frequency_hz=5000.0,
                    locked=True, overload=False, lia_status_raw=0, error_status=0,
                    statuses=("clean",), problems=(),
                ))
        return tuple(rows)

    @staticmethod
    def _fit(role: str, harmonic: int):
        model = ScalarHarmonicScalingModel(
            name="scalar_offset_free_order",
            includes_background=True,
            free_exponent=True,
            phase_ignored=True,
            current_reference_a_rms=2e-6,
            exponent=1.2,
            exponent_standard_error=0.05,
            exponent_ci_low=1.1,
            exponent_ci_high=1.3,
            background_v=2e-6,
            response_v_at_reference_current=8e-6,
            r_squared=0.998,
            relative_rmse=0.025,
            weighted_residual_sum_squares=0.1,
            aicc=4.2,
        )
        points = tuple(
            HarmonicScalingPoint(
                current_a_rms=current,
                x_v=amplitude,
                x_standard_error_v=0.1e-6,
                y_v=0.0,
                y_standard_error_v=0.1e-6,
                amplitude_v=amplitude,
                amplitude_standard_deviation_v=0.2e-6,
                amplitude_standard_error_v=0.1e-6,
                phase_deg=phase,
                phase_standard_deviation_deg=1.0,
                count=4,
                snr=20.0,
                included=True,
                complex_included=True,
            )
            for current, amplitude, phase in (
                (1e-6, 5e-6, 179.0),
                (2e-6, 10e-6, -179.0),
                (4e-6, 20e-6, -175.0),
            )
        )
        return SimpleNamespace(
            role=role,
            harmonic=harmonic,
            points=points,
            scalar_models=(model,),
            scalar_selected_free_model=model.name,
            scalar_excluded_point_count=0,
        )


if __name__ == "__main__":
    unittest.main()
