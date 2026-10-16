"""Run-command semantics and audit; all acquisition is injected/fake."""
from contextlib import closing, nullcontext, redirect_stderr, redirect_stdout
from dataclasses import replace
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from attodry_control import photoresponse_cli as cli
from test_photoresponse_integration import hardware_setup, environment_config
from test_photoresponse_smu import configuration


ROOT = Path(__file__).resolve().parents[1]
DEFAULT = "config/hardware.local.toml"


def completed(config, args, event, attempt):
    return {"completed": True, "outcome": "completed", "simulated": False,
            "conditions": [], "cleanup": {"laser_off_confirmed": True,
            "electrical_safe_confirmed": True, "errors": []}}


class RunCommandTests(unittest.TestCase):
    def fixture(self, directory, name=DEFAULT):
        root = Path(directory)
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# injected hardware configuration\n", encoding="utf-8")
        config = hardware_setup("open_circuit_voltage")[0].config
        config = replace(config, nkt=replace(config.nkt, output_directory=root / "output"))
        return path, config

    def invoke(self, root, config, argv, run=completed):
        previous = Path.cwd()
        try:
            os.chdir(root)
            with patch.object(cli, "load_photoresponse_config", return_value=config) as loader, \
                    patch.object(cli, "_run", side_effect=run) as acquisition, \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                status = cli.main(argv)
            return status, loader, acquisition
        finally:
            os.chdir(previous)

    def test_plain_run_uses_default_and_records_exact_config_without_flags(self):
        with tempfile.TemporaryDirectory() as directory:
            path, config = self.fixture(directory)
            config = replace(config, database_path=Path(directory) / "audit.sqlite")
            status, loader, acquisition = self.invoke(directory, config, ["run"])
            self.assertEqual(status, 0)
            loader.assert_called_once_with(str(path.resolve()))
            args = acquisition.call_args.args[1]
            self.assertTrue(args.authorize_connect and args.authorize_writes and args.confirm_route)
            self.assertFalse(args.authorize_environment or args.authorize_smu)
            record = json.loads(next(config.nkt.output_directory.glob("*_completed.json")).read_text())
            invocation = record["invocation"]
            self.assertEqual(invocation["config_path"], str(path.resolve()))
            self.assertEqual(invocation["config_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(invocation["authorization_source"], "run_command")
            self.assertEqual(invocation["selected_modules"], ["cld1010lp", "keithley2182a", "keithley6221_relay_host"])
            events = [json.loads(line) for line in next(config.nkt.output_directory.glob("*.jsonl")).read_text().splitlines()]
            self.assertEqual(next(e for e in events if e["phase"] == "invocation")["config_sha256"], invocation["config_sha256"])
            with closing(sqlite3.connect(config.database_path)) as database:
                saved = json.loads(database.execute("SELECT record_json FROM photo_runs").fetchone()[0])
                self.assertEqual(saved["invocation"], invocation)
                stored_events = [json.loads(row[0]) for row in database.execute("SELECT event_json FROM photo_events")]
                self.assertEqual(next(e for e in stored_events if e["phase"] == "invocation")["config_path"], str(path.resolve()))

    def test_config_override_and_legacy_flags_still_work(self):
        with tempfile.TemporaryDirectory() as directory:
            path, config = self.fixture(directory, "custom.toml")
            status, loader, _ = self.invoke(directory, config,
                ["run", "--config", "custom.toml", "--authorize-connect", "--authorize-writes", "--confirm-route"])
            self.assertEqual(status, 0)
            loader.assert_called_once_with(str(path.resolve()))

    def test_selected_environment_and_smu_receive_run_permission(self):
        with tempfile.TemporaryDirectory() as directory:
            _, base = self.fixture(directory)
            config = configuration("cld")
            hardware = replace(base.hardware, detector={**base.hardware.detector, "voltage_range_v": 1.})
            smu = replace(config.smu, wiring_confirmed=True,
                expected_idn={role: "KEITHLEY INSTRUMENTS INC.,MODEL 2400,FAKE,1.0" for role in config.smu.roles})
            config = replace(config, nkt=base.nkt, hardware=hardware, smu=smu, environment=environment_config())
            status, _, acquisition = self.invoke(directory, config, ["run"])
            self.assertEqual(status, 0)
            args = acquisition.call_args.args[1]
            self.assertTrue(args.authorize_environment and args.authorize_smu)

    def test_missing_default_never_falls_back_to_old_trial_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            _, config = self.fixture(directory, "config/photoresponse_trial/11_nkt_630nm_dark_50-300uW_7pt_2182.local.toml")
            status, _, acquisition = self.invoke(directory, config, ["run"])
            self.assertEqual(status, 2)
            acquisition.assert_not_called()

    def test_wiring_check_still_blocks_before_acquisition(self):
        with tempfile.TemporaryDirectory() as directory:
            _, config = self.fixture(directory)
            config = replace(config, hardware=replace(config.hardware, wiring_confirmed=False))
            status, _, acquisition = self.invoke(directory, config, ["run"])
            self.assertEqual(status, 2)
            acquisition.assert_not_called()

    def test_invalid_power_targets_reject_with_real_loader_before_any_factory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.toml"
            text = (ROOT / "config/photoresponse_dark_nkt_simulation.toml").read_text()
            doc = tomllib.loads(text)
            import re
            text = re.sub(r'^power_points\s*=.*$',
                f'power_points = ["dark", {doc["power_feedback"]["max_power_w"] * 2}]', text, flags=re.MULTILINE)
            path.write_text(text)
            with patch.object(cli, "_run") as acquisition, patch("ctypes.CDLL") as dll, \
                    redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["run", "--config", str(path)]), 2)
            acquisition.assert_not_called()
            dll.assert_not_called()

    def test_plain_run_keeps_existing_fake_acquisition_and_overload_cleanup(self):
        for overload in (False, True):
            with self.subTest(overload=overload), tempfile.TemporaryDirectory() as directory:
                path, _ = self.fixture(directory)
                runner, host, meter, electrical, backend = hardware_setup("open_circuit_voltage")
                config = replace(runner.config, nkt=replace(runner.config.nkt, output_directory=Path(directory)/"output"))
                runner.config = config
                if overload:
                    query = meter.query
                    meter.query = lambda command: "+9.9E37" if command == "READ?" else query(command)
                def factory(*args, **kwargs):
                    self.assertTrue(kwargs["authorize_writes"] and kwargs["confirm_manual_route"])
                    runner.on_event = kwargs["on_event"]
                    return runner
                with patch.object(cli, "load_photoresponse_config", return_value=config), \
                        patch("attodry_control.photoresponse_cld.TL4000", return_value=backend), \
                        patch("attodry_control.photoresponse_transport.visa_resources", return_value=nullcontext(electrical.transports)), \
                        patch("attodry_control.photoresponse_hardware.HardwareElectrical", return_value=electrical), \
                        patch.object(cli, "PhotoresponseRunner", side_effect=factory), \
                        patch("attodry_control.photoresponse_environment.open_environment") as environment, \
                        patch("attodry_control.photoresponse_smu.PhotoresponseSmu") as smu, \
                        patch("attodry_control.photoresponse_power.open_photoresponse_power") as power, \
                        redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(cli.main(["run", "--config", str(path)]), 1 if overload else 0)
                environment.assert_not_called(); smu.assert_not_called(); power.assert_not_called()
                self.assertNotIn("OUTP:STAT 1", host.commands)
                record = json.loads(next(config.nkt.output_directory.glob("*_rejected.json" if overload else "*_completed.json")).read_text())
                self.assertTrue(record["cleanup"]["laser_off_confirmed"])
                self.assertTrue(record["cleanup"]["electrical_safe_confirmed"])
                if overload:
                    self.assertEqual(record["invocation"]["authorization_source"], "run_command")
                    self.assertEqual(record["conditions"][0]["windows"][0]["samples"][0]["detector_last_raw_reading"], "+9.9E37")

    def test_changed_config_during_loading_rejects_before_acquisition(self):
        with tempfile.TemporaryDirectory() as directory:
            path, config = self.fixture(directory)
            def changed(value):
                path.write_text("# changed during parsing\n")
                return config
            previous = Path.cwd()
            try:
                os.chdir(directory)
                with patch.object(cli, "load_photoresponse_config", side_effect=changed), \
                        patch.object(cli, "_run") as acquisition, redirect_stderr(io.StringIO()):
                    self.assertEqual(cli.main(["run"]), 2)
                    acquisition.assert_not_called()
            finally:
                os.chdir(previous)

    def test_diagnostic_and_bench_commands_keep_their_existing_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path, config = self.fixture(directory)
            for command in ("diagnose", "test-6221"):
                with patch.object(cli, "_diagnose") as diagnose:
                    status, _, acquisition = self.invoke(directory, config, [command, "--config", str(path)])
                self.assertEqual(status, 2)
                diagnose.assert_not_called()
                acquisition.assert_not_called()

    def test_module_command_from_project_root_does_not_use_another_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            foreign = Path(directory) / "attodry_control"
            foreign.mkdir()
            (foreign / "__init__.py").write_text("raise RuntimeError('wrong checkout')\n")
            result = subprocess.run([sys.executable, "-s", "-B", "-m", "attodry_control.photoresponse_cli", "describe"],
                cwd=ROOT, env={**os.environ, "PYTHONPATH": directory, "PYTHONNOUSERSITE": "1"},
                capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["run"]["default_config"], DEFAULT)


if __name__ == "__main__":
    unittest.main()
