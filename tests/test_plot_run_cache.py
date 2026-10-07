"""Run-first SQLite selection, cached snapshots and saved-identity regressions."""
from contextlib import closing, contextmanager, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from attodry_control.analysis_observations import channel_quality, observation_id
from attodry_control.combination_analysis import list_combination_runs, load_combination_rows
from attodry_control.combination_store import CombinationStore, encode
from attodry_control.unified_plotting import (
    UnifiedPlotDashboard, _sample_token, _source_signature, load_plot_sources,
)

X, Y = "actual.field_x_t", "measured.signal"


@contextmanager
def database(path):
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            yield connection


class RunCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.path = self.directory / "runs.sqlite"
        with CombinationStore(self.path):
            pass
        with database(self.path) as connection:
            self.insert_run(connection, "A", 10)
            self.insert_run(connection, "B", 20)
            self.insert_run(connection, "failed", 30, status="interrupted")
        self.cleanup_json = encode({"clean": True, "audit": "cleanup-once-" + "x" * 1000})

    def insert_run(self, connection, run_id, base, *, status="completed"):
        cleanup = encode({"clean": True, "audit": "cleanup-once-" + "x" * 1000})
        connection.execute(
            "INSERT INTO combination_runs VALUES(?,?,?,?,?,?,?)",
            (run_id, 1, "{}", "2026-10-07T00:00:00Z", status, cleanup, None),
        )
        for condition in range(2):
            context = {
                "sequence_index": condition, "repeat_index": 0, "loop_order": ["magnetic"],
                "axes": {"magnetic": {"index": condition, "direction": "forward"}},
                "requested": {"field_x_t": condition, "field_z_t": 0},
            }
            connection.execute("INSERT INTO combination_conditions VALUES(?,?,?,?)",
                               (run_id, str(condition), condition, encode(context)))
            connection.execute("INSERT INTO combination_attempts VALUES(?,?,?,?,?,?,?)",
                               (run_id, str(condition), 0, "accepted", None, "start", "finish"))
            for sample_index in range(2):
                sample = {
                    "clean": True, "started_at_utc": "start", "finished_at_utc": "finish",
                    "simulated": True, "actual": {"field_x_t": condition, "field_z_t": 0},
                    "measurements": {"signal": base + condition + sample_index},
                    "reads": [{"module": "lockin", "captured_at_utc": "formal",
                               "status": {"marker": "original"}}],
                }
                connection.execute("INSERT INTO combination_samples VALUES(?,?,?,?,?)",
                                   (run_id, str(condition), 0, sample_index, encode(sample)))

    def dashboard(self, *, run=None):
        dashboard = UnifiedPlotDashboard(self.directory)
        dashboard.add_plot("curve")
        card = dashboard.cards[0]
        card["sources"]._controls[str(self.path)].value = True
        if run is not None:
            card["runs"].value = ((str(self.path), run),)
            card["x"].value, card["y"].value = X, Y
        return dashboard, card

    def test_sr865_native_status_is_never_decoded_with_sr830_masks(self):
        column = "measured.lockin_xy_h1_amplitude_v"
        formal = {"model": "SR865A", "reading": {"model": "SR865A", "harmonic": 1},
                  "lia_status": {"model": "SR865A", "raw": 1, "status_known": True,
                                 "output_overload": True, "input_or_reserve_overload": False},
                  "error_status": 0}
        row = {"status.lockin": {"samples": [{"lockin_xy": formal}]}, column: 0.005}
        state, issues = channel_quality(row, column)
        self.assertEqual(state, "flagged")
        self.assertEqual(issues, ("output_overload",))
        formal["lia_status"] = {"model": "SR865A", "raw": 4, "status_known": False,
                                "unknown_status_bits": 4}
        state, issues = channel_quality(row, column)
        self.assertEqual(state, "flagged")
        self.assertEqual(issues, ("unknown_status_bits",))
        formal["lia_status"] = {"model": "SR865A", "raw": 0}
        self.assertEqual(channel_quality(row, column), ("unknown", ()))
        formal["lia_status"] = {"model": "SR865A", "raw": 0, "status_known": True}
        row["status.lockin"]["samples"][0]["settings_after"] = {
            "roles": {"lockin_xy": {"model": "SR865A", "sensitivity_code": 6,
                                    "sensitivity_full_scale_v": 0.01,
                                    "time_constant_code": 0, "time_constant_s": 1e-6}}}
        self.assertEqual(channel_quality(row, column), ("clear", ()))
        row[column] = 0.02
        self.assertEqual(channel_quality(row, column), ("flagged", ("exceeds_full_scale",)))
        row[column] = 0.005
        formal.pop("model")
        formal["reading"].pop("model")
        formal["lia_status"] = {"raw": 1}
        self.assertEqual(channel_quality(row, column), ("flagged", ("input_or_reserve_overload",)))

    def test_formal_loader_preserves_sr865_model_native_raw_and_physical_settings(self):
        settings = {"model": "SR865A", "sensitivity_code": 6,
                    "sensitivity_full_scale_v": 0.01, "time_constant_code": 0,
                    "time_constant_s": 1e-6}
        instrument = {
            "model": "SR865A",
            "reading": {"model": "SR865A", "role": "xy", "harmonic": 1,
                        "x_v": 0.0, "y_v": 0.0, "amplitude_v": 0.0, "phase_deg": None},
            "lia_status": {"model": "SR865A", "raw": 0, "status_known": True},
            "native_sample": {"snap": {"x": 0.0, "y": 0.0, "r": 0.0, "phase": None},
                              "native_status": {"scal": 6, "oflt": 0}},
            "raw": {"SNAP? 0,1,2": "0,0,0"}, "error_status": 0,
        }
        with database(self.path) as connection:
            payload = json.loads(connection.execute(
                "SELECT payload_json FROM combination_samples WHERE run_id='A' LIMIT 1"
            ).fetchone()[0])
            status = {"samples": [{"lockin_xy": instrument, "settings_verified": True,
                                  "settings_before": {"roles": {"lockin_xy": settings}},
                                  "settings_after": {"roles": {"lockin_xy": settings}}}]}
            payload["reads"][0]["status"] = status
            payload["measurements"] = {"lockin_xy_h1_x_v": 0.0, "lockin_xy_h1_y_v": 0.0,
                                       "lockin_xy_h1_amplitude_v": 0.0}
            connection.execute("UPDATE combination_samples SET payload_json=? "
                               "WHERE run_id='A' AND condition_id='0' AND sample_index=0",
                               (encode(payload),))
        row = load_combination_rows(self.path, run_id="A")[0]
        self.assertEqual(row["status.lockin"], status)
        self.assertNotIn("measured.lockin_xy_h1_phase_deg", row)
        self.assertIsNone(row["status.lockin"]["samples"][0]["lockin_xy"]["reading"]["phase_deg"])
        self.assertEqual(channel_quality(row, "measured.lockin_xy_h1_amplitude_v"), ("clear", ()))

    def test_catalog_reads_no_cleanup_context_or_sample_json(self):
        with patch("attodry_control.combination_analysis.json.loads",
                   side_effect=AssertionError("catalog decoded JSON")):
            runs = list_combination_runs(self.path)
        self.assertEqual({row["run_id"] for row in runs}, {"A", "B", "failed"})
        self.assertEqual(set(runs[0]), {"run_id", "schema_version", "created_at_utc", "status"})

    def test_selected_query_never_decodes_an_unselected_corrupt_run(self):
        with database(self.path) as connection:
            connection.execute("UPDATE combination_samples SET payload_json='invalid JSON' WHERE run_id='B'")
        self.assertEqual(len(load_combination_rows(self.path, run_id="A")), 4)
        self.assertEqual(len(load_plot_sources([self.path], run_ids_by_source={str(self.path): ["A"]})), 4)
        self.assertEqual(load_plot_sources([self.path], run_ids_by_source={}), ())
        with self.assertRaises(json.JSONDecodeError):
            load_combination_rows(self.path)  # Historical all-runs API remains explicit.

    def test_cleanup_is_decoded_once_per_loaded_run(self):
        loads = json.loads
        with patch("attodry_control.combination_analysis.json.loads", wraps=loads) as parser:
            rows = load_combination_rows(self.path)
        self.assertEqual(len(rows), 8)
        self.assertEqual(sum(call.args[0] == self.cleanup_json for call in parser.call_args_list), 3)
        with patch("attodry_control.combination_analysis.json.loads", wraps=loads) as parser:
            load_combination_rows(self.path, run_id="B")
        self.assertEqual(sum(call.args[0] == self.cleanup_json for call in parser.call_args_list), 1)

    def test_no_samples_before_run_selection_and_cache_shared_between_cards(self):
        with redirect_stdout(io.StringIO()), patch(
            "attodry_control.unified_plotting.load_plot_sources", wraps=load_plot_sources
        ) as loader:
            dashboard, card = self.dashboard()
            self.assertEqual(card["runs"].value, ())
            self.assertEqual(dashboard._selected_rows(card), ())
            self.assertEqual(card["excluded"].options, ())
            self.assertEqual(loader.call_count, 0)
            card["runs"].value = ((str(self.path), "A"),)
            self.assertEqual(loader.call_count, 1)
            self.assertEqual(len(card["excluded"].options), 4)
            self.assertEqual({row["run_id"] for row in dashboard._selected_rows(card)}, {"A"})
            card["x"].value, card["y"].value = X, Y
            card["title"].value = "Changed title"
            card["statistics"].value = "mean_sem"
            card["quality"].value = "include"
            dashboard.add_plot("field", {"source_paths": [str(self.path)],
                                       "run_ids_by_source": {str(self.path): ["A"]}})
            self.assertEqual(loader.call_count, 1)
            card["runs"].value = ((str(self.path), "B"),)
            card["runs"].value = ((str(self.path), "A"),)
            self.assertEqual(loader.call_count, 2)
            rows = dashboard._selected_rows(card)
            rows[0]["status.lockin"]["marker"] = "mutated"
            self.assertEqual(dashboard._selected_rows(card)[0]["status.lockin"]["marker"], "original")
            dashboard._refresh()
            self.assertEqual(loader.call_count, 3)
            self.assertIsNone(card["rendered"])
            self.assertEqual(card["runs"].value, ((str(self.path), "A"),))

    def test_database_and_audit_are_part_of_cache_identity(self):
        second = self.directory / "second.sqlite"
        with CombinationStore(second):
            pass
        with database(second) as connection:
            self.insert_run(connection, "A", 100)
        with redirect_stdout(io.StringIO()):
            dashboard, card = self.dashboard(run="A")
            card["sources"]._controls[str(second)].value = True
            card["runs"].value = ((str(self.path), "A"), (str(second), "A"))
            rows = dashboard._selected_rows(card)
            self.assertEqual({row[Y] for row in rows}, {10, 11, 12, 100, 101, 102})
            card["runs"].value = ((str(self.path), "failed"),)
            self.assertEqual(dashboard._selected_rows(card), ())
            dashboard.include_audit.value = True
            rows = dashboard._selected_rows(card)
            self.assertEqual(len(rows), 4)
            self.assertTrue(all(row["accepted"] is False for row in rows))
            dashboard.include_audit.value = False
            self.assertEqual(dashboard._selected_rows(card), ())

    def test_recorded_ids_stable_across_scope_audit_and_old_positional_tokens(self):
        all_rows = load_plot_sources([self.path])
        scoped = load_plot_sources([self.path], run_ids_by_source={str(self.path): ["B"]})
        old_row = next(row for row in all_rows if row["run_id"] == "B")
        self.assertNotEqual(old_row["row_index"], scoped[0]["row_index"])
        self.assertEqual(_sample_token(old_row), _sample_token(scoped[0]))
        audited = load_plot_sources([self.path], include_audit=True,
                                    run_ids_by_source={str(self.path): ["B"]})
        self.assertEqual(_sample_token(audited[0]), _sample_token(scoped[0]))
        old_token = json.dumps({**observation_id(old_row), "row_index": old_row["row_index"]},
                               sort_keys=True, ensure_ascii=False)
        with redirect_stdout(io.StringIO()):
            dashboard = UnifiedPlotDashboard(self.directory)
            dashboard.add_plot("curve", {
                "source_paths": [str(self.path)], "x": X, "y": Y,
                "filters": {"run_id": ["B"]}, "excluded_sample_ids": [old_token],
            })
            card = dashboard.cards[0]
            self.assertEqual(card["runs"].value, ((str(self.path), "B"),))
            self.assertEqual(card["excluded"].value, (_sample_token(scoped[0]),))
            dashboard._render_card(card)
            self.assertIsNotNone(card["rendered"])
            self.assertEqual(card["rendered"]["data"]["report"]["plotted_row_count"], 3)
        first_csv = {"source_path": "file.csv", "run_id": "file", "row_index": 0}
        self.assertNotEqual(_sample_token(first_csv), _sample_token({**first_csv, "row_index": 1}))

    def test_setup_restores_runs_and_empty_selection_and_rejects_missing_run(self):
        with redirect_stdout(io.StringIO()):
            dashboard, card = self.dashboard(run="B")
            card["excluded"].value = (card["excluded"].options[0][1],)
            excluded = card["excluded"].value
            dashboard.configuration_path.value = str(self.directory / "saved.json")
            dashboard._save_setup()
            card["runs"].value = ((str(self.path), "A"),)
            dashboard._load_setup()
            loaded = dashboard.cards[0]
            self.assertEqual(loaded["runs"].value, ((str(self.path), "B"),))
            self.assertEqual(loaded["excluded"].value, excluded)
            payload = dashboard._configuration()
            payload["plots"][0]["run_ids_by_source"] = {str(self.path): []}
            empty = self.directory / "empty.json"
            empty.write_text(json.dumps(payload), encoding="utf-8")
            dashboard.configuration_path.value = str(empty)
            dashboard._load_setup()
            self.assertEqual(dashboard.cards[0]["runs"].value, ())
            self.assertEqual(dashboard._selected_rows(dashboard.cards[0]), ())
            payload["plots"][0]["run_ids_by_source"] = {str(self.path): ["deleted"]}
            missing = self.directory / "missing-run.json"
            missing.write_text(json.dumps(payload), encoding="utf-8")
            before = dashboard.cards[0]
            dashboard.configuration_path.value = str(missing)
            dashboard._load_setup()
            self.assertIn("missing", dashboard.status.value)
            self.assertIs(dashboard.cards[0], before)

    def test_old_whole_database_setup_preserves_all_run_selection(self):
        with redirect_stdout(io.StringIO()):
            dashboard = UnifiedPlotDashboard(self.directory)
            dashboard.add_plot("curve", {"source_paths": [str(self.path)], "x": X, "y": Y})
            card = dashboard.cards[0]
            self.assertEqual({run for _, run in card["runs"].value}, {"A", "B", "failed"})
            self.assertEqual(len(dashboard._selected_rows(card)), 8)

    def test_load_race_never_caches_or_attests_a_changed_source(self):
        with redirect_stdout(io.StringIO()):
            dashboard, card = self.dashboard()
            card["updating"] = True
            card["runs"].value = ((str(self.path), "A"),)
            card["updating"] = False
            with patch("attodry_control.unified_plotting._source_signature",
                       side_effect=[("before",), ("after",)]):
                with self.assertRaisesRegex(ValueError, "while loading"):
                    dashboard._selected_rows(card)
            self.assertEqual(dashboard._cache, {})

    def test_wal_change_keeps_cached_rows_and_refuses_render_export_until_refresh(self):
        writer = sqlite3.connect(self.path)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        with redirect_stdout(io.StringIO()), patch(
            "attodry_control.unified_plotting.load_plot_sources", wraps=load_plot_sources
        ) as loader:
            dashboard, card = self.dashboard(run="A")
            dashboard._render_card(card)
            self.assertIsNotNone(card["rendered"])
            before_signature = _source_signature(self.path)
            before_db = (self.path.stat().st_size, self.path.stat().st_mtime_ns)
            sample = json.loads(writer.execute(
                "SELECT payload_json FROM combination_samples "
                "WHERE run_id='A' AND condition_id='0' AND sample_index=0"
            ).fetchone()[0])
            sample["measurements"]["signal"] = 99
            writer.execute("UPDATE combination_samples SET payload_json=? "
                           "WHERE run_id='A' AND condition_id='0' AND sample_index=0", (encode(sample),))
            self.insert_run(writer, "new-run", 50)
            writer.commit()
            self.assertEqual((self.path.stat().st_size, self.path.stat().st_mtime_ns), before_db)
            self.assertNotEqual(_source_signature(self.path), before_signature)
            self.assertEqual(dashboard._selected_rows(card)[0][Y], 10)
            self.assertEqual(loader.call_count, 1)
            self.assertNotIn("new-run", {value[1] for _, value in card["runs"].options})
            dashboard._export()
            self.assertIn("changed after rendering", dashboard.status.value)
            self.assertIsNone(card["rendered"])
            with patch("attodry_control.unified_plotting.render_plot") as render:
                dashboard._render_card(card)
                render.assert_not_called()
            self.assertIsNone(card["rendered"])
            dashboard._refresh()
            self.assertEqual(loader.call_count, 2)
            self.assertEqual(dashboard._selected_rows(card)[0][Y], 99)
            self.assertIn("new-run", {value[1] for _, value in card["runs"].options})
            dashboard._render_card(card)
            self.assertIsNotNone(card["rendered"])
            self.assertEqual(card["rendered"]["spec"]["source_signatures"][str(self.path)],
                             _source_signature(self.path))

    def test_mixed_run_revisions_require_one_refresh_before_render(self):
        writer = sqlite3.connect(self.path)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA wal_autocheckpoint=0")
        with redirect_stdout(io.StringIO()):
            dashboard, card = self.dashboard(run="A")
            writer.execute("UPDATE combination_runs SET error='audit change' WHERE run_id='B'")
            writer.commit()
            card["runs"].value = ((str(self.path), "A"), (str(self.path), "B"))
            with self.assertRaisesRegex(ValueError, "different source snapshots"):
                dashboard._loaded_signatures(card)
            dashboard._refresh()
            self.assertEqual(dashboard._loaded_signatures(card)[str(self.path)], _source_signature(self.path))

    def test_failed_refresh_clears_run_catalog_selection_cache_and_render(self):
        with redirect_stdout(io.StringIO()):
            dashboard, card = self.dashboard(run="A")
            dashboard._render_card(card)
            dashboard.directory.value = str(self.directory / "missing")
            dashboard._refresh()
            self.assertEqual(dashboard._cache, {})
            self.assertEqual(dashboard._run_catalog, {})
            self.assertEqual(card["runs"].value, ())
            self.assertIsNone(card["rendered"])

    def test_both_notebooks_use_same_run_first_dashboard(self):
        root = Path(__file__).resolve().parents[1]
        with redirect_stdout(io.StringIO()), patch("IPython.display.display"):
            for name in ("unified_plotting.ipynb", "combination_analysis.ipynb"):
                document = json.loads((root / "notebooks" / name).read_text(encoding="utf-8"))
                cells = ["".join(cell["source"]) for cell in document["cells"] if cell["cell_type"] == "code"]
                namespace = {}
                for source in cells:
                    compile(source, name, "exec")
                exec(cells[0], namespace)
                namespace["DATA_DIRECTORY"] = self.directory
                with patch("attodry_control.unified_plotting.load_plot_sources", wraps=load_plot_sources) as loader:
                    exec(cells[1], namespace)
                    dashboard = namespace["dashboard"]
                    card = dashboard.cards[0]
                    card["sources"]._controls[str(self.path)].value = True
                    self.assertEqual(loader.call_count, 0)
                    card["runs"].value = ((str(self.path), "B"),)
                    self.assertEqual({row["run_id"] for row in dashboard._selected_rows(card)}, {"B"})
                    self.assertEqual(loader.call_count, 1)
