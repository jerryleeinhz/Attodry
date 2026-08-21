import json
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from attodry_control.commissioning_analysis import (
    ExcitationPathResistance,
    aggregate_sweep_samples,
    browse_and_load_commissioning_file,
    discover_commissioning_records,
    export_commissioning_csv,
    load_sweep_sample_files,
    load_sweep_samples,
    plot_role_harmonic_sweep,
    plot_six_role_harmonic_sweeps,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CommissioningAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.paths: list[Path] = []
        self.addCleanup(self._cleanup_files)

    def test_catalog_filters_completed_rejected_and_diagnostic_records(self) -> None:
        completed = self._write_json("completed.json", self._sweep(completed=True))
        self._write_json("rejected.json", self._sweep(completed=False))
        diagnostic = self._temporary_path(".jsonl")
        diagnostic.write_text(
            json.dumps({"sample_index": 0, "problems": []}) + "\n",
            encoding="utf-8",
        )

        catalog = discover_commissioning_records(PROJECT_ROOT)
        by_path = {record.path: record for record in catalog}
        self.assertEqual(by_path[completed].record_status, "completed")
        self.assertEqual(by_path[diagnostic].record_status, "diagnostic")
        accepted = discover_commissioning_records(
            PROJECT_ROOT,
            record_statuses={"completed"},
            scan_types={"frequency"},
        )
        self.assertIn(completed, [record.path for record in accepted])
        diagnostics = discover_commissioning_records(
            PROJECT_ROOT, record_statuses={"diagnostic"}
        )
        self.assertIn(diagnostic, [record.path for record in diagnostics])

    def test_rejected_sweep_requires_explicit_audit_mode_and_filters_status(self) -> None:
        path = self._write_json(
            "rejected.json", self._sweep(completed=False, xy_lia_raw=8)
        )
        with self.assertRaisesRegex(ValueError, "include_rejected=True"):
            load_sweep_samples(path)

        rows = load_sweep_samples(path, include_rejected=True)
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            [row.role for row in load_sweep_samples(
                path, include_rejected=True, sample_statuses={"clean"}
            )],
            ["xx"],
        )
        unlocked = load_sweep_samples(
            path, include_rejected=True, sample_statuses={"unlocked"}
        )
        self.assertEqual([row.role for row in unlocked], ["xy"])

    def test_formal_loader_excludes_transition_and_aggregates_samples(self) -> None:
        payload = self._sweep(completed=True)
        payload["points"][0]["transition_status"] = {
            "lockin_xy": self._instrument("xy", 9.0, lia_raw=8)
        }
        second = self._sample(xx_amplitude=3.0, xy_amplitude=0.4)
        second["sample_index"] = 1
        payload["points"][0]["samples"].append(second)
        path = self._write_json("completed.json", payload)

        rows = load_sweep_samples(path)
        self.assertEqual(len(rows), 4)
        xx_statistics = [
            item
            for item in aggregate_sweep_samples(rows)
            if item.role == "xx"
        ]
        self.assertEqual(len(xx_statistics), 1)
        self.assertEqual(xx_statistics[0].mean, 2.0)
        self.assertEqual(xx_statistics[0].count, 2)

        xx_rows = [row for row in rows if row.role == "xx"]
        phase_rows = (
            replace(xx_rows[0], phase_deg=179.0),
            replace(xx_rows[1], phase_deg=-179.0),
        )
        phase = aggregate_sweep_samples(phase_rows, metric="phase_deg")[0]
        self.assertAlmostEqual(abs(phase.mean), 180.0)
        self.assertLess(phase.standard_deviation, 2.0)

    def test_csv_export_and_injected_browse_open_data_directly(self) -> None:
        source = self._write_json("completed.json", self._sweep(completed=True))
        browsed = browse_and_load_commissioning_file(
            PROJECT_ROOT, chooser=lambda initial: source
        )
        self.assertIsNotNone(browsed)
        selected, payload = browsed
        self.assertEqual(selected, source)
        self.assertIsInstance(payload, dict)

        destination = self._temporary_path(".csv")
        export_commissioning_csv(load_sweep_samples(source), destination)
        text = destination.read_text(encoding="utf-8")
        self.assertIn("record_status", text)
        self.assertIn("clean", text)

    def test_power_shell_utf16_record_is_directly_loadable(self) -> None:
        source = self._temporary_path(".json")
        source.write_text(
            json.dumps(self._sweep(completed=True)),
            encoding="utf-16",
        )

        rows = load_sweep_samples(source)

        self.assertEqual([row.role for row in rows], ["xx", "xy"])

    def test_sine_output_current_uses_readback_and_explicit_path_resistance(self) -> None:
        payload = self._sweep(completed=True)
        payload["points"][0]["source_readback_v_rms"] = 0.0039
        source = self._write_json("completed.json", payload)
        path = ExcitationPathResistance(
            external_series_resistance_ohm=100_000.0,
            sr830_output_resistance_ohm=50.0,
            approximate_device_resistance_ohm=500.0,
        )

        rows = load_sweep_sample_files([source])
        statistics = aggregate_sweep_samples(
            rows,
            x_axis="sine_output_current_a_rms",
            excitation_path=path,
        )

        self.assertEqual(path.total_resistance_ohm, 100_550.0)
        self.assertAlmostEqual(rows[0].sine_output_v_rms, 0.0039)
        self.assertAlmostEqual(
            statistics[0].x_value, 0.0039 / 100_550.0
        )
        with self.assertRaisesRegex(ValueError, "non-negative"):
            ExcitationPathResistance(-1.0, 50.0, 500.0)

    def test_role_harmonic_plots_have_voltage_and_phase_axes(self) -> None:
        try:
            import matplotlib.pyplot as plt
        except ImportError:
            self.skipTest("matplotlib is not installed")
        source = self._write_json("completed.json", self._sweep(completed=True))
        rows = load_sweep_samples(source)
        path = ExcitationPathResistance(100_000.0, 50.0, 500.0)

        figure = plot_role_harmonic_sweep(
            rows, role="xx", harmonic=1, excitation_path=path
        )
        self.addCleanup(plt.close, figure)
        self.assertEqual(len(figure.axes), 2)
        self.assertIn("I_RMS = 39.78 nA", figure.axes[0].get_title())
        self.assertEqual(figure.axes[0].get_ylabel(), "Vxx R (V RMS)")
        self.assertEqual(figure.axes[1].get_ylabel(), "Phase (degree)")

        figures = plot_six_role_harmonic_sweeps(rows, excitation_path=path)
        self.addCleanup(lambda: [plt.close(item) for item in figures.values()])
        self.assertEqual(set(figures), {
            ("xx", 1), ("xx", 2), ("xx", 3),
            ("xy", 1), ("xy", 2), ("xy", 3),
        })
        self.assertIn(
            "No selected Vxy h2 samples",
            figures[("xy", 2)].axes[0].texts[0].get_text(),
        )

    def test_notebook_is_valid_json_and_all_code_cells_compile(self) -> None:
        path = PROJECT_ROOT / "notebooks" / "sr830_commissioning_sweeps.ipynb"
        notebook = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(notebook["nbformat"], 4)
        code_cells = [
            cell for cell in notebook["cells"] if cell["cell_type"] == "code"
        ]
        self.assertGreaterEqual(len(code_cells), 6)
        for index, cell in enumerate(code_cells):
            compile("".join(cell["source"]), f"notebook-cell-{index}", "exec")

    def test_notebook_exposes_browse_button_and_completed_filter(self) -> None:
        path = PROJECT_ROOT / "notebooks" / "sr830_commissioning_sweeps.ipynb"
        notebook = json.loads(path.read_text(encoding="utf-8"))
        code = "\n".join(
            "".join(cell["source"])
            for cell in notebook["cells"]
            if cell["cell_type"] == "code"
        )

        self.assertIn("widgets.Button", code)
        self.assertIn("completed_only_widget", code)
        self.assertIn("browse_button.on_click", code)
        self.assertIn("browse_and_load_commissioning_file", code)

    def _write_json(self, _name: str, payload: dict[str, object]) -> Path:
        path = self._temporary_path(".json")
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _temporary_path(self, suffix: str) -> Path:
        temporary = tempfile.NamedTemporaryFile(
            dir=PROJECT_ROOT,
            prefix="commissioning_analysis_",
            suffix=suffix,
            delete=False,
        )
        temporary.close()
        path = Path(temporary.name)
        self.paths.append(path)
        return path

    def _cleanup_files(self) -> None:
        for path in self.paths:
            if path.exists():
                path.unlink()

    def _sweep(
        self, *, completed: bool, xy_lia_raw: int = 0
    ) -> dict[str, object]:
        return {
            "scan": "frequency",
            "completed": completed,
            "error": None if completed else "injected rejection",
            "points": [
                {
                    "point_index": 0,
                    "target_frequency_hz": 17.777,
                    "source_v_rms": 0.004,
                    "nominal_current_a_rms": None,
                    "samples": [self._sample(xy_lia_raw=xy_lia_raw)],
                }
            ],
        }

    def _sample(
        self,
        *,
        xx_amplitude: float = 1.0,
        xy_amplitude: float = 0.2,
        xy_lia_raw: int = 0,
    ) -> dict[str, object]:
        return {
            "sample_index": 0,
            "lockin_xx": self._instrument("xx", xx_amplitude),
            "lockin_xy": self._instrument("xy", xy_amplitude, lia_raw=xy_lia_raw),
            "problems": [],
        }

    @staticmethod
    def _instrument(
        role: str, amplitude: float, *, lia_raw: int = 0
    ) -> dict[str, object]:
        unlocked = bool(lia_raw & 8)
        return {
            "reading": {
                "role": role,
                "harmonic": 1,
                "x_v": amplitude,
                "y_v": 0.0,
                "amplitude_v": amplitude,
                "phase_deg": 0.0,
                "frequency_hz": 17.777,
                "locked": not unlocked,
                "overload": False,
            },
            "lia_status": {
                "raw": lia_raw,
                "input_or_reserve_overload": False,
                "filter_overload": False,
                "output_overload": False,
                "reference_unlocked": unlocked,
                "frequency_range_changed": False,
                "time_constant_changed": False,
                "triggered": False,
            },
            "error_status": 0,
        }


if __name__ == "__main__":
    unittest.main()
