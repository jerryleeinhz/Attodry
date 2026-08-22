import json
from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest

from attodry_control.xy_sweep_analysis import (
    load_xy_sweep_samples,
    plot_xy_sweep,
    xy_sweep_harmonic,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class XYSweepAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.paths: list[Path] = []
        self.addCleanup(self._cleanup_files)

    def test_loader_returns_only_xy_frequency_samples(self) -> None:
        path = self._write_json(self._sweep("frequency"))

        rows = load_xy_sweep_samples(path, sample_statuses={"clean"})

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].role, "xy")
        self.assertEqual(rows[0].scan_type, "frequency")
        self.assertEqual(xy_sweep_harmonic(rows), 1)

    def test_loader_preserves_excitation_axis(self) -> None:
        path = self._write_json(self._sweep("excitation"))

        rows = load_xy_sweep_samples(path)

        self.assertEqual(rows[0].source_v_rms, 0.004)
        self.assertEqual(rows[0].nominal_current_a_rms, 3.958e-8)

    def test_mixed_harmonic_orders_are_not_combined(self) -> None:
        path = self._write_json(self._sweep("frequency"))
        rows = load_xy_sweep_samples(path)

        with self.assertRaisesRegex(ValueError, "one XY harmonic"):
            xy_sweep_harmonic((rows[0], replace(rows[0], harmonic=2)))

    def test_plot_discards_xx_and_labels_xy_harmonic(self) -> None:
        try:
            import matplotlib.pyplot as plt
        except ImportError:
            self.skipTest("matplotlib is not installed")
        path = self._write_json(self._sweep("frequency"))
        xy_rows = load_xy_sweep_samples(path)

        figure = plot_xy_sweep(xy_rows)
        self.addCleanup(plt.close, figure)

        axis = figure.axes[0]
        _, labels = axis.get_legend_handles_labels()
        self.assertEqual(labels, ["XY · h1"])
        self.assertEqual(axis.get_title(), "SR830 XY h1 frequency sweep")

    def test_excitation_current_plot_uses_recorded_path_by_default(self) -> None:
        try:
            import matplotlib.pyplot as plt
        except ImportError:
            self.skipTest("matplotlib is not installed")
        payload = self._sweep("excitation")
        payload["measurement_config"] = self._measurement_config_path()
        payload["points"][0]["source_readback_v_rms"] = 0.0039
        path = self._write_json(payload)
        xy_rows = load_xy_sweep_samples(path)

        figure = plot_xy_sweep(
            xy_rows,
            x_axis="sine_output_current_a_rms",
            log_x=False,
        )
        self.addCleanup(plt.close, figure)

        axis = figure.axes[0]
        self.assertEqual(axis.get_xlabel(), "SINE OUT current (A RMS)")
        self.assertAlmostEqual(axis.lines[0].get_xdata()[0], 0.0039 / 100_550.0)

    def test_notebook_keeps_frequency_and_excitation_but_no_xx_series(self) -> None:
        path = PROJECT_ROOT / "notebooks" / "sr830_xy_sweeps.ipynb"
        notebook = json.loads(path.read_text(encoding="utf-8"))
        source = "\n".join(
            "".join(cell["source"])
            for cell in notebook["cells"]
            if cell["cell_type"] == "code"
        )

        self.assertIn("load_xy_sweep_samples", source)
        self.assertIn("frequency_rows", source)
        self.assertIn("excitation_rows", source)
        self.assertIn("excitation_path_from_sweep_files", source)
        self.assertIn("EXCITATION_PATH_OVERRIDE", source)
        self.assertIn("sine_output_current_a_rms", source)
        self.assertNotIn("roles={'xx'", source.lower())
        for index, cell in enumerate(notebook["cells"]):
            if cell["cell_type"] == "code":
                compile("".join(cell["source"]), f"xy-sweep-cell-{index}", "exec")

    def _write_json(self, payload: dict[str, object]) -> Path:
        descriptor, raw_path = tempfile.mkstemp(
            prefix="xy_sweep_", suffix=".json", dir=PROJECT_ROOT
        )
        os.close(descriptor)
        path = Path(raw_path)
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.paths.append(path)
        return path

    def _cleanup_files(self) -> None:
        for path in self.paths:
            if path.exists():
                path.unlink()

    @staticmethod
    def _sweep(scan_type: str) -> dict[str, object]:
        return {
            "scan": scan_type,
            "completed": True,
            "points": [
                {
                    "point_index": 0,
                    "target_frequency_hz": 17.777,
                    "source_v_rms": 0.004,
                    "nominal_current_a_rms": 3.958e-8,
                    "samples": [
                        {
                            "sample_index": 0,
                            "lockin_xx": XYSweepAnalysisTests._instrument("xx", 1e-4),
                            "lockin_xy": XYSweepAnalysisTests._instrument("xy", 1e-6),
                            "problems": [],
                        }
                    ],
                }
            ],
        }

    @staticmethod
    def _measurement_config_path() -> dict[str, object]:
        return {
            "excitation_path": {
                "series_resistance_ohm": 100_000.0,
                "sr830_output_resistance_ohm": 50.0,
                "approximate_device_resistance_ohm": 500.0,
            }
        }

    @staticmethod
    def _instrument(role: str, amplitude: float) -> dict[str, object]:
        return {
            "reading": {
                "role": role,
                "harmonic": 1,
                "x_v": amplitude,
                "y_v": 0.0,
                "amplitude_v": amplitude,
                "phase_deg": 0.0,
                "frequency_hz": 17.777,
                "locked": True,
                "overload": False,
            },
            "lia_status": {
                "raw": 0,
                "input_or_reserve_overload": False,
                "filter_overload": False,
                "output_overload": False,
                "reference_unlocked": False,
            },
            "error_status": 0,
        }


if __name__ == "__main__":
    unittest.main()
