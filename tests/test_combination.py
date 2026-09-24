import ast
from contextlib import closing, redirect_stdout
import io
import itertools
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from attodry_control.combination_analysis import (
    load_combination_rows, load_legacy_three_smu, load_legacy_temperature_lockin,
    select_series, plot_series, export_selection,
)
from attodry_control.combination_cli import load_plan, run as cli
from attodry_control.combination_scan import (
    AxisPoint, ScanAxis, CombinationPlan, SimulatedCombinationStation, run_simulated_combination,
)
from attodry_control.combination_store import CombinationStore, run_snapshot, open_readonly


ROOT = Path(__file__).resolve().parents[1]


def axes():
    return {
        "smu": ScanAxis("smu", tuple(AxisPoint({"smu_bias_v": v})
                                   for v in (-0.1, 0.0, 0.1))),
        "lockin": ScanAxis("lockin", tuple(AxisPoint({
            "lockin_excitation_v_rms": v, "lockin_frequency_hz": 17.0})
            for v in (0.004, 0.008))),
        "temperature": ScanAxis("temperature", tuple(AxisPoint({"temperature_k": t})
                                                    for t in (2.0, 2.1))),
        "magnetic": ScanAxis("magnetic", tuple(AxisPoint({"field_x_t": b, "field_z_t": 0.0})
                                              for b in (0.0, 0.1))),
    }


class CombinationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.path = self.directory / "scan.sqlite"
        self.plan = CombinationPlan((axes()["smu"], axes()["lockin"]))

    def execute(self, plan=None, station=None, *, run_id="test", resume=False):
        with CombinationStore(self.path) as store:
            return run_simulated_combination(plan or self.plan, store, run_id,
                                             station=station, resume=resume)

    def test_all_64_subset_orders_and_exact_cartesian_counts(self):
        choices = axes()
        count = 0
        for n in range(1, 5):
            for order in itertools.permutations(choices, n):
                count += 1
                plan = CombinationPlan(tuple(choices[k] for k in order))
                station = SimulatedCombinationStation()
                result = self.execute(plan, station, run_id=f"order-{count}")
                self.assertEqual(result["status"], "completed")
                rows = load_combination_rows(self.path, run_id=result["run_id"])
                expected = (3 if "smu" in order else 1) * (2 if "lockin" in order else 1)
                expected *= (2 if "temperature" in order else 1) * (2 if "magnetic" in order else 1)
                self.assertEqual(len(rows), expected)
                self.assertEqual(rows[0]["loop_order"], list(order))
                self.assertEqual(sum(op[0] == "open" for op in station.operations), 1)
                self.assertEqual({op[1] for op in station.operations if op[0] == "read"},
                                 set(order))
        self.assertEqual(count, 64)

    def test_smu_outer_reads_again_at_each_excitation_and_regroups(self):
        station = SimulatedCombinationStation()
        self.assertEqual(self.execute(station=station)["status"], "completed")
        self.assertEqual(sum(op[:2] == ("read", "smu") for op in station.operations), 6)
        self.assertEqual(sum(op[:2] == ("apply", "smu") for op in station.operations), 3)
        rows = load_combination_rows(self.path)
        series = select_series(rows, x="measured.smu_bias_voltage_v",
                               y="measured.smu_bias_current_a",
                               group_by=("requested.lockin_excitation_v_rms",))
        self.assertEqual([len(s.rows) for s in series], [3, 3])
        for item in series:
            excitation = dict(item.group)["requested.lockin_excitation_v_rms"]
            self.assertEqual([r["measured.smu_bias_voltage_v"] for r in item.rows], [-0.1, 0, 0.1])
            for row in item.rows:
                self.assertAlmostEqual(row["measured.smu_bias_current_a"],
                                       row["measured.smu_bias_voltage_v"] / (1000 * (1 + excitation)))
        self.assertNotEqual(rows[0]["measured.smu_bias_current_a"],
                            rows[1]["measured.smu_bias_current_a"])

    def test_reversed_loops_give_same_selectable_coordinates(self):
        self.execute(run_id="forward")
        self.execute(CombinationPlan(tuple(reversed(self.plan.axes))), run_id="reversed")
        def coordinates(run):
            return sorted((r["requested.smu_bias_v"], r["requested.lockin_excitation_v_rms"],
                           r["measured.smu_bias_current_a"])
                          for r in load_combination_rows(self.path, run_id=run))
        self.assertEqual(coordinates("forward"), coordinates("reversed"))

    def test_ordered_field_duplicates_repeats_segments_and_directions_survive(self):
        field = ScanAxis("magnetic", (
            AxisPoint({"field_x_t": 0, "field_z_t": 0}, "out", "ascending"),
            AxisPoint({"field_x_t": 0.1, "field_z_t": 0}, "out", "ascending"),
            AxisPoint({"field_x_t": 0.1, "field_z_t": 0}, "back", "descending"),
            AxisPoint({"field_x_t": 0, "field_z_t": 0}, "back", "descending"),
        ))
        plan = CombinationPlan((field, axes()["smu"]), repeats=2)
        station = SimulatedCombinationStation()
        self.execute(plan, station)
        rows = load_combination_rows(self.path)
        self.assertEqual(len(rows), 24)
        self.assertEqual(len({r["condition_id"] for r in rows}), 24)
        field_sets = [op for op in station.operations if op[:2] == ("apply", "magnetic")]
        self.assertEqual([op[2]["field_x_t"] for op in field_sets], [0, .1, .1, 0] * 2)
        grouped = select_series(rows, x="measured.smu_bias_voltage_v",
                                y="measured.smu_bias_current_a")
        self.assertEqual(len(grouped), 8)

    def test_requalifies_temperature_after_field_change_before_formal_read(self):
        station = SimulatedCombinationStation()
        plan = CombinationPlan((axes()["temperature"], axes()["magnetic"], axes()["smu"]))
        self.execute(plan, station)
        operations = station.operations
        field = next(i for i, op in enumerate(operations) if op[:2] == ("apply", "magnetic"))
        qualify = next(i for i, op in enumerate(operations) if op[0] == "qualify")
        read = next(i for i, op in enumerate(operations) if op[0] == "read")
        self.assertLess(field, qualify)
        self.assertLess(qualify, read)

    def test_partial_read_failure_retained_cleanup_all_modules_then_close(self):
        class Fault(SimulatedCombinationStation):
            def read(self, module):
                if module == "lockin":
                    raise OSError("lost connection; not zero")
                return super().read(module)
        station = Fault()
        plan = CombinationPlan(tuple(axes().values()))
        result = self.execute(plan, station)
        self.assertEqual(result["status"], "failed")
        self.assertIn("lost connection", result["error"])
        self.assertTrue(result["cleanup"]["manual_verification_required"])
        self.assertFalse(result["cleanup"]["clean"])
        self.assertEqual(load_combination_rows(self.path), ())
        with closing(open_readonly(self.path)) as db:
            events = [json.loads(r[0]) for r in db.execute(
                "SELECT payload_json FROM combination_events WHERE event_type='raw_reading'")]
        self.assertEqual(events[0]["reading"]["module"], "smu")
        self.assertEqual([op[1] for op in station.operations if op[0] == "cleanup"],
                         ["lockin", "smu", "magnetic", "temperature"])
        self.assertTrue(station.closed)

    def test_interrupt_resume_rejects_old_attempt_and_rereads_pending_leaf(self):
        class Interrupt(SimulatedCombinationStation):
            def apply(self, module, point):
                if module == "lockin" and point.values["lockin_excitation_v_rms"] == .008:
                    raise KeyboardInterrupt()
                return super().apply(module, point)
        result = self.execute(station=Interrupt())
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(load_combination_rows(self.path), ())
        self.assertEqual(len(load_combination_rows(self.path, audit=True)), 1)
        self.assertEqual(self.execute(resume=True)["status"], "completed")
        rows = load_combination_rows(self.path)
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[1]["attempt_index"], 1)
        with closing(open_readonly(self.path)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM combination_attempts "
                                        "WHERE status='rejected'").fetchone()[0], 1)

    def test_magnetic_resume_is_rejected_before_open(self):
        class Fault(SimulatedCombinationStation):
            def qualify(self, modules):
                raise RuntimeError("field failed")
        plan = CombinationPlan((axes()["magnetic"],))
        self.execute(plan, Fault())
        station = SimulatedCombinationStation()
        with self.assertRaisesRegex(ValueError, "Magnetic history"):
            self.execute(plan, station, resume=True)
        self.assertEqual(station.operations, [])

    def test_cleanup_error_does_not_skip_other_modules_or_mark_completed(self):
        class Fault(SimulatedCombinationStation):
            def cleanup(self, module, failed):
                if module == "lockin":
                    raise OSError("unknown source")
                return super().cleanup(module, failed)
        station = Fault()
        result = self.execute(station=station)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["cleanup"]["manual_verification_required"])
        self.assertTrue(station.closed)
        self.assertIn(("cleanup", "smu", True), station.operations)
        self.assertEqual(load_combination_rows(self.path), ())
        self.assertEqual(len(load_combination_rows(self.path, audit=True)), 6)
        with self.assertRaisesRegex(ValueError, "Unverified cleanup"):
            self.execute(resume=True)

    def test_primary_read_error_survives_environment_and_audit_errors(self):
        class Fault(SimulatedCombinationStation):
            def read(self, module):
                raise OSError("primary instrument failure")

            def end_sample(self, reads):
                raise RuntimeError("secondary environmental failure")
        original = CombinationStore.event
        def event(store, run_id, kind, payload):
            if kind == "environment_after_read_failed":
                raise OSError("secondary audit failure")
            return original(store, run_id, kind, payload)
        station = Fault()
        with patch.object(CombinationStore, "event", event):
            result = self.execute(station=station)
        self.assertEqual(result["error"], "OSError: primary instrument failure")
        self.assertTrue(station.closed)
        self.assertTrue(result["cleanup"]["manual_verification_required"])

    def test_nonfinite_raw_value_is_retained_as_explicit_invalid_numeric(self):
        class Fault(SimulatedCombinationStation):
            def read(self, module):
                value = super().read(module)
                value["actual"][next(iter(value["actual"]))] = float("nan")
                return value
        self.assertEqual(self.execute(station=Fault())["status"], "failed")
        with closing(open_readonly(self.path)) as db:
            raw = db.execute("SELECT payload_json FROM combination_events "
                             "WHERE event_type='raw_reading' LIMIT 1").fetchone()[0]
        self.assertIn('"invalid_numeric": "nan"', raw)
        self.assertEqual(load_combination_rows(self.path), ())

    def test_stale_or_missing_channel_read_is_never_promoted(self):
        for stale in (True, False):
            class Fault(SimulatedCombinationStation):
                def read(self, module):
                    value = super().read(module)
                    if stale:
                        value["captured_at_utc"] = "2000-01-01T00:00:00+00:00"
                    else:
                        value["measurements"] = {}
                    return value
            result = self.execute(station=Fault(), run_id=str(stale))
            self.assertEqual(result["status"], "failed")
        self.assertEqual(load_combination_rows(self.path), ())

    def test_monitor_is_readonly_and_shows_run_condition_attempt_cleanup(self):
        self.execute()
        snapshot = run_snapshot(self.path, "test")
        self.assertEqual(snapshot["accepted_conditions"], 6)
        self.assertEqual(snapshot["current_attempt"]["status"], "accepted")
        self.assertTrue(snapshot["cleanup"]["clean"])
        self.assertEqual(snapshot["process_liveness"], "unknown")
        self.assertEqual(set(snapshot["last_recorded_readings"]), {"smu", "lockin"})
        with closing(open_readonly(self.path)) as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM combination_runs")
        missing = self.directory / "missing.sqlite"
        with self.assertRaises(sqlite3.OperationalError):
            run_snapshot(missing, "missing")
        self.assertFalse(missing.exists())

    def test_resume_plan_mismatch_and_duplicate_run_do_not_open_station(self):
        self.execute()
        for resume in (False, True):
            station = SimulatedCombinationStation()
            with self.assertRaises(ValueError):
                self.execute(CombinationPlan((axes()["smu"],)), station, resume=resume)
            self.assertEqual(station.operations, [])

    def test_invalid_plans_rejected_before_database_run_and_open(self):
        invalid = [
            CombinationPlan(()), CombinationPlan((axes()["smu"], axes()["smu"])),
            CombinationPlan((ScanAxis("magnetic", (AxisPoint({"field_x_t": 0, "field_z_t": 3.01}),)),)),
            CombinationPlan((ScanAxis("magnetic", (AxisPoint({"field_x_t": 0}),)),)),
            CombinationPlan((ScanAxis("smu", (AxisPoint({"smu_bias_v": float("nan")}),)),)),
            CombinationPlan((ScanAxis("smu", (AxisPoint({"smu_bias_v": True}),)),)),
            CombinationPlan((ScanAxis("smu", (AxisPoint({"smu_bias_v": 0, "smu_bias_a": 0}),)),)),
            CombinationPlan((axes()["smu"],), samples_per_condition=True),
            CombinationPlan((axes()["smu"],), repeats=1001),
        ]
        for plan in invalid:
            with self.subTest(plan=plan):
                station = SimulatedCombinationStation()
                with self.assertRaises(ValueError):
                    self.execute(plan, station)
                self.assertEqual(station.operations, [])

    def test_one_accepted_attempt_and_no_incomplete_promotion(self):
        with CombinationStore(self.path) as store:
            condition = self.plan.conditions()[0]
            cid = condition["condition_id"]
            store.begin_run("run", self.plan.snapshot(), [condition])
            attempt = store.begin_attempt("run", cid)
            with self.assertRaisesRegex(ValueError, "incomplete"):
                store.finish_attempt("run", cid, attempt, expected_samples=1)
            with self.assertRaisesRegex(ValueError, "pending"):
                store.finish_run("run", "completed", {"clean": True}, None)
            store.sample("run", cid, attempt, 0, {"clean": True})
            store.finish_attempt("run", cid, attempt, expected_samples=1)
            again = store.begin_attempt("run", cid)
            store.sample("run", cid, again, 0, {"clean": True})
            with self.assertRaises(sqlite3.IntegrityError):
                store.finish_attempt("run", cid, again, expected_samples=1)

    def test_unknown_filters_missing_values_and_no_averaging(self):
        self.execute()
        rows = load_combination_rows(self.path)
        with self.assertRaisesRegex(ValueError, "Unknown columns"):
            select_series(rows, x="mistyped", y="measured.smu_bias_current_a")
        series = select_series(rows, x="measured.smu_bias_voltage_v",
                               y="measured.smu_bias_current_a",
                               filters={"requested.lockin_excitation_v_rms": .004})
        self.assertEqual(len(series), 1)
        self.assertEqual(len(series[0].rows), 3)
        missing = [dict(r) for r in rows]
        missing[0]["measured.smu_bias_current_a"] = None
        series = select_series(missing, x="measured.smu_bias_voltage_v",
                               y="measured.smu_bias_current_a")
        self.assertEqual(sum(len(s.rows) for s in series), 5)

    def test_cli_plan_and_simulation_and_readonly_monitor(self):
        config = ROOT / "config/combination.simulation.toml"
        self.assertEqual(len(load_plan(config).conditions()), 6)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli(["describe", "--config", str(config)]), 0)
            self.assertEqual(cli(["simulate", "--config", str(config),
                                  "--database", str(self.path), "--run-id", "cli"]), 0)
            self.assertEqual(cli(["monitor", "--database", str(self.path),
                                  "--run-id", "cli", "--once"]), 0)
        text = config.read_text(encoding="utf-8").replace('backend = "simulation"', 'backend = "hardware"')
        bad = self.directory / "bad.toml"
        bad.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "simulation"):
            load_plan(bad)

    def test_plot_and_selection_export_preserve_every_sample(self):
        self.execute()
        rows = load_combination_rows(self.path)
        x, y = "measured.smu_bias_voltage_v", "measured.smu_bias_current_a"
        group = ("requested.lockin_excitation_v_rms",)
        series = select_series(rows, x=x, y=y, group_by=group)
        target = export_selection(self.directory / "selection", series, x=x, y=y, group_by=group)
        manifest = json.loads((target / "selection_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["selected_count"], 6)
        self.assertEqual(manifest["aggregation"], "none")
        with self.assertRaises(FileExistsError):
            export_selection(target, series, x=x, y=y)
        try:
            import matplotlib.pyplot as plt
        except ImportError:
            self.skipTest("optional Matplotlib unavailable")
        figure, axis = plot_series(series, x=x, y=y)
        self.assertEqual(sum(len(c.get_offsets()) for c in axis.collections), 6)
        figure.savefig(target / "plot.png")
        plt.close(figure)

    def test_legacy_temperature_adapter_does_not_invent_smu_or_timestamps(self):
        from tests.test_temperature_excitation_analysis import TemperatureExcitationAnalysisTests
        helper = TemperatureExcitationAnalysisTests()
        path = self.directory / "legacy.json"
        path.write_text(json.dumps(helper._summary()), encoding="utf-8")
        rows = load_legacy_temperature_lockin(path)
        self.assertEqual(len(rows), 32)
        self.assertTrue(all("actual.temperature_k" in row for row in rows))
        self.assertFalse(any("smu_bias" in key or key.startswith("timestamps.") for row in rows for key in row))

    def test_legacy_smu_uses_archived_mode_and_keeps_absent_roles_absent(self):
        from tests.test_three_smu_analysis import make_run
        directory = make_run(self.directory, active_roles=("smu_bias",))
        rows = load_legacy_three_smu(directory)
        self.assertEqual(len(rows), 4)
        self.assertNotIn("requested.smu_bias_v", rows[0])
        self.assertIn("legacy.smu_bias.coordinate", rows[0])
        metadata_path = directory / "metadata.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["hardware"] = {"smu_bias": {"source_mode": "current"}}
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        rows = load_legacy_three_smu(directory)
        self.assertIn("requested.smu_bias_a", rows[0])
        self.assertNotIn("requested.smu_bias_v", rows[0])
        self.assertFalse(any("gate_top" in key or "lockin" in key for row in rows for key in row))
        metadata["status"] = "rejected"
        metadata["accepted"] = False
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
        self.assertEqual(load_legacy_three_smu(directory), ())
        self.assertEqual(len(load_legacy_three_smu(directory, audit=True)), 4)

    def test_audit_write_failure_still_attempts_every_cleanup(self):
        station = SimulatedCombinationStation()
        with CombinationStore(self.path) as store:
            original = store.event
            def failed_event(run_id, kind, payload):
                if kind in {"raw_reading", "cleanup_started"}:
                    raise OSError("disk full")
                return original(run_id, kind, payload)
            store.event = failed_event
            result = run_simulated_combination(self.plan, store, "disk", station=station)
        self.assertEqual(result["status"], "failed")
        self.assertIn("disk full", result["error"])
        self.assertEqual([op[1] for op in station.operations if op[0] == "cleanup"], ["lockin", "smu"])
        self.assertTrue(station.closed)

    def test_unfinished_active_run_cannot_be_assumed_dead_or_resumed(self):
        with CombinationStore(self.path) as store:
            store.begin_run("test", self.plan.snapshot(), self.plan.conditions())
        self.assertEqual(run_snapshot(self.path, "test")["process_liveness"], "unknown")
        station = SimulatedCombinationStation()
        with self.assertRaisesRegex(ValueError, "terminal"):
            self.execute(station=station, resume=True)
        self.assertEqual(station.operations, [])

    def test_multiple_samples_are_distinct_and_rejected_sample_is_audit_only(self):
        plan = CombinationPlan((axes()["smu"],), samples_per_condition=2)
        self.execute(plan)
        rows = load_combination_rows(self.path)
        self.assertEqual(len(rows), 6)
        self.assertEqual({r["sample_index"] for r in rows}, {0, 1})
        self.assertEqual(len({(r["condition_id"], r["sample_index"]) for r in rows}), 6)

    def test_schema_is_additive_to_the_existing_simulation_store(self):
        from attodry_control.storage import RunStore
        old = RunStore(self.path)
        old.close()
        self.execute()
        with closing(open_readonly(self.path)) as db:
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({"runs", "conditions", "attempts", "combination_runs",
                         "combination_conditions", "combination_attempts"} <= names)

    def test_analysis_and_monitor_modules_have_no_hardware_imports(self):
        forbidden = {"attodry", "sr830", "keithley2400", "three_smu", "pyvisa", "qcodes"}
        for filename in ("combination_analysis.py", "combination_store.py", "combination_cli.py",
                         "combination_scan.py"):
            tree = ast.parse((ROOT / "src/attodry_control" / filename).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn((node.module or "").split(".")[-1], forbidden)
                if isinstance(node, ast.Import):
                    self.assertFalse({a.name.split(".")[0] for a in node.names} & forbidden)

    def test_notebook_refresh_load_exclusions_and_missing_channels(self):
        try:
            import ipywidgets
        except ImportError:
            self.skipTest("optional ipywidgets unavailable")
        self.execute()
        notebook = json.loads((ROOT / "notebooks/combination_analysis.ipynb").read_text(encoding="utf-8"))
        sources = ["".join(cell["source"]) for cell in notebook["cells"] if cell["cell_type"] == "code"]
        namespace = {}
        for source in sources:
            compile(source, "combination_analysis.ipynb", "exec")
        with redirect_stdout(io.StringIO()), patch("IPython.display.display"):
            exec(sources[0], namespace)
            namespace["DATA_DIRECTORY"] = self.directory
            exec(sources[1], namespace)
            self.assertEqual(len(namespace["files"].options), 1)
            namespace["files"].value = (namespace["files"].options[0][1],)
            namespace["load_selected"]()
            self.assertEqual(len(namespace["excluded"].options), 6)
            self.assertEqual(len(namespace["loaded_rows"]), 6)
            namespace["excluded"].value = (namespace["excluded"].options[0][1],)
            exec(sources[2], namespace)
            self.assertEqual(sum(len(s.rows) for s in namespace["selection"]), 5)
            # A single-module dataset lacking Lock-in/SMU channels should explain,
            # not raise or connect hardware.
            namespace["loaded_rows"] = [{"accepted": True, "actual.temperature_k": 2}]
            exec(sources[2], namespace)
            self.assertEqual(namespace["selection"], ())
            exec(sources[3], namespace)  # Export remains explicitly disabled.
