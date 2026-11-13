"""Native external frequency tolerance has bounded, model-specific scope."""
from dataclasses import asdict, replace
import json
import math
from types import SimpleNamespace
import unittest

from attodry_control import lockin_test as daily
from attodry_control.electrical_lockin_backend import Sr865aReceiver
from attodry_control.models import LockinRole
from attodry_control.sr830 import Sr830Error
from attodry_control.sr865a import Sr865a, Sr865aError
from tests.test_sr865a import FakeResource
from tests import test_external_frequency_qualification as external_fixture


class ExternalFrequencyTolerance200PpmTests(unittest.TestCase):
    def driver(self, reference, detection, harmonic=2, *, internal=False):
        resource = FakeResource()
        resource.responses.update({"HARM?": str(harmonic), "FREQEXT?": str(reference),
                                   "FREQINT?": str(reference),
                                   "FREQDET?": detection if isinstance(detection, Exception) else str(detection),
                                   "RSRC?": "0" if internal else "1"})
        waits = []
        driver = Sr865a(resource, LockinRole.XY, sleep=waits.append)
        return resource, driver, waits

    @staticmethod
    def sample(driver):
        return driver.read_sample(consume_status_latches=True, current_status_supported=True)

    def test_all_three_real_failed_pairs_now_pass_once_with_raw_values_and_clean_status(self):
        pairs = (("4807.7280273", "9616.5849609"),
                 ("4807.7211914", "9616.6025391"),
                 ("4807.7241211", "9616.6113281"))
        for reference, detection in pairs:
            with self.subTest(reference=reference, detection=detection):
                resource, driver, waits = self.driver(reference, detection)
                sample = self.sample(driver)
                self.assertTrue(sample.status.valid)
                self.assertEqual(waits, [])
                self.assertEqual(len(sample.frequency_checks), 1)
                check = sample.frequency_checks[0]
                self.assertTrue(check.frequency_consistent)
                self.assertEqual(check.reference_tolerance_hz, float(reference) * 200e-6)
                self.assertEqual(sample.reference_frequency_hz, float(reference))
                self.assertEqual(sample.detection_frequency_hz, float(detection))
                raw = [r for r in sample.raw if r.command in ("FREQEXT?", "FREQDET?")]
                self.assertEqual([r.response for r in raw], [reference, detection])
                self.assertTrue(all(r.started_at_utc <= r.completed_at_utc for r in raw))
                self.assertEqual(resource.events.count(("query", "SNAP? X,Y")), 1)
                self.assertEqual(resource.events.count(("query", "CUROVLDSTAT?")), 1)
                self.assertEqual(resource.writes, [])

    def test_success_query_trace_matches_a_perfect_pair_without_extra_waits(self):
        traces = []
        for detection in ("9616", "9617.4"):
            resource, driver, waits = self.driver("4808", detection)
            self.assertTrue(self.sample(driver).status.valid)
            self.assertEqual(waits, [])
            traces.append(resource.events)
        self.assertEqual(traces[0], traces[1])

    def test_inside_each_frequency_and_harmonic_bound_is_accepted(self):
        for reference in (20.0, 4808.0, 8000.0):
            tolerance = max(0.005, reference * 200e-6)
            for harmonic in (1, 2, 3):
                for sign in (-1, 1):
                    with self.subTest(reference=reference, harmonic=harmonic, sign=sign):
                        detection = harmonic * (reference + sign * tolerance * 0.999)
                        _, driver, waits = self.driver(reference, detection, harmonic)
                        sample = self.sample(driver)
                        check = sample.frequency_checks[0]
                        self.assertTrue(check.frequency_consistent)
                        self.assertEqual(check.reference_tolerance_hz, tolerance)
                        self.assertEqual(check.detection_tolerance_hz, harmonic * tolerance)
                        self.assertEqual(waits, [])

    def test_binary_exact_200ppm_boundary_is_inclusive_for_each_harmonic(self):
        # 6250 Hz * 200 ppm = exactly 1.25 Hz in binary; no decimal-boundary ambiguity.
        for harmonic in (1, 2, 3):
            for sign in (-1, 1):
                with self.subTest(harmonic=harmonic, sign=sign):
                    _, driver, waits = self.driver(6250.0, harmonic * (6250.0 + sign * 1.25), harmonic)
                    check = self.sample(driver).frequency_checks[0]
                    self.assertEqual(check.reference_error_hz, 1.25)
                    self.assertEqual(check.reference_tolerance_hz, 1.25)
                    self.assertTrue(check.frequency_consistent)
                    self.assertEqual(waits, [])

    def test_adjacent_float_outside_200ppm_boundary_remains_rejected(self):
        for harmonic in (1, 2, 3):
            for sign in (-1, 1):
                with self.subTest(harmonic=harmonic, sign=sign):
                    edge = 6250.0 + sign * 1.25
                    outside = math.nextafter(edge, math.inf if sign > 0 else -math.inf)
                    resource, driver, waits = self.driver(6250.0, harmonic * outside, harmonic)
                    with self.assertRaisesRegex(Sr865aError, "exhausted") as error:
                        self.sample(driver)
                    self.assertEqual(len(error.exception.frequency_checks), 3)
                    self.assertTrue(all(not c.frequency_consistent for c in error.exception.frequency_checks))
                    self.assertEqual(waits, [1.0, 1.0])
                    self.assertNotIn(("query", "SNAP? X,Y"), resource.events)

    def test_outside_each_frequency_and_harmonic_bound_remains_rejected(self):
        for reference in (20.0, 4808.0, 8000.0):
            tolerance = max(0.005, reference * 200e-6)
            for harmonic in (1, 2, 3):
                for sign in (-1, 1):
                    with self.subTest(reference=reference, harmonic=harmonic, sign=sign):
                        detection = harmonic * (reference + sign * tolerance * 1.001)
                        _, driver, waits = self.driver(reference, detection, harmonic)
                        with self.assertRaises(Sr865aError) as error:
                            self.sample(driver)
                        self.assertEqual(len(error.exception.frequency_checks), 3)
                        self.assertEqual(waits, [1.0, 1.0])

    def test_five_millihertz_fundamental_floor_and_harmonic_scaling_are_unchanged(self):
        for harmonic in (1, 2, 3):
            with self.subTest(harmonic=harmonic):
                _, driver, waits = self.driver(0.005, harmonic * 0.01, harmonic)
                check = self.sample(driver).frequency_checks[0]
                self.assertEqual(check.reference_error_hz, 0.005)
                self.assertEqual(check.reference_tolerance_hz, 0.005)
                self.assertEqual(check.detection_tolerance_hz, harmonic * 0.005)
                self.assertTrue(check.frequency_consistent)
                self.assertEqual(waits, [])

    def test_exhausted_new_bound_retains_three_observations_and_revokes_identity(self):
        resource, driver, waits = self.driver(4808.0, 9620.0)
        with self.assertRaisesRegex(Sr865aError, "3/3") as error:
            self.sample(driver)
        self.assertEqual(len(error.exception.frequency_checks), 3)
        self.assertEqual(resource.events.count(("query", "FREQEXT?")), 3)
        self.assertEqual(resource.events.count(("query", "FREQDET?")), 3)
        self.assertEqual(waits, [1.0, 1.0])
        self.assertNotIn(("query", "SNAP? X,Y"), resource.events)
        with self.assertRaisesRegex(Sr865aError, "identity"):
            driver.set_harmonic(1, authorized=True)
        self.assertEqual(resource.writes, [])

    def test_faulted_status_never_becomes_valid_inside_the_wider_frequency_bound(self):
        for command, value in (("CUROVLDSTAT?", "8"), ("CUROVLDSTAT?", "16"),
                               ("CUROVLDSTAT?", "256"), ("CUROVLDSTAT?", "4"),
                               ("LIAS?", "8"), ("LIAS?", "16"), ("LIAS?", "32"),
                               ("LIAS?", "1"), ("ERRS?", "1"),
                               ("*ESR?", "32"), ("*ESR?", "64"), ("*ESR?", "128")):
            with self.subTest(command=command, value=value):
                resource, driver, waits = self.driver("4807.7241211", "9616.6113281")
                resource.responses[command] = value
                sample = self.sample(driver)
                self.assertTrue(sample.frequency_checks[0].frequency_consistent)
                self.assertFalse(sample.status.valid)
                self.assertEqual(waits, [])
                self.assertEqual(resource.writes, [])

    def test_invalid_frequency_and_transport_failure_are_not_tolerance_exceptions(self):
        for value in ("nan", "inf", "not-a-number", "0", "4000000", OSError("fake transport")):
            with self.subTest(value=value):
                resource, driver, waits = self.driver(4808.0, value)
                with self.assertRaises(Sr865aError):
                    self.sample(driver)
                self.assertEqual(waits, [])
                self.assertEqual(resource.events.count(("query", "FREQDET?")), 1)
                self.assertNotIn(("query", "SNAP? X,Y"), resource.events)

    def test_internal_reference_tolerance_is_not_relaxed(self):
        _, external, waits = self.driver(4808.0, 9616.1)
        self.assertTrue(self.sample(external).frequency_checks[0].frequency_consistent)
        self.assertEqual(waits, [])
        resource, internal, waits = self.driver(4808.0, 9616.1, internal=True)
        with self.assertRaisesRegex(Sr865aError, "Detection frequency") as error:
            self.sample(internal)
        self.assertEqual(len(error.exception.frequency_checks), 1)
        check = error.exception.frequency_checks[0]
        self.assertAlmostEqual(check.detection_tolerance_hz, 0.0096161)
        self.assertEqual(waits, [])
        self.assertNotIn(("query", "SNAP? X,Y"), resource.events)

    def test_external_session_target_tolerance_stays_at_100ppm_with_its_original_floor(self):
        self.assertEqual(daily.SWEEP_FREQUENCY_REL_TOLERANCE, 100e-6)
        self.assertEqual(daily.SWEEP_FREQUENCY_ABS_TOLERANCE_HZ, 0.00101)
        _, driver, _ = self.driver(1217.0, 1217.18, harmonic=1)
        self.assertTrue(self.sample(driver).frequency_checks[0].frequency_consistent)
        fixture = external_fixture.ExternalFrequencyPendingTests()
        self.addCleanup(fixture.doCleanups)
        f, _, session, clock, _, _ = fixture.ready()
        query = f.xy.query
        def shifted(command):
            value = query(command)
            return "1217.18" if command == "FREQEXT?" else value
        f.xy.query = shifted
        with self.assertRaisesRegex(Sr830Error, "1217.18"):
            session._read_coordinates()
        self.assertEqual(clock.waits, [])

    def test_recorded_old_check_threshold_and_failure_are_not_recomputed(self):
        _, driver, _ = self.driver("4807.7241211", "9616.6113281")
        current = self.sample(driver).frequency_checks[0]
        old = replace(current, reference_tolerance_hz=0.48077241211,
                      detection_tolerance_hz=0.96154482422, frequency_consistent=False)
        historical = json.dumps({"run_status": "failed", "frequency_checks": [asdict(old)]}, default=str)
        decoded = json.loads(historical)
        self.assertFalse(decoded["frequency_checks"][0]["frequency_consistent"])
        self.assertEqual(decoded["frequency_checks"][0]["reference_tolerance_hz"], 0.48077241211)
        self.assertEqual(decoded["run_status"], "failed")
        self.assertTrue(current.frequency_consistent)
        self.assertAlmostEqual(current.reference_tolerance_hz, 0.96154482422)
        self.assertEqual(json.dumps({"run_status": "failed", "frequency_checks": [asdict(old)]}, default=str), historical)

    def test_receiver_serializes_actual_200ppm_thresholds_and_native_raw_values(self):
        resource, _, waits = self.driver("4807.7241211", "9616.6113281")
        receiver = Sr865aReceiver(resource, config=SimpleNamespace(current_status_supported=True))
        receiver._driver._sleep = waits.append
        record = json.loads(json.dumps(asdict(receiver.read_harmonic_sample(2)), default=str))
        native = record["native_sample"]
        check = native["frequency_checks"][0]
        self.assertTrue(check["frequency_consistent"])
        self.assertAlmostEqual(check["reference_tolerance_hz"], 0.96154482422)
        self.assertAlmostEqual(check["detection_tolerance_hz"], 1.92308964844)
        self.assertEqual(native["reference_frequency_hz"], 4807.7241211)
        self.assertEqual(native["detection_frequency_hz"], 9616.6113281)
        self.assertEqual(waits, [])
        self.assertEqual(resource.writes, [])


if __name__ == "__main__":
    unittest.main()
