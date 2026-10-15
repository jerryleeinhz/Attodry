"""Offline USB/SCPI regressions; no VISA library or real instruments are used."""
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import tomllib
import unittest
from unittest.mock import Mock, patch

from attodry_control.nkt_config import NktError
from attodry_control.photoresponse_cld import CldController, SimulatedCld, TL4000, VisaCld, load_cld_config
from attodry_control.photoresponse_cli import _run
from attodry_control.photoresponse_config import load_photoresponse_config


ROOT = Path(__file__).resolve().parents[1]


def configuration(**changes):
    synthetic = load_photoresponse_config(ROOT / "config/photoresponse_cld_simulation.toml").nkt
    fields = dict(backend="visa", dll_path="", address="USB0::FAKE::INSTR",
                  expected_serial="TEST_SERIAL", diode_model="FAKE DIODE",
                  diode_wiring_confirmed=True)
    fields.update(changes)
    return replace(synthetic, **fields)


class Resource:
    def __init__(self):
        self.calls = []
        self.closed = False
        self.failure = None
        self.ignore_write = None
        self.values = {
            "*IDN?": "Thorlabs,CLD1010LP,TEST_SERIAL,2.3.0",
            "SOUR1:FUNC:MODE?": "CURR", "SOUR2:FUNC:MODE?": "TEMP",
            "UNIT:TEMP?": "CEL", "OUTP1:PROT:TEMP:MODE?": "PROT",
            "OUTP1:STAT?": "0", "OUTP2:STAT?": "0", "OUTP1:PON:STAT?": "0",
            "OUTP1:PROT:INTL:TRIP?": "0", "SOUR1:CURR:LIM:AMPL?": "+3.000000E-02",
            "OUTP1:PROT:TEMP:TRIP?": "0", "OUTP1:PROT:CONN:TRIP?": "0",
            "OUTP2:PROT:CONN:TRIP?": "0", "OUTP2:PROT:TRAN:TRIP?": "0",
            "OUTP2:PROT:OTEM:TRIP?": "0", "OUTP2:PROT:COMP:TRIP?": "0",
            "SOUR1:CURR:LEV:IMM:AMPL?": "+0.000000E+00",
            "SOUR2:TEMP:SPO?": "+2.500000E+01", "SOUR2:CURR:LIM:AMPL?": "+1.000000E-01",
            "SENS2:TEMP:PROT:WIND:AMPL?": "+2.000000E-01", "SENS2:TEMP:DATA?": "+2.500000E+01",
            "SENS3:CURR:DC:DATA?": "+0.000000E+00", "SENS4:VOLT:DC:DATA?": "+0.000000E+00",
            "SOUR2:CURR:DATA?": "+0.000000E+00", "STAT:MEAS:COND?": "0",
            "SYST:ERR?": '+0,"No error"',
        }

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
            path, value = command.split(" ", 1)
            self.values[path + "?"] = value
            if self.values["OUTP1:STAT?"] == "1":
                self.values["SENS3:CURR:DC:DATA?"] = self.values["SOUR1:CURR:LEV:IMM:AMPL?"]
                self.values["SENS4:VOLT:DC:DATA?"] = "1.2"
            else:
                self.values["SENS3:CURR:DC:DATA?"] = "0"
                self.values["SENS4:VOLT:DC:DATA?"] = "0"
        return len(command)

    def lock_excl(self, timeout):
        self.calls.append(("lock", timeout))

    def close(self):
        self.closed = True


class Clock:
    def __init__(self): self.now = 0.
    def __call__(self): return self.now
    def sleep(self, interval): self.now += interval


def backend(config=None, writes=True):
    resource = Resource()
    manager = Mock()
    manager.open_resource.return_value = resource
    driver = VisaCld(config or configuration(), authorize_connection=True,
                     authorize_writes=writes, manager_factory=lambda: manager)
    return driver, resource, manager


