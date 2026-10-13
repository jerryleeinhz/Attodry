"""Bounded DC source and fresh electrical acquisition, with injected SCPI transports.

Command contracts: 622x-901-01 Rev C, 2182A-900-01 Rev B, 2400S-900-01 Rev K.
No reset, saved-setup recall, Delta initiation or implicit output takeover.
"""
from __future__ import annotations

from copy import deepcopy
from functools import wraps
import math
import time

from .nkt_config import NktError, number
from .photoresponse_keithley import Keithley6221Queries, Keithley2182Queries, _float, _flag, _word
from .three_smu import ramp_values


def acquisition_operation(method):
    """A failed acquisition cannot be resumed inside the same device session."""
    @wraps(method)
    def guarded(self, *args, **kwargs):
        if self.faulted:
            raise NktError("Device session faulted; cleanup and a new session are required")
        try:
            return method(self, *args, **kwargs)
        except (Exception, KeyboardInterrupt):
            self.faulted, self.ready = True, False
            raise
    return guarded


def bounded_ramp(limit, step):
    if math.ceil(2 * limit / step) > 10000:
        raise NktError("A full source ramp exceeds 10000 steps")


def same(actual, expected, *, tolerance=0.0):
    if isinstance(expected, bool):
        return _flag(actual) == expected
    if isinstance(expected, (float, int)):
        return math.isclose(_float(actual), expected, rel_tol=1e-7, abs_tol=tolerance)
    aliases = {"VOLT:DC": "VOLT", "CURR:DC": "CURR", "ASCII": "ASC", "ASC,0": "ASC"}
    actual = actual.strip().strip('"').upper()
    return aliases.get(actual, actual) == aliases.get(expected.upper(), expected.upper())


def no_error(transport):
    raw = transport.query("SYST:ERR?")
    try:
        code = int(raw.split(",", 1)[0])
    except (ValueError, IndexError) as exc:
        raise NktError("Malformed error response: " + raw) from exc
    if code:
        raise NktError("Instrument error: " + raw)


def ensure(transport, command, value, *, tolerance=0, matches=None):
    if matches is None:
        matches = lambda actual: same(actual, value, tolerance=tolerance)
    before = transport.query(command + "?")
    if not matches(before):
        argument = str(int(value)) if isinstance(value, bool) else str(value)
        if command == "SENS:FUNC":
            argument = '"' + argument + '"'
        transport.write(command + " " + argument)
        no_error(transport)
        if not matches(transport.query(command + "?")):
            raise NktError("Setting readback mismatch: " + command)


