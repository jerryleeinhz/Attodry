"""Offline USB/SCPI regressions; no VISA library or real instruments are used."""
from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import tomllib
import unittest
from unittest.mock import Mock, patch

from attodry_control.nkt_config import NktError
from attodry_control.photoresponse_cld import CldController, VisaCld, load_cld_config
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
            "OUTP2:PROT:CONN:TRIP?": "0", "OUTP2:PROT:TRAN:TRIP?": "0",
            "OUTP2:PROT:OTEM:TRIP?": "0", "OUTP2:PROT:COMP:TRIP?": "0",
            "SOUR1:CURR:LEV:IMM:AMPL?": "+0.000000E+00",
            "SOUR2:TEMP:SPO?": "+2.500000E+01", "SOUR2:CURR:LIM:AMPL?": "+1.000000E-01",
            "SENS2:TEMP:PROT:WIND:AMPL?": "+1.000000E-01", "SENS2:TEMP:DATA?": "+2.500000E+01",
            "SENS3:CURR:DC:DATA?": "+0.000000E+00", "SENS4:VOLT:DC:DATA?": "+0.000000E+00",
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
        for command in ("OUTP2:PROT:CONN:TRIP?", "OUTP2:PROT:TRAN:TRIP?",
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
        self.assertIn(("query", "SOUR1:CURR:LIM:AMPL?"), resource.calls[on_index-5:on_index])
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
        config = replace(config, nkt=configuration(), hardware=SimpleNamespace(addresses=lambda: {}, timeout_ms=1000))
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
