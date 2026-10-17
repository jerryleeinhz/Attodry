"""Hardware-blocked CLD USB/SCPI and native TL4000 driver regressions."""
from dataclasses import replace
import ctypes as ct
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from optical_helpers import Clock
from attodry_control.nkt_config import NktError
from attodry_control.optical_config import ScanConfig
from attodry_control.optical_points import OpticalPointConfig, OpticalPointSession
from attodry_control.photoresponse_cld import CldConfig, CldController, CldPoint, TL4000, VisaCld


def configuration(**changes):
    config = CldConfig(backend="visa", points=(CldPoint(.01, 730),),
        dll_path="", address="USB0::FAKE::INSTR", expected_serial="TEST_SERIAL",
        diode_model="FAKE DIODE", diode_wiring_confirmed=True, max_current_a=.03,
        max_tec_current_a=.1, temperature_min_c=20, temperature_max_c=30,
        temperature_c=25, temperature_tolerance_c=.1, stable_dwell_s=.1,
        current_tolerance_a=1e-6, manual_route="FAKE detector only", timeout_s=2,
        poll_interval_s=.02, output_directory=Path("simulation-only"),
        run_name="cld-driver-fake", note="No real hardware")
    return replace(config, **changes)


class Resource:
    """Only the documented CLD SCPI primitives; no discovery/reset support."""
    def __init__(self):
        self.calls, self.closed, self.close_count = [], False, 0
        self.failure = self.ignore_write = None
        self.values = {
            "*IDN?": "Thorlabs,CLD1010LP,TEST_SERIAL,2.3.0",
            "SOUR1:FUNC:MODE?": "CURR", "SOUR2:FUNC:MODE?": "TEMP",
            "UNIT:TEMP?": "CEL", "OUTP1:PROT:TEMP:MODE?": "PROT",
            "OUTP1:STAT?": "0", "OUTP2:STAT?": "0", "OUTP1:PON:STAT?": "0",
            "OUTP1:PROT:INTL:TRIP?": "0", "OUTP1:PROT:TEMP:TRIP?": "0",
            "OUTP1:PROT:CONN:TRIP?": "0", "OUTP2:PROT:CONN:TRIP?": "0",
            "OUTP2:PROT:TRAN:TRIP?": "0", "OUTP2:PROT:OTEM:TRIP?": "0",
            "OUTP2:PROT:COMP:TRIP?": "0", "STAT:MEAS:COND?": "0",
            "SOUR1:CURR:LIM:AMPL?": ".03", "SOUR1:CURR:LEV:IMM:AMPL?": "0",
            "SOUR2:TEMP:SPO?": "25", "SOUR2:CURR:LIM:AMPL?": ".1",
            "SENS2:TEMP:PROT:WIND:AMPL?": ".2", "SENS2:TEMP:DATA?": "25",
            "SENS3:CURR:DC:DATA?": "0", "SENS4:VOLT:DC:DATA?": "0",
            "SOUR2:CURR:DATA?": "0", "SYST:ERR?": '+0,"No error"'}

    def query(self, command):
        self.calls.append(("query", command))
        if self.closed or command == self.failure:
            raise OSError("lost USB response")
        return self.values[command] + "\n"

    def write(self, command):
        self.calls.append(("write", command))
        if self.closed or command == self.failure:
            raise OSError("lost USB write")
        if command != self.ignore_write:
            name, value = command.split(" ", 1)
            self.values[name + "?"] = value
            self.values["SENS3:CURR:DC:DATA?"] = (self.values["SOUR1:CURR:LEV:IMM:AMPL?"]
                if self.values["OUTP1:STAT?"] == "1" else "0")
            self.values["SENS4:VOLT:DC:DATA?"] = "1.2" if self.values["OUTP1:STAT?"] == "1" else "0"
        return len(command)

    def lock_excl(self, timeout):
        self.calls.append(("lock", timeout))

    def close(self):
        self.close_count += 1
        self.closed = True


def visa_backend(config=None, *, writes=True, events=None):
    resource, manager = Resource(), Mock()
    manager.open_resource.return_value = resource
    driver = VisaCld(config or configuration(), authorize_connection=True,
        authorize_writes=writes, manager_factory=lambda: manager,
        on_event=events.append if events is not None else None)
    return driver, resource, manager