class CurrentSource6221(Keithley6221Queries):
    def __init__(self, resource, *, expected_idn, max_current_a, compliance_v,
                 max_step_a, settle_s, tolerance_a, sleep=time.sleep):
        super().__init__(resource, expected_idn=expected_idn)
        number(max_current_a, "max_current_a", 1e-13, 0.105)
        number(compliance_v, "6221 compliance_v", 0.1, 105)
        number(max_step_a, "max_step_a", 1e-13, max_current_a)
        number(settle_s, "source settle_s", 0, 3600)
        number(tolerance_a, "current tolerance_a", 0, max_step_a / 2)
        bounded_ramp(max_current_a, max_step_a)
        self.max_current_a, self.compliance_v = max_current_a, compliance_v
        self.max_step_a, self.settle_s, self.tolerance_a = max_step_a, settle_s, tolerance_a
        self.sleep = sleep
        self.owned = False
        self.ready = False
        self.faulted = False
        self.expected_current = 0.0
        self.expected_output = False

    def snapshot(self, *, authorize_queries=True):
        previous = deepcopy(self.last_confirmed_state)
        try:
            state = super().snapshot(authorize_queries=authorize_queries)
            self.state_current = False
            state["operation_condition"] = _word(self._query("STAT:OPER:COND?"))
            state["questionable_condition"] = _word(self._query("STAT:QUES:COND?"))
            # B1 means interlock asserted (output permitted), not an error.
            state["interlock_asserted"] = bool(state["measurement_condition"] & 2)
            state["unsafe_condition"] = bool(state["measurement_condition"] & 5) or bool(state["questionable_condition"])
            state["dc_takeover_verified"] = self.ready and not self.faulted
            return self._retain(state)
        except (Exception, KeyboardInterrupt):
            self.last_confirmed_state, self.state_current = previous, False
            raise

    @acquisition_operation
    def begin(self):
        state = self.snapshot()
        if state["output_enabled"] or state["source_current_setpoint_a"] != 0:
            raise NktError("6221 preflight requires source setpoint exactly zero and output off")
        if state["unsafe_condition"]:
            raise NktError("6221 unsafe preflight condition")
        no_error(self.resource)
        self.owned = True
        # An off source may still be armed. These documented aborts disarm it;
        # an unsupported speculative WAVE:ARM? query is deliberately avoided.
        for command in ("SOUR:WAVE:ABOR", "SOUR:SWE:ABOR"):
            self.resource.write(command)
            no_error(self.resource)
        ensure(self.resource, "SOUR:CURR:COMP", self.compliance_v)
        ensure(self.resource, "SOUR:CURR:RANG:AUTO", True)
        state = self.snapshot()
        if state["output_enabled"] or state["source_current_setpoint_a"] != 0:
            raise NktError("6221 did not remain zero/off during DC configuration")
        self.ready = True

    @acquisition_operation
    def check(self, state, *, check_compliance=True):
        if state["unsafe_condition"] or (check_compliance and state["compliance_tripped"]):
            raise NktError("6221 compliance or unsafe condition")
        if state["output_enabled"] != self.expected_output:
            raise NktError("6221 output changed externally")
        if not math.isclose(state["source_current_setpoint_a"], self.expected_current,
                            abs_tol=self.tolerance_a, rel_tol=1e-7):
            raise NktError("6221 current setting changed")
        if not math.isclose(state["voltage_compliance_v"], self.compliance_v, rel_tol=1e-7):
            raise NktError("6221 compliance setting changed")

    @acquisition_operation
    def set_bias(self, target):
        if not self.ready:
            raise NktError("6221 has not completed DC preflight")
        number(target, "current target", -self.max_current_a, self.max_current_a)
        state = self.snapshot()
        self.check(state)
        if not self.expected_output:
            ensure(self.resource, "OUTP:STAT", True)
            self.expected_output = True
        for value in ramp_values(self.expected_current, target, self.max_step_a):
            self.check(self.snapshot())
            ensure(self.resource, "SOUR:CURR", value, tolerance=self.tolerance_a)
            self.expected_current = value
            self.sleep(self.settle_s)
            self.check(self.snapshot())
        no_error(self.resource)

    def cleanup(self):
        if not self.owned:
            return {"safe": False, "errors": ["6221 not owned; no takeover"]}
        errors = []
        self.ready = False
        try:
            state = self.snapshot()
            current = state["source_current_setpoint_a"]
            number(current, "cleanup current", -self.max_current_a, self.max_current_a)
            for value in ramp_values(current, 0.0, self.max_step_a):
                ensure(self.resource, "SOUR:CURR", value, tolerance=self.tolerance_a)
                self.sleep(self.settle_s)
        except (Exception, KeyboardInterrupt) as exc:
            errors.append("ramp: " + str(exc))
        try:
            self.resource.emergency_off("OUTP:STAT 0")
        except (Exception, KeyboardInterrupt) as exc:
            errors.append("disable: " + str(exc))
        state = None
        try:
            state = self.snapshot()
            if state["output_enabled"] or abs(state["source_current_setpoint_a"]) > self.tolerance_a:
                raise NktError("6221 zero/off not confirmed")
        except (Exception, KeyboardInterrupt) as exc:
            errors.append("verification: " + str(exc))
        self.expected_current, self.expected_output = 0.0, False
        return {"safe": not errors, "state": state, "errors": errors}


