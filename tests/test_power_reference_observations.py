"""Power references retain archive evidence through loading and statistics."""
import copy
from pathlib import Path
import tempfile
import unittest

from attodry_control.analysis_observations import channel_quality, repeat_statistics
from attodry_control.combination_analysis import load_combination_rows
from attodry_control.combination_scan import (
    AxisPoint, CombinationPlan, ScanAxis, run_simulated_combination,
)
from attodry_control.combination_store import CombinationStore


class PowerReferenceArchiveTests(unittest.TestCase):
    def test_loader_retains_planned_grid_once_per_run_without_changing_database(self):
        optical = ScanAxis("optical", tuple(AxisPoint({
            "optical_current_a": .02 + i * .001,
            "optical_wavelength_nm": 730., "optical_target_power_w": p,
        }) for i, p in enumerate((1e-6, 6e-6))))
        lockin = ScanAxis("lockin", (AxisPoint({
            "lockin_excitation_v_rms": .004, "lockin_frequency_hz": 17.,
        }),))
        plan = CombinationPlan((optical, lockin))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "archive.sqlite"
            with CombinationStore(path) as store:
                run_simulated_combination(plan, store, "power-grid")
            before = path.read_bytes()
            rows = load_combination_rows(path)
            self.assertEqual(len(rows), 2)
            evidence = rows[0]["analysis_context"]["power_reference_plan"]
            self.assertEqual(evidence["loop_order"], ["optical", "lockin"])
            self.assertEqual([a["module"] for a in evidence["axes"]], ["optical"])
            self.assertEqual([p["values"]["optical_target_power_w"]
                              for p in evidence["axes"][0]["points"]], [1e-6, 6e-6])
            self.assertIs(rows[0]["analysis_context"], rows[1]["analysis_context"])
            self.assertEqual(path.read_bytes(), before)


class PowerReferenceObservationTests(unittest.TestCase):
    def rows(self):
        from tests.test_optical_power_reference import make_rows
        from attodry_control.optical_power_reference import subtract_power_reference
        return subtract_power_reference(make_rows(), mode="power_lowest")

    def test_quality_uses_power_evidence_separately_from_dark(self):
        rows = self.rows()
        row = rows[-1]
        column = "measured.lockin_xx_h1_power_delta_x_v"
        self.assertEqual(channel_quality(row, column), ("clear", ()))
        bad = copy.deepcopy(row)
        bad["status.lockin"]["power_reference"]["channels"]["xx:h1"]["valid_for_analysis"] = False
        self.assertIn("invalid_power_reference", channel_quality(bad, column)[1])
        del bad["status.lockin"]["power_reference"]
        self.assertEqual(channel_quality(bad, column)[0], "unknown")

    def test_repeat_statistics_routes_to_power_covariance(self):
        from attodry_control.optical_power_reference import power_reference_statistics
        rows = self.rows()
        column = "measured.lockin_xx_h1_power_delta_x_v"
        target = [r for r in rows if r["requested.optical_target_power_w"] == 6e-6]
        expected = power_reference_statistics(target, column)
        point, = repeat_statistics(target, x="requested.optical_target_power_w", y=column, mode="mean_sem")
        self.assertEqual(point["y"], expected["mean"])
        self.assertEqual(point["sem"], expected["sem"])
        self.assertEqual(point["power_reference_mean_variance"], expected["reference_mean_variance"])
        self.assertIsNone(point["dark_mean_variance"])

    def test_self_reference_summary_cancels_shared_error(self):
        rows = [r for r in self.rows() if r["requested.optical_target_power_w"] == 1e-6]
        for metric in ("x_v", "y_v", "amplitude_v"):
            column = "measured.lockin_xx_h1_power_delta_" + metric
            point, = repeat_statistics(rows, x="requested.optical_target_power_w", y=column, mode="mean_sem")
            self.assertEqual(point["y"], 0.)
            self.assertEqual(point["sem"], 0.)

    def test_mixed_formal_reference_contexts_yield_unknown_summary_not_crash(self):
        from tests.test_optical_power_reference import make_rows
        from attodry_control.optical_power_reference import subtract_power_reference
        rows = make_rows(schema="photonics")
        for row in rows:
            if row["sample_index"] == 1:
                for stage in ("settings_before", "settings_after"):
                    row["status.lockin"]["samples"][0][stage]["xx"]["native_settings"]["phase_shift_deg"] = 1.
        corrected = subtract_power_reference(rows)
        target = [r for r in corrected if r["requested.optical_target_power_w"] == 6e-6]
        point, = repeat_statistics(target, x="requested.optical_target_power_w",
                                   y="measured.lockin_xx_h1_power_delta_x_v", mode="mean_sem")
        self.assertIsNone(point["y"])
        self.assertIsNone(point["sem"])
        self.assertTrue(point["uncertainty_reason"])


if __name__ == "__main__":
    unittest.main()
