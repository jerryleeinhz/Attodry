import ast
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from attodry_control.commissioning_analysis import (
    HarmonicScalingPoint,
    ScalarHarmonicScalingModel,
)
from attodry_control.report_plotting import (
    condensed_iv_report_manifest,
    plot_condensed_iv_report,
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
        self.assertIn("plot_condensed_iv_report", code)
        self.assertIn("REPORT_AVAILABLE_CHANNELS", code)
        self.assertIn("REPORT_AMPLITUDE_CHANNELS", code)
        self.assertIn('REPORT_PHASE_MODE = "none"', code)
        self.assertIn('# REPORT_PHASE_MODE = "right"', code)
        self.assertIn("REPORT_OUTPUT_STEM = None", code)
        self.assertIn("condensed_iv_report_manifest", code)

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
