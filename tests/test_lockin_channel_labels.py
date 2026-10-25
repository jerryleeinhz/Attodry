"""Archived XX/XY wiring labels: configuration, acquisition and read-only plots."""
from contextlib import redirect_stdout
from copy import deepcopy
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest

from attodry_control.combination_analysis import load_combination_rows, lockin_channel_labels
from attodry_control.combination_hardware import load_hardware_combination, run_hardware_combination
from attodry_control.combination_resume import configuration_changes
from attodry_control.combination_scan import AxisPoint, CombinationPlan, ScanAxis
from attodry_control.combination_store import CombinationStore, encode, utc_now
from attodry_control.unified_plotting import (
    UnifiedPlotDashboard, column_label, numeric_columns, render_plot, export_plot_bundle,
)
from tests import test_combination_hardware as dual
from tests.test_combination_resume_store import recorded_sample
from tests.test_lockin_settling import Clock
from tests import test_mixed_lockin_integration as mixed
from tests import test_sr865a_config as native_config
from tests.test_unified_plotting import observation, X, Y


class ConfigLabels(unittest.TestCase):
    def test_both_supported_models_accept_optional_labels(self):
        fixture = native_config.Sr865aConfigurationTests()
        for model in ("SR830", "SR865A"):
            document = fixture.document(model)
            self.assertEqual(fixture.load(document).lockin_xy.channel_label, "")
            document["lockin_xx"]["channel_label"] = "  contact A-B  "
            document["lockin_xy"]["channel_label"] = "端口3-4 / b轴"
            config = fixture.load(document)
            self.assertEqual(config.lockin_xx.channel_label, "contact A-B")
            self.assertEqual(config.lockin_xy.channel_label, "端口3-4 / b轴")
            self.assertEqual(config.lockin_xy.model, model)

    def test_labels_are_single_line_strings_and_blank_is_legacy(self):
        fixture = native_config.Sr865aConfigurationTests()
        for value in (42, True, ["name"], "first\nsecond", "first\rsecond", "first\tsecond", "bad\x7f"):
            document = fixture.document()
            document["lockin_xy"]["channel_label"] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "channel_label"):
                fixture.load(document)
        document["lockin_xy"]["channel_label"] = "   "
        self.assertEqual(fixture.load(document).lockin_xy.channel_label, "")

    def test_labels_are_reviewed_metadata_and_missing_default_does_not_change_settings(self):
        before = {"hardware": {"lockin": {"lockin_xx": {}, "lockin_xy": {}}}}
        current = deepcopy(before)
        current["hardware"]["lockin"]["lockin_xx"]["channel_label"] = ""
        current["hardware"]["lockin"]["lockin_xy"]["channel_label"] = ""
        self.assertEqual(configuration_changes(before, current), [])
        current["hardware"]["lockin"]["lockin_xy"]["channel_label"] = "new wiring"
        self.assertEqual(configuration_changes(before, current), [{
            "path": "hardware.lockin.lockin_xy.channel_label",
            "before": "", "after": "new wiring", "category": "metadata"}])
        self.assertNotIn("channel_label", before["hardware"]["lockin"]["lockin_xy"])