class Voltmeter2182(Keithley2182Queries):
    def __init__(self, resource, *, expected_idn, voltage_range_v, nplc, max_voltage_v):
        super().__init__(resource, expected_idn=expected_idn)
        if voltage_range_v not in (0.01, 0.1, 1.0, 10.0, 100.0):
            raise NktError("2182A range must be 10 mV, 100 mV, 1 V, 10 V or 100 V")
        number(nplc, "2182 NPLC", 0.01, 50)
        number(max_voltage_v, "max_voltage_v", 1e-15, voltage_range_v)
        self.range, self.nplc, self.limit = voltage_range_v, nplc, max_voltage_v
        self.ready = False
        self.faulted = False

    @property
    def settings(self):
        # An inherited internal scan can reject filter changes with -221.
        # Leave scanning/derived modes before configuring single-channel DCV.
        return {"INIT:CONT": False, "ROUT:SCAN:LSEL": "NONE",
                "SENS:VOLT:RAT": False, "SENS:VOLT:DELT": False,
                "SENS:CHAN": 1, "SENS:FUNC": "VOLT",
                "TRIG:SOUR": "IMM", "TRIG:COUN": 1, "TRIG:DEL": 0,
                "SENS:VOLT:DC:RANG:AUTO": False, "SENS:VOLT:DC:RANG": self.range,
                "SENS:VOLT:DC:NPLC": self.nplc, "SENS:VOLT:CHAN1:DFIL": False,
                "SENS:VOLT:CHAN1:LPAS": False, "SENS:VOLT:CHAN1:REF:STAT": False,
                "CALC:STAT": False, "SENS:HOLD:STAT": False, "SYST:AZER": True,
                "SYST:FAZ": True, "SYST:LSYN": True, "FORM:DATA": "ASC",
                "FORM:ELEM": "READ"}

    @acquisition_operation
    def begin(self):
        self._identity(True)
        if "MODEL 2182" not in self.expected_idn:
            raise NktError("Expected a 2182/2182A")
        no_error(self.resource)
        self.resource.write("ABOR")
        no_error(self.resource)
        for command, value in self.settings.items():
            ensure(self.resource, command, value)
        self.verify()
        self.ready = True

    @acquisition_operation
    def verify(self):
        identity = self._identity(True)
        for command, expected in self.settings.items():
            if not same(self._query(command + "?"), expected):
                raise NktError("2182 acquisition setting changed: " + command)
        state = self._retain({"identity": identity, "fresh_acquisition_configured": True})
        state["acquisition_ready"] = True
        self.last_confirmed_state = deepcopy(state)
        return state

    @acquisition_operation
    def read_voltage(self):
        if not self.ready:
            raise NktError("2182 fresh acquisition is not configured")
        self.verify()
        self.state_current = False
        started = time.perf_counter()
        raw = self._query("READ?")  # documented ABORt + INITiate + FETCh
        finished = time.perf_counter()
        self.last_raw_reading = raw
        value = number(_float(raw), "2182 voltage", -self.limit, self.limit)
        no_error(self.resource)
        self.verify()
        return {"voltage_v": value, "current_a": None, "fresh": True, "raw_response": raw,
                "acquisition_started_monotonic_s": started, "acquisition_finished_monotonic_s": finished}


