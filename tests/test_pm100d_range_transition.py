from dataclasses import replace
import math
import unittest

from optical_helpers import EXAMPLE, Clock
from attodry_control.nkt_config import NktError
from attodry_control.optical_config import load_pm_config
from attodry_control.pm100d import Pm100d, SimulatedPmResource


class WavelengthRangeResource(SimulatedPmResource):
    """Model the observed W-range conversion and a rounded-boundary write."""

    native_range_w = 0.01507291

    def __init__(self, *, converted_range_w=None, boundary_write_jumps=False,
                 after_wavelength=None):
        super().__init__()
        self.values.update({"SENS:CORR:WAV?": "630", "SENS:POW:RANG:AUTO?": "0",
                            "SENS:POW:RANG?": "0.01125863"})
        self.converted_range_w = (self.native_range_w if converted_range_w is None
                                  else converted_range_w)
        self.boundary_write_jumps = boundary_write_jumps
        self.after_wavelength = after_wavelength
        self.wavelength_written = False

    def write(self, command):
        super().write(command)
        if command.startswith("SENS:CORR:WAV "):
            self.wavelength_written = True
            self.values["SENS:POW:RANG?"] = str(self.converted_range_w)
            if self.after_wavelength is not None:
                self.after_wavelength(self)
        elif command.startswith("SENS:POW:RANG ") and self.boundary_write_jumps:
            self.values["SENS:POW:RANG?"] = str(float(command.rsplit(" ", 1)[1]) * 10)


