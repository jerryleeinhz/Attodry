import json
from dataclasses import replace
import hashlib
import math
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
import uuid
from unittest.mock import patch

from attodry_control.commissioning_analysis import (
    HarmonicScalingRules,
    ExcitationPathResistance,
    aggregate_sweep_samples,
    aggregate_sweep_repeatability,
    browse_and_load_commissioning_file,
    discover_commissioning_records,
    excitation_path_from_sweep_files,
    export_commissioning_csv,
    fit_harmonic_scaling,
    load_sweep_sample_files,
    load_sweep_samples,
    aggregate_frequency_excitation_iv,
    plot_harmonic_scaling_fit,
    plot_role_harmonic_sweep,
    plot_six_role_harmonic_sweeps,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CommissioningAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.paths: list[Path] = []
        self.addCleanup(self._cleanup_files)

    def test_catalog_filters_completed_rejected_and_diagnostic_records(self) -> None:
        completed = self._write_json("completed.json", self._sweep(completed=True))
        self._write_json("rejected.json", self._sweep(completed=False))
        diagnostic = self._temporary_path(".jsonl")
        diagnostic.write_text(
            json.dumps({"sample_index": 0, "problems": []}) + "\n",
            encoding="utf-8",
        )

        catalog = discover_commissioning_records(PROJECT_ROOT)
        by_path = {record.path: record for record in catalog}
        self.assertEqual(by_path[completed].record_status, "completed")
        self.assertEqual(by_path[diagnostic].record_status, "diagnostic")
        accepted = discover_commissioning_records(
            PROJECT_ROOT,
            record_statuses={"completed"},
            scan_types={"frequency"},
        )
        self.assertIn(completed, [record.path for record in accepted])
        diagnostics = discover_commissioning_records(
            PROJECT_ROOT, record_statuses={"diagnostic"}
        )
        self.assertIn(diagnostic, [record.path for record in diagnostics])

    def test_rejected_sweep_requires_explicit_audit_mode_and_filters_status(self) -> None:
        path = self._write_json(
            "rejected.json", self._sweep(completed=False, xy_lia_raw=8)
        )
        with self.assertRaisesRegex(ValueError, "include_rejected=True"):
            load_sweep_samples(path)

        rows = load_sweep_samples(path, include_rejected=True)
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            [row.role for row in load_sweep_samples(
                path, include_rejected=True, sample_statuses={"clean"}
            )],
            ["xx"],
        )
        unlocked = load_sweep_samples(
            path, include_rejected=True, sample_statuses={"unlocked"}
        )
        self.assertEqual([row.role for row in unlocked], ["xy"])

    def test_frequency_analysis_defaults_to_recorded_actual_frequency(self) -> None:
        payload = self._sweep(completed=True)
        point = payload["points"][0]
        point["target_frequency_hz"] = 5622.80243375
        point["actual_frequency_hz"] = 5622.0
        path = self._write_json("actual_frequency.json", payload)

        rows = load_sweep_samples(path)
        statistics = aggregate_sweep_samples(rows)

        self.assertEqual({row.target_frequency_hz for row in rows}, {5622.80243375})
        self.assertEqual({row.actual_frequency_hz for row in rows}, {5622.0})
        self.assertEqual({item.x_value for item in statistics}, {5622.0})

    def test_combined_sweep_aggregates_one_iv_curve_per_actual_frequency(self) -> None:
        payload = self._sweep(completed=True)
        payload["scan"] = "frequency_excitation"
        payload["measurement_config"] = self._measurement_config_path()
        payload["points"] = []
        for point_index, frequency_hz in enumerate((17.777, 316.1)):
            for excitation_v, amplitude in ((0.004, 1.0), (0.008, 4.0)):
                point = {
                    "point_index": point_index * 2 + int(excitation_v * 1000),
                    "target_frequency_hz": frequency_hz,
                    "actual_frequency_hz": frequency_hz,
                    "source_v_rms": excitation_v,
                    "source_readback_v_rms": excitation_v,
                    "nominal_current_a_rms": None,
                    "samples": [self._sample(xx_amplitude=amplitude)],
                }
                payload["points"].append(point)
        path = self._write_json("combined.json", payload)

        rows = load_sweep_samples(path)
        statistics = aggregate_frequency_excitation_iv(
            rows, role="xx", harmonic=1
        )

        self.assertEqual({item.frequency_hz for item in statistics}, {17.777, 316.1})
        self.assertEqual(len(statistics), 4)
        self.assertEqual({item.current_a_rms for item in statistics}, {0.004 / 100550.0, 0.008 / 100550.0})

    def test_analysis_ignores_unused_output_overload_bit(self) -> None:
        payload = self._sweep(completed=True)
        xy = payload["points"][0]["samples"][0]["lockin_xy"]
        xy["reading"]["overload"] = True
        xy["lia_status"]["raw"] = 4
        xy["lia_status"]["output_overload"] = True
        path = self._write_json("output_overload_only.json", payload)

        rows = load_sweep_samples(path)

        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row.statuses == ("clean",) for row in rows))

    def test_formal_loader_excludes_transition_and_aggregates_samples(self) -> None:
        payload = self._sweep(completed=True)
        payload["points"][0]["transition_status"] = {
            "lockin_xy": self._instrument("xy", 9.0, lia_raw=8)
        }
        second = self._sample(xx_amplitude=3.0, xy_amplitude=0.4)
        second["sample_index"] = 1
        payload["points"][0]["samples"].append(second)
        path = self._write_json("completed.json", payload)

        rows = load_sweep_samples(path)
        self.assertEqual(len(rows), 4)
        xx_statistics = [
            item
            for item in aggregate_sweep_samples(rows)
            if item.role == "xx"
        ]
        self.assertEqual(len(xx_statistics), 1)
        self.assertEqual(xx_statistics[0].mean, 2.0)
        self.assertEqual(xx_statistics[0].count, 2)

        xx_rows = [row for row in rows if row.role == "xx"]
        phase_rows = (
            replace(xx_rows[0], phase_deg=179.0),
            replace(xx_rows[1], phase_deg=-179.0),
        )
        phase = aggregate_sweep_samples(phase_rows, metric="phase_deg")[0]
        self.assertAlmostEqual(abs(phase.mean), 180.0)
        self.assertLess(phase.standard_deviation, 2.0)

    def test_formal_loader_respects_selected_roles_and_preserves_legacy_pairs(self) -> None:
        payload = self._sweep(completed=True)
        payload["points"][0]["samples"][0]["selected_roles"] = ["xy"]
        selected_path = self._write_json("xy-only.json", payload)

        self.assertEqual(
            [row.role for row in load_sweep_samples(selected_path)], ["xy"]
        )
        self.assertEqual(
            [row.role for row in load_sweep_samples(selected_path, roles={"xy"})],
            ["xy"],
        )
        legacy_path = self._write_json("legacy-pair.json", self._sweep(completed=True))
        self.assertEqual(
            [row.role for row in load_sweep_samples(legacy_path)], ["xx", "xy"]
        )

    def test_csv_export_and_injected_browse_open_data_directly(self) -> None:
        source = self._write_json("completed.json", self._sweep(completed=True))
        browsed = browse_and_load_commissioning_file(
            PROJECT_ROOT, chooser=lambda initial: source
        )
        self.assertIsNotNone(browsed)
        selected, payload = browsed
        self.assertEqual(selected, source)
        self.assertIsInstance(payload, dict)

        destination = self._temporary_path(".csv")
        export_commissioning_csv(load_sweep_samples(source), destination)
        text = destination.read_text(encoding="utf-8")
        self.assertIn("record_status", text)
        self.assertIn("clean", text)

    def test_power_shell_utf16_record_is_directly_loadable(self) -> None:
        source = self._temporary_path(".json")
        source.write_text(
            json.dumps(self._sweep(completed=True)),
            encoding="utf-16",
        )

        rows = load_sweep_samples(source)

        self.assertEqual([row.role for row in rows], ["xx", "xy"])

    def test_sine_output_current_uses_readback_and_explicit_path_resistance(self) -> None:
        payload = self._sweep(completed=True)
        payload["points"][0]["source_readback_v_rms"] = 0.0039
        source = self._write_json("completed.json", payload)
        path = ExcitationPathResistance(
            external_series_resistance_ohm=100_000.0,
            sr830_output_resistance_ohm=50.0,
            approximate_device_resistance_ohm=500.0,
        )

        rows = load_sweep_sample_files([source])
        statistics = aggregate_sweep_samples(
            rows,
            x_axis="sine_output_current_a_rms",
            excitation_path=path,
        )

        self.assertEqual(path.total_resistance_ohm, 100_550.0)
        self.assertAlmostEqual(rows[0].sine_output_v_rms, 0.0039)
        self.assertAlmostEqual(
            statistics[0].x_value, 0.0039 / 100_550.0
        )
        with self.assertRaisesRegex(ValueError, "non-negative"):
            ExcitationPathResistance(-1.0, 50.0, 500.0)

    def test_sine_output_current_defaults_to_recorded_sweep_path(self) -> None:
        payload = self._sweep(completed=True)
        payload["measurement_config"] = self._measurement_config_path()
        payload["points"][0]["source_readback_v_rms"] = 0.0039
        source = self._write_json("recorded-path.json", payload)

        rows = load_sweep_sample_files([source])
        statistics = aggregate_sweep_samples(
            rows,
            x_axis="sine_output_current_a_rms",
        )

        self.assertEqual(rows[0].recorded_external_series_resistance_ohm, 100_000.0)
        self.assertEqual(rows[0].recorded_sr830_output_resistance_ohm, 50.0)
        self.assertEqual(rows[0].recorded_approximate_device_resistance_ohm, 500.0)
        self.assertAlmostEqual(statistics[0].x_value, 0.0039 / 100_550.0)

    def test_legacy_current_requires_explicit_path_override(self) -> None:
        payload = self._sweep(completed=True)
        source = self._write_json("legacy.json", payload)
        rows = load_sweep_sample_files([source])
        override = ExcitationPathResistance(100_000.0, 50.0, 500.0)

        with self.assertRaisesRegex(ValueError, "recorded measurement_config"):
            aggregate_sweep_samples(rows, x_axis="sine_output_current_a_rms")

        statistics = aggregate_sweep_samples(
            rows,
            x_axis="sine_output_current_a_rms",
            excitation_path=override,
        )
        self.assertAlmostEqual(statistics[0].x_value, 0.004 / 100_550.0)

    def test_selected_sweep_paths_require_one_recorded_calibration(self) -> None:
        first_payload = self._sweep(completed=True)
        first_payload["measurement_config"] = self._measurement_config_path()
        first = self._write_json("first.json", first_payload)
        second_payload = self._sweep(completed=True)
        second_payload["measurement_config"] = self._measurement_config_path(
            external_series_resistance_ohm=200_000.0
        )
        second = self._write_json("second.json", second_payload)
        legacy = self._write_json("legacy.json", self._sweep(completed=True))
        override = ExcitationPathResistance(300_000.0, 50.0, 500.0)

        resolved = excitation_path_from_sweep_files([first])

        self.assertEqual(resolved.total_resistance_ohm, 100_550.0)
        with self.assertRaisesRegex(ValueError, "different excitation paths"):
            excitation_path_from_sweep_files([first, second])
        with self.assertRaisesRegex(ValueError, "different excitation paths"):
            aggregate_sweep_samples(
                load_sweep_sample_files([first, second]),
                x_axis="sine_output_current_a_rms",
            )
        with self.assertRaisesRegex(ValueError, "no recorded measurement_config"):
            excitation_path_from_sweep_files([legacy])
        self.assertEqual(
            excitation_path_from_sweep_files(
                [first, second], excitation_path_override=override
            ),
            override,
        )

    def test_sweep_loader_resolves_shared_hashed_measurement_profiles(self) -> None:
        directory = PROJECT_ROOT / "tests"
        tag = uuid.uuid4().hex
        profile = {
            "profile_schema_version": 1,
            "test_tag": tag,
            "excitation_path": {
                "series_resistance_ohm": 100_000.0,
                "sr830_output_resistance_ohm": 50.0,
                "approximate_device_resistance_ohm": 500.0,
            },
        }
        canonical = json.dumps(
            profile, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        profile_path = directory / f"measurement-profile-{digest}.json"
        record_path = directory / f"shared-profile-{tag}.json"
        self.addCleanup(profile_path.unlink, missing_ok=True)
        self.addCleanup(record_path.unlink, missing_ok=True)
        profile_path.write_text(
            json.dumps(profile), encoding="utf-8"
        )
        payload = self._sweep(completed=True)
        payload["measurement_profile_ref"] = {
            "id": f"sha256:{digest}",
            "path": f"measurement-profile-{digest}.json",
        }
        record_path.write_text(json.dumps(payload), encoding="utf-8")

        rows = load_sweep_samples(record_path)
        self.assertEqual(rows[0].recorded_external_series_resistance_ohm, 100_000.0)
        self.assertEqual(rows[0].recorded_approximate_device_resistance_ohm, 500.0)
        self.assertEqual(
            excitation_path_from_sweep_files([record_path]),
            ExcitationPathResistance(100_000.0, 50.0, 500.0),
        )
        catalog_paths = {
            record.path
            for record in discover_commissioning_records(
                directory, scan_types={"frequency"}
            )
        }
        self.assertIn(record_path, catalog_paths)
        self.assertNotIn(profile_path, catalog_paths)

        profile["excitation_path"]["series_resistance_ohm"] = 200_000.0
        profile_path.write_text(json.dumps(profile), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "SHA-256 check"):
            load_sweep_samples(record_path)

    def test_role_harmonic_plots_have_voltage_and_phase_axes(self) -> None:
        try:
            import matplotlib.pyplot as plt
        except ImportError:
            self.skipTest("matplotlib is not installed")
        payload = self._sweep(completed=True)
        payload["measurement_config"] = self._measurement_config_path()
        source = self._write_json("completed.json", payload)
        rows = load_sweep_samples(source)

        figure = plot_role_harmonic_sweep(rows, role="xx", harmonic=1)
        self.addCleanup(plt.close, figure)
        self.assertEqual(len(figure.axes), 2)
        self.assertTrue(
            figure.axes[0].get_shared_x_axes().joined(
                figure.axes[0], figure.axes[1]
            )
        )
        self.assertIn("I_RMS = 39.78 nA", figure.axes[0].get_title())
        self.assertEqual(figure.axes[0].get_ylabel(), "Vxx R (V RMS)")
        self.assertEqual(figure.axes[1].get_ylabel(), "Unwrapped phase (°)")

        figures = plot_six_role_harmonic_sweeps(rows)
        self.addCleanup(lambda: [plt.close(item) for item in figures.values()])
        self.assertEqual(set(figures), {("xx", 1), ("xy", 1)})

    def test_role_harmonic_phase_plot_unwraps_and_omits_unqualified_points(self) -> None:
        try:
            import matplotlib.pyplot as plt
        except ImportError:
            self.skipTest("matplotlib is not installed")
        source = self._write_json("completed.json", self._sweep(completed=True))
        base = next(row for row in load_sweep_samples(source) if row.role == "xx")
        rows = (
            replace(
                base,
                target_frequency_hz=10.0,
                actual_frequency_hz=10.0,
                sample_index=0,
                amplitude_v=0.5e-6,
                phase_deg=0.0,
            ),
            replace(
                base,
                target_frequency_hz=10.0,
                actual_frequency_hz=10.0,
                sample_index=1,
                amplitude_v=0.5e-6,
                phase_deg=90.0,
            ),
            replace(
                base,
                target_frequency_hz=100.0,
                actual_frequency_hz=100.0,
                sample_index=0,
                amplitude_v=5e-6,
                phase_deg=179.0,
            ),
            replace(
                base,
                target_frequency_hz=100.0,
                actual_frequency_hz=100.0,
                sample_index=1,
                amplitude_v=5e-6,
                phase_deg=-179.0,
            ),
            replace(
                base,
                target_frequency_hz=1000.0,
                actual_frequency_hz=1000.0,
                sample_index=0,
                amplitude_v=5e-6,
                phase_deg=-178.0,
            ),
            replace(
                base,
                target_frequency_hz=1000.0,
                actual_frequency_hz=1000.0,
                sample_index=1,
                amplitude_v=5e-6,
                phase_deg=-178.0,
            ),
        )
        path = ExcitationPathResistance(100_000.0, 50.0, 500.0)

        figure = plot_role_harmonic_sweep(
            rows,
            role="xx",
            harmonic=1,
            excitation_path=path,
            phase_minimum_amplitude_v=1e-6,
            phase_maximum_standard_deviation_deg=5.0,
        )
        self.addCleanup(plt.close, figure)
        phase_line = next(
            line for line in figure.axes[1].get_lines() if line.get_marker() == "s"
        )
        phase_values = phase_line.get_ydata()

        self.assertTrue(math.isnan(float(phase_values[0])))
        self.assertAlmostEqual(float(phase_values[1]), 180.0)
        self.assertAlmostEqual(float(phase_values[2]), 182.0)
        self.assertEqual(figure.axes[1].get_ylabel(), "Unwrapped phase (°)")

    def test_harmonic_scaling_recovers_expected_order_and_phase(self) -> None:
        path = ExcitationPathResistance(100_000.0, 50.0, 500.0)
        rows = self._scaling_rows(exponent=2.0, phase_slope_deg_per_decade=0.0)

        fit = fit_harmonic_scaling(
            rows,
            role="xy",
            harmonic=2,
            excitation_path=path,
        )

        self.assertEqual(fit.amplitude_verdict, "consistent")
        self.assertEqual(fit.complex_response_verdict, "consistent")
        self.assertAlmostEqual(fit.exponent or 0.0, 2.0, places=6)
        self.assertAlmostEqual(fit.phase_slope_deg_per_decade or 0.0, 0.0, places=6)
        self.assertLessEqual(fit.delta_aicc_fixed_minus_free or 0.0, 2.0)

    def test_harmonic_scaling_plot_uses_right_side_model_legend(self) -> None:
        try:
            import matplotlib.pyplot as plt
        except ImportError:
            self.skipTest("matplotlib is not installed")
        path = ExcitationPathResistance(100_000.0, 50.0, 500.0)
        fit = fit_harmonic_scaling(
            self._scaling_rows(exponent=2.0, phase_slope_deg_per_decade=0.0),
            role="xy",
            harmonic=2,
            excitation_path=path,
        )

        figure = plot_harmonic_scaling_fit(fit)
        self.addCleanup(plt.close, figure)

        axis = figure.axes[0]
        legend = axis.get_legend()
        self.assertIsNotNone(legend)
        assert legend is not None
        anchor = legend.get_bbox_to_anchor().transformed(axis.transAxes.inverted())
        self.assertGreater(anchor.x0, 1.0)
        self.assertFalse(figure.texts)
        self.assertEqual(axis.get_title(), "Vxy h2 harmonic-scaling fits")
        self.assertIn(
            "observed mean ± sample SD",
            [item.get_text() for item in legend.texts],
        )
        legend_text = "\n".join(item.get_text() for item in legend.texts)
        self.assertIn("Log fixed, n=2", legend_text)
        self.assertIn("R(I) =", legend_text)
        self.assertIn("Z(I) =", legend_text)
        self.assertIn("I /", legend_text)
        self.assertIn("rRMSE=", legend_text)
        self.assertIn("AICc=", legend_text)
        legend_title = legend.get_title().get_text()
        self.assertIn("log:", legend_title)
        self.assertIn("scalar:", legend_title)
        self.assertIn("phase/complex:", legend_title)

    def test_harmonic_scaling_plot_can_show_one_method_at_a_time(self) -> None:
        try:
            import matplotlib.pyplot as plt
        except ImportError:
            self.skipTest("matplotlib is not installed")
        path = ExcitationPathResistance(100_000.0, 50.0, 500.0)
        fit = fit_harmonic_scaling(
            self._scaling_rows(exponent=2.0, phase_slope_deg_per_decade=0.0),
            role="xy",
            harmonic=2,
            excitation_path=path,
        )

        expectations = {
            "log": ("Log fixed", "log fixed residual", ("Scalar", "Z(I) =")),
            "scalar": ("Scalar fixed", "scalar R residual", ("Log fixed", "Z(I) =")),
            "complex": ("Complex fixed", "complex residual", ("Log fixed", "R(I) =")),
        }
        for method, (expected, expected_residual, absent) in expectations.items():
            with self.subTest(method=method):
                figure = plot_harmonic_scaling_fit(fit, methods=(method,))
                self.addCleanup(plt.close, figure)
                fit_legend = figure.axes[0].get_legend()
                residual_legend = figure.axes[1].get_legend()
                self.assertIsNotNone(fit_legend)
                self.assertIsNotNone(residual_legend)
                assert fit_legend is not None
                assert residual_legend is not None
                fit_text = "\n".join(item.get_text() for item in fit_legend.texts)
                residual_text = "\n".join(
                    item.get_text() for item in residual_legend.texts
                )
                self.assertIn(expected, fit_text)
                self.assertEqual(residual_text, expected_residual)
                for unexpected in absent:
                    self.assertNotIn(unexpected, fit_text)
                title = fit_legend.get_title().get_text()
                self.assertIn(f"{method}:", title)
                for other in {"log", "scalar", "complex"} - {method}:
                    label = "phase/complex:" if other == "complex" else f"{other}:"
                    self.assertNotIn(label, title)

    def test_harmonic_scaling_plot_rejects_invalid_method_selection(self) -> None:
        try:
            import matplotlib.pyplot as plt
        except ImportError:
            self.skipTest("matplotlib is not installed")
        path = ExcitationPathResistance(100_000.0, 50.0, 500.0)
        fit = fit_harmonic_scaling(
            self._scaling_rows(exponent=2.0, phase_slope_deg_per_decade=0.0),
            role="xy",
            harmonic=2,
            excitation_path=path,
        )

        with self.assertRaisesRegex(ValueError, "At least one"):
            plot_harmonic_scaling_fit(fit, methods=())
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            plot_harmonic_scaling_fit(fit, methods=("linear",))
        with self.assertRaisesRegex(ValueError, "must not contain duplicates"):
            plot_harmonic_scaling_fit(fit, methods=("scalar", "scalar"))

    def test_harmonic_scaling_legend_preserves_complex_background_terms(self) -> None:
        try:
            import matplotlib.pyplot as plt
        except ImportError:
            self.skipTest("matplotlib is not installed")
        path = ExcitationPathResistance(100_000.0, 50.0, 500.0)
        fit = fit_harmonic_scaling(
            self._complex_scaling_rows(
                exponent=2.0,
                background_x_v=1.0e-6,
                background_y_v=-0.5e-6,
                response_x_v_at_reference_current=2.0e-8,
                response_y_v_at_reference_current=1.0e-8,
            ),
            role="xy",
            harmonic=2,
            excitation_path=path,
        )

        figure = plot_harmonic_scaling_fit(fit)
        self.addCleanup(plt.close, figure)

        legend = figure.axes[0].get_legend()
        self.assertIsNotNone(legend)
        assert legend is not None
        legend_text = "\n".join(item.get_text() for item in legend.texts)
        self.assertIn("Z(I) = (", legend_text)
        self.assertIn("− i", legend_text)
        self.assertIn("(I /", legend_text)

    def test_harmonic_scaling_rejects_a_distinct_exponent(self) -> None:
        path = ExcitationPathResistance(100_000.0, 50.0, 500.0)
        rows = self._scaling_rows(exponent=1.25, phase_slope_deg_per_decade=0.0)

        fit = fit_harmonic_scaling(
            rows,
            role="xy",
            harmonic=2,
            excitation_path=path,
        )

        self.assertEqual(fit.amplitude_verdict, "inconsistent")
        self.assertFalse(
            (fit.exponent_ci_low or 0.0) <= 2.0 <= (fit.exponent_ci_high or 0.0)
        )
        self.assertGreater(fit.delta_aicc_fixed_minus_free or 0.0, 6.0)

    def test_harmonic_scaling_separates_amplitude_and_complex_phase_verdicts(self) -> None:
        path = ExcitationPathResistance(100_000.0, 50.0, 500.0)
        rows = self._scaling_rows(exponent=2.0, phase_slope_deg_per_decade=20.0)

        fit = fit_harmonic_scaling(
            rows,
            role="xy",
            harmonic=2,
            excitation_path=path,
        )

        self.assertEqual(fit.amplitude_verdict, "consistent")
        self.assertEqual(fit.complex_response_verdict, "inconsistent")
        self.assertAlmostEqual(fit.phase_slope_deg_per_decade or 0.0, 20.0, places=6)

    def test_scalar_fit_ignores_phase_and_recovers_quadratic_magnitude(self) -> None:
        path = ExcitationPathResistance(100_000.0, 50.0, 500.0)
        rows = self._scaling_rows(exponent=2.0, phase_slope_deg_per_decade=37.0)

        fit = fit_harmonic_scaling(
            rows,
            role="xy",
            harmonic=2,
            excitation_path=path,
        )

        self.assertTrue(fit.scalar_phase_ignored)
        self.assertEqual(fit.scalar_power_law_verdict, "consistent")
        self.assertAlmostEqual(fit.scalar_exponent or 0.0, 2.0, places=6)
        self.assertEqual(fit.scalar_selected_model, "scalar_no_offset_fixed_order")
        self.assertEqual(fit.scalar_background_verdict, "not_needed")
        self.assertEqual(len(fit.scalar_models), 4)
        json.dumps(fit.as_dict())

    def test_scalar_fit_rejects_distinct_exponent_even_with_phase_rotation(self) -> None:
        path = ExcitationPathResistance(100_000.0, 50.0, 500.0)
        rows = self._scaling_rows(exponent=1.25, phase_slope_deg_per_decade=45.0)

        fit = fit_harmonic_scaling(
            rows,
            role="xy",
            harmonic=2,
            excitation_path=path,
        )

        self.assertEqual(fit.scalar_power_law_verdict, "inconsistent")
        self.assertFalse(
            (fit.scalar_exponent_ci_low or 0.0)
            <= 2.0
            <= (fit.scalar_exponent_ci_high or 0.0)
        )
        self.assertGreater(fit.scalar_delta_aicc_fixed_minus_free or 0.0, 6.0)

    def test_scalar_fit_recovers_nonnegative_amplitude_background(self) -> None:
        path = ExcitationPathResistance(100_000.0, 50.0, 500.0)
        base_rows = self._scaling_rows(exponent=2.0, phase_slope_deg_per_decade=29.0)
        currents = tuple(row.nominal_current_a_rms or 0.0 for row in base_rows)
        current_reference = math.sqrt(min(currents) * max(currents))
        rows = tuple(
            replace(
                row,
                amplitude_v=3.0e-6
                + 1.0e-6 * (row.nominal_current_a_rms / current_reference) ** 2,
            )
            for row in base_rows
        )

        fit = fit_harmonic_scaling(
            rows,
            role="xy",
            harmonic=2,
            excitation_path=path,
        )

        self.assertEqual(fit.scalar_selected_model, "scalar_offset_fixed_order")
        selected = next(
            model for model in fit.scalar_models
            if model.name == fit.scalar_selected_model
        )
        self.assertAlmostEqual(selected.background_v, 3.0e-6, places=12)
        self.assertAlmostEqual(selected.response_v_at_reference_current, 1.0e-6, places=12)
        self.assertEqual(fit.scalar_power_law_verdict, "consistent")

    def test_harmonic_scaling_reports_insufficient_range(self) -> None:
        path = ExcitationPathResistance(100_000.0, 50.0, 500.0)
        rows = self._scaling_rows(exponent=2.0, phase_slope_deg_per_decade=0.0)[:6]
        rules = HarmonicScalingRules(minimum_current_decades=1.0)

        fit = fit_harmonic_scaling(
            rows,
            role="xy",
            harmonic=2,
            excitation_path=path,
            rules=rules,
        )

        self.assertEqual(fit.amplitude_verdict, "insufficient_data")
        self.assertTrue(any("current span" in reason for reason in fit.reasons))

    def test_complex_background_recovers_quadratic_response(self) -> None:
        path = ExcitationPathResistance(100_000.0, 50.0, 500.0)
        rows = self._complex_scaling_rows(
            exponent=2.0,
            background_x_v=1.0e-6,
            background_y_v=-0.5e-6,
            response_x_v_at_reference_current=2.0e-8,
            response_y_v_at_reference_current=1.0e-8,
        )

        fit = fit_harmonic_scaling(
            rows,
            role="xy",
            harmonic=2,
            excitation_path=path,
        )

        self.assertEqual(fit.complex_selected_model, "offset_fixed_order")
        self.assertEqual(fit.complex_background_verdict, "preferred")
        self.assertEqual(fit.complex_power_law_verdict, "consistent")
        self.assertAlmostEqual(fit.complex_exponent or 0.0, 2.0, places=5)
        self.assertGreater(fit.complex_background_delta_aicc or 0.0, 6.0)
        selected = next(
            model for model in fit.complex_models
            if model.name == fit.complex_selected_model
        )
        self.assertAlmostEqual(selected.background_x_v, 1.0e-6, places=12)
        self.assertAlmostEqual(selected.background_y_v, -0.5e-6, places=12)
        json.dumps(fit.as_dict())

    def test_complex_background_rejects_wrong_exponent(self) -> None:
        path = ExcitationPathResistance(100_000.0, 50.0, 500.0)
        rows = self._complex_scaling_rows(
            exponent=1.25,
            background_x_v=1.0e-6,
            background_y_v=-0.5e-6,
            response_x_v_at_reference_current=2.0e-8,
            response_y_v_at_reference_current=1.0e-8,
        )

        fit = fit_harmonic_scaling(
            rows,
            role="xy",
            harmonic=2,
            excitation_path=path,
        )

        self.assertEqual(fit.complex_selected_model, "offset_fixed_order")
        self.assertEqual(fit.complex_power_law_verdict, "inconsistent")
        self.assertFalse(
            (fit.complex_exponent_ci_low or 0.0)
            <= 2.0
            <= (fit.complex_exponent_ci_high or 0.0)
        )
        self.assertGreater(fit.complex_delta_aicc_fixed_minus_free or 0.0, 6.0)

    def test_complex_background_mode_can_force_no_offset(self) -> None:
        path = ExcitationPathResistance(100_000.0, 50.0, 500.0)
        rows = self._complex_scaling_rows(
            exponent=2.0,
            background_x_v=1.0e-6,
            background_y_v=-0.5e-6,
            response_x_v_at_reference_current=2.0e-8,
            response_y_v_at_reference_current=1.0e-8,
        )

        fit = fit_harmonic_scaling(
            rows,
            role="xy",
            harmonic=2,
            excitation_path=path,
            rules=HarmonicScalingRules(complex_background_mode="none"),
        )

        self.assertEqual(fit.complex_selected_model, "no_offset_fixed_order")
        self.assertEqual(fit.complex_background_verdict, "preferred")

    def test_notebook_is_valid_json_and_all_code_cells_compile(self) -> None:
        path = PROJECT_ROOT / "notebooks" / "sr830_commissioning_sweeps.ipynb"
        notebook = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(notebook["nbformat"], 4)
        code_cells = [
            cell for cell in notebook["cells"] if cell["cell_type"] == "code"
        ]
        self.assertGreaterEqual(len(code_cells), 6)
        for index, cell in enumerate(code_cells):
            compile("".join(cell["source"]), f"notebook-cell-{index}", "exec")

    def test_notebook_exposes_independent_record_and_point_selectors(self) -> None:
        path = PROJECT_ROOT / "notebooks" / "sr830_commissioning_sweeps.ipynb"
        notebook = json.loads(path.read_text(encoding="utf-8"))
        code = "\n".join(
            "".join(cell["source"])
            for cell in notebook["cells"]
            if cell["cell_type"] == "code"
        )

        self.assertIn("widgets.Button", code)
        self.assertIn("completed_only_widget", code)
        self.assertIn("refresh_records_button.on_click", code)
        self.assertIn("load_selected_records_button.on_click", code)
        self.assertIn("frequency_record_widget", code)
        self.assertIn("excitation_record_widget", code)
        self.assertIn("record_categories_widget.selected_index = None", code)
        self.assertIn("repeatability_options_widget.selected_index = None", code)
        self.assertNotIn("condition_message", code)
        self.assertNotIn("_condition_summary", code)
        self.assertIn("paired differences use only shared requested coordinates", code)
        self.assertIn("frequency_excluded_points_widget", code)
        self.assertIn("excitation_excluded_points_widget", code)
        self.assertIn("apply_point_exclusions_button", code)
        self.assertIn("def _load_selected_formal_samples", code)
        self.assertIn("def _load_selected_formal_samples():\n    _sync_filters()", code)
        self.assertIn("loaded = _load_selected_formal_samples()", code)
        self.assertIn("selection_manifest.json", code)
        self.assertIn("'phase_display': {", code)
        self.assertIn(
            "'PHASE_MINIMUM_AMPLITUDE_V': PHASE_MINIMUM_AMPLITUDE_V", code
        )
        self.assertIn(
            "'PHASE_MAXIMUM_STANDARD_DEVIATION_DEG': "
            "PHASE_MAXIMUM_STANDARD_DEVIATION_DEG",
            code,
        )
        self.assertIn("SCALING_RULES = HarmonicScalingRules", code)
        self.assertIn(
            'SCALING_PLOT_METHODS = ("log", "scalar", "complex")', code
        )
        self.assertIn('# SCALING_PLOT_METHODS = ("scalar",)', code)
        self.assertIn('# SCALING_PLOT_METHODS = ("log",)', code)
        self.assertIn('# SCALING_PLOT_METHODS = ("complex",)', code)
        self.assertIn("scalar_background_mode='auto'", code)
        self.assertIn("minimum_current_decades=1.0", code)
        self.assertIn("complex_background_mode='auto'", code)
        self.assertIn("complex_free_exponent_min=0.05", code)
        self.assertIn("fit_harmonic_scalings", code)
        self.assertIn("plot_harmonic_scaling_fit", code)
        self.assertIn("methods=SCALING_PLOT_METHODS", code)
        self.assertIn("export_publication_figure_set", code)
        self.assertNotIn("dpi=200", code)
        self.assertIn("'harmonic_scaling_rules': asdict(SCALING_RULES)", code)
        self.assertIn(
            "'harmonic_scaling_plot_methods': list(SCALING_PLOT_METHODS)", code
        )
        self.assertIn("'harmonic_scaling_results':", code)
        self.assertIn("if frequency_rows", code)
        self.assertIn("if excitation_rows", code)
        self.assertNotIn("display([result.as_dict()", code)
        self.assertNotIn("Fitted formulas", code)
        self.assertNotIn("prints all numerical verdicts", code)
        self.assertNotIn("current_calibration", code)
        self.assertNotIn("catalog = discover_commissioning_records", code)
        self.assertNotIn("browse_and_load_commissioning_file", code)
        self.assertNotIn("A completed frequency record is missing", code)
        self.assertNotIn("A completed excitation record is missing", code)
        self.assertIn("excitation_path_from_sweep_files", code)
        self.assertIn("EXCITATION_PATH_OVERRIDE", code)
        self.assertNotIn("EXTERNAL_SERIES_RESISTANCE_OHM", code)

    def test_notebook_exclusions_are_global_coordinates_and_allow_unequal_runs(self) -> None:
        first_payload = self._sweep(completed=True)
        first_payload["scan"] = "excitation"
        first_payload["measurement_config"] = self._measurement_config_path()
        first_payload["points"][0]["point_index"] = 0
        second_point = {
            **first_payload["points"][0],
            "point_index": 1,
            "source_v_rms": 0.008,
        }
        first_payload["points"].append(second_point)
        first = self._write_json("first.json", first_payload)

        second_payload = self._sweep(completed=True)
        second_payload["scan"] = "excitation"
        second_payload["measurement_config"] = self._measurement_config_path()
        second = self._write_json("second.json", second_payload)

        scope = self._notebook_selector_scope(first.parent)
        scope["excitation_record_widget"].value = (str(first), str(second))
        scope["_load_selected_records"](None)

        self.assertEqual(len(scope["excitation_rows"]), 6)
        options = scope["excitation_excluded_points_widget"].options
        self.assertEqual([value for _, value in options], [0.004, 0.008])
        labels = [label for label, _ in options]
        self.assertTrue(all("point(s) #" in label for label in labels))
        self.assertTrue(all("first.json" not in label and "second.json" not in label for label in labels))
        self.assertEqual(scope["excitation_record_widget"].title, "Excitation")
        self.assertEqual(scope["record_categories_widget"].selected_index, None)

        scope["excitation_excluded_points_widget"].value = (0.004,)
        scope["_apply_point_exclusions"](None)
        self.assertEqual(len(scope["excitation_rows"]), 2)
        self.assertEqual({row.source_path for row in scope["excitation_rows"]}, {str(first)})
        self.assertEqual({row.source_v_rms for row in scope["excitation_rows"]}, {0.008})

    def test_notebook_load_button_populates_selected_point_options(self) -> None:
        payload = self._sweep(completed=True)
        payload["scan"] = "excitation"
        payload["measurement_config"] = self._measurement_config_path()
        source = self._write_json("excitation.json", payload)
        scope = self._notebook_selector_scope(source.parent)
        scope["excitation_record_widget"].value = (str(source),)
        scope["_load_selected_records"](None)

        self.assertEqual(len(scope["frequency_excluded_points_widget"].options), 0)
        self.assertEqual(len(scope["excitation_excluded_points_widget"].options), 1)
        self.assertEqual(len(scope["frequency_rows"]), 0)
        self.assertEqual(len(scope["excitation_rows"]), 2)

    def _notebook_selector_scope(self, directory: Path) -> dict[str, object]:
        notebook = json.loads(
            (PROJECT_ROOT / "notebooks" / "sr830_commissioning_sweeps.ipynb").read_text(
                encoding="utf-8"
            )
        )
        cells = notebook["cells"]
        widgets = _fake_notebook_widgets()
        matplotlib = types.ModuleType("matplotlib")
        pyplot = types.ModuleType("matplotlib.pyplot")
        matplotlib.pyplot = pyplot
        ipython = types.ModuleType("IPython")
        display_module = types.ModuleType("IPython.display")
        display_module.display = lambda _: None
        ipython.display = display_module
        scope: dict[str, object] = {}

        with patch.dict(
            sys.modules,
            {
                "ipywidgets": widgets,
                "matplotlib": matplotlib,
                "matplotlib.pyplot": pyplot,
                "IPython": ipython,
                "IPython.display": display_module,
            },
        ):
            exec("".join(cells[1]["source"]), scope)
            scope["DATA_DIRECTORY"] = directory
            selector_cell = next(
                cell
                for cell in cells
                if "def _load_selected_formal_samples" in "".join(cell["source"])
            )
            exec("".join(selector_cell["source"]), scope)
        return scope

    def test_notebook_record_checkboxes_preserve_selection_on_refresh(self) -> None:
        payload = self._sweep(completed=True)
        payload["scan"] = "excitation"
        payload["measurement_config"] = self._measurement_config_path()
        first = self._write_json("first.json", payload)
        second = self._write_json("second.json", payload)
        scope = self._notebook_selector_scope(first.parent)
        selector = scope["excitation_record_widget"]
        self.assertEqual(selector.value, ())
        selector._checkboxes[str(first)].value = True
        selector._checkboxes[str(second)].value = True
        scope["_refresh_records"]()
        self.assertEqual(set(selector.value), {str(first), str(second)})
        scope["_load_selected_records"](None)
        self.assertEqual(
            {row.source_path for row in scope["excitation_rows"]},
            {str(first), str(second)},
        )
        # A coordinate exclusion applies to every selected source file.
        options = scope["excitation_excluded_points_widget"].options
        self.assertTrue(all(str(first) not in label and str(second) not in label for label, _ in options))
        key = options[0][1]
        scope["excitation_excluded_points_widget"].value = (key,)
        scope["_apply_point_exclusions"](None)
        self.assertEqual(scope["excitation_rows"], ())
        selector._checkboxes[str(first)].value = False
        scope["_load_selected_records"](None)
        self.assertEqual(selector.value, (str(second),))
        self.assertEqual(scope["excitation_excluded_points_widget"].value, ())
        selector.value = ()
        scope["_load_selected_records"](None)
        self.assertEqual(scope["excitation_rows"], ())
        self.assertEqual(scope["excitation_excluded_points_widget"].options, ())

    def test_notebook_record_labels_show_formal_count_and_recorded_channels(self) -> None:
        for scan_type, widget_name in (
            ("frequency", "frequency_record_widget"),
            ("excitation", "excitation_record_widget"),
            ("frequency_excitation", "combined_record_widget"),
        ):
            payload = self._sweep(completed=True)
            payload["scan"] = scan_type
            first = payload["points"][0]["samples"][0]
            first["lockin_xy"]["reading"]["harmonic"] = 2
            second = self._sample()
            second["sample_index"] = 1
            second["selected_roles"] = ["xx"]
            second["lockin_xx"]["reading"]["harmonic"] = 2
            second.pop("lockin_xy")
            payload["points"][0]["samples"].append(second)
            path = self._write_json(f"{scan_type}.json", payload)
            scope = self._notebook_selector_scope(path.parent)
            label, selected_path = scope[widget_name].options[0]
            self.assertEqual(selected_path, str(path))
            self.assertEqual(
                label,
                f"{path.name} | 2 formal samples | XX h1/h2, XY h2",
            )

    def test_notebook_combined_exclusions_remove_requested_rows_and_columns(self) -> None:
        payload = self._sweep(completed=True)
        payload["scan"] = "frequency_excitation"
        payload["measurement_config"] = self._measurement_config_path()
        payload["points"] = [
            {
                "point_index": index,
                "target_frequency_hz": frequency,
                "actual_frequency_hz": frequency + 0.0001,
                "source_v_rms": excitation,
                "source_readback_v_rms": excitation + 0.000001,
                "samples": [self._sample()],
            }
            for index, (frequency, excitation) in enumerate(
                (frequency, excitation)
                for frequency in (17.777, 37.123)
                for excitation in (0.004, 0.008, 0.012)
            )
        ]
        first = self._write_json("combined-first.json", payload)
        second = self._write_json("combined-second.json", payload)
        scope = self._notebook_selector_scope(first.parent)
        scope["combined_record_widget"].value = (str(first), str(second))
        scope["_load_selected_records"](None)
        original = scope["combined_loaded_rows"]
        self.assertEqual(len(original), 24)
        frequencies = scope["combined_excluded_frequencies_widget"]
        excitations = scope["combined_excluded_excitations_widget"]
        self.assertEqual([value for _, value in frequencies.options], [17.777, 37.123])
        self.assertEqual([value for _, value in excitations.options], [0.004, 0.008, 0.012])
        for frequency_values, excitation_values, expected_count in (
            ((17.777,), (), 12),
            ((), (0.008,), 16),
            ((17.777,), (0.008,), 8),
            ((17.777, 37.123), (), 0),
            ((), (), 24),
        ):
            with self.subTest(frequencies=frequency_values, excitations=excitation_values):
                frequencies.value = frequency_values
                excitations.value = excitation_values
                scope["_apply_point_exclusions"](None)
                rows = scope["combined_rows"]
                self.assertEqual(len(rows), expected_count)
                self.assertTrue(all(row.target_frequency_hz not in frequency_values for row in rows))
                self.assertTrue(all(row.source_v_rms not in excitation_values for row in rows))
                self.assertEqual(scope["combined_loaded_rows"], original)
        frequencies.value = (17.777,)
        scope["_apply_point_exclusions"](None)
        scope["combined_record_widget"].value = (str(second),)
        scope["_load_selected_records"](None)
        self.assertEqual(frequencies.value, ())
        self.assertEqual(len(scope["combined_rows"]), 12)

    def test_repeatability_keeps_sources_separate_and_uses_actual_x(self):
        path = self._write_json("base.json", self._sweep(completed=True))
        rows = load_sweep_samples(path)
        second = tuple(replace(row, source_path="second.json",
                               actual_frequency_hz=row.actual_frequency_hz + 0.0001,
                               amplitude_v=row.amplitude_v + 1e-6) for row in rows)
        stats = aggregate_sweep_repeatability(rows + second, role="xx", harmonic=1)
        self.assertEqual(len(stats), 2)
        self.assertEqual(stats[0].coordinates, stats[1].coordinates)
        self.assertNotEqual(stats[0].source_path, stats[1].source_path)
        self.assertAlmostEqual(abs(stats[0].mean - stats[1].mean), 1e-6)
        self.assertAlmostEqual(abs(stats[0].x_value - stats[1].x_value), .0001)

    def test_repeatability_phase_uses_circular_statistics(self):
        path = self._write_json("phase.json", self._sweep(completed=True))
        row = next(r for r in load_sweep_samples(path) if r.role == "xx")
        rows = (replace(row, phase_deg=179.0), replace(row, phase_deg=-179.0))
        stats = aggregate_sweep_repeatability(rows, role="xx", harmonic=1, metric="phase_deg")
        self.assertEqual(len(stats), 1)
        self.assertAlmostEqual(abs(stats[0].mean), 180.0)
        self.assertLess(stats[0].standard_deviation, 2.0)

    def test_repeatability_does_not_merge_distinct_requested_points(self):
        path = self._write_json("requested.json", self._sweep(completed=True))
        row = next(r for r in load_sweep_samples(path) if r.role == "xx")
        stats = aggregate_sweep_repeatability(
            (row, replace(row, target_frequency_hz=row.target_frequency_hz + 1e-8)),
            role="xx", harmonic=1)
        self.assertEqual(len(stats), 2)


    def _write_json(self, _name: str, payload: dict[str, object]) -> Path:
        path = self._temporary_path(".json")
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _temporary_path(self, suffix: str) -> Path:
        temporary = tempfile.NamedTemporaryFile(
            dir=PROJECT_ROOT,
            prefix="commissioning_analysis_",
            suffix=suffix,
            delete=False,
        )
        temporary.close()
        path = Path(temporary.name)
        self.paths.append(path)
        return path

    def _cleanup_files(self) -> None:
        for path in self.paths:
            if path.exists():
                path.unlink()

    def _scaling_rows(
        self,
        *,
        exponent: float,
        phase_slope_deg_per_decade: float,
    ) -> tuple[object, ...]:
        base = next(
            row for row in load_sweep_samples(
                self._write_json("scaling-base.json", self._sweep(completed=True))
            )
            if row.role == "xy"
        )
        path_resistance = 100_550.0
        currents = tuple(1e-8 * (10.0 ** (index / 3.0)) for index in range(10))
        rows = []
        for point_index, current in enumerate(currents):
            amplitude = 1.0e8 * current**exponent
            phase = phase_slope_deg_per_decade * math.log10(current / currents[0])
            x_value = amplitude * math.cos(math.radians(phase))
            y_value = amplitude * math.sin(math.radians(phase))
            for sample_index in range(3):
                rows.append(
                    replace(
                        base,
                        scan_type="excitation",
                        point_index=point_index,
                        sample_index=sample_index,
                        source_v_rms=current * path_resistance,
                        sine_output_v_rms=current * path_resistance,
                        nominal_current_a_rms=current,
                        harmonic=2,
                        amplitude_v=amplitude,
                        x_v=x_value,
                        y_v=y_value,
                        phase_deg=phase,
                    )
                )
        return tuple(rows)

    def _complex_scaling_rows(
        self,
        *,
        exponent: float,
        background_x_v: float,
        background_y_v: float,
        response_x_v_at_reference_current: float,
        response_y_v_at_reference_current: float,
    ) -> tuple[object, ...]:
        base = next(
            row for row in load_sweep_samples(
                self._write_json("complex-scaling-base.json", self._sweep(completed=True))
            )
            if row.role == "xy"
        )
        path_resistance = 100_550.0
        currents = tuple(1e-8 * (10.0 ** (index / 3.0)) for index in range(10))
        current_reference = math.sqrt(currents[0] * currents[-1])
        rows = []
        for point_index, current in enumerate(currents):
            scale = (current / current_reference) ** exponent
            x_value = background_x_v + response_x_v_at_reference_current * scale
            y_value = background_y_v + response_y_v_at_reference_current * scale
            amplitude = math.hypot(x_value, y_value)
            phase = math.degrees(math.atan2(y_value, x_value))
            for sample_index in range(3):
                rows.append(
                    replace(
                        base,
                        scan_type="excitation",
                        point_index=point_index,
                        sample_index=sample_index,
                        source_v_rms=current * path_resistance,
                        sine_output_v_rms=current * path_resistance,
                        nominal_current_a_rms=current,
                        harmonic=2,
                        amplitude_v=amplitude,
                        x_v=x_value,
                        y_v=y_value,
                        phase_deg=phase,
                    )
                )
        return tuple(rows)

    def _sweep(
        self, *, completed: bool, xy_lia_raw: int = 0
    ) -> dict[str, object]:
        return {
            "scan": "frequency",
            "completed": completed,
            "error": None if completed else "injected rejection",
            "points": [
                {
                    "point_index": 0,
                    "target_frequency_hz": 17.777,
                    "source_v_rms": 0.004,
                    "nominal_current_a_rms": None,
                    "samples": [self._sample(xy_lia_raw=xy_lia_raw)],
                }
            ],
        }

    @staticmethod
    def _measurement_config_path(
        *, external_series_resistance_ohm: float = 100_000.0
    ) -> dict[str, object]:
        return {
            "excitation_path": {
                "series_resistance_ohm": external_series_resistance_ohm,
                "sr830_output_resistance_ohm": 50.0,
                "approximate_device_resistance_ohm": 500.0,
            }
        }

    def _sample(
        self,
        *,
        xx_amplitude: float = 1.0,
        xy_amplitude: float = 0.2,
        xy_lia_raw: int = 0,
    ) -> dict[str, object]:
        return {
            "sample_index": 0,
            "lockin_xx": self._instrument("xx", xx_amplitude),
            "lockin_xy": self._instrument("xy", xy_amplitude, lia_raw=xy_lia_raw),
            "problems": [],
        }

    @staticmethod
    def _instrument(
        role: str, amplitude: float, *, lia_raw: int = 0
    ) -> dict[str, object]:
        unlocked = bool(lia_raw & 8)
        return {
            "reading": {
                "role": role,
                "harmonic": 1,
                "x_v": amplitude,
                "y_v": 0.0,
                "amplitude_v": amplitude,
                "phase_deg": 0.0,
                "frequency_hz": 17.777,
                "locked": not unlocked,
                "overload": False,
            },
            "lia_status": {
                "raw": lia_raw,
                "input_or_reserve_overload": False,
                "filter_overload": False,
                "output_overload": False,
                "reference_unlocked": unlocked,
                "frequency_range_changed": False,
                "time_constant_changed": False,
                "triggered": False,
            },
            "error_status": 0,
        }


def _fake_notebook_widgets() -> types.ModuleType:
    class Widget:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.children = args[0] if args else ()
            self.options = kwargs.get("options", ())
            self.value = kwargs.get("value", ())

        def on_click(self, callback: object) -> None:
            self.callback = callback

        def set_title(self, index: int, title: str) -> None:
            titles = list(getattr(self, "titles", ()))
            while len(titles) <= index:
                titles.append("")
            titles[index] = title
            self.titles = tuple(titles)

    widgets = types.ModuleType("ipywidgets")
    widgets.Checkbox = Widget
    widgets.SelectMultiple = Widget
    widgets.Button = Widget
    widgets.Dropdown = Widget
    widgets.HTML = Widget
    widgets.HBox = Widget
    widgets.VBox = Widget
    widgets.Accordion = Widget
    widgets.Layout = lambda **kwargs: kwargs
    return widgets


if __name__ == "__main__":
    unittest.main()
