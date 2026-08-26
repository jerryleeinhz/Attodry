from dataclasses import replace
import unittest

from attodry_control.keithley2400 import (
    KEITHLEY_2400_TIMEOUT_MS,
    QcodesKeithley2400,
    VisaKeithley2400Monitor,
    open_keithley2400_monitor,
)
from attodry_control.three_smu_config import SmuHardwareConfig, SourceMode


def config() -> SmuHardwareConfig:
    return SmuHardwareConfig(
        role="smu_bias",
        model="Keithley2400",
        address="FAKE::1",
        source_mode=SourceMode.VOLTAGE,
        max_abs_voltage_v=10.0,
        max_abs_current_a=1e-3,
        nplc=1.0,
        source_auto_range=True,
        measure_auto_range=True,
        four_wire=True,
    )


class Parameter:
    def __init__(self, instrument, name):
        self.instrument = instrument
        self.name = name

    def __call__(self, value=None):
        if value is None:
            return self.instrument.values.get(self.name)
        self.instrument.values[self.name] = value
        self.instrument.calls.append((self.name, value))


class FakeQcodesInstrument:
    def __init__(self):
        self.calls = []
        self.values = {"volt": 0.0, "curr": 0.0, "output": "off"}
        for name in (
            "timeout", "mode", "compliancei", "compliancev",
            "nplci", "nplcv", "volt", "curr", "output",
        ):
            setattr(self, name, Parameter(self, name))
        self.responses = {
            "*IDN?": "KEITHLEY,MODEL 2400,1234,1.0",
            ":SOUR:FUNC?": "VOLT",
            ":SOUR:VOLT?": "0.125",
            ":OUTP?": "1",
            ":READ?": "0.125,0.0005,250",
            ":SENS:CURR:PROT?": "0.001",
            ":SOUR:VOLT:RANG?": "1.0",
            ":SENS:CURR:RANG?": "0.001",
            ":SENS:VOLT:PROT?": "10.0",
            ":SOUR:CURR:RANG?": "0.001",
            ":SENS:VOLT:RANG?": "10.0",
            ":SYST:RSEN?": "0",
            "SENS:CURR:PROT:TRIP?": "0",
            ":SYST:ERR?": "0,No error",
        }
        self.closed = False
        self.fail_write = None

    def ask(self, command):
        self.calls.append(("ask", command))
        return self.responses[command]

    def write(self, command):
        self.calls.append(("write", command))
        if command == self.fail_write:
            raise OSError("injected write failure")

    def close(self):
        self.closed = True


class FakeVisaResource:
    """Minimal query-only VISA double: it intentionally has no write method."""

    def __init__(self, *, identity="KEITHLEY,MODEL 2400,4321,1.0"):
        self.identity = identity
        self.timeout = None
        self.closed = False
        self.queries = []
        self.responses = {
            "*IDN?": identity,
            ":SOUR:FUNC?": "VOLT",
            ":SOUR:VOLT?": "0.125",
            ":OUTP?": "0",
            ":READ?": "0.125,0.0000005,250000",
            ":SENS:CURR:PROT?": "0.001",
            ":SOUR:VOLT:RANG?": "1.0",
            ":SENS:CURR:RANG?": "0.001",
            ":SYST:RSEN?": "0",
            "SENS:CURR:PROT:TRIP?": "0",
            ":SYST:ERR?": "0,No error",
        }

    def query(self, command):
        self.queries.append(command)
        return self.responses[command]

    def close(self):
        self.closed = True


class FakeVisaManager:
    def __init__(self, resource):
        self.resource = resource
        self.opened = []
        self.closed = False

    def open_resource(self, address):
        self.opened.append(address)
        return self.resource

    def close(self):
        self.closed = True


