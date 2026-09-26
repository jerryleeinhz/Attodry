from __future__ import annotations

from dataclasses import replace
import json
import math
from pathlib import Path
import types
import unittest

from attodry_control.commissioning_analysis import CommissioningSample
from attodry_control.frequency_calibration import (
    correct_h2,
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

            def on_click(self, callback):
                self.callback = callback

        widgets = types.SimpleNamespace(
            Dropdown=Widget, FloatText=Widget, Button=Widget,
            HTML=Widget, VBox=Widget,
        )
        rows = (
            row(10, 0.1, 0.2 + 0.1j, point=0, scan="frequency"),
            row(20, 0.1, 0.4 + 0.2j, point=1, scan="frequency"),
            row(10, 0.1, 0.01j, point=0, role="xy", harmonic=2, scan="frequency"),
            row(20, 0.1, 0.04j, point=1, role="xy", harmonic=2, scan="frequency"),
        )
        namespace = {
            "widgets": widgets, "Path": Path, "PROJECT_ROOT": Path.cwd(),
            "frequency_rows": rows, "combined_rows": (),
            "display": lambda *_: None,
        }
        exec(compile(cell, "calibration-cell", "exec"), namespace)
        namespace["plot_frequency_response"] = lambda *_: object()
        namespace["plot_h2_correction"] = lambda *_, **__: object()
        namespace["_calibration_refresh"]()
        namespace["calibration_run_widget"].value = "one.json"
        namespace["calibration_role_widget"].value = "xx"
        namespace["_calibration_build"](None)
        self.assertIsNotNone(namespace["calibration_response"])
        namespace["calibration_model_widget"].value = "excitation_squared"
        namespace["calibration_h2_role_widget"].value = "xy"
        namespace["calibration_voltage_widget"].value = 0.1
        namespace["_calibration_apply_h2"](None)
        self.assertEqual(len(namespace["calibration_h2_rows"]), 2)
        self.assertAlmostEqual(namespace["calibration_h2_rows"][1].corrected_v.imag, 0.01)


if __name__ == "__main__":
    unittest.main()
