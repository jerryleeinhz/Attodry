from __future__ import annotations

from dataclasses import replace
import json
import math
from pathlib import Path
import types
import tempfile
import unittest

from attodry_control.commissioning_analysis import CommissioningSample
from attodry_control.frequency_calibration import (
    correct_h2,
    calibrate_h1_samples,
    plot_h1_calibration,
    plot_h2_correction,
    plot_h2_voltage_coefficient,
    estimate_frequency_response,
    lcr_anchored_impedance,
)


def row(frequency: float, voltage: float, signal: complex, *, point: int, role: str = "xx", harmonic: int = 1, scan: str = "frequency_excitation") -> CommissioningSample:
    return CommissioningSample(
        source_path="one.json", record_status="completed", scan_type=scan,
        point_index=point, sample_index=0, target_frequency_hz=frequency,
        actual_frequency_hz=frequency, source_v_rms=voltage,
        sine_output_v_rms=voltage, nominal_current_a_rms=None,
        recorded_external_series_resistance_ohm=None,
        recorded_sr830_output_resistance_ohm=None,
        recorded_approximate_device_resistance_ohm=None,
        role=role, harmonic=harmonic, x_v=signal.real, y_v=signal.imag,
        amplitude_v=abs(signal), phase_deg=math.degrees(math.atan2(signal.imag, signal.real)),
        reference_frequency_hz=harmonic * frequency, locked=True, overload=False,
        lia_status_raw=0, error_status=0, statuses=("clean",), problems=(),
        phase_shift_deg=10.0,
        source_readback_confirmed=True,
    )


