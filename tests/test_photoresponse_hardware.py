from __future__ import annotations

from dataclasses import replace
import unittest

from attodry_control.nkt_config import NktError
from attodry_control.photoresponse_devices import CurrentSource6221, Voltmeter2182, Meter2400
from attodry_control.photoresponse_transport import VisaTransport, Serial2182Transport, clean_response


class Resource:
    def __init__(self, model="6221"):
        self.values = {"*IDN": f"KEITHLEY INSTRUMENTS INC.,MODEL {model},FAKE,1.0",
            "OUTP:STAT": "0", "OUTP": "0", "SOUR:CURR": "0", "SOUR:CURR:COMP": "1",
            "SOUR:CURR:RANG": "2e-8", "SOUR:CURR:RANG:AUTO": "0", "STAT:MEAS:COND": "0",
            "STAT:OPER:COND": "1024", "STAT:QUES:COND": "0", "SOUR:FUNC": "VOLT",
            "SOUR:VOLT:LEV": "0", "SOUR:CURR:LEV": "0"}
        self.commands = []
        self.failure = None
        self.trip = False
        self.pending = ""
        self.downstream = None

    def query(self, command):
        self.commands.append(command)
        if command == self.failure:
            raise OSError("injected bus failure")
        if command == "SYST:COMM:SER:ENT?":
            response, self.pending = self.pending, ""
            return response
        if command == "SYST:ERR?":
            return '0,"No error"'
        if command == "STAT:MEAS:COND?" and self.trip and self.values["OUTP:STAT"] == "1":
            return "8"
        if command.endswith("PROT:TRIP?"):
            return "1" if self.trip else "0"
        if command == "READ?":
            if "2182" in self.values["*IDN"]: return "1e-6"
            status = 2048 + 4096 + (16384 if self.values["SOUR:FUNC"] == "VOLT" else 32768)
            return f"0.001,1e-9,{status}"
        return self.values.get(command[:-1], "0")

    def write(self, command):
        self.commands.append(command)
        if command == self.failure:
            raise OSError("injected bus failure")
        if command.startswith("SYST:COMM:SER:SEND "):
            inner = command.split(" ", 1)[1][1:-1].replace('""', '"')
            if inner.endswith("?"):
                self.pending = "\x13" + self.downstream.query(inner) + "\x11"
            else:
                self.downstream.write(inner)
            return
        if " " in command:
            key, value = command.split(" ", 1)
            if key == "SENS:FUNC:OFF":
                self.values["SENS:FUNC"] = ",".join(v for v in self.values["SENS:FUNC"].split(",") if v != value.strip('"'))
                return
            self.values[key] = value.replace('"', '')


def source(resource=None):
    r = resource or Resource()
    t = VisaTransport(r, role="6221", authorize_writes=True)
    return CurrentSource6221(t, expected_idn=r.values["*IDN"], max_current_a=1e-8,
        compliance_v=.1, max_step_a=2.5e-9, settle_s=0, tolerance_a=1e-13, sleep=lambda t: None), r, t


class TransportTests(unittest.TestCase):
    def test_relay_flow_control_and_quote_escaping(self):
        host = Resource()
        host.downstream = Resource("2182A")
        t = VisaTransport(host, role="6221", authorize_writes=True)
        relay = Serial2182Transport(t, sleep=lambda t: None)
        self.assertIn("MODEL 2182A", relay.query("*IDN?"))
        relay.write('SENS:FUNC "VOLT"')
        self.assertEqual(host.downstream.values["SENS:FUNC"], "VOLT")
        self.assertIn('SYST:COMM:SER:SEND "SENS:FUNC ""VOLT"""', host.commands)
        self.assertIn("\x13", t.transcript[1]["response"])

    def test_query_only_gate_has_no_settings_trigger_or_status_consumption(self):
        resource = Resource()
        resource.downstream = Resource("2182A")
        t = VisaTransport(resource, role="test")
        relay = Serial2182Transport(t, sleep=lambda t: None)
        for operation in (lambda: t.write("OUTP 1"), lambda: t.query("READ?"),
                          lambda: relay.query("READ?"), lambda: relay.query("SYST:ERR?")):
            with self.assertRaises(NktError): operation()
        self.assertEqual(resource.commands, [])
        self.assertIn("2182A", relay.query("*IDN?"))

    def test_failed_query_blocks_retry_but_off_is_best_effort(self):
        r = Resource()
        t = VisaTransport(r, role="test", authorize_writes=True)
        r.failure = "*IDN?"
        with self.assertRaises(OSError): t.query("*IDN?")
        with self.assertRaises(NktError): t.query("OUTP?")
        t.emergency_off("OUTP 0")
        self.assertTrue(t.poisoned)
        self.assertEqual(r.commands, ["*IDN?", "OUTP 0"])

    def test_invalid_response_and_multi_command_rejected(self):
        with self.assertRaises(NktError): clean_response("1\x00")
        t = VisaTransport(Resource(), role="test", authorize_writes=True)
        with self.assertRaises(NktError): t.write("OUTP 0;OUTP 1")


