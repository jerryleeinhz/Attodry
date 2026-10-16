"""2182A acquisition settings and filter history, with injected resources only."""
from collections import deque
from dataclasses import replace
import unittest

from attodry_control.nkt_config import NktError
from attodry_control.photoresponse_devices import Voltmeter2182
from attodry_control.photoresponse_hardware import HardwareElectrical
from attodry_control.photoresponse_transport import VisaTransport
from test_photoresponse_hardware import Resource
from test_photoresponse_integration import hardware_setup


def voltmeter(resource=None, **kwargs):
    resource = resource or Resource("2182A")
    values = dict(expected_idn=resource.values["*IDN"], voltage_range_v=1.0,
                  nplc=1.0, max_voltage_v=1.0, channel=2)
    values.update(kwargs)
    return Voltmeter2182(VisaTransport(resource, role="detector", authorize_writes=True), **values), resource


class NanovoltmeterConfigurationTests(unittest.TestCase):
    def test_strict_processing_settings_rejected_before_io(self):
        cases = [{"analog_lowpass_enabled": 1}, {"autozero": "off"}, {"front_autozero": 0},
                 {"digital_filter": False}, {"digital_filter": {}},
                 {"digital_filter": {"enabled": 1}},
                 {"digital_filter": {"enabled": True, "type": "REPEAT"}},
                 {"digital_filter": {"enabled": False, "count": 0}},
                 {"digital_filter": {"enabled": True, "count": 101}},
                 {"digital_filter": {"enabled": True, "count": 2.0}},
                 {"digital_filter": {"enabled": True, "window_percent": -1}},
                 {"digital_filter": {"enabled": True, "window_percent": 10.01}},
                 {"digital_filter": {"enabled": True, "window_percent": float("nan")}},
                 {"digital_filter": {"enabled": True, "unknown": 1}}]
        for values in cases:
            resource = Resource("2182A")
            with self.subTest(values=values), self.assertRaises(NktError):
                voltmeter(resource, **values)
            self.assertEqual(resource.commands, [])

    def test_legacy_defaults_keep_twenty_three_queries_per_verification(self):
        device, resource = voltmeter()
        device.begin()
        self.assertEqual(len(device.settings), 22)
        resource.commands.clear()
        evidence = device.prepare_condition()
        self.assertEqual(resource.commands, [])
        self.assertEqual(evidence["readings"], [])
        reading = device.read_voltage()
        self.assertEqual(len(resource.commands), 48)  # 2 * 23 settings/ID + READ + ERR
        self.assertEqual(resource.commands.count("READ?"), 1)
        self.assertNotIn("STAT:OPER:COND?", resource.commands)
        self.assertFalse(reading["filter_samples_may_overlap"])
        self.assertEqual(device.minimum_timeout_s, 1.08)

    def test_processing_fields_are_channel_scoped_and_read_back(self):
        device, resource = voltmeter(analog_lowpass_enabled=True, autozero=False, front_autozero=False,
                                    digital_filter={"enabled": True, "type": "repeating",
                                                    "count": 5, "window_percent": .25})
        resource.values["STAT:OPER:COND"] = "1280"  # Idle + filter settled, no event consumption.
        device.begin()
        self.assertEqual(resource.values["SENS:VOLT:CHAN2:DFIL:COUN"], "5")
        self.assertEqual(resource.values["SENS:VOLT:CHAN2:DFIL:WIND"], "0.25")
        self.assertEqual(resource.values["SENS:VOLT:CHAN2:DFIL:TCON"], "REP")
        self.assertEqual(resource.values["SENS:VOLT:CHAN2:LPAS"], "1")
        self.assertEqual(resource.query("SYST:AZER?"), "0")
        self.assertEqual(resource.query("SYST:FAZ?"), "0")
        self.assertFalse(any(command.startswith("SENS:VOLT:CHAN1:") for command in resource.commands))
        resource.commands.clear()
        self.assertTrue(device.prepare_condition()["complete"])
        self.assertEqual(resource.commands, [])  # Repeating stack flushes after every result.
        reading = device.read_voltage()
        self.assertFalse(reading["detector_after"]["autozero"])
        self.assertTrue(reading["detector_after"]["analog_lowpass_enabled"])
        self.assertEqual(reading["filter_condition"], 1280)
        self.assertNotIn("STAT:OPER:EVEN?", resource.commands)
        self.assertEqual(resource.commands.count("READ?"), 1)
        self.assertTrue(all(command.endswith("?") for command in resource.commands))

    def test_disabled_filter_does_not_query_or_change_inactive_parameters(self):
        device, resource = voltmeter(autozero=False, front_autozero=False,
                                    digital_filter={"enabled": False, "type": "moving",
                                                    "count": 100, "window_percent": 0})
        resource.values.update({"SENS:VOLT:CHAN2:DFIL:COUN": "7", "SYST:AZER": "1", "SYST:FAZ": "1"})
        device.begin()
        resource.commands.clear()
        device.read_voltage()
        self.assertTrue(all(command.endswith("?") for command in resource.commands))
        self.assertFalse(any(":DFIL:" in command for command in resource.commands))
        self.assertEqual(resource.values["SENS:VOLT:CHAN2:DFIL:COUN"], "7")
        self.assertEqual(resource.values["SYST:AZER"], "0")
        self.assertEqual(resource.values["SYST:FAZ"], "0")
        self.assertEqual(resource.commands.count("READ?"), 1)

    def test_all_processing_settings_drift_blocks_formal_read(self):
        for setting in ("SENS:VOLT:CHAN2:DFIL:WIND", "SENS:VOLT:CHAN2:DFIL:COUN",
                        "SENS:VOLT:CHAN2:DFIL:TCON", "SENS:VOLT:CHAN2:LPAS",
                        "SYST:AZER", "SYST:FAZ"):
            device, resource = voltmeter(digital_filter={"enabled": True})
            device.begin()
            resource.commands.clear()
            resource.values[setting] = ("1" if setting.endswith("LPAS") else "0"
                                        if setting in ("SYST:AZER", "SYST:FAZ") else "9")
            with self.subTest(setting=setting), self.assertRaisesRegex(NktError, setting):
                device.read_voltage()
            self.assertNotIn("READ?", resource.commands)
            self.assertTrue(device.faulted)

    def test_unsettled_filter_keeps_raw_and_fails_closed(self):
        device, resource = voltmeter(digital_filter={"enabled": True})
        device.begin()
        with self.assertRaisesRegex(NktError, "filter has not settled"):
            device.read_voltage()
        self.assertEqual(device.last_raw_reading, "1e-6")
        self.assertTrue(device.faulted)
        with self.assertRaises(NktError):
            device.read_voltage()
        self.assertEqual(resource.commands.count("READ?"), 1)

    def test_nplc_above_fifty_requires_confirmed_sixty_hertz_before_writes(self):
        for frequency, permitted in ((50, False), (60, True), (400, False), (0, False)):
            device, resource = voltmeter(nplc=60)
            resource.values["SYST:LFR"] = str(frequency)
            if permitted:
                device.begin()
                self.assertEqual(device.verify()["nplc"], 60)
                resource.values["SYST:LFR"] = "50"
                with self.assertRaisesRegex(NktError, "60 Hz"):
                    device.read_voltage()
            else:
                with self.assertRaisesRegex(NktError, "60 Hz"):
                    device.begin()
                self.assertTrue(all(command.endswith("?") for command in resource.commands))

    def test_timeout_budget_accounts_for_count_analog_and_autozero(self):
        device, _ = voltmeter(nplc=5, analog_lowpass_enabled=True,
                              digital_filter={"enabled": True, "count": 10})
        self.assertAlmostEqual(device.minimum_timeout_s, 10.0)
        runner, _, _, _, _ = hardware_setup("open_circuit_voltage", relay=False)
        hardware = replace(runner.config.hardware, timeout_ms=9000,
                           detector={**runner.config.hardware.detector, "nplc": 5,
                                     "analog_lowpass_enabled": True,
                                     "digital_filter": {"enabled": True, "count": 10}})
        with self.assertRaisesRegex(NktError, "timeout"):
            hardware.validate(runner.config)
        replace(hardware, timeout_ms=10000).validate(runner.config)
        hardware = replace(hardware, timeout_ms=4000,
                           detector={**hardware.detector, "autozero": False, "front_autozero": False})
        hardware.validate(runner.config)


