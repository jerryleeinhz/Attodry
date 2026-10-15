"""Synthetic controls for the Notebook-local, analysis-only progress panel."""

import ast
from dataclasses import replace
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import Mock, patch

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from attodry_control.commissioning_progress_analysis import ProgressSweepSnapshot


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _notebook_functions():
    document = json.loads((PROJECT_ROOT / "notebooks/sr830_commissioning_sweeps.ipynb")
                          .read_text(encoding="utf-8"))
    source = next("".join(cell["source"]) for cell in document["cells"]
                  if cell["cell_type"] == "code"
                  and "def build_notebook_progress_panel(" in "".join(cell["source"]))
    tree = ast.parse(source)
    # Import analysis helpers and define functions, without running the panel,
    # browsing files, executing any data/configuration cell or using a kernel.
    definitions = ast.Module(body=[node for node in tree.body
                                  if isinstance(node, (ast.Import, ast.ImportFrom,
                                                       ast.FunctionDef))], type_ignores=[])
    scope = {}
    exec(compile(definitions, "saved-progress-notebook-definitions", "exec"), scope)
    return scope


def _fake_widgets():
    class Widget:
        def __init__(self, children=(), **kwargs):
            self.__dict__.update(children=tuple(children), value=None, description="",
                                 disabled=False, options=(), callbacks=[], observers=[],
                                 clear_count=0)
            self.__dict__.update(kwargs)

        def __setattr__(self, name, value):
            old = getattr(self, name, None)
            self.__dict__[name] = value
            if name == "value" and old != value:
                for callback in tuple(self.observers):
                    callback({"name": name, "old": old, "new": value, "owner": self})

        def observe(self, callback, names="value"):
            self.observers.append(callback)

        def on_click(self, callback):
            self.callbacks.append(callback)

        def click(self):
            if not self.disabled:
                for callback in self.callbacks:
                    callback(self)

        def clear_output(self, wait=False):
            self.clear_count += 1

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    module = types.ModuleType("ipywidgets")
    for name in ("Dropdown", "Button", "Checkbox", "SelectMultiple", "HTML",
                 "Output", "HBox", "VBox"):
        setattr(module, name, Widget)
    module.Layout = lambda **kwargs: kwargs
    return module


def _row(point_index, frequency, excitation, *, statuses=("clean",)):
    return types.SimpleNamespace(point_index=point_index, target_frequency_hz=frequency,
                                 source_v_rms=excitation, nominal_current_a_rms=excitation / 1000,
                                 role="xy", harmonic=2, statuses=statuses)


