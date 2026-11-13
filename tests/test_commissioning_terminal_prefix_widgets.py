"""Terminal accepted-prefix labels, fit eligibility and exported provenance."""
from contextlib import closing
import csv
from dataclasses import replace
import json
import sqlite3
import unittest
from unittest.mock import patch

from attodry_control.commissioning_6221_analysis import CurrentSweepSnapshot
from attodry_control.commissioning_6221_widgets import build_6221_panel, current_sweep_report
from attodry_control.commissioning_analysis import HarmonicScalingRules
from tests import test_commissioning_6221 as _fixtures


class TerminalPrefixWidgetTests(unittest.TestCase):
    setUpClass = classmethod(_fixtures.CurrentCommissioningTests.setUpClass.__func__)
    tearDownClass = classmethod(_fixtures.CurrentCommissioningTests.tearDownClass.__func__)
    setUp = _fixtures.CurrentCommissioningTests.setUp
    snapshot = _fixtures.CurrentCommissioningTests.snapshot
    mutate_sample = _fixtures.CurrentCommissioningTests.mutate_sample

    def terminal_run(self, status="failed"):
        cleanup = {"clean": False, "manual_verification_required": True,
                   "errors": ["original cleanup uncertainty"]}
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE combination_runs SET status=?,error=?,cleanup_json=?",
                       (status, "original terminal error", json.dumps(cleanup)))
        return cleanup

    def report(self, snapshot=None, *, rules=None):
        snapshot = snapshot or self.snapshot()
        return current_sweep_report(snapshot, next(iter(snapshot.groups)), rules=rules)

    @staticmethod
    def figure_title(figure):
        titles = [axis.get_title() for axis in figure.axes]
        if figure._suptitle is not None:
            titles.append(figure._suptitle.get_text())
        return "\n".join(titles)

    def test_failed_and_interrupted_clean_prefixes_enter_fits_without_relaxing_rules(self):
        for status in ("failed", "interrupted"):
            with self.subTest(status=status):
                cleanup = self.terminal_run(status)
                snapshot = self.snapshot()
                rows = next(iter(snapshot.groups.values()))
                self.assertEqual(len(rows), 10)
                self.assertEqual({row.record_status for row in rows}, {"terminal_prefix"})
                self.assertEqual({row.run_status for row in rows}, {status})
                self.assertTrue(all(row.terminal_prefix and row.attempt_status == "accepted" for row in rows))
                result = self.report(snapshot)
                self.assertEqual(result["manifest"]["fit_row_count"], 10)
                self.assertEqual(result["manifest"]["terminal_prefix_fit_row_count"], 10)
                self.assertEqual(result["manifest"]["fit_scope"], "accepted_clean_formal_prefix")
                self.assertTrue(all(any("requires at least 6" in reason for reason in fit.reasons)
                                    for fit in result["fits"].values()))
                for figure in result["figures"].values():
                    self.assertIn(status.upper() + " PARTIAL PREFIX", self.figure_title(figure))
                run = result["manifest"]["snapshot"]["runs"][0]
                self.assertEqual((run["status"], run["error"], run["cleanup"]),
                                 (status, "original terminal error", cleanup))

    def test_fit_and_merged_scalar_figures_identify_the_partial_prefix(self):
        self.terminal_run("interrupted")
        snapshot = self.snapshot()
        groups = {}
        for key, rows in snapshot.groups.items():
            groups[key] = tuple(replace(row, x_v=(row.excitation_current_rms_a * 1e7) ** row.harmonic,
                y_v=0., amplitude_v=(row.excitation_current_rms_a * 1e7) ** row.harmonic, phase_deg=0.)
                for row in rows)
        synthetic = CurrentSweepSnapshot(groups, snapshot.metadata)
        result = self.report(synthetic, rules=HarmonicScalingRules(minimum_points=5, minimum_snr=None))
        self.assertIn("merged_scalar_report", result["figures"])
        self.assertTrue(any(name.endswith("_fits") for name in result["figures"]))
        for figure in result["figures"].values():
            self.assertIn("INTERRUPTED PARTIAL PREFIX", self.figure_title(figure))

    def test_actual_rejected_and_flagged_audit_rows_never_enter_prefix_fits(self):
        self.terminal_run()
        with closing(sqlite3.connect(self.database)) as db, db:
            raw = db.execute("SELECT payload_json FROM combination_samples "
                             "WHERE condition_id='condition-000001'").fetchone()[0]
            db.execute("INSERT INTO combination_samples VALUES(?,?,?,?,?)",
                       ("current-run", "condition-000001", 0, 0, raw))
        def flag_selected_channel(sample):
            pair = next(pair for reading in sample["reads"] if reading["module"] == "lockin"
                        for pair in reading["status"]["samples"] if pair["harmonic"] == 2)
            pair["valid_for_analysis_by_role"]["lockin_xy"] = False
        self.mutate_sample(flag_selected_channel)
        result = self.report(self.snapshot(include_rejected=True, include_problem=True))
        self.assertEqual(len(result["rows"]), 12)
        self.assertEqual(sum(row.record_status == "rejected" for row in result["rows"]), 2)
        self.assertEqual(result["manifest"]["fit_row_count"], 9)
        self.assertEqual(result["manifest"]["terminal_prefix_fit_row_count"], 9)
        self.assertTrue(all("AUDIT" in self.figure_title(figure) for figure in result["figures"].values()))

    def test_prefix_label_without_explicit_accepted_terminal_provenance_cannot_enable_fitting(self):
        self.terminal_run()
        snapshot = self.snapshot()
        for changes in ({"terminal_prefix": False}, {"attempt_status": "rejected"}, {"run_status": "active"}):
            with self.subTest(changes=changes):
                forged = CurrentSweepSnapshot({key: tuple(replace(row, **changes) for row in rows)
                    for key, rows in snapshot.groups.items()}, snapshot.metadata)
                result = self.report(forged)
                self.assertEqual(result["manifest"]["fit_row_count"], 0)
                self.assertEqual(result["fits"], {})

    def test_audit_only_selection_keeps_terminal_outcome_on_every_figure(self):
        self.terminal_run("interrupted")
        snapshot = self.snapshot()
        audit = CurrentSweepSnapshot({key: tuple(replace(row, record_status="rejected", terminal_prefix=False,
            attempt_status="rejected") for row in rows) for key, rows in snapshot.groups.items()}, snapshot.metadata)
        result = self.report(audit)
        self.assertEqual(result["manifest"]["fit_row_count"], 0)
        self.assertEqual(result["manifest"]["fit_scope"], "audit_only_no_fit")
        self.assertEqual(result["fits"], {})
        for figure in result["figures"].values():
            title = self.figure_title(figure)
            self.assertIn("INTERRUPTED AUDIT", title)
            self.assertNotIn("PARTIAL PREFIX", title)

    def test_panel_load_fit_export_preserves_original_failure_and_csv_provenance(self):
        cleanup = self.terminal_run()
        panel = build_6221_panel(self.database, output_directory=self.directory / "exports")
        self.addCleanup(panel.close)
        controls = panel.analysis_controls
        controls["refresh"].click()
        controls["runs"].value = "current-run"
        controls["load"].click()
        self.assertIsNotNone(controls["state"]["snapshot"], controls["message"].value)
        self.assertIn("FAILED PARTIAL PREFIX", controls["message"].value)
        self.assertIn("FAILED PARTIAL PREFIX", controls["groups"].options[0][0])
        with patch("IPython.display.display"):
            controls["apply"].click()
        self.assertIsNotNone(controls["state"]["report"], controls["message"].value)
        controls["export"].click()
        manifests = tuple((self.directory / "exports").glob("*/manifest.json"))
        self.assertEqual(len(manifests), 1, controls["message"].value)
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        self.assertEqual(manifest["terminal_prefix_fit_row_count"], 10)
        self.assertEqual(manifest["snapshot"]["runs"][0]["error"], "original terminal error")
        self.assertEqual(manifest["snapshot"]["runs"][0]["cleanup"], cleanup)
        with (manifests[0].parent / "samples.csv").open(encoding="utf-8", newline="") as stream:
            rows = tuple(csv.DictReader(stream))
        self.assertEqual(len(rows), 10)
        self.assertTrue(all(row["run_status"] == "failed" and row["attempt_status"] == "accepted"
                            and row["terminal_prefix"] == "True" and row["record_status"] == "terminal_prefix"
                            for row in rows))

    def test_active_preview_remains_explicit_and_does_not_become_terminal_prefix(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE combination_runs SET status='active'")
        self.assertEqual(self.snapshot().groups, {})
        snapshot = self.snapshot(preview=True)
        rows = next(iter(snapshot.groups.values()))
        self.assertTrue(all(row.run_status == "active" and not row.terminal_prefix for row in rows))
        result = self.report(snapshot)
        self.assertEqual(result["manifest"]["fit_row_count"], 10)
        self.assertEqual(result["manifest"]["terminal_prefix_fit_row_count"], 0)
        self.assertTrue(all("PARTIAL PREFIX" not in self.figure_title(figure)
                            for figure in result["figures"].values()))


if __name__ == "__main__":
    unittest.main()