def controller(driver):
    clock = Clock()
    result = CldController(driver.config, driver, clock=clock, sleep=clock.sleep)
    return result


class VisaCldDriverTests(unittest.TestCase):
    def test_authority_identity_configuration_and_explicit_usb_gate_before_open(self):
        for config, connect, writes in (
            (configuration(), False, False),
            (configuration(diode_wiring_confirmed=False), True, True),
            (configuration(expected_serial="CHANGE_ME"), True, True),
            (configuration(max_voltage_v=float("nan")), True, True),
            (configuration(address="GPIB0::1::INSTR"), True, True),
            (configuration(backend="tl4000", dll_path="fake.dll"), True, True)):
            with self.subTest(config=config, connect=connect):
                factory = Mock()
                with self.assertRaises(NktError):
                    VisaCld(config, authorize_connection=connect, authorize_writes=writes,
                            manager_factory=factory)
                factory.assert_not_called()

    def test_readonly_snapshot_has_exclusive_lock_no_reset_and_idempotent_close(self):
        driver, resource, manager = visa_backend(writes=False)
        state = driver.snapshot()
        self.assertEqual(state["identity"], ["Thorlabs", "CLD1010LP", "TEST_SERIAL", "2.3.0"])
        self.assertIs(state["getLdOutputState"], False)
        self.assertEqual(state["getTempProtMode"], 2)
        self.assertIn(("lock", 2000), resource.calls)
        manager.open_resource.assert_called_once_with("USB0::FAKE::INSTR", open_timeout=2000)
        self.assertFalse(any(kind == "write" for kind, _ in resource.calls))
        with self.assertRaises(NktError):
            driver.set("setLdCurrSetpoint", .01)
        driver.close(); driver.close()
        self.assertEqual(resource.close_count, 1)
        manager.close.assert_called_once()

    def test_wrong_identity_blocks_writes_and_retains_original_reply(self):
        for identity in ("Other,CLD1010LP,TEST_SERIAL,2.3.0",
                         "Thorlabs,CLD1011LP,TEST_SERIAL,2.3.0",
                         "Thorlabs,CLD1010LP,WRONG,2.3.0",
                         "Thorlabs,CLD1010LP,TEST_SERIAL,"):
            with self.subTest(identity=identity):
                driver, resource, _ = visa_backend()
                resource.values["*IDN?"] = identity
                with self.assertRaises(NktError): driver.set("setLdCurrSetpoint", .01)
                self.assertEqual(driver.transcript[-1]["response"], identity + "\n")
                self.assertFalse(any(kind == "write" for kind, _ in resource.calls))
                driver.close()

    def test_unknown_boolean_enum_and_invalid_numeric_replies_keep_raw_evidence(self):
        for getter, raw in (("getLdOutputState", "2"), ("getTempProtMode", "ON"),
                            ("measLdCurr", "NaN"), ("measLdVolt", "+9.9E37"),
                            ("measTemp", "not a number")):
            with self.subTest(getter=getter, raw=raw):
                events = []
                driver, resource, _ = visa_backend(writes=False, events=events)
                resource.values[driver.QUERIES[getter]] = raw
                with self.assertRaises(NktError): driver.get(getter)
                self.assertEqual(driver.transcript[-1]["response"], raw + "\n")
                self.assertEqual(events[-1]["response"], raw + "\n")
                driver.close()

    def test_protection_and_temperature_faults_prevent_emission(self):
        for command, value in (("OUTP1:PROT:INTL:TRIP?", "1"),
                               ("OUTP2:PROT:TRAN:TRIP?", "1"),
                               ("SENS2:TEMP:DATA?", "31"),
                               ("SENS2:TEMP:PROT:WIND:AMPL?", "10.00001"),
                               ("OUTP1:PROT:TEMP:MODE?", "ENAB")):
            with self.subTest(command=command):
                driver, resource, _ = visa_backend()
                resource.values[command] = value
                session = controller(driver)
                with self.assertRaises(NktError):
                    session.begin(authorize_writes=True, confirm_manual_route=True)
                self.assertFalse(session.owned)
                self.assertFalse(any(kind == "write" for kind, _ in resource.calls))
                session.finish()

    def test_ld_temperature_or_unknown_connection_blocks_ld_but_cleanup_remains(self):
        for command in ("OUTP1:PROT:TEMP:TRIP?", "OUTP1:PROT:CONN:TRIP?",
                        "OUTP2:PROT:CONN:TRIP?"):
            with self.subTest(command=command):
                driver, resource, _ = visa_backend()
                resource.values[command] = "1"
                session = controller(driver)
                session.begin(authorize_writes=True, confirm_manual_route=True)
                with self.assertRaises(NktError): session.prepare_point(driver.config.points[0])
                self.assertNotIn(("write", "OUTP1:STAT 1"), resource.calls)
                self.assertTrue(session.finish()["off_confirmed"])

    def test_normal_acquisition_cleanup_zeroes_ld_restores_tec_and_closes_once(self):
        for tec in (False, True):
            with self.subTest(tec=tec):
                driver, resource, manager = visa_backend()
                resource.values["OUTP2:STAT?"] = str(int(tec))
                session = controller(driver)
                session.begin(authorize_writes=True, confirm_manual_route=True)
                session.prepare_point(driver.config.points[0]); session.set_emission(True)
                self.assertEqual(resource.values["OUTP1:STAT?"], "1")
                cleanup = session.finish(); session.finish()
                self.assertTrue(cleanup["off_confirmed"])
                self.assertEqual(cleanup["errors"], [])
                self.assertEqual(resource.values["OUTP1:STAT?"], "0")
                self.assertEqual(float(resource.values["SOUR1:CURR:LEV:IMM:AMPL?"]), 0)
                self.assertEqual(resource.values["OUTP2:STAT?"], str(int(tec)))
                self.assertEqual(resource.close_count, 1)
                manager.close.assert_called_once()

    def test_lost_or_poisoned_response_only_permits_best_effort_off_not_certificate(self):
        for response in (None, "25\x00"):
            with self.subTest(response=response):
                driver, resource, _ = visa_backend()
                session = controller(driver)
                session.begin(authorize_writes=True, confirm_manual_route=True)
                session.prepare_point(driver.config.points[0]); session.set_emission(True)
                if response is None:
                    resource.failure = "SENS2:TEMP:DATA?"
                else:
                    resource.values["SENS2:TEMP:DATA?"] = response
                with self.assertRaises((NktError, OSError)): session.verify_point(emitting=True)
                failed_row = next(row for row in reversed(driver.transcript) if "error" in row)
                self.assertEqual(failed_row["response"], None if response is None else response + "\n")
                before = len(resource.calls)
                with self.assertRaisesRegex(NktError, "pending response"): driver.get("measTemp")
                self.assertEqual(len(resource.calls), before)
                cleanup = session.finish()
                self.assertFalse(cleanup["off_confirmed"])
                self.assertTrue(cleanup["errors"])
                self.assertIn(("write", "OUTP1:STAT 0"), resource.calls[before:])
                self.assertIn(("write", "OUTP2:STAT 0"), resource.calls[before:])
                self.assertTrue(resource.closed)
                self.assertTrue(session.record["last_confirmed_state"]["getLdOutputState"])
                self.assertFalse(session.record["state_current"])

    def test_failed_off_does_not_skip_independent_tec_and_close(self):
        driver, resource, manager = visa_backend()
        session = controller(driver)
        session.begin(authorize_writes=True, confirm_manual_route=True)
        session.prepare_point(driver.config.points[0]); session.set_emission(True)
        resource.failure = "OUTP1:STAT 0"
        manager.close.side_effect = OSError("fake manager close failure")
        cleanup = session.finish()
        self.assertFalse(cleanup["off_confirmed"])
        self.assertFalse(cleanup["transport_closed"])
        self.assertIn(("write", "OUTP2:STAT 0"), resource.calls)
        self.assertTrue(any("LD off" in error for error in cleanup["errors"]))
        self.assertTrue(any("close" in error for error in cleanup["errors"]))
        self.assertTrue(resource.closed)


