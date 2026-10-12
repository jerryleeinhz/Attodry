"""Four-channel auditing, using only injected fake DLL transports."""
from copy import deepcopy
import unittest

from attodry_control.attodry import AttoDryDriver, AttoDryDllError, AttoDryError
from attodry_control.config import load_config
from attodry_control.temperature_telemetry import TemperatureTelemetryRecorder
from attodry_control.temperature_run import _record_failure_diagnostic
from tests import test_attodry as driver_fixtures
from tests import test_combination_cryostat as combination_fixtures


class TemperatureTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.dll = driver_fixtures.FakeAttoDryDll()
        self.driver = AttoDryDriver.from_config(
            load_config("config/hardware.example.toml"), dll=self.dll,
            connection_authorized=True, writes_authorized=True)
        self.driver.connect(monotonic=lambda: 0, sleeper=lambda _: None)
        self.addCleanup(self.driver.close)
        self.events = []
        self.recorder = TemperatureTelemetryRecorder(
            self.driver, lambda kind, payload: self.events.append((kind, deepcopy(payload))))

    def test_queries_are_sequential_and_raw_is_immutable(self):
        ticks = iter(range(100))
        self.driver.telemetry_monotonic = lambda: next(ticks)
        state = self.driver.read_state()
        raw = self.recorder.start(state, phase="stability")
        original = deepcopy(raw)
        completed = self.recorder.finish(raw)
        self.assertEqual(raw, original)
        self.assertEqual(raw["heater_power"], {"sample_w": None, "vti_w": None})
        self.assertEqual(completed["sample_id"], raw["sample_id"])
        self.assertEqual(completed["heater_power"], {"sample_w": .25, "vti_w": .5})
        timing = completed["state_read_timing"]
        powers = completed["heater_power_reads"]
        self.assertLess(timing["finished_monotonic_s"], powers["sample_w"]["started_monotonic_s"])
        self.assertLess(powers["sample_w"]["finished_monotonic_s"], powers["vti_w"]["started_monotonic_s"])
        self.assertEqual(self.events, [("temperature_telemetry", completed)])

    def test_vti_failure_retains_new_sample_power_and_no_stale_vti(self):
        self.recorder.finish(self.recorder.start(self.driver.read_state()))
        self.dll.sample_heater_power_w = .75
        self.dll.return_codes["get_vti_heater_power"] = 7
        raw = self.recorder.start(self.driver.read_state())
        with self.assertRaises(AttoDryDllError):
            self.recorder.finish(raw)
        failed = self.events[-1][1]
        self.assertEqual(failed["heater_power"], {"sample_w": .75, "vti_w": None})
        self.assertFalse(failed["telemetry_complete"])
        self.assertIn("7", failed["heater_power_read_error"])
        self.assertIn("error", failed["heater_power_reads"]["vti_w"])
        self.assertIn("finished_unix_s", failed["heater_power_read_timing"])

    def test_first_failure_never_queries_second_power(self):
        self.dll.return_codes["get_sample_heater_power"] = 9
        with self.assertRaises(AttoDryDllError):
            self.recorder.finish(self.recorder.start(self.driver.read_state()))
        self.assertNotIn("get_vti_heater_power", self.dll.events)
        self.assertEqual(self.events[-1][1]["heater_power"], {"sample_w": None, "vti_w": None})

    def test_failure_summary_retains_same_partial_attempt_as_audit(self):
        self.driver.temperature_telemetry = self.recorder
        self.driver.read_state()
        self.dll.return_codes["get_vti_heater_power"] = 7
        summary = {}
        _record_failure_diagnostic(summary, self.driver, wall_time=lambda: 123)
        diagnostic = summary["failure_diagnostic"]
        self.assertEqual(diagnostic["heater_power"], {"sample_w": .25, "vti_w": None})
        self.assertEqual(diagnostic["heater_power_sample_id"], self.events[-1][1]["sample_id"])
        self.assertIn("heater_power_read_error", diagnostic)

    def test_negative_or_nonfinite_power_still_fails_closed(self):
        for value in (-.1, float("nan"), float("inf")):
            with self.subTest(value=value):
                self.dll.sample_heater_power_w = value
                with self.assertRaises(AttoDryError):
                    self.recorder.finish(self.recorder.start(self.driver.read_state()))
                self.assertFalse(self.events[-1][1]["telemetry_complete"])
                self.assertIsNone(self.events[-1][1]["heater_power"]["sample_w"])

    def test_sample_ids_are_unique_and_host_timing_does_not_use_wait_clock(self):
        first = self.recorder.start(self.driver.read_state())
        second = self.recorder.start(self.driver.read_state())
        self.assertNotEqual(first["sample_id"], second["sample_id"])
        self.assertIn("finished_monotonic_s", second["state_read_timing"])
        other = TemperatureTelemetryRecorder(self.driver, lambda *_: None)
        self.assertNotEqual(first["sample_id"], other.start(self.driver.read_state())["sample_id"])


class CombinationTemperatureTelemetryTests(unittest.TestCase):
    setUp = combination_fixtures.CryostatCombinationTests.setUp
    write_config = combination_fixtures.CryostatCombinationTests.write_config
    factory = combination_fixtures.CryostatCombinationTests.factory
    events = combination_fixtures.CryostatCombinationTests.events
    execute = combination_fixtures.CryostatCombinationTests.execute

    def test_qualification_formal_cleanup_all_have_linked_power(self):
        self.write_config(("temperature",))
        result = self.execute()
        self.assertEqual(result["status"], "completed", result)
        events = self.events()
        raw_ids = {row["sample_id"] for kind, row in events if kind == "cryostat_state"}
        completed = [row for kind, row in events if kind == "temperature_telemetry"]
        self.assertTrue({"temperature_qualification", "formal_temperature", "cleanup"}
                        <= {row.get("stage") for row in completed})
        self.assertTrue(all(row["sample_id"] in raw_ids for row in completed))
        self.assertTrue(all(row["heater_power"] == {"sample_w": .25, "vti_w": .5}
                            for row in completed))
        self.assertEqual(self.dll.events.count("connect"), 1)

    def test_magnetic_only_does_not_add_heater_queries(self):
        self.write_config(("magnetic",))
        result = self.execute()
        self.assertEqual(result["status"], "completed", result)
        self.assertNotIn("get_sample_heater_power", self.dll.events)
        self.assertFalse(any(kind == "temperature_telemetry" for kind, _ in self.events()))

    def test_drift_is_persisted_and_rejected_before_optional_power(self):
        self.write_config(("temperature",))
        configuration = combination_fixtures.load_hardware_combination(self.path)
        events = []
        session = combination_fixtures.CryostatPointSession(
            configuration.cryostat, configuration.temperature, None,
            lambda kind, row: events.append((kind, deepcopy(row))), dll=self.dll,
            monotonic=driver_fixtures.StepClock(), sleep=lambda _: None)
        session.open()
        self.addCleanup(session.close)
        session.temperature_target = 2.0
        session.baseline_temperature = 2.0
        self.dll.temperature_control = 1
        self.dll.sample_temperature_k = 2.1
        self.dll.events.clear()
        with self.assertRaisesRegex(AttoDryError, "drifted"):
            session.capture("after_sample")
        self.assertNotIn("get_sample_heater_power", self.dll.events)
        self.assertEqual(events[-1][0], "environment_sample")
        self.assertAlmostEqual(events[-1][1]["state"]["sample_temperature_k"], 2.1, places=5)
        self.assertIsNone(events[-1][1]["heater_power"]["sample_w"])
