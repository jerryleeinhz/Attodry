"""Range-rule recovery and analysis use archived coordinates and settings only."""
from copy import deepcopy
import csv
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from attodry_control.combination_analysis import (
    export_selection, load_combination_rows, select_series,
)
from attodry_control.combination_resume import build_recovery, configuration_changes
from attodry_control.combination_scan import AxisPoint, CombinationPlan, ScanAxis
from attodry_control.combination_store import CombinationStore
from tests.test_combination_resume_store import recorded_sample


X = "actual.temperature_k"
Y = "measured.lockin_xx_h1_amplitude_v"


def range_fixture():
    plan = CombinationPlan((
        ScanAxis("temperature", (AxisPoint({"temperature_k": 10.}), AxisPoint({"temperature_k": 20.}))),
        ScanAxis("lockin", (AxisPoint({"lockin_excitation_v_rms": .1, "lockin_frequency_hz": 200.}),)),
    ))
    rules, bindings = [], []
    for index, condition in enumerate(plan.conditions()):
        scales = {"lockin_xx": 1. / (index + 1), "lockin_xy": .1 * (index + 1)}
        name = f"temperature-{index}"
        rules.append({"name": name, "match": {"temperature_k": [condition["requested"]["temperature_k"]]},
                      "lockin_xx_full_scale_v": scales["lockin_xx"],
                      "lockin_xy_full_scale_v": scales["lockin_xy"]})
        bindings.append({"condition_id": condition["condition_id"], "repeat_index": 0,
                         "outer_indices": {"temperature": index}, "requested": condition["requested"],
                         "rule_name": name, "full_scales_v": scales})
    snapshot = {**plan.snapshot(), "mode": "hardware", "config_sha256": "test-configuration",
                "source_sha256": {"fake_station.py": "same-source"},
                "hardware": {"lockin": {"harmonics_by_role": {"xx": [1, 2], "xy": [1, 2]}}},
                "lockin_range_rules": {"contract": "outer-condition-lockin-range-v1",
                    "outer_modules": ["temperature"], "rules": rules, "preview": [],
                    "condition_bindings": bindings}}
    return plan, snapshot


def ranged_sample(condition, binding):
    sample = recorded_sample(condition)
    reading = next(item for item in sample["reads"] if item["module"] == "lockin")
    scales = deepcopy(binding["full_scales_v"])
    reading["status"]["lockin_range_rule"] = {
        "rule_name": binding["rule_name"], "requested_full_scales_v": scales,
        "confirmed_full_scales_v": deepcopy(scales), "verified": True,
        "changed_roles": list(scales), "transitions": [],
    }
    for pair in reading["status"]["samples"]:
        for stage in ("settings_before", "settings_after"):
            for role, scale in scales.items():
                pair[stage]["roles"][role]["sensitivity_full_scale_v"] = scale
    return sample


