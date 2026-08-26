import ast
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from attodry_control import three_smu_cli
from attodry_control.three_smu_cli import run


PROJECT_ROOT = Path(__file__).resolve().parents[1]


OPERATION_TEXT = """
[smu_bias]
model = "Keithley2400"
address = "FAKE::1"
timeout_ms = 1000
source_mode = "voltage"
max_abs_voltage_v = 10.0
max_abs_current_a = 0.001
nplc = 1.0
source_auto_range = true
measure_auto_range = true
four_wire = false

[gate_top]
model = "Keithley2400"
address = "FAKE::2"
source_mode = "voltage"
max_abs_voltage_v = 10.0
max_abs_current_a = 0.001

[gate_top.smu]
timeout_ms = 1000
nplc = 1.0
source_auto_range = true
measure_auto_range = true
four_wire = false

[gate_bottom]
model = "Keithley2400"
address = "FAKE::3"
source_mode = "voltage"
max_abs_voltage_v = 10.0
max_abs_current_a = 0.001

[gate_bottom.smu]
timeout_ms = 1000
nplc = 1.0
source_auto_range = true
measure_auto_range = true
four_wire = false

[three_smu_run]
output_directory = "runs"
run_name = "fake"
note = "offline test"
mode = "time_trace"
samples_per_point = 1
delay_s = 0.0
serpentine = false
finish_action = "zero_disable"
point_count = 2
pulse_high_s = 0.0
pulse_period_s = 0.0

[three_smu_run.smu_bias]
role = "fixed"
bidirectional = false
fixed = 0.1

[three_smu_run.gate_top]
role = "off"
bidirectional = false

[three_smu_run.gate_bottom]
role = "off"
bidirectional = false
"""


class FakeMonitorResource:
    """A VISA fake with no write method, for CLI query-only coverage."""

    def __init__(self, identity: str):
        self.closed = False
        self.queries = []
        self.timeout = None
        self.responses = {
            "*IDN?": identity,
            ":SOUR:FUNC?": "VOLT",
            ":SOUR:VOLT?": "0.1",
            ":OUTP?": "0",
            ":READ?": "0.1,0.0000005,200000",
            ":SENS:CURR:PROT?": "0.001",
            ":SOUR:VOLT:RANG?": "1.0",
            ":SENS:CURR:RANG?": "0.001",
            ":SYST:RSEN?": "0",
            "SENS:CURR:PROT:TRIP?": "0",
            ":SYST:ERR?": "0,No error",
        }

    def query(self, command):
        self.queries.append(command)
        return self.responses[command]

    def close(self):
        self.closed = True


class FakeMonitorManager:
    def __init__(self, resources):
        self.resources = resources
        self.closed = False

    def open_resource(self, address):
        return self.resources[address]

    def close(self):
        self.closed = True


