import ast
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from attodry_control import three_smu_cli
from attodry_control.keithley2400 import KeithleyReading
from attodry_control.three_smu import (
    ThreeSmuSafetyError,
    ThreeSmuSample,
    TimedReading,
)
from attodry_control.three_smu_cli import run


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class _FakeLivePublisher:
    """Keep CLI tests offline without binding the fixed Notebook port."""

    endpoint = "http://127.0.0.1:8765/events"

    def __init__(self) -> None:
        self.events = []
        self.closed = False

    def start(self, plan, *, total_samples) -> None:
        self.events.append(("started", plan.mode.value, total_samples))

    def publish_sample(self, sample) -> None:
        self.events.append(("sample", sample))

    def finish(self, *, status, error=None) -> None:
        self.events.append(("finished", status, error))

    def close(self) -> None:
        self.closed = True


OPERATION_TEXT = """
[smu_bias]
model = "Keithley2400"
address = "FAKE::1"
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
        self.opened = []

    def open_resource(self, address):
        self.opened.append(address)
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
            self.assertEqual(document["active_roles"], ["smu_bias"])
            self.assertEqual(document["off_roles"], ["gate_top", "gate_bottom"])

    def test_hold_requires_exact_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "hardware.local.toml"
            config.write_text(
                OPERATION_TEXT.replace(
                    'finish_action = "zero_disable"', 'finish_action = "hold"'
                ),
                encoding="utf-8",
            )
            responses = iter(("not holding",))
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

    def test_run_starts_without_confirmation_and_passes_internal_authorizations(
        self,
    ) -> None:
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
                input_fn=lambda _prompt: self.fail("zero-disable must not prompt"),
                session_open=fake_session_open,
                live_publisher_factory=_FakeLivePublisher,
            )
            self.assertEqual(result, 0)
            self.assertEqual(
                opened["kwargs"],
                {"authorize_writes": True, "authorize_status_consumption": True},
            )

    def test_live_endpoint_failure_stops_before_session_open(self) -> None:
        class BrokenPublisher(_FakeLivePublisher):
            def start(self, plan, *, total_samples) -> None:
                raise OSError("port is already in use")

        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "hardware.local.toml"
            config.write_text(OPERATION_TEXT, encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "No QCoDeS/VISA resource was opened"):
                run(
                    ["run", "--config", str(config)],
                    session_open=lambda *_args, **_kwargs: self.fail("must not open"),
                    live_publisher_factory=BrokenPublisher,
                )

    def test_run_panel_displays_queued_clean_samples_without_status_queue(self) -> None:
        def sample(point_index: int) -> ThreeSmuSample:
            return ThreeSmuSample(
                point_index=point_index,
                repeat_index=0,
                segment="time_trace",
                elapsed_s=0.25 * (point_index + 1),
                coordinates={"smu_bias": 0.1},
                readings={
                    "smu_bias": TimedReading(
                        "2026-09-01T00:00:00+00:00",
                        KeithleyReading(
                            voltage_v=0.1,
                            current_a=5e-7,
                            source_setpoint=0.1,
                            output_enabled=True,
                            compliance_trip=False,
                            status='0,"No error"',
                            status_query_consumed=True,
                        ),
                    )
                },
                clean=True,
                problems=(),
            )

        class FakeSession:
            last_run_dir = Path("fake-run")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def run(self, **kwargs):
                for item in (sample(0), sample(1)):
                    kwargs["on_sample"](item)
                    yield item

        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "hardware.local.toml"
            config.write_text(OPERATION_TEXT, encoding="utf-8")
            output: list[str] = []
            with patch(
                "attodry_control.three_smu_cli.shutil.get_terminal_size",
                return_value=os.terminal_size((120, 24)),
            ):
                result = run(
                    ["run", "--config", str(config)],
                    input_fn=lambda _prompt: self.fail("zero-disable must not prompt"),
                    print_fn=output.append,
                    session_open=lambda *_args, **_kwargs: FakeSession(),
                    live_publisher_factory=_FakeLivePublisher,
                )
        rendered = "\n".join(map(str, output))
        self.assertEqual(result, 0)
        self.assertEqual(rendered.count("Role        │ Setpoint rb"), 1)
        self.assertIn("[1/2]  repeat 1/1 · segment time_trace", rendered)
        self.assertIn("[2/2]  repeat 1/1 · segment time_trace", rendered)
        self.assertIn("100 mV", rendered)
        self.assertIn("500 nA", rendered)
        self.assertIn("200 kΩ", rendered)
        self.assertIn("CLEAN", rendered)
        self.assertNotIn("status/error queue:", rendered)

    def test_run_panel_compact_view_covers_all_active_roles(self) -> None:
        operation_text = (
            OPERATION_TEXT.replace('mode = "time_trace"', 'mode = "multi_smu_map"')
            .replace(
                'role = "fixed"\nbidirectional = false\nfixed = 0.1',
                'role = "sweep"\nbidirectional = false\npoints = [0.1]',
            )
            .replace(
                'role = "off"\nbidirectional = false\n\n[three_smu_run.gate_bottom]',
                'role = "fixed"\nbidirectional = false\nfixed = 0.2\n\n'
                '[three_smu_run.gate_bottom]',
                1,
            )
            .replace(
                'role = "off"\nbidirectional = false\n',
                'role = "fixed"\nbidirectional = false\nfixed = 0.3\n',
                1,
            )
        )
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "hardware.local.toml"
            config.write_text(operation_text, encoding="utf-8")
            operation = three_smu_cli.load_three_smu_operation_config(config)
        sample = ThreeSmuSample(
            point_index=0,
            repeat_index=0,
            segment="map",
            elapsed_s=1.25,
            coordinates={"smu_bias": 0.1, "gate_top": 0.2, "gate_bottom": 0.3},
            readings={
                role: TimedReading(
                    "2026-09-01T00:00:00+00:00",
                    KeithleyReading(
                        voltage_v=value,
                        current_a=value * 1e-6,
                        source_setpoint=value,
                        output_enabled=True,
                        compliance_trip=False,
                        status='0,"No error"',
                        status_query_consumed=True,
                    ),
                )
                for role, value in (
                    ("smu_bias", 0.1),
                    ("gate_top", 0.2),
                    ("gate_bottom", 0.3),
                )
            },
            clean=True,
            problems=(),
        )
        output: list[str] = []
        with patch(
            "attodry_control.three_smu_cli.shutil.get_terminal_size",
            return_value=os.terminal_size((80, 24)),
        ):
            three_smu_cli._print_run_sample(
                sample,
                sample_number=1,
                total_samples=1,
                hardware=operation.hardware,
                plan=operation.plan,
                show_table_header=True,
                print_fn=output.append,
            )
        rendered = "\n".join(output)
        self.assertIn("compact terminal view", rendered)
        self.assertNotIn("Role        │ Setpoint rb", rendered)
        self.assertIn("smu_bias: src=100 mV", rendered)
        self.assertIn("gate_top: src=200 mV", rendered)
        self.assertIn("gate_bottom: src=300 mV", rendered)

    def test_run_panel_displays_problem_status_from_queued_sample(self) -> None:
        problem_sample = ThreeSmuSample(
            point_index=0,
            repeat_index=0,
            segment="time_trace",
            elapsed_s=0.25,
            coordinates={"smu_bias": 0.1},
            readings={
                "smu_bias": TimedReading(
                    "2026-09-01T00:00:00+00:00",
                    KeithleyReading(
                        voltage_v=0.1,
                        current_a=5e-7,
                        source_setpoint=0.1,
                        output_enabled=True,
                        compliance_trip=True,
                        status='-200,"Execution error"',
                        status_query_consumed=True,
                    ),
                )
            },
            clean=False,
            problems=("smu_bias compliance trip",),
        )

        class FakeSession:
            last_run_dir = Path("fake-run")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def run(self, **kwargs):
                kwargs["on_sample"](problem_sample)
                raise ThreeSmuSafetyError("smu_bias compliance trip")
                yield problem_sample

        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "hardware.local.toml"
            config.write_text(OPERATION_TEXT, encoding="utf-8")
            output: list[str] = []
            with self.assertRaisesRegex(ThreeSmuSafetyError, "compliance trip"):
                run(
                    ["run", "--config", str(config)],
                    input_fn=lambda _prompt: self.fail("zero-disable must not prompt"),
                    print_fn=output.append,
                    session_open=lambda *_args, **_kwargs: FakeSession(),
                    live_publisher_factory=_FakeLivePublisher,
                )
        rendered = "\n".join(output)
        self.assertIn("PROBLEM", rendered)
        self.assertIn("status/error queue:", rendered)
        self.assertIn('-200,"Execution error"', rendered)
        self.assertIn("smu_bias compliance trip", rendered)

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
            self.assertEqual(manager.opened, ["FAKE::1"])
            self.assertTrue(resources["FAKE::1"].closed)
            self.assertFalse(resources["FAKE::2"].closed)
            self.assertFalse(resources["FAKE::3"].closed)
            self.assertNotIn(":READ?", resources["FAKE::1"].queries)
            self.assertIn(
                "live V/I/R and trip state unavailable", output.getvalue()
            )
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
                ":SYST:ERR?" in consuming_resources["FAKE::1"].queries
            )
            self.assertEqual(consuming_resources["FAKE::2"].queries, [])
            self.assertEqual(consuming_resources["FAKE::3"].queries, [])

    def test_monitor_live_keyboard_interrupt_closes_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "hardware.local.toml"
            config_path.write_text(OPERATION_TEXT, encoding="utf-8")
            operation = three_smu_cli.load_three_smu_operation_config(config_path)
            resource = FakeMonitorResource("KEITHLEY,2400,bias,1")
            manager = FakeMonitorManager(
                {
                    "FAKE::1": resource,
                    "FAKE::2": FakeMonitorResource("KEITHLEY,2400,top,1"),
                    "FAKE::3": FakeMonitorResource("KEITHLEY,2400,bottom,1"),
                }
            )
            output = io.StringIO()

            def interrupt(_seconds: float) -> None:
                raise KeyboardInterrupt()

            with patch(
                "attodry_control.three_smu_cli.load_three_smu_operation_config",
                return_value=operation,
            ), redirect_stdout(output):
                result = run(
                    ["monitor-live", "--samples", "2", "--interval-s", "0"],
                    monitor_resource_manager_factory=lambda: manager,
                    sleep=interrupt,
                )
            self.assertEqual(result, 130)
            self.assertIn("Three-SMU live monitor stopped.", output.getvalue())
            self.assertTrue(resource.closed)
            self.assertTrue(manager.closed)

    def test_unified_notebook_is_clean_syntax_valid_and_has_no_hardware_path(self) -> None:
        notebook = PROJECT_ROOT / "notebooks" / "three_smu.ipynb"
        self.assertFalse((PROJECT_ROOT / "notebooks" / "three_smu_live.ipynb").exists())
        self.assertFalse((PROJECT_ROOT / "notebooks" / "three_smu_analysis.ipynb").exists())
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
        self.assertNotIn("qcodes", code.lower())
        self.assertNotIn("ThreeSmuSession", code)
        self.assertNotIn("three_smu_cli", code)
        self.assertIn("ipywidgets", code)
        self.assertIn("Connect live run", code)
        self.assertIn("Series:", code)
        self.assertIn("Slice:", code)
        self.assertIn("load_three_smu_plot_samples", code)


if __name__ == "__main__":
    unittest.main()