class FrequencyCalibrationTests(unittest.TestCase):
    def combined_rows(self) -> tuple[CommissioningSample, ...]:
        rows = []
        for frequency, slope, intercept in (
            (10.0, 2 + 1j, 0.3 - 0.2j),
            (20.0, 4 + 2j, -0.1 + 0.4j),
            (40.0, 6 + 3j, 0.2 + 0.1j),
        ):
            for index, voltage in enumerate((0.1, 0.2, 0.4)):
                rows.append(row(frequency, voltage, slope * voltage + intercept, point=len(rows)))
        return tuple(rows)

    def test_complex_intercept_and_relative_response(self) -> None:
        result = estimate_frequency_response(self.combined_rows(), role="xx")
        self.assertEqual(result.method, "frequency_excitation")
        self.assertAlmostEqual(result.points[0].slope_v_per_v.real, 2)
        self.assertAlmostEqual(result.points[0].slope_v_per_v.imag, 1)
        self.assertAlmostEqual(result.points[0].intercept_v.real, 0.3)
        self.assertAlmostEqual(result.points[0].intercept_v.imag, -0.2)
        self.assertAlmostEqual(result.points[0].residual_rms_v, 0, places=12)
        q, interpolated = result.relative_at(20)
        self.assertAlmostEqual(q.real, 2)
        self.assertAlmostEqual(q.imag, 0)
        self.assertFalse(interpolated)

    def test_frequency_sweep_uses_ratio_and_marks_intercept_unidentifiable(self) -> None:
        rows = (
            row(10, 0.1, 0.2 + 0.1j, point=0, scan="frequency"),
            row(20, 0.1, 0.4 + 0.2j, point=1, scan="frequency"),
        )
        result = estimate_frequency_response(rows)
        self.assertIsNone(result.points[0].intercept_v)
        self.assertAlmostEqual(result.relative_at(20)[0].real, 2)

    def test_h1_fitted_and_corrected_samples_remove_complex_intercept(self) -> None:
        rows = self.combined_rows()
        response = estimate_frequency_response(rows)
        calibrated = calibrate_h1_samples(rows, response)
        for source, result in zip(rows, calibrated):
            self.assertAlmostEqual(abs(result.fitted_v - complex(source.x_v, source.y_v)), 0)
            self.assertAlmostEqual(abs(result.corrected_v / source.sine_output_v_rms - response.reference_slope), 0)
            self.assertEqual(result.point_index, source.point_index)
            self.assertEqual(result.sample_index, source.sample_index)
        changed = (*rows[:-1], replace(rows[-1], x_v=rows[-1].x_v + 0.01))
        with self.assertRaisesRegex(ValueError, "rebuild"):
            calibrate_h1_samples(changed, response)

    def test_frequency_scatter_preserves_repeats_instead_of_zero_mean_residual(self) -> None:
        rows = (
            row(10, 0.1, 0.19+0.1j, point=0, scan="frequency"),
            replace(row(10, 0.1, 0.21+0.1j, point=0, scan="frequency"), sample_index=1),
            row(20, 0.1, 0.4+0.2j, point=1, scan="frequency"),
        )
        response = estimate_frequency_response(rows)
        self.assertAlmostEqual(response.points[0].residual_rms_v, 0.01)
        result = calibrate_h1_samples(rows, response)
        self.assertEqual(len(result), 3)
        self.assertIsNone(result[0].intercept_v)
        self.assertNotEqual(result[0].corrected_v, result[1].corrected_v)
        self.assertAlmostEqual(abs(result[-1].corrected_v - (0.2+0.1j)), 0)

    def test_plot_preserves_large_correction_zero_and_uncorrected_raw(self) -> None:
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError:
            self.skipTest("matplotlib unavailable")
        h1 = (
            row(10, 0.1, 0.0001+0j, point=0, scan="frequency"),
            row(20, 0.1, 0.1+0j, point=1, scan="frequency"),
        )
        response = estimate_frequency_response(h1)
        h2 = (
            row(10, 0.1, 0j, point=0, harmonic=2, role="xy", scan="frequency"),
            row(20, 0.1, 0.001j, point=1, harmonic=2, role="xy", scan="frequency"),
            row(30, 0.1, 0.002j, point=2, harmonic=2, role="xy", scan="frequency"),
        )
        result = correct_h2(h2, response, model="excitation_squared", role="xy")
        self.assertAlmostEqual(abs(result[1].corrected_v), 1e-9, places=15)
        fig = plot_h2_correction(result, model="excitation_squared", magnitude_scale="log")
        self.assertEqual(fig.axes[0].get_yscale(), "log")
        self.assertEqual(len(fig.axes[0].collections[0].get_offsets()), 3)
        self.assertEqual(len(fig.axes[2].collections[0].get_offsets()), 2)
        self.assertAlmostEqual(fig.axes[2].collections[0].get_offsets()[1, 1], 1e-9, places=15)
        self.assertTrue(fig.axes[0].get_shared_x_axes().joined(fig.axes[0], fig.axes[2]))
        self.assertIn("1 unavailable", fig._suptitle.get_text())
        self.assertIn("2 zero magnitudes", fig._suptitle.get_text())
        fig.canvas.draw()
        plt.close(fig)
        fig = plot_h2_correction(result, model="excitation_squared", magnitude_scale="linear")
        self.assertEqual(fig.axes[0].collections[0].get_offsets()[0, 1], 0)
        plt.close(fig)
        fig = plot_h2_voltage_coefficient(result, x_scale="linear")
        self.assertEqual(len(fig.axes), 3)
        self.assertEqual(fig.axes[2].get_yscale(), "linear")
        self.assertEqual(fig.axes[0].get_xscale(), "linear")
        self.assertIn("1/V", fig.axes[2].get_ylabel())
        self.assertEqual(len(fig.axes[0].collections[0].get_offsets()), 3)
        self.assertEqual(len(fig.axes[2].collections[0].get_offsets()), 2)
        fig.canvas.draw()
        plt.close(fig)
        fig = plot_h1_calibration(calibrate_h1_samples(h1, response))
        self.assertEqual(len(fig.axes), 4)
        self.assertEqual(len(fig.axes[0].collections), 2)
        fig.canvas.draw()
        plt.close(fig)
        # Tiny imaginary roundoff around negative X must not create 360-degree
        # apparent jumps in the self-normalized panel.
        boundary = (
            row(10, 0.1, -0.2+1e-9j, point=0, scan="frequency"),
            replace(row(10, 0.1, -0.2-1e-9j, point=0, scan="frequency"), sample_index=1),
            row(20, 0.1, -0.4+0j, point=1, scan="frequency"),
        )
        fig = plot_h1_calibration(calibrate_h1_samples(boundary, estimate_frequency_response(boundary)))
        phases = fig.axes[3].collections[0].get_offsets()[:, 1]
        self.assertLess(max(phases) - min(phases), 1e-5)
        self.assertAlmostEqual(float(phases.mean()), 180.0)
        plt.close(fig)

    def test_log_frequency_interpolation_is_complex_and_no_extrapolation(self) -> None:
        result = estimate_frequency_response(self.combined_rows())
        q, interpolated = result.relative_at(math.sqrt(10 * 20))
        self.assertTrue(interpolated)
        self.assertAlmostEqual(abs(q), math.sqrt(2))
        with self.assertRaisesRegex(ValueError, "no extrapolation"):
            result.relative_at(100)

    def test_h2_limiting_models_and_out_of_range_audit(self) -> None:
        result = estimate_frequency_response(self.combined_rows())
        h2 = (
            row(20, 0.2, 8 + 4j, point=100, role="xy", harmonic=2),
            row(40, 0.2, 8 + 4j, point=101, role="xy", harmonic=2),
        )
        squared = correct_h2(h2, result, model="excitation_squared", role="xy")
        self.assertAlmostEqual(squared[0].corrected_v.real, 2)
        self.assertIsNone(squared[1].reason)
        with self.assertRaisesRegex(ValueError, "same SR830"):
            correct_h2(h2, result, model="readout_2f", role="xy")
        same_channel = tuple(replace(item, role="xx") for item in h2)
        readout = correct_h2(same_channel, result, model="readout_2f", role="xx")
        self.assertAlmostEqual(readout[0].factor.real, 1.5)
        self.assertIsNone(readout[1].corrected_v)
        self.assertIn("outside", readout[1].reason)

    def test_voltage_coefficient_is_reference_independent_and_uses_driven_h1(self) -> None:
        data = self.combined_rows()
        response = estimate_frequency_response(data)
        another = estimate_frequency_response(data, reference_target_frequency_hz=20)
        h2 = (row(20, 0.2, 0.03+0.01j, point=100, role="xy", harmonic=2),)
        a = correct_h2(h2, response, model="excitation_squared", role="xy")[0]
        b = correct_h2(h2, another, model="excitation_squared", role="xy")[0]
        expected_proxy = (4+2j)*0.2  # Does not include the fitted -0.1+0.4j intercept.
        self.assertAlmostEqual(abs(a.h1_proxy_v - expected_proxy), 0)
        self.assertAlmostEqual(abs(a.coefficient_per_v - (0.03+0.01j)/expected_proxy**2), 0)
        self.assertAlmostEqual(abs(a.coefficient_per_v - b.coefficient_per_v), 0)
        self.assertNotEqual(a.corrected_v, b.corrected_v)
        missing = correct_h2((replace(h2[0], source_readback_confirmed=False),), response, model="excitation_squared", role="xy")[0]
        self.assertIsNone(missing.coefficient_per_v)
        self.assertIn("readback", missing.coefficient_reason)

    def test_lcr_anchor_is_conditional_and_model_specific(self) -> None:
        result = estimate_frequency_response(self.combined_rows())
        inverse = lcr_anchored_impedance(result, anchor_impedance_ohm=100 + 10j, model="inverse_current_proxy")
        direct = lcr_anchored_impedance(result, anchor_impedance_ohm=100 + 10j, model="direct_impedance_proxy")
        self.assertAlmostEqual(inverse[1][1].real, 50)
        self.assertAlmostEqual(direct[1][1].real, 200)

    def test_rejects_mixed_runs_phase_shift_and_insufficient_levels(self) -> None:
        rows = self.combined_rows()
        with self.assertRaisesRegex(ValueError, "one calibration run"):
            estimate_frequency_response((*rows, replace(rows[0], source_path="other.json")))
        with self.assertRaisesRegex(ValueError, "phase setting changed"):
            estimate_frequency_response((*rows, replace(rows[0], phase_shift_deg=20)))
        with self.assertRaisesRegex(ValueError, "three distinct"):
            estimate_frequency_response(tuple(item for item in rows if item.source_v_rms < 0.4))
        with self.assertRaisesRegex(ValueError, "mislabeled sweep"):
            estimate_frequency_response((*rows[1:], replace(rows[0], actual_frequency_hz=17.777)))
        with self.assertRaisesRegex(ValueError, "not a requested-voltage fallback"):
            estimate_frequency_response((*rows[1:], replace(rows[0], source_readback_confirmed=False)))

    def test_notebook_calibration_section_is_valid_and_hardware_free(self) -> None:
        notebook = json.loads((Path(__file__).resolve().parents[1] / "notebooks" / "sr830_commissioning_sweeps.ipynb").read_text(encoding="utf-8"))
        code = "\n".join("".join(cell["source"]) for cell in notebook["cells"] if cell["cell_type"] == "code")
        compile(code, "sweep-notebook", "exec")
        self.assertIn("estimate_frequency_response", code)
        self.assertIn("correct_h2", code)
        self.assertIn("lcr_anchored_impedance", code)
        self.assertIn("calibration_manifest.json", code)

    def test_notebook_calibration_widgets_build_and_apply_without_hardware(self) -> None:
        notebook = json.loads((Path(__file__).resolve().parents[1] / "notebooks" / "sr830_commissioning_sweeps.ipynb").read_text(encoding="utf-8"))
        cell = "".join(notebook["cells"][-1]["source"])

        class Widget:
            def __init__(self, *args, **kwargs):
                self.options = kwargs.get("options", ())
                self.value = kwargs.get("value")
                self.children = args[0] if args else ()
                self.outputs = ()

            def append_display_data(self, value):
                self.outputs += (("display", value),)

            def append_stdout(self, value):
                self.outputs += (("text", value),)

            def observe(self, callback, names):
                self.observer = callback

            def on_click(self, callback):
                self.callback = callback

        widgets = types.SimpleNamespace(
            Dropdown=Widget, FloatText=Widget, Button=Widget,
            HTML=Widget, VBox=Widget, Output=Widget,
        )
        rows = (
            row(10, 0.1, 0.2 + 0.1j, point=0, scan="frequency"),
            row(20, 0.1, 0.4 + 0.2j, point=1, scan="frequency"),
            row(10, 0.1, 0.01j, point=0, role="xy", harmonic=2, scan="frequency"),
            row(20, 0.1, 0.04j, point=1, role="xy", harmonic=2, scan="frequency"),
        )
        displayed = []
        namespace = {
            "widgets": widgets, "Path": Path, "PROJECT_ROOT": Path.cwd(),
            "frequency_rows": rows, "combined_rows": (),
            "display": displayed.append,
            "plt": types.SimpleNamespace(close=lambda *_: None),
            "html": __import__("html"),
        }
        exec(compile(cell, "calibration-cell", "exec"), namespace)
        namespace["plot_frequency_response"] = lambda *_, **__: object()
        namespace["plot_h2_correction"] = lambda *_, **__: object()
        namespace["plot_h1_calibration"] = lambda *_, **__: object()
        namespace["plot_h2_voltage_coefficient"] = lambda *_, **__: object()
        namespace["_calibration_refresh"]()
        namespace["calibration_run_widget"].value = "one.json"
        namespace["calibration_role_widget"].value = "xx"
        namespace["_calibration_build"](None)
        self.assertIsNotNone(namespace["calibration_response"])
        h1_output = namespace["calibration_h1_output"]
        h2_output = namespace["calibration_h2_output"]
        lcr_output = namespace["calibration_lcr_output"]
        for output in (h1_output, h2_output, lcr_output):
            self.assertIn(output, displayed[0].children)
        self.assertEqual(h1_output.outputs[0], ("display", namespace["calibration_response_figure"]))
        self.assertIn("without an intercept", namespace["calibration_message"].value)
        self.assertEqual(len(h1_output.outputs), 2)
        self.assertTrue(all(kind == "display" for kind, _ in h1_output.outputs))
        self.assertEqual(h1_output.outputs[1], ("display", namespace["calibration_h1_figure"]))
        output_count = len(h1_output.outputs)
        namespace["_calibration_build"](None)
        self.assertEqual(len(h1_output.outputs), output_count)
        namespace["calibration_model_widget"].value = "excitation_squared"
        namespace["calibration_h2_role_widget"].value = "xy"
        namespace["calibration_voltage_widget"].value = 0.1
        namespace["_calibration_apply_h2"](None)
        self.assertEqual(len(namespace["calibration_h2_rows"]), 2)
        self.assertAlmostEqual(namespace["calibration_h2_rows"][1].corrected_v.imag, 0.01)
        self.assertEqual(h2_output.outputs[0], ("display", namespace["calibration_h2_figure"]))
        namespace["calibration_model_widget"].value = "voltage_proxy"
        namespace["_calibration_apply_h2"](None)
        self.assertEqual(namespace["calibration_h2_parameters"]["quantity"], "voltage_coefficient")
        self.assertAlmostEqual(namespace["calibration_h2_rows"][1].coefficient_per_v.real, 0.16)
        self.assertIn("1/V", namespace["calibration_message"].value)
        namespace["lcr_real_widget"].value = 100
        namespace["lcr_imag_widget"].value = 0
        namespace["lcr_model_widget"].value = "inverse_current_proxy"
        namespace["_calibration_lcr"](None)
        self.assertIn("ohm", lcr_output.outputs[-1][1])
        exports = []
        namespace.update({
            "SAMPLE_STATUSES": ("clean",), "INCLUDE_REJECTED": False,
            "FREQUENCY_EXCLUDED_TARGET_HZ": (), "COMBINED_EXCLUDED_FREQUENCIES_HZ": (),
            "COMBINED_EXCLUDED_EXCITATIONS_V_RMS": (), "json": json,
            "export_publication_figure_set": lambda fig, path: exports.append(path.name),
        })
        with tempfile.TemporaryDirectory() as folder:
            namespace["PROJECT_ROOT"] = Path(folder)
            namespace["_calibration_export"](None)
            manifests = list(Path(folder).rglob("calibration_manifest.json"))
            self.assertEqual(len(manifests), 1)
            manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
            self.assertEqual(manifest["h2_magnitude_scale"], "linear")
            self.assertEqual(manifest["h2_x_scale"], "log")
            self.assertEqual(manifest["residual_kind"], "formal_read_scatter")
            import csv
            with (manifests[0].parent / "h1_derived.csv").open(newline="", encoding="utf-8") as stream:
                exported = list(csv.DictReader(stream))
            self.assertEqual(len(exported), 2)
            self.assertAlmostEqual(float(exported[1]["corrected_x_v"]), 0.2)
            self.assertEqual(set(exports), {"h1_relative_response", "h1_raw_fitted_normalized", "h2_voltage_coefficient"})
        namespace["repeatability_x_scale_widget"] = Widget(value="linear")
        namespace["_calibration_build"](None)
        self.assertEqual(namespace["calibration_h1_x_scale"], "linear")
        namespace["_calibration_apply_h2"](None)
        self.assertEqual(namespace["calibration_h2_parameters"]["x_scale"], "linear")
        namespace["calibration_run_widget"].value = "missing.json"
        namespace["_calibration_build"](None)
        self.assertIsNone(namespace["calibration_response"])
        for output in (h1_output, h2_output, lcr_output):
            self.assertEqual(output.outputs, ())
        self.assertIn("Calibration unavailable", namespace["calibration_message"].value)
        self.assertEqual(len(displayed), 1)  # No unbound callback display calls.


if __name__ == "__main__":
    unittest.main()
