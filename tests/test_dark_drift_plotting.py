"""Stored dark samples, time axes and existing plot controls; no hardware I/O."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest

from attodry_control.analysis_observations import channel_quality
from attodry_control.combination_analysis import load_combination_rows, load_combination_dark_rows, _sample_time
from attodry_control.combination_store import CombinationStore
from attodry_control.optical_background import BackgroundConfig, MatchedLockinBackground
from attodry_control.unified_plotting import UnifiedPlotDashboard, load_plot_sources, render_plot, export_plot_bundle
from tests.test_photonics_analysis import observation


DARK = "dark.lockin_xy_h2_x_v"
LIGHT = "measured.lockin_xy_h2_x_v"


class DarkDriftTests(unittest.TestCase):
    def setUp(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        self.addCleanup(lambda: plt.close("all"))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.database = self.directory / "scan.sqlite"
        with CombinationStore(self.database) as store:
            store.begin_run("selected", {}, [])
            store.begin_run("other", {}, [])
            for run in ("selected", "other"):
                with store.connection:
                    store.connection.execute("UPDATE combination_runs SET created_at_utc=? WHERE run_id=?",
                                             ("2026-10-18T12:00:00Z", run))
                for group in range(2):
                    context = {"background_visit": group + 1,
                        "requested_conditions": {"optical": {"optical_target_power_w": (group+1)*3e-6,
                                                              "optical_wavelength_nm": 630},
                                                 "lockin": {"lockin_frequency_hz": 8}},
                        "axis_visits": {"optical": {"repeat_index": 0, "axis_indices": [group], "condition_id": str(group)},
                                        "lockin": {"repeat_index": 0, "axis_indices": [group, 0], "condition_id": str(group)}}}
                    reads = []
                    for sample in range(3):
                        row = observation()
                        # The optional AUX gets independently archived status/settings.
                        entry = row["status.lockin"]["samples"][0]
                        entry["selected_harmonics"]["aux"] = 1
                        entry["samples"]["aux"] = {**deepcopy(entry["samples"]["xx"]), "role": "aux"}
                        for stage in ("settings_before", "settings_after"):
                            entry[stage]["aux"] = {**deepcopy(entry[stage]["xx"]), "role": "aux"}
                        for metric in ("x_v", "y_v", "amplitude_v", "phase_deg"):
                            row["measured.lockin_aux_h1_" + metric] = entry["samples"]["aux"][metric]
                        if group == sample == 0:
                            entry["samples"]["xx"]["status"].update(input_overload=True, validity=False)
                        reading = {"module": "lockin", "clean": group != 0 or sample != 0,
                            "valid_for_analysis": group != 0 or sample != 0,
                            "captured_at_utc": f"2026-10-18T15:{group:02d}:{10+sample:02d}+03:00",
                            "status": row["status.lockin"], "actual": {},
                            "measurements": {key[9:]: value for key, value in row.items() if key.startswith("measured.")}}
                        reads.append(reading)
                    bg = MatchedLockinBackground(BackgroundConfig(True, 3, 600), clock=lambda: group*60+10)
                    baseline = bg.capture(reads, context)
                    store.event(run, "optical_dark_background", baseline)
                    store.event(run, "optical_dark_background", baseline)  # replay must not duplicate reads
                store.finish_run(run, "completed", {"clean": True}, None)

    def load(self):
        return load_combination_dark_rows(self.database, run_id="selected")

    def test_each_capture_read_once_and_utc_offset_not_baseline_mean(self):
        original = self.database.read_bytes()
        rows = self.load()
        self.assertEqual(len(rows), 6)
        self.assertEqual([r["elapsed_s"] for r in rows], [10, 11, 12, 70, 71, 72])
        self.assertEqual(len({r["baseline_id"] for r in rows}), 2)
        self.assertEqual({r["run_id"] for r in rows}, {"selected"})
        self.assertIn("+03:00", rows[0]["captured_at_utc"])
        self.assertNotIn("measured.optical_power_w", rows[0])
        self.assertEqual(rows[0]["requested.optical_target_power_w"], 3e-6)
        self.assertEqual(self.database.read_bytes(), original)

    def test_missing_naive_or_invalid_time_is_not_zero(self):
        for timestamp in (None, "invalid", "2026-10-18T12:00:10"):
            row = {}
            _sample_time(row, timestamp, "2026-10-18T12:00:00Z")
            self.assertNotIn("elapsed_s", row)
        row = {}
        _sample_time(row, "2026-10-18T12:00:10Z", "2026-10-18T12:00:00Z")
        self.assertEqual(row["elapsed_s"], 10)

    def test_formal_time_uses_lockin_capture_and_dark_reuse_does_not_add_samples(self):
        darks = self.load()
        condition = {"condition_id": "c0", "sequence_index": 0, "repeat_index": 0,
                     "loop_order": ["lockin"], "axes": {}, "requested": {}}
        with CombinationStore(self.database) as store:
            store.begin_run("light-only", {}, [condition])
            with store.connection:
                store.connection.execute("UPDATE combination_runs SET created_at_utc='2026-10-18T12:00:00Z' WHERE run_id='light-only'")
            attempt = store.begin_attempt("light-only", "c0")
            for index in range(3):
                reading = {"module": "lockin", "captured_at_utc": f"2026-10-18T12:01:{index:02d}Z",
                           "status": {**darks[3]["status.lockin"], "background": {"baseline_id": darks[3]["baseline_id"]}}}
                store.sample("light-only", "c0", attempt, index, {
                    "started_at_utc": "2026-10-18T12:00:59Z", "finished_at_utc": "2026-10-18T12:02:00Z",
                    "simulated": False, "clean": True, "actual": {}, "measurements": {}, "reads": [reading]})
            store.finish_attempt("light-only", "c0", attempt, expected_samples=3)
            store.finish_run("light-only", "completed", {"clean": True}, None)
        light = load_combination_rows(self.database, run_id="light-only")
        self.assertEqual([r["elapsed_s"] for r in light], [60, 61, 62])
        self.assertEqual(load_combination_dark_rows(self.database, run_id="light-only"), ())
        self.assertEqual(len(self.load()), 6)  # referenced means are never reconstructed as new darks

    def test_active_failed_and_unclean_runs_need_audit(self):
        for status, clean in (("active", None), ("failed", False), ("completed", False)):
            with CombinationStore(self.database) as store, store.connection:
                store.connection.execute("UPDATE combination_runs SET status=?,cleanup_json=? WHERE run_id='selected'",
                                         (status, json.dumps({"clean": clean})))
            self.assertEqual(self.load(), ())
            self.assertEqual(len(load_combination_dark_rows(self.database, run_id="selected", audit=True)), 6)

    def test_three_roles_quality_is_per_raw_sample_not_group_or_light(self):
        rows = self.load()
        self.assertEqual(channel_quality(rows[0], DARK), ("clear", ()))
        self.assertEqual(channel_quality(rows[0], "dark.lockin_aux_h1_x_v"), ("clear", ()))
        self.assertEqual(channel_quality(rows[0], "dark.lockin_xx_h1_x_v")[0], "flagged")
        self.assertEqual(channel_quality(rows[1], "dark.lockin_xx_h1_x_v"), ("clear", ()))
        unknown = {**rows[1], "status.lockin": {}}
        self.assertEqual(channel_quality(unknown, DARK)[0], "unknown")

    def test_time_dark_plot_skips_light_rows_and_upcoming_power_dimension(self):
        rows = self.load()
        before = deepcopy(rows)
        mixed = (*rows, {"condition_id": "light", "elapsed_s": 90, LIGHT: 99})
        figure, data = render_plot(mixed, {"x": "elapsed_s", "y": DARK, "statistics": "mean_sd"})
        self.assertEqual(data["report"]["plotted_row_count"], 6)
        self.assertEqual([p["n"] for p in data["summary_rows"]], [3, 3])
        self.assertEqual([p["x"] for p in data["summary_rows"]], [11, 71])
        self.assertEqual(figure.axes[0].get_xlabel(), "Time since run start (s)")
        self.assertEqual(figure.axes[0].get_ylabel(), "Dark Lock-in XY h2 X (V)")
        self.assertEqual(rows, before)
        _, light = render_plot(mixed, {"x": "elapsed_s", "y": LIGHT})
        self.assertEqual(light["report"]["selected_row_count"], 1)

    def test_time_dark_keeps_gate_dimension_guard_and_no_subtraction(self):
        rows = self.load()
        rows = tuple({**r, "requested.gate_bottom_v": i//3} for i, r in enumerate(rows))
        with self.assertRaisesRegex(ValueError, "Unresolved"):
            render_plot(rows, {"x": "elapsed_s", "y": DARK})
        render_plot(rows, {"x": "elapsed_s", "y": DARK, "group_by": "requested.gate_bottom_v"})
        with self.assertRaisesRegex(ValueError, "no subtraction"):
            render_plot(rows, {"x": "elapsed_s", "y": DARK, "photoresponse": "matched"})

    def test_raw_quality_audit_and_export_provenance(self):
        rows = self.load()
        spec = {"x": "elapsed_s", "y": "dark.lockin_xx_h1_x_v"}
        figure, data = render_plot(rows, spec)
        self.assertEqual(data["report"]["excluded_quality_count"], 1)
        self.assertEqual(data["report"]["plotted_row_count"], 5)
        _, audit = render_plot(rows, {**spec, "quality_policy": "include"})
        self.assertEqual(audit["report"]["plotted_row_count"], 6)
        destination = export_plot_bundle(self.directory / "export", [{"figure": figure, "data": data, "spec": spec}])
        text = (destination / "selected_samples.csv").read_text(encoding="utf-8")
        self.assertIn("captured_at_utc", text)
        self.assertIn("baseline_id", text)
        self.assertIn("dark_context", text)

    def test_existing_loader_default_unchanged_and_widget_axes_available(self):
        self.assertEqual(load_plot_sources([self.database]), ())
        self.assertEqual(len(load_plot_sources([self.database], run_ids={str(self.database): ["selected"]}, include_dark=True)), 6)
        with redirect_stdout(io.StringIO()):
            d = UnifiedPlotDashboard(self.directory)
            d.add_plot("curve", {"source_paths": [str(self.database)],
                "run_ids": {str(self.database): ["selected"]}, "x": "elapsed_s", "y": DARK})
            card = d.cards[0]
            self.assertEqual(card["x"].value, "elapsed_s")
            self.assertEqual(card["y"].value, DARK)
            self.assertIn("dark.lockin_aux_h1_x_v", {value for _, value in card["y"].options})
            d._render_card(card)
            self.assertIsNotNone(card["rendered"])
            self.assertTrue(card["image"].value)
            d.configuration_path.value = str(self.directory / "setup.json")
            d._save_setup()
            d._load_setup()
            self.assertEqual(d.cards[0]["y"].value, DARK)


if __name__ == "__main__":
    unittest.main()
