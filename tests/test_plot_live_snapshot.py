"""Manual plotting snapshots while a synthetic local SQLite writer keeps working."""
from contextlib import redirect_stdout
import copy
import csv
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from attodry_control.combination_store import CombinationStore, encode
from attodry_control.unified_plotting import UnifiedPlotDashboard, _source_signature


X = "actual.temperature_k"
Y = "measured.lockin_xy_h2_amplitude_v"


class LiveSnapshotDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.path = self.directory / "synthetic.sqlite"
        with CombinationStore(self.path):
            pass
        self.writer = sqlite3.connect(self.path)
        self.addCleanup(self.writer.close)
        self.writer.execute("PRAGMA wal_autocheckpoint=0")
        self.insert_run("complete", status="completed")
        self.insert_condition("complete", 0)
        self.insert_condition("complete", 1)
        self.insert_run("active", status="active")
        self.insert_condition("active", 0)
        self.insert_condition("active", 1, attempt_status="started")
        self.insert_condition("active", 2, attempt_status="rejected")
        self.insert_condition("active", 3, clean=False)
        self.insert_condition("active", 4, flagged=True)
        self.insert_run("failed", status="failed")
        self.insert_condition("failed", 0, attempt_status="rejected")
        self.writer.commit()

    def insert_run(self, run_id, *, status):
        self.writer.execute("INSERT INTO combination_runs VALUES(?,?,?,?,?,?,?)",
            (run_id, 1, "{}", "2020-01-02T12:00:00Z", status,
             encode({"clean": status == "completed"}), None))

    def insert_condition(self, run_id, index, *, attempt_status="accepted", clean=True,
                         flagged=False):
        context = {"sequence_index": index, "repeat_index": 0,
            "loop_order": ["temperature", "lockin"],
            "axes": {"temperature": {"index": index, "direction": "ordered"}},
            "requested": {"temperature_k": 10.0 + index,
                          "lockin_excitation_v_rms": .1, "lockin_frequency_hz": 200.0}}
        self.writer.execute("INSERT INTO combination_conditions VALUES(?,?,?,?)",
                            (run_id, str(index), index, encode(context)))
        self.writer.execute("INSERT INTO combination_attempts VALUES(?,?,?,?,?,?,?)",
            (run_id, str(index), 0, attempt_status,
             "synthetic failure" if attempt_status == "rejected" else None,
             "2020-01-02T12:00:00Z", "2020-01-02T12:00:01Z"))
        status = {"samples": [{"harmonic": 2, "selected_roles": ["xy"],
            "settings_verified": True,
            "valid_for_analysis_by_role": {"lockin_xy": not flagged},
            "lockin_xy": {"lia_status": {"raw": 1 if flagged else 0},
                          "error_status": 0, "reading": {"harmonic": 2, "locked": True}}}]}
        payload = {"clean": clean, "simulated": True,
            "started_at_utc": "2020-01-02T12:00:00Z", "finished_at_utc": "2020-01-02T12:00:01Z",
            "actual": {"temperature_k": 10.0 + index},
            "measurements": {"lockin_xy_h2_amplitude_v": (index + 1) / 1000.0},
            "reads": [{"module": "lockin", "captured_at_utc": "2020-01-02T12:00:01Z",
                       "status": status}]}
        self.writer.execute("INSERT INTO combination_samples VALUES(?,?,?,?,?)",
                            (run_id, str(index), 0, 0, encode(payload)))

    def dashboard(self, runs=("complete",), *, preview=False):
        dashboard = UnifiedPlotDashboard(self.directory)
        if preview:
            dashboard.preview_active.value = True
        dashboard.add_plot("curve")
        card = dashboard.cards[0]
        card["sources"]._controls[str(self.path)].value = True
        card["runs"].value = tuple((str(self.path), run) for run in runs)
        if card["x"].options:
            card["x"].value, card["y"].value = X, Y
        return dashboard, card

    def render(self, dashboard, card):
        dashboard._render_card(card)
        self.assertIsNotNone(card["rendered"], "A committed WAL change must not reject loaded rows")
        return card["rendered"]

    def append_event(self, run_id="active"):
        self.writer.execute("INSERT INTO combination_events(run_id,created_at_utc,event_type,payload_json) "
            "VALUES(?,?,?,?)", (run_id, "2020-01-02T12:01:00Z", "synthetic_telemetry", "{}"))
        self.writer.commit()

    def test_committed_telemetry_other_run_and_checkpoint_do_not_block_cached_render(self):
        with redirect_stdout(io.StringIO()):
            dashboard, card = self.dashboard()
            loaded = dashboard._selected_rows(card)
            signature = _source_signature(self.path)
            self.append_event()
            self.assertNotEqual(_source_signature(self.path), signature)
            rendered = self.render(dashboard, card)
            self.assertEqual(tuple(rendered["data"]["rows"]), loaded)
            self.insert_run("later", status="completed")
            self.insert_condition("later", 0)
            self.writer.commit()
            self.writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self.assertEqual(dashboard._selected_rows(card), loaded)
            self.render(dashboard, card)
            self.assertNotIn("later", {value[1] for _, value in card["runs"].options})

    def test_active_preview_is_opt_in_and_retains_only_whole_clean_attempts(self):
        with redirect_stdout(io.StringIO()):
            dashboard, card = self.dashboard(runs=("active",))
            self.assertFalse(dashboard.preview_active.value)
            self.assertFalse(dashboard.include_audit.value)
            self.assertEqual(dashboard._selected_rows(card), ())
            dashboard.preview_active.value = True
            card["x"].value, card["y"].value = X, Y
            rows = dashboard._selected_rows(card)
            self.assertEqual({row["condition_id"] for row in rows}, {"0", "4"})
            self.assertTrue(all(row["preview"] is True and row["accepted"] is False for row in rows))
            self.assertTrue(all(row["attempt_status"] == "accepted" and row["clean"] for row in rows))
            rendered = self.render(dashboard, card)
            self.assertEqual([row["condition_id"] for row in rendered["data"]["rows"]], ["0"])
            self.assertEqual(rendered["data"]["report"]["excluded_quality_count"], 1)
            title = rendered["figure"].axes[0].get_title()
            self.assertIn("IN-PROGRESS PREVIEW", title)
            self.assertNotIn("AUDIT: rejected/problem records", title)

    def test_manual_refresh_alone_includes_new_accepted_condition(self):
        with redirect_stdout(io.StringIO()):
            dashboard, card = self.dashboard(runs=("active",), preview=True)
            before = dashboard._selected_rows(card)
            rendered = self.render(dashboard, card)
            snapshot = rendered["spec"]["source_snapshots"][str(self.path)]
            self.insert_condition("active", 5)
            self.writer.commit()
            self.assertEqual(dashboard._selected_rows(card), before)
            self.assertEqual(self.render(dashboard, card)["spec"]["source_snapshots"][str(self.path)], snapshot)
            dashboard._refresh()
            self.assertEqual(card["runs"].value, ((str(self.path), "active"),))
            after = dashboard._selected_rows(card)
            self.assertEqual({row["condition_id"] for row in after}, {"0", "4", "5"})
            fresh = self.render(dashboard, card)["spec"]["source_snapshots"][str(self.path)]
            self.assertNotEqual(fresh["snapshot_id"], snapshot["snapshot_id"])
            self.assertNotEqual(fresh["rows_sha256"], snapshot["rows_sha256"])
            self.assertEqual(fresh["row_count"], len(after))
            self.assertIn("loaded_at_utc", fresh)

    def test_export_after_writer_commit_uses_exact_rendered_rows_and_metadata(self):
        with redirect_stdout(io.StringIO()):
            dashboard, card = self.dashboard(runs=("active",), preview=True)
            rendered = self.render(dashboard, card)
            original = rendered["spec"]["source_snapshots"][str(self.path)]
            selected = tuple(rendered["data"]["rows"])
            self.insert_condition("active", 5)
            self.writer.commit()
            dashboard.output_directory.value = str(self.directory / "exports")
            dashboard._export()
            self.assertIn("Exported figures", dashboard.status.value)
            manifests = tuple((self.directory / "exports").glob("*/plot_manifest.json"))
            self.assertEqual(len(manifests), 1)
            manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
            self.assertEqual(manifest["selected_sample_rows"], len(selected))
            self.assertEqual(manifest["plots"][0]["spec"]["source_snapshots"][str(self.path)], original)
            with manifests[0].with_name("selected_samples.csv").open(newline="", encoding="utf-8") as stream:
                exported = list(csv.DictReader(stream))
            self.assertEqual([row["condition_id"] for row in exported], ["0"])
            exact = [{**dict(row), "plot_id": "plot_01"} for row in selected]
            expected_hash = hashlib.sha256(json.dumps(exact, sort_keys=True,
                ensure_ascii=False).encode("utf-8")).hexdigest()
            self.assertEqual(manifest["selected_samples_sha256"], expected_hash)

    def test_selected_run_batch_and_second_card_share_one_snapshot(self):
        from attodry_control.combination_analysis import load_combination_snapshot
        with redirect_stdout(io.StringIO()), patch(
                "attodry_control.unified_plotting.load_combination_snapshot",
                wraps=load_combination_snapshot) as loader:
            dashboard, first = self.dashboard(runs=("complete", "active"), preview=True)
            calls = loader.call_count
            one = self.render(dashboard, first)["spec"]["source_snapshots"][str(self.path)]
            self.assertEqual(set(one["run_ids"]), {"complete", "active"})
            self.append_event()
            dashboard.add_plot("curve", {"source_paths": [str(self.path)],
                "run_ids_by_source": {str(self.path): ["active", "complete"]}, "x": X, "y": Y})
            second = dashboard.cards[-1]
            two = self.render(dashboard, second)["spec"]["source_snapshots"][str(self.path)]
            self.assertEqual(loader.call_count, calls)
            self.assertEqual(one, two)
            self.assertEqual(dashboard._selected_rows(first), dashboard._selected_rows(second))

    def test_preview_does_not_include_failed_run_without_explicit_audit(self):
        with redirect_stdout(io.StringIO()):
            dashboard, card = self.dashboard(runs=("failed",), preview=True)
            self.assertEqual(dashboard._selected_rows(card), ())
            dashboard.include_audit.value = True
            card["x"].value, card["y"].value = X, Y
            rows = dashboard._selected_rows(card)
            self.assertEqual(len(rows), 1)
            self.assertFalse(rows[0]["accepted"])
            self.assertFalse(rows[0].get("preview", False))
            self.assertEqual(rows[0]["attempt_status"], "rejected")
            title = self.render(dashboard, card)["figure"].axes[0].get_title()
            self.assertIn("AUDIT: rejected/problem records", title)

    def test_preview_setup_round_trip_snapshot_status_and_legacy_default(self):
        with redirect_stdout(io.StringIO()):
            dashboard, card = self.dashboard(runs=("active",), preview=True)
            rendered = self.render(dashboard, card)
            snapshot = rendered["spec"]["source_snapshots"][str(self.path)]
            self.assertIn(snapshot["loaded_at_utc"], card["snapshot_status"].value)
            self.assertIn("formal rows", card["snapshot_status"].value)
            self.assertIn("active: active", card["snapshot_status"].value)
            detached = dashboard._loaded_snapshots(card)
            original = copy.deepcopy(snapshot)
            detached[str(self.path)]["runs"][0]["status"] = "mutated outside cache"
            self.assertEqual(dashboard._loaded_snapshots(card)[str(self.path)], original)
            self.assertEqual(rendered["spec"]["source_snapshots"][str(self.path)], original)
            setup = self.directory / "preview-setup.json"
            dashboard.configuration_path.value = str(setup)
            dashboard._save_setup()
            saved = json.loads(setup.read_text(encoding="utf-8"))
            self.assertTrue(saved["preview_active"])
            dashboard.preview_active.value = False
            self.assertEqual(dashboard._selected_rows(card), ())
            dashboard._load_setup()
            restored = dashboard.cards[0]
            self.assertTrue(dashboard.preview_active.value)
            self.assertFalse(dashboard.include_audit.value)
            self.assertEqual(restored["runs"].value, ((str(self.path), "active"),))
            self.assertEqual({row["condition_id"] for row in dashboard._selected_rows(restored)}, {"0", "4"})
            self.render(dashboard, restored)
            legacy = copy.deepcopy(saved)
            legacy.pop("preview_active")
            for plot in legacy["plots"]:
                plot.pop("preview_active", None)
            legacy_path = self.directory / "legacy-setup.json"
            legacy_path.write_text(json.dumps(legacy), encoding="utf-8")
            dashboard.configuration_path.value = str(legacy_path)
            dashboard._load_setup()
            self.assertFalse(dashboard.preview_active.value)
            self.assertEqual(dashboard._selected_rows(dashboard.cards[0]), ())
            self.assertIsNone(dashboard.cards[0]["rendered"])

    def test_failed_refresh_discards_cached_snapshot_and_preview_figure(self):
        with redirect_stdout(io.StringIO()):
            dashboard, card = self.dashboard(runs=("active",), preview=True)
            self.render(dashboard, card)
            dashboard.directory.value = str(self.directory / "missing")
            dashboard._refresh()
            self.assertEqual(card["sources"].value, ())
            self.assertEqual(card["runs"].value, ())
            self.assertEqual(dashboard._selected_rows(card), ())
            self.assertIsNone(card["rendered"])
            self.assertFalse(card["image"].value)

    def test_static_csv_changes_still_require_refresh(self):
        path = self.directory / "static.csv"
        path.write_text("U,V\n1,2\n2,4\n", encoding="utf-8")
        with redirect_stdout(io.StringIO()):
            dashboard = UnifiedPlotDashboard(self.directory)
            dashboard.add_plot("curve", {"source_paths": [str(path)], "x": "csv.U", "y": "csv.V"})
            card = dashboard.cards[0]
            self.render(dashboard, card)
            path.write_text("U,V\n1,20\n2,40\n", encoding="utf-8")
            dashboard._render_card(card)
            self.assertIsNone(card["rendered"])
            self.assertFalse(card["image"].value)
            dashboard._refresh()
            self.render(dashboard, card)
            self.assertEqual([row["csv.V"] for row in card["rendered"]["data"]["rows"]], [20., 40.])


if __name__ == "__main__":
    unittest.main()
