"""Operator CLI defaults, atomic registration and readable audit; offline only."""
from contextlib import closing, redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from attodry_control.combination_cli import run as cli
from attodry_control.combination_registry import index_path, register_run, resolve_monitor
from attodry_control.combination_store import CombinationStore, open_readonly, run_snapshot
from attodry_control.combination_scan import (
    AxisPoint, ScanAxis, CombinationPlan, SimulatedCombinationStation, _run_combination)
from attodry_control.combination_terminal import launch_text, snapshot_text
from attodry_control.combination_launch import launch_summary
from attodry_control.combination_hardware import load_hardware_combination
from tests import test_combination_hardware as fixture_module


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = self.root / "config" / "hardware.local.toml"
        self.config.parent.mkdir()
        self.database = self.root / "run_data" / "configured.sqlite"
        self.database.parent.mkdir()
        self.config.write_text('[project]\ndatabase_path = "../run_data/configured.sqlite"\n',
                               encoding="utf-8")
        self.plan = CombinationPlan((ScanAxis("smu", (AxisPoint({"smu_bias_v": 0.0}),)),))

    def add_run(self, database, run_id, register=False):
        with CombinationStore(database) as store:
            store.begin_run(run_id, self.plan.snapshot(), self.plan.conditions())
            if register:
                return register_run(self.config, store, run_id)

    def test_registration_requires_a_committed_run_and_does_not_publish_preview(self):
        with CombinationStore(self.database) as store, self.assertRaisesRegex(ValueError, "uncommitted"):
            register_run(self.config, store, "preview")
        self.assertFalse(index_path(self.config).exists())

    def test_registered_override_database_wins_over_toml(self):
        override = self.root / "override.sqlite"
        record = self.add_run(override, "override", register=True)
        self.add_run(self.database, "configured")
        self.assertEqual(resolve_monitor(self.config), (override, "override"))
        self.assertEqual(Path(record["database"]), override)
        self.assertIsInstance(record["pid"], int)
        self.assertEqual(record, json.loads(index_path(self.config).read_text(encoding="utf-8")))
        self.assertEqual(run_snapshot(override, "override")["process_liveness"], "unknown")

    def test_explicit_database_and_id_override_index(self):
        override = self.root / "override.sqlite"
        self.add_run(override, "indexed", register=True)
        self.add_run(self.database, "first")
        self.add_run(self.database, "second")
        self.assertEqual(resolve_monitor(self.config, self.database), (self.database, "second"))
        self.assertEqual(resolve_monitor(self.config, self.database, "first"), (self.database, "first"))

    def test_fallback_uses_registration_time_not_database_mtime(self):
        self.add_run(self.database, "first")
        self.add_run(self.database, "second")
        with CombinationStore(self.database) as store:
            store.connection.execute("UPDATE combination_runs SET created_at_utc=? WHERE run_id=?",
                                     ("2099-01-01T00:00:00+00:00", "first"))
            store.connection.commit()
        self.assertEqual(resolve_monitor(self.config), (self.database, "first"))

    def test_bad_deleted_or_mismatched_index_falls_back_readonly(self):
        record = self.add_run(self.database, "configured", register=True)
        for data in ("broken JSON", "[]", json.dumps({**record, "database": str(self.root / "gone.sqlite")}),
                     json.dumps({**record, "run_id": "preview"}),
                     json.dumps({**record, "created_at_utc": "wrong"})):
            index_path(self.config).write_text(data, encoding="utf-8")
            self.assertEqual(resolve_monitor(self.config), (self.database, "configured"))
        self.assertFalse((self.root / "gone.sqlite").exists())

    def test_atomic_replace_failure_preserves_previous_index(self):
        self.add_run(self.database, "old", register=True)
        old = index_path(self.config).read_bytes()
        with CombinationStore(self.database) as store:
            store.begin_run("new", self.plan.snapshot(), self.plan.conditions())
            with patch("attodry_control.combination_registry.os.replace", side_effect=OSError("disk fault")), \
                    self.assertRaises(OSError):
                register_run(self.config, store, "new")
        self.assertEqual(index_path(self.config).read_bytes(), old)
        self.assertEqual(list(index_path(self.config).parent.glob(".latest-combination-*")), [])

    def test_callback_runs_after_commit_before_station_opens(self):
        station = SimulatedCombinationStation()
        def callback(store, run_id):
            self.assertEqual(station.operations, [])
            self.assertEqual(run_snapshot(store.path, run_id)["status"], "active")
            return register_run(self.config, store, run_id)
        with CombinationStore(self.database) as store:
            result = _run_combination(self.plan, store, "registered", station=station,
                snapshot=self.plan.snapshot(), on_registered=callback)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(resolve_monitor(self.config), (self.database, "registered"))

    def test_failed_registration_stops_before_station_open(self):
        station = SimulatedCombinationStation()
        with CombinationStore(self.database) as store:
            result = _run_combination(self.plan, store, "registration-fault", station=station,
                snapshot=self.plan.snapshot(), on_registered=lambda *_: (_ for _ in ()).throw(OSError("disk fault")))
        self.assertEqual(result["status"], "failed")
        self.assertFalse(any(op[0] == "open" for op in station.operations))
        self.assertIn("disk fault", result["error"])

    def test_monitor_explicit_paths_need_no_toml_or_hardware_config(self):
        self.add_run(self.database, "files-only")
        self.config.unlink()
        before = self.database.read_bytes()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli(["monitor", "--database", str(self.database), "--once"]), 0)
        self.assertIn("files-only", output.getvalue())
        self.assertIn(str(self.database), output.getvalue())
        self.assertEqual(self.database.read_bytes(), before)

    def test_plain_monitor_uses_project_index_and_json_keeps_audit(self):
        override = self.root / "override.sqlite"
        self.add_run(override, "chosen", register=True)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli(["monitor", "--config", str(self.config), "--once", "--json"]), 0)
        value = json.loads(output.getvalue())
        self.assertEqual(value["run_id"], "chosen")
        self.assertIn("last_recorded_readings", value)
        self.assertEqual(value["process_liveness"], "unknown")

    def test_monitor_does_not_follow_new_registration_mid_run(self):
        self.add_run(self.database, "original", register=True)
        other = self.root / "next.sqlite"
        self.add_run(other, "next")
        calls = []
        def snapshot(database, run_id):
            calls.append((database, run_id))
            result = run_snapshot(database, run_id)
            if len(calls) == 1:
                self.add_index_for_next(other)
            else:
                result["status"] = "completed"
            return result
        with patch("attodry_control.combination_cli.run_snapshot", side_effect=snapshot), \
                patch("attodry_control.combination_cli.time.sleep"), redirect_stdout(io.StringIO()):
            self.assertEqual(cli(["monitor", "--config", str(self.config)]), 0)
        self.assertEqual(calls, [(self.database, "original")] * 2)

    def add_index_for_next(self, other):
        with CombinationStore(other) as store:
            register_run(self.config, store, "next")

    def test_missing_database_does_not_get_created(self):
        with self.assertRaises(Exception):
            resolve_monitor(self.config)
        self.assertFalse(self.database.exists())


