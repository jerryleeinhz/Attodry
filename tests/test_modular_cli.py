"""Shared CLI, progress, journal and persistence using offline fake runs."""
from contextlib import closing, redirect_stderr, redirect_stdout
from copy import deepcopy
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from attodry_control import combination_cli, modular_cli, photoresponse_cli
from attodry_control.combination_registry import index_path, resolve_monitor
from attodry_control.combination_store import run_snapshot
from attodry_control.photoresponse_monitor import initial_progress, update_progress
from attodry_control.combination_terminal import launch_text, snapshot_text


class ModularCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root / "config" / "modular.local.toml"
        self.config.parent.mkdir()
        self.config.write_text('''[combination_scan]
order = ["nanovoltmeter"]
backend = "simulation"
output_directory = "../run_data/results"
samples_per_condition = 2
repeats = 2
run_name = "module_test"

[measurement]
max_abs_voltage_v = 1.0
sample_interval_s = 0.01

[nanovoltmeter]
channel = 2
voltage_range_v = 10.0
nplc = 1.0
''', encoding="utf-8")

    def invoke(self, argv, *, entry=modular_cli.main):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = entry(argv)
        return result, stdout.getvalue(), stderr.getvalue()

    def fake_run(self, config, event, attempt_index=1, reject=False):
        record = {"schema_version": 1, "measurement_type": "dc_photoresponse",
            "simulated": True, "measurement_config": config.snapshot(), "conditions": [],
            "captured_at_utc": "2026-10-16T00:00:00+00:00", "completed": not reject,
            "outcome": "rejected" if reject else "completed", "error": "fake failure" if reject else None,
            "cleanup": {"laser_off_confirmed": True, "electrical_safe_confirmed": True, "errors": []}}
        for condition in config.plan.conditions():
            row = {**condition, "attempt_index": attempt_index, "accepted": not reject,
                   "windows": [], "result": {"quantity": "voltage_v", "voltage_v": .1}}
            event({"phase": "condition_start", "condition_id": row["condition_id"],
                   "attempt_index": attempt_index, "requested": condition["requested"]})
            event({"phase": "condition_complete", "condition": row})
            record["conditions"].append(row)
        if reject:
            event({"phase": "run_error", "error": record["error"]})
        event({"phase": "cleanup_complete", "cleanup": record["cleanup"]})
        return record

    def test_describe_and_validate_never_call_runner_or_hardware(self):
        with patch("attodry_control.modular_runner.run_modular", side_effect=AssertionError("must remain offline")):
            for command in ("validate-config", "describe-hardware", "describe"):
                with self.subTest(command=command):
                    code, stdout, stderr = self.invoke([command, "--config", str(self.config)])
                    self.assertEqual(code, 0, stderr)
                    snapshot = json.loads(stdout)
                    self.assertEqual(snapshot["combination"]["condition_count"], 2)
                    self.assertIsNone(snapshot["nkt"])
        self.assertFalse(index_path(self.config).exists())

    def test_common_and_legacy_entries_route_only_modular_configs(self):
        for entry in (combination_cli.main, photoresponse_cli.main):
            with self.subTest(entry=entry), patch.object(modular_cli, "main", return_value=7) as routed:
                code, _, _ = self.invoke(["simulate", "--config", str(self.config), "--json"], entry=entry)
                self.assertEqual(code, 7)
                routed.assert_called_once()
                self.assertIn(str(self.config), routed.call_args.args[0])

    def test_both_public_entries_and_modular_service_share_hardware_local_default(self):
        default = self.root/"config"/"hardware.local.toml"
        shutil.copyfile(self.config, default)
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            for entry in (combination_cli.main, photoresponse_cli.main, modular_cli.main):
                with self.subTest(entry=entry):
                    code, stdout, stderr = self.invoke(["validate-config"], entry=entry)
                    self.assertEqual(code, 0, stderr)
                    self.assertEqual(json.loads(stdout)["modular"]["source_path"], str(default))
                    with patch("attodry_control.modular_runner.run_modular", side_effect=self.fake_run):
                        code, stdout, stderr = self.invoke(["simulate", "--json"], entry=entry)
                    self.assertEqual(code, 0, stderr)
                    self.assertEqual(json.loads(stdout)["invocation"]["config_path"], str(default))
        finally:
            os.chdir(previous)

    def test_combination_monitor_uses_same_default_config_path(self):
        with patch.object(combination_cli, "resolve_monitor", return_value=(self.root/"audit.sqlite", "run")) as resolver, \
                patch.object(combination_cli, "run_snapshot", return_value={"status": "completed"}):
            code, _, stderr = self.invoke(["monitor", "--once", "--json"], entry=combination_cli.main)
        self.assertEqual(code, 0, stderr)
        resolver.assert_called_once_with(Path("config/hardware.local.toml"), None, None)
        self.assertEqual(modular_cli._parser().parse_args(["run"]).config, "config/hardware.local.toml")

    def test_legacy_describe_remains_capabilities_when_default_is_modular(self):
        default = self.root/"config"/"hardware.local.toml"
        shutil.copyfile(self.config, default)
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            with patch.object(modular_cli, "main", side_effect=AssertionError("describe is capabilities")):
                code, stdout, stderr = self.invoke(["describe"], entry=photoresponse_cli.main)
            self.assertEqual(code, 0, stderr)
            self.assertEqual(json.loads(stdout)["run"]["default_config"], "config/hardware.local.toml")
        finally:
            os.chdir(previous)

    def test_simulation_persists_registers_monitor_and_audit(self):
        database = self.root / "custom.sqlite"
        with patch("attodry_control.modular_runner.run_modular", side_effect=self.fake_run):
            code, stdout, stderr = self.invoke(["simulate", "--config", str(self.config),
                "--database", str(database), "--json", "--authorize-connect", "--authorize-writes", "--confirm-route"])
        self.assertEqual(code, 0, stderr)
        record = json.loads(stdout)
        self.assertEqual(record["outcome"], "completed")
        self.assertEqual(record["invocation"]["selected_modules"], ["nanovoltmeter"])
        self.assertEqual(resolve_monitor(self.config), (database, record["run_id"]))
        snapshot = run_snapshot(database, record["run_id"])
        self.assertEqual(snapshot["total_conditions"], 2)
        self.assertEqual(snapshot["accepted_conditions"], 2)
        self.assertTrue(snapshot["cleanup"]["clean"])
        journal = [json.loads(line) for line in Path(record["journal_path"]).read_text(encoding="utf-8").splitlines()]
        self.assertEqual(journal[-1]["phase"], "run_end")
        self.assertEqual(journal[0]["phase"], "software")
        self.assertEqual(len([v for v in journal if v["phase"] == "condition_complete"]), 2)

    def test_resume_keeps_failed_attempt_and_restarts_full_grid(self):
        with patch("attodry_control.modular_runner.run_modular", side_effect=lambda *a, **k: self.fake_run(*a, reject=True, **k)):
            first_code, stdout, stderr = self.invoke(["simulate", "--config", str(self.config), "--json"])
        self.assertEqual(first_code, 1, stderr)
        first = json.loads(stdout)
        with patch("attodry_control.modular_runner.run_modular", side_effect=self.fake_run):
            code, stdout, stderr = self.invoke(["simulate", "--config", str(self.config), "--json", "--resume-run", first["run_id"]])
        self.assertEqual(code, 0, stderr)
        second = json.loads(stdout)
        self.assertEqual(second["series_id"], first["series_id"])
        self.assertTrue(all(row["attempt_index"] == 2 for row in second["conditions"]))
        database, _ = resolve_monitor(self.config)
        with closing(sqlite3.connect(database)) as db:
            outcomes = dict(db.execute("SELECT run_id,outcome FROM photo_runs"))
            self.assertEqual(outcomes[first["run_id"]], "rejected")
            self.assertEqual(outcomes[second["run_id"]], "completed")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM photo_conditions WHERE run_id=? AND accepted=1", (first["run_id"],)).fetchone()[0], 0)

    def test_explicit_run_rejects_simulation_before_runner(self):
        with patch("attodry_control.modular_runner.run_modular") as runner:
            code, _, stderr = self.invoke(["run", "--config", str(self.config)])
        self.assertEqual(code, 2)
        self.assertIn("Use simulate", stderr)
        runner.assert_not_called()

    def test_launch_and_progress_support_absent_laser_and_module_readbacks(self):
        from attodry_control.modular_config import load_modular_config
        from attodry_control.modular_runtime import RuntimeConfig
        snapshot = RuntimeConfig(load_modular_config(self.config)).snapshot()
        text = launch_text({"measurement_type": "dc_photoresponse", "measurement_config": snapshot,
            "simulated": True, "run_id": "test", "database": "test.sqlite", "config": str(self.config), "config_sha256": "hash"})
        self.assertIn("Conditions: 2", text)
        self.assertIn("Channel: 2", text)
        self.assertIn("nanovoltmeter", text)
        progress = initial_progress(snapshot)
        update_progress(progress, {"phase": "axis_qualified", "module": "temperature", "scope": [["repeat", 0]], "point": {"temperature_k": 2.}})
        update_progress(progress, {"phase": "module_readback", "module": "temperature", "reading": {"actual": {"temperature_k": 2.}}})
        progress.update(status="active", run_id="test")
        rendered = snapshot_text(progress)
        self.assertIn("Module: temperature", rendered)
        self.assertIn("temperature_k", rendered)

    def test_monitor_config_fallback_uses_modular_database(self):
        database = self.root / "custom.sqlite"
        with patch("attodry_control.modular_runner.run_modular", side_effect=self.fake_run):
            code, stdout, stderr = self.invoke(["simulate", "--config", str(self.config), "--database", str(database), "--json"])
        self.assertEqual(code, 0, stderr)
        record = json.loads(stdout)
        # A second config has no registration, and resolves only explicit paths.
        other = self.root / "other.toml"
        other.write_text('[combination_scan]\ndatabase_path="custom.sqlite"\n', encoding="utf-8")
        self.assertEqual(resolve_monitor(other), (database, record["run_id"]))

    def test_actual_nanovoltmeter_only_simulation_through_public_entry(self):
        code, stdout, stderr = self.invoke(["simulate", "--config", str(self.config), "--json"], entry=combination_cli.main)
        self.assertEqual(code, 0, stderr + stdout)
        record = json.loads(stdout)
        self.assertEqual(len(record["conditions"]), 2)
        self.assertTrue(record["cleanup"]["laser_off_confirmed"])
        self.assertTrue(record["cleanup"]["electrical_safe_confirmed"])

    def test_checked_in_example_contracts_and_incomplete_hardware_template(self):
        from attodry_control.modular_config import load_modular_config
        from attodry_control.modular_runtime import RuntimeConfig
        root = Path(__file__).resolve().parents[1]
        expected = {"optics": 8, "electrical": 3, "environment": 4}
        for name, count in expected.items():
            with self.subTest(name=name):
                config = RuntimeConfig(load_modular_config(root/"config"/f"modular_{name}_simulation.toml"))
                self.assertEqual(config.plan.condition_count, count)
                self.assertFalse(config.hardware_mode)
        with self.assertRaises(ValueError):
            RuntimeConfig(load_modular_config(root/"config/modular_optics_hardware.example.toml"))

    def test_record_stem_budgets_longest_outcome_and_preserves_unique_token(self):
        directory = self.root / ("d"*max(1, 170-len(str(self.root))-1))
        original = "measurement_"*20
        stem = modular_cli._record_stem(directory, original, token="12345678", max_path=259)
        self.assertTrue(stem.endswith("_12345678"))
        for suffix in ("_interrupted.json", "_completed.json", "_rejected.json", ".jsonl"):
            length = len(str(directory/(stem+suffix)).encode("utf-16-le"))//2
            self.assertLessEqual(length, 259)
        with self.assertRaisesRegex(ValueError, "too long"):
            modular_cli._record_stem(directory, "name", token="12345678", max_path=30)

    @unittest.skipUnless(os.name == "nt", "Windows MAX_PATH integration check")
    def test_long_public_output_shortens_filename_only_before_opening_resources(self):
        target_length = 211
        count = target_length-len(str(self.root))-1
        if count < 1:
            self.skipTest("Temporary test root already exceeds the required output-directory budget")
        directory = self.root/("d"*count)
        long_name = "scan_"*16
        text = self.config.read_text(encoding="utf-8").replace(
            'output_directory = "../run_data/results"', "output_directory = " + json.dumps(directory.as_posix()))
        text = text.replace('run_name = "module_test"', 'run_name = "'+long_name+'"')
        self.config.write_text(text, encoding="utf-8")
        with patch("attodry_control.modular_runner.run_modular", side_effect=self.fake_run):
            code, stdout, stderr = self.invoke(["simulate", "--config", str(self.config), "--json"])
        self.assertEqual(code, 0, stderr)
        record = json.loads(stdout)
        self.assertEqual(record["measurement_config"]["combination"]["run_name"], long_name)
        paths = list(directory.glob("*.json"))
        self.assertEqual(len(paths), 1)
        self.assertLessEqual(len(str(paths[0]).encode("utf-16-le"))//2, 259)
        self.assertLessEqual(len(str(Path(record["journal_path"])).encode("utf-16-le"))//2, 259)

    @unittest.skipUnless(os.name == "nt", "Windows MAX_PATH integration check")
    def test_impossible_output_path_rejected_before_store_or_runner(self):
        count = max(1, 240-len(str(self.root))-1)
        directory = self.root/("d"*count)
        text = self.config.read_text(encoding="utf-8").replace(
            'output_directory = "../run_data/results"', "output_directory = " + json.dumps(directory.as_posix()))
        self.config.write_text(text, encoding="utf-8")
        with patch("attodry_control.modular_runner.run_modular") as runner, \
                patch("attodry_control.photoresponse_storage.PhotoStore") as store:
            code, _, stderr = self.invoke(["simulate", "--config", str(self.config)])
        self.assertEqual(code, 2)
        self.assertIn("too long", stderr)
        runner.assert_not_called()
        store.assert_not_called()
        self.assertFalse(directory.exists())


if __name__ == "__main__":
    unittest.main()
