import ast
import csv
import json
import math
from pathlib import Path
import tempfile
import unittest

from attodry_control.temperature_excitation_analysis import (
    aggregate_temperature_iv,
    discover_temperature_excitation_records,
    load_temperature_excitation_sample_files,
    load_temperature_excitation_samples,
    plot_temperature_iv_curves,
    plot_temperature_iv_suite,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = PROJECT_ROOT / "notebooks" / "sr830_commissioning_sweeps.ipynb"


class TemperatureExcitationAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(dir=PROJECT_ROOT)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_loader_uses_actual_temperature_and_recorded_current(self) -> None:
        path = self._write_summary(self._summary())

        rows = load_temperature_excitation_samples(path)

        self.assertEqual(len(rows), 32)
        self.assertEqual(
            {row.condition_measurement_temperature_k for row in rows},
            {1.775, 2.025},
        )
        self.assertEqual({row.requested_temperature_k for row in rows}, {1.8, 2.0})
        self.assertEqual({row.current_a_rms for row in rows}, {4.0e-8, 8.0e-8})
        self.assertNotIn(0.004 / 100_550.0, {row.current_a_rms for row in rows})
        self.assertTrue(all(row.statuses == ("clean",) for row in rows))

    def test_loader_defaults_to_clean_but_supports_explicit_status_audit(self) -> None:
        payload = self._summary()
        audited = payload["temperature_conditions"][0]["excitation"]["points"][0][
            "samples"
        ][0]["lockin_xy"]
        audited["lia_status"] = {"raw": 8, "reference_unlocked": True}
        path = self._write_summary(payload)

        clean = load_temperature_excitation_samples(path)
        audit = load_temperature_excitation_samples(path, sample_statuses=None)

        self.assertEqual(len(clean), 31)
        self.assertEqual(len(audit), 32)
        unlocked = [row for row in audit if "unlocked" in row.statuses]
        self.assertEqual([(row.role, row.harmonic) for row in unlocked], [("xy", 1)])

    def test_phase_aggregation_is_circular_and_temperatures_remain_separate(self) -> None:
        path = self._write_summary(self._summary())
        rows = load_temperature_excitation_samples(path)

        statistics = aggregate_temperature_iv(
            rows, role="xx", harmonic=1, metric="phase_deg"
        )

        self.assertEqual(len(statistics), 4)
        wrapped = next(
            item
            for item in statistics
            if item.temperature_index == 0 and item.current_a_rms == 4.0e-8
        )
        self.assertAlmostEqual(abs(wrapped.mean), 180.0)
        self.assertLess(wrapped.standard_deviation, 2.0)
        self.assertEqual({item.condition_measurement_temperature_k for item in statistics}, {1.775, 2.025})

    def test_discovery_prefers_summary_over_matching_formal_csv(self) -> None:
        summary = self._write_summary(self._summary())
        formal = summary.with_name(
            summary.name.replace("_summary.json", "_formal_samples.csv")
        )
        formal.write_text("temperature_index\n", encoding="utf-8")

        records = discover_temperature_excitation_records(self.root)

        self.assertEqual(records, (summary,))

    def test_formal_csv_loader_preserves_recorded_current_phase_and_actual_temperature(
        self,
    ) -> None:
        path = self.root / "csv_temperature_excitation_formal_samples.csv"
        fieldnames = (
            "temperature_index",
            "requested_temperature_k",
            "stability_measurement_temperature_k",
            "condition_measurement_temperature_k",
            "source_v_rms",
            "source_readback_v_rms",
            "nominal_current_a_rms",
            "harmonic",
            "sample_index",
            "role",
            "measurement_temperature_k",
            "x_v",
            "y_v",
            "amplitude_v",
            "phase_deg",
            "frequency_hz",
            "lia_status_raw",
            "error_status",
        )
        row = {
            "temperature_index": 3,
            "requested_temperature_k": 12.0,
            "stability_measurement_temperature_k": 11.98,
            "condition_measurement_temperature_k": 11.991,
            "source_v_rms": 0.004,
            "source_readback_v_rms": 0.00402,
            "nominal_current_a_rms": 4.001e-8,
            "harmonic": 3,
            "sample_index": 1,
            "role": "lockin_xy",
            "measurement_temperature_k": 11.993,
            "x_v": 2.0e-8,
            "y_v": -3.0e-8,
            "amplitude_v": 3.6e-8,
            "phase_deg": -56.3,
            "frequency_hz": 53.331,
            "lia_status_raw": 0,
            "error_status": 0,
        }
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerow(row)

        loaded = load_temperature_excitation_samples(path)

        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0].role, "xy")
        self.assertEqual(loaded[0].harmonic, 3)
        self.assertEqual(loaded[0].current_a_rms, 4.001e-8)
        self.assertEqual(loaded[0].condition_measurement_temperature_k, 11.991)
        self.assertEqual(loaded[0].phase_deg, -56.3)

    def test_multiple_files_are_loaded_without_merging_run_identity(self) -> None:
        first = self._write_summary(self._summary(), name="first")
        second_payload = self._summary()
        second_payload["temperature_conditions"][0][
            "measurement_temperature_k"
        ] = 1.776
        second = self._write_summary(second_payload, name="second")

        rows = load_temperature_excitation_sample_files((first, second))
        statistics = aggregate_temperature_iv(
            rows, role="xx", harmonic=1, metric="amplitude_v"
        )

        first_points = [item for item in statistics if item.source_path == str(first)]
        second_points = [item for item in statistics if item.source_path == str(second)]
        self.assertEqual(len(first_points), 4)
        self.assertEqual(len(second_points), 4)

    def test_amplitude_and_phase_figures_have_one_curve_per_temperature(self) -> None:
        try:
            import matplotlib.pyplot as plt
        except (ImportError, RuntimeError) as exc:
            self.skipTest(f"matplotlib unavailable: {exc}")
        rows = load_temperature_excitation_samples(
            self._write_summary(self._summary())
        )

        amplitude = plot_temperature_iv_curves(
            rows, role="xx", harmonic=1, metric="amplitude_v"
        )
        phase = plot_temperature_iv_curves(
            rows,
            role="xx",
            harmonic=1,
            metric="phase_deg",
            destination=self.root / "xx_h1_phase.png",
        )
        suite = plot_temperature_iv_suite(rows)
        self.addCleanup(plt.close, "all")

        self.assertEqual(amplitude.axes[0].get_xlabel(), "SINE OUT current (A RMS)")
        self.assertEqual(amplitude.axes[0].get_ylabel(), "Vxx R (V RMS)")
        self.assertEqual(phase.axes[0].get_ylabel(), "Vxx phase (degree)")
        self.assertEqual(len(amplitude.axes[0].get_legend_handles_labels()[1]), 2)
        self.assertEqual(len(phase.axes[0].get_legend_handles_labels()[1]), 2)
        self.assertGreater((self.root / "xx_h1_phase.png").stat().st_size, 0)
        self.assertEqual(
            set(suite),
            {
                (role, harmonic, metric)
                for role in ("xx", "xy")
                for harmonic in (1, 2)
                for metric in ("amplitude_v", "phase_deg")
            },
        )

    def test_notebook_exposes_temperature_record_browser_and_both_metrics(self) -> None:
        document = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
        code = "\n\n".join(
            "".join(cell.get("source", ()))
            for cell in document["cells"]
            if cell["cell_type"] == "code"
        )

        ast.parse(code)
        self.assertIn("discover_temperature_excitation_records", code)
        self.assertIn("load_temperature_excitation_sample_files", code)
        self.assertIn("plot_temperature_iv_suite", code)
        self.assertIn("temperature_excitation_record_widget", code)
        self.assertIn("TEMPERATURE_DATA_DIRECTORY", code)
        self.assertIn("phase_statistics", code)
        self.assertIn("circular mean/std", code)
        self.assertIn("SOURCE_DIRECTORY = PROJECT_ROOT / 'src'", code)
        self.assertLess(
            code.index("sys.path.insert(0, source_directory_text)"),
            code.index("from attodry_control.commissioning_analysis"),
        )
        self.assertNotIn("attodry_control.attodry", code)
        self.assertNotIn("attodry_control.sr830", code)

    def _write_summary(
        self, payload: dict[str, object], *, name: str = "test"
    ) -> Path:
        path = self.root / f"{name}_temperature_excitation_summary.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _summary(self) -> dict[str, object]:
        return {
            "command": "temperature-excitation-scan",
            "completed": True,
            "outcome": "completed",
            "temperature_conditions": [
                self._condition(0, requested_k=1.8, measured_k=1.775),
                self._condition(1, requested_k=2.0, measured_k=2.025),
            ],
        }

    def _condition(
        self, temperature_index: int, *, requested_k: float, measured_k: float
    ) -> dict[str, object]:
        points = []
        for point_index, current in enumerate((4.0e-8, 8.0e-8)):
            samples = []
            for sample_index in range(2):
                phase = (
                    (179.0 if sample_index == 0 else -179.0)
                    if temperature_index == 0 and point_index == 0
                    else 20.0 + 5.0 * temperature_index + sample_index
                )
                samples.append(
                    {
                        "harmonic": 1,
                        "sample_index": sample_index,
                        "measurement_temperature_k": measured_k
                        + (sample_index - 0.5) * 0.001,
                        "selected_roles": ["xx", "xy"],
                        "problems": [],
                        "lockin_xx": self._instrument(
                            amplitude=1.0e-6 * (point_index + 1), phase=phase
                        ),
                        "lockin_xy": self._instrument(
                            amplitude=2.0e-7 * (point_index + 1), phase=-phase
                        ),
                    }
                )
                samples.append(
                    {
                        "harmonic": 2,
                        "sample_index": sample_index,
                        "measurement_temperature_k": measured_k
                        + (sample_index - 0.5) * 0.001,
                        "selected_roles": ["xx", "xy"],
                        "problems": [],
                        "lockin_xx": self._instrument(
                            amplitude=3.0e-7 * (point_index + 1), phase=phase / 2.0
                        ),
                        "lockin_xy": self._instrument(
                            amplitude=5.0e-8 * (point_index + 1), phase=-phase / 2.0
                        ),
                    }
                )
            points.append(
                {
                    "point_index": point_index,
                    "source_v_rms": 0.004 * (point_index + 1),
                    "source_readback_v_rms": 0.0039 * (point_index + 1),
                    "nominal_current_a_rms": current,
                    "samples": samples,
                }
            )
        return {
            "temperature_index": temperature_index,
            "requested_temperature_k": requested_k,
            "stability_measurement_temperature_k": measured_k - 0.01,
            "measurement_temperature_k": measured_k,
            "excitation": {
                "completed": True,
                "outcome": "completed",
                "points": points,
                "cleanup": {"attempted": True, "verified": True, "errors": []},
            },
        }

    @staticmethod
    def _instrument(*, amplitude: float, phase: float) -> dict[str, object]:
        radians = math.radians(phase)
        return {
            "reading": {
                "x_v": amplitude * math.cos(radians),
                "y_v": amplitude * math.sin(radians),
                "amplitude_v": amplitude,
                "phase_deg": phase,
                "frequency_hz": 17.777,
                "locked": True,
            },
            "lia_status": {"raw": 0},
            "error_status": 0,
        }


if __name__ == "__main__":
    unittest.main()
