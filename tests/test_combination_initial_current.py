"""Initial-current strategies through the full coordinator with fake resources."""
import tomllib
import unittest

import test_combination_photonics as fixture
from attodry_control.combination_analysis import load_combination_rows
from attodry_control.combination_hardware import load_hardware_combination
from attodry_control.combination_launch import launch_summary
from attodry_control.combination_terminal import launch_text


class CombinationInitialCurrentTests(unittest.TestCase):
    def make(self, mode, rows):
        case = fixture.PhotonicsCombinationTests(methodName="runTest")
        case.setUp()
        self.addCleanup(case.doCleanups)
        doc = tomllib.loads(case.base)
        first = doc["nkt_run"]["points"][0]
        doc["nkt_run"]["points"] = [first]
        if rows == 2:
            doc["nkt_run"]["points"].append({**first, "wavelength_nm": 650.0})
        feedback = doc["power_feedback"]
        feedback.update(target_mapping="cartesian", initial_current_mode=mode)
        if mode == "per_target":
            feedback["initial_source_levels_pct"] = [9.0, 19.0, 25.0]
        elif mode == "grid":
            feedback["initial_source_levels_pct"] = [[9.0, 19.0, 25.0], [10.0, 18.0, 24.0]]
        case.write_config(source=fixture.toml_document(doc))
        return case

    def test_full_scans_keep_requested_targets_actual_power_and_initial_provenance(self):
        for mode, row_count in (("per_target", 1), ("previous", 2), ("grid", 2)):
            with self.subTest(mode=mode):
                case = self.make(mode, row_count)
                config = load_hardware_combination(case.path)
                preview = launch_summary(config, case.database, "preview")
                self.assertEqual(preview["total_conditions"], row_count * 3 * 2)
                self.assertIn("Optical initial current: " + mode, launch_text(preview))
                self.assertEqual(case.manager.opened, [])
                result = case.execute(mode)
                self.assertEqual(result["status"], "completed", result["error"])
                self.assertTrue(result["cleanup"]["clean"], result["cleanup"])
                rows = load_combination_rows(case.database, run_id=mode)
                self.assertEqual(len(rows), row_count * 3 * 2 * 2)
                self.assertTrue(all(row["acquisition_accepted"] for row in rows))
                qualified = [payload["evidence"] for kind, payload in case.events()
                             if kind == "optical_condition_qualified"]
                self.assertTrue(qualified)
                self.assertTrue(all(row["initial_current"]["mode"] == mode for row in qualified))
                self.assertTrue(all(row["feedback_result"]["target_in_tolerance"] for row in qualified))
                if mode == "previous":
                    inherited = [row for row in qualified if row["initial_current"]["source"] == "previous"]
                    self.assertTrue(inherited)
                    self.assertTrue(all(row["initial_current"]["target_index"] > 0 for row in inherited))
                    first_in_second_row = [row for row in qualified
                        if row["initial_current"]["input_point_index"] == 1
                        and row["initial_current"]["target_index"] == 0]
                    self.assertTrue(first_in_second_row)
                    self.assertTrue(all(row["initial_current"]["source"] == "point"
                                        for row in first_in_second_row))

    def test_duplicate_rows_and_powers_keep_identity_and_reset_previous_start(self):
        case = self.make("previous", 2)
        doc = tomllib.loads(case.path.read_text(encoding="utf-8"))
        doc.pop("combination_scan")
        first = doc["nkt_run"]["points"][0]
        doc["nkt_run"]["points"] = [first, dict(first)]
        doc["power_feedback"]["target_powers_w"] = [5e-6, 5e-6, 22.5e-6]
        case.write_config(source=fixture.toml_document(doc))
        config = load_hardware_combination(case.path)
        axis = next(a for a in config.plan.axes if a.module == "optical")
        self.assertEqual(axis.points[0].values, axis.points[1].values)
        self.assertEqual(axis.points[0].values, axis.points[3].values)
        self.assertEqual([p.metadata["optical_point_index"] for p in axis.points], list(range(6)))
        result = case.execute("duplicate-grid")
        self.assertEqual(result["status"], "completed", result["error"])
        prepared = [payload["evidence"] for kind, payload in case.events()
                    if kind == "optical_point_prepared"]
        indices = {int(row["condition_id"].split("-")[1]) for row in prepared}
        self.assertEqual(indices, set(range(6)))
        starts = [row["initial_current"] for row in prepared]
        self.assertTrue(any(p["target_index"] == 1 and p["source"] == "previous" for p in starts))
        self.assertTrue(all(p["source"] == "point" for p in starts
                            if p["input_point_index"] == 1 and p["target_index"] == 0))


if __name__ == "__main__":
    unittest.main()