class Meter2400:
    """2400 source-V/current sensing or documented zero-A/fixed-range voltmeter."""
    def __init__(self, resource, *, expected_idn, current_mode, voltage_range_v,
                 current_range_a, compliance, nplc, max_voltage_v, max_current_a,
                 max_step_v, settle_s, tolerance_v, sleep=time.sleep):
        if type(current_mode) is not bool:
            raise NktError("2400 current_mode must be boolean")
        if voltage_range_v not in (.2, 2., 20., 200.) or current_range_a not in (1e-6, 1e-5, 1e-4, .001, .01, .1, 1.):
            raise NktError("Unsupported 2400 fixed range")
        number(max_voltage_v, "2400 voltage limit", 1e-15, voltage_range_v)
        number(max_current_a, "2400 current limit", 1e-12, current_range_a)
        number(nplc, "2400 NPLC", .01, 10)
        number(max_step_v, "2400 voltage step", 1e-6, max_voltage_v)
        number(settle_s, "2400 settle_s", 0, 3600)
        number(tolerance_v, "2400 tolerance_v", 0, max_step_v / 2)
        bounded_ramp(max_voltage_v, max_step_v)
        if current_mode:
            number(compliance, "2400 current compliance", max(1e-9, current_range_a * .001), max_current_a)
        else:
            number(compliance, "2400 voltage compliance", max(.02, voltage_range_v * .001), 210)
            if compliance <= max_voltage_v:
                raise NktError("Voltmeter compliance must exceed maximum expected voltage")
        self.resource, self.expected_idn = resource, expected_idn
        self.current_mode = current_mode
        self.voltage_range, self.current_range = voltage_range_v, current_range_a
        self.compliance, self.nplc = compliance, nplc
        self.max_voltage, self.max_current = max_voltage_v, max_current_a
        self.max_step, self.settle_s, self.tolerance = max_step_v, settle_s, tolerance_v
        self.sleep = sleep
        self.owned = self.ready = False
        self.faulted = False
        self.expected = 0.0
        self.last_confirmed_state = None
        self.partial_raw = {}

    def q(self, command):
        raw = self.resource.query(command)
        self.partial_raw[command] = raw
        return raw

    def snapshot(self):
        self.partial_raw = {}
        identity = self.q("*IDN?")
        if identity != self.expected_idn or identity.split(",")[1].strip() != "MODEL 2400":
            raise NktError("2400 identity mismatch")
        mode = self.q("SOUR:FUNC?").strip('"').upper()
        if mode not in ("VOLT", "CURR"):
            raise NktError("Unknown 2400 source mode")
        state = {"identity": identity, "output_enabled": _flag(self.q("OUTP?")),
                 "mode": mode, "setpoint": _float(self.q(f"SOUR:{mode}:LEV?"))}
        self.last_confirmed_state = deepcopy(state)
        return state

    @property
    def settings(self):
        mode = "VOLT" if self.current_mode else "CURR"
        return {"SOUR:FUNC": mode, f"SOUR:{mode}:MODE": "FIX",
                f"SOUR:{mode}:RANG:AUTO": False,
                f"SOUR:{mode}:RANG": self.voltage_range if self.current_mode else 1e-6,
                "SENS:FUNC:CONC": True, "SENS:FUNC": "VOLT:DC,CURR:DC",
                "SENS:VOLT:RANG:AUTO": False, "SENS:VOLT:RANG": self.voltage_range,
                "SENS:CURR:RANG:AUTO": False, "SENS:CURR:RANG": self.current_range,
                "SENS:CURR:NPLC": self.nplc, "SENS:VOLT:NPLC": self.nplc,
                "SENS:CURR:PROT" if self.current_mode else "SENS:VOLT:PROT": self.compliance,
                "SYST:RSEN": False, "SYST:AZER": True, "SENS:AVER:STAT": False,
                "CALC:STAT": False, "CALC2:NULL:STAT": False,
                "SOUR:DEL:AUTO": False, "SOUR:DEL": 0, "TRIG:DEL": 0, "ARM:SOUR": "IMM", "ARM:COUN": 1,
                "TRIG:SOUR": "IMM", "TRIG:COUN": 1, "SOUR:CLE:AUTO": False,
                "OUTP:SMOD": "HIMP", "FORM:DATA": "ASC", "FORM:ELEM": "VOLT,CURR,STAT"}

    def _matches(self, command, response, expected):
        if command == "SENS:FUNC":
            return set(response.replace('"', '').split(",")) == {"VOLT:DC", "CURR:DC"}
        if command in ("SOUR:VOLT:RANG", "SOUR:CURR:RANG", "SENS:VOLT:RANG", "SENS:CURR:RANG"):
            # 2400 firmware reports the full scale (e.g. 2.10 V / 1.05 uA),
            # while configuration uses nominal ranges (2 V / 1 uA).
            return same(response, expected) or same(response, expected * 1.05)
        return same(response, expected)

    @acquisition_operation
    def begin(self):
        state = self.snapshot()
        if state["output_enabled"] or state["setpoint"] != 0:
            raise NktError("2400 preflight requires zero/off; active sample is never taken over")
        no_error(self.resource)
        self.owned = True
        # Establish safe off behavior before any later configuration can fail.
        ensure(self.resource, "OUTP:SMOD", "HIMP")
        self.resource.write("ABOR")
        no_error(self.resource)
        # Both stored source levels must be zero BEFORE changing function.
        ensure(self.resource, "SOUR:VOLT:LEV", 0)
        ensure(self.resource, "SOUR:CURR:LEV", 0)
        if "RES" in self.q("SENS:FUNC?").upper():
            self.resource.write('SENS:FUNC:OFF "RES"')
            no_error(self.resource)
        for command, expected in self.settings.items():
            if command == "SENS:FUNC":
                if not self._matches(command, self.q(command + "?"), expected):
                    self.resource.write('SENS:FUNC "VOLT:DC","CURR:DC"')
                    no_error(self.resource)
            else:
                ensure(self.resource, command, expected,
                       matches=lambda actual: self._matches(command, actual, expected))
        self.verify(output=False)
        ensure(self.resource, "OUTP", True)
        self.ready = True
        self.verify()

    @acquisition_operation
    def verify(self, *, output=True):
        state = self.snapshot()
        if state["output_enabled"] != output or not math.isclose(state["setpoint"], self.expected,
                abs_tol=self.tolerance if self.current_mode else 1e-15, rel_tol=1e-7):
            raise NktError("2400 source changed")
        for command, expected in self.settings.items():
            if not self._matches(command, self.q(command + "?"), expected):
                raise NktError("2400 setting changed: " + command)
        sense = "CURR" if self.current_mode else "VOLT"
        state["compliance_tripped"] = _flag(self.q(f"SENS:{sense}:PROT:TRIP?"))
        if state["compliance_tripped"]:
            raise NktError("2400 compliance trip")
        return state

    @acquisition_operation
    def set_bias(self, target):
        if not self.ready:
            raise NktError("2400 is not prepared")
        if not self.current_mode:
            if target is not None:
                raise NktError("Voltmeter accepts no voltage bias")
            return
        number(target, "2400 voltage bias", -self.max_voltage, self.max_voltage)
        self.verify()
        for value in ramp_values(self.expected, target, self.max_step):
            ensure(self.resource, "SOUR:VOLT:LEV", value, tolerance=self.tolerance)
            self.expected = value
            self.sleep(self.settle_s)
            self.read_values()

    @acquisition_operation
    def read_values(self):
        if not self.ready:
            raise NktError("2400 acquisition is not configured")
        self.verify()
        started = time.perf_counter()
        raw = self.q("READ?")
        finished = time.perf_counter()
        self.last_raw_reading = raw
        fields = raw.split(",")
        if len(fields) != 3:
            raise NktError("2400 requires voltage,current,status response")
        voltage = number(_float(fields[0]), "2400 voltage", -self.max_voltage, self.max_voltage)
        current = number(_float(fields[1]), "2400 current", -self.max_current, self.max_current)
        value = _float(fields[2])
        if not value.is_integer() or not 0 <= value < 2**24:
            raise NktError("Malformed 2400 24-bit reading status")
        status = int(value)
        # Section 18-50/51: only V/I measurement, selected source and terminal
        # indication are expected. Reject compliance (including range compliance),
        # overflow, OVP, processing, ohms, contact failure and remote/pulse modes.
        required = (1 << 11) | (1 << 12) | (1 << (14 if self.current_mode else 15))
        if status & required != required or status & ~(required | (1 << 2)):
            raise NktError("2400 reading status violates the configured DC acquisition")
        no_error(self.resource)
        self.verify()
        return {"voltage_v": voltage, "current_a": current, "fresh": True,
                "raw_response": raw, "reading_status": status,
                "acquisition_started_monotonic_s": started, "acquisition_finished_monotonic_s": finished}

    def cleanup(self):
        errors = []
        if not self.owned:
            return {"safe": False, "errors": ["2400 not owned; no takeover"]}
        try:
            state = self.snapshot()
            if self.current_mode:
                number(state["setpoint"], "cleanup voltage", -self.max_voltage, self.max_voltage)
                if state["mode"] != "VOLT":
                    raise NktError("2400 source mode changed during run")
                for value in ramp_values(state["setpoint"], 0, self.max_step):
                    ensure(self.resource, "SOUR:VOLT:LEV", value, tolerance=self.tolerance)
                    self.sleep(self.settle_s)
            elif state["mode"] != "CURR" or state["setpoint"] != 0:
                raise NktError("2400 voltmeter zero-current setting changed")
        except (Exception, KeyboardInterrupt) as exc:
            errors.append("ramp: " + str(exc))
        try:
            self.resource.emergency_off("OUTP 0")
        except (Exception, KeyboardInterrupt) as exc:
            errors.append("disable: " + str(exc))
        state = None
        try:
            state = self.snapshot()
            if state["output_enabled"] or state["setpoint"] != 0:
                raise NktError("2400 zero/off not confirmed")
            if self.q("OUTP:SMOD?").strip('"') != "HIMP":
                raise NktError("2400 high-impedance off mode not confirmed")
        except (Exception, KeyboardInterrupt) as exc:
            errors.append("verification: " + str(exc))
        self.ready = False
        self.expected = 0.0
        return {"safe": not errors, "state": state, "errors": errors}
