"""Common CLI/monitor/plotting integration: synthetic resources and files only."""
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from attodry_control import combination_cli, photoresponse_cli
from attodry_control.combination_registry import index_path, register_run, resolve_monitor
from attodry_control.combination_store import run_snapshot
from attodry_control.photoresponse_storage import PhotoStore
from attodry_control.photoresponse_plotting import load_photo_plot_rows
from attodry_control.unified_plotting import discover_plot_sources, load_plot_sources, render_plot, export_plot_bundle, UnifiedPlotDashboard
from test_photoresponse_dark_scan import shared
from test_photoresponse_power import config_for, setup


ROOT = Path(__file__).resolve().parents[1]


def acquisition(directory, *, kind="nkt", reject=False, unsafe=False, current=False):
    directory = Path(directory)
    config = shared(config_for(kind), mode="open_circuit_voltage", bias_points=(None,))
    if current:
        config = replace(config, mode="voltage_bias_current", detector="keithley2400", bias_points=(0.,))
    config = replace(config, nkt=replace(config.nkt, points=config.nkt.points[:1], output_directory=directory),
                     database_path=directory / "audit.sqlite")
    runner, _, _, power, _ = setup(kind, config=config)
    store = PhotoStore(config.database_path)
    run_id, _ = store.start(config)
    runner.on_event = store.event
    power.on_event = store.event
    runner.laser.on_event = store.event
    if reject:
        original = power.meter.resource.power_provider
        power.meter.resource.power_provider = lambda: original() + (
            .0001 if power.row and power.row["qualified"] and
            power.row["target_power_w"] == config.power.targets[-1] else 0)
    record = runner.run()
    if unsafe:
        record["completed"], record["outcome"] = False, "rejected"
        record["cleanup"]["laser_off_confirmed"] = False
        for c in record["conditions"]:
            c["accepted"] = False
    store.finalize(record)
    path = directory / ("result_" + record["outcome"] + ".json")
    path.write_text(json.dumps(record), encoding="utf-8")
    return config, record, path, store, run_id


class OperatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def fixture(self, **kwargs):
        values = acquisition(self.root, **kwargs)
        self.addCleanup(values[3].close)
        return values

    def test_monitor_snapshots_committed_progress_while_writer_remains_open(self):
        config = shared(config_for(), mode="open_circuit_voltage", bias_points=(None,))
        store = PhotoStore(self.root / "audit.sqlite")
        self.addCleanup(store.close)
        run_id, _ = store.start(config)
        store.event({"phase":"condition_start", "condition_id":"photo-000000", "attempt_index":1,
                     "optical_state":"dark", "target_power_w":None, "captured_at_utc":"2026-10-16T00:00:00+00:00"})
        snapshot = run_snapshot(store.path, run_id)
        self.assertEqual(snapshot["status"], "active")
        self.assertEqual(snapshot["current_attempt"]["optical_state"], "dark")
        self.assertEqual(snapshot["accepted_conditions"], 0)
        self.assertGreater(snapshot["last_event_age_s"], 0)
        self.assertEqual(snapshot["process_liveness"], "unknown")

    def test_collected_and_accepted_are_distinct_until_finalize(self):
        config, record, _, store, run_id = self.fixture()
        snapshot = run_snapshot(store.path, run_id)
        self.assertTrue(record["completed"], record["error"])
        self.assertEqual(snapshot["collected_conditions"], len(record["conditions"]))
        self.assertEqual(snapshot["accepted_conditions"], len(record["conditions"]))
        self.assertTrue(snapshot["cleanup"]["clean"])
        self.assertEqual(snapshot["last_recorded_readings"]["electrical"]["measurements"]["current_a"], None)

    def test_failure_reports_primary_error_and_independent_cleanup(self):
        _, record, _, store, run_id = self.fixture(reject=True)
        self.assertIn("Formal power drift", record["error"])
        self.assertIn("peak-to-peak", record["error"])
        snapshot = run_snapshot(store.path, run_id)
        self.assertEqual(snapshot["accepted_conditions"], 0)
        self.assertGreater(snapshot["collected_conditions"], 0)
        self.assertTrue(snapshot["cleanup"]["clean"])
        self.assertEqual(snapshot["error"], record["error"])
        self.assertGreater(snapshot["power_rejection"]["peak_to_peak_w"], snapshot["power_rejection"]["max_peak_to_peak_w"])

    def test_nested_config_registers_in_project_run_data_and_pins_monitor(self):
        _, _, _, store, run_id = self.fixture()
        config = self.root / "config/photoresponse_trial/trial.local.toml"
        config.parent.mkdir(parents=True)
        config.write_text("# config intentionally not needed to resolve registered monitor\n")
        register_run(config, store, run_id)
        self.assertEqual(index_path(config), self.root / "run_data/latest-combination-run.json")
        self.assertEqual(resolve_monitor(config), (store.path.resolve(), run_id))
        self.assertEqual(resolve_monitor(config, store.path, run_id), (store.path.resolve(), run_id))

    def test_monitor_old_schema_is_read_only_and_never_creates_progress_table(self):
        _, record, _, store, run_id = self.fixture(reject=True)
        with store.db:
            store.db.execute("DROP TABLE photo_progress")
        store.db.execute("PRAGMA wal_checkpoint(FULL)")
        before = hashlib.sha256(store.path.read_bytes()).hexdigest()
        snapshot = run_snapshot(store.path, run_id)
        self.assertEqual(snapshot["status"], "rejected")
        self.assertIn("Formal power drift", snapshot["error"])
        self.assertEqual(snapshot["collected_conditions"], sum(c["result"] is not None for c in record["conditions"]))
        self.assertEqual(hashlib.sha256(store.path.read_bytes()).hexdigest(), before)
        self.assertFalse(store.db.execute("SELECT 1 FROM sqlite_master WHERE name='photo_progress'").fetchone())

    def test_common_entry_and_legacy_entry_use_the_same_acquisition_service(self):
        with patch.object(photoresponse_cli, "main", return_value=7) as service:
            self.assertEqual(combination_cli.main(["run", "--config", "chosen.toml"]), 7)
        service.assert_called_once_with(["run", "--config", "chosen.toml"], entrypoint="combination_cli")

    def test_monitor_subprocess_imports_no_hardware_or_acquisition_modules(self):
        _, _, _, store, run_id = self.fixture()
        source = """
import sys
from attodry_control.combination_cli import main
status = main(['monitor', '--database', sys.argv[1], '--run-id', sys.argv[2], '--once', '--json'])
forbidden = {'pyvisa','qcodes','attodry_control.attodry','attodry_control.sr830','attodry_control.nkt_sdk','attodry_control.photoresponse_cli','attodry_control.photoresponse_hardware'}
assert not forbidden.intersection(sys.modules), forbidden.intersection(sys.modules)
raise SystemExit(status)
"""
        result = subprocess.run([sys.executable, "-s", "-B", "-c", source, str(store.path), run_id],
                                cwd=ROOT, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["run_id"], run_id)

    def test_json_and_sqlite_share_acceptance_and_difference_projection(self):
        for kind in ("nkt", "cld"):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                _, record, path, store, run_id = acquisition(directory, kind=kind)
                try:
                    json_rows = load_photo_plot_rows(path)
                    sql_rows = load_photo_plot_rows(store.path, run_id=run_id)
                    self.assertEqual(json_rows, sql_rows)
                    self.assertTrue(any(r["optical_state"] == "dark" for r in json_rows))
                    for row in json_rows:
                        if row["optical_state"] == "light":
                            self.assertAlmostEqual(row["measured.delta_voltage_v"],
                                row["measured.light_voltage_v"] - row["measured.dark_voltage_v"])
                            self.assertIsNotNone(row["dark_condition_id"])
                            self.assertGreater(row["baseline_age_s"], 0)
                    self.assertEqual(len(json_rows), len(record["conditions"]))
                finally:
                    store.close()

    def test_rejected_and_unsafe_runs_are_excluded_but_audit_retains_partial_data(self):
        for kwargs in ({"reject":True}, {"unsafe":True}):
            with self.subTest(kwargs=kwargs), tempfile.TemporaryDirectory() as directory:
                _, _, path, store, run_id = acquisition(directory, **kwargs)
                try:
                    self.assertEqual(load_photo_plot_rows(path), ())
                    self.assertEqual(load_photo_plot_rows(store.path, run_id=run_id), ())
                    rows = load_photo_plot_rows(path, audit=True)
                    self.assertTrue(rows)
                    self.assertTrue(all(r["accepted"] is False for r in rows))
                finally:
                    store.close()

    def test_invalid_raw_overload_remains_audit_text_and_never_a_numeric_plot_value(self):
        _, record, path, _, _ = self.fixture(reject=True)
        sample = record["conditions"][-1]["windows"][0]["samples"][0]
        sample["voltage_v"], sample["detector_last_raw_reading"] = 9.9e37, "+9.9E37"
        path.write_text(json.dumps(record), encoding="utf-8")
        row = load_photo_plot_rows(path, audit=True)[-1]
        self.assertEqual(row["raw.detector_readings"], ["+9.9E37"])
        self.assertIsNone(row["measured.detector_mean_voltage_v"])
        self.assertIsNone(row["measured.delta_voltage_v"])

    def test_discovery_widget_run_selection_and_loading_use_shared_interfaces(self):
        _, record, path, store, run_id = self.fixture()
        sources = discover_plot_sources(self.root)
        self.assertEqual({s.kind for s in sources}, {"combination_sqlite", "photoresponse"})
        self.assertEqual(len(load_plot_sources([store.path], run_ids={str(store.path.resolve()):[run_id]})), len(record["conditions"]))
        with redirect_stdout(io.StringIO()):
            dashboard = UnifiedPlotDashboard(self.root)
            dashboard.add_plot("curve")
            card = dashboard.cards[0]
            card["sources"]._controls[str(store.path.resolve())].value = True
            self.assertEqual(len(card["runs"].options), 1)
            card["runs"].value = (card["runs"].options[0][1],)
            dashboard._load_selected_runs(card)
            self.assertEqual(len(dashboard._selected_rows(card)), len(record["conditions"]))

    def test_power_difference_plot_uses_measured_power_and_does_not_invent_repeats(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        self.addCleanup(lambda: plt.close("all"))
        _, _, path, _, _ = self.fixture()
        rows = load_plot_sources([path])
        spec = {"x":"actual.optical_power_w", "y":"measured.delta_voltage_v",
                "filters":{"requested.optical_state":["light"]}, "statistics":"mean_sd"}
        figure, plotted = render_plot(rows, spec)
        self.assertTrue(plotted["rows"])
        self.assertTrue(all(p["n"] == 1 and p["sd"] is None for p in plotted["summary_rows"]))
        self.assertIn("Light minus dark", figure.axes[0].get_ylabel())

    def test_read_only_plotting_import_does_not_load_drivers(self):
        source = """
import sys
from attodry_control.unified_plotting import load_plot_sources
load_plot_sources([sys.argv[1]], include_audit=True)
forbidden = {'pyvisa','qcodes','attodry_control.attodry','attodry_control.sr830','attodry_control.photoresponse_cli','attodry_control.photoresponse_hardware'}
assert not forbidden.intersection(sys.modules), forbidden.intersection(sys.modules)
"""
        _, _, path, _, _ = self.fixture()
        result = subprocess.run([sys.executable, "-s", "-B", "-c", source, str(path)], cwd=ROOT,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_file_only_monitor_with_unknown_run_returns_actionable_error(self):
        _, _, _, store, _ = self.fixture()
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            status = combination_cli.main(["monitor", "--database", str(store.path), "--run-id", "missing", "--once"])
        self.assertEqual(status, 2)
        self.assertIn("Unknown photoresponse run", stderr.getvalue())

    def test_rejected_cli_prints_error_and_cleanup_without_opening_instruments(self):
        config, record, _, _, _ = self.fixture(reject=True)
        config_path = self.root / "trial.toml"
        config_path.write_text("# injected simulation\n")
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(photoresponse_cli, "load_photoresponse_config", return_value=config), \
                patch.object(photoresponse_cli, "_run", return_value=copy.deepcopy(record)), \
                redirect_stdout(output), redirect_stderr(errors):
            status = combination_cli.main(["simulate", "--config", str(config_path)])
        self.assertEqual(status, 1)
        self.assertIn("Primary error: NktError: Formal power drift", errors.getvalue())
        self.assertIn("Cleanup overall: clean=True", output.getvalue())
        self.assertIn("Selected modules:", output.getvalue())
        self.assertIn("Config SHA256:", output.getvalue())

    def test_json_cli_returns_one_structured_record_without_human_progress(self):
        config, record, _, _, _ = self.fixture()
        config_path = self.root / "trial.toml"
        config_path.write_text("# injected simulation\n")
        output = io.StringIO()
        with patch.object(photoresponse_cli, "load_photoresponse_config", return_value=config), \
                patch.object(photoresponse_cli, "_run", return_value=copy.deepcopy(record)), redirect_stdout(output):
            status = combination_cli.main(["simulate", "--config", str(config_path), "--json"])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue())["outcome"], "completed")

    def test_photocurrent_and_voltage_bias_keep_signed_quantities_and_units(self):
        _, record, path, _, _ = self.fixture(current=True)
        self.assertTrue(record["completed"], record["error"])
        rows = load_photo_plot_rows(path)
        for row in rows:
            self.assertNotIn("measured.delta_voltage_v", row)
            self.assertEqual(row["requested.bias_voltage_v"], 0.)
            if row["optical_state"] == "light":
                self.assertIsNotNone(row["measured.delta_current_a"])
                self.assertEqual(row["actual.bias_voltage_v"], 0.)

    def test_smu_environment_coordinates_and_leakage_are_projected_from_archived_values(self):
        from attodry_control.unified_plotting import column_label
        from test_photoresponse_smu import configuration, setup as smu_setup
        from test_photoresponse_integration import environment_config
        config = shared(configuration(short=False), samples_per_window=1, environment=environment_config())
        runner, _, _, _, _ = smu_setup(config)
        record = runner.run()
        self.assertTrue(record["completed"], record["error"])
        path = self.root / "smu.json"
        path.write_text(json.dumps(record), encoding="utf-8")
        row = next(r for r in load_photo_plot_rows(path) if r["optical_state"] == "light")
        self.assertIsNotNone(row["actual.temperature_k"])
        self.assertIsNotNone(row["requested.gate_bottom_v"])
        self.assertIsNotNone(row["actual.gate_bottom_v"])
        self.assertIsNotNone(row["measured.gate_bottom_leakage_current_a"])
        self.assertIn("(A)", column_label("measured.gate_bottom_leakage_current_a"))
        self.assertNotIn("measured.gate_bottom_source_setpoint", row)

    def test_notebooks_have_valid_python_and_unified_notebook_has_no_hardware_calls(self):
        import ast
        for name in ("unified_plotting.ipynb", "Photoresponse_Analysis.ipynb"):
            notebook = json.loads((ROOT / "notebooks" / name).read_text(encoding="utf-8"))
            for cell in notebook["cells"]:
                if cell["cell_type"] == "code":
                    ast.parse("".join(cell["source"]))
        source = (ROOT / "notebooks/unified_plotting.ipynb").read_text(encoding="utf-8")
        self.assertNotIn("photoresponse_cli", source)
        self.assertNotIn("pyvisa", source)

    def test_photoresponse_export_preserves_baseline_provenance_and_raw_file(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        self.addCleanup(lambda: plt.close("all"))
        _, _, path, _, _ = self.fixture()
        before = path.read_bytes()
        rows = load_plot_sources([path])
        spec = {"x":"actual.optical_power_w", "y":"measured.delta_voltage_v",
                "filters":{"requested.optical_state":["light"]}}
        figure, data = render_plot(rows, spec)
        directory = export_plot_bundle(self.root / "exports", [{"figure":figure, "data":data, "spec":spec}])
        self.assertTrue((directory / "plot_01.svg").is_file())
        self.assertIn("dark_condition_id", (directory / "selected_samples.csv").read_text(encoding="utf-8-sig"))
        self.assertEqual(path.read_bytes(), before)

    def test_phase_report_failure_preserves_original_detector_error_and_cleanup(self):
        config = shared(config_for(), mode="open_circuit_voltage", bias_points=(None,))
        runner, _, electrical, _, _ = setup(config=config)
        def event(value):
            if value["phase"] == "phase_end" and value["operation_phase"] == "electrical_acquisition":
                raise OSError("phase audit failure")
        runner.on_event = event
        electrical.read = lambda **kwargs: (_ for _ in ()).throw(OSError("original detector failure"))
        record = runner.run()
        self.assertEqual(record["error"], "OSError: original detector failure")
        self.assertTrue(record["cleanup"]["laser_off_confirmed"])
        self.assertTrue(record["cleanup"]["electrical_safe_confirmed"])
        self.assertTrue(any("phase audit failure" in e for e in record["cleanup"]["errors"]))

    def test_active_sqlite_audit_loads_collected_rows_without_promoting_them(self):
        config, record, _, store, _ = self.fixture()
        run_id, _ = store.start(config)
        store.event({"phase":"condition_complete", "condition":record["conditions"][0]})
        self.assertEqual(load_photo_plot_rows(store.path, run_id=run_id), ())
        rows = load_photo_plot_rows(store.path, run_id=run_id, audit=True)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["accepted"])
        self.assertTrue(rows[0]["simulated"])


if __name__ == "__main__":
    unittest.main()
