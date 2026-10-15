"""Axis-entry qualification and final-setting timing with fake hardware only."""
import unittest

from attodry_control.combination_analysis import load_combination_rows
from attodry_control.combination_hardware import load_hardware_combination, run_hardware_combination
from attodry_control.combination_store import CombinationStore
from tests import test_combination_cryostat as cryostat_fixtures
from tests.test_lockin_settling import Clock


class CombinationTimingTests(unittest.TestCase):
    setUp = cryostat_fixtures.CryostatCombinationTests.setUp
    write_config = cryostat_fixtures.CryostatCombinationTests.write_config
    factory = cryostat_fixtures.CryostatCombinationTests.factory
    events = cryostat_fixtures.CryostatCombinationTests.events
    execute = cryostat_fixtures.CryostatCombinationTests.execute

    def test_temperature_dwell_once_outside_all_inner_axes(self):
        self.write_config(("temperature", "smu", "lockin"))
        result = self.execute()
        self.assertEqual(result["status"], "completed", result)
        events = self.events()
        qualifications = [v for k, v in events if k == "temperature_qualification_completed"]
        self.assertEqual(len(qualifications), 1)
        self.assertEqual(len(load_combination_rows(self.database)), 6)

    def test_temperature_drift_is_recorded_after_initial_qualification(self):
        self.write_config(("temperature", "smu"))
        def modify(adapter):
            original = adapter.read
            def read():
                result = original()
                if adapter.read_count == 2:
                    self.dll.sample_temperature_k = 2.1
                return result
            adapter.read = read
        self.modify_adapter = modify
        result = self.execute()
        self.assertEqual(result["status"], "completed", result)
        windows = [v for k, v in self.events() if k == "environment_formal_window"]
        self.assertTrue(any("temperature_baseline_drift" in v["temperature_quality_flags"]
                            for v in windows))
        self.assertEqual(sum(k == "temperature_qualification_completed"
                             for k, _ in self.events()), 1)
        self.assertTrue(any(r["actual.temperature_k"] > 2
                            for r in load_combination_rows(self.database)))

    def test_temperature_control_loss_still_rejects(self):
        self.write_config(("temperature", "smu"))
        def modify(adapter):
            original = adapter.read
            def read():
                result = original()
                if adapter.read_count == 2:
                    self.dll.temperature_control = 0
                return result
            adapter.read = read
        self.modify_adapter = modify
        result = self.execute()
        self.assertEqual(result["status"], "failed")
        self.assertIn("control became disabled", result["error"])
        self.assertEqual(load_combination_rows(self.database), ())

    def test_smu_delay_counts_towards_lockin_qualification(self):
        import re
        source = re.sub(r"(?m)^delay_s = .*", "delay_s = 10.0", self.base)
        source = re.sub(r"(?m)^excitation_(xx|xy)_harmonics = .*",
                        r"excitation_\1_harmonics = [1]", source)
        self.write_config(("lockin", "smu"), source=source)
        clock = Clock()
        config = load_hardware_combination(self.path)
        with CombinationStore(self.database) as store:
            result = run_hardware_combination(config, store, "credit",
                authorize_hardware=True, confirm_xy_sine_disconnected=True,
                smu_adapter_factory=self.factory, manager_factory=lambda: self.manager,
                monotonic=clock.monotonic, sleep=clock.sleep)
        self.assertEqual(result["status"], "completed", result)
        qualifications = [v for k, v in self.events() if k == "lockin_point_qualification"]
        self.assertEqual(len(qualifications), 6)
        self.assertTrue(all(v["settling_waits"][-1]["sleep_requested_s"] == 0
                            for v in qualifications))
        self.assertTrue(all(min(v["settling_waits"][-1]["already_elapsed_s_by_role"].values()) >= 10
                            for v in qualifications))