class RangeRecoveryTests(unittest.TestCase):
    def test_exact_rules_and_compiled_binding_are_required(self):
        _, snapshot = range_fixture()
        self.assertEqual(configuration_changes(snapshot, deepcopy(snapshot)), [])
        for mutate in (
            lambda s: s.pop("lockin_range_rules"),
            lambda s: s["lockin_range_rules"]["rules"][0].update(name="renamed"),
            lambda s: s["lockin_range_rules"]["condition_bindings"][0]["full_scales_v"].update(lockin_xx=.5),
            lambda s: s["lockin_range_rules"]["condition_bindings"][0].update(condition_id="condition-000099"),
            lambda s: s["lockin_range_rules"].update(contract="unreviewed-v2"),
        ):
            changed = deepcopy(snapshot)
            mutate(changed)
            with self.subTest(change=changed), self.assertRaisesRegex(ValueError, "range rules/resolved schedule changed"):
                configuration_changes(snapshot, changed)
        legacy = deepcopy(snapshot)
        legacy.pop("lockin_range_rules")
        with self.assertRaisesRegex(ValueError, "start a new run"):
            configuration_changes(legacy, snapshot)
        self.assertEqual(configuration_changes(legacy, {**legacy, "lockin_range_rules": None}), [])

    def test_file_only_resume_rechecks_archived_schedule_without_writing(self):
        plan, snapshot = range_fixture()
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "scan.sqlite"
            with CombinationStore(database) as store:
                store.begin_run("test", snapshot, plan.conditions())
                store.finish_run("test", "failed", {"clean": False, "communication_uncertain": False,
                    "actions": [{"module": module, "result": {"verified": True, "errors": []}}
                                for module in ("temperature", "lockin")]}, "known failure")
            before = database.read_bytes()
            config = SimpleNamespace(plan=plan, snapshot=deepcopy(snapshot))
            review = build_recovery(config, database, "test", acknowledge_failure=True)
            self.assertTrue(review["review"]["ready"])
            self.assertEqual(review["next_condition"]["condition_id"], "condition-000000")
            config.snapshot["lockin_range_rules"]["rules"][0]["match"]["temperature_k"] = [20.]
            with self.assertRaisesRegex(ValueError, "start a new run"):
                build_recovery(config, database, "test", acknowledge_failure=True)
            self.assertEqual(database.read_bytes(), before)


class RangeAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.database = self.directory / "scan.sqlite"
        self.plan, self.snapshot = range_fixture()

    def archive(self, *, rules=True, change=None):
        snapshot = deepcopy(self.snapshot)
        if not rules:
            snapshot.pop("lockin_range_rules")
        with CombinationStore(self.database) as store:
            store.begin_run("test", snapshot, self.plan.conditions())
            for condition, binding in zip(self.plan.conditions(), self.snapshot["lockin_range_rules"]["condition_bindings"]):
                sample = ranged_sample(condition, binding)
                if not rules:
                    sample["reads"][-1]["status"].pop("lockin_range_rule")
                if change is not None:
                    change(sample)
                attempt = store.begin_attempt("test", condition["condition_id"])
                store.sample("test", condition["condition_id"], attempt, 0, sample)
                store.finish_attempt("test", condition["condition_id"], attempt, expected_samples=1)
            store.finish_run("test", "completed", {"clean": True, "errors": []}, None)

    def test_role_ranges_loaded_and_exported_without_current_config(self):
        self.archive()
        rows = load_combination_rows(self.database)
        self.assertEqual([row["lockin_range.rule_name"] for row in rows], ["temperature-0", "temperature-1"])
        self.assertEqual([row["lockin_range.confirmed_full_scales_v.lockin_xx"] for row in rows], [1., .5])
        self.assertEqual([row["lockin_range.confirmed_full_scales_v.lockin_xy"] for row in rows], [.1, .2])
        series = select_series(rows, x=X, y=Y)
        self.assertEqual(len(series), 2)
        self.assertTrue(all("lockin_range_configuration" in dict(item.group) for item in series))
        target = export_selection(self.directory / "selection", series, x=X, y=Y)
        manifest = json.loads((target / "selection_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["execution_segments"][0]["lockin_range_configurations"]), 2)
        with (target / "selected_samples.csv").open(encoding="utf-8", newline="") as stream:
            exported = list(csv.DictReader(stream))
        self.assertEqual(exported[1]["lockin_range.confirmed_full_scales_v.lockin_xx"], "0.5")

    def test_missing_readback_is_unknown_not_inferred_from_request(self):
        self.archive(change=lambda sample: sample["reads"][-1]["status"].pop("lockin_range_rule"))
        rows = load_combination_rows(self.database, audit=True)
        self.assertIsNone(rows[0]["lockin_range.verified"])
        self.assertNotIn("lockin_range.confirmed_full_scales_v.lockin_xx", rows[0])
        self.assertEqual(rows[0]["lockin_range.requested_full_scales_v.lockin_xx"], 1.)

    def test_conflicting_recorded_request_does_not_get_archived_rule_label(self):
        def corrupt(sample):
            sample["reads"][-1]["status"]["lockin_range_rule"]["requested_full_scales_v"]["lockin_xx"] = .05
        self.archive(change=corrupt)
        with self.assertRaisesRegex(ValueError, "differs from its archived condition binding"):
            load_combination_rows(self.database)

    def test_old_archives_keep_existing_columns_and_grouping(self):
        self.archive(rules=False)
        rows = load_combination_rows(self.database)
        self.assertTrue(all(not any(key.startswith("lockin_range") for key in row) for row in rows))
        self.assertEqual(len(select_series(rows, x=X, y=Y)), 1)

    def test_missing_or_duplicate_archived_binding_is_not_inferred(self):
        from attodry_control.combination_analysis import _lockin_range_bindings, _lockin_range_columns
        duplicated = deepcopy(self.snapshot)
        duplicated["lockin_range_rules"]["condition_bindings"].append(
            duplicated["lockin_range_rules"]["condition_bindings"][0])
        with self.assertRaisesRegex(ValueError, "unique archived Lock-in range binding"):
            _lockin_range_bindings(duplicated)
        with self.assertRaisesRegex(ValueError, "unique archived Lock-in range binding"):
            _lockin_range_columns(_lockin_range_bindings(self.snapshot), "condition-000999", {})

    def test_curve_statistics_heatmap_and_scatter_separate_effective_ranges(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from attodry_control.analysis_observations import repeat_statistics
        from attodry_control.unified_plotting import _curve_groups, export_plot_bundle, render_plot
        self.addCleanup(plt.close, "all")
        rows = []
        for scale in (.1, .2):
            for x in (0., 1.):
                for y in (0., 1.):
                    rows.append({"source_path": "grid.sqlite", "run_id": "test", "accepted": True,
                        "condition_id": f"{x}-{y}", "attempt_index": 0, "sample_index": 0,
                        "repeat_index": 0, "configuration_revision": "same-config",
                        "lockin_range_configuration": json.dumps({"lockin_xx": scale}, sort_keys=True),
                        "csv.x": x, "csv.y": y, "csv.z": x + y})
        self.assertEqual(len(_curve_groups(rows, None, separate_samples=False)), 2)
        self.assertEqual(len(_curve_groups([{k: v for k, v in rows[0].items()
                                            if k != "lockin_range_configuration"}, rows[0]],
                                          None, separate_samples=False)), 2)
        self.assertEqual(len(repeat_statistics([rows[0], rows[4]], x="csv.x", y="csv.z")), 2)
        figure, curve = render_plot(rows, {"x": "csv.x", "y": "csv.z", "statistics": "mean_sd"})
        self.assertEqual(len(figure.axes[0].get_legend_handles_labels()[1]), 2)
        _, heatmap = render_plot(rows, {"mode": "heatmap", "x": "csv.x", "y": "csv.y", "z": "csv.z"})
        self.assertEqual(heatmap["report"]["panel_count"], 2)
        self.assertIn("lockin_range_configuration", heatmap["report"]["facet_keys"])
        scatter, data = render_plot(rows, {"mode": "xy_z", "x": "csv.x", "y": "csv.y", "z": "csv.z"})
        self.assertEqual(len(scatter.axes[0].collections), 2)
        target = export_plot_bundle(self.directory, [{"figure": scatter, "data": data,
            "spec": {"mode": "xy_z", "x": "csv.x", "y": "csv.y", "z": "csv.z"}}])
        with (target / "selected_samples.csv").open(encoding="utf-8", newline="") as stream:
            exported = list(csv.DictReader(stream))
        self.assertEqual(len({row["lockin_range_configuration"] for row in exported}), 2)


if __name__ == "__main__":
    unittest.main()