class TerminalTests(unittest.TestCase):
    def fixture(self):
        fixture = fixture_module.ElectricalCombinationTests("test_both_loop_orders_fresh_reads_harmonics_regroup_and_cleanup")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def test_launch_tables_wrap_and_preserve_limits_harmonics_and_full_paths(self):
        fixture = self.fixture()
        config = load_hardware_combination(fixture.path)
        summary = launch_summary(config, fixture.database, "full-id")
        for width in (40, 70, 120):
            text = launch_text(summary, width)
            self.assertTrue(all(len(line) <= width for line in text.splitlines()), text)
            flattened = "".join(text.splitlines()).replace(" ", "")
            self.assertIn("1uA", flattened)
            self.assertIn("h1", flattened)
            self.assertIn("h3", flattened)
            if width >= 70:
                self.assertIn("h1,h2,h3", flattened)
            self.assertIn(str(fixture.database).replace(" ", ""), flattened)
            self.assertIn("record_continue" if config.lockin.lockin_sweep.overload_policy == "record_continue"
                          else "abort", flattened)
            self.assertIn("RunID:full-id", flattened)

    def test_nonuniform_reversal_and_duplicate_points_are_visible(self):
        fixture = self.fixture()
        config = load_hardware_combination(fixture.path)
        axis = ScanAxis("smu", tuple(AxisPoint({"smu_bias_v": v})
            for v in (-.01, -.004, 0, .01, .01, 0, -.004, -.01)))
        config = replace(config, plan=replace(config.plan, axes=(axis,)))
        summary = launch_summary(config, fixture.database, "hysteresis")
        self.assertEqual(summary["axes"][0]["segments"][0]["routes"]["smu_bias_v"],
                         [-.01, .01, -.01])
        self.assertEqual(summary["axes"][0]["points"], 8)
        text = launch_text(summary, 140)
        self.assertIn("-10 mV -> 10 mV -> -10 mV", text)
        self.assertIn("ordered grid", text)

    def test_segment_sensitivity_and_harmonic_overrides_are_visible(self):
        fixture = self.fixture()
        source = fixture.base.replace("excitation_points_v_rms = [0.004, 0.008]",
            'excitation_ranges = [{ min = 0.004, max = 0.008, scale = "linear", points = 2, xx_full_scale_v = 0.05 }]')
        fixture.write_config(source=source)
        config = load_hardware_combination(fixture.path)
        summary = launch_summary(config, fixture.database, "overrides")
        text = launch_text(summary, 140)
        self.assertIn("XX 50 mV", text)
        role = summary["lockin"]["roles"]["lockin_xy"]
        role["harmonic_settings"] = [{"harmonic": 2, "sensitivity_full_scale_v": .002,
                                      "reserve_mode": "normal", "sensitivity_mode": "fixed"}]
        self.assertIn("h2 override", launch_text(summary, 140))
        self.assertIn("2 mV", launch_text(summary, 140))

    def test_bounded_auto_baseline_and_harmonic_limits_are_visible(self):
        fixture = self.fixture()
        summary = launch_summary(load_hardware_combination(fixture.path), fixture.database, "auto")
        role = summary["lockin"]["roles"]["lockin_xy"]
        role.update(sensitivity_mode="bounded_auto", autorange_min_full_scale_v=.002,
                    autorange_max_full_scale_v=.02)
        role["harmonic_settings"] = [{"harmonic": 2, "sensitivity_mode": "bounded_auto",
            "sensitivity_full_scale_v": .002, "reserve_mode": "normal",
            "autorange_min_full_scale_v": .002, "autorange_max_full_scale_v": .05}]
        text = launch_text(summary, 160)
        self.assertIn("2 mV .. 20 mV", text)
        self.assertIn("2 mV .. 50 mV", text)
        self.assertIn("h2 AUTO limits", text)

    def test_failure_is_distinct_from_successful_individual_reset(self):
        text = snapshot_text({"run_id": "failed", "status": "failed", "error": "SafetyViolation: limit",
            "cleanup": {"clean": False, "manual_verification_required": True, "errors": ["fault retained"],
                "actions": [{"module": "lockin", "clean": False, "failure_requires_manual_review": True,
                             "result": {"verified": True, "errors": []}},
                            {"module": "smu", "clean": False, "failure_requires_manual_review": True,
                             "result": {"manual_verification_required": False, "errors": []}}]}}, 120)
        self.assertIn("Status: failed", text)
        self.assertEqual(text.count("verified"), 2)
        self.assertIn("Primary error: SafetyViolation: limit", text)
        self.assertIn("clean=False", text)
        self.assertIn("fault retained", text)

    def test_small_terminal_keeps_long_primary_errors_and_unknown_liveness(self):
        error = "Communication uncertain; last confirmed field is not current. " * 3
        text = snapshot_text({"run_id": "id", "status": "failed", "error": error}, 40)
        self.assertTrue(all(len(line) <= 40 for line in text.splitlines()))
        self.assertEqual("".join(text.splitlines()).replace(" ", "").count(
            "Communicationuncertain;lastconfirmedfieldisnotcurrent."), 3)
        self.assertIn("unknown", text)
