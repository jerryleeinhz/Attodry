import json
from dataclasses import replace
import math
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from attodry_control.commissioning_analysis import (
    ExcitationPathResistance,
    aggregate_sweep_samples,
    browse_and_load_commissioning_file,
    discover_commissioning_records,
    excitation_path_from_sweep_files,
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

    def test_sine_output_current_defaults_to_recorded_sweep_path(self) -> None:
        payload = self._sweep(completed=True)
        payload["measurement_config"] = self._measurement_config_path()
        payload["points"][0]["source_readback_v_rms"] = 0.0039
        source = self._write_json("recorded-path.json", payload)

        rows = load_sweep_sample_files([source])
        statistics = aggregate_sweep_samples(
            rows,
            x_axis="sine_output_current_a_rms",
        )

        self.assertEqual(rows[0].recorded_external_series_resistance_ohm, 100_000.0)
        self.assertEqual(rows[0].recorded_sr830_output_resistance_ohm, 50.0)
        self.assertEqual(rows[0].recorded_approximate_device_resistance_ohm, 500.0)
        self.assertAlmostEqual(statistics[0].x_value, 0.0039 / 100_550.0)

    def test_legacy_current_requires_explicit_path_override(self) -> None:
        payload = self._sweep(completed=True)
        source = self._write_json("legacy.json", payload)
        rows = load_sweep_sample_files([source])
        override = ExcitationPathResistance(100_000.0, 50.0, 500.0)

        with self.assertRaisesRegex(ValueError, "recorded measurement_config"):
            aggregate_sweep_samples(rows, x_axis="sine_output_current_a_rms")

        statistics = aggregate_sweep_samples(
            rows,
            x_axis="sine_output_current_a_rms",
            excitation_path=override,
        )
        self.assertAlmostEqual(statistics[0].x_value, 0.004 / 100_550.0)

    def test_selected_sweep_paths_require_one_recorded_calibration(self) -> None:
        first_payload = self._sweep(completed=True)
        first_payload["measurement_config"] = self._measurement_config_path()
        first = self._write_json("first.json", first_payload)
        second_payload = self._sweep(completed=True)
        second_payload["measurement_config"] = self._measurement_config_path(
            external_series_resistance_ohm=200_000.0
        )
        second = self._write_json("second.json", second_payload)
        legacy = self._write_json("legacy.json", self._sweep(completed=True))
        override = ExcitationPathResistance(300_000.0, 50.0, 500.0)

        resolved = excitation_path_from_sweep_files([first])

        self.assertEqual(resolved.total_resistance_ohm, 100_550.0)
        with self.assertRaisesRegex(ValueError, "different excitation paths"):
            excitation_path_from_sweep_files([first, second])
        with self.assertRaisesRegex(ValueError, "different excitation paths"):
            aggregate_sweep_samples(
                load_sweep_sample_files([first, second]),
                x_axis="sine_output_current_a_rms",
            )
        with self.assertRaisesRegex(ValueError, "no recorded measurement_config"):
            excitation_path_from_sweep_files([legacy])
        self.assertEqual(
            excitation_path_from_sweep_files(
                [first, second], excitation_path_override=override
            ),
            override,
        )

    def test_role_harmonic_plots_have_voltage_and_phase_axes(self) -> None:
        try:
            import matplotlib.pyplot as plt
        except ImportError:
            self.skipTest("matplotlib is not installed")
        payload = self._sweep(completed=True)
        payload["measurement_config"] = self._measurement_config_path()
        source = self._write_json("completed.json", payload)
        rows = load_sweep_samples(source)

        figure = plot_role_harmonic_sweep(rows, role="xx", harmonic=1)
        self.addCleanup(plt.close, figure)
        self.assertEqual(len(figure.axes), 2)
        self.assertIn("I_RMS = 39.78 nA", figure.axes[0].get_title())
        self.assertEqual(figure.axes[0].get_ylabel(), "Vxx R (V RMS)")
        self.assertEqual(figure.axes[1].get_ylabel(), "Unwrapped phase (degree)")

        figures = plot_six_role_harmonic_sweeps(rows)
        self.addCleanup(lambda: [plt.close(item) for item in figures.values()])
        self.assertEqual(set(figures), {
            ("xx", 1), ("xx", 2), ("xx", 3),
            ("xy", 1), ("xy", 2), ("xy", 3),
        })
        self.assertIn(
            "No selected Vxy h2 samples",
            figures[("xy", 2)].axes[0].texts[0].get_text(),
        )

    def test_role_harmonic_phase_plot_unwraps_and_omits_unqualified_points(self) -> None:
        try:
            import matplotlib.pyplot as plt
        except ImportError:
            self.skipTest("matplotlib is not installed")
        source = self._write_json("completed.json", self._sweep(completed=True))
        base = next(row for row in load_sweep_samples(source) if row.role == "xx")
        rows = (
            replace(
                base,
                target_frequency_hz=10.0,
                sample_index=0,
                amplitude_v=0.5e-6,
                phase_deg=0.0,
            ),
            replace(
                base,
                target_frequency_hz=10.0,
                sample_index=1,
                amplitude_v=0.5e-6,
                phase_deg=90.0,
            ),
            replace(
                base,
                target_frequency_hz=100.0,
                sample_index=0,
                amplitude_v=5e-6,
                phase_deg=179.0,
            ),
            replace(
                base,
                target_frequency_hz=100.0,
                sample_index=1,
                amplitude_v=5e-6,
                phase_deg=-179.0,
            ),
            replace(
                base,
                target_frequency_hz=1000.0,
                sample_index=0,
                amplitude_v=5e-6,
                phase_deg=-178.0,
            ),
            replace(
                base,
                target_frequency_hz=1000.0,
                sample_index=1,
                amplitude_v=5e-6,
                phase_deg=-178.0,
            ),
        )
        path = ExcitationPathResistance(100_000.0, 50.0, 500.0)

        figure = plot_role_harmonic_sweep(
            rows,
            role="xx",
            harmonic=1,
            excitation_path=path,
            phase_minimum_amplitude_v=1e-6,
            phase_maximum_standard_deviation_deg=5.0,
        )
        self.addCleanup(plt.close, figure)
        phase_line = next(
            line for line in figure.axes[1].get_lines() if line.get_marker() == "s"
        )
        phase_values = phase_line.get_ydata()

        self.assertTrue(math.isnan(float(phase_values[0])))
        self.assertAlmostEqual(float(phase_values[1]), 180.0)
        self.assertAlmostEqual(float(phase_values[2]), 182.0)
        self.assertEqual(figure.axes[1].get_ylabel(), "Unwrapped phase (degree)")

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

    def test_notebook_exposes_independent_record_and_point_selectors(self) -> None:
        path = PROJECT_ROOT / "notebooks" / "sr830_commissioning_sweeps.ipynb"
        notebook = json.loads(path.read_text(encoding="utf-8"))
        code = "\n".join(
            "".join(cell["source"])
            for cell in notebook["cells"]
            if cell["cell_type"] == "code"
        )

        self.assertIn("widgets.Button", code)
        self.assertIn("completed_only_widget", code)
        self.assertIn("refresh_records_button.on_click", code)
        self.assertIn("load_selected_records_button.on_click", code)
        self.assertIn("frequency_record_widget", code)
        self.assertIn("excitation_record_widget", code)
        self.assertIn("frequency_excluded_points_widget", code)
        self.assertIn("excitation_excluded_points_widget", code)
        self.assertIn("apply_point_exclusions_button", code)
        self.assertIn("def _load_selected_formal_samples", code)
        self.assertIn("loaded = _load_selected_formal_samples()", code)
        self.assertIn("selection_manifest.json", code)
        self.assertIn("if frequency_rows", code)
        self.assertIn("if excitation_rows", code)
        self.assertNotIn("browse_and_load_commissioning_file", code)
        self.assertNotIn("A completed frequency record is missing", code)
        self.assertNotIn("A completed excitation record is missing", code)
        self.assertIn("excitation_path_from_sweep_files", code)
        self.assertIn("EXCITATION_PATH_OVERRIDE", code)
        self.assertNotIn("EXTERNAL_SERIES_RESISTANCE_OHM", code)

    def test_notebook_load_button_populates_selected_point_options(self) -> None:
        payload = self._sweep(completed=True)
        payload["scan"] = "excitation"
        payload["measurement_config"] = self._measurement_config_path()
        source = self._write_json("excitation.json", payload)
        notebook = json.loads(
            (PROJECT_ROOT / "notebooks" / "sr830_commissioning_sweeps.ipynb").read_text(
                encoding="utf-8"
            )
        )
        cells = notebook["cells"]
        widgets = _fake_notebook_widgets()
        matplotlib = types.ModuleType("matplotlib")
        pyplot = types.ModuleType("matplotlib.pyplot")
        matplotlib.pyplot = pyplot
        ipython = types.ModuleType("IPython")
        display_module = types.ModuleType("IPython.display")
        display_module.display = lambda _: None
        ipython.display = display_module
        scope: dict[str, object] = {}

        with patch.dict(
            sys.modules,
            {
                "ipywidgets": widgets,
                "matplotlib": matplotlib,
                "matplotlib.pyplot": pyplot,
                "IPython": ipython,
                "IPython.display": display_module,
            },
        ):
            exec("".join(cells[1]["source"]), scope)
            scope["DATA_DIRECTORY"] = source.parent
            exec("".join(cells[3]["source"]), scope)
            scope["excitation_record_widget"].value = str(source)
            scope["_load_selected_records"](None)

        self.assertEqual(len(scope["frequency_excluded_points_widget"].options), 0)
        self.assertEqual(len(scope["excitation_excluded_points_widget"].options), 1)
        self.assertEqual(len(scope["frequency_rows"]), 0)
        self.assertEqual(len(scope["excitation_rows"]), 2)


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

    @staticmethod
    def _measurement_config_path(
        *, external_series_resistance_ohm: float = 100_000.0
    ) -> dict[str, object]:
        return {
            "excitation_path": {
                "series_resistance_ohm": external_series_resistance_ohm,
                "sr830_output_resistance_ohm": 50.0,
                "approximate_device_resistance_ohm": 500.0,
            }
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


def _fake_notebook_widgets() -> types.ModuleType:
    class Widget:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.children = args[0] if args else ()
            self.options = kwargs.get("options", ())
            self.value = kwargs.get("value", ())

        def on_click(self, callback: object) -> None:
            self.callback = callback

    widgets = types.ModuleType("ipywidgets")
    widgets.Checkbox = Widget
    widgets.SelectMultiple = Widget
    widgets.Button = Widget
    widgets.Dropdown = Widget
    widgets.HTML = Widget
    widgets.HBox = Widget
    widgets.VBox = Widget
    widgets.Layout = lambda **kwargs: kwargs
    return widgets


if __name__ == "__main__":
    unittest.main()
