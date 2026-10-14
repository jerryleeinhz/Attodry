"""Offline legacy recovery, execution provenance and actual plot regressions."""
from contextlib import closing
import csv
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from attodry_control.combination_analysis import (
    export_selection, load_combination_rows, select_series,
)
from attodry_control.combination_scan import AxisPoint, CombinationPlan, ScanAxis
from attodry_control.combination_store import (
    CombinationStore, open_readonly, run_snapshot,
)
from attodry_control.combination_terminal import snapshot_text
from tests.test_combination_resume_store import recorded_sample


RUN = "20261014T124726025034Z_T-e-sweep_17-250K_0.05-3V_200Hz"
X = "actual.lockin_excitation_v_rms"
Y = "measured.lockin_xx_h1_amplitude_v"
OLD_CONFIG = "a" * 64
NEW_CONFIG = "b" * 64
FAULT = "AttoDryError: Temperature drifted from qualified baseline"


class ResumeAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.database = self.directory / "scan.sqlite"
        temperatures = (11.7, 44.46666666666667, 77.23333333333333, 110.,
                        *tuple(115. + 2.5 * i for i in range(11)), 150., 200., 250.)
        voltages = (*tuple(.1 + .07 * i for i in range(11)),
                    *tuple(.85 + i / 7 for i in range(8)))
        self.plan = CombinationPlan((
            ScanAxis("temperature", tuple(AxisPoint({"temperature_k": t}) for t in temperatures)),
            ScanAxis("lockin", tuple(AxisPoint({"lockin_excitation_v_rms": v,
                "lockin_frequency_hz": 200.}) for v in voltages)),
        ))
        self.conditions = self.plan.conditions()
        self.old_plan = {**self.plan.snapshot(), "mode": "hardware", "config_sha256": OLD_CONFIG,
            "source_sha256": {"cryostat_points.py": "c" * 64},
            "hardware": {"effective_cryostat": {
                "cryostat": {"temperature_min_k": 1.6, "temperature_max_k": 300.0},
                "magnet": {"limits": {"hardware_x_max_t": 3.0,
                    "hardware_z_max_t": 3.0, "experiment_vector_max_t": 3.0}}},
                "lockin": {"harmonics_by_role": {"xx": [1, 2], "xy": [1, 2]},
                **{role: {"time_constant_s": 1.0, "sensitivity_full_scale_v": 1.0,
                          "reserve_mode": "normal", "sensitivity_mode": "fixed"}
                   for role in ("lockin_xx", "lockin_xy")}}}}
        self.new_plan = {**self.old_plan, "config_sha256": NEW_CONFIG}
        self.failure_cleanup = {"clean": False, "communication_uncertain": False,
            "manual_verification_required": True, "errors": ["temperature: cleanup not certified"],
            "actions": [{"module": module, "failure_requires_manual_review": True,
                         "clean": False, "result": {"attempted": True, "verified": True,
                                                     "errors": []}} for module in ("lockin", "temperature")]}

    def sample(self, condition, *, invalid=False):
        sample = recorded_sample(condition)
        reading = next(item for item in sample["reads"] if item["module"] == "lockin")
        for pair in reading["status"]["samples"]:
            for role in ("lockin_xx", "lockin_xy"):
                for metric in ("x_v", "y_v", "amplitude_v"):
                    pair[role]["reading"][metric] *= .1
            if invalid and pair["harmonic"] == 1:
                pair["lockin_xy"]["lia_status"].update(raw=1, input_or_reserve_overload=True)
                pair["valid_for_analysis_by_role"]["lockin_xy"] = False
                pair["problems"] = pair["continued_overload_problems"] = ["lockin_xy input/reserve overload"]
                pair["problems_by_role"]["lockin_xy"] = pair["problems"]
        for key in reading["measurements"]:
            if key.endswith(("_x_v", "_y_v", "_amplitude_v")):
                reading["measurements"][key] *= .1
        if invalid:
            sample["clean"] = reading["clean"] = False
            reading["problems"] = ["lockin_xy input/reserve overload"]
            reading["overload_continuation"] = True
        sample["measurements"] = {key: value for item in sample["reads"]
                                  for key, value in item["measurements"].items()}
        return sample

    def append(self, store, condition, *, rejected=False, invalid=False):
        cid = condition["condition_id"]
        attempt = store.begin_attempt(RUN, cid)
        store.sample(RUN, cid, attempt, 0, self.sample(condition, invalid=invalid))
        store.finish_attempt(RUN, cid, attempt, expected_samples=1,
                             error=FAULT if rejected else None)

    def legacy_failure(self):
        with CombinationStore(self.database) as store:
            # Deliberately use the original schema-1 run entry with no executions.
            store.begin_run(RUN, self.old_plan, self.conditions)
            for condition in self.conditions[:6]:
                self.append(store, condition)
            self.append(store, self.conditions[6], rejected=True)
            store.finish_run(RUN, "failed", self.failure_cleanup, FAULT)

    def recovery(self, store):
        from attodry_control.combination_resume import build_recovery
        reviewed = build_recovery(SimpleNamespace(plan=self.plan, snapshot=self.new_plan),
                                  self.database, RUN, acknowledge_failure=True)
        return store.begin_hardware_execution(RUN, self.new_plan, self.conditions, recovery=reviewed)

    def complete(self, *, invalid_index=None):
        self.legacy_failure()
        with CombinationStore(self.database) as store:
            self.assertEqual(len(self.recovery(store)), 6)
            for index, condition in enumerate(self.conditions[6:], 6):
                self.append(store, condition, invalid=index == invalid_index)
            store.finish_run(RUN, "completed", {"clean": True, "errors": []}, None)

    def test_old_schema_without_execution_tables_reads_without_migration(self):
        self.legacy_failure()
        with CombinationStore(self.database) as store:
            store.connection.executescript("DROP TABLE combination_attempt_executions; "
                                          "DROP TABLE combination_executions;")
        before = hashlib.sha256(self.database.read_bytes()).hexdigest()
        self.assertEqual(load_combination_rows(self.database), ())
        rows = load_combination_rows(self.database, audit=True)
        self.assertEqual(len(rows), 7)
        self.assertEqual(rows[0]["config_sha256"], OLD_CONFIG)
        self.assertIsNone(rows[0]["execution_id"])
        self.assertEqual(rows[0]["execution_kind"], "legacy_unsegmented")
        self.assertEqual(before, hashlib.sha256(self.database.read_bytes()).hexdigest())

    def test_6_of_342_legacy_data_and_failure_are_preserved_after_recovery(self):
        self.legacy_failure()
        with closing(open_readonly(self.database)) as connection:
            original = [dict(row) for row in connection.execute("SELECT * FROM combination_samples")]
        with CombinationStore(self.database) as store:
            self.assertEqual(self.recovery(store), {c["condition_id"] for c in self.conditions[:6]})
            self.assertEqual(load_combination_rows(self.database), ())
            for condition in self.conditions[6:]:
                self.append(store, condition)
            store.finish_run(RUN, "completed", {"clean": True, "errors": []}, None)
        rows = load_combination_rows(self.database)
        self.assertEqual(len(rows), 342)
        self.assertEqual([row["sequence_index"] for row in rows], list(range(342)))
        self.assertEqual(rows[6]["attempt_index"], 1)
        self.assertEqual({row["execution_kind"] for row in rows[:6]}, {"legacy"})
        self.assertEqual({row["execution_status"] for row in rows[:6]}, {"failed"})
        self.assertTrue(all(row["accepted"] for row in rows[:6]))
        self.assertEqual(rows[0]["execution_error"], FAULT)
        self.assertEqual(rows[0]["execution_cleanup"], self.failure_cleanup)
        self.assertEqual({row["config_sha256"] for row in rows[:6]}, {OLD_CONFIG})
        self.assertEqual({row["config_sha256"] for row in rows[6:]}, {NEW_CONFIG})
        audit = load_combination_rows(self.database, audit=True)
        self.assertEqual(len(audit), 343)
        self.assertEqual(sum(row["attempt_status"] == "rejected" for row in audit), 1)
        with closing(open_readonly(self.database)) as connection:
            now = [dict(row) for row in connection.execute("SELECT * FROM combination_samples")]
            self.assertTrue(all(row in now for row in original))
            plan = json.loads(connection.execute("SELECT plan_json FROM combination_runs").fetchone()[0])
            self.assertEqual(plan, json.loads(json.dumps(self.old_plan)))

    def test_accepted_record_continue_invalid_stays_excluded(self):
        self.complete(invalid_index=7)
        self.assertEqual(run_snapshot(self.database, RUN)["accepted_conditions"], 342)
        rows = load_combination_rows(self.database)
        self.assertEqual(len(rows), 341)
        self.assertNotIn(7, {row["sequence_index"] for row in rows})
        invalid = next(row for row in load_combination_rows(self.database, audit=True)
                       if row["sequence_index"] == 7)
        self.assertEqual(invalid["attempt_status"], "accepted")
        self.assertFalse(invalid["accepted"])
        self.assertFalse(invalid["clean"])

    def test_unmapped_segment_sample_fails_closed(self):
        self.complete()
        with CombinationStore(self.database) as store:
            store.connection.execute("DELETE FROM combination_attempt_executions WHERE "
                                     "condition_id='condition-000006' AND attempt_index=1")
            store.connection.commit()
        with self.assertRaisesRegex(ValueError, "lacks its archived execution"):
            load_combination_rows(self.database)

    def test_incomplete_execution_extension_is_not_mislabelled_legacy(self):
        self.legacy_failure()
        with CombinationStore(self.database) as store:
            store.connection.execute("DROP TABLE combination_attempt_executions")
            store.connection.commit()
        with self.assertRaisesRegex(ValueError, "Incomplete execution provenance"):
            load_combination_rows(self.database, audit=True)

    def test_same_configuration_execution_ids_remain_auditable_without_splitting_curve(self):
        from attodry_control.analysis_observations import observation_id
        from attodry_control.unified_plotting import _curve_groups
        common = {"source_path": "test.sqlite", "run_id": RUN, "accepted": True,
            "condition_id": "condition-000000", "attempt_index": 0, "sample_index": 0,
            "repeat_index": 0, "configuration_revision": OLD_CONFIG,
            "requested.lockin_frequency_hz": 200., X: .1, Y: .01}
        rows = ({**common, "execution_id": "one"},
                {**common, "condition_id": "condition-000001", X: .2,
                 "execution_id": "two"})
        self.assertEqual(len(select_series(rows, x=X, y=Y)), 1)
        self.assertEqual(len(_curve_groups(rows, None)), 1)
        original = {key: value for key, value in rows[0].items() if key not in
                    {"execution_id", "configuration_revision"}}
        self.assertEqual(observation_id(rows[0]), observation_id(original))

    def test_default_series_keeps_changed_configurations_separate(self):
        self.complete()
        rows = tuple(row for row in load_combination_rows(self.database)
                     if row["requested.temperature_k"] == 11.7)
        series = select_series(rows, x=X, y=Y)
        self.assertEqual([len(item.rows) for item in series], [6, 13])
        self.assertEqual({dict(item.group)["configuration_revision"] for item in series},
                         {OLD_CONFIG, NEW_CONFIG})
        exported = export_selection(self.directory / "selection", series, x=X, y=Y)
        manifest = json.loads((exported / "selection_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["execution_segments"]), 2)
        self.assertEqual(manifest["execution_segments"][0]["execution_error"], FAULT)
        with (exported / "selected_samples.csv").open(encoding="utf-8", newline="") as stream:
            values = list(csv.DictReader(stream))
        self.assertEqual(len(values), 19)
        self.assertEqual({row["config_sha256"] for row in values}, {OLD_CONFIG, NEW_CONFIG})

    def test_monitor_exposes_current_execution_and_retains_failed_history(self):
        self.legacy_failure()
        with CombinationStore(self.database) as store:
            self.recovery(store)
            self.append(store, self.conditions[6])
            snapshot = run_snapshot(self.database, RUN)
            text = snapshot_text(snapshot, width=110)
            self.assertIn("Accepted: 7/342", text)
            self.assertIn("Segment 2/2", text)
            self.assertIn("reused 6 accepted conditions", text)
            self.assertIn("current execution accepted 1", text)
            self.assertIn("Retained history:", text)
            self.assertIn(FAULT, " ".join(text.splitlines()))
            self.assertTrue(all(len(line) <= 110 for line in text.splitlines()))

    def test_agg_curves_heatmaps_and_scatter_do_not_pool_changed_configurations(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from attodry_control.unified_plotting import render_plot, export_plot_bundle
        self.addCleanup(plt.close, "all")
        self.complete()
        rows = tuple(row for row in load_combination_rows(self.database)
                     if row["requested.temperature_k"] == 11.7)
        spec = {"x": X, "y": Y, "curve_style": "line+points", "statistics": "mean_sd"}
        figure, data = render_plot(rows, spec)
        groups = {point["group"]["configuration_revision"] for point in data["summary_rows"]}
        self.assertEqual(groups, {OLD_CONFIG, NEW_CONFIG})
        self.assertEqual(len(figure.axes[0].get_legend_handles_labels()[1]), 2)
        destination = export_plot_bundle(self.directory, [{"figure": figure, "data": data, "spec": spec}])
        exported = json.loads((destination / "plot_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(exported["execution_segments"]), 2)
        self.assertTrue(all((destination / name).is_file() for name in
                            exported["plots"][0]["figure_files"]))
        grid = []
        for revision in (OLD_CONFIG, NEW_CONFIG):
            for x in (0., 1.):
                for y in (0., 1.):
                    grid.append({"source_path": "grid.sqlite", "run_id": "grid",
                        "condition_id": f"{revision[0]}-{x}-{y}", "attempt_index": 0,
                        "sample_index": 0, "repeat_index": 0, "accepted": True,
                        "configuration_revision": revision, "csv.x": x, "csv.y": y,
                        "csv.z": x + y})
        _, heatmap = render_plot(grid, {"mode": "heatmap", "x": "csv.x", "y": "csv.y", "z": "csv.z"})
        self.assertEqual(heatmap["report"]["panel_count"], 2)
        self.assertIn("configuration_revision", heatmap["report"]["facet_keys"])
        scatter, _ = render_plot(grid, {"mode": "xy_z", "x": "csv.x", "y": "csv.y", "z": "csv.z"})
        self.assertEqual(len(scatter.axes[0].collections), 2)
        self.assertEqual(len(scatter.axes[0].get_legend_handles_labels()[1]), 2)

    def test_notebook_remains_readonly_and_code_cells_compile(self):
        notebook = json.loads((Path(__file__).resolve().parents[1] /
                               "notebooks/combination_analysis.ipynb").read_text(encoding="utf-8"))
        self.assertIn("configuration_revision", "".join(notebook["cells"][0]["source"]))
        for cell in notebook["cells"]:
            if cell["cell_type"] == "code":
                compile("".join(cell["source"]), "combination_analysis.ipynb", "exec")


if __name__ == "__main__":
    unittest.main()