class Keithley2400AdapterTests(unittest.TestCase):
    def test_fixed_timeout_is_five_seconds(self) -> None:
        instrument = FakeQcodesInstrument()
        adapter = QcodesKeithley2400("smu_bias", instrument)
        adapter.set_timeout(KEITHLEY_2400_TIMEOUT_MS)
        self.assertEqual(KEITHLEY_2400_TIMEOUT_MS, 5000)
        self.assertIn(("timeout", 5.0), instrument.calls)

    def test_preflight_is_query_only(self) -> None:
        instrument = FakeQcodesInstrument()
        adapter = QcodesKeithley2400("smu_bias", instrument)
        state = adapter.preflight()
        self.assertTrue(state.output_enabled)
        self.assertEqual(state.source_setpoint, 0.125)
        self.assertEqual(state.compliance_limit, 0.001)
        self.assertFalse(state.four_wire)
        self.assertFalse(any(call[0] == "write" for call in instrument.calls))
        self.assertFalse(any(call[0] in {"volt", "curr", "output"} for call in instrument.calls))

    def test_configuration_preserves_range_nplc_and_four_wire_settings(self) -> None:
        instrument = FakeQcodesInstrument()
        adapter = QcodesKeithley2400("smu_bias", instrument)
        adapter.configure(config())
        self.assertIn(("mode", "VOLT"), instrument.calls)
        self.assertIn(("compliancei", 1e-3), instrument.calls)
        self.assertIn(("nplci", 1.0), instrument.calls)
        self.assertIn(("nplcv", 1.0), instrument.calls)
        self.assertIn(("write", ":SOUR:VOLT:RANG:AUTO ON"), instrument.calls)
        self.assertIn(("write", ":SENS:CURR:RANG:AUTO ON"), instrument.calls)
        self.assertIn(("write", ":SYST:RSEN ON"), instrument.calls)

    def test_command_error_is_not_swallowed(self) -> None:
        instrument = FakeQcodesInstrument()
        instrument.fail_write = ":SENS:CURR:RANG:AUTO ON"
        adapter = QcodesKeithley2400("smu_bias", instrument)
        with self.assertRaises(OSError):
            adapter.configure(config())

    def test_bias_current_source_uses_voltage_compliance_and_matching_ranges(self) -> None:
        instrument = FakeQcodesInstrument()
        adapter = QcodesKeithley2400("smu_bias", instrument)
        adapter.configure(replace(config(), source_mode=SourceMode.CURRENT))
        self.assertIn(("mode", "CURR"), instrument.calls)
        self.assertIn(("compliancev", 10.0), instrument.calls)
        self.assertIn(("write", ":SOUR:CURR:RANG:AUTO ON"), instrument.calls)
        self.assertIn(("write", ":SENS:VOLT:RANG:AUTO ON"), instrument.calls)

    def test_read_includes_trip_output_source_and_status(self) -> None:
        instrument = FakeQcodesInstrument()
        adapter = QcodesKeithley2400("smu_bias", instrument)
        adapter.configure(config())
        adapter.authorize_status_consumption()
        reading = adapter.read()
        self.assertEqual(reading.voltage_v, 0.125)
        self.assertEqual(reading.current_a, 0.0005)
        self.assertTrue(reading.output_enabled)
        self.assertFalse(reading.compliance_trip)
        self.assertEqual(reading.resistance_ohm, 250.0)
        self.assertTrue(reading.status_query_consumed)

    def test_configuration_rejects_compliance_readback_above_max_abs_limit(self) -> None:
        instrument = FakeQcodesInstrument()
        instrument.responses[":SENS:CURR:PROT?"] = "0.002"
        adapter = QcodesKeithley2400("smu_bias", instrument)
        with self.assertRaisesRegex(Exception, "compliance readback"):
            adapter.configure(config())

    def test_set_source_checks_absolute_limit_before_driver_write(self) -> None:
        instrument = FakeQcodesInstrument()
        adapter = QcodesKeithley2400("smu_bias", instrument)
        adapter.configure(config())
        before = list(instrument.calls)
        with self.assertRaisesRegex(Exception, "max_abs"):
            adapter.set_source(11.0)
        self.assertEqual(instrument.calls, before)

    def test_live_monitor_queries_state_without_consuming_error_queue_by_default(self) -> None:
        resource = FakeVisaResource()
        reading = VisaKeithley2400Monitor("smu_bias", resource).read_monitor()
        self.assertEqual(reading.identity, "KEITHLEY,MODEL 2400,4321,1.0")
        self.assertEqual(reading.voltage_v, 0.125)
        self.assertEqual(reading.current_a, 0.0000005)
        self.assertEqual(reading.resistance_ohm, 250000.0)
        self.assertFalse(reading.output_enabled)
        self.assertFalse(reading.status_queue_consumed)
        self.assertNotIn(":SYST:ERR?", resource.queries)
        self.assertFalse(hasattr(resource, "write"))

    def test_live_monitor_status_queue_consumption_is_explicit(self) -> None:
        resource = FakeVisaResource()
        reading = VisaKeithley2400Monitor("smu_bias", resource).read_monitor(
            consume_status_queue=True
        )
        self.assertTrue(reading.status_queue_consumed)
        self.assertEqual(reading.status, "0,No error")
        self.assertIn(":SYST:ERR?", resource.queries)

    def test_open_live_monitor_sets_only_local_timeout_and_closes_resource(self) -> None:
        resource = FakeVisaResource()
        manager = FakeVisaManager(resource)
        monitor = open_keithley2400_monitor("smu_bias", config(), manager)
        self.assertEqual(manager.opened, ["FAKE::1"])
        self.assertEqual(resource.timeout, 5000)
        self.assertEqual(resource.queries, [])
        monitor.close()
        self.assertTrue(resource.closed)


if __name__ == "__main__":
    unittest.main()