class Dll:
    """ctypes pointer API fake, including partial init and failed status writes."""
    def __init__(self):
        self.calls, self.statuses = [], {}
        self.identity = [b"Thorlabs", b"CLD1010LP", b"TEST_SERIAL", b"2.3.0"]
        self.values = {name: 0 for name in TL4000.SHORT_GETTERS | TL4000.BOOL_GETTERS
                       | TL4000.DOUBLE_GETTERS | TL4000.SETTING_GETTERS}
        self.values.update(getTecOperatingMode=1, getTempProtMode=2, getLdCurrLimit=.03,
            getTempSetpoint=25, getTecCurrLimit=.1, getTempProtWindow=.2, measTemp=25)

    def __getattr__(self, function):
        def call(*args):
            name = function.removeprefix("TL4000_")
            self.calls.append((name, args))
            if name == "init": args[3]._obj.value = 42
            elif name == "identificationQuery":
                for pointer, value in zip(args[1:], self.identity): pointer.value = value
            elif name in self.values: args[-1]._obj.value = self.values[name]
            elif name.startswith("set") and not self.statuses.get(name):
                self.values["get" + name[3:]] = args[-1].value
            elif name.startswith("switch") and not self.statuses.get(name):
                self.values["get" + name[6:] + "State"] = args[-1].value
            return self.statuses.get(name, 0)
        return call


