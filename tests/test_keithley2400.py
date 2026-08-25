from dataclasses import replace
import unittest

from attodry_control.keithley2400 import QcodesKeithley2400
from attodry_control.three_smu_config import SmuHardwareConfig, SourceMode


def config() -> SmuHardwareConfig:
    return SmuHardwareConfig(
        role="smu_bias",
        model="Keithley2400",
        address="FAKE::1",
        timeout_ms=5000,
        source_mode=SourceMode.VOLTAGE,
        compliance_current_a=1e-3,
        compliance_voltage_v=10.0,
        max_abs_voltage_v=10.0,
        max_abs_current_a=1e-3,
        source_min_v=-1.0,
        source_max_v=1.0,
        ramp_step_v=0.1,
        readback_tolerance_v=1e-6,
        source_min_a=-1e-3,
        source_max_a=1e-3,
        ramp_step_a=1e-4,
        readback_tolerance_a=1e-9,
        settle_s=0.0,
        nplc=1.0,
        source_auto_range=True,
        measure_auto_range=False,
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


class Keithley2400AdapterTests(unittest.TestCase):
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
        self.assertIn(("write", ":SENS:CURR:RANG:AUTO OFF"), instrument.calls)
        self.assertIn(("write", ":SYST:RSEN ON"), instrument.calls)

    def test_command_error_is_not_swallowed(self) -> None:
        instrument = FakeQcodesInstrument()
        instrument.fail_write = ":SENS:CURR:RANG:AUTO OFF"
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
        self.assertIn(("write", ":SENS:VOLT:RANG:AUTO OFF"), instrument.calls)

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
        self.assertFalse(reading.near_compliance)
        self.assertEqual(reading.resistance_ohm, 250.0)
        self.assertTrue(reading.status_query_consumed)


if __name__ == "__main__":
    unittest.main()
