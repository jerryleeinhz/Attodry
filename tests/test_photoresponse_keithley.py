from __future__ import annotations

import unittest

from attodry_control.nkt_config import NktError
from attodry_control.photoresponse_keithley import Keithley6221Queries, Keithley2182Queries


class Resource:
    def __init__(self, values):
        self.values = dict(values)
        self.commands = []

    def query(self, command):
        self.commands.append(command)
        value = self.values[command]
        if isinstance(value, Exception):
            raise value
        return value


def current_resource():
    return Resource({"*IDN?": "KEITHLEY INSTRUMENTS INC.,MODEL 6221,SIMULATED,1.0",
                     "OUTP:STAT?": "0", "SOUR:CURR?": "1e-6", "SOUR:CURR:COMP?": "1.0",
                     "SOUR:CURR:RANG?": "2e-6", "SOUR:CURR:RANG:AUTO?": "1",
                     "STAT:MEAS:COND?": "8"})


def voltage_resource():
    return Resource({"*IDN?": "KEITHLEY INSTRUMENTS INC.,MODEL 2182A,SIMULATED,1.0",
                     "SENS:FUNC?": '"VOLT:DC"', "SENS:CHAN?": "1",
                     "SENS:VOLT:DC:RANG?": "0.01", "SENS:VOLT:DC:RANG:AUTO?": "0",
                     "SENS:VOLT:CHAN2:RANG?": "0.1", "SENS:VOLT:CHAN2:RANG:AUTO?": "0",
                     "SENS:VOLT:DC:NPLC?": "1", "FETC?": "0.000001"})


class QueryContractTests(unittest.TestCase):
    def test_channel2_snapshot_reads_its_own_range(self):
        resource = voltage_resource()
        resource.values["SENS:CHAN?"] = "2"
        adapter = Keithley2182Queries(resource, expected_idn=resource.values["*IDN?"])
        state = adapter.snapshot(authorize_queries=True)
        self.assertEqual(state["channel"], 2)
        self.assertEqual(state["voltage_range_v"], .1)
        self.assertNotIn("SENS:VOLT:DC:RANG?", resource.commands)

    def test_no_implicit_io_and_exact_identity_before_other_queries(self):
        for cls, resource in ((Keithley6221Queries, current_resource()),
                              (Keithley2182Queries, voltage_resource())):
            with self.subTest(cls=cls):
                adapter = cls(resource, expected_idn="different identity")
                self.assertEqual(resource.commands, [])
                with self.assertRaises(NktError):
                    adapter.snapshot()
                self.assertEqual(resource.commands, [])
                with self.assertRaises(NktError):
                    adapter.snapshot(authorize_queries=True)
                self.assertEqual(resource.commands, ["*IDN?"])

    def test_source_setting_is_not_measured_current_and_condition_is_not_consumed(self):
        resource = current_resource()
        adapter = Keithley6221Queries(resource, expected_idn=resource.values["*IDN?"])
        state = adapter.snapshot(authorize_queries=True)
        self.assertEqual(state["source_current_setpoint_a"], 1e-6)
        self.assertIsNone(state["measured_current_a"])
        self.assertTrue(state["compliance_tripped"])
        self.assertFalse(state["dc_takeover_verified"])
        self.assertFalse(state["acquisition_ready"])
        self.assertFalse(state["error_queue_consumed"])
        self.assertEqual(resource.commands, ["*IDN?", "OUTP:STAT?", "SOUR:CURR?", "SOUR:CURR:COMP?",
                                            "SOUR:CURR:RANG?", "SOUR:CURR:RANG:AUTO?", "STAT:MEAS:COND?"])

    def test_communication_failure_preserves_last_confirmed_and_partial_response(self):
        resource = current_resource()
        adapter = Keithley6221Queries(resource, expected_idn=resource.values["*IDN?"])
        state = adapter.snapshot(authorize_queries=True)
        resource.values["SOUR:CURR?"] = OSError("lost connection")
        with self.assertRaises(OSError):
            adapter.snapshot(authorize_queries=True)
        self.assertFalse(adapter.state_current)
        self.assertEqual(adapter.last_confirmed_state, state)
        self.assertEqual(adapter.partial_raw["OUTP:STAT?"], "0")

    def test_malformed_output_current_status_fail_closed(self):
        for key, value in (("OUTP:STAT?", "unknown"), ("SOUR:CURR?", "nan"),
                           ("STAT:MEAS:COND?", "1.5"), ("SOUR:CURR:COMP?", "0")):
            with self.subTest(key=key):
                resource = current_resource()
                resource.values[key] = value
                adapter = Keithley6221Queries(resource, expected_idn=resource.values["*IDN?"])
                with self.assertRaises(NktError):
                    adapter.snapshot(authorize_queries=True)
                self.assertFalse(adapter.state_current)

    def test_fetch_is_explicitly_stale_and_never_accepted_or_triggered(self):
        resource = voltage_resource()
        adapter = Keithley2182Queries(resource, expected_idn=resource.values["*IDN?"])
        result = adapter.fetch_existing_voltage(authorize_queries=True, max_abs_voltage_v=0.01)
        self.assertEqual(result["voltage_v"], 1e-6)
        self.assertFalse(result["fresh"])
        self.assertFalse(result["accepted"])
        self.assertEqual(resource.commands[-1], "FETC?")
        self.assertNotIn("READ?", resource.commands)
        self.assertTrue(all(command.endswith("?") for command in resource.commands))
        self.assertFalse(any("ERR" in command or "EVEN" in command for command in resource.commands))

    def test_overload_sentinel_and_wrong_channel_are_rejected(self):
        for key, value in (("FETC?", "+9.9E37"), ("FETC?", "inf"), ("SENS:CHAN?", "2")):
            resource = voltage_resource()
            resource.values[key] = value
            adapter = Keithley2182Queries(resource, expected_idn=resource.values["*IDN?"])
            with self.subTest(key=key, value=value), self.assertRaises(NktError):
                adapter.fetch_existing_voltage(authorize_queries=True, max_abs_voltage_v=0.01)

    def test_impossible_voltage_settings_are_not_confirmed(self):
        for key, value in (("SENS:CHAN?", "3"), ("SENS:VOLT:DC:RANG?", "-1"),
                           ("SENS:VOLT:DC:NPLC?", "0")):
            resource = voltage_resource()
            resource.values[key] = value
            adapter = Keithley2182Queries(resource, expected_idn=resource.values["*IDN?"])
            with self.subTest(key=key), self.assertRaises(NktError):
                adapter.snapshot(authorize_queries=True)
            self.assertFalse(adapter.state_current)
            self.assertIsNone(adapter.last_confirmed_state)


if __name__ == "__main__":
    unittest.main()
