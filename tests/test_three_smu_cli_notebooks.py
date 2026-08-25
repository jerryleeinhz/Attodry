import ast
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from attodry_control.three_smu_cli import run


PROJECT_ROOT = Path(__file__).resolve().parents[1]


HARDWARE_TEXT = """
[smu_bias]
model = "Keithley2400"
address = "FAKE::1"
timeout_ms = 1000
source_mode = "voltage"
compliance_current_a = 0.001
compliance_voltage_v = 10.0
max_abs_voltage_v = 10.0
max_abs_current_a = 0.001
source_min_v = -1.0
source_max_v = 1.0
ramp_step_v = 0.1
readback_tolerance_v = 0.000001
source_min_a = -0.001
source_max_a = 0.001
ramp_step_a = 0.0001
readback_tolerance_a = 0.000000001
settle_s = 0.0
nplc = 1.0
source_auto_range = true
measure_auto_range = true
four_wire = false

[gate_top]
model = "Keithley2400"
address = "FAKE::2"
timeout_ms = 1000
source_mode = "voltage"
compliance_current_a = 0.001
compliance_voltage_v = 10.0
max_abs_voltage_v = 10.0
max_abs_current_a = 0.001
source_min_v = -1.0
source_max_v = 1.0
ramp_step_v = 0.1
readback_tolerance_v = 0.000001
source_min_a = -0.001
source_max_a = 0.001
ramp_step_a = 0.0001
readback_tolerance_a = 0.000000001
settle_s = 0.0
nplc = 1.0
source_auto_range = true
measure_auto_range = true
four_wire = false
leakage_limit_a = 0.000001

[gate_bottom]
model = "Keithley2400"
address = "FAKE::3"
timeout_ms = 1000
source_mode = "voltage"
compliance_current_a = 0.001
compliance_voltage_v = 10.0
max_abs_voltage_v = 10.0
max_abs_current_a = 0.001
source_min_v = -1.0
source_max_v = 1.0
ramp_step_v = 0.1
readback_tolerance_v = 0.000001
source_min_a = -0.001
source_max_a = 0.001
ramp_step_a = 0.0001
readback_tolerance_a = 0.000000001
settle_s = 0.0
nplc = 1.0
source_auto_range = true
measure_auto_range = true
four_wire = false
leakage_limit_a = 0.000001
"""


SCAN_TEXT = """
[scan]
mode = "time_trace"
samples_per_point = 1
delay_s = 0.0
bidirectional = false
serpentine = false
finish_action = "zero_disable"
point_count = 2
pulse_high_s = 0.0
pulse_period_s = 0.0

[smu_bias]
role = "fixed"
fixed = 0.1
start = 0.0
stop = 0.0
step = 1.0

[gate_top]
role = "off"
fixed = 0.0
start = 0.0
stop = 0.0
step = 1.0

[gate_bottom]
role = "off"
fixed = 0.0
start = 0.0
stop = 0.0
step = 1.0
"""


class ThreeSmuCliNotebookTests(unittest.TestCase):
    def test_describe_is_fully_offline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            hardware = Path(directory) / "hardware.toml"
            scan = Path(directory) / "scan.toml"
            hardware.write_text(HARDWARE_TEXT, encoding="utf-8")
            scan.write_text(SCAN_TEXT, encoding="utf-8")
            output = io.StringIO()
            with redirect_stdout(output):
                result = run([
                    "describe", "--hardware", str(hardware), "--plan", str(scan)
                ])
            self.assertEqual(result, 0)
            document = json.loads(output.getvalue())
            self.assertFalse(document["hardware_opened"])
            self.assertEqual(document["generated_points"], 2)

    def test_run_without_write_authorization_stops_before_driver_import(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            hardware = Path(directory) / "hardware.toml"
            scan = Path(directory) / "scan.toml"
            hardware.write_text(HARDWARE_TEXT, encoding="utf-8")
            scan.write_text(SCAN_TEXT, encoding="utf-8")
            with self.assertRaisesRegex(SystemExit, "authorize-writes"):
                run([
                    "run", "--hardware", str(hardware), "--plan", str(scan),
                    "--output-dir", str(Path(directory) / "runs"),
                ])

    def test_hold_requires_a_separate_cli_authorization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            hardware = Path(directory) / "hardware.toml"
            scan = Path(directory) / "scan.toml"
            hardware.write_text(HARDWARE_TEXT, encoding="utf-8")
            scan.write_text(
                SCAN_TEXT.replace(
                    'finish_action = "zero_disable"', 'finish_action = "hold"'
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(SystemExit, "authorize-hold"):
                run([
                    "run", "--hardware", str(hardware), "--plan", str(scan),
                    "--output-dir", str(Path(directory) / "runs"),
                    "--authorize-writes",
                ])

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
