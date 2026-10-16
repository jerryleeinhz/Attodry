"""Public modular examples through SQLite monitor and unified plots; fakes only."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import tempfile
import unittest

from attodry_control import combination_cli
from attodry_control.photoresponse_plotting import load_photo_plot_rows
from attodry_control.unified_plotting import render_plot


ROOT = Path(__file__).resolve().parents[1]


class ModularPlottingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.directory = Path(cls.tmp.name)
        cls.records, cls.paths = {}, {}
        for name in ("optics", "electrical", "environment"):
            source = ROOT / "config" / f"modular_{name}_simulation.toml"
            before = hashlib.sha256(source.read_bytes()).hexdigest()
            copied = cls.directory / name / "config" / source.name
            copied.parent.mkdir(parents=True)
            shutil.copyfile(source, copied)
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                status = combination_cli.main(["simulate", "--config", str(copied), "--json"])
            if status:
                raise AssertionError(f"{name} public simulation returned {status}: {stderr.getvalue()} {stdout.getvalue()[-2000:]}")
            record = json.loads(stdout.getvalue())
            cls.records[name] = record
            cls.paths[name] = copied
            if hashlib.sha256(source.read_bytes()).hexdigest() != before:
                raise AssertionError("A checked-in example was modified by the offline run")

    def test_examples_monitor_counts_cleanup_and_no_false_simultaneity(self):
        for name, expected in (("optics", 8), ("electrical", 3), ("environment", 4)):
            with self.subTest(name=name):
                stdout, stderr = io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    status = combination_cli.main(["monitor", "--config", str(self.paths[name]), "--once", "--json"])
                self.assertEqual(status, 0, stderr.getvalue())
                snapshot = json.loads(stdout.getvalue())
                self.assertEqual(snapshot["status"], "completed")
                self.assertEqual(snapshot["total_conditions"], expected)
                self.assertEqual(snapshot["collected_conditions"], expected)
                self.assertEqual(snapshot["accepted_conditions"], expected)
                self.assertTrue(snapshot["cleanup"]["clean"])
                self.assertIsNone(snapshot["error"])
                self.assertEqual(snapshot["process_liveness"], "unknown")

    def test_json_sqlite_projection_agree_and_dark_matching_remains_exact(self):
        for name, record in self.records.items():
            with self.subTest(name=name):
                database = Path(record["measurement_config"]["database_path"])
                paths = list(Path(record["journal_path"]).parent.glob("*_completed.json"))
                self.assertEqual(len(paths), 1)
                json_rows = load_photo_plot_rows(paths[0])
                rows = load_photo_plot_rows(database, run_id=record["run_id"])
                self.assertEqual(json_rows, rows)
                self.assertEqual(len(rows), len(record["conditions"]))
                self.assertTrue(all(row["accepted"] is True for row in rows))
                if name == "optics":
                    dark = {row["condition_id"]: row for row in rows if row["optical_state"] == "dark"}
                    self.assertEqual(len(dark), 2)
                    for row in rows:
                        if row["optical_state"] == "dark":
                            self.assertIsNone(row["requested.optical_target_power_w"])
                            self.assertIsNone(row["measured.delta_voltage_v"])
                        else:
                            baseline = dark[row["dark_condition_id"]]
                            self.assertEqual(row["requested.optical_wavelength_nm"], baseline["requested.optical_wavelength_nm"])
                            self.assertAlmostEqual(row["measured.delta_voltage_v"],
                                row["measured.detector_mean_voltage_v"] - baseline["measured.detector_mean_voltage_v"])
                else:
                    self.assertTrue(all(row["requested.optical_target_power_w"] is None for row in rows))

    def test_unified_optics_actual_power_curve_renders_without_source_stack(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        record = self.records["optics"]
        rows = load_photo_plot_rows(record["measurement_config"]["database_path"], run_id=record["run_id"])
        figure, plotted = render_plot(rows, {"x": "actual.optical_power_w", "y": "measured.delta_voltage_v",
            "group_by": None, "filters": {"requested.optical_state": ["light"], "requested.optical_wavelength_nm": [594.4]}})
        try:
            self.assertEqual(len(plotted["rows"]), 3)
            self.assertEqual(len(figure.axes[0].lines), 1)
            self.assertIn("monitor", figure.axes[0].get_xlabel().lower())
            self.assertIn("light minus dark", figure.axes[0].get_ylabel().lower())
            output = self.directory / "actual_power_voltage.png"
            figure.savefig(output, dpi=100)
            self.assertGreater(output.stat().st_size, 1000)
        finally:
            plt.close(figure)

    def test_unified_current_voltage_and_temperature_curves_render(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        for name, x, filters, count in (("electrical", "requested.bias_current_a", {}, 3),
                ("environment", "actual.temperature_k", {"requested.field_x_t": [0.0], "requested.field_z_t": [0.0]}, 2)):
            record = self.records[name]
            rows = load_photo_plot_rows(record["measurement_config"]["database_path"], run_id=record["run_id"])
            figure, plotted = render_plot(rows, {"x": x, "y": "measured.detector_mean_voltage_v",
                "group_by": None, "filters": filters})
            try:
                self.assertEqual(len(plotted["rows"]), count)
                self.assertTrue(figure.axes[0].lines)
            finally:
                plt.close(figure)

    def test_module_only_readbacks_do_not_invent_a_voltage_detector(self):
        source = self.paths["environment"].read_text(encoding="utf-8").replace(
            'order = ["temperature", "magnetic", "nanovoltmeter"]', 'order = ["temperature", "magnetic"]')
        path = self.directory / "module_only" / "config" / "readbacks.toml"
        path.parent.mkdir(parents=True)
        path.write_text(source, encoding="utf-8")
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = combination_cli.main(["simulate", "--config", str(path), "--json"])
        self.assertEqual(status, 0, stderr.getvalue())
        record = json.loads(stdout.getvalue())
        self.assertIsNone(record["measurement_config"]["quantity"])
        rows = load_photo_plot_rows(record["measurement_config"]["database_path"], run_id=record["run_id"])
        self.assertEqual(len(rows), 4)
        for row in rows:
            self.assertIsNone(row["quantity"])
            self.assertEqual(set(row["selected_modules"]), {"temperature", "magnetic"})
            self.assertIsNotNone(row["actual.temperature_k"])
            self.assertIsNotNone(row["actual.field_z_t"])
            self.assertEqual(row["detector_sample_count"], 0)
            self.assertIsNone(row["measured.detector_mean_voltage_v"])

    def test_public_analysis_export_preserves_raw_files(self):
        for name, count in (("optics", 6), ("electrical", 3), ("environment", 4)):
            with self.subTest(name=name):
                record = self.records[name]
                path = next(Path(record["journal_path"]).parent.glob("*_completed.json"))
                before = hashlib.sha256(path.read_bytes()).hexdigest()
                stdout, stderr = io.StringIO(), io.StringIO()
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    status = combination_cli.main(["analyze", "--record", str(path)])
                self.assertEqual(status, 0, stderr.getvalue())
                rows = json.loads(stdout.getvalue())
                self.assertEqual(len(rows), count)
                self.assertTrue(all(row["accepted"] for row in rows))
                output = self.directory / f"{name}_export.csv"
                with redirect_stdout(io.StringIO()), redirect_stderr(stderr):
                    status = combination_cli.main(["export", "--record", str(path), "--output", str(output)])
                self.assertEqual(status, 0, stderr.getvalue())
                self.assertTrue(output.is_file())
                manifest = json.loads(output.with_suffix(".manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(manifest["rows"], count)
                self.assertEqual(manifest["sha256"], before)
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)


class ModularSmuPlottingTests(unittest.TestCase):
    def test_optics_plus_smu_without_nanovoltmeter_matches_module_dark_means(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        source_path = ROOT/"config/photoresponse_smu_nkt_simulation.toml"
        source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        source = re.sub(r"\[photoresponse_combination\][\s\S]*?(?=\n\[)",
            '[combination_scan]\norder=["optics","smu"]\nbackend="simulation"\n'
            'samples_per_condition=2\noutput_directory="../run_data/optics_smu"\n',
            source_path.read_text(encoding="utf-8"), count=1)
        source += '''
[optics]
laser="nkt"
use_pem=false
use_power_meter=false
mode="direct"
initial_source_level_pct=5.0

[optics_scan]
wavelength_points_nm=[630.0]
bandwidth_points_nm=[10.0]
dark_first=true
'''
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"config"/"optics_smu.local.toml"
            path.parent.mkdir()
            path.write_text(source, encoding="utf-8")
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                status = combination_cli.main(["simulate", "--config", str(path), "--json"])
            self.assertEqual(status, 0, stderr.getvalue())
            record = json.loads(stdout.getvalue())
            self.assertIsNone(record["measurement_config"]["quantity"])
            self.assertEqual(record["measurement_config"]["detector"], "none")
            self.assertEqual(len(record["conditions"]), 4)
            self.assertTrue(record["cleanup"]["smu_safe_confirmed"])
            self.assertTrue(record["cleanup"]["laser_off_confirmed"])
            conditions = {row["condition_id"]: row for row in record["conditions"]}
            raw_path = next(Path(record["journal_path"]).parent.glob("*_completed.json"))
            rows = load_photo_plot_rows(record["measurement_config"]["database_path"], run_id=record["run_id"])
            self.assertEqual(rows, load_photo_plot_rows(raw_path))
            light = [row for row in rows if row["optical_state"] == "light"]
            self.assertEqual(len(light), 2)
            for row in light:
                condition = conditions[row["condition_id"]]
                baseline = conditions[row["dark_condition_id"]]
                self.assertEqual(condition["requested_smu"]["coordinates"], baseline["requested_smu"]["coordinates"])
                expected = (condition["result"]["module_means"]["smu"]["gate_bottom_leakage_current_a"]
                            - baseline["result"]["module_means"]["smu"]["gate_bottom_leakage_current_a"])
                self.assertEqual(row["measured.delta_smu_gate_bottom_leakage_current_a"], expected)
                self.assertAlmostEqual(expected, 0.) # Synthetic gate model has no illumination response.
                self.assertIsNone(row["quantity"])
                self.assertIsNone(row["measured.detector_mean_voltage_v"])
                self.assertNotIn("current_source", condition["result"]["module_delta"])
                self.assertNotIn("optics", condition["result"]["module_delta"])
                self.assertGreater(row["baseline_age_s"], 0.)
            figure, plotted = render_plot(rows, {"x": "requested.gate_bottom_v",
                "y": "measured.delta_smu_gate_bottom_leakage_current_a", "group_by": None,
                "filters": {"requested.optical_state": ["light"]}})
            try:
                self.assertEqual(len(plotted["rows"]), 2)
                self.assertTrue(figure.axes[0].lines)
            finally:
                plt.close(figure)
        self.assertEqual(hashlib.sha256(source_path.read_bytes()).hexdigest(), source_hash)

    def test_source_only_smu_and_gate_leakage_survive_public_analysis(self):
        """A source-only module measures its own I/V without an external meter."""
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        source_path = ROOT/"config/photoresponse_smu_cld_simulation.toml"
        source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        source = re.sub(r"\[photoresponse_combination\][\s\S]*?(?=\n\[)",
            '[combination_scan]\norder=["smu"]\nbackend="simulation"\n'
            'samples_per_condition=2\noutput_directory="../run_data/smu_only"\n',
            source_path.read_text(encoding="utf-8"), count=1)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"config"/"smu.local.toml"
            path.parent.mkdir()
            path.write_text(source, encoding="utf-8")
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                status = combination_cli.main(["simulate", "--config", str(path), "--json"])
            self.assertEqual(status, 0, stderr.getvalue())
            record = json.loads(stdout.getvalue())
            self.assertEqual(record["measurement_config"]["combination"]["selected_modules"], ["smu"])
            self.assertIsNone(record["measurement_config"]["nkt"])
            self.assertIsNone(record["measurement_config"]["quantity"])
            self.assertEqual(record["measurement_config"]["detector"], "none")
            self.assertEqual(len(record["conditions"]), 4)
            self.assertTrue(record["cleanup"]["smu_safe_confirmed"])
            raw_path = next(Path(record["journal_path"]).parent.glob("*_completed.json"))
            rows = load_photo_plot_rows(record["measurement_config"]["database_path"], run_id=record["run_id"])
            self.assertEqual(load_photo_plot_rows(raw_path), rows)
            self.assertEqual(len(rows), 4)
            for row, condition in zip(rows, record["conditions"]):
                self.assertEqual(condition["windows"][0]["phase"], "none")
                fresh = condition["windows"][0]["samples"][-1]["smu_after"]["readings"]
                self.assertAlmostEqual(row["measured.smu_bias_current_a"], fresh["smu_bias"]["reading"]["current_a"])
                self.assertAlmostEqual(row["measured.smu_bias_voltage_v"], fresh["smu_bias"]["reading"]["voltage_v"])
                self.assertAlmostEqual(row["measured.gate_bottom_leakage_current_a"], fresh["gate_bottom"]["reading"]["current_a"])
                self.assertAlmostEqual(row["actual.gate_bottom_v"], fresh["gate_bottom"]["reading"]["source_setpoint"])
                self.assertNotIn("measured.gate_bottom_current_a", row)
                self.assertIsNone(row["quantity"])
                self.assertIsNone(row["measured.detector_mean_voltage_v"])
                self.assertEqual(row["detector_sample_count"], 0)
            for x in ("requested.gate_bottom_v", "actual.gate_bottom_v"):
                figure, plotted = render_plot(rows, {"x": x, "y": "measured.gate_bottom_leakage_current_a",
                    "group_by": None, "filters": {"requested.smu_bias_a": [1e-9], "requested.gate_top_v": [0.]}})
                try:
                    self.assertEqual(len(plotted["rows"]), 2)
                    self.assertTrue(figure.axes[0].lines)
                    output = Path(directory)/(x.replace(".", "_")+".png")
                    figure.savefig(output, dpi=100)
                    self.assertGreater(output.stat().st_size, 1000)
                finally:
                    plt.close(figure)
            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                status = combination_cli.main(["analyze", "--record", str(raw_path)])
            self.assertEqual(status, 0, stderr.getvalue())
            self.assertTrue(all("smu_bias_current_a" in row and "gate_bottom_leakage_current_a" in row
                                for row in json.loads(stdout.getvalue())))
        self.assertEqual(hashlib.sha256(source_path.read_bytes()).hexdigest(), source_hash)


if __name__ == "__main__":
    unittest.main()