class ThreeSmuCliNotebookTests(unittest.TestCase):
    def test_legacy_split_toml_arguments_are_not_accepted(self) -> None:
        with self.assertRaises(SystemExit):
            three_smu_cli.build_parser().parse_args(
                ["describe", "--hardware", "hardware.toml"]
            )

    def test_describe_is_fully_offline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "hardware.local.toml"
            config.write_text(OPERATION_TEXT, encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                result = run(["describe", "--config", str(config)])
            self.assertEqual(result, 0)
            document = json.loads(output.getvalue())
            self.assertFalse(document["hardware_opened"])
            self.assertEqual(document["generated_points"], 2)

    def test_run_without_exact_confirmation_stops_before_driver_import(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "hardware.local.toml"
            config.write_text(OPERATION_TEXT, encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "was not authorized"):
                run(["run", "--config", str(config)], input_fn=lambda _prompt: "")

    def test_hold_requires_a_second_exact_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "hardware.local.toml"
            config.write_text(
                OPERATION_TEXT.replace(
                    'finish_action = "zero_disable"', 'finish_action = "hold"'
                ),
                encoding="utf-8",
            )
            responses = iter(("RUN THREE SMU", "not holding"))
            with self.assertRaisesRegex(SystemExit, "Hold was not authorized"):
                run(
                    ["run", "--config", str(config)],
                    input_fn=lambda _prompt: next(responses),
                )

    def test_default_config_path_is_used_for_describe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "hardware.local.toml"
            config_path.write_text(OPERATION_TEXT, encoding="utf-8")
            operation = three_smu_cli.load_three_smu_operation_config(config_path)
            output = io.StringIO()
            with patch(
                "attodry_control.three_smu_cli.load_three_smu_operation_config",
                return_value=operation,
            ) as loader, redirect_stdout(output):
                result = run(["describe"])
            self.assertEqual(result, 0)
            loader.assert_called_once_with(three_smu_cli.DEFAULT_CONFIG_PATH)
            self.assertIn('"hardware_opened": false', output.getvalue())

    def test_confirmed_run_passes_internal_authorizations_to_session(self) -> None:
        class FakeSession:
            last_run_dir = Path("fake-run")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def run(self, **_kwargs):
                return ()

        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "hardware.local.toml"
            config.write_text(OPERATION_TEXT, encoding="utf-8")
            opened = {}

            def fake_session_open(*args, **kwargs):
                opened["args"] = args
                opened["kwargs"] = kwargs
                return FakeSession()

            result = run(
                ["run", "--config", str(config)],
                input_fn=lambda _prompt: "RUN THREE SMU",
                session_open=fake_session_open,
            )
            self.assertEqual(result, 0)
            self.assertEqual(
                opened["kwargs"],
                {"authorize_writes": True, "authorize_status_consumption": True},
            )

    def test_monitor_live_uses_only_queries_and_skips_error_queue_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "hardware.local.toml"
            config_path.write_text(OPERATION_TEXT, encoding="utf-8")
            operation = three_smu_cli.load_three_smu_operation_config(config_path)
            resources = {
                "FAKE::1": FakeMonitorResource("KEITHLEY,2400,bias,1"),
                "FAKE::2": FakeMonitorResource("KEITHLEY,2400,top,1"),
                "FAKE::3": FakeMonitorResource("KEITHLEY,2400,bottom,1"),
            }
            manager = FakeMonitorManager(resources)
            output = io.StringIO()
            with patch(
                "attodry_control.three_smu_cli.load_three_smu_operation_config",
                return_value=operation,
            ), redirect_stdout(output):
                result = run(
                    ["monitor-live", "--samples", "1", "--interval-s", "0"],
                    monitor_resource_manager_factory=lambda: manager,
                )
            self.assertEqual(result, 0)
            self.assertIn("Three-SMU live status", output.getvalue())
            self.assertTrue(manager.closed)
            self.assertTrue(all(resource.closed for resource in resources.values()))
            self.assertTrue(
                all(":SYST:ERR?" not in resource.queries for resource in resources.values())
            )
            self.assertTrue(all(not hasattr(resource, "write") for resource in resources.values()))
            consuming_resources = {
                "FAKE::1": FakeMonitorResource("KEITHLEY,2400,bias,1"),
                "FAKE::2": FakeMonitorResource("KEITHLEY,2400,top,1"),
                "FAKE::3": FakeMonitorResource("KEITHLEY,2400,bottom,1"),
            }
            consuming_manager = FakeMonitorManager(consuming_resources)
            with patch(
                "attodry_control.three_smu_cli.load_three_smu_operation_config",
                return_value=operation,
            ), redirect_stdout(io.StringIO()):
                result = run(
                    [
                        "monitor-live", "--samples", "1", "--interval-s", "0",
                        "--consume-status-queue",
                    ],
                    monitor_resource_manager_factory=lambda: consuming_manager,
                )
            self.assertEqual(result, 0)
            self.assertTrue(
                all(
                    ":SYST:ERR?" in resource.queries
                    for resource in consuming_resources.values()
                )
            )

    def test_notebooks_are_clean_syntax_valid_and_obey_import_boundaries(self) -> None:
        live = PROJECT_ROOT / "notebooks" / "three_smu_live.ipynb"
        analysis = PROJECT_ROOT / "notebooks" / "three_smu_analysis.ipynb"
        for notebook in (live, analysis):
            document = json.loads(notebook.read_text(encoding="utf-8"))
            code = "\n\n".join(
                "".join(cell.get("source", ()))
                for cell in document["cells"]
                if cell["cell_type"] == "code"
            )
            ast.parse(code)
            for cell in document["cells"]:
                if cell["cell_type"] == "code":
                    self.assertIsNone(cell["execution_count"])
                    self.assertEqual(cell["outputs"], [])
        live_code = "\n".join(
            "".join(cell.get("source", ()))
            for cell in json.loads(live.read_text(encoding="utf-8"))["cells"]
            if cell["cell_type"] == "code"
        )
        analysis_code = "\n".join(
            "".join(cell.get("source", ()))
            for cell in json.loads(analysis.read_text(encoding="utf-8"))["cells"]
            if cell["cell_type"] == "code"
        )
        self.assertNotIn("qcodes", live_code.lower())
        self.assertIn("session.run", live_code)
        self.assertNotIn("from attodry_control.three_smu import", analysis_code)
        self.assertIn("attodry_control.three_smu_analysis", analysis_code)


if __name__ == "__main__":
    unittest.main()