class SavedProgressNotebookControlsTests(unittest.TestCase):
    def setUp(self):
        self.scope = _notebook_functions()
        self.path = (PROJECT_ROOT / "tests/synthetic_lockin_frequency_excitation_progress.jsonl").resolve()
        self.rows = tuple(_row(index, frequency, excitation)
                          for index, (frequency, excitation) in enumerate(
                              (f, u) for f in (170.0, 200.0) for u in (0.1, 0.2, 0.3)))
        self.snapshot = ProgressSweepSnapshot(
            path=self.path, scan_type="frequency_excitation", run_name="synthetic",
            points_total=6, completed_point_count=6, formal_pair_count=6,
            rows=self.rows, last_event="formal", last_event_unix_s=None,
            outcome=None, cleanup_verified=None, finished=False,
        )
        self.reader = Mock()
        self.reader.refresh.side_effect = lambda: self.snapshot
        self.factory = Mock(return_value=self.reader)
        self.scope["_NotebookProgressReader"] = self.factory
        self.scope["_notebook_discover_progress_records"] = Mock(return_value=(self.path,))
        self.plot = Mock(return_value=())
        self.fit = Mock(return_value={"figures": (), "summary_html": "synthetic fit"})
        self.addCleanup(plt.close, "all")
        with patch.dict(sys.modules, {"ipywidgets": _fake_widgets()}), patch("IPython.display.display"):
            self.panel = self.scope["build_notebook_progress_panel"](
                self.path.parent, plot_callback=self.plot, fit_callback=self.fit,
            )
        self.catalog, self.load, self.refresh = self.panel.children[2].children
        self.selector = self.panel.children[3]
        self.metric, self.axis, self.fit_switch = self.panel.children[4].children
        self.excluded_f, self.excluded_u = self.panel.children[5:7]
        self.apply, self.restore = self.panel.children[7].children
        self.status, self.selection_status, self.notice, self.plot_output, self.fit_status, self.fit_output = (
            self.panel.children[8:]
        )

    def _load(self):
        self.selector.value = str(self.path)
        self.load.click()

    def _plotted_rows(self):
        return self.plot.call_args.args[0].rows

    def test_catalog_and_selection_are_manual_and_fitting_needs_no_frequency_preselection(self):
        self.factory.assert_not_called()
        self.plot.assert_not_called()
        self.fit.assert_not_called()
        self.selector.value = str(self.path)
        self.factory.assert_not_called()
        self.load.click()
        self.reader.refresh.assert_called_once_with()
        self.assertEqual(self._plotted_rows(), self.rows)
        self.fit.assert_called_once_with(self.rows)
        self.assertFalse(self.fit_switch.disabled)
        self.assertTrue(self.fit_switch.value)
        self.assertIn("synthetic fit", self.fit_status.value)

    def test_apply_combines_frequency_and_excitation_exclusions_and_redraws_without_file_read(self):
        self._load()
        raw_snapshot = self.snapshot
        self.excluded_f.value = (170.0,)
        self.apply.click()
        self.assertEqual({row.target_frequency_hz for row in self._plotted_rows()}, {200.0})
        self.excluded_u.value = (0.2,)
        count = self.plot.call_count
        self.apply.click()
        expected = tuple(row for row in self.rows
                         if row.target_frequency_hz == 200.0 and row.source_v_rms != 0.2)
        self.assertEqual(self.plot.call_count, count + 1)
        self.assertEqual(self._plotted_rows(), expected)
        self.assertEqual(self.fit.call_args.args[0], expected)
        self.reader.refresh.assert_called_once_with()
        self.assertIs(self.snapshot, raw_snapshot)
        self.assertEqual(self.snapshot.rows, self.rows)
        self.assertIn("2 clean formal rows remain", self.selection_status.value)
        self.assertIn("Requested-frequency exclusions: 1", self.selection_status.value)
        self.assertIn("requested-excitation exclusions: 1", self.selection_status.value)
        self.assertIn("formal pairs: 6", self.status.value)

    def test_refresh_preserves_applied_exclusions_and_keeps_new_coordinates(self):
        self._load()
        self.excluded_f.value, self.excluded_u.value = (170.0,), (0.2,)
        self.apply.click()
        new_row = _row(6, 200.0, 0.4)
        self.snapshot = replace(self.snapshot, rows=self.rows + (new_row,))
        self.refresh.click()
        expected = tuple(row for row in self.snapshot.rows
                         if row.target_frequency_hz != 170.0 and row.source_v_rms != 0.2)
        self.assertEqual(self.reader.refresh.call_count, 2)
        self.assertEqual(self.excluded_f.value, (170.0,))
        self.assertEqual(self.excluded_u.value, (0.2,))
        self.assertEqual(self._plotted_rows(), expected)
        self.assertIn(new_row, self._plotted_rows())
        self.assertEqual(self.fit.call_args.args[0], expected)
        self.restore.click()
        self.assertEqual(self._plotted_rows(), self.snapshot.rows)
        self.assertEqual(self.excluded_f.value, ())
        self.assertEqual(self.excluded_u.value, ())
        self.assertEqual(self.reader.refresh.call_count, 2)

    def test_quality_filter_and_fit_toggle_apply_to_exact_displayed_selection(self):
        invalid = _row(6, 200.0, 0.4, statuses=("overload",))
        self.snapshot = replace(self.snapshot, rows=self.rows + (invalid,))
        self._load()
        self.assertNotIn(invalid, self._plotted_rows())
        self.assertNotIn(invalid, self.fit.call_args.args[0])
        fitted = self.fit.call_count
        self.fit_switch.value = False
        self.refresh.click()
        self.assertEqual(self.fit.call_count, fitted)
        self.assertEqual(self.fit_status.value, "")
        self.fit_switch.value = True
        self.refresh.click()
        self.assertEqual(self.fit.call_count, fitted + 1)
        self.assertEqual(self.fit.call_args.args[0], self._plotted_rows())

    def test_failed_refresh_clears_old_selection_curves_and_fit(self):
        self._load()
        cleared = self.plot_output.clear_count
        self.reader.refresh.side_effect = ValueError("invalid synthetic record")
        self.refresh.click()
        self.assertGreater(self.plot_output.clear_count, cleared)
        self.assertEqual(self.selection_status.value, "")
        self.assertEqual(self.fit_status.value, "")
        self.assertTrue(self.refresh.disabled)
        self.assertIn("No previous snapshot, curves or fits", self.status.value)


if __name__ == "__main__":
    unittest.main()
