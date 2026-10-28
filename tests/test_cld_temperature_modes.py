"""ENAB/PROT admission and session mode consistency, using fake devices only."""
import unittest

from optical_helpers import Clock
from tests.test_cld_drivers import Dll, configuration, visa_backend
from attodry_control.cld_optical import CldOpticalController
from attodry_control.nkt_config import NktError
from attodry_control.photoresponse_cld import CldController, SimulatedCld, TL4000


MODELS = ("CLD1010LP", "CLD1015")
MODES = ((1, "ENAB"), (2, "PROT"))


class CldTemperatureModeTests(unittest.TestCase):
    def make(self, mode=1, model="CLD1015", cls=CldController):
        driver, resource, manager = visa_backend(configuration(expected_model=model))
        resource.values["*IDN?"] = f"Thorlabs,{model},TEST_SERIAL,2.3.0"
        resource.values["OUTP1:PROT:TEMP:MODE?"] = dict(MODES)[mode]
        clock = Clock()
        session = cls(driver.config, driver, clock=clock, sleep=clock.sleep)
        self.addCleanup(session.finish)
        return session, resource, manager, clock

    def prepare(self, session):
        session.begin(authorize_writes=True, confirm_manual_route=True)
        session.prepare_point(session.config.points[0])

    def assert_no_on(self, resource):
        self.assertNotIn(("write", "OUTP1:STAT 1"), resource.calls)

    def test_both_models_and_modes_complete_with_identical_command_and_wait_budgets(self):
        for model in MODELS:
            traces = []
            for mode, _ in MODES:
                with self.subTest(model=model, mode=mode):
                    session, resource, manager, clock = self.make(mode, model)
                    self.assertIsNone(session.temperature_protection_mode)
                    self.prepare(session)
                    self.assertEqual(session.temperature_protection_mode, mode)
                    self.assertEqual(session.record["temperature_protection_mode"], mode)
                    session.set_emission(True)
                    session.verify_point(emitting=True)
                    cleanup = session.finish()
                    self.assertTrue(cleanup["off_confirmed"])
                    self.assertTrue(cleanup["transport_closed"])
                    self.assertEqual(cleanup["errors"], [])
                    self.assertEqual(float(resource.values["SOUR1:CURR:LEV:IMM:AMPL?"]), 0)
                    self.assertFalse(any(kind == "write" and command.startswith("OUTP1:PROT:TEMP:MODE")
                                         for kind, command in resource.calls))
                    manager.close.assert_called_once()
                    traces.append((list(resource.calls), clock()))
            self.assertEqual(traces[0], traces[1])

    def test_optical_preflight_freezes_mode_before_configuration_and_rejects_drift(self):
        for mode, _ in MODES:
            with self.subTest(mode=mode):
                session, resource, _, _ = self.make(mode, cls=CldOpticalController)
                session.begin_session(authorize_writes=True, confirm_manual_route=True)
                self.assertFalse(session.owned)
                self.assertEqual(session.temperature_protection_mode, mode)
                resource.values["OUTP1:PROT:TEMP:MODE?"] = dict(MODES)[3 - mode]
                with self.assertRaises(NktError):
                    session.configure_session()
                self.assertEqual(session.temperature_protection_mode, mode)
                self.assertFalse(any(kind == "write" for kind, _ in resource.calls))

    def test_unhealthy_or_already_on_preflight_does_not_freeze_or_take_ownership(self):
        for command, value in (("SENS2:TEMP:DATA?", "31"), ("OUTP1:STAT?", "1")):
            with self.subTest(command=command):
                session, resource, _, _ = self.make(cls=CldOpticalController)
                resource.values[command] = value
                with self.assertRaises(NktError):
                    session.begin_session(authorize_writes=True, confirm_manual_route=True)
                self.assertIsNone(session.temperature_protection_mode)
                self.assertFalse(session.owned)
                self.assertFalse(any(kind == "write" for kind, _ in resource.calls))

    def test_mode_drift_during_tec_wait_aborts_and_independent_cleanup_still_runs(self):
        for mode, _ in MODES:
            with self.subTest(mode=mode):
                session, resource, _, _ = self.make(mode)
                original_query = resource.query
                mode_reads = 0
                def query(command):
                    nonlocal mode_reads
                    if command == "OUTP1:PROT:TEMP:MODE?":
                        mode_reads += 1
                        if mode_reads == 2:
                            resource.values[command] = dict(MODES)[3 - mode]
                    return original_query(command)
                resource.query = query
                with self.assertRaises(NktError):
                    session.begin(authorize_writes=True, confirm_manual_route=True)
                self.assertTrue(session.owned)
                self.assertTrue(session.failed)
                self.assertEqual(session.temperature_protection_mode, mode)
                self.assert_no_on(resource)
                cleanup = session.finish()
                self.assertTrue(cleanup["off_confirmed"])
                self.assertEqual(cleanup["errors"], [])
                self.assertEqual(resource.values["OUTP2:STAT?"], "0")

    def test_mode_drift_before_emission_blocks_on_without_resetting_baseline(self):
        for mode, _ in MODES:
            with self.subTest(mode=mode):
                session, resource, _, _ = self.make(mode)
                self.prepare(session)
                resource.values["OUTP1:PROT:TEMP:MODE?"] = dict(MODES)[3 - mode]
                with self.assertRaises(NktError):
                    session.set_emission(True)
                self.assertTrue(session.failed)
                self.assertEqual(session.temperature_protection_mode, mode)
                self.assert_no_on(resource)
                self.assertTrue(session.finish()["off_confirmed"])

    def test_disabled_mode_at_adapter_on_guard_blocks_write_after_controller_guard_passes(self):
        session, resource, _, _ = self.make()
        self.prepare(session)
        original_query = resource.query
        mode_reads = 0
        def query(command):
            nonlocal mode_reads
            if command == "OUTP1:PROT:TEMP:MODE?":
                mode_reads += 1
                if mode_reads == 2:
                    resource.values[command] = "OFF"
            return original_query(command)
        resource.query = query
        with self.assertRaises(NktError):
            session.set_emission(True)
        self.assertGreaterEqual(mode_reads, 2)
        self.assert_no_on(resource)
        self.assertTrue(session.failed)
        self.assertTrue(session.finish()["off_confirmed"])

    def test_mode_drift_after_on_is_rejected_without_second_on_and_cleanup_is_independent(self):
        for mode, _ in MODES:
            with self.subTest(mode=mode):
                session, resource, _, _ = self.make(mode)
                self.prepare(session)
                original_write = resource.write
                def write(command):
                    result = original_write(command)
                    if command == "OUTP1:STAT 1":
                        resource.values["OUTP1:PROT:TEMP:MODE?"] = dict(MODES)[3 - mode]
                    return result
                resource.write = write
                with self.assertRaises(NktError):
                    session.set_emission(True)
                self.assertTrue(session.failed)
                self.assertEqual(resource.calls.count(("write", "OUTP1:STAT 1")), 1)
                self.assertTrue(session.finish()["off_confirmed"])
                self.assertEqual(resource.calls.count(("write", "OUTP1:STAT 1")), 1)

    def test_formal_mode_drift_remains_failed_even_if_the_original_mode_returns(self):
        for mode, raw in MODES:
            with self.subTest(mode=mode):
                session, resource, _, _ = self.make(mode)
                self.prepare(session)
                session.set_emission(True)
                resource.values["OUTP1:PROT:TEMP:MODE?"] = dict(MODES)[3 - mode]
                with self.assertRaises(NktError):
                    session.verify_point(emitting=True)
                resource.values["OUTP1:PROT:TEMP:MODE?"] = raw
                with self.assertRaises(NktError):
                    session.set_emission(True)
                self.assertEqual(resource.calls.count(("write", "OUTP1:STAT 1")), 1)
                self.assertTrue(session.finish()["off_confirmed"])

    def test_temperature_trip_blocks_emission_and_clearing_it_does_not_restart_failed_session(self):
        for mode, _ in MODES:
            with self.subTest(mode=mode):
                session, resource, _, _ = self.make(mode)
                self.prepare(session)
                resource.values["OUTP1:PROT:TEMP:TRIP?"] = "1"
                with self.assertRaises(NktError):
                    session.set_emission(True)
                self.assertTrue(session.failed)
                self.assert_no_on(resource)
                resource.values["OUTP1:PROT:TEMP:TRIP?"] = "0"
                with self.assertRaises(NktError):
                    session.set_emission(True)
                self.assert_no_on(resource)
                self.assertTrue(session.finish()["off_confirmed"])

    def test_temperature_trip_during_formal_guard_still_aborts_in_both_modes(self):
        for mode, _ in MODES:
            with self.subTest(mode=mode):
                session, resource, _, _ = self.make(mode)
                self.prepare(session)
                session.set_emission(True)
                resource.values["OUTP1:PROT:TEMP:TRIP?"] = "1"
                resource.values["OUTP1:STAT?"] = "0"
                with self.assertRaises(NktError):
                    session.verify_point(emitting=True)
                self.assertTrue(session.failed)
                self.assertEqual(resource.calls.count(("write", "OUTP1:STAT 1")), 1)
                self.assertTrue(session.finish()["off_confirmed"])

    def test_off_unknown_and_noninteger_modes_are_rejected_before_ownership(self):
        for raw in ("OFF", "3", "ON", "", "NaN"):
            with self.subTest(raw=raw):
                session, resource, _, _ = self.make()
                resource.values["OUTP1:PROT:TEMP:MODE?"] = raw
                with self.assertRaises(NktError):
                    session.begin(authorize_writes=True, confirm_manual_route=True)
                self.assertFalse(session.owned)
                self.assertIsNone(session.temperature_protection_mode)
                self.assertFalse(any(kind == "write" for kind, _ in resource.calls))
        for value in (0, 3, True, 1.0, "ENAB", None):
            with self.subTest(value=value):
                config = configuration(backend="simulation")
                backend = SimulatedCld(config)
                backend.state["getTempProtMode"] = value
                session = CldController(config, backend)
                self.addCleanup(session.finish)
                with self.assertRaises(NktError):
                    session.begin()
                self.assertFalse(session.owned)
                self.assertIsNone(session.temperature_protection_mode)

    def test_native_backend_uses_the_same_two_mode_controller_guard(self):
        for mode, _ in MODES:
            with self.subTest(mode=mode):
                config = configuration(backend="tl4000", dll_path="fake.dll")
                dll = Dll()
                dll.values["getTempProtMode"] = mode
                driver = TL4000(config, authorize_connection=True, authorize_writes=True, dll=dll)
                clock = Clock()
                session = CldController(config, driver, clock=clock, sleep=clock.sleep)
                self.addCleanup(session.finish)
                self.prepare(session)
                session.set_emission(True)
                self.assertEqual(session.temperature_protection_mode, mode)
                dll.values["getTempProtMode"] = 3 - mode
                with self.assertRaises(NktError):
                    session.verify_point(emitting=True)
                self.assertTrue(session.finish()["off_confirmed"])


if __name__ == "__main__":
    unittest.main()