def native_backend(*, writes=True, events=None, dll=None):
    dll = dll or Dll()
    driver = TL4000(configuration(backend="tl4000", dll_path="fake.dll"),
        authorize_connection=True, authorize_writes=writes, dll=dll,
        on_event=events.append if events is not None else None)
    return driver, dll


class NativeCldDriverTests(unittest.TestCase):
    def test_native_init_is_64bit_id_query_without_reset_and_partial_init_closes(self):
        driver, dll = native_backend()
        init = dll.calls[0][1]
        self.assertEqual((init[1].value, init[2].value), (1, 0))
        self.assertEqual(driver.session.value, 42)
        driver.close(); driver.close()
        self.assertEqual([name for name, _ in dll.calls].count("close"), 1)
        broken = Dll(); broken.statuses["init"] = -1
        events = []
        with self.assertRaisesRegex(NktError, "init returned -1"):
            native_backend(dll=broken, events=events)
        self.assertEqual([name for name, _ in broken.calls], ["init", "close"])
        self.assertEqual(events[0]["status"], -1)
        self.assertIn("error", events[0])
        with patch("attodry_control.photoresponse_cld.ct.sizeof", return_value=4), patch("ctypes.CDLL") as load:
            with self.assertRaisesRegex(NktError, "64-bit"):
                TL4000(configuration(backend="tl4000", dll_path="fake.dll"),
                       authorize_connection=True, authorize_writes=True)
            load.assert_not_called()

    def test_native_init_implicit_writes_require_explicit_authorization_before_dll(self):
        config = configuration(backend="tl4000", dll_path="fake.dll")
        for permission in (False, None, 1, "yes"):
            with self.subTest(permission=permission):
                dll = Dll()
                with patch("ctypes.CDLL") as load:
                    with self.assertRaisesRegex(NktError, "initialization.*write authorization"):
                        TL4000(config, authorize_connection=True, authorize_writes=permission)
                    load.assert_not_called()
                with self.assertRaisesRegex(NktError, "initialization.*write authorization"):
                    TL4000(config, authorize_connection=True, authorize_writes=permission, dll=dll)
                self.assertEqual(dll.calls, [])

    def test_native_authority_and_wiring_fail_before_any_api_call(self):
        for config, connect, writes in ((configuration(backend="tl4000", dll_path="fake.dll"), False, False),
            (configuration(backend="tl4000", dll_path="fake.dll", diode_wiring_confirmed=False), True, True)):
            dll = Dll()
            with self.assertRaises(NktError):
                TL4000(config, authorize_connection=connect, authorize_writes=writes, dll=dll)
            self.assertEqual(dll.calls, [])
        driver, dll = native_backend()
        # Authorized SDK initialization is distinct from permission for a later
        # setting write; the existing per-set guard remains in force.
        driver.writes = False
        before = len(dll.calls)
        with self.assertRaises(NktError): driver.set("switchLdOutput", False)
        self.assertEqual(len(dll.calls), before)
        driver.close()

    def test_rejected_native_identity_preserves_all_four_raw_outputs_before_event(self):
        for index, value in ((1, b"WRONG_MODEL"), (2, b"WRONG_SERIAL"), (0, b"\xff")):
            with self.subTest(index=index):
                events = []
                driver, dll = native_backend(events=events)
                dll.identity[index] = value
                before = len(dll.calls)
                with self.assertRaises((NktError, UnicodeDecodeError)): driver.snapshot()
                self.assertEqual(len(dll.calls) - before, 1)
                row = driver.transcript[-1]
                self.assertEqual(row, events[-1])
                self.assertEqual(row["raw_identity_hex"], [value.hex() for value in dll.identity])
                self.assertEqual(len(row["raw_identity"]), 4)
                self.assertIn("error", row)
                json.dumps(row, allow_nan=False)
                driver.close()

    def test_native_boolean_invalidnumeric_and_failed_get_keep_same_transaction_raw(self):
        for getter, value, status in (("getLdOutputState", 2, 0), ("measTemp", float("nan"), 0),
                ("measLdCurr", 9.9e37, 0), ("getTecOutputState", 1, -7)):
            with self.subTest(getter=getter):
                events = []
                driver, dll = native_backend(events=events)
                dll.values[getter], dll.statuses[getter] = value, status
                before = len(dll.calls)
                with self.assertRaises(NktError): driver.get(getter)
                self.assertEqual(len(dll.calls) - before, 1)
                row = driver.transcript[-1]
                self.assertEqual(row, events[-1])
                self.assertEqual(row["function"], getter)
                self.assertEqual(row["raw_value"], "nan" if isinstance(value, float) and value != value else value)
                self.assertEqual(row["status"], status)
                self.assertIn("error", row)
                json.dumps(row, allow_nan=False)
                driver.close()

    def test_native_failed_set_records_requested_value_before_event(self):
        events = []
        driver, dll = native_backend(events=events)
        dll.statuses["setLdCurrSetpoint"] = -3
        with self.assertRaises(NktError): driver.set("setLdCurrSetpoint", .0123)
        row = driver.transcript[-1]
        self.assertEqual(row, events[-1])
        self.assertEqual((row["function"], row["requested"], row["status"]),
                         ("setLdCurrSetpoint", .0123, -3))
        self.assertIn("error", row)
        driver.set("switchLdOutput", False)
        self.assertIs(driver.transcript[-1]["requested"], False)
        driver.close()

    def test_native_boolean_abi_and_shared_controller_cleanup(self):
        driver, dll = native_backend()
        dll.values["isTrippedLdOutputProtTemperature"] = 1
        self.assertIs(driver.get("isTrippedLdOutputProtTemperature"), True)
        dll.values["isTrippedLdOutputProtTemperature"] = 0
        session = controller(driver)
        session.begin(authorize_writes=True, confirm_manual_route=True)
        session.prepare_point(driver.config.points[0]); session.set_emission(True)
        cleanup = session.finish()
        self.assertTrue(cleanup["off_confirmed"])
        self.assertEqual(cleanup["errors"], [])
        self.assertEqual(dll.values["getLdCurrSetpoint"], 0)
        self.assertEqual(dll.values["getLdOutputState"], 0)
        self.assertEqual(dll.values["getTecOutputState"], 0)

    def test_rejected_native_raw_evidence_survives_both_final_audit_paths(self):
        for with_sink in (False, True):
            with self.subTest(with_sink=with_sink):
                api_events, events = [], []
                driver, dll = native_backend(events=api_events)
                dll.statuses["setLdCurrSetpoint"] = -3
                with self.assertRaises(NktError): driver.set("setLdCurrSetpoint", .0123)
                dll.values["getLdOutputState"] = 2
                config = OpticalPointConfig(driver.config, ScanConfig("direct", False, False, .01,
                                                                      laser_source="cld"))
                clock = Clock()
                session = OpticalPointSession(config,
                    (lambda kind, value: events.append((kind, value))) if with_sink else None,
                    backend_factory=lambda _: driver, clock=clock, sleep=clock.sleep)
                with self.assertRaisesRegex(NktError, "boolean"):
                    session.open(authorize_writes=True, confirm_manual_route=True)
                cleanup = session.cleanup()
                audit = (next(value for kind, value in events if kind == "optical_audit")
                         if with_sink else cleanup["optical_audit"])
                raw = audit["laser_transcript"]
                self.assertTrue(any(row.get("function") == "getLdOutputState"
                    and row.get("raw_value") == 2 and "error" in row for row in raw))
                self.assertTrue(any(row.get("requested") == .0123 and row.get("status") == -3 for row in raw))
                self.assertTrue(any(row.get("raw_value") == 2 for row in api_events))
                self.assertFalse(cleanup["off_confirmed"])
                self.assertFalse(any(name == "switchLdOutput" for name, _ in dll.calls))
                self.assertIn("close", [name for name, _ in dll.calls])
                json.dumps(audit, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