class AcquisitionLabels(unittest.TestCase):
    def fixture(self, labelled):
        fixture = dual.ElectricalCombinationTests("test_only_requested_modules_open")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        source = fixture.base
        if labelled:
            source = source.replace("[lockin_xx]\n", '[lockin_xx]\nchannel_label = "contact A-B"\n', 1)
            source = source.replace("[lockin_xy]\n", '[lockin_xy]\nchannel_label = "contact C-D"\n', 1)
        fixture.write_config(order=("lockin",), source=source)
        return fixture

    def test_labels_add_no_instrument_commands_and_preserve_measurements(self):
        results = []
        for labelled in (False, True):
            fixture = self.fixture(labelled)
            clock = Clock()
            with CombinationStore(fixture.database) as store:
                result = run_hardware_combination(load_hardware_combination(fixture.path), store, "test",
                    authorize_hardware=True, confirm_xy_sine_disconnected=True,
                    manager_factory=lambda: fixture.manager,
                    monotonic=clock.monotonic, sleep=clock.sleep)
            self.assertEqual(result["status"], "completed", result)
            rows = load_combination_rows(fixture.database, run_id="test")
            results.append((list(fixture.xx.queries), list(fixture.xy.queries),
                            list(fixture.xx.writes), list(fixture.xy.writes),
                            list(clock.sleeps),
                            [{k: v for k, v in row.items() if k.startswith("measured.")} for row in rows]))
            status = rows[0]["status.lockin"]
            if labelled:
                self.assertEqual(status["channel_labels"], {"xx": "contact A-B", "xy": "contact C-D"})
                self.assertEqual(rows[0]["channel_label.xy"], "contact C-D")
                pairs = [payload for kind, payload in fixture.events() if kind == "lockin_formal_pair"]
                self.assertTrue(pairs)
                self.assertTrue(all(p["channel_labels"]["xy"] == "contact C-D" for p in pairs))
            else:
                self.assertNotIn("channel_labels", status)
                self.assertNotIn("channel_label.xy", rows[0])
        self.assertEqual(results[0], results[1])

    def test_mixed_receiver_keeps_native_capabilities_and_archived_label(self):
        helper = mixed.MixedLockinIntegrationTests()
        fixture = helper.fixture()
        self.addCleanup(helper.doCleanups)
        source = fixture.path.read_text(encoding="utf-8").replace(
            "[lockin_xy]\n", '[lockin_xy]\nchannel_label = "native XY port"\n', 1)
        fixture.path.write_text(source, encoding="utf-8")
        result = helper.execute(fixture)
        self.assertEqual(result["status"], "completed", result)
        helper.assert_receiver_owns_no_source_writes(fixture, allow_sync_filter_write=True)
        row = load_combination_rows(fixture.database, run_id="mixed")[0]
        self.assertEqual(row["channel_label.xy"], "native XY port")
        self.assertEqual(row["status.lockin"]["channel_labels"]["xy"], "native XY port")


class ArchiveLabels(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.database = self.directory / "scan.sqlite"

    def archive(self, *, segmented=True, labels=True):
        plan = CombinationPlan((
            ScanAxis("temperature", (AxisPoint({"temperature_k": 10.}), AxisPoint({"temperature_k": 20.}))),
            ScanAxis("lockin", (AxisPoint({"lockin_excitation_v_rms": .1, "lockin_frequency_hz": 200.}),)),
        ))
        original = plan.snapshot()
        if labels:
            original["hardware"] = {"lockin": {"lockin_xy": {"channel_label": "original run"}}}
        with CombinationStore(self.database) as store:
            store.begin_run("test", original, plan.conditions())
            for index, condition in enumerate(plan.conditions()):
                attempt = store.begin_attempt("test", condition["condition_id"])
                store.sample("test", condition["condition_id"], attempt, 0, recorded_sample(condition))
                store.finish_attempt("test", condition["condition_id"], attempt, expected_samples=1)
                if segmented:
                    snapshot = deepcopy(original)
                    if labels:
                        snapshot["hardware"]["lockin"]["lockin_xy"]["channel_label"] = f"execution {index}"
                    execution_id = f"execution-{index}"
                    with store.connection:
                        store.connection.execute(
                            "INSERT INTO combination_executions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                            ("test", execution_id, index, "initial" if index == 0 else "resume",
                             "completed", encode(snapshot), "{}", None, utc_now(), utc_now(), '{"clean":true}', None))
                        store.connection.execute("INSERT INTO combination_attempt_executions VALUES (?,?,?,?)",
                            ("test", condition["condition_id"], attempt, execution_id))
            store.finish_run("test", "completed", {"clean": True}, None)

    def test_per_execution_archive_labels_do_not_use_original_or_live_toml(self):
        self.archive()
        before = self.database.read_bytes()
        rows = load_combination_rows(self.database, run_id="test")
        self.assertEqual([row["channel_label.xy"] for row in rows], ["execution 0", "execution 1"])
        self.assertEqual(self.database.read_bytes(), before)

    def test_legacy_unsegmented_falls_back_to_its_own_archive(self):
        self.archive(segmented=False)
        rows = load_combination_rows(self.database)
        self.assertEqual({row["channel_label.xy"] for row in rows}, {"original run"})

    def test_old_unlabelled_archives_remain_unlabelled(self):
        self.archive(segmented=False, labels=False)
        self.assertTrue(all(lockin_channel_labels(row) == {} for row in load_combination_rows(self.database)))

    def test_saved_plot_uses_reloaded_archive_labels_with_same_semantic_keys(self):
        self.archive(segmented=False)
        before = self.database.read_bytes()
        with redirect_stdout(io.StringIO()):
            dashboard = UnifiedPlotDashboard(self.directory)
            dashboard.add_plot("curve", {"source_paths": [str(self.database)],
                "run_ids": {str(self.database): ["test"]}, "x": "actual.temperature_k", "y": Y})
            dashboard.configuration_path.value = str(self.directory / "label-setup.json")
            dashboard._save_setup()
            dashboard._load_setup()
            card = dashboard.cards[0]
            self.assertEqual(card["y"].value, Y)
            labels = dict((value, label) for label, value in card["y"].options)
            self.assertIn("XY/original run", labels[Y])
            dashboard._render_card(card)
            self.assertIsNotNone(card["rendered"])
        self.assertEqual(self.database.read_bytes(), before)


