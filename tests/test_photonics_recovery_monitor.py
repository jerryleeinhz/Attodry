"""File-only recovery progress survives later low-level I/O events."""
from pathlib import Path
import tempfile
import unittest

from attodry_control.combination_scan import AxisPoint, ScanAxis, CombinationPlan
from attodry_control.combination_store import CombinationStore, run_snapshot
from attodry_control.combination_terminal import snapshot_text


class RecoveryMonitorTests(unittest.TestCase):
    def test_optical_monitor_separates_retained_target_from_measured_watts(self):
        for assessment in (None, {"target_in_tolerance": False, "target_deviation_w": -0.00010154345}):
            with self.subTest(assessment=assessment):
                status = {"power_target_w": 0.001, "power_target_assessment": assessment}
                snapshot = {"status": "completed", "last_recorded_readings": {"optical": {
                    "captured_at_utc": "recorded", "actual": {"optical_target_power_w": 0.00089845655},
                    "measurements": {"optical_power_w": 0.00089845655}, "status": status, "clean": True}}}
                text = snapshot_text(snapshot, width=1000)
                self.assertIn('"optical_target_power_w": 0.001', text)
                self.assertIn('"optical_power_w": 0.00089845655', text)
                if assessment:
                    self.assertIn('"target_in_tolerance": false', text)
                # Rendering must not mutate archived legacy evidence.
                self.assertEqual(snapshot["last_recorded_readings"]["optical"]["actual"]
                                 ["optical_target_power_w"], 0.00089845655)

    def test_recovery_progress_is_retained_when_io_is_the_latest_event(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / 'scan.sqlite'
            plan = CombinationPlan((ScanAxis('lockin', (AxisPoint({
                'lockin_excitation_v_rms': 0.004, 'lockin_frequency_hz': 50000.0}),)),))
            with CombinationStore(database) as store:
                store.begin_run('test', plan.snapshot(), plan.conditions())
                store.event('test', 'reference_recovery_poll', {
                    'stage': 'combination_formal_sample', 'consecutive_good': 2,
                    'required_good': 3, 'elapsed_s': 8.0, 'remaining_s': 37.0})
                store.event('test', 'lockin_model_io', {'raw': []})
            snapshot = run_snapshot(database, 'test')
            self.assertEqual(snapshot['last_event']['event_type'], 'lockin_model_io')
            text = snapshot_text(snapshot, width=200)
            self.assertIn('stable 2/3', text)
            self.assertIn('remaining 37.0 s', text)
            self.assertEqual(snapshot['process_liveness'], 'unknown')


if __name__ == '__main__':
    unittest.main()
