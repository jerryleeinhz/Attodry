"""Filter-driven curves and identifiable, stable manual exclusions; offline only."""
from contextlib import closing, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from attodry_control.combination_store import CombinationStore, encode
from attodry_control.unified_plotting import UnifiedPlotDashboard, _sample_token, render_plot


CURRENT = "actual.excitation_current_rms_a"
FREQUENCY = "actual.excitation_frequency_hz"
TARGET_FREQUENCY = "requested.excitation_frequency_hz"
SIGNAL = "measured.signal"


class UnifiedFilterStackTests(unittest.TestCase):
    def test_condition_labels_preserve_distinguishing_precision_without_current_conversion_noise(self):
        with redirect_stdout(io.StringIO()):
            dashboard, _ = self.dashboard()
        first = {"run_id": "run-one", "condition_id": "a", TARGET_FREQUENCY: 100., CURRENT: 1e-7}
        second = {**first, "condition_id": "b", TARGET_FREQUENCY: 100.000001}
        label = dashboard._exclusion_label(second, (TARGET_FREQUENCY, CURRENT))
        self.assertIn("100.000001 Hz", label)
        self.assertIn("0.1 µA RMS", label)
        self.assertNotEqual(dashboard._exclusion_label(first, (TARGET_FREQUENCY, CURRENT)), label)

    def setUp(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        self.addCleanup(plt.close, "all")
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.database = self.directory / "scan.sqlite"
        with CombinationStore(self.database):
            pass
        with closing(sqlite3.connect(self.database)) as connection, connection:
            self._insert_run(connection, "run-one", (150, 200, 250, 300))
            self._insert_run(connection, "run-two", (150,))
        self.dashboards = []
        self.addCleanup(self._close_dashboards)

    def _close_dashboards(self):
        for dashboard in self.dashboards:
            for card in list(dashboard.cards):
                dashboard._remove_card(card)
            dashboard.widget.close()

    def _insert_run(self, connection, run_id, frequencies):
        connection.execute("INSERT INTO combination_runs VALUES(?,?,?,?,?,?,?)",
            (run_id, 1, "{}", "2026-11-13T15:00:00Z", "completed",
             encode({"clean": True}), None))
        for index, (frequency, current) in enumerate(
                (frequency, current) for frequency in frequencies for current in (1e-7, 3e-7)):
            condition = f"condition-{index:06d}"
            context = {"sequence_index": index, "repeat_index": 0,
                "loop_order": ["excitation"], "axes": {"excitation": {"index": index}},
                "requested": {"excitation_current_rms_a": current,
                              "excitation_frequency_hz": frequency}}
            connection.execute("INSERT INTO combination_conditions VALUES(?,?,?,?)",
                               (run_id, condition, index, encode(context)))
            connection.execute("INSERT INTO combination_attempts VALUES(?,?,?,?,?,?,?)",
                               (run_id, condition, 0, "accepted", None, "start", "finish"))
            payload = {"clean": True, "simulated": True, "started_at_utc": "start", "finished_at_utc": "finish", "actual": {
                "excitation_current_rms_a": current, "excitation_frequency_hz": frequency},
                "measurements": {"signal": frequency * current}, "reads": []}
            connection.execute("INSERT INTO combination_samples VALUES(?,?,?,?,?)",
                               (run_id, condition, 0, 0, encode(payload)))

    def dashboard(self, *, runs=("run-one",), mode="curve", saved=None):
        dashboard = UnifiedPlotDashboard(self.directory)
        self.dashboards.append(dashboard)
        dashboard.add_plot(mode, saved)
        card = dashboard.cards[-1]
        if saved is None:
            card["sources"]._controls[str(self.database)].value = True
            card["runs"].value = tuple((str(self.database), run) for run in runs)
            card["x"].value = CURRENT
            if mode != "field":
                card["y"].value = SIGNAL
        return dashboard, card

    def add_frequency_filter(self, dashboard, card, values=(150, 200, 250, 300), *, stack=False):
        dashboard._add_filter(card, FREQUENCY, values, stack=stack)
        return card["filters"][-1]

    def select_first_condition(self, dashboard, card):
        row = next(row for row in dashboard._selected_rows(card)
                   if row["run_id"] == "run-one" and row["condition_id"] == "condition-000000")
        token = _sample_token(row)
        card["excluded"].value = (token,)
        return row, token

    def test_filter_stack_checkbox_reproduces_four_frequency_curves(self):
        with redirect_stdout(io.StringIO()):
            dashboard, card = self.dashboard()
            row = self.add_frequency_filter(dashboard, card)
            self.assertFalse(row["stack"].value)
            self.assertEqual(row["choices"].value, (150, 200, 250, 300))
            dashboard._render_card(card)
            self.assertIsNone(card["rendered"])
            row["stack"].value = True
            self.assertEqual(dashboard._card_spec(card)["stack_filters"], [FREQUENCY])
            dashboard._render_card(card)
            self.assertIsNotNone(card["rendered"])
            figure = card["figure"]
            self.assertEqual(len(figure.axes[0].lines), 4)
            self.assertTrue(all(len(line.get_xdata()) == 2 for line in figure.axes[0].lines))
            legend = " ".join(text.get_text() for text in figure.axes[0].get_legend().get_texts())
            for frequency in (150, 200, 250, 300):
                self.assertIn(str(frequency), legend)
            row["choices"].value = (150, 250)
            self.assertIsNone(card["rendered"])
            dashboard._render_card(card)
            self.assertEqual(len(card["figure"].axes[0].lines), 2)

    def test_exclusion_labels_show_run_frequency_current_and_formal_identity(self):
        with redirect_stdout(io.StringIO()):
            dashboard, card = self.dashboard(runs=("run-one", "run-two"))
            first = next(row for row in dashboard._selected_rows(card)
                         if row["run_id"] == "run-one" and row["condition_id"] == "condition-000000")
            token = _sample_token(first)
            label = dict((value, label) for label, value in card["excluded"].options)[token]
            for text in ("run-one", "150", "Hz", "RMS", "condition-000000", "attempt", "sample"):
                self.assertIn(text, label)
            twin = next(row for row in dashboard._selected_rows(card)
                        if row["run_id"] == "run-two" and row["condition_id"] == "condition-000000")
            self.assertNotEqual(_sample_token(twin), token)
            self.assertIn("run-two", dict((value, label) for label, value in card["excluded"].options)[_sample_token(twin)])

    def test_multiple_filter_checkboxes_stack_only_recorded_condition_combinations(self):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            for condition, raw_context in connection.execute(
                    "SELECT condition_id,context_json FROM combination_conditions WHERE run_id='run-one'").fetchall():
                context = json.loads(raw_context)
                context["requested"]["gate_bottom_v"] = int(condition.rsplit("-", 1)[-1]) % 2
                connection.execute("UPDATE combination_conditions SET context_json=? WHERE run_id='run-one' AND condition_id=?",
                                   (encode(context), condition))
        with redirect_stdout(io.StringIO()):
            dashboard, card = self.dashboard()
            self.add_frequency_filter(dashboard, card, stack=True)
            gate = "requested.gate_bottom_v"
            dashboard._add_filter(card, gate, (0, 1), stack=True)
            self.assertEqual(dashboard._card_spec(card)["stack_filters"], [FREQUENCY, gate])
            dashboard._render_card(card)
            self.assertIsNotNone(card["rendered"])
            self.assertEqual(len(card["figure"].axes[0].lines), 8)
            self.assertTrue(all(len(line.get_xdata()) == 1 for line in card["figure"].axes[0].lines))

    def test_excluding_one_frequency_condition_keeps_same_current_other_frequency_and_run(self):
        with redirect_stdout(io.StringIO()):
            dashboard, card = self.dashboard(runs=("run-one", "run-two"))
            self.add_frequency_filter(dashboard, card, stack=True)
            _, token = self.select_first_condition(dashboard, card)
            figure, result = render_plot(dashboard._selected_rows(card), dashboard._card_spec(card))
            self.assertNotIn(token, {_sample_token(row) for row in result["rows"]})
            self.assertTrue(any(row["run_id"] == "run-one" and row[FREQUENCY] == 200
                                and row[CURRENT] == 1e-7 for row in result["rows"]))
            self.assertTrue(any(row["run_id"] == "run-two" and row[FREQUENCY] == 150
                                and row[CURRENT] == 1e-7 for row in result["rows"]))
            self.assertEqual(result["report"]["manual_excluded_sample_ids"], [token])

    def test_filter_hides_rows_without_forgetting_previous_exclusion(self):
        with redirect_stdout(io.StringIO()):
            dashboard, card = self.dashboard()
            filter_row = self.add_frequency_filter(dashboard, card, stack=True)
            _, token = self.select_first_condition(dashboard, card)
            filter_row["choices"].value = (200,)
            spec = dashboard._card_spec(card)
            self.assertIn(token, spec["excluded_sample_ids"])
            self.assertIn("1", card["exclusion_status"].value)
            visible = {_sample_token(row) for row in dashboard._selected_rows(card) if row[FREQUENCY] == 200}
            option_ids = {value for _, value in card["excluded"].options}
            self.assertTrue(option_ids <= visible | {token})
            self.assertTrue(visible <= option_ids)
            filter_row["choices"].value = (150, 200, 250, 300)
            self.assertIn(token, dashboard._card_spec(card)["excluded_sample_ids"])
            _, result = render_plot(dashboard._selected_rows(card), dashboard._card_spec(card))
            self.assertEqual(len(result["rows"]), 7)

    def test_saved_setup_restores_stack_filters_and_hidden_exclusions(self):
        with redirect_stdout(io.StringIO()):
            dashboard, card = self.dashboard()
            filter_row = self.add_frequency_filter(dashboard, card, stack=True)
            _, token = self.select_first_condition(dashboard, card)
            filter_row["choices"].value = (200,)
            dashboard.configuration_path.value = str(self.directory / "stack-setup.json")
            dashboard._save_setup()
            payload = json.loads(Path(dashboard.configuration_path.value).read_text(encoding="utf-8"))
            self.assertEqual(payload["plots"][0]["stack_filters"], [FREQUENCY])
            self.assertEqual(payload["plots"][0]["excluded_sample_ids"], [token])
            dashboard._load_setup()
            loaded = dashboard.cards[0]
            self.assertTrue(loaded["filters"][0]["stack"].value)
            self.assertEqual(loaded["filters"][0]["choices"].value, (200,))
            self.assertEqual(dashboard._card_spec(loaded)["excluded_sample_ids"], [token])
            loaded["filters"][0]["choices"].value = (150, 200, 250, 300)
            dashboard._render_card(loaded)
            self.assertIsNotNone(loaded["rendered"])
            self.assertEqual(loaded["rendered"]["data"]["report"]["plotted_row_count"], 7)

    def test_legacy_setup_keeps_existing_single_group_and_no_implicit_filter_stack(self):
        saved = {"source_paths": [str(self.database)], "run_ids_by_source": {str(self.database): ["run-one"]},
                 "x": CURRENT, "y": SIGNAL, "group_by": TARGET_FREQUENCY,
                 "filters": {FREQUENCY: [150, 200]}, "statistics": "raw"}
        with redirect_stdout(io.StringIO()):
            dashboard, card = self.dashboard(saved=saved)
            self.assertFalse(card["filters"][0]["stack"].value)
            self.assertEqual(card["group"].value, TARGET_FREQUENCY)
            self.assertEqual(dashboard._card_spec(card).get("stack_filters", []), [])
            dashboard._render_card(card)
            self.assertIsNotNone(card["rendered"])
            self.assertEqual(len(card["figure"].axes[0].lines), 2)

    def test_filter_stack_is_disabled_for_maps_and_heatmaps(self):
        for mode in ("xy_z", "heatmap"):
            with self.subTest(mode=mode), redirect_stdout(io.StringIO()):
                dashboard, card = self.dashboard(mode=mode)
                row = self.add_frequency_filter(dashboard, card)
                self.assertTrue(row["stack"].disabled)
                self.assertFalse(row["stack"].value)
                self.assertEqual(dashboard._card_spec(card).get("stack_filters", []), [])

    def test_malformed_saved_stacking_is_rejected_before_adding_a_card(self):
        cases = ((FREQUENCY, "curve"), (None, "curve"),
                 (["requested.not_filtered"], "curve"), ([FREQUENCY, 3], "curve"),
                 ([FREQUENCY], "xy_z"), ([FREQUENCY], "heatmap"))
        with redirect_stdout(io.StringIO()):
            dashboard, card = self.dashboard()
            for stacked, mode in cases:
                saved = {"source_paths": [str(self.database)],
                         "run_ids_by_source": {str(self.database): ["run-one"]},
                         "x": CURRENT, "y": SIGNAL, "mode": mode,
                         "filters": {FREQUENCY: [150, 200]}, "stack_filters": stacked}
                with self.subTest(stacked=stacked, mode=mode), self.assertRaises(ValueError):
                    dashboard.add_plot(mode, saved)
                self.assertEqual(dashboard.cards, [card])

    def test_malformed_setup_load_preserves_existing_cards_and_reports_error(self):
        cases = ((FREQUENCY, "curve"), (None, "curve"),
                 (["requested.not_filtered"], "curve"), ([FREQUENCY, 3], "curve"),
                 ([FREQUENCY], "xy_z"), ([FREQUENCY], "heatmap"))
        with redirect_stdout(io.StringIO()):
            dashboard, card = self.dashboard()
            self.add_frequency_filter(dashboard, card, stack=True)
            for index, (stacked, mode) in enumerate(cases):
                payload = dashboard._configuration()
                payload["plots"][0].update(stack_filters=stacked, mode=mode)
                path = self.directory / f"malformed-setup-{index}.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                dashboard.configuration_path.value = str(path)
                with self.subTest(stacked=stacked, mode=mode):
                    dashboard._load_setup()
                    self.assertEqual(dashboard.cards, [card])
                    self.assertIn("stack", dashboard.status.value.lower())

    def test_null_exclusion_metadata_is_shown_as_not_recorded(self):
        with redirect_stdout(io.StringIO()):
            dashboard, _ = self.dashboard()
            label = dashboard._exclusion_label({"source_path": str(self.database), "run_id": None,
                "condition_id": None, "attempt_index": None, "sample_index": None,
                CURRENT: None, FREQUENCY: None}, (CURRENT, FREQUENCY))
            self.assertNotIn("None", label)
            self.assertGreaterEqual(label.count("not recorded"), 6)


if __name__ == "__main__":
    unittest.main()
