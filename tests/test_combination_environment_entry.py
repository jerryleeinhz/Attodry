"""Adjacent entry observation reuse, with fake DLLs and no hardware imports."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from contextlib import closing
import re
import unittest
from unittest.mock import patch

from attodry_control.attodry import AttoDryDllError, AttoDryError
from attodry_control.combination_hardware import HardwareCombinationStation, load_hardware_combination
from attodry_control.cryostat_points import CryostatPointSession
from tests import test_combination_cryostat as fixtures
from tests import test_combination_6221 as source_fixtures
from tests.test_lockin_settling import Clock


class EnvironmentEntryTests(unittest.TestCase):
    setUp = fixtures.CryostatCombinationTests.setUp
    write_config = fixtures.CryostatCombinationTests.write_config
    factory = fixtures.CryostatCombinationTests.factory
    events = fixtures.CryostatCombinationTests.events
    execute = fixtures.CryostatCombinationTests.execute

    def session(self, order=("temperature",)):
        self.write_config(order)
        config = load_hardware_combination(self.path)
        clock, events = Clock(), []
        session = CryostatPointSession(config.cryostat, config.temperature, config.magnetic,
            lambda kind, row: events.append((kind, deepcopy(row))), dll=self.dll,
            monotonic=clock.monotonic, sleep=clock.sleep)
        session.open()
        self.addCleanup(session.close)
        if "temperature" in order:
            self.dll.temperature_control = 1
            session.temperature_target = session.baseline_temperature = 2.0
            session.temperature_qualification = {"qualified_at_utc": "2030-01-01T00:00:00+00:00"}
        station = HardwareCombinationStation(config)
        station.cryo = session
        station.set_event_sink(lambda *_: None)
        self.dll.events.clear()
        return session, station, clock, events

    def test_first_environment_read_reuses_original_once_without_duplicate_window_weight(self):
        session, station, clock, events = self.session()
        station.begin_sample()
        original = deepcopy(session.window[0])
        calls = list(self.dll.events)
        reading = station.read("temperature")
        self.assertEqual(self.dll.events, calls)
        self.assertEqual({k: v for k, v in reading["status"].items()
                          if k != "entry_observation_reuse"}, original)
        self.assertEqual(reading["captured_at_utc"], original["captured_at_utc"])
        self.assertEqual(len(session.window), 1)
        alias = next(row for kind, row in events if kind == "environment_sample_reuse_candidate")
        self.assertEqual(alias["sample_id"], original["sample_id"])
        self.assertEqual(alias["age_s"], 0)
        self.assertEqual(alias["maximum_age_s"], 1)
        clock.now += .2
        self.dll.sample_temperature_k = 2.01
        station.end_sample([reading])
        self.assertEqual(self.dll.events.count("get_sample_temperature"), 2)
        self.assertEqual(self.dll.events.count("get_sample_heater_power"), 2)
        self.assertEqual(len(reading["status"]["samples"]), 2)
        self.assertEqual([row["stage"] for row in reading["status"]["samples"]],
                         ["before_sample", "after_sample"])
        self.assertGreater(reading["actual"]["temperature_k"], 2)

    def test_default_direct_read_retains_fresh_capture(self):
        session, _, _, _ = self.session()
        session.begin_sample()
        reading = session.read("temperature")
        self.assertEqual(reading["status"]["stage"], "formal_temperature")
        self.assertEqual(self.dll.events.count("get_sample_temperature"), 2)
        self.assertEqual(len(session.window), 2)

    def test_second_read_never_reuses_entry(self):
        session, station, _, _ = self.session()
        station.begin_sample()
        station.read("temperature")
        self.dll.sample_temperature_k = 2.01
        second = station.read("temperature")
        self.assertAlmostEqual(second["actual"]["temperature_k"], 2.01, places=5)
        self.assertEqual(self.dll.events.count("get_sample_temperature"), 2)

    def test_age_boundary_expiry_clock_rollback_and_nonfinite_age(self):
        for elapsed, reused in ((1, True), (1.00001, False), (-.001, False),
                                (float("nan"), False), (float("inf"), False)):
            with self.subTest(elapsed=elapsed):
                session, station, clock, _ = self.session()
                station.begin_sample()
                clock.now = elapsed
                before = self.dll.events.count("get_sample_temperature")
                station.read("temperature")
                self.assertEqual(self.dll.events.count("get_sample_temperature") - before,
                                 0 if reused else 1)
                clock.now = 0
                session.close()

    def test_age_is_also_bounded_by_environment_poll_interval(self):
        session, station, clock, _ = self.session()
        session.config.temperature_stability = replace(
            session.config.temperature_stability, poll_interval_s=.1)
        station.begin_sample()
        clock.now = .1001
        station.read("temperature")
        self.assertEqual(self.dll.events.count("get_sample_temperature"), 2)

    def test_capture_io_and_audit_time_count_towards_age(self):
        session, station, clock, _ = self.session()
        original = session.event
        # Full-state audit is inside the age bracket, even before power reads.
        def delayed(kind, row):
            original(kind, row)
            if kind == "cryostat_state":
                clock.now += 1.01
        session.event = delayed
        station.begin_sample()
        station.read("temperature")
        self.assertEqual(self.dll.events.count("get_sample_temperature"), 2)

    def test_any_intervening_dll_operation_including_failed_power_query_consumes_entry(self):
        for operation in ("state", "power", "failed_power"):
            with self.subTest(operation=operation):
                session, station, _, _ = self.session()
                station.begin_sample()
                if operation == "state":
                    session.driver.read_state()
                elif operation == "power":
                    session.driver.read_heater_powers()
                else:
                    self.dll.return_codes["get_sample_heater_power"] = 7
                    with self.assertRaises(AttoDryDllError):
                        session.driver.read_heater_powers()
                    self.dll.return_codes.clear()
                before = self.dll.events.count("get_sample_temperature")
                station.read("temperature")
                self.assertEqual(self.dll.events.count("get_sample_temperature"), before + 1)
                session.close()

    def test_candidate_audit_delay_or_observer_io_invalidates_before_reuse(self):
        for operation in ("delay", "power", "invalidate"):
            with self.subTest(operation=operation):
                session, station, clock, _ = self.session()
                station.begin_sample()
                original = session.event
                def observer(kind, row):
                    original(kind, row)
                    if kind == "environment_sample_reuse_candidate":
                        if operation == "delay":
                            clock.now += 1.01
                        elif operation == "power":
                            session.driver.read_heater_powers()
                        else:
                            session.invalidate_sample_entry()
                session.event = observer
                reading = station.read("temperature")
                self.assertNotIn("entry_observation_reuse", reading["status"])
                self.assertEqual(self.dll.events.count("get_sample_temperature"), 2)
                session.close()

    def test_intervening_station_preparation_consumes_entry_even_without_io(self):
        for operation in ("range", "qualify_axis", "apply"):
            with self.subTest(operation=operation):
                session, station, _, _ = self.session()
                station.begin_sample()
                if operation == "range":
                    station.prepare_lockin_ranges({})
                elif operation == "qualify_axis":
                    station.qualify_axis("lockin", None, {})
                else:
                    station.apply("excitation", SimpleNamespace(values={}))
                station.read("temperature")
                self.assertEqual(self.dll.events.count("get_sample_temperature"), 2)
                session.close()

    def test_nonenvironment_first_axis_does_not_enable_reuse(self):
        session, station, _, _ = self.session(("smu", "temperature"))
        station.begin_sample()
        # Even an out-of-order direct caller cannot gain the first-axis proof.
        station.read("temperature")
        self.assertEqual(self.dll.events.count("get_sample_temperature"), 2)

    def test_second_environment_axis_gets_its_own_fresh_observation(self):
        session, station, _, _ = self.session(("temperature", "magnetic"))
        station.begin_sample()
        station.read("temperature")
        station.read("magnetic")
        self.assertEqual(self.dll.events.count("get_sample_temperature"), 2)

    def test_magnetic_only_reuses_state_without_inventing_heater_telemetry(self):
        session, station, _, events = self.session(("magnetic",))
        station.begin_sample()
        station.read("magnetic")
        self.assertEqual(self.dll.events.count("get_sample_temperature"), 1)
        self.assertNotIn("get_sample_heater_power", self.dll.events)
        alias = next(row for kind, row in events if kind == "environment_sample_reuse_candidate")
        self.assertIsNone(alias["sample_id"])

    def test_changed_window_or_raw_record_forces_fresh_capture(self):
        for change in ("window", "raw", "partial"):
            with self.subTest(change=change):
                session, station, _, _ = self.session()
                station.begin_sample()
                if change == "window":
                    session.window = list(session.window)
                elif change == "raw":
                    session.window[0]["state"]["sample_temperature_k"] = 9
                else:
                    session.window[0]["telemetry_complete"] = False
                station.read("temperature")
                self.assertEqual(self.dll.events.count("get_sample_temperature"), 2)
                session.close()

    def test_failed_begin_sample_has_no_reusable_earlier_evidence(self):
        session, station, _, _ = self.session()
        station.begin_sample()
        self.dll.return_codes["get_vti_heater_power"] = 7
        with self.assertRaises(AttoDryDllError):
            station.begin_sample()
        self.assertIsNone(session._sample_entry)
        self.assertIsNone(station._sample_entry_module)
        self.dll.return_codes.clear()
        before = self.dll.events.count("get_sample_temperature")
        station.read("temperature")
        self.assertEqual(self.dll.events.count("get_sample_temperature"), before + 1)

    def test_reuse_audit_failure_propagates_and_cannot_replay_proof(self):
        session, station, _, _ = self.session()
        station.begin_sample()
        with patch.object(session, "event", side_effect=OSError("audit full")):
            with self.assertRaisesRegex(OSError, "audit full"):
                station.read("temperature")
        self.assertIsNone(session._sample_entry)
        station.read("temperature")
        self.assertEqual(self.dll.events.count("get_sample_temperature"), 2)

    def test_formal_after_read_remains_fresh_and_rejects_hard_fault(self):
        session, station, _, _ = self.session()
        station.begin_sample()
        reading = station.read("temperature")
        self.dll.error_code = 4
        with self.assertRaisesRegex(AttoDryError, "error code 4"):
            station.end_sample([reading])
        self.assertIsNone(session.window)
        self.assertIsNone(session._sample_entry)
        self.assertEqual(self.dll.events.count("get_sample_temperature"), 2)
        self.dll.error_code = 0

    def test_full_combination_saves_one_state_and_power_snapshot_per_condition(self):
        from attodry_control.combination_analysis import load_combination_rows
        self.write_config(("temperature", "lockin"))
        with patch.object(CryostatPointSession, "_consume_sample_entry", return_value=None):
            baseline = self.execute("fresh")
        self.assertEqual(baseline["status"], "completed", baseline)
        before = {name: self.dll.events.count(name) for name in (
            "get_sample_temperature", "get_sample_heater_power", "get_vti_heater_power")}
        total_before = len(self.dll.events)
        self.dll = fixtures.FakeAttoDryDll()
        self.dll.temperature_follows_setpoint = True
        optimized = self.execute("reuse")
        self.assertEqual(optimized["status"], "completed", optimized)
        rows = load_combination_rows(self.database, run_id="reuse")
        self.assertEqual(len(rows), 2)
        self.assertEqual(total_before - len(self.dll.events), 12 * len(rows))
        for name, count in before.items():
            self.assertEqual(count - self.dll.events.count(name), len(rows), name)
        self.assertTrue(all(row["actual.temperature_k"] == 2 for row in rows))
        self.assertTrue(all(row["status.temperature"]["samples"][0]["stage"] == "before_sample"
                            and row["status.temperature"]["samples"][-1]["stage"] == "after_sample"
                            for row in rows))


class MixedDetectorEnvironmentEntryTests(unittest.TestCase):
    setUp = source_fixtures.Combination6221Tests.setUp
    devices = source_fixtures.Combination6221Tests.devices
    config = source_fixtures.Combination6221Tests.config
    execute = source_fixtures.Combination6221Tests.execute
    events = source_fixtures.Combination6221Tests.events

    def test_temperature_6221_split_roles_keep_four_true_environment_brackets(self):
        from attodry_control.combination_analysis import load_combination_rows
        from attodry_control.combination_store import open_readonly, validate_accepted_records
        thermal = (source_fixtures.ROOT / "config/hardware.example.toml").read_text(encoding="utf-8")
        thermal = "[cryostat]" + thermal.split("[cryostat]", 1)[1].split("[visa]", 1)[0]
        thermal = re.sub(r"(?m)^temperature_ranges = \[.*?^\]",
            "start_k = 2.0\nstop_k = 2.1\nstep_k = 0.1", thermal, flags=re.DOTALL)
        thermal = thermal.replace("stable_dwell_s = 30.0", "stable_dwell_s = 3.0")
        text = (self.text.replace('order = ["excitation", "lockin"]',
                'order = ["temperature", "excitation", "lockin"]')
            .replace("current_rms_points_a = [0.000001, 0.000002]", "current_rms_points_a = [0.000001]")
            .replace("frequency_points_hz = [17.777, 200.0]", "frequency_points_hz = [25.0]")
            .replace("xx_harmonics = [1, 2]", "xx_harmonics = [1]")
            .replace("xy_harmonics = [1, 2]", "xy_harmonics = [2]"))
        self.path.write_text(text + "\n" + thermal, encoding="utf-8")
        dll = fixtures.FakeAttoDryDll()
        dll.temperature_follows_setpoint = True
        result = self.execute(dll=dll, authorize_cryostat=True)
        self.assertEqual(result["status"], "completed", result)
        events = self.events()
        self.assertEqual(sum(kind == "temperature_qualification_completed" for kind, _ in events), 2)
        self.assertEqual(sum(kind == "environment_sample_reuse_candidate" for kind, _ in events), 2)
        pairs = [row for kind, row in events if kind == "lockin_formal_pair"]
        self.assertEqual(len(pairs), 2)
        for pair in pairs:
            self.assertIsNone(pair["harmonic"])
            self.assertEqual(pair["harmonics_by_role"], {"lockin_xx": 1, "lockin_xy": 2})
        rows = load_combination_rows(self.database, run_id="current-run")
        self.assertEqual(len(rows), 2)
        for row in rows:
            samples = row["status.temperature"]["samples"]
            self.assertEqual([sample["stage"] for sample in samples],
                             ["before_sample", "before", "after", "after_sample"])
            self.assertEqual(len({sample["sample_id"] for sample in samples}), 4)
            self.assertTrue(all(sample["telemetry_complete"] for sample in samples))
        with closing(open_readonly(self.database)) as db:
            self.assertEqual(validate_accepted_records(db, "current-run")["accepted_conditions"], 2)


if __name__ == "__main__":
    unittest.main()
