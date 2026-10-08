import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from attodry_control import commissioning_progress_widgets as progress_widgets


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _fake_widgets():
    class Widget:
        def __init__(self, children=(), **kwargs):
            self.__dict__.update(
                children=tuple(children), value=None, description="", disabled=False,
                options=(), callbacks=[], observers=[], clear_count=0,
            )
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
    for name in ("Dropdown", "Button", "HTML", "Output", "HBox", "VBox"):
        setattr(module, name, Widget)
    module.Layout = lambda **kwargs: kwargs
    return module


def _snapshot(path, *, clean=True, current=1e-6, finished=False, outcome=None):
    row = types.SimpleNamespace(nominal_current_a_rms=current)
    return types.SimpleNamespace(
        path=path,
        scan_type="excitation",
        run_name="chosen <run>",
        points_total=10,
        completed_point_count=2,
        formal_pair_count=4,
        rows=(row,),
        clean_rows=(row,) if clean else (),
        quality_counts={
            "xx": {"total": 4, "clean": 3 if clean else 0, "excluded": 1 if clean else 4},
            "xy": {"total": 4, "clean": 2 if clean else 0, "excluded": 2 if clean else 4},
        },
        last_event="lockin_formal_sample",
        last_event_unix_s=1791453600.0,
        outcome=outcome,
        cleanup_verified=True if finished else None,
        finished=finished,
        current_point={
            "point_index": 2,
            "source_v_rms": 0.5,
            "source_readback_v_rms": 0.501,
            "actual_frequency_hz": 5000.0,
        },
    )


