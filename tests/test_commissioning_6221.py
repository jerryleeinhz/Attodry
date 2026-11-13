"""Real archived fake scans through the read-only commissioning UI and fits."""
from contextlib import closing
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from attodry_control.commissioning_6221_analysis import list_6221_runs, load_6221_sweep
from attodry_control.commissioning_6221_widgets import build_6221_panel, current_sweep_report
from attodry_control.commissioning_analysis import (ExcitationPathResistance, HarmonicScalingRules,
    fit_harmonic_scalings, rms_current_for_row)
from attodry_control.combination_store import CombinationStore, open_readonly
from attodry_control.combination_resume import build_recovery
from tests import test_combination_6221 as fixtures

ROOT = fixtures.ROOT


class CurrentCommissioningTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = fixtures.Combination6221Tests()
        fixture.setUp()
        cls.fixture = fixture
        text = (ROOT / "config/hardware.6221_25Hz.example.toml").read_text(encoding="utf-8")
        for old, new in (("CHANGE_ME_6221_VISA_ADDRESS", "FAKE::SOURCE"),
                         ("CHANGE_ME_SR830_XX_VISA_ADDRESS", "FAKE::XX"),
                         ("CHANGE_ME_SR865A_XY_VISA_ADDRESS", "FAKE::XY"),
                         ("current_status_supported = false", "current_status_supported = true")):
            text = text.replace(old, new)
        text = re.sub(r"current_rms_ranges_a = \[[\s\S]*?\]", "current_rms_points_a = [1e-9, 13.5e-9, 26e-9, 38.5e-9, 51e-9]", text)
        fixture.path.write_text(text, encoding="utf-8")
        safety = fixture.directory / "lockin_safety.toml"
        safety.write_text(re.sub(r"allowed_fixed_full_scales_v = (\[[^\]]+\])",
            lambda m: "allowed_fixed_full_scales_v = " + json.dumps(sorted(set(json.loads(m[1])) | {0.5})),
            safety.read_text()), encoding="utf-8")
        original = CombinationStore.event
        failed = [False]
        def event(store, run_id, kind, payload):
            original(store, run_id, kind, payload)
            if kind == "condition_started" and payload["condition_id"] == "condition-000001" and not failed[0]:
                failed[0] = True
                raise RuntimeError("injected second condition failure")
        config = fixture.config()
        with patch.object(CombinationStore, "event", event):
            first = fixture.execute(config)
        assert first["status"] == "failed", first
        fixture.devices()
        review = build_recovery(config, fixture.database, "current-run", acknowledge_failure=True, manual_verification=True)
        result = fixture.execute(config, recovery=review)
        assert result["status"] == "completed", result

    @classmethod
    def tearDownClass(cls):
        cls.fixture.doCleanups()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.database = self.directory / "scan.sqlite"
        with closing(open_readonly(self.fixture.database)) as source, closing(sqlite3.connect(self.database)) as dest:
            source.backup(dest)
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        self.addCleanup(plt.close, "all")

    def snapshot(self, **kwargs):
        return load_6221_sweep(self.database, "current-run", **kwargs)

    def mutate_sample(self, change):
        with closing(sqlite3.connect(self.database)) as db, db:
            key, raw = db.execute("SELECT rowid,payload_json FROM combination_samples LIMIT 1").fetchone()
            sample = json.loads(raw)
            change(sample)
            db.execute("UPDATE combination_samples SET payload_json=? WHERE rowid=?", (json.dumps(sample), key))

    def test_resumed_prefix_exact_native_channels_and_readback_no_resistance(self):
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        snapshot = self.snapshot()
        self.assertEqual(list_6221_runs(self.database), ({"run_id": "current-run", "status": "completed"},))
        self.assertEqual(len(snapshot.groups), 1)
        rows = next(iter(snapshot.groups.values()))
        self.assertEqual(len(rows), 10)
        self.assertEqual({(r.role, r.harmonic, r.model) for r in rows}, {("xx", 1, None), ("xy", 2, "SR865A")})
        self.assertEqual(len({r.execution_id for r in rows}), 2)
        self.assertEqual({r.attempt_index for r in rows if r.point_index == 1}, {1})
        self.assertEqual(sorted({r.requested_current_rms_a for r in rows}), [1e-9, 13.5e-9, 26e-9, 38.5e-9, 51e-9])
        self.assertTrue(all(r.source_v_rms is None and r.sine_output_v_rms is None for r in rows))
        self.assertTrue(all(r.nominal_current_a_rms is None for r in rows))
        override = ExcitationPathResistance(1e12, 50, 0)
        self.assertTrue(all(rms_current_for_row(r, override) == r.excitation_current_rms_a for r in rows))
        self.assertTrue(all(r.channel_label for r in rows))
        self.assertFalse(snapshot.metadata["current_is_measured"])
        self.assertEqual(before, hashlib.sha256(self.database.read_bytes()).hexdigest())

    def test_missing_actual_current_never_uses_target_or_voltage(self):
        self.mutate_sample(lambda s: s["actual"].pop("excitation_current_rms_a"))
        with self.assertRaisesRegex(ValueError, "RMS current readback"):
            self.snapshot()

    def test_native_invalid_selected_channel_excluded_not_companion(self):
        def corrupt(s):
            pair = next(p for r in s["reads"] if r["module"] == "lockin" for p in r["status"]["samples"] if p["harmonic"] == 2)
            pair["valid_for_analysis_by_role"]["lockin_xy"] = False
        self.mutate_sample(corrupt)
        snapshot = self.snapshot()
        self.assertEqual(snapshot.metadata["selected_channel_rows"], 9)
        self.assertEqual(snapshot.metadata["quality_exclusions"][0]["role"], "xy")
        audit = self.snapshot(include_problem=True)
        self.assertEqual(audit.metadata["selected_channel_rows"], 10)
        self.assertEqual(sum("problem" in r.statuses for rows in audit.groups.values() for r in rows), 1)

    def test_preview_explicit_and_snapshot_immutable_when_db_changes(self):
        snapshot = self.snapshot()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE combination_runs SET status='active'")
        self.assertEqual(self.snapshot().groups, {})
        self.assertEqual(self.snapshot(preview=True).metadata["selected_channel_rows"], 10)
        self.assertEqual(snapshot.metadata["selected_channel_rows"], 10)
        self.assertEqual({r.record_status for rows in snapshot.groups.values() for r in rows}, {"completed"})

    def test_rejected_formal_audit_rows_never_join_fits(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            raw = db.execute("SELECT payload_json FROM combination_samples WHERE condition_id='condition-000001'").fetchone()[0]
            db.execute("INSERT INTO combination_samples VALUES (?,?,?,?,?)",
                       ("current-run", "condition-000001", 0, 0, raw))
        self.assertEqual(self.snapshot().metadata["selected_channel_rows"], 10)
        snapshot = self.snapshot(include_rejected=True)
        self.assertEqual(snapshot.metadata["selected_channel_rows"], 12)
        report = current_sweep_report(snapshot, next(iter(snapshot.groups)))
        self.assertEqual(report["manifest"]["fit_row_count"], 10)
        self.assertEqual(sum(r.record_status == "rejected" for r in report["rows"]), 2)

    def test_archived_configuration_and_outer_visits_separate_curves(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            key, raw = db.execute("SELECT execution_id,plan_json FROM combination_executions ORDER BY sequence_index LIMIT 1").fetchone()
            plan = json.loads(raw)
            plan["config_sha256"] = "different-archived-configuration"
            db.execute("UPDATE combination_executions SET plan_json=? WHERE execution_id=?", (json.dumps(plan), key))
            raw = db.execute("SELECT context_json FROM combination_conditions WHERE condition_id='condition-000002'").fetchone()[0]
            context = json.loads(raw)
            context["axes"]["temperature"] = {"index": 1, "temperature_k": 20.0}
            db.execute("UPDATE combination_conditions SET context_json=? WHERE condition_id='condition-000002'", (json.dumps(context),))
        self.assertEqual(len(self.snapshot().groups), 3)

    def test_explicit_run_id_and_missing_status_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "does not exist"):
            load_6221_sweep(self.database, "wrong-run")
        def corrupt(sample):
            for reading in sample["reads"]:
                if reading["module"] == "lockin":
                    reading["status"]["samples"][0]["settings_verified"] = None
        self.mutate_sample(corrupt)
        snapshot = self.snapshot()
        self.assertEqual(snapshot.metadata["selected_channel_rows"], 9)
        self.assertIn("settings_unverified", snapshot.metadata["quality_exclusions"][0]["reasons"])

    def test_fits_use_actual_rms_and_retain_minimum_points(self):
        rows = next(iter(self.snapshot().groups.values()))
        synthetic = tuple(replace(r, excitation_current_rms_a=r.requested_current_rms_a * 0.9999999,
            x_v=(r.requested_current_rms_a * 0.9999999 * 1e7) ** r.harmonic, y_v=0.0,
            amplitude_v=(r.requested_current_rms_a * 0.9999999 * 1e7) ** r.harmonic, phase_deg=0.0) for r in rows)
        fits = fit_harmonic_scalings(synthetic, rules=HarmonicScalingRules(minimum_points=5, minimum_snr=None))
        for key, fit in fits.items():
            self.assertAlmostEqual(fit.exponent, key[1], places=7)
            self.assertEqual(fit.current_basis, "6221_rms_setting_readback")
            self.assertEqual(fit.current_min_a_rms, 1e-9 * 0.9999999)
        default = fit_harmonic_scalings(synthetic)
        self.assertTrue(all(any("requires at least 6" in s for s in fit.reasons) for fit in default.values()))

    def test_all_plot_fit_report_engines_and_point_exclusions(self):
        snapshot = self.snapshot()
        key = next(iter(snapshot.groups))
        report = current_sweep_report(snapshot, key, excluded_conditions=("condition-000001",),
            metrics=("amplitude_v", "x_v", "y_v", "phase_deg"))
        self.assertEqual(len(report["rows"]), 8)
        self.assertTrue(all(r.point_index != 1 for r in report["rows"]))
        self.assertEqual(set(report["fits"]), {("xx", 1), ("xy", 2)})
        for figure in report["figures"].values():
            self.assertTrue(any("6221 current readback" in ax.get_xlabel() for ax in figure.axes))
            figure.canvas.draw()
        json.dumps(report["manifest"], allow_nan=False)

    def test_ui_explicit_load_stale_clear_and_export(self):
        panel = build_6221_panel(self.database, output_directory=self.directory / "exports")
        self.addCleanup(panel.close)
        c = panel.analysis_controls
        self.assertIsNone(c["state"]["snapshot"])
        c["refresh"].click()
        self.assertIsNone(c["runs"].value)
        c["runs"].value = "current-run"
        c["load"].click()
        self.assertIsNotNone(c["state"]["snapshot"], c["message"].value)
        self.assertIsNotNone(c["groups"].value, "Load must explicitly select an available curve group")
        with patch("IPython.display.display"):
            c["apply"].click()
        self.assertIsNotNone(c["state"]["report"], c["message"].value)
        c["export"].click()
        manifests = tuple((self.directory / "exports").glob("*/manifest.json"))
        self.assertEqual(len(manifests), 1, c["message"].value)
        exported = json.loads(manifests[0].read_text(encoding="utf-8"))
        self.assertEqual(exported["snapshot"]["current_basis"], "6221_rms_setting_readback")
        c["exclude"].value = ("condition-000001",)
        self.assertIsNone(c["state"]["report"])
        c["path"].value = str(self.directory / "missing.sqlite")
        self.assertIsNone(c["state"]["snapshot"])
        c["load"].click()
        self.assertIsNone(c["state"]["snapshot"])
        self.assertFalse((self.directory / "missing.sqlite").exists())

    def test_notebook_keeps_legacy_cells_and_adds_explicit_panel(self):
        notebook = json.loads((ROOT / "notebooks/sr830_commissioning_sweeps.ipynb").read_text(encoding="utf-8"))
        cells = {c.get("id"): c for c in notebook["cells"]}
        self.assertIn("6221-current-sweep-panel", cells)
        self.assertIn("commissioning-progress-panel", cells)
        for c in notebook["cells"]:
            if c["cell_type"] == "code":
                compile("".join(c["source"]), c.get("id", "legacy-cell"), "exec")
