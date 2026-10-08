"""Synthetic saved-progress tests; no instrument modules or resources."""

from copy import deepcopy
from dataclasses import replace
import json
import math
import os
from pathlib import Path
import tempfile
import unittest

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
except ImportError:
    plt = None

from attodry_control.commissioning_analysis import aggregate_sweep_samples, load_sweep_samples

from attodry_control.commissioning_progress_analysis import (
    CommissioningProgressReader, discover_progress_records, plot_progress_sweep,
)
from attodry_control.progress_monitor import ProgressFormatError


def header(scan="excitation", *, name="same name", points_total=2):
    return {"event": "scan_started", "schema_version": 1,
            "command": "lockin-sweep", "scan": scan, "points_total": points_total,
            "run_metadata": {"name": name, "note": "only a label"},
            "captured_unix_s": 1.0}


def point(index=0, *, voltage=0.0499, current=1.25e-6, frequency=17.77):
    return {"point_index": index, "points_total": 2,
            "target_frequency_hz": 17.777, "actual_frequency_hz": frequency,
            "source_v_rms": 0.05, "source_readback_v_rms": voltage,
            "nominal_current_a_rms": current}


def instrument(role, *, harmonic=1, amplitude=2e-6, phase=179.0, model="SR830"):
    return {"model": model, "error_status": 0,
            "reading": {"model": model, "role": role, "harmonic": harmonic,
                        "frequency_hz": 17.77, "x_v": amplitude, "y_v": 0.0,
                        "amplitude_v": amplitude, "phase_deg": phase,
                        "locked": True, "overload": False},
            "lia_status": {"model": model, "raw": 0,
                           "input_or_reserve_overload": False,
                           "filter_overload": False, "output_overload": False,
                           "reference_unlocked": False, "status_known": True,
                           "unknown_status_bits": 0, "instrument_error": False}}


def formal(scan="excitation", *, sweep_point=None, sample_index=0,
           harmonic=1, selected=("xx", "xy")):
    coordinates = point() if sweep_point is None else sweep_point
    return {"event": "lockin_formal_sample", "scan": scan, "captured_unix_s": 2.0,
            "sweep_point": coordinates,
            "sample": {"sweep_point": deepcopy(coordinates),
                       "harmonic": harmonic, "sample_index": sample_index,
                       "selected_roles": list(selected), "problems": [],
                       "problems_by_role": {"lockin_xx": [], "lockin_xy": []},
                       "valid_for_analysis_by_role": {"lockin_xx": True, "lockin_xy": True},
                       "lockin_xx": instrument("xx", harmonic=harmonic if "xx" in selected else 1),
                       "lockin_xy": instrument("xy", harmonic=harmonic if "xy" in selected else 2,
                                               model="SR865A")}}


def terminal(outcome="rejected", *, cleanup=True):
    return {"event": "scan_finished", "scan": "excitation",
            "captured_unix_s": 5.0, "completed": outcome == "completed",
            "outcome": outcome, "cleanup_verified": cleanup}


class CommissioningProgressAnalysisTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="progress-analysis-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.path = self.directory / "a_lockin_excitation_progress.jsonl"
        if plt is not None:
            self.addCleanup(plt.close, "all")

    def write(self, *events):
        self.path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")

    def append(self, *events):
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write("".join(json.dumps(event) + "\n" for event in events))

    def test_discovery_is_explicit_standalone_and_never_selects_latest(self):
        for name in ("a_lockin_excitation_progress.jsonl", "b_lockin_frequency_progress.jsonl",
                     "c_lockin_frequency_excitation_progress.jsonl",
                     "d_temperature_excitation_progress.jsonl", "not-a-progress.jsonl"):
            (self.directory / name).write_text("", encoding="utf-8")
        discovered = discover_progress_records(self.directory)
        self.assertEqual([path.name[0] for path in discovered], ["a", "b", "c"])
        self.assertTrue(all(isinstance(path, Path) and path.is_absolute() for path in discovered))
        with self.assertRaises(NotADirectoryError):
            discover_progress_records(self.directory / "missing")

    def test_incremental_rows_use_actual_roles_harmonics_and_raw_pair_counts(self):
        first = formal(selected=("xx",))
        self.write(header(), first)
        reader = CommissioningProgressReader(self.path)
        first_snapshot = reader.refresh()
        self.assertEqual(first_snapshot.formal_pair_count, 1)
        self.assertEqual([(r.role, r.harmonic) for r in first_snapshot.rows], [("xx", 1)])
        self.assertEqual(first_snapshot.completed_point_count, 0)
        self.assertEqual(first_snapshot.rows[0].sine_output_v_rms, 0.0499)
        self.assertEqual(first_snapshot.rows[0].nominal_current_a_rms, 1.25e-6)
        self.assertIsNone(first_snapshot.rows[0].recorded_external_series_resistance_ohm)
        self.assertEqual(reader.refresh(), first_snapshot)
        self.append(first, formal(harmonic=2, selected=("xy",)))
        snapshot = reader.refresh()
        self.assertEqual(snapshot.formal_pair_count, 2)
        self.assertEqual([(r.role, r.harmonic) for r in snapshot.rows], [("xx", 1), ("xy", 2)])
        self.assertEqual(snapshot.quality_counts,
                         {"xx": {"total": 1, "clean": 1, "excluded": 0},
                          "xy": {"total": 1, "clean": 1, "excluded": 0}})
        self.assertEqual(snapshot.last_event_unix_s, 2.0)
        self.assertFalse(snapshot.finished)

    def test_nonfinite_selected_reading_retained_raw_and_filtered_clean(self):
        for field in ("x_v", "y_v", "amplitude_v", "phase_deg", "frequency_hz"):
            with self.subTest(field=field):
                event = formal()
                event["sample"]["lockin_xy"]["reading"][field] = math.nan
                self.write(header(), event)
                snapshot = CommissioningProgressReader(self.path).refresh()
                self.assertEqual(len(snapshot.rows), 2)
                self.assertEqual([r.role for r in snapshot.clean_rows], ["xx"])
                self.assertEqual(snapshot.quality_counts["xy"]["excluded"], 1)
                self.assertTrue(math.isnan(getattr(snapshot.rows[1],
                                                   "reference_frequency_hz" if field == "frequency_hz" else field)))

    def test_per_role_invalid_overload_and_native_unknown_status_are_excluded(self):
        mutations = (
            lambda event: event["sample"]["valid_for_analysis_by_role"].update(lockin_xy=False),
            lambda event: event["sample"]["lockin_xy"]["lia_status"].update(input_or_reserve_overload=True),
            lambda event: event["sample"]["lockin_xy"]["lia_status"].update(output_overload=True),
            lambda event: event["sample"]["lockin_xy"]["lia_status"].update(status_known=False),
            lambda event: event["sample"]["lockin_xy"]["lia_status"].update(unknown_status_bits=1024),
            lambda event: event["sample"]["lockin_xy"]["lia_status"].update(instrument_error=True),
            lambda event: event["sample"]["lockin_xy"].update(error_status=1),
            lambda event: event["sample"]["lockin_xy"]["reading"].update(locked=False),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                event = formal()
                mutation(event)
                self.write(header(), event)
                snapshot = CommissioningProgressReader(self.path).refresh()
                self.assertEqual([row.role for row in snapshot.clean_rows], ["xx"])
                self.assertEqual(snapshot.formal_pair_count, 1)

    def test_missing_unselected_phase_does_not_discard_selected_role(self):
        event = formal(selected=("xx",))
        event["sample"]["lockin_xy"]["reading"].pop("phase_deg")
        self.write(header(), event)
        snapshot = CommissioningProgressReader(self.path).refresh()
        self.assertEqual([row.role for row in snapshot.clean_rows], ["xx"])
        self.assertEqual(snapshot.formal_pair_count, 1)
        self.assertEqual(snapshot.current_point["source_readback_v_rms"], 0.0499)

    def test_selected_harmonic_and_reading_role_conflicts_fail_closed(self):
        for update in ({"harmonic": 2}, {"role": "xy"}):
            event = formal(selected=("xx",))
            event["sample"]["lockin_xx"]["reading"].update(update)
            self.write(header(), event)
            with self.assertRaises(ProgressFormatError):
                CommissioningProgressReader(self.path).refresh()

    def test_unverified_settings_unknown_sr830_status_and_invalid_physics_keep_raw(self):
        mutations = (
            lambda event: event["sample"].update(settings_verified=False),
            lambda event: event["sample"]["lockin_xx"]["lia_status"].pop("filter_overload"),
            lambda event: event["sample"]["lockin_xx"]["reading"].update(amplitude_v=-1.0),
            lambda event: event["sample"]["lockin_xx"]["reading"].update(frequency_hz=0.0),
        )
        for mutation in mutations:
            event = formal(selected=("xx",))
            mutation(event)
            self.write(header(), event)
            snapshot = CommissioningProgressReader(self.path).refresh()
            self.assertEqual(len(snapshot.rows), 1)
            self.assertEqual(snapshot.clean_rows, ())
            self.assertEqual(snapshot.quality_counts["xx"]["excluded"], 1)

    def test_undefined_native_zero_phase_keeps_rxy_and_no_fabricated_phase(self):
        event = formal(selected=("xy",))
        event["sample"]["lockin_xy"]["reading"].update(
            x_v=0.0, y_v=0.0, amplitude_v=0.0, phase_deg=None)
        self.write(header(), event)
        snapshot = CommissioningProgressReader(self.path).refresh()
        self.assertEqual(len(snapshot.clean_rows), 1)
        self.assertIsNone(snapshot.clean_rows[0].phase_deg)
        if plt is None:
            return
        self.assertEqual(len(plot_progress_sweep(snapshot)), 1)
        phase_figure, = plot_progress_sweep(snapshot, metric="phase_deg")
        self.assertEqual(len(phase_figure.axes[0].lines), 0)
        self.assertIn("No usable phase points after display thresholds",
                      [text.get_text() for text in phase_figure.axes[0].texts])

    def test_formal_schema_requires_explicit_context_identity_readback_and_roles(self):
        mutations = (
            lambda e: e.pop("sweep_point"),
            lambda e: e["sweep_point"].pop("point_index"),
            lambda e: e["sweep_point"].pop("actual_frequency_hz"),
            lambda e: e["sweep_point"].pop("source_readback_v_rms"),
            lambda e: e["sample"].pop("sample_index"),
            lambda e: e["sample"].pop("selected_roles"),
            lambda e: e["sample"].update(selected_roles=[]),
            lambda e: e["sample"].update(selected_roles=["xx", "xx"]),
            lambda e: e["sample"].update(selected_roles=["lockin_xx"]),
            lambda e: e["sample"].pop("lockin_xy"),
            lambda e: e["sample"]["lockin_xy"]["reading"].pop("harmonic"),
            lambda e: e["sample"]["valid_for_analysis_by_role"].pop("lockin_xy"),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                event = formal()
                mutation(event)
                self.write(header(), event)
                reader = CommissioningProgressReader(self.path)
                for _ in range(2):
                    with self.assertRaises(ProgressFormatError):
                        reader.refresh()

    def test_conflicting_same_pair_identity_fails_each_refresh_and_replacement_recovers(self):
        self.write(header(), formal())
        reader = CommissioningProgressReader(self.path)
        self.assertEqual(reader.refresh().formal_pair_count, 1)
        changed = formal()
        changed["sample"]["lockin_xx"]["reading"]["x_v"] = 3e-6
        self.append(changed)
        for _ in range(2):
            with self.assertRaisesRegex(ProgressFormatError, "identity"):
                reader.refresh()
        replacement = self.directory / "replacement.jsonl"
        replacement.write_text(json.dumps(header(name="new run")) + "\n", encoding="utf-8")
        os.replace(replacement, self.path)
        snapshot = reader.refresh()
        self.assertEqual(snapshot.run_name, "new run")
        self.assertEqual(snapshot.rows, ())
        self.assertEqual(snapshot.formal_pair_count, 0)

    def test_incomplete_line_defers_sample_and_truncation_clears_rows(self):
        self.write(header())
        encoded = json.dumps(formal()).encode("utf-8")
        with self.path.open("ab") as stream:
            stream.write(encoded[:70])
        reader = CommissioningProgressReader(self.path)
        self.assertEqual(reader.refresh().rows, ())
        with self.path.open("ab") as stream:
            stream.write(encoded[70:] + b"\n")
        self.assertEqual(reader.refresh().formal_pair_count, 1)
        self.write(header(name="replacement"))
        snapshot = reader.refresh()
        self.assertEqual(snapshot.rows, ())
        self.assertEqual(snapshot.run_name, "replacement")

    def test_complete_corruption_or_missing_file_never_returns_old_data_fresh(self):
        self.write(header(), formal())
        reader = CommissioningProgressReader(self.path)
        reader.refresh()
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write("{bad}\n")
        for _ in range(2):
            with self.assertRaises(ProgressFormatError):
                reader.refresh()
        self.path.unlink()
        with self.assertRaises(FileNotFoundError):
            reader.refresh()

    def test_completed_points_count_events_not_samples_and_verified_terminal(self):
        events = [header()]
        for index in range(2):
            coordinates = point(index, voltage=0.02 * (index + 1))
            events.extend([formal(sweep_point=coordinates),
                           {"event": "lockin_point_completed", "scan": "excitation",
                            "sweep_point": coordinates, "captured_unix_s": 4.0}])
        self.write(*events, terminal("completed"))
        snapshot = CommissioningProgressReader(self.path).refresh()
        self.assertEqual(snapshot.completed_point_count, 2)
        self.assertEqual(snapshot.formal_pair_count, 2)
        self.assertTrue(snapshot.finished)
        self.assertTrue(snapshot.cleanup_verified)
        self.assertEqual(snapshot.outcome, "completed")
        if plt is not None:
            self.assertEqual(len(plot_progress_sweep(snapshot)), 2)

    def test_complete_progress_matches_final_json_selected_quality_coordinates_and_statistics(self):
        for scan in ("frequency", "excitation", "frequency_excitation"):
            events = [header(scan)]
            archived_points = []
            for index in range(2):
                coordinates = point(index, voltage=0.05 * (index + 1),
                                    current=1.25e-6 * (index + 1), frequency=17.77 * (index + 1))
                samples = []
                for harmonic, role in ((1, "xx"), (2, "xy")):
                    event = formal(scan, sweep_point=coordinates, harmonic=harmonic, selected=(role,))
                    if role == "xy" and index == 1:
                        event["sample"]["valid_for_analysis_by_role"]["lockin_xy"] = False
                    samples.append(event["sample"])
                    events.append(event)
                archived_points.append({**coordinates, "samples": samples})
                events.append({"event": "lockin_point_completed", "scan": scan,
                               "sweep_point": coordinates})
            events.append({**terminal("completed"), "scan": scan})
            self.write(*events)
            final_path = self.directory / f"final_{scan}.json"
            final_path.write_text(json.dumps({"scan": scan, "completed": True,
                                             "outcome": "completed", "points": archived_points}),
                                  encoding="utf-8")
            snapshot = CommissioningProgressReader(self.path).refresh()
            final_rows = load_sweep_samples(final_path)
            self.assertEqual(tuple(replace(row, source_path=str(final_path)) for row in snapshot.rows),
                             final_rows)
            final_clean = load_sweep_samples(final_path, sample_statuses=("clean",))
            self.assertEqual(tuple(replace(row, source_path=str(final_path)) for row in snapshot.clean_rows),
                             final_clean)
            for metric in ("amplitude_v", "x_v", "y_v", "phase_deg"):
                for axis in ("actual_frequency_hz", "sine_output_v_rms", "nominal_current_a_rms"):
                    self.assertEqual(aggregate_sweep_samples(snapshot.clean_rows, metric=metric, x_axis=axis),
                                     aggregate_sweep_samples(final_clean, metric=metric, x_axis=axis))

    def test_terminal_rejected_interrupted_keep_raw_quality_but_default_plot_rejects(self):
        for outcome in ("rejected", "interrupted"):
            self.write(header(), formal(), terminal(outcome, cleanup=False))
            snapshot = CommissioningProgressReader(self.path).refresh()
            self.assertEqual(len(snapshot.rows), 2)
            self.assertEqual(snapshot.quality_counts["xx"]["clean"], 1)
            self.assertTrue(snapshot.finished)
            self.assertFalse(snapshot.cleanup_verified)
            if plt is not None:
                with self.assertRaisesRegex(ValueError, "explicit audit"):
                    plot_progress_sweep(snapshot)

    def test_conflicting_terminal_or_data_after_terminal_fails_closed(self):
        endings = ([terminal(), formal(sample_index=1)],
                   [terminal(), terminal("interrupted")],
                   [terminal("completed")],
                   [dict(terminal(), completed=True)],
                   [dict(terminal(), cleanup_verified="false")])
        for ending in endings:
            with self.subTest(ending=ending):
                self.write(header(), formal(), *ending)
                with self.assertRaises(ProgressFormatError):
                    CommissioningProgressReader(self.path).refresh()

    @unittest.skipIf(plt is None, "Matplotlib is not installed")
    def test_all_scan_modes_metrics_and_archived_current_axis_render_actual_channels(self):
        for scan in ("frequency", "excitation", "frequency_excitation"):
            self.write(header(scan), formal(scan, selected=("xy",)))
            snapshot = CommissioningProgressReader(self.path).refresh()
            for metric in ("amplitude_v", "x_v", "y_v", "phase_deg"):
                for x_axis in ("sine_output_v_rms", "nominal_current_a_rms"):
                    with self.subTest(scan=scan, metric=metric, x_axis=x_axis):
                        figure, = plot_progress_sweep(snapshot, metric=metric, x_axis=x_axis)
                        figure.canvas.draw()
                        axis = figure.axes[0]
                        line = axis.containers[0].lines[0]
                        expected = 17.77 if scan == "frequency" else (
                            1.25e-6 if x_axis == "nominal_current_a_rms" else 0.0499)
                        self.assertEqual(list(line.get_xdata()), [expected])
                        self.assertIsNotNone(axis.get_legend())
                        plt.close(figure)

    @unittest.skipIf(plt is None, "Matplotlib is not installed")
    def test_combined_curve_frequency_separation_and_phase_circular_mean_unwrap(self):
        events = [header("frequency_excitation", points_total=4)]
        for index, (frequency, voltage, phase) in enumerate(
                ((10.0, 0.01, 179.0), (10.0, 0.02, -179.0),
                 (20.0, 0.01, 10.0), (20.0, 0.02, 11.0))):
            coordinates = point(index, voltage=voltage, frequency=frequency)
            coordinates["points_total"] = 4
            event = formal("frequency_excitation", sweep_point=coordinates, selected=("xx",))
            event["sample"]["lockin_xx"]["reading"]["phase_deg"] = phase
            events.append(event)
        self.write(*events)
        snapshot = CommissioningProgressReader(self.path).refresh()
        figure, = plot_progress_sweep(snapshot, metric="phase_deg")
        self.assertEqual([list(container.lines[0].get_ydata()) for container in figure.axes[0].containers],
                         [[179.0, 181.0], [10.0, 11.0]])
        repeated = deepcopy(events[1])
        repeated["sample"]["sample_index"] = 1
        repeated["sample"]["lockin_xx"]["reading"]["phase_deg"] = -179.0
        self.append(repeated)
        snapshot = CommissioningProgressReader(self.path).refresh()
        figure, = plot_progress_sweep(snapshot, metric="phase_deg")
        first = figure.axes[0].containers[0].lines[0]
        self.assertAlmostEqual(abs(first.get_ydata()[0]), 180.0)

    @unittest.skipIf(plt is None, "Matplotlib is not installed")
    def test_phase_threshold_gaps_and_missing_current_rejects_only_that_axis(self):
        event = formal(selected=("xx",))
        event["sweep_point"].pop("nominal_current_a_rms")
        event["sample"]["sweep_point"].pop("nominal_current_a_rms")
        self.write(header(), event)
        snapshot = CommissioningProgressReader(self.path).refresh()
        self.assertEqual(len(plot_progress_sweep(snapshot)), 1)
        with self.assertRaisesRegex(ValueError, "Archived nominal current is missing"):
            plot_progress_sweep(snapshot, x_axis="nominal_current_a_rms")
        figure, = plot_progress_sweep(snapshot, metric="phase_deg", minimum_phase_amplitude_v=3e-6)
        self.assertTrue(math.isnan(figure.axes[0].containers[0].lines[0].get_ydata()[0]))
        self.assertIn("No usable phase points after display thresholds",
                      [text.get_text() for text in figure.axes[0].texts])
        second = formal(selected=("xx",), sweep_point=point(1, voltage=0.0999))
        second["sample"]["lockin_xx"]["reading"].update(x_v=4e-6, amplitude_v=4e-6)
        self.append(second)
        partial = CommissioningProgressReader(self.path).refresh()
        figure, = plot_progress_sweep(partial, metric="phase_deg", minimum_phase_amplitude_v=3e-6)
        values = figure.axes[0].containers[0].lines[0].get_ydata()
        self.assertTrue(math.isnan(values[0]))
        self.assertTrue(math.isfinite(values[1]))
        self.assertNotIn("No usable phase points after display thresholds",
                         [text.get_text() for text in figure.axes[0].texts])

    @unittest.skipIf(plt is None, "Matplotlib is not installed")
    def test_optional_phase_sd_threshold_and_circular_repeat_spread(self):
        first = formal(selected=("xx",))
        first["sample"]["lockin_xx"]["reading"]["phase_deg"] = 10.0
        second = deepcopy(first)
        second["sample"]["sample_index"] = 1
        second["sample"]["lockin_xx"]["reading"]["phase_deg"] = 30.0
        self.write(header(), first, second)
        snapshot = CommissioningProgressReader(self.path).refresh()
        figure, = plot_progress_sweep(snapshot, metric="phase_deg")
        self.assertTrue(math.isnan(figure.axes[0].containers[0].lines[0].get_ydata()[0]))
        self.assertIn("No usable phase points after display thresholds",
                      [text.get_text() for text in figure.axes[0].texts])
        figure, = plot_progress_sweep(snapshot, metric="phase_deg",
                                      maximum_phase_standard_deviation_deg=None)
        self.assertAlmostEqual(figure.axes[0].containers[0].lines[0].get_ydata()[0], 20.0)
        self.assertNotIn("No usable phase points after display thresholds",
                         [text.get_text() for text in figure.axes[0].texts])

    def test_nonpositive_frequency_and_nonfinite_coordinates_are_raw_exclusions(self):
        for updates in ({"actual_frequency_hz": -1.0}, {"source_readback_v_rms": math.inf},
                        {"nominal_current_a_rms": math.nan}):
            coordinates = point()
            coordinates.update(updates)
            self.write(header("frequency"), formal("frequency", sweep_point=coordinates))
            snapshot = CommissioningProgressReader(self.path).refresh()
            self.assertEqual(len(snapshot.rows), 2)
            self.assertEqual(snapshot.clean_rows, ())
            if plt is not None:
                self.assertEqual(plot_progress_sweep(snapshot), ())


if __name__ == "__main__":
    unittest.main()
