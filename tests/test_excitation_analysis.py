"""Offline 6221 setting provenance and mixed-source analysis regressions."""
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from attodry_control import combination_analysis as analysis
from attodry_control.analysis_observations import channel_quality, repeat_statistics
from attodry_control.combination_store import CombinationStore, encode
from attodry_control.unified_plotting import _curve_groups, column_label, render_plot


CURRENT = "actual.excitation_current_rms_a"
FREQUENCY = "actual.excitation_frequency_hz"
SIGNAL = "measured.lockin_xy_h2_amplitude_v"
CONTRACT = "electrical-excitation-detectors-v1"


def formal(harmonic=2, *, model="SR865A", invalid=False):
    """Native bit 0 means output overload for SR865A, not SR830 INPUT/RESRV."""
    status = {"model": model, "raw": 1 if invalid else 0, "status_known": True,
              "output_overload": invalid, "input_or_reserve_overload": False}
    return {"harmonic": harmonic, "selected_roles": ["xy"], "settings_verified": True,
            "valid_for_analysis_by_role": {"lockin_xy": not invalid},
            "excitation_source": {"kind": "keithley6221", "current_basis": "source_setting_readback",
                                  "measured_current": False},
            "lockin_xy": {"model": model, "lia_status": status, "error_status": 0,
                          "reading": {"model": model, "harmonic": harmonic, "locked": True}}}


def observation(index, current, frequency=200., temperature=20., *, temperature_index=0):
    return {"source_path": "synthetic.sqlite", "run_id": "ac-current", "condition_id": str(index),
            "attempt_index": 0, "sample_index": 0, "repeat_index": 0, "accepted": True,
            "configuration_revision": "a" * 64, "excitation_source": "keithley6221",
            "measurement_contract": CONTRACT, "axes.excitation.index": index,
            "axes.temperature.index": temperature_index, "axes.lockin.index": 0,
            "requested.temperature_k": temperature, "actual.temperature_k": temperature + .001,
            "requested.excitation_current_rms_a": current, CURRENT: current,
            "requested.excitation_frequency_hz": frequency, FREQUENCY: frequency,
            SIGNAL: index * 1e-6 + 1e-6, "status.lockin": {"samples": [formal()]}}


class ExcitationSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "synthetic.sqlite"
        with CombinationStore(self.path):
            pass
        self.writer = sqlite3.connect(self.path)
        self.addCleanup(self.writer.close)
        self.writer.execute("PRAGMA journal_mode=WAL")
        self.plan = {"config_sha256": "a" * 64, "excitation_source": "keithley6221",
                     "measurement_contract": CONTRACT}
        self.cleanup = {"clean": True, "errors": []}
        self.writer.execute("INSERT INTO combination_runs VALUES(?,?,?,?,?,?,?)",
            ("ac", 1, encode(self.plan), "start", "completed", encode(self.cleanup), None))
        self.writer.execute("INSERT INTO combination_executions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("ac", "execution-ac", 0, "initial", "completed", encode(self.plan), "{}", None,
             "start", "finish", encode(self.cleanup), None))
        self.add_sample(0, 1e-6)
        self.writer.commit()

    def add_sample(self, index, current, *, frequency=200., invalid=False):
        context = {"sequence_index": index, "repeat_index": 0,
                   "loop_order": ["temperature", "excitation", "lockin"],
                   "axes": {"temperature": {"index": 0}, "excitation": {"index": index},
                            "lockin": {"index": 0, "metadata": {"detector_only": True}}},
                   "requested": {"temperature_k": 20., "excitation_current_rms_a": current,
                                 "excitation_frequency_hz": frequency}}
        source_status = {"source": "keithley6221", "current_basis": "source_setting_readback",
                         "measured_current": False}
        sample = {"clean": True, "started_at_utc": "start", "finished_at_utc": "finish",
                  "simulated": True,
                  "actual": {"temperature_k": 20.001, "excitation_current_rms_a": current,
                             "excitation_frequency_hz": frequency},
                  "measurements": {"lockin_xy_h2_amplitude_v": 1e-6},
                  "reads": [{"module": "excitation", "captured_at_utc": "source-setting",
                             "actual": {"excitation_current_rms_a": current,
                                        "excitation_frequency_hz": frequency},
                             "measurements": {}, "status": source_status},
                            {"module": "lockin", "captured_at_utc": "formal", "actual": {},
                             "status": {"samples": [formal(invalid=invalid)]}}]}
        self.writer.execute("INSERT INTO combination_conditions VALUES(?,?,?,?)",
                            ("ac", str(index), index, encode(context)))
        self.writer.execute("INSERT INTO combination_attempts VALUES(?,?,?,?,?,?,?)",
                            ("ac", str(index), 0, "accepted", None, "start", "finish"))
        self.writer.execute("INSERT INTO combination_samples VALUES(?,?,?,?,?)",
                            ("ac", str(index), 0, 0, encode(sample)))
        self.writer.execute("INSERT INTO combination_attempt_executions VALUES(?,?,?,?)",
                            ("ac", str(index), 0, "execution-ac"))

    def test_6221_readback_is_not_measured_or_resistance_estimated_current(self):
        snapshot = analysis.load_combination_snapshot(self.path)
        row, = snapshot.rows
        self.assertEqual(row[CURRENT], 1e-6)
        self.assertEqual(row[FREQUENCY], 200.)
        self.assertEqual(row["excitation_source"], "keithley6221")
        self.assertEqual(row["measurement_contract"], CONTRACT)
        self.assertEqual(row["status.excitation"]["current_basis"], "source_setting_readback")
        self.assertFalse(row["status.excitation"]["measured_current"])
        self.assertNotIn("measured.lockin_current_a_rms", row)
        self.assertNotIn("measured.excitation_current_rms_a", row)
        self.assertNotIn("actual.lockin_excitation_v_rms", row)
        self.assertNotIn("source_v_rms", row["status.lockin"]["samples"][0])

    def test_execution_archive_wins_over_run_plan_and_is_exported(self):
        self.writer.execute("UPDATE combination_runs SET plan_json=? WHERE run_id='ac'",
                            (encode({"excitation_source": "sr830"}),))
        self.writer.commit()
        rows = analysis.load_combination_snapshot(self.path).rows
        self.assertEqual(rows[0]["excitation_source"], "keithley6221")
        selected = analysis.select_series(rows, x=CURRENT, y=SIGNAL)
        directory = analysis.export_selection(Path(self.tmp.name) / "selection", selected,
                                              x=CURRENT, y=SIGNAL)
        manifest = json.loads((directory / "selection_manifest.json").read_text(encoding="utf-8"))
        segment, = manifest["execution_segments"]
        self.assertEqual(segment["excitation_source"], "keithley6221")
        self.assertEqual(segment["measurement_contract"], CONTRACT)

    def test_refresh_is_explicit_and_existing_snapshot_remains_unchanged(self):
        snapshot = analysis.load_combination_snapshot(self.path)
        before = copy.deepcopy(snapshot)
        self.add_sample(1, 2e-6)
        self.writer.commit()
        self.assertEqual(snapshot, before)
        refreshed = analysis.load_combination_snapshot(self.path)
        self.assertEqual((len(snapshot.rows), len(refreshed.rows)), (1, 2))
        self.assertNotEqual(snapshot.metadata["snapshot_id"], refreshed.metadata["snapshot_id"])

    def test_legacy_voltage_and_estimated_current_fields_are_preserved(self):
        legacy_plan = {"config_sha256": "b" * 64}
        self.writer.execute("UPDATE combination_executions SET plan_json=?", (encode(legacy_plan),))
        payload = json.loads(self.writer.execute("SELECT payload_json FROM combination_samples").fetchone()[0])
        payload["actual"] = {"lockin_excitation_v_rms": .1, "lockin_frequency_hz": 200.}
        payload["measurements"]["lockin_current_a_rms"] = 1e-6
        self.writer.execute("UPDATE combination_samples SET payload_json=?", (encode(payload),))
        self.writer.commit()
        row, = analysis.load_combination_snapshot(self.path).rows
        self.assertIsNone(row["excitation_source"])
        self.assertIsNone(row["measurement_contract"])
        self.assertEqual(row["actual.lockin_excitation_v_rms"], .1)
        self.assertEqual(row["measured.lockin_current_a_rms"], 1e-6)
        self.assertIn("Estimated", column_label("measured.lockin_current_a_rms"))

    def test_native_status_and_invalid_evidence_survive_loading(self):
        self.add_sample(1, 2e-6, invalid=True)
        self.writer.commit()
        good, bad = analysis.load_combination_snapshot(self.path).rows
        self.assertEqual(channel_quality(good, SIGNAL), ("clear", ()))
        state, reasons = channel_quality(bad, SIGNAL)
        self.assertEqual(state, "flagged")
        self.assertIn("recorded_invalid_for_analysis", reasons)
        self.assertIn("output_overload", reasons)
        self.assertNotIn("input_or_reserve_overload", reasons)
        self.assertEqual(bad["status.lockin"]["samples"][0]["lockin_xy"]["lia_status"]["raw"], 1)


