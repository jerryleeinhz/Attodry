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