class CommissioningProgressWidgetsTests(unittest.TestCase):
    def setUp(self):
        self.directory = PROJECT_ROOT / "tests" / "progress-widget-fixtures"
        self.paths = tuple(self.directory / name for name in (
            "a_lockin_excitation_progress.jsonl", "b_lockin_frequency_progress.jsonl"
        ))
        self.snapshots = {path: _snapshot(path) for path in self.paths}
        self.instances = []
        self.failures = {}
        self.figures = []
        test = self

        class Reader:
            def __init__(self, path):
                self.path = path
                self.refresh_count = 0
                test.instances.append(self)

            def refresh(self):
                self.refresh_count += 1
                if self.path in test.failures:
                    raise test.failures[self.path]
                return test.snapshots[self.path]

        def plot(snapshot, **kwargs):
            figure, axis = plt.subplots()
            axis.plot([0, 1], [0, 1], label=kwargs["metric"])
            self.figures.append(figure)
            return (figure,)

        self.discover = self._patch(
            progress_widgets, "discover_progress_records", return_value=self.paths
        )
        self.reader_factory = self._patch(
            progress_widgets, "CommissioningProgressReader", side_effect=Reader
        )
        self.plot = self._patch(progress_widgets, "plot_progress_sweep", side_effect=plot)
        self.display = self._patch("IPython.display", "display")
        self.addCleanup(plt.close, "all")

    def _patch(self, target, name, **kwargs):
        if isinstance(target, str):
            patcher = patch(f"{target}.{name}", **kwargs)
        else:
            patcher = patch.object(target, name, **kwargs)
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def _build(self, *, real=False):
        if real:
            panel = progress_widgets.build_progress_panel(self.directory)
        else:
            with patch.dict(sys.modules, {"ipywidgets": _fake_widgets()}):
                panel = progress_widgets.build_progress_panel(
                    self.directory,
                    minimum_phase_amplitude_v=2e-6,
                    maximum_phase_standard_deviation_deg=4.0,
                )
        self.panel = panel
        self.catalog_button, self.load_button, self.refresh_button = panel.children[2].children
        self.selector = panel.children[3]
        self.metric, self.axis = panel.children[4].children
        self.status, self.notice, self.output = panel.children[5:]
        return panel

    def _load(self, index=0):
        self.selector.value = str(self.paths[index])
        self.load_button.click()

    def test_catalog_and_selection_do_not_read_or_plot_and_real_widgets_load_manually(self):
        import ipywidgets

        panel = self._build(real=True)
        self.assertIsInstance(panel, ipywidgets.VBox)
        self.assertIsNone(self.selector.value)
        self.assertTrue(self.load_button.disabled)
        self.assertTrue(self.refresh_button.disabled)
        self.reader_factory.assert_not_called()
        self.plot.assert_not_called()
        self.selector.value = str(self.paths[0])
        self.reader_factory.assert_not_called()
        self.load_button.click()
        self.assertEqual(len(self.instances), 1)
        self.assertEqual(self.instances[0].refresh_count, 1)
        self.assertIn(str(self.paths[0]), self.status.value)
        self.assertIn("chosen &lt;run&gt;", self.status.value)
        self.assertIn("Unfinished snapshot", self.status.value)
        self.assertIn("Completed points: 2 / 10", self.status.value)
        self.assertIn("formal pairs: 4", self.status.value)
        self.assertIn("lockin_xx (Vxx)</td><td>4</td><td>3</td><td>1", self.status.value)
        self.assertIn("lockin_xy (Vxy)</td><td>4</td><td>2</td><td>2", self.status.value)
        self.assertIn("No finish record observed", self.status.value)
        self.assertIn("Process and hardware state are unknown", self.status.value)
        self.assertIn("lockin_formal_sample", self.status.value)
        self.assertIn("Latest recorded point (historical snapshot): index 2", self.status.value)
        self.assertIn("Requested SINE OUT (V RMS): 0.5", self.status.value)
        self.assertIn("SINE OUT readback (V RMS): 0.501", self.status.value)
        self.assertIn("Actual frequency (Hz): 5000.0", self.status.value)
        self.assertFalse(plt.get_fignums())

        # Updating real Dropdown options can temporarily select None; the same
        # explicit file must retain its reader and displayed snapshot.
        self.catalog_button.click()
        self.assertEqual(self.selector.value, str(self.paths[0]))
        self.assertFalse(self.refresh_button.disabled)
        self.assertEqual(self.instances[0].refresh_count, 1)
        self.refresh_button.click()
        self.assertEqual(self.instances[0].refresh_count, 2)
        self.assertEqual(len(self.instances), 1)

    def test_fake_widgets_only_manual_refresh_applies_r_x_y_phase_and_axis(self):
        self._build()
        self._load()
        for count, metric in enumerate(("x_v", "y_v", "phase_deg"), start=2):
            with self.subTest(metric=metric):
                self.metric.value = metric
                self.assertEqual(self.instances[0].refresh_count, count - 1)
                self.assertIn("Plot settings changed", self.notice.value)
                self.refresh_button.click()
                self.assertEqual(self.instances[0].refresh_count, count)
                self.assertEqual(self.plot.call_args.kwargs["metric"], metric)
                self.assertEqual(self.plot.call_args.kwargs["minimum_phase_amplitude_v"], 2e-6)
                self.assertEqual(self.plot.call_args.kwargs["maximum_phase_standard_deviation_deg"], 4.0)
                self.assertFalse(plt.get_fignums())
        self.assertEqual(self.plot.call_args_list[0].kwargs["metric"], "amplitude_v")
        self.axis.value = "nominal_current_a_rms"
        self.assertEqual(self.instances[0].refresh_count, 4)
        self.refresh_button.click()
        self.assertEqual(self.plot.call_args.kwargs["x_axis"], "nominal_current_a_rms")
        self.assertEqual(len(self.instances), 1)
        self.catalog_button.click()
        self.assertEqual(self.instances[0].refresh_count, 5)

    def test_changing_files_clears_snapshot_and_uses_new_reader_when_reselected(self):
        self._build()
        self._load()
        cleared = self.output.clear_count
        self.selector.value = str(self.paths[1])
        self.assertGreater(self.output.clear_count, cleared)
        self.assertTrue(self.refresh_button.disabled)
        self.assertNotIn("chosen", self.status.value)
        self.assertEqual(len(self.instances), 1)
        self.load_button.click()
        self.assertEqual(self.instances[1].path, self.paths[1])
        self._load(0)
        self.assertEqual(len(self.instances), 3)
        self.assertEqual(self.instances[2].refresh_count, 1)

    def test_failed_incremental_read_clears_old_data_and_retry_loads_fresh_reader(self):
        self._build()
        self._load()
        cleared = self.output.clear_count
        self.failures[self.paths[0]] = ValueError("invalid <record>")
        self.refresh_button.click()
        self.assertGreater(self.output.clear_count, cleared)
        self.assertTrue(self.refresh_button.disabled)
        self.assertIn("Progress load failed", self.status.value)
        self.assertIn("invalid &lt;record&gt;", self.status.value)
        self.assertIn("No previous snapshot or curves", self.status.value)
        self.assertNotIn("chosen", self.status.value)
        self.assertEqual(self.plot.call_count, 1)
        del self.failures[self.paths[0]]
        self.load_button.click()
        self.assertEqual(len(self.instances), 2)
        self.assertEqual(self.instances[1].refresh_count, 1)

    def test_failed_new_file_load_does_not_retain_previously_loaded_snapshot(self):
        self._build()
        self._load()
        self.failures[self.paths[1]] = OSError("file disappeared")
        self._load(1)
        self.assertIn(str(self.paths[1]), self.status.value)
        self.assertIn("file disappeared", self.status.value)
        self.assertNotIn("chosen", self.status.value)
        self.assertTrue(self.refresh_button.disabled)
        self.assertFalse(plt.get_fignums())

    def test_missing_current_explains_voltage_option_without_recomputing(self):
        self.snapshots[self.paths[0]] = _snapshot(self.paths[0], current=None)
        self._build()
        self._load()
        self.assertEqual(self.plot.call_count, 1)
        self.axis.value = "nominal_current_a_rms"
        self.refresh_button.click()
        self.assertEqual(self.plot.call_count, 1)
        self.assertIn("Archived estimated current is missing", self.notice.value)
        self.assertIn("Select SINE OUT readback", self.notice.value)
        self.assertIn("never recalculated", self.notice.value)
        self.assertIn("Completed points: 2 / 10", self.status.value)
        self.axis.value = "sine_output_v_rms"
        self.refresh_button.click()
        self.assertEqual(self.plot.call_count, 2)

    def test_no_clean_samples_has_counts_and_explanation(self):
        self.snapshots[self.paths[0]] = _snapshot(self.paths[0], clean=False)
        self._build()
        self._load()
        self.plot.assert_not_called()
        self.assertIn("No clean selected formal samples", self.notice.value)
        self.assertIn("lockin_xx (Vxx)</td><td>4</td><td>0</td><td>4", self.status.value)
        self.assertFalse(self.refresh_button.disabled)

    def test_missing_recorded_point_context_is_explicit(self):
        self.snapshots[self.paths[0]].current_point = None
        self._build()
        self._load()
        self.assertIn("index not recorded", self.status.value)
        self.assertIn("Requested SINE OUT (V RMS): not recorded", self.status.value)
        self.assertIn("SINE OUT readback (V RMS): not recorded", self.status.value)
        self.assertIn("Actual frequency (Hz): not recorded", self.status.value)

    def test_finish_outcome_and_cleanup_are_historical_and_rejected_data_is_not_plotted(self):
        self._build()
        for outcome in ("completed", "rejected", "interrupted"):
            with self.subTest(outcome=outcome):
                self.snapshots[self.paths[0]] = _snapshot(
                    self.paths[0], finished=True, outcome=outcome
                )
                self.plot.reset_mock()
                self._load()
                self.assertIn("Finished historical snapshot", self.status.value)
                self.assertIn(f"<b>{outcome}</b>", self.status.value)
                self.assertIn("recorded cleanup verified: <b>true</b>", self.status.value)
                self.assertIn("do not confirm current process or hardware state", self.status.value)
                if outcome == "completed":
                    self.plot.assert_called_once()
                else:
                    self.plot.assert_not_called()
                    self.assertIn("excluded from default progress plots", self.notice.value)
                    self.assertIn("explicit audit selection", self.notice.value)

    def test_empty_metric_and_plot_failure_clear_curves_and_explain(self):
        self._build()
        self._load()
        self.plot.side_effect = None
        self.plot.return_value = ()
        self.metric.value = "phase_deg"
        self.refresh_button.click()
        self.assertIn("No curves are available", self.notice.value)
        self.assertIn("circular-spread thresholds", self.notice.value)
        self.plot.side_effect = ValueError("no matching channel")
        cleared = self.output.clear_count
        self.refresh_button.click()
        self.assertGreater(self.output.clear_count, cleared)
        self.assertIn("Plot failed", self.notice.value)
        self.assertIn("No previous curves are retained", self.notice.value)
        self.assertIn("Completed points: 2 / 10", self.status.value)

    def test_removed_selected_file_and_catalog_failure_clear_old_snapshot(self):
        self._build()
        self._load()
        self.discover.return_value = (self.paths[1],)
        self.catalog_button.click()
        self.assertIsNone(self.selector.value)
        self.assertTrue(self.refresh_button.disabled)
        self.assertNotIn("chosen", self.status.value)
        self.discover.side_effect = OSError("unreadable directory")
        self.catalog_button.click()
        self.assertIn("Catalog refresh failed", self.status.value)

    def test_notebook_has_thin_manual_progress_cell_and_all_code_compiles(self):
        notebook = json.loads((PROJECT_ROOT / "notebooks" / "sr830_commissioning_sweeps.ipynb").read_text(encoding="utf-8"))
        cell = next(cell for cell in notebook["cells"] if cell.get("id") == "commissioning-progress-panel")
        source = "".join(cell["source"])
        self.assertIn("build_progress_panel(", source)
        self.assertIn("DATA_DIRECTORY", source)
        self.assertNotIn("lockin_test", source)
        self.assertNotIn("Timer", source)
        self.assertNotIn("latest", source)
        for index, current in enumerate(notebook["cells"]):
            if current["cell_type"] == "code":
                compile("".join(current["source"]), f"notebook-cell-{index}", "exec")
                self.assertEqual(current["outputs"], [])
                self.assertIsNone(current["execution_count"])


if __name__ == "__main__":
    unittest.main()
