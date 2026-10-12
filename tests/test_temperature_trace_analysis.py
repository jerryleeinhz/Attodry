"""File-only raw temperature/heater analysis, with real Agg figure exports."""
import csv
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
import tempfile
import unittest

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from attodry_control.temperature_trace_analysis import (
    export_temperature_trace_csv, load_temperature_trace, plot_temperature_trace,
    run,
)


def observation(index, *, event="temperature_telemetry", power=True):
    sample = {
        "event": event, "sample_id": f"sample-{index}",
        "captured_unix_s": 1_000 + index * 60,
        "point_index": index, "requested_temperature_k": 17.3,
        "state": {
            "sample_temperature_k": (5.7, 11.7, 23.47)[index],
            "vti_temperature_k": 4.5 + index * 0.1,
            "user_temperature_k": 17.3, "temperature_control_enabled": True,
            "error_code": 0,
        },
        "state_read_timing": {"started_unix_s": 999 + index * 60,
                              "finished_unix_s": 1_000 + index * 60},
    }
    if power:
        sample.update({
            "heater_power": {"sample_w": 1.3 - index * 0.1, "vti_w": 1.4},
            "telemetry_complete": True,
            "heater_power_read_timing": {"started_unix_s": 1_000 + index * 60,
                                         "finished_unix_s": 1_002 + index * 60},
            "heater_power_reads": {
                "sample_w": {"finished_unix_s": 1_001 + index * 60},
                "vti_w": {"finished_unix_s": 1_002 + index * 60},
            },
        })
    return sample


class TemperatureTraceAnalysisTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="temperature-trace-")
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.addCleanup(plt.close, "all")

    def progress(self, events, *, tail=b""):
        path = self.directory / "temperature_progress.jsonl"
        path.write_bytes(b"".join((json.dumps(e) + "\n").encode() for e in events) + tail)
        return path

    def test_legacy_samples_load_without_inventing_power_and_defer_truncated_tail(self):
        events = [observation(0, event="temperature_sample", power=False),
                  observation(1, event="measurement_temperature_sample", power=False)]
        path = self.progress(events, tail=b'{"event":"temperature_telemetry",')
        trace = load_temperature_trace(progress=path)
        self.assertEqual(len(trace.samples), 2)
        self.assertNotIn("heater_power", trace.samples[1])
        self.assertEqual(trace.samples[1]["state"]["sample_temperature_k"], 11.7)
        figure = plot_temperature_trace(trace)
        self.assertTrue(all(math.isnan(y) for y in figure.axes[1].lines[0].get_ydata()))
        self.assertIn("not recorded", figure.axes[1].texts[0].get_text())

    def test_completion_merges_by_id_while_unsafe_raw_observation_remains(self):
        raw = observation(0, event="temperature_sample", power=False)
        raw["attempt_index"] = 7
        complete = observation(0)
        unsafe = observation(2, event="temperature_sample", power=False)
        path = self.progress([raw, complete, unsafe])
        trace = load_temperature_trace(progress=path)
        self.assertEqual(len(trace.samples), 2)
        self.assertEqual(trace.samples[0]["attempt_index"], 7)
        self.assertEqual(trace.samples[0]["heater_power"]["sample_w"], 1.3)
        self.assertEqual(len(trace.samples[0]["raw_events"]), 2)
        self.assertEqual(trace.samples[1]["state"]["sample_temperature_k"], 23.47)
        self.assertNotIn("heater_power", trace.samples[1])

    def test_unsafe_preflight_setpoint_and_snapshot_events_keep_their_sample_ids(self):
        events = [observation(i, event=name, power=False) for i, name in enumerate(
            ("point_preflight", "setpoint_confirmed", "temperature_snapshot"))]
        trace = load_temperature_trace(progress=self.progress(events))
        self.assertEqual(len(trace.samples), 3)
        self.assertEqual(trace.samples[-1]["state"]["sample_temperature_k"], 23.47)

    def test_csv_keeps_partial_read_errors_unknown_fields_and_missing_power_blank(self):
        partial = observation(1)
        partial.update({"heater_power": {"sample_w": 1.2, "vti_w": None},
                        "telemetry_complete": False,
                        "heater_power_read_error": "VTI transport failure",
                        "future_raw_field": {"keep": True}})
        path = self.progress([partial, observation(2, power=False)])
        trace = load_temperature_trace(progress=path)
        destination = export_temperature_trace_csv(trace, self.directory / "exports" / "samples.csv")
        with destination.open(encoding="utf-8", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(rows[0]["sample_heater_power_w"], "1.2")
        self.assertEqual(rows[0]["vti_heater_power_w"], "")
        self.assertEqual(rows[1]["sample_heater_power_w"], "")
        self.assertEqual(rows[0]["telemetry_complete"], "False")
        self.assertEqual(rows[0]["heater_power_read_error"], "VTI transport failure")
        self.assertEqual(rows[0]["vti_heater_read_finished_unix_s"], "1062")
        self.assertTrue(json.loads(rows[0]["raw_events_json"])[0]["future_raw_field"]["keep"])

    def test_sqlite_requires_explicit_existing_run_and_never_writes_database(self):
        path = self.directory / "scan.sqlite"
        with closing(sqlite3.connect(path)) as connection:
            connection.executescript(
                "CREATE TABLE combination_runs(run_id TEXT, plan_json TEXT);"
                "CREATE TABLE combination_events(event_id INTEGER PRIMARY KEY, run_id TEXT, "
                "created_at_utc TEXT, event_type TEXT, payload_json TEXT);"
            )
            plan = {"hardware": {"temperature": {"temperature_stability": {
                "acceptance_mode": "target", "criteria": {"tolerance": 0.3}}}}}
            connection.executemany("INSERT INTO combination_runs VALUES (?, ?)",
                                   [("chosen", json.dumps(plan)), ("other", "{}")])
            for index, run_id in enumerate(("chosen", "other")):
                connection.execute("INSERT INTO combination_events VALUES (?, ?, ?, ?, ?)",
                    (index, run_id, "2026-10-12T12:00:00+00:00", "temperature_telemetry",
                     json.dumps(observation(index))))
            connection.commit()
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "explicit"):
            load_temperature_trace(database=path)
        with self.assertRaisesRegex(ValueError, "does not exist"):
            load_temperature_trace(database=path, run_id="missing")
        trace = load_temperature_trace(database=path, run_id="chosen")
        self.assertEqual(trace.run_id, "chosen")
        self.assertEqual(len(trace.samples), 1)
        self.assertEqual(trace.samples[0]["sample_id"], "sample-0")
        self.assertEqual(trace.samples[0]["run_id"], "chosen")
        self.assertEqual(trace.samples[0]["tolerance_k"], 0.3)
        self.assertEqual(trace.samples[0]["acceptance_mode"], "target")
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(path.with_name(path.name + "-journal").exists())

    def test_raw_plot_uses_channel_query_times_retains_spike_and_missing_power_gap(self):
        first, partial, last = observation(0), observation(1), observation(2)
        partial["heater_power"]["sample_w"] = None
        partial["telemetry_complete"] = False
        events = [
            {"event": "scan_started", "captured_unix_s": 999,
             "measurement_config": {"temperature_stability": {
                 "tolerance_k": 0.1, "acceptance_mode": "target"}}},
            {"event": "setpoint_confirmed", "captured_unix_s": 999, "target_k": 17.3},
            first, partial,
            {"event": "temperature_qualification_completed", "captured_unix_s": 1060},
            last,
            {"event": "scan_finished", "outcome": "rejected", "captured_unix_s": 1122},
        ]
        trace = load_temperature_trace(progress=self.progress(events))
        figure = plot_temperature_trace(trace)
        temperature, power = figure.axes
        self.assertEqual(list(temperature.lines[0].get_ydata()), [5.7, 11.7, 23.47])
        self.assertEqual(list(power.lines[0].get_xdata()), [1 / 60, 61 / 60, 121 / 60])
        self.assertEqual(list(power.lines[1].get_xdata()), [2 / 60, 62 / 60, 122 / 60])
        self.assertTrue(math.isnan(power.lines[0].get_ydata()[1]))
        labels = temperature.get_legend_handles_labels()[1]
        self.assertIn("Recorded target tolerance", labels)
        self.assertIn("Qualified", labels)
        self.assertIn("Failure / interruption", labels)
        figure.canvas.draw()
        renderer = figure.canvas.get_renderer()
        for axis in figure.axes:
            bounds = axis.get_legend().get_window_extent(renderer)
            self.assertLessEqual(bounds.x1, figure.bbox.x1 + 1)

    def test_cli_exports_real_png_pdf_csv_manifest_without_overwriting(self):
        path = self.progress([observation(0), observation(1), observation(2)])
        output = self.directory / "report"
        arguments = ["--progress", str(path), "--output-directory", str(output), "--plot"]
        self.assertEqual(run(arguments), 0)
        for suffix in ("csv", "png", "pdf"):
            self.assertGreater((output / f"temperature_trace.{suffix}").stat().st_size, 100)
        manifest = json.loads((output / "temperature_trace_manifest.json").read_text())
        self.assertEqual(manifest["samples"], 3)
        self.assertIn("no zero fill", manifest["missing_values"])
        self.assertIn("not sensor refresh", manifest["timestamps"])
        with self.assertRaises(FileExistsError):
            run(arguments)
        with self.assertRaisesRegex(ValueError, "separate output"):
            run(["--progress", str(path), "--output-directory", str(path.parent)])

    def test_complete_malformed_line_is_rejected(self):
        path = self.progress([], tail=b'{malformed}\n')
        with self.assertRaises(ValueError):
            load_temperature_trace(progress=path)

    def test_nested_temperature_config_and_stable_readback_do_not_imply_target_band(self):
        events = [{"event": "scan_started", "measurement_config": {
            "temperature": {"temperature_stability": {
                "acceptance_mode": "stable-readback", "criteria": {"tolerance": 0.2}}}
        }}, observation(0)]
        trace = load_temperature_trace(progress=self.progress(events))
        self.assertEqual(trace.samples[0]["tolerance_k"], 0.2)
        self.assertEqual(trace.samples[0]["acceptance_mode"], "stable-readback")
        figure = plot_temperature_trace(trace)
        self.assertNotIn("Recorded target tolerance", figure.axes[0].get_legend_handles_labels()[1])

    def test_state_control_and_device_error_transitions_are_marked(self):
        first, last = observation(0), observation(1)
        last["state"].update(temperature_control_enabled=False, error_code=3)
        figure = plot_temperature_trace(load_temperature_trace(progress=self.progress([first, last])))
        labels = figure.axes[0].get_legend_handles_labels()[1]
        self.assertIn("Device error", labels)
        self.assertIn("Control off", labels)


if __name__ == "__main__":
    unittest.main()