class CurrentSourceTests(unittest.TestCase):
    def test_limits_before_io(self):
        r = Resource()
        with self.assertRaises(NktError):
            CurrentSource6221(r, expected_idn=r.values["*IDN"], max_current_a=1e-8,
                compliance_v=.0001, max_step_a=2e-9, settle_s=0, tolerance_a=0)
        self.assertEqual(r.commands, [])

    def test_no_takeover_of_active_or_nonzero_source(self):
        for key in ("OUTP:STAT", "SOUR:CURR"):
            device, r, _ = source()
            r.values[key] = "1"
            with self.assertRaises(NktError): device.begin()
            self.assertFalse(device.owned)
            self.assertFalse(any(not c.endswith("?") for c in r.commands))

    def test_five_points_bounded_steps_and_zero_off_cleanup(self):
        device, r, _ = source()
        device.begin()
        self.assertLess(r.commands.index("SOUR:CURR:COMP 0.1"), len(r.commands))
        for point in (4e-12, 2.503e-9, 5.002e-9, 7.501e-9, 1e-8):
            device.set_bias(point)
        result = device.cleanup()
        self.assertTrue(result["safe"], result)
        levels = [0.] + [float(c.split()[1]) for c in r.commands if c.startswith("SOUR:CURR ")]
        self.assertTrue(all(abs(b-a) <= 2.5e-9 + 1e-20 for a,b in zip(levels,levels[1:])))
        self.assertEqual(r.values["OUTP:STAT"], "0")
        self.assertEqual(float(r.values["SOUR:CURR"]), 0.)
        self.assertNotIn("*RST", r.commands)

    def test_compliance_stops_ramp_and_cleanup_still_disables(self):
        device, r, _ = source()
        device.begin()
        r.trip = True
        with self.assertRaises(NktError): device.set_bias(1e-8)
        r.trip = False
        with self.assertRaises(NktError): device.set_bias(4e-12)
        self.assertTrue(device.cleanup()["safe"])
        self.assertNotIn("SOUR:CURR 1e-08", r.commands)

    def test_bus_failure_does_not_claim_cleanup_or_zero(self):
        device, r, _ = source()
        device.begin()
        device.set_bias(4e-12)
        r.failure = "SOUR:CURR?"
        with self.assertRaises(OSError): device.snapshot()
        result = device.cleanup()
        self.assertFalse(result["safe"])
        self.assertIn("OUTP:STAT 0", r.commands)
        self.assertIsNone(result["state"])


class DetectorTests(unittest.TestCase):
    def test_2182_fresh_read_and_processing_checks(self):
        r = Resource("2182A")
        t = VisaTransport(r, role="2182", authorize_writes=True)
        meter = Voltmeter2182(t, expected_idn=r.values["*IDN"], voltage_range_v=.1, nplc=1, max_voltage_v=.01)
        meter.begin()
        reading = meter.read_voltage()
        self.assertTrue(reading["fresh"])
        self.assertEqual(reading["voltage_v"], 1e-6)
        self.assertIn("READ?", r.commands)
        self.assertNotIn("FETC?", r.commands)
        r.values["SENS:VOLT:CHAN1:DFIL"] = "1"
        with self.assertRaises(NktError): meter.read_voltage()

    def test_2400_current_and_voltage_modes(self):
        for current_mode in (False, True):
            r = Resource("2400")
            t = VisaTransport(r, role="2400", authorize_writes=True)
            meter = Meter2400(t, expected_idn=r.values["*IDN"], current_mode=current_mode,
                voltage_range_v=.2, current_range_a=1e-6, compliance=1e-7 if current_mode else .2,
                nplc=1, max_voltage_v=.1, max_current_a=1e-7, max_step_v=.01, settle_s=0,
                tolerance_v=1e-6, sleep=lambda t:None)
            meter.begin()
            meter.set_bias(.03 if current_mode else None)
            self.assertEqual(meter.read_values()["current_a"],1e-9)
            self.assertTrue(meter.cleanup()["safe"])
            self.assertEqual(r.values["OUTP:SMOD"],"HIMP")
            meter.begin()
            self.assertTrue(meter.cleanup()["safe"])
            if not current_mode:
                self.assertEqual(r.values["SOUR:CURR:RANG"], "1e-06")
                self.assertEqual(r.query("SENS:VOLT:RANG:AUTO?"), "0")


if __name__ == "__main__":
    unittest.main()