class ExcitationPlotTests(unittest.TestCase):
    def setUp(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        self.addCleanup(lambda: plt.close("all"))

    def test_labels_distinguish_target_from_setting_readback(self):
        self.assertEqual(column_label(CURRENT), "setting readback Excitation current (A RMS)")
        self.assertEqual(column_label(FREQUENCY), "setting readback Excitation frequency (Hz)")
        self.assertEqual(column_label("requested.excitation_current_rms_a"),
                         "target Excitation current (A RMS)")
        self.assertIn("setting readback", analysis._column_label(CURRENT))
        self.assertIn("setting readback", analysis._column_label(FREQUENCY))

    def test_excitation_index_does_not_fragment_current_or_frequency_curves(self):
        rows = [observation(i, current, frequency) for i, (frequency, current) in enumerate(
                ((100., 1e-6), (100., 2e-6), (200., 1e-6), (200., 2e-6)))]
        for x, grouped in ((CURRENT, "requested.excitation_frequency_hz"),
                           (FREQUENCY, "requested.excitation_current_rms_a")):
            with self.subTest(x=x):
                series = analysis.select_series(rows, x=x, y=SIGNAL)
                self.assertEqual([len(item.rows) for item in series], [2, 2])
                figure, data = render_plot(rows, dict(x=x, y=SIGNAL, group_by=grouped,
                                                     curve_style="line"))
                self.assertEqual([len(line.get_xdata()) for line in figure.axes[0].lines], [2, 2])
                self.assertEqual(data["report"]["plotted_row_count"], 4)

    def test_non_x_temperature_revisits_remain_separate(self):
        rows = [observation(i, current, temperature_index=visit)
                for i, (visit, current) in enumerate(((0, 1e-6), (0, 2e-6), (1, 1e-6), (1, 2e-6)))]
        self.assertEqual([len(item.rows) for item in analysis.select_series(rows, x=CURRENT, y=SIGNAL)], [2, 2])
        with self.assertRaisesRegex(ValueError, "temperature index"):
            render_plot(rows, dict(x=CURRENT, y=SIGNAL))

    def test_sources_contracts_configuration_and_harmonics_are_never_pooled(self):
        first = observation(0, 1e-6)
        variants = [first, {**first, "excitation_source": None},
                    {**first, "measurement_contract": None},
                    {**first, "configuration_revision": "b" * 64},
                    {**first, "harmonic": 1}, {**first, "harmonic": 2}]
        self.assertEqual(len(analysis.select_series(variants, x=CURRENT, y=SIGNAL)), 6)
        self.assertEqual(len(_curve_groups(variants, None, separate_samples=False)), 6)
        _, data = render_plot(variants, dict(x=CURRENT, y=SIGNAL, statistics="mean_sd"))
        self.assertEqual(len(data["summary_rows"]), 6)
        self.assertTrue(all(point["n"] == 1 for point in data["summary_rows"]))

    def test_quality_exclusion_keeps_raw_native_status_and_source(self):
        rows = [observation(i, (i + 1) * 1e-6) for i in range(3)]
        rows[1]["status.lockin"]["samples"][0] = formal(invalid=True)
        before = copy.deepcopy(rows)
        _, data = render_plot(rows, dict(x=CURRENT, y=SIGNAL))
        self.assertEqual(data["report"]["excluded_quality_count"], 1)
        self.assertEqual(len(data["rows"]), 2)
        self.assertEqual(rows, before)
        _, included = render_plot(rows, dict(x=CURRENT, y=SIGNAL, quality_policy="include"))
        self.assertEqual(len(included["rows"]), 3)
        self.assertEqual(included["rows"][1]["status.lockin"], rows[1]["status.lockin"])

    def test_source_contract_heatmap_facets_and_scatter_markers_stay_separate(self):
        grid = [observation(i, current, frequency) for i, (frequency, current) in enumerate(
                ((100., 1e-6), (100., 2e-6), (200., 1e-6), (200., 2e-6)))]
        rows = grid + [{**row, "excitation_source": None, SIGNAL: 9e-6} for row in grid]
        _, data = render_plot(rows, dict(mode="heatmap", x=CURRENT, y=FREQUENCY, z=SIGNAL))
        self.assertEqual(data["report"]["panel_count"], 2)
        self.assertIn("excitation_source", data["report"]["facet_keys"])
        figure, data = render_plot(rows, dict(mode="xy_z", x=CURRENT, y=FREQUENCY, z=SIGNAL))
        self.assertEqual(len(figure.axes[0].collections), 2)
        self.assertEqual(data["report"]["plotted_row_count"], 8)

    def test_real_adapter_fake_scan_archive_matches_analysis_contract(self):
        from tests.test_combination_6221 import Combination6221Tests
        fixture = Combination6221Tests("test_full_grid_native_pairs_no_detector_source_writes_and_cleanup")
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        result = fixture.execute()
        self.assertEqual(result["status"], "completed", result)
        snapshot = analysis.load_combination_snapshot(fixture.database)
        self.assertEqual(len(snapshot.rows), 4)
        for row in snapshot.rows:
            self.assertEqual(row["excitation_source"], "keithley6221")
            self.assertEqual(row["measurement_contract"], "electrical-excitation-detectors-v4")
            self.assertEqual(row["status.excitation"]["current_basis"], "source_setting_readback")
            self.assertEqual(row["status.excitation"]["source_kind"], "keithley6221_ac")
            self.assertGreater(row[CURRENT], 0.)
            self.assertGreater(row[FREQUENCY], 0.)
            self.assertNotIn("measured.lockin_current_a_rms", row)
            self.assertNotIn("actual.lockin_excitation_v_rms", row)
            self.assertNotIn("measured.excitation_current_rms_a", row)
            for pair in row["status.lockin"]["samples"]:
                self.assertEqual(pair["excitation_source"]["current_basis"], "source_setting_readback")
                self.assertEqual(pair["lockin_xy"]["model"], "SR865A")
                self.assertNotIn("source_v_rms", pair)
        series = analysis.select_series(snapshot.rows, x=CURRENT, y=SIGNAL)
        self.assertEqual([len(item.rows) for item in series], [2, 2])
        _, data = render_plot(snapshot.rows, dict(x=CURRENT, y=SIGNAL,
                            group_by="requested.excitation_frequency_hz"))
        self.assertEqual(data["report"]["plotted_row_count"], 4)


class ExcitationStatisticsTests(unittest.TestCase):
    def test_direct_statistics_separate_source_and_contract(self):
        first = observation(0, 1e-6)
        rows = [first, {**first, "sample_index": 1, SIGNAL: 3e-6},
                {**first, "excitation_source": "other_source", SIGNAL: 20e-6},
                {**first, "measurement_contract": "other_contract", SIGNAL: 30e-6}]
        points = repeat_statistics(rows, x=CURRENT, y=SIGNAL, mode="mean_sd")
        self.assertEqual([point["n"] for point in points], [2, 1, 1])
        self.assertAlmostEqual(points[0]["y"], 2e-6)
        self.assertEqual([point["y"] for point in points[1:]], [20e-6, 30e-6])

    def test_absent_and_none_metadata_keep_same_legacy_identity(self):
        legacy = observation(0, 1e-6)
        legacy.pop("excitation_source")
        legacy.pop("measurement_contract")
        rows = [legacy, {**legacy, "sample_index": 1, "excitation_source": None,
                         "measurement_contract": None, SIGNAL: 3e-6}]
        point, = repeat_statistics(rows, x=CURRENT, y=SIGNAL, mode="mean_sd")
        self.assertEqual(point["n"], 2)
        self.assertAlmostEqual(point["y"], 2e-6)


if __name__ == "__main__":
    unittest.main()