class PmRangeTransitionTests(unittest.TestCase):
    def device(self, resource=None, *, range_w=None, hardware=False, **kwargs):
        resource = resource or WavelengthRangeResource()
        config = replace(load_pm_config(EXAMPLE), auto_range=False,
                         range_w=resource.native_range_w if range_w is None else range_w)
        return Pm100d(config, resource, is_hardware=hardware, clock=Clock(), **kwargs)

    def setting_commands(self, resource):
        return [command for kind, command in resource.commands if kind == "write"]

    def test_wavelength_native_range_does_not_get_rewritten_at_boundary(self):
        resource = WavelengthRangeResource(boundary_write_jumps=True)
        meter = self.device(resource)
        state = meter.prepare(500)
        self.assertEqual(state["wavelength_nm"], 500)
        self.assertEqual(state["range_w"], resource.native_range_w)
        self.assertEqual(self.setting_commands(resource), ["SENS:CORR:WAV 500"])
        self.assertEqual(len(resource.commands), 28)  # 13 + WAV/ACK + 13.
        self.assertEqual(resource.commands[13:16], [
            ("write", "SENS:CORR:WAV 500"), ("query", "*OPC?"), ("query", "CONF?")])
        self.assertEqual(len(meter.transcript), len(resource.commands))
        self.assertFalse(any(command == "READ?" for _, command in resource.commands))

    def test_range_write_and_final_check_use_the_same_one_ppm_tolerance(self):
        for factor in (1, 1 + 0.5e-6, 1 - 0.5e-6):
            resource = WavelengthRangeResource(boundary_write_jumps=True)
            meter = self.device(resource, range_w=resource.native_range_w * factor)
            with self.subTest(factor=factor):
                meter.prepare(500)
                self.assertEqual(self.setting_commands(resource), ["SENS:CORR:WAV 500"])
                resource.commands.clear()
                meter.prepare(500)
                self.assertEqual(len(resource.commands), 13)
                self.assertEqual(self.setting_commands(resource), [])

    def test_genuinely_different_range_still_written_and_verified_after_new_capture(self):
        resource = WavelengthRangeResource(converted_range_w=0.001507291)
        meter = self.device(resource)
        state = meter.prepare(500)
        self.assertEqual(state["range_w"], resource.native_range_w)
        self.assertEqual(self.setting_commands(resource), [
            "SENS:CORR:WAV 500", "SENS:POW:RANG 0.01507291"])
        range_index = resource.commands.index(("write", "SENS:POW:RANG 0.01507291"))
        self.assertEqual(range_index, 28)
        self.assertEqual(resource.commands[range_index + 1:range_index + 3], [
            ("query", "*OPC?"), ("query", "CONF?")])
        self.assertEqual(len(resource.commands), 43)

    def test_outside_one_ppm_range_requires_write(self):
        resource = WavelengthRangeResource()
        meter = self.device(resource, range_w=resource.native_range_w * (1 + 2e-6))
        state = meter.prepare(500)
        self.assertEqual(len(self.setting_commands(resource)), 2)
        self.assertTrue(math.isclose(state["range_w"], meter.config.range_w,
                                     rel_tol=1e-6, abs_tol=0))

    def test_required_range_write_that_selects_wrong_decade_remains_a_hard_failure(self):
        # A device left at the higher range needs a real range change. This fix
        # does not solve or accept a boundary request that itself jumps a decade.
        resource = WavelengthRangeResource(boundary_write_jumps=True)
        resource.values.update({"SENS:CORR:WAV?": "500", "SENS:POW:RANG?": "0.1507291"})
        meter = self.device(resource)
        with self.assertRaisesRegex(NktError, "fixed range mismatch"):
            meter.prepare(500)
        self.assertEqual(self.setting_commands(resource), ["SENS:POW:RANG 0.01507291"])
        self.assertEqual(meter.last_confirmed_state["range_w"], 0.1507291)
        self.assertIsNone(meter.target_nm)
        self.assertEqual(meter.transcript[-4]["command"], "SENS:POW:RANG?")
        self.assertEqual(meter.transcript[-4]["reply"], "0.1507291")
        self.assertFalse(any(command == "READ?" for _, command in resource.commands))

    def test_new_capture_rejects_faults_before_any_range_setting(self):
        faults = [("*IDN?", "THORLABS,PM100D,CHANGED,SIMULATION", "identity changed"),
                  ("SYST:SENS:IDN?", '"NEW SENSOR","CHANGED","CAL",1,0,33', "sensor changed"),
                  ("STAT:QUES:COND?", "1", "questionable condition"),
                  ("SENS:AVER:COUN?", "2", "average/range settings mismatch"),
                  ("SENS:POW:UNIT?", "DBM", "power mode and W"),
                  ("SENS:POW:REF:STAT?", "1", "relative/user-loss"),
                  ("SENS:CORR:WAV?", "630", "wavelength correction mismatch")]
        for key, value, message in faults:
            resource = WavelengthRangeResource(
                converted_range_w=0.001507291,
                after_wavelength=lambda r, k=key, v=value: r.values.update({k: v}))
            meter = self.device(resource)
            with self.subTest(key=key), self.assertRaisesRegex(NktError, message):
                meter.prepare(500)
            self.assertEqual(self.setting_commands(resource), ["SENS:CORR:WAV 500"])
            self.assertIsNone(meter.target_nm)
            self.assertTrue(any(item["command"] == key and item["reply"] == value
                                for item in meter.transcript))

    def test_wavelength_ack_fault_poison_stops_before_new_capture_or_range_write(self):
        resource = WavelengthRangeResource()
        resource.values["*OPC?"] = "0"
        meter = self.device(resource)
        with self.assertRaisesRegex(NktError, "not acknowledged"):
            meter.prepare(500)
        self.assertTrue(meter.poisoned)
        self.assertFalse(meter.state_current)
        self.assertEqual(len(resource.commands), 15)
        self.assertEqual(meter.transcript[-1]["reply"], "0")
        self.assertIn("error", meter.transcript[-1])
        count = len(resource.commands)
        with self.assertRaises(NktError):
            meter.prepare(500)
        self.assertEqual(len(resource.commands), count)

    def test_post_wavelength_capture_communication_fault_poison_keeps_old_state(self):
        resource = WavelengthRangeResource(converted_range_w=0.001507291)
        query = resource.query
        def fail_after_wavelength(command):
            if resource.wavelength_written and command == "SENS:POW:RANG?":
                resource.commands.append(("query", command))
                raise TimeoutError("range read lost")
            return query(command)
        resource.query = fail_after_wavelength
        meter = self.device(resource)
        with self.assertRaisesRegex(TimeoutError, "range read lost"):
            meter.prepare(500)
        self.assertTrue(meter.poisoned)
        self.assertFalse(meter.state_current)
        self.assertEqual(meter.last_confirmed_state["wavelength_nm"], 630)
        self.assertEqual(meter.last_confirmed_state["range_w"], 0.01125863)
        self.assertEqual(meter.transcript[-1]["command"], "SENS:POW:RANG?")
        self.assertIsNone(meter.transcript[-1]["reply"])
        self.assertIn("TimeoutError", meter.transcript[-1]["error"])
        self.assertEqual(self.setting_commands(resource), ["SENS:CORR:WAV 500"])

    def test_range_ack_or_readback_mismatch_blocks_read_acquisition(self):
        for fault in ("ack", "readback"):
            resource = WavelengthRangeResource(converted_range_w=0.001507291)
            write = resource.write
            def fail_range_setting(command, fault=fault):
                write(command)
                if command.startswith("SENS:POW:RANG "):
                    if fault == "ack":
                        resource.values["*OPC?"] = "0"
                    else:
                        resource.values["SENS:POW:RANG?"] = str(resource.native_range_w * (1 + 2e-6))
            resource.write = fail_range_setting
            meter = self.device(resource)
            with self.subTest(fault=fault), self.assertRaises(NktError):
                meter.read_power_w(500)
            self.assertIsNone(meter.last_sample)
            self.assertFalse(any(command == "READ?" for _, command in resource.commands))
            self.assertEqual(meter.poisoned, fault == "ack")

    def test_hardware_setting_authorization_is_still_required(self):
        resource = WavelengthRangeResource()
        meter = self.device(resource, hardware=True, authorize_measurement=True)
        with self.assertRaisesRegex(NktError, "wavelength correction mismatch"):
            meter.prepare(500)
        self.assertEqual(self.setting_commands(resource), [])
        self.assertEqual(len(resource.commands), 13)

