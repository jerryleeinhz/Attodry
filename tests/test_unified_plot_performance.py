"""Cached notebook interactions preserve data and avoid repeated full-row copies."""
from contextlib import closing, redirect_stdout
import copy
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from attodry_control.combination_analysis import load_combination_snapshot
from attodry_control.combination_store import CombinationStore, encode
from attodry_control.unified_plotting import UnifiedPlotDashboard, _filter_rows, render_plot


X = "actual.field_x_t"
Y = "measured.signal"
CURRENT = "requested.excitation_current_rms_a"
FREQUENCY = "requested.excitation_frequency_hz"


class UnifiedPlotPerformanceTests(unittest.TestCase):
    def setUp(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        self.addCleanup(plt.close, "all")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.path = self.directory / "scan.sqlite"
        with CombinationStore(self.path):
            pass
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO combination_runs VALUES(?,?,?,?,?,?,?)",
                       ("run", 1, "{}", "2026-11-16T00:00:00Z", "completed",
                        encode({"clean": True}), None))
            for index, (frequency, current, field) in enumerate(
                    (f, i, b) for f in (450., 600.) for i in (1e-7, 3e-7) for b in (-1., 1.)):
                condition_id = str(index)
                context = {"sequence_index": index, "repeat_index": 0,
                           "loop_order": ["magnetic", "excitation"],
                           "axes": {"magnetic": {"direction": "forward", "index": index // 2}},
                           "requested": {"field_x_t": field,
                                         "excitation_current_rms_a": current,
                                         "excitation_frequency_hz": frequency}}
                payload = {"clean": True, "simulated": True,
                           "started_at_utc": "start", "finished_at_utc": "finish",
                           "actual": {"field_x_t": field},
                           "measurements": {"signal": index + 1.},
                           "reads": [{"module": "lockin", "captured_at_utc": "sample",
                                      "status": {"marker": "original", "raw": [{"value": 1}]}}]}
                db.execute("INSERT INTO combination_conditions VALUES(?,?,?,?)",
                           ("run", condition_id, index, encode(context)))
                db.execute("INSERT INTO combination_attempts VALUES(?,?,?,?,?,?,?)",
                           ("run", condition_id, 0, "accepted", None, "start", "finish"))
                db.execute("INSERT INTO combination_samples VALUES(?,?,?,?,?)",
                           ("run", condition_id, 0, 0, encode(payload)))
        self.dashboards = []
        self.addCleanup(self.close_dashboards)

    def close_dashboards(self):
        for dashboard in self.dashboards:
            for card in list(dashboard.cards):
                dashboard._remove_card(card)
            dashboard.widget.close()

    def dashboard(self):
        dashboard = UnifiedPlotDashboard(self.directory)
        self.dashboards.append(dashboard)
        dashboard.add_plot("curve", {"source_paths": [str(self.path)],
                                   "run_ids_by_source": {str(self.path): ["run"]},
                                   "x": X, "y": Y, "group_by": CURRENT,
                                   "filters": {FREQUENCY: [450.]}})
        return dashboard, dashboard.cards[-1]

    def test_presentation_changes_only_invalidate_their_plot(self):
        with redirect_stdout(io.StringIO()):
            dashboard, card = self.dashboard()
            controls = (("title", "Updated title"), ("show_legend", False),
                        ("curve_style", "points"), ("x_scale", "log"),
                        ("y_scale", "log"), ("statistics", "mean_sem"),
                        ("quality", "include"), ("component", "x_v"))
            with patch.object(dashboard, "_update_card_options", wraps=dashboard._update_card_options) as update, \
                    patch.object(dashboard, "_selected_rows", wraps=dashboard._selected_rows) as selected:
                for key, value in controls:
                    card["rendered"] = {"previous": True}
                    card["image"].value = b"old plot"
                    card[key].value = value
                    self.assertIsNone(card["rendered"], key)
                    self.assertEqual(card["image"].value, b"", key)
                self.assertEqual(update.call_count, 0)
                self.assertEqual(selected.call_count, 0)

    def test_full_option_update_reads_rows_once_with_multiple_filters(self):
        with redirect_stdout(io.StringIO()):
            dashboard, card = self.dashboard()
            dashboard._add_filter(card, CURRENT, (1e-7, 3e-7))
            with patch.object(dashboard, "_selected_rows", wraps=dashboard._selected_rows) as selected:
                dashboard._update_card_options(card)
            self.assertEqual(selected.call_count, 1)
            self.assertIs(selected.call_args.kwargs.get("copy_rows"), False)
            self.assertEqual(len(card["excluded"].options), 4)

    def test_filter_changes_reuse_snapshot_and_do_not_copy_nested_payloads(self):
        with redirect_stdout(io.StringIO()):
            dashboard, card = self.dashboard()
            original_deepcopy = copy.deepcopy
            copied_formal_rows = []

            def tracked(value, *args, **kwargs):
                if isinstance(value, dict) and "status.lockin" in value:
                    copied_formal_rows.append(value)
                return original_deepcopy(value, *args, **kwargs)

            with patch("attodry_control.unified_plotting.load_combination_snapshot",
                       wraps=load_combination_snapshot) as loader, \
                    patch("attodry_control.unified_plotting.copy.deepcopy", side_effect=tracked), \
                    patch.object(dashboard, "_update_card_options", wraps=dashboard._update_card_options) as options:
                card["filters"][0]["choices"].value = (600.,)
                card["group"].value = FREQUENCY
                card["x"].value = CURRENT
                self.assertEqual(loader.call_count, 0)
                self.assertEqual(options.call_count, 0)
                self.assertEqual(copied_formal_rows, [])
            self.assertEqual(len(card["excluded"].options), 4)
            self.assertTrue(all("600" in label for label, _ in card["excluded"].options))

    def test_default_rows_remain_detached_and_rendered_rows_cannot_mutate_other_cards(self):
        with redirect_stdout(io.StringIO()):
            dashboard, first = self.dashboard()
            saved = dashboard._card_spec(first)
            saved["source_paths"] = [str(self.path)]
            dashboard.add_plot("curve", saved)
            second = dashboard.cards[-1]
            rows = dashboard._selected_rows(first)
            rows[0]["status.lockin"]["raw"][0]["value"] = -1
            self.assertEqual(dashboard._selected_rows(first)[0]["status.lockin"]["raw"][0]["value"], 1)
            dashboard._render_card(first)
            self.assertIsNotNone(first["rendered"])
            first["rendered"]["data"]["rows"][0]["status.lockin"]["raw"][0]["value"] = -2
            self.assertEqual(dashboard._selected_rows(second)[0]["status.lockin"]["raw"][0]["value"], 1)
            dashboard._render_card(second)
            self.assertTrue(all(row["status.lockin"]["raw"][0]["value"] == 1
                                for row in second["rendered"]["data"]["rows"]))

    def test_cached_render_matches_detached_selection_statistics_and_exclusions(self):
        with redirect_stdout(io.StringIO()):
            dashboard, card = self.dashboard()
            card["excluded"].value = (card["excluded"].options[0][1],)
            card["statistics"].value = "mean_sd"
            detached = dashboard._selected_rows(card)
            expected_figure, expected = render_plot(detached, dashboard._card_spec(card))
            dashboard._render_card(card)
            self.assertIsNotNone(card["rendered"])
            self.assertEqual(card["rendered"]["data"], expected)
            self.assertNotIn("_manual_excluded", dashboard._selected_rows(card)[0])
            dashboard._close_figure(expected_figure)

    def test_refresh_replaces_cached_observations_and_retains_selection(self):
        with redirect_stdout(io.StringIO()):
            dashboard, card = self.dashboard()
            before = dashboard._selected_rows(card)
            with closing(sqlite3.connect(self.path)) as db, db:
                payload = json.loads(db.execute("SELECT payload_json FROM combination_samples "
                                                "WHERE condition_id='0'").fetchone()[0])
                payload["measurements"]["signal"] = 99.
                db.execute("UPDATE combination_samples SET payload_json=? WHERE condition_id='0'",
                           (encode(payload),))
            self.assertEqual(dashboard._selected_rows(card), before)
            dashboard._refresh()
            self.assertEqual(card["runs"].value, ((str(self.path), "run"),))
            self.assertEqual(dashboard._selected_rows(card)[0][Y], 99.)
            self.assertIsNone(card["rendered"])

    def test_static_csv_still_requires_refresh_after_file_change(self):
        csv_path = self.directory / "static.csv"
        csv_path.write_text("x,y\n1,2\n2,4\n", encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            dashboard = UnifiedPlotDashboard(self.directory)
            self.dashboards.append(dashboard)
            dashboard.add_plot("curve", {"source_paths": [str(csv_path)], "x": "x", "y": "y"})
            card = dashboard.cards[-1]
            dashboard._render_card(card)
            self.assertIsNotNone(card["rendered"])
            csv_path.write_text("x,y\n1,200\n2,400\n3,600\n", encoding="utf-8")
            dashboard._render_card(card)
            self.assertIsNone(card["rendered"])
            dashboard._refresh()
            dashboard._render_card(card)
            self.assertIsNotNone(card["rendered"])
            self.assertEqual(card["rendered"]["data"]["report"]["plotted_row_count"], 3)

    def test_exclusion_count_updates_immediately_on_select_hide_and_clear(self):
        with redirect_stdout(io.StringIO()):
            dashboard, card = self.dashboard()
            self.assertIn("4 observations", card["exclusion_status"].value)
            self.assertIn("0 excluded", card["exclusion_status"].value)
            token = card["excluded"].options[0][1]
            card["excluded"].value = (token,)
            self.assertIn("1 excluded", card["exclusion_status"].value)
            card["filters"][0]["choices"].value = (600.,)
            self.assertIn("1 excluded observations outside", card["exclusion_status"].value)
            self.assertIn(token, card["excluded_ids"])
            card["filters"][0]["choices"].value = (450.,)
            self.assertEqual(card["excluded"].value, (token,))
            card["excluded"].value = ()
            self.assertIn("0 excluded", card["exclusion_status"].value)
            self.assertNotIn("outside", card["exclusion_status"].value)
            self.assertEqual(card["excluded_ids"], ())

    def test_fast_filter_preserves_historical_exact_equality(self):
        nan = float("nan")
        large = 2 ** 60
        shared_list = [nan]
        rows = [
            {"id": "missing"}, {"id": "none", "value": None},
            {"id": "true", "value": True}, {"id": "one", "value": 1},
            {"id": "one-float", "value": 1.}, {"id": "false", "value": False},
            {"id": "zero", "value": 0}, {"id": "text", "value": "1"},
            {"id": "large", "value": large}, {"id": "next-large", "value": large + 1},
            {"id": "large-float", "value": float(large)},
            {"id": "dict", "value": {"x": 1}}, {"id": "list", "value": [1, 2]},
            {"id": "nan", "value": nan}, {"id": "other-nan", "value": float("nan")},
            {"id": "nested-nan", "value": shared_list},
            {"id": "inf", "value": float("inf")},
        ]
        cases = [None, {}, {"value": []}, {"value": [None]}, {"value": [True]},
                 {"value": [1.]}, {"value": [False]}, {"value": ["1"]},
                 {"value": [large + 1]}, {"value": [float(large)]},
                 {"value": [{"x": 1}]}, {"value": [[1, 2]]},
                 {"value": [nan]}, {"value": [shared_list]},
                 {"value": [float("inf")]}, {"value": [None, 1, {"x": 1}]},
                 {"value": [1, 2], "missing_key": [None]}]
        for filters in cases:
            with self.subTest(filters=filters):
                expected = [row["id"] for row in rows if all(
                    any(row.get(key) == value for value in values)
                    for key, values in (filters or {}).items())]
                actual = _filter_rows(rows, filters)
                self.assertEqual([row["id"] for row in actual], expected)
        self.assertEqual(_filter_rows(rows, {"value": [nan]}), ())
        self.assertEqual([row["id"] for row in _filter_rows(rows, {"value": [large + 1]})],
                         ["next-large"])


if __name__ == "__main__":
    unittest.main()