class PlotLabels(unittest.TestCase):
    def setUp(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        self.addCleanup(lambda: plt.close("all"))

    def labelled(self, label="contact C-D", run="test", **kwargs):
        row = observation(**kwargs)
        row["run_id"] = run
        row["status.lockin"]["channel_labels"] = {"xy": label}
        row["channel_label.xy"] = label
        return row

    def test_axes_options_and_source_rows_keep_semantic_keys(self):
        row = self.labelled()
        before = deepcopy(row)
        self.assertIn("XY/contact C-D", column_label(Y, [row]))
        self.assertEqual({value for _, value in numeric_columns([row])},
                         {value for _, value in numeric_columns([observation()])})
        figure, data = render_plot([row], {"x": X, "y": Y})
        self.assertIn("XY/contact C-D", figure.axes[0].get_ylabel())
        self.assertEqual(row, before)
        self.assertEqual(data["rows"][0]["channel_label.xy"], "contact C-D")
        self.assertEqual(column_label(Y), "measured Lock-in XY h2 R (V RMS)")

    def test_multirun_labels_and_unknown_legacy_are_not_misassigned(self):
        rows = [self.labelled("A", "one"), self.labelled("B", "two"), {**observation(), "run_id": "legacy"}]
        figure, _ = render_plot(rows, {"x": X, "y": Y, "statistics": "mean_sd"})
        labels = figure.axes[0].get_legend_handles_labels()[1]
        self.assertTrue(any("one" in text and "XY/A" in text and "XY/B" not in text for text in labels))
        self.assertTrue(any("two" in text and "XY/B" in text and "XY/A" not in text for text in labels))
        self.assertTrue(any("legacy" in text and "XY/A" not in text and "XY/B" not in text for text in labels))
        self.assertIn("XY (unlabelled)", figure.axes[0].get_ylabel())

    def test_field_panels_xy_z_color_and_heatmap_show_label(self):
        row = self.labelled()
        for role in ("xx", "xy"):
            for harmonic in (1, 2):
                row[f"measured.lockin_{role}_h{harmonic}_amplitude_v"] = 1.
        figure, _ = render_plot([row], {"x": X, "mode": "field"})
        self.assertIn("XY/contact C-D", figure.axes[2].get_title())
        figure, _ = render_plot([row], {"x": X, "y": Y, "z": Y, "mode": "xy_z"})
        self.assertIn("XY/contact C-D", figure.axes[-1].get_ylabel())
        grid = []
        for i in range(2):
            for j in range(2):
                item = self.labelled(condition=i * 2 + j)
                item.update({X: float(i), "requested.field_x_t": float(i),
                             "actual.field_z_t": float(j), "requested.field_z_t": float(j)})
                grid.append(item)
        figure, _ = render_plot(grid, {"x": X, "y": "actual.field_z_t", "z": Y, "mode": "heatmap"})
        self.assertIn("XY/contact C-D", figure.axes[-1].get_ylabel())

    def test_export_keeps_label_and_global_fonts(self):
        import matplotlib as mpl
        fonts_before = list(mpl.rcParams["font.family"])
        rows = [self.labelled("端口3-4")]
        figure, data = render_plot(rows, {"x": X, "y": Y})
        self.assertEqual(mpl.rcParams["font.family"], fonts_before)
        with tempfile.TemporaryDirectory() as directory:
            destination = export_plot_bundle(Path(directory), [
                {"figure": figure, "data": data, "spec": {"x": X, "y": Y}}])
            with (destination / "selected_samples.csv").open(encoding="utf-8") as stream:
                exported = list(csv.DictReader(stream))
            self.assertEqual(exported[0]["channel_label.xy"], "端口3-4")
        self.assertEqual(mpl.rcParams["font.family"], fonts_before)


if __name__ == "__main__":
    unittest.main()
