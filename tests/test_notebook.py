import ast
import json
import math
from pathlib import Path
import unittest
from unittest.mock import Mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = PROJECT_ROOT / "notebooks" / "transport_analysis.ipynb"


class NotebookTests(unittest.TestCase):
    def test_notebook_is_clean_syntax_valid_and_hardware_free(self) -> None:
        document = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
        code = "\n\n".join(
            "".join(cell.get("source", ()))
            for cell in document["cells"]
            if cell["cell_type"] == "code"
        )
        ast.parse(code)
        forbidden_imports = (
            "attodry_control.attodry",
            "attodry_control.sr830",
            "attodry_control.gates",
            "MultiPyVu",
            "ppms_control",
        )
        self.assertFalse(any(name in code for name in forbidden_imports))
        for cell in document["cells"]:
            if cell["cell_type"] == "code":
                self.assertIsNone(cell["execution_count"])
                self.assertEqual(cell["outputs"], [])

    def test_optional_export_without_running_temperature_section(self) -> None:
        import tempfile
        from types import SimpleNamespace

        document = json.loads(
            (PROJECT_ROOT / "notebooks/sr830_commissioning_sweeps.ipynb")
            .read_text(encoding="utf-8")
        )
        source = next("".join(cell["source"]) for cell in document["cells"]
                      if cell["cell_type"] == "code"
                      and "selection_manifest = {" in "".join(cell["source"]))
        tree = ast.parse(source)
        # Enable exports in the test without changing the notebook's safe default.
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "SAVE_OUTPUTS"
                for target in node.targets
            ):
                node.value = ast.Constant(True)
        code = compile(ast.fix_missing_locations(tree), "optional-export", "exec")
        for enabled in (("frequency",), ("excitation",), ("combined",),
                        ("frequency", "excitation", "combined")):
            for with_temperature in (False, True):
                with self.subTest(enabled=enabled, with_temperature=with_temperature):
                    with tempfile.TemporaryDirectory() as directory:
                        scope = self._optional_export_scope(directory, enabled)
                        if with_temperature:
                            scope.update(
                                temperature_excitation_rows=(object(),),
                                temperature_excitation_paths=(Path(directory) / "temperature.json",),
                                temperature_iv_figures={("xy", 2, "amplitude_v"): "temperature-figure"},
                                temperature_sample_status_widget=SimpleNamespace(value=("clean",)),
                                temperature_condition_widget=SimpleNamespace(value=("temperature::0",)),
                                temperature_current_minimum_a_rms=None,
                                temperature_current_maximum_a_rms=None,
                                temperature_iv_x_scale="linear",
                                temperature_iv_resolved_x_scales={"xy_h2_amplitude_v": "linear"},
                            )
                        exec(code, scope)
                        output = scope["OUTPUT_DIRECTORY"]
                        manifest = json.loads((output / "selection_manifest.json").read_text())
                        self.assertEqual(scope["export_commissioning_csv"].call_count, len(enabled))
                        stems = {"frequency": "frequency", "excitation": "excitation",
                                 "combined": "frequency_excitation"}
                        for name in enabled:
                            scope["export_commissioning_csv"].assert_any_call(
                                scope[name + "_rows"], output / (stems[name] + "_samples.csv")
                            )
                        if with_temperature:
                            scope["export_temperature_excitation_csv"].assert_called_once_with(
                                scope["temperature_excitation_rows"],
                                output / "temperature_excitation_samples.csv",
                            )
                            scope["export_publication_figure_set"].assert_called_once_with(
                                "temperature-figure", output / "temperature_iv_xy_h2_amplitude_v"
                            )
                            self.assertEqual(manifest["temperature_excitation"]["selected_rows"], 1)
                            self.assertEqual(manifest["temperature_excitation"]["x_scale"], "linear")
                        else:
                            scope["export_temperature_excitation_csv"].assert_not_called()
                            scope["export_publication_figure_set"].assert_not_called()
                            self.assertIsNone(manifest["temperature_excitation"])
                            self.assertNotIn("temperature_excitation_rows", scope)

    @staticmethod
    def _optional_export_scope(directory, enabled=()):
        from types import SimpleNamespace

        scope = {
            "PROJECT_ROOT": Path(directory), "Path": Path,
            "json": json, "asdict": Mock(return_value={}),
            "DATA_DIRECTORY": Path(directory),
            "TEMPERATURE_DATA_DIRECTORY": Path(directory),
            "RECORD_STATUSES": {"completed"}, "SAMPLE_STATUSES": {"clean"},
            "INCLUDE_REJECTED": False, "PHASE_MINIMUM_AMPLITUDE_V": 0.0,
            "PHASE_MAXIMUM_STANDARD_DEVIATION_DEG": 10.0,
            "SCALING_RULES": object(), "SCALING_PLOT_METHODS": ("scalar",),
            "harmonic_scaling_results": {}, "harmonic_scaling_results_by_run": {},
            "harmonic_scaling_figures": {}, "repeatability_summary": {},
            "FREQUENCY_EXCLUDED_TARGET_HZ": set(),
            "EXCITATION_EXCLUDED_SOURCE_V_RMS": set(),
            "COMBINED_EXCLUDED_FREQUENCIES_HZ": set(),
            "COMBINED_EXCLUDED_EXCITATIONS_V_RMS": set(),
            "export_commissioning_csv": Mock(),
            "export_temperature_excitation_csv": Mock(),
            "export_publication_figure_set": Mock(), "display": Mock(),
        }
        for name, value in (
            ("excitation_x_axis_widget", "current"),
            ("repeatability_metrics_widget", ("amplitude_v",)),
            ("repeatability_x_scale_widget", "linear"),
            ("frequency_baseline_widget", None),
            ("excitation_baseline_widget", None),
            ("combined_baseline_widget", None),
        ):
            scope[name] = SimpleNamespace(value=value)
        for name in ("frequency", "excitation", "combined"):
            scope[name + "_rows"] = (object(),) if name in enabled else ()
            scope[name + "_paths"] = (Path(directory) / (name + ".json"),)
        scope.update(frequency_figures={}, current_voltage_figures={},
                     combined_iv_figures={}, combined_phase_figures={})
        return scope

    def test_optional_export_includes_cached_condensed_report_figures_and_manifests(self) -> None:
        import tempfile
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from attodry_control.scientific_plotting import export_publication_figure_set

        document = json.loads(
            (PROJECT_ROOT / "notebooks/sr830_commissioning_sweeps.ipynb")
            .read_text(encoding="utf-8")
        )
        source = next("".join(cell["source"]) for cell in document["cells"]
                      if cell["cell_type"] == "code"
                      and "selection_manifest = {" in "".join(cell["source"]))
        tree = ast.parse(source)
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "SAVE_OUTPUTS"
                for target in node.targets
            ):
                node.value = ast.Constant(True)
        code = compile(ast.fix_missing_locations(tree), "report-export", "exec")
        with tempfile.TemporaryDirectory() as directory:
            scope = self._optional_export_scope(directory)
            keys = (("first/same.json", "xx", 1), ("first/same.json", "xy", 2),
                    ("second/same.json", "xy", 2))
            figures, manifests = {}, {}
            for key in keys:
                figure, axis = plt.subplots(figsize=(2, 1))
                axis.plot((1, 2), (3, 4), label="experiment")
                axis.plot((1, 2), (3.1, 3.9), label="fitting")
                figures[key] = figure
                plt.close(figure)  # The displayed report closes pyplot, retaining its Figure.
                manifests[key] = {"source_files": [key[0]], "channel": list(key[1:]),
                                  "fit_methods": ["complex"], "fit": {"exponent": 2.1},
                                  "phase_minimum_amplitude_v": 1e-8}
            merged_figures = {keys[0][0]: figures[keys[0]], keys[-1][0]: figures[keys[-1]]}
            merged_manifests = {
                name: {"source_files": [name], "amplitude_channels": [["xy", 2]],
                       "scalar_curve": "scalar_selected_free_model", "phase_mode": "none"}
                for name in merged_figures
            }
            frozen_merged = json.loads(json.dumps(merged_manifests))
            frozen_manifests = json.loads(json.dumps(list(manifests.values())))
            exporter = Mock(wraps=export_publication_figure_set)
            scope.update(report_figures=figures, report_manifests=manifests,
                         merged_report_figures=merged_figures, merged_report_manifests=merged_manifests,
                         export_publication_figure_set=exporter,
                         plot_experiment_fit_report=Mock(side_effect=AssertionError("Do not replot.")),
                         experiment_fit_report_manifest=Mock(side_effect=AssertionError("Do not relabel.")))
            # The existing export switch must still gate every output.
            exec(compile(source, "disabled-export", "exec"), scope)
            exporter.assert_not_called()
            self.assertFalse(scope["OUTPUT_DIRECTORY"].exists())
            exec(code, scope)
            output = scope["OUTPUT_DIRECTORY"]
            selection = json.loads((output / "selection_manifest.json").read_text())
            self.assertEqual(exporter.call_count, 5)
            self.assertEqual(len(selection["condensed_report"]), 5)
            stems = ("condensed_report_run01_xx_h1", "condensed_report_run01_xy_h2",
                     "condensed_report_run02_xy_h2")
            for key, stem, entry in zip(keys, stems, selection["condensed_report"]):
                exporter.assert_any_call(figures[key], output / stem)
                self.assertEqual(entry["layout"], "per_channel")
                self.assertEqual(entry["source_path"], key[0])
                self.assertEqual((entry["role"], entry["harmonic"]), key[1:])
                expected = {str(output / (stem + suffix)) for suffix in (".png", ".pdf", ".svg")}
                self.assertEqual(set(entry["figure_files"]), expected)
                for filename in expected:
                    self.assertGreater(Path(filename).stat().st_size, 100)
                saved = json.loads(Path(entry["manifest_file"]).read_text())
                self.assertEqual(saved, manifests[key])
            for index, (name, entry) in enumerate(
                zip(merged_figures, selection["condensed_report"][3:]), start=1
            ):
                stem = f"condensed_report_run{index:02d}_merged"
                exporter.assert_any_call(merged_figures[name], output / stem)
                self.assertEqual(entry["layout"], "merged")
                self.assertEqual(entry["source_path"], name)
                self.assertEqual(entry["amplitude_channels"], [["xy", 2]])
                expected = {str(output / (stem + suffix)) for suffix in (".png", ".pdf", ".svg")}
                self.assertEqual(set(entry["figure_files"]), expected)
                for filename in expected:
                    self.assertGreater(Path(filename).stat().st_size, 100)
                self.assertEqual(json.loads(Path(entry["manifest_file"]).read_text()), merged_manifests[name])
            self.assertEqual(merged_manifests, frozen_merged)
            self.assertEqual(list(manifests.values()), frozen_manifests)
            scope["plot_experiment_fit_report"].assert_not_called()
            scope["experiment_fit_report_manifest"].assert_not_called()
            # A retained figure with missing provenance must prompt a report rerun.
            exporter.reset_mock()
            del manifests[keys[-1]]
            with self.assertRaisesRegex(RuntimeError, "rerun.*Condensed report"):
                exec(code, scope)
            exporter.assert_not_called()
            manifests[keys[-1]] = frozen_manifests[-1]
            del merged_manifests[keys[-1][0]]
            with self.assertRaisesRegex(RuntimeError, "rerun.*Condensed report"):
                exec(code, scope)
            exporter.assert_not_called()

    def test_temperature_scale_controls_plot_existing_rows_and_export_actual_scale(self) -> None:
        try:
            import ipywidgets as widgets
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError as error:
            self.skipTest(str(error))

        document = json.loads(
            (PROJECT_ROOT / "notebooks" / "sr830_commissioning_sweeps.ipynb")
            .read_text(encoding="utf-8")
        )
        sources = ["".join(cell["source"]) for cell in document["cells"]
                   if cell["cell_type"] == "code"]
        selector_source = next(source for source in sources
                               if source.startswith("temperature_sample_status_widget ="))
        figure_source = next(source for source in sources
                             if source.startswith("temperature_iv_figures = ("))
        export_source = next(source for source in sources
                             if "selection_manifest = {" in source)
        manifest_assignment = next(
            node for node in ast.walk(ast.parse(export_source))
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "selection_manifest"
                    for target in node.targets)
        )
        temperature_manifest = next(
            value for key, value in zip(manifest_assignment.value.keys,
                                       manifest_assignment.value.values)
            if isinstance(key, ast.Constant) and key.value == "temperature_excitation"
        )
        self.assertIsInstance(temperature_manifest, ast.IfExp)
        temperature_manifest = temperature_manifest.body
        manifest_expression = compile(ast.Expression(temperature_manifest), "temperature-manifest", "eval")
        discovery = Mock(return_value=())
        loader = Mock(side_effect=AssertionError("Changing plot scale must not load data."))
        display = Mock()
        scope = {
            "widgets": widgets, "plt": plt, "Path": Path, "math": math,
            "TEMPERATURE_DATA_DIRECTORY": PROJECT_ROOT / "tests" / "temperature-scale-fixtures",
            "discover_temperature_excitation_records": discovery,
            "load_temperature_excitation_sample_files": loader,
            "display": display,
        }
        exec(selector_source, scope)
        control = scope["temperature_x_scale_widget"]
        self.assertEqual(tuple(value for _label, value in control.options), ("auto", "linear", "log"))
        self.assertEqual(control.value, "auto")
        self.assertIn(control, display.call_args.args[0].children)
        self.assertIsNone(eval(manifest_expression, scope)["x_scale"])

        rows = ((1e-6, 1e-6), (2e-6, 4e-6))

        def suite(selected_rows, *, x_scale):
            figure, axis = plt.subplots()
            self.addCleanup(plt.close, figure)
            axis.plot(*zip(*selected_rows))
            axis.set_xscale("log" if x_scale == "auto" else x_scale)
            return {("xy", 2, "amplitude_v"): figure}

        plot_suite = Mock(side_effect=suite)
        scope.update(temperature_excitation_rows=rows, plot_temperature_iv_suite=plot_suite)
        control.value = "linear"
        plot_suite.assert_not_called()
        exec(figure_source, scope)
        plot_suite.assert_called_once_with(rows, x_scale="linear")
        figure = scope["temperature_iv_figures"][("xy", 2, "amplitude_v")]
        self.assertEqual(figure.axes[0].get_xscale(), "linear")
        self.assertEqual(tuple(figure.axes[0].lines[0].get_xdata()), (1e-6, 2e-6))
        self.assertFalse(plt.fignum_exists(figure.number))
        self.assertIs(display.call_args.args[0], figure)

        # A changed control must not rewrite the settings of an existing figure
        # in the actual temperature section of the export manifest.
        control.value = "log"
        manifest = eval(manifest_expression, scope)
        self.assertEqual(manifest["x_scale"], "linear")
        self.assertEqual(manifest["resolved_x_scales"], {"xy_h2_amplitude_v": "linear"})
        self.assertEqual(plot_suite.call_count, 1)
        control.value = "auto"
        exec(figure_source, scope)
        manifest = eval(manifest_expression, scope)
        self.assertEqual(manifest["x_scale"], "auto")
        self.assertEqual(manifest["resolved_x_scales"], {"xy_h2_amplitude_v": "log"})

        previous_figures = scope["temperature_iv_figures"]
        control.value = "log"
        plot_suite.side_effect = ValueError("Log requires positive finite currents.")
        with self.assertRaisesRegex(ValueError, "positive finite"):
            exec(figure_source, scope)
        self.assertIs(scope["temperature_iv_figures"], previous_figures)
        self.assertEqual(eval(manifest_expression, scope)["x_scale"], "auto")
        scope["temperature_excitation_rows"] = ()
        exec(figure_source, scope)
        self.assertEqual(scope["temperature_iv_figures"], {})
        manifest = eval(manifest_expression, scope)
        self.assertIsNone(manifest["x_scale"])
        self.assertEqual(manifest["resolved_x_scales"], {})
        discovery.assert_called_once()
        loader.assert_not_called()

        for index, cell in enumerate(document["cells"]):
            if cell["cell_type"] == "code":
                compile("".join(cell["source"]), f"commissioning-cell-{index}", "exec")
                self.assertIsNone(cell["execution_count"])
                self.assertEqual(cell["outputs"], [])


if __name__ == "__main__":
    unittest.main()