class MovingHistoryTests(unittest.TestCase):
    class MovingResource(Resource):
        def __init__(self):
            super().__init__("2182A")
            self.values["STAT:OPER:COND"] = "1280"
            self.signal = .009
            self.history = deque([.009] * 3, maxlen=3)

        def query(self, command):
            raw = super().query(command)
            if command == "READ?":
                self.history.append(self.signal)
                return str(sum(self.history) / len(self.history))
            return raw

    def test_new_condition_excludes_previous_dark_or_light_history(self):
        resource = self.MovingResource()
        device, _ = voltmeter(resource, digital_filter={"enabled": True, "type": "moving", "count": 3})
        device.begin()
        guards = []
        for signal in (.001, .005):
            resource.signal = signal
            before = resource.commands.count("READ?")
            evidence = device.prepare_condition(guard=lambda: guards.append("safe") or {"safe": True})
            self.assertEqual(resource.commands.count("READ?") - before, 2)
            self.assertEqual(len(evidence["readings"]), 2)
            self.assertTrue(all(not row["valid"] for row in evidence["readings"]))
            self.assertIn("guard_before", evidence["readings"][0])
            first = device.read_voltage()
            self.assertAlmostEqual(first["voltage_v"], signal)
            self.assertTrue(first["filter_samples_may_overlap"])
            device.read_voltage()
            self.assertEqual(resource.commands.count("READ?") - before, 4)  # No reset between formal samples.
        self.assertEqual(len(guards), 8)
        self.assertNotIn("*RST", resource.commands)
        self.assertNotIn("*CLS", resource.commands)

    def test_transition_overload_retained_without_retrying_session(self):
        resource = Resource("2182A")
        device, _ = voltmeter(resource, digital_filter={"enabled": True, "type": "moving", "count": 3})
        device.begin()
        original = resource.query
        resource.query = lambda command: "9.9e37" if command == "READ?" else original(command)
        with self.assertRaises(NktError):
            device.prepare_condition()
        transition = device.filter_transition
        self.assertFalse(transition["complete"])
        self.assertFalse(transition["readings"][0]["valid"])
        self.assertEqual(transition["readings"][0]["raw_response"], "9.9e37")
        self.assertIn("error", transition["readings"][0])
        self.assertTrue(device.faulted)
        commands = list(resource.commands)
        with self.assertRaises(NktError):
            device.prepare_condition()
        self.assertEqual(resource.commands, commands)

    def test_external_guard_failure_does_not_poison_passive_detector(self):
        device, resource = voltmeter(digital_filter={"enabled": True, "type": "moving", "count": 3})
        device.begin()
        def rejected():
            raise NktError("external optical deviation")
        with self.assertRaisesRegex(NktError, "optical deviation"):
            device.prepare_condition(guard=rejected)
        self.assertNotIn("READ?", resource.commands)
        self.assertFalse(device.faulted)
        self.assertFalse(device.condition_prepared)
        self.assertFalse(device.filter_transition["complete"])
        self.assertIn("error", device.filter_transition["readings"][0])

    def test_hardware_wrapper_brackets_warmups_with_source_safety(self):
        runner, host, resource, _, _ = hardware_setup("open_circuit_voltage", relay=False)
        hardware = replace(runner.config.hardware,
                           detector={**runner.config.hardware.detector,
                                     "digital_filter": {"enabled": True, "type": "moving", "count": 3}})
        config = replace(runner.config, hardware=hardware)
        electrical = HardwareElectrical(config, {"6221": VisaTransport(host, role="relay", authorize_writes=True),
                                                "detector": VisaTransport(resource, role="detector", authorize_writes=True)})
        electrical.begin()
        guards = []
        result = electrical.prepare_condition(guard=lambda: guards.append(True) or {"optics": "safe"})
        self.assertEqual(len(guards), 4)
        self.assertEqual(resource.commands.count("READ?"), 2)
        self.assertIn("electrical", result["readings"][0]["guard_before"])
        self.assertIn("external", result["readings"][0]["guard_after"])


if __name__ == "__main__":
    unittest.main()
