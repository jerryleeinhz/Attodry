"""Independent multi-fault checks for additive coordinator timing evidence."""
from contextlib import contextmanager
import unittest
from unittest.mock import patch

from tests import test_measurement_timing as fixtures
from tests import test_combination_cryostat as hardware_fixtures


class TimingMultiFaultReviewTests(unittest.TestCase):
    setUp = fixtures.CoordinatorTimingTests.setUp
    execute = fixtures.CoordinatorTimingTests.execute

    def test_primary_read_survives_all_subsequent_phase_and_timing_audit_faults(self):
        failed_read = False

        def fail_audit(kind, payload):
            nonlocal failed_read
            if kind == "measurement_stage_timing" and payload["stage"] == "module_read":
                failed_read = True
            return failed_read and kind in {"measurement_stage_timing", "scan_phase"}

        result, station, _, _ = self.execute(
            failure=ValueError("primary detector fault"), audit_failure=fail_audit)
        self.assertEqual(result["status"], "failed")
        self.assertIn("primary detector fault", result["error"])
        self.assertEqual([op[1] for op in station.operations if op[0] == "cleanup"],
                         ["lockin", "smu"])
        self.assertTrue(station.closed)
        self.assertTrue(result["cleanup"]["manual_verification_required"])
        self.assertGreater(len(result["cleanup"]["errors"]), 2)

    def test_first_cleanup_and_close_faults_preserve_later_cleanup_and_primary(self):
        original_cleanup = fixtures.TimedStation.cleanup
        original_close = fixtures.TimedStation.close

        def cleanup(station, module, failed):
            action = original_cleanup(station, module, failed)
            if module == "lockin":
                raise OSError("detector cleanup transport fault")
            return action

        def close(station):
            original_close(station)
            raise OSError("close transport fault")

        with patch.object(fixtures.TimedStation, "cleanup", cleanup), \
                patch.object(fixtures.TimedStation, "close", close):
            result, station, _, _ = self.execute(
                failure=ValueError("primary detector fault"),
                audit_failure=lambda kind, payload: kind == "measurement_stage_timing"
                and payload["stage"] in {"module_read", "cleanup", "close"})
        self.assertIn("primary detector fault", result["error"])
        self.assertEqual([op[1] for op in station.operations if op[0] == "cleanup"],
                         ["lockin", "smu"])
        self.assertTrue(station.closed)
        errors = " ".join(result["cleanup"]["errors"])
        self.assertIn("detector cleanup transport fault", errors)
        self.assertIn("close transport fault", errors)
        self.assertIn("timing disk failure", errors)


class HardwareTranscriptReviewTests(unittest.TestCase):
    def test_real_coordinator_fake_devices_keep_queries_writes_and_actions(self):
        class NoTiming:
            def __init__(self, event, **kwargs):
                self.emit, self.audit_errors = event, []

            @contextmanager
            def timed_stage(self, *args, **kwargs):
                yield {}

            def summary(self):
                return {}

        def run(disabled):
            fixture = hardware_fixtures.CryostatCombinationTests()
            fixture.setUp()
            try:
                fixture.write_config(("temperature", "smu", "lockin"))
                if disabled:
                    with patch("attodry_control.combination_scan.MeasurementTiming", NoTiming):
                        result = fixture.execute()
                else:
                    result = fixture.execute()
                self.assertEqual(result["status"], "completed", result)
                return (list(fixture.dll.events), list(fixture.log),
                        list(fixture.xx.queries), list(fixture.xx.writes),
                        list(fixture.xy.queries), list(fixture.xy.writes))
            finally:
                fixture.doCleanups()

        self.assertEqual(run(False), run(True))


if __name__ == "__main__":
    unittest.main()