class VisaCldTests(unittest.TestCase):
    def test_configuration_and_permission_fail_before_resource_manager(self):
        for config, connect, writes in (
            (configuration(), False, False),
            (configuration(diode_wiring_confirmed=False), True, True),
            (configuration(expected_serial="CHANGE_ME"), True, True),
            (configuration(max_voltage_v=float("nan")), True, True),
            (configuration(address="GPIB0::1::INSTR"), True, True),
            (configuration(backend="tl4000", dll_path="fake.dll"), True, True),
        ):
            with self.subTest(config=config, connect=connect, writes=writes):
                factory = Mock()
                with self.assertRaises(NktError):
                    VisaCld(config, authorize_connection=connect, authorize_writes=writes,
                            manager_factory=factory)
                factory.assert_not_called()

    def test_usb_identity_and_real_style_snapshot_without_dll_or_writes(self):
        with patch("ctypes.CDLL") as load:
            driver, resource, manager = backend(writes=False)
            state = driver.snapshot()
            self.assertEqual(state["identity"], ["Thorlabs", "CLD1010LP", "TEST_SERIAL", "2.3.0"])
            self.assertEqual(state["getTempProtMode"], 2)
            self.assertEqual(state["getTecOperatingMode"], 1)
            self.assertEqual(state["getTempUnit"], 0)
            self.assertAlmostEqual(state["getLdCurrLimit"], .03)
            self.assertFalse(state["getLdOutputState"])
            self.assertFalse(any(kind == "write" for kind, _ in resource.calls))
            self.assertEqual(resource.read_termination, "\n")
            manager.open_resource.assert_called_once_with("USB0::FAKE::INSTR", open_timeout=5000)
            with self.assertRaises(NktError): driver.set("setLdCurrSetpoint", 0.)
            driver.close(); driver.close()
            manager.close.assert_called_once()
            self.assertTrue(resource.closed)
            load.assert_not_called()

    def test_optional_dll_field_and_existing_physical_gates(self):
        document = tomllib.loads((ROOT / "config/photoresponse_cld_simulation.toml").read_text(encoding="utf-8"))
        del document["cld1010"]["dll_path"]
        document["cld1010"]["backend"] = "visa"
        config = load_cld_config(document, ROOT / "config/any.local.toml")
        self.assertEqual(config.dll_path, "")
        with self.assertRaises(NktError): config.require_hardware(writes=True)
        with self.assertRaises(NktError): configuration(diode_model="CHANGE_ME").require_hardware(writes=True)
        with self.assertRaises(NktError): configuration(backend="tl4000").require_hardware()

    def test_thermal_timeout_does_not_extend_usb_transaction_timeout(self):
        driver, resource, manager = backend(configuration(timeout_s=120.))
        self.assertEqual(resource.timeout, 10000)
        manager.open_resource.assert_called_once_with("USB0::FAKE::INSTR", open_timeout=10000)
        driver.close()

    def test_omitted_voltage_threshold_loads_as_none_and_hardware_ready(self):
        document = tomllib.loads((ROOT / "config/photoresponse_cld_simulation.toml").read_text(encoding="utf-8"))
        table = document["cld1010"]
        del table["max_voltage_v"]
        table.update(backend="visa", address="USB0::FAKE::INSTR", expected_serial="TEST_SERIAL",
                     diode_model="FAKE DIODE", diode_wiring_confirmed=True)
        config = load_cld_config(document, ROOT / "config/any.local.toml")
        config.require_hardware(writes=True)
        self.assertIsNone(config.max_voltage_v)
        self.assertIsNone(config.snapshot()["max_voltage_v"])

    def test_no_voltage_threshold_allows_normal_ld_and_cleanup(self):
        driver, resource, _ = backend(configuration(max_voltage_v=None))
        clock = Clock()
        controller = CldController(driver.config, driver, clock=clock, sleep=clock.sleep)
        controller.begin(authorize_writes=True, confirm_manual_route=True)
        controller.prepare_point(driver.config.points[0]); controller.set_emission(True)
        self.assertEqual(controller.record["last_confirmed_state"]["measLdVolt"], 1.2)
        self.assertIsNone(controller.config.snapshot()["max_voltage_v"])
        self.assertTrue(controller.finish()["off_confirmed"])
        self.assertEqual(resource.values["OUTP1:STAT?"], "0")

    def test_explicit_voltage_threshold_still_rejects_excess_voltage(self):
        driver, _, _ = backend(configuration(max_voltage_v=.6))
        clock = Clock()
        controller = CldController(driver.config, driver, clock=clock, sleep=clock.sleep)
        controller.begin(authorize_writes=True, confirm_manual_route=True)
        controller.prepare_point(driver.config.points[0])
        with self.assertRaisesRegex(NktError, "diode voltage"):
            controller.set_emission(True)
        self.assertTrue(controller.failed)
        self.assertTrue(controller.finish()["off_confirmed"])

    def test_invalid_optional_voltage_threshold_stops_before_io(self):
        for value in (float("nan"), float("inf"), -1., 0., "3.0", True):
            with self.subTest(value=value):
                factory = Mock()
                with self.assertRaises(NktError):
                    VisaCld(configuration(max_voltage_v=value), authorize_connection=True,
                            authorize_writes=True, manager_factory=factory)
                factory.assert_not_called()

    def test_invalid_voltage_response_still_rejected_without_threshold(self):
        for value in ("NaN", "Inf", "-Inf", "invalid", "-0.01", "+9.9E37", "-9.9E37", "+9.91E37"):
            with self.subTest(value=value):
                driver, resource, _ = backend(configuration(max_voltage_v=None))
                resource.values["SENS4:VOLT:DC:DATA?"] = value
                controller = CldController(driver.config, driver)
                with self.assertRaises(NktError):
                    controller.begin(authorize_writes=True, confirm_manual_route=True)
                self.assertFalse(controller.owned)
                self.assertFalse(any(kind == "write" for kind, _ in resource.calls))
                controller.finish()

    def test_no_voltage_threshold_preserves_current_tec_and_connection_guards(self):
        for command, value in (("SENS3:CURR:DC:DATA?", ".031"), ("OUTP2:PROT:TRAN:TRIP?", "1"),
                               ("OUTP1:PROT:INTL:TRIP?", "1"), ("OUTP2:PROT:CONN:TRIP?", "1")):
            with self.subTest(command=command):
                driver, resource, _ = backend(configuration(max_voltage_v=None))
                resource.values[command] = value
                clock = Clock()
                controller = CldController(driver.config, driver, clock=clock, sleep=clock.sleep)
                with self.assertRaises(NktError):
                    controller.begin(authorize_writes=True, confirm_manual_route=True)
                    controller.prepare_point(driver.config.points[0])
                self.assertNotIn(("write", "OUTP1:STAT 1"), resource.calls)
                controller.finish()

    def test_simulation_voltage_is_not_derived_from_optional_threshold(self):
        states = []
        for limit in (None, .5, 3.):
            config = configuration(backend="simulation", max_voltage_v=limit)
            synthetic = SimulatedCld(config)
            clock = Clock()
            controller = CldController(config, synthetic, clock=clock, sleep=clock.sleep)
            controller.begin(); controller.prepare_point(config.points[0]); controller.set_emission(True)
            states.append(controller.record["last_confirmed_state"]["measLdVolt"])
            self.assertTrue(controller.finish()["off_confirmed"])
        self.assertEqual(states, [states[0]]*3)

    def test_shared_voltage_validation_rejects_overload_without_threshold(self):
        config = configuration(backend="simulation", max_voltage_v=None)
        for value in (float("nan"), float("inf"), 9.9e37, -0.01):
            synthetic = SimulatedCld(config)
            synthetic.state["measLdVolt"] = value
            controller = CldController(config, synthetic)
            with self.assertRaises(NktError): controller.begin()
            self.assertFalse(controller.owned)
            controller.finish()

    def test_wrong_identity_prevents_any_setting_write(self):
        for identity in ("Thorlabs,CLD1011LP,TEST_SERIAL,2.3.0", "Thorlabs,CLD1010LP,WRONG,2.3.0",
                         "Other,CLD1010LP,TEST_SERIAL,2.3.0", "Thorlabs,CLD1010LP,TEST_SERIAL"):
            driver, resource, _ = backend()
            resource.values["*IDN?"] = identity
            with self.assertRaises(NktError): driver.set("setLdCurrSetpoint", .01)
            self.assertFalse(any(kind == "write" for kind, _ in resource.calls))
            driver.close()

    def test_unknown_enums_booleans_and_nonfinite_values_rejected(self):
        for name, response in (("getTempProtMode", "ON"), ("getTempUnit", "BANANA"),
                               ("getLdOperatingMode", "2"), ("getTecOperatingMode", "AUTO"),
                               ("getLdOutputState", "2"), ("measLdCurr", "NaN"),
                               ("measLdVolt", "Inf"), ("measTemp", "not a number")):
            driver, resource, _ = backend(writes=False)
            resource.values[driver.QUERIES[name]] = response
            with self.assertRaises(NktError): driver.get(name)
            driver.close()

    def test_enable_protection_enum_does_not_count_as_protection(self):
        driver, resource, _ = backend()
        resource.values["OUTP1:PROT:TEMP:MODE?"] = "ENAB"
        self.assertEqual(driver.get("getTempProtMode"), 1)
        controller = CldController(driver.config, driver)
        with self.assertRaises(NktError): controller.begin(authorize_writes=True, confirm_manual_route=True)
        self.assertFalse(controller.owned)
        self.assertFalse(any(kind == "write" for kind, _ in resource.calls))
        controller.finish()

    def test_tec_protection_trip_prevents_all_output_enable_writes(self):
        for command in ("OUTP2:PROT:TRAN:TRIP?",
                        "OUTP2:PROT:OTEM:TRIP?", "OUTP2:PROT:COMP:TRIP?"):
            with self.subTest(command=command):
                driver, resource, _ = backend()
                resource.values[command] = "1"
                controller = CldController(driver.config, driver)
                with self.assertRaisesRegex(NktError, "TEC protection"):
                    controller.begin(authorize_writes=True, confirm_manual_route=True)
                self.assertFalse(controller.owned)
                self.assertFalse(any(kind == "write" for kind, _ in resource.calls))
                self.assertIsNotNone(controller.record["last_confirmed_state"])
                controller.finish()

    def test_real_connected_tec_snapshot_retains_unqualified_connection_bits(self):
        driver, resource, _ = backend(configuration(temperature_c=26., temperature_min_c=15.,
                                                   temperature_max_c=50., max_tec_current_a=.85))
        resource.values.update({"OUTP1:PROT:CONN:TRIP?": "1", "OUTP2:PROT:CONN:TRIP?": "1",
            "STAT:MEAS:COND?": "8192", "OUTP2:STAT?": "1", "SOUR2:TEMP:SPO?": "26",
            "SENS2:TEMP:DATA?": "26.00024", "SOUR2:CURR:DATA?": ".1578135",
            "SOUR2:CURR:LIM:AMPL?": ".85", "SENS2:TEMP:PROT:WIND:AMPL?": "1"})
        state = driver.snapshot()
        self.assertNotIn("isTrippedTecOutputProtCable", state)
        self.assertEqual(state["diagnostics"]["ld_connection_raw"], 1)
        self.assertEqual(state["diagnostics"]["tec_connection_raw"], 1)
        self.assertEqual(state["diagnostics"]["measurement_condition_raw"], 8192)
        self.assertEqual(state["diagnostics"]["qualification"], "unqualified")
        self.assertTrue(state["diagnostics"]["reason"])
        self.assertAlmostEqual(state["measTecCurr"], .1578135)
        self.assertFalse(state["isTrippedLdOutputProtTemperature"])
        clock = Clock()
        controller = CldController(driver.config, driver, clock=clock, sleep=clock.sleep)
        controller.begin(authorize_writes=True, confirm_manual_route=True)
        with self.assertRaisesRegex(NktError, "connection diagnostic unresolved"):
            controller.prepare_point(driver.config.points[0])
        with self.assertRaisesRegex(NktError, "connection diagnostic unresolved"):
            driver.set("switchLdOutput", True)
        self.assertFalse(any(kind == "write" and command in ("OUTP1:STAT 1", "OUTP2:STAT 1")
                             for kind, command in resource.calls))
        self.assertEqual(controller.record["last_confirmed_state"]["diagnostics"], state["diagnostics"])
        self.assertTrue(controller.finish()["off_confirmed"])

    def test_temperature_window_is_full_span_with_inclusive_limits(self):
        driver, resource, _ = backend()
        controller = CldController(driver.config, driver)
        resource.values["SENS2:TEMP:PROT:WIND:AMPL?"] = "10"  # 25 +/-5, exactly 20..30 C.
        controller.healthy(driver.snapshot())
        resource.values["SENS2:TEMP:PROT:WIND:AMPL?"] = "10.00001"
        with self.assertRaises(NktError): controller.healthy(driver.snapshot())
        resource.values["SENS2:TEMP:PROT:WIND:AMPL?"] = ".2"  # tolerance .1 equals half-span.
        controller.healthy(driver.snapshot())
        resource.values["SENS2:TEMP:PROT:WIND:AMPL?"] = ".19999"
        with self.assertRaisesRegex(NktError, "tolerance"):
            controller.healthy(driver.snapshot())
        driver.close()

    def test_ld_temperature_trip_allows_warmup_but_blocks_enable(self):
        driver, resource, _ = backend()
        resource.values["OUTP1:PROT:TEMP:TRIP?"] = "1"
        clock = Clock()
        controller = CldController(driver.config, driver, clock=clock, sleep=clock.sleep)
        controller.begin(authorize_writes=True, confirm_manual_route=True)
        self.assertIn(("write", "OUTP2:STAT 1"), resource.calls)
        with self.assertRaisesRegex(NktError, "temperature protection"):
            controller.prepare_point(driver.config.points[0])
        with self.assertRaisesRegex(NktError, "temperature protection"):
            driver.set("switchLdOutput", True)
        self.assertNotIn(("write", "OUTP1:STAT 1"), resource.calls)
        self.assertTrue(controller.finish()["off_confirmed"])

    def test_ld_temperature_trip_during_emission_faults_controller(self):
        driver, resource, _ = backend()
        clock = Clock()
        controller = CldController(driver.config, driver, clock=clock, sleep=clock.sleep)
        controller.begin(authorize_writes=True, confirm_manual_route=True)
        controller.prepare_point(driver.config.points[0]); controller.set_emission(True)
        resource.values["OUTP1:PROT:TEMP:TRIP?"] = "1"
        with self.assertRaisesRegex(NktError, "temperature protection"):
            controller.verify_point(emitting=True)
        self.assertTrue(controller.failed)
        self.assertTrue(controller.finish()["off_confirmed"])

    def test_native_and_simulated_ld_temperature_contract(self):
        self.assertIn("isTrippedLdOutputProtTemperature", TL4000.BOOL_GETTERS)
        config = configuration(backend="simulation")
        synthetic = SimulatedCld(config)
        self.assertFalse(synthetic.snapshot()["isTrippedLdOutputProtTemperature"])
        self.assertEqual(synthetic.snapshot()["getTempProtWindow"], 2*config.temperature_tolerance_c)
        clock = Clock()
        controller = CldController(config, synthetic, clock=clock, sleep=clock.sleep)
        synthetic.state["isTrippedLdOutputProtTemperature"] = True
        controller.begin()
        with self.assertRaisesRegex(NktError, "temperature protection"):
            controller.prepare_point(config.points[0])
        controller.finish()

    def test_native_ld_temperature_getter_has_boolean_abi(self):
        class Dll:
            def __getattr__(self, name):
                def call(*args):
                    if name == "TL4000_init": args[3]._obj.value = 42
                    if name == "TL4000_isTrippedLdOutputProtTemperature":
                        args[1]._obj.value = 1
                    return 0
                return call
        config = configuration(backend="tl4000", dll_path="fake.dll")
        driver = TL4000(config, authorize_connection=True, dll=Dll())
        self.assertIs(driver.get("isTrippedLdOutputProtTemperature"), True)
        driver.close()

    def test_operator_acknowledged_exact_pattern_allows_ld_and_keeps_other_guards(self):
        ack = dict(acknowledged_connection_diagnostics=(1, 1, 8192),
                   acknowledgement_note="operator 2026-10-15: manual emission works, no front-panel error")
        asserted = {"OUTP1:PROT:CONN:TRIP?": "1", "OUTP2:PROT:CONN:TRIP?": "1", "STAT:MEAS:COND?": "8192"}
        driver, resource, _ = backend(configuration(**ack))
        resource.values.update(asserted)
        clock = Clock()
        controller = CldController(driver.config, driver, clock=clock, sleep=clock.sleep)
        controller.begin(authorize_writes=True, confirm_manual_route=True)
        controller.prepare_point(driver.config.points[0])
        controller.set_emission(True)
        self.assertIn(("write", "OUTP1:STAT 1"), resource.calls)
        diagnostics = controller.record["last_confirmed_state"]["diagnostics"]
        self.assertEqual(diagnostics["measurement_condition_raw"], 8192)
        self.assertEqual(diagnostics["acknowledged_pattern"], [1, 1, 8192])
        self.assertIn("manual emission", diagnostics["acknowledgement_note"])
        self.assertTrue(controller.finish()["off_confirmed"])
        self.assertEqual(resource.values["OUTP1:STAT?"], "0")

        for changes in ({"STAT:MEAS:COND?": "8193"}, {"OUTP2:PROT:CONN:TRIP?": "0"},
                        {"OUTP1:PROT:CONN:TRIP?": "0", "STAT:MEAS:COND?": "8192"}):
            with self.subTest(changes=changes):
                driver, resource, _ = backend(configuration(**ack))
                resource.values.update({**asserted, **changes})
                with self.assertRaisesRegex(NktError, "connection diagnostic unresolved"):
                    driver.set("switchLdOutput", True)
                self.assertNotIn(("write", "OUTP1:STAT 1"), resource.calls)
                driver.close()

        driver, resource, _ = backend(configuration(**ack))
        resource.values.update({**asserted, "OUTP1:PROT:INTL:TRIP?": "1"})
        controller = CldController(driver.config, driver, clock=clock, sleep=clock.sleep)
        with self.assertRaisesRegex(NktError, "interlock"):
            controller.begin(authorize_writes=True, confirm_manual_route=True)
        self.assertFalse(any(kind == "write" for kind, _ in resource.calls))
        driver.close()

    def test_acknowledgement_configuration_is_strict(self):
        for changes in ({"acknowledgement_note": "note without a pattern"},
                        {"acknowledged_connection_diagnostics": (1, 1, 8192)},
                        {"acknowledged_connection_diagnostics": (2, 1, 8192), "acknowledgement_note": "x"},
                        {"acknowledged_connection_diagnostics": (1, 1, 70000), "acknowledgement_note": "x"},
                        {"acknowledged_connection_diagnostics": (True, 1, 8192), "acknowledgement_note": "x"},
                        {"acknowledged_connection_diagnostics": (1, 8192), "acknowledgement_note": "x"},
                        {"acknowledged_connection_diagnostics": [1, 1, 8192], "acknowledgement_note": "x"}):
            with self.subTest(changes=changes):
                with self.assertRaises(NktError):
                    configuration(**changes).validate()
        with (ROOT / "config/photoresponse_cld_simulation.toml").open("rb") as stream:
            doc = tomllib.load(stream)
        doc["cld1010"].update(acknowledged_connection_diagnostics=[1, 1, 8192], acknowledgement_note="operator ack")
        loaded = load_cld_config(doc, ROOT / "config/photoresponse_cld_simulation.toml")
        self.assertEqual(loaded.acknowledged_connection_diagnostics, (1, 1, 8192))
        self.assertIsNone(load_cld_config({**doc, "cld1010": {k: v for k, v in doc["cld1010"].items()
            if k not in ("acknowledged_connection_diagnostics", "acknowledgement_note")}},
            ROOT / "config/photoresponse_cld_simulation.toml").acknowledged_connection_diagnostics)

    def test_asserted_or_missing_connection_diagnostics_block_formal_stage(self):
        driver, _, _ = backend()
        controller = CldController(driver.config, driver)
        state = driver.snapshot()
        for changes in ({"ld_connection_raw": 1}, {"tec_connection_raw": 1},
                        {"measurement_condition_raw": 8192}, {"qualification": None},
                        {"measurement_condition_raw": None}, {"ld_connection_raw": False}):
            with self.subTest(changes=changes):
                state["diagnostics"] = {**driver._diagnostics(), **changes}
                controller.healthy(state)  # TEC-only preparation remains allowed.
                with self.assertRaisesRegex(NktError, "connection diagnostic unresolved"):
                    controller.healthy(state, require_ld_ready=True)
        del state["diagnostics"]
        with self.assertRaisesRegex(NktError, "connection diagnostic unresolved"):
            controller.healthy(state, require_ld_ready=True)
        driver.close()

    def test_malformed_raw_diagnostics_still_fail_closed(self):
        for command, response in (("OUTP1:PROT:CONN:TRIP?", "2"),
                                  ("STAT:MEAS:COND?", "nan"), ("STAT:MEAS:COND?", "65536")):
            driver, resource, _ = backend()
            resource.values[command] = response
            with self.assertRaises(NktError): driver.snapshot()
            driver.close()

    def test_invalid_requests_send_no_instrument_commands(self):
        for name, value in (("setLdCurrSetpoint", .031), ("setLdCurrLimit", float("nan")),
                            ("setTempSetpoint", 31.), ("setLdOperatingMode", 1),
                            ("setLdOperatingMode", False), ("switchLdOutput", 1),
                            ("switchTecOutput", "ON"), ("unknown", 0.)):
            driver, resource, _ = backend()
            resource.calls.clear()
            with self.assertRaises(NktError): driver.set(name, value)
            self.assertEqual(resource.calls, [])
            driver.close()

    def test_changed_limit_is_rejected_before_ld_output_on(self):
        driver, resource, _ = backend()
        resource.values["SOUR1:CURR:LIM:AMPL?"] = ".031"
        with self.assertRaises(NktError): driver.set("switchLdOutput", True)
        self.assertNotIn(("write", "OUTP1:STAT 1"), resource.calls)
        driver.close()

    def test_failed_limit_readback_prevents_tec_and_emission(self):
        driver, resource, _ = backend()
        resource.values["SOUR1:CURR:LIM:AMPL?"] = ".06"
        resource.ignore_write = "SOUR1:CURR:LIM:AMPL 0.03"
        controller = CldController(driver.config, driver)
        with self.assertRaises(NktError): controller.begin(authorize_writes=True, confirm_manual_route=True)
        self.assertNotIn(("write", "OUTP1:STAT 1"), resource.calls)
        self.assertNotIn(("write", "OUTP2:STAT 1"), resource.calls)
        self.assertTrue(controller.finish()["off_confirmed"])

    def test_full_controller_uses_verified_commands_and_restores_zero_off(self):
        driver, resource, _ = backend()
        clock = Clock()
        controller = CldController(driver.config, driver, clock=clock, sleep=clock.sleep)
        controller.begin(authorize_writes=True, confirm_manual_route=True)
        controller.prepare_point(driver.config.points[0])
        controller.set_emission(True)
        writes = [command for kind, command in resource.calls if kind == "write"]
        self.assertIn("SOUR1:CURR:LEV:IMM:AMPL 0.01", writes)
        self.assertIn("OUTP2:STAT 1", writes)
        self.assertIn("OUTP1:STAT 1", writes)
        on_index = resource.calls.index(("write", "OUTP1:STAT 1"))
        self.assertIn(("query", "SOUR1:CURR:LIM:AMPL?"), resource.calls[on_index-10:on_index])
        result = controller.finish()
        self.assertTrue(result["off_confirmed"])
        self.assertEqual(result["errors"], [])
        self.assertEqual(resource.values["OUTP1:STAT?"], "0")
        self.assertEqual(resource.values["OUTP2:STAT?"], "0")
        self.assertEqual(float(resource.values["SOUR1:CURR:LEV:IMM:AMPL?"]), 0.)
        self.assertFalse(any("*RST" in command or "*CLS" in command for _, command in resource.calls if isinstance(command, str)))

    def test_poisoned_cleanup_attempts_both_outputs_without_claiming_confirmation(self):
        driver, resource, manager = backend()
        clock = Clock()
        controller = CldController(driver.config, driver, clock=clock, sleep=clock.sleep)
        controller.begin(authorize_writes=True, confirm_manual_route=True)
        controller.prepare_point(driver.config.points[0]); controller.set_emission(True)
        driver.transport.poisoned = True
        resource.calls.clear()
        result = controller.finish()
        self.assertIn(("write", "OUTP1:STAT 0"), resource.calls)
        self.assertIn(("write", "OUTP2:STAT 0"), resource.calls)
        self.assertFalse(result["off_confirmed"])
        self.assertFalse(controller.record["state_current"])
        self.assertTrue(controller.record["last_confirmed_state"]["getLdOutputState"])
        self.assertTrue(result["errors"])
        self.assertTrue(resource.closed)
        manager.close.assert_called_once()

    def test_failed_ld_off_does_not_skip_tec_off(self):
        driver, resource, _ = backend()
        clock = Clock()
        controller = CldController(driver.config, driver, clock=clock, sleep=clock.sleep)
        controller.begin(authorize_writes=True, confirm_manual_route=True)
        controller.prepare_point(driver.config.points[0]); controller.set_emission(True)
        resource.failure = "OUTP1:STAT 0"
        resource.calls.clear()
        result = controller.finish()
        self.assertIn(("write", "OUTP2:STAT 0"), resource.calls)
        self.assertFalse(result["off_confirmed"])
        self.assertEqual(resource.values["OUTP1:STAT?"], "1")
        self.assertEqual(resource.values["OUTP2:STAT?"], "0")

    def test_cli_chooses_visa_and_keeps_resource_closure(self):
        config = load_photoresponse_config(ROOT / "config/photoresponse_cld_simulation.toml")
        config = replace(config, nkt=configuration(), hardware=SimpleNamespace(
            addresses=lambda: {}, timeout_ms=1000, detector_transport="direct", validate=lambda config: None))
        fake_backend = Mock()
        with patch("attodry_control.photoresponse_cld.VisaCld", return_value=fake_backend) as visa, \
             patch("attodry_control.photoresponse_cld.TL4000") as dll, \
             patch("attodry_control.photoresponse_transport.visa_resources", return_value=nullcontext({})), \
             patch("attodry_control.photoresponse_hardware.HardwareElectrical"), \
             patch("attodry_control.photoresponse_cli.PhotoresponseRunner") as runner:
            runner.return_value.run.return_value = {"completed": True}
            result = _run(config, SimpleNamespace(confirm_route=True, authorize_environment=False), Mock(), 1)
            self.assertTrue(result["completed"])
            visa.assert_called_once()
            dll.assert_not_called()
            fake_backend.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
