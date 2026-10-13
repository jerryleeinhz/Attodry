"""CLD1010LP point controller with TL4000 and USB/VISA SCPI backends.

API names/types follow Thorlabs' official CLD1010 ctypes example and TL4000
reference XML. Fake-tested; installed DLL/diode/firmware require commissioning.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import ctypes as ct
import math
from pathlib import Path
import time

from .nkt_config import NktError, keys, number, text
from .nkt_control import utc_now


@dataclass(frozen=True)
class CldPoint:
    current_a: float
    wavelength_nm: float  # nominal diode wavelength, not a tuned/readback wavelength


@dataclass(frozen=True)
class CldConfig:
    backend: str
    points: tuple[CldPoint, ...]
    dll_path: str
    address: str
    expected_serial: str
    diode_model: str
    diode_wiring_confirmed: bool
    max_current_a: float
    max_voltage_v: float
    max_tec_current_a: float
    temperature_min_c: float
    temperature_max_c: float
    temperature_c: float
    temperature_tolerance_c: float
    stable_dwell_s: float
    current_tolerance_a: float
    manual_route: str
    timeout_s: float
    poll_interval_s: float
    output_directory: Path
    run_name: str
    note: str
    emit: bool = True
    dwell_s: float = 0.0

    def validate(self):
        if self.backend not in ("simulation", "tl4000", "visa"):
            raise NktError("CLD backend must be simulation, tl4000 or visa")
        for name in ("diode_model", "manual_route", "run_name"):
            text(getattr(self, name), name)
        if type(self.diode_wiring_confirmed) is not bool:
            raise NktError("diode_wiring_confirmed must be boolean")
        number(self.max_current_a, "diode max_current_a", 1e-9, 1.)
        number(self.max_voltage_v, "diode max_voltage_v", .01, 10)
        number(self.max_tec_current_a, "max_tec_current_a", .001, 3)
        number(self.temperature_min_c, "temperature_min_c", -20, 80)
        number(self.temperature_max_c, "temperature_max_c", self.temperature_min_c, 80)
        number(self.temperature_c, "temperature_c", self.temperature_min_c, self.temperature_max_c)
        number(self.temperature_tolerance_c, "temperature_tolerance_c", .001, 5)
        if not self.temperature_min_c <= self.temperature_c-self.temperature_tolerance_c < self.temperature_c+self.temperature_tolerance_c <= self.temperature_max_c:
            raise NktError("Temperature stability band must fit inside diode limits")
        number(self.stable_dwell_s, "stable_dwell_s", .1, 3600)
        number(self.timeout_s, "timeout_s", self.stable_dwell_s, 3600)
        number(self.poll_interval_s, "poll_interval_s", .01, self.timeout_s)
        number(self.current_tolerance_a, "current_tolerance_a", 0, self.max_current_a / 100)
        if not self.points or len(self.points) > 10000:
            raise NktError("Provide 1 to 10000 diode current points")
        for point in self.points:
            number(point.current_a, "diode current", 0, self.max_current_a)
            number(point.wavelength_nm, "nominal wavelength", 100, 10000)

    def require_hardware(self, writes=False):
        self.validate()
        if self.backend not in ("tl4000", "visa"):
            raise NktError("Expected CLD hardware backend")
        names = ("address", "expected_serial", "diode_model")
        if self.backend == "tl4000":
            names += ("dll_path",)
        for name in names:
            value = text(getattr(self, name), name)
            if "CHANGE_ME" in value:
                raise NktError("Configure " + name + " before CLD connection")
        if writes and not self.diode_wiring_confirmed:
            raise NktError("Exact diode pinout/TEC/limits must be physically confirmed")

    def snapshot(self):
        result = asdict(self)
        result["output_directory"] = str(self.output_directory)
        return result


def load_cld_config(doc, path):
    table = dict(doc.get("cld1010", {}))
    fields = set(CldConfig.__dataclass_fields__) - {"points", "emit", "dwell_s", "dll_path"}
    keys(table, "cld1010", fields | {"current_points_a", "wavelength_nm"}, {"dll_path"})
    table.setdefault("dll_path", "")
    points = table.pop("current_points_a")
    if not isinstance(points, list):
        raise NktError("current_points_a must be an array")
    wavelength = table.pop("wavelength_nm")
    directory = Path(table["output_directory"])
    table["output_directory"] = directory if directory.is_absolute() else Path(path).resolve().parent / directory
    result = CldConfig(points=tuple(CldPoint(p, wavelength) for p in points), **table)
    result.validate()
    return result


class TL4000:
    is_hardware = True
    SHORT_GETTERS = {"getLdOperatingMode", "getTecOperatingMode", "getTempUnit", "getTempProtMode"}
    BOOL_GETTERS = {"getLdOutputState", "getTecOutputState", "getLdOutpAutoOnState",
                    "isTrippedLdOutputProtInterlock", "isTrippedTecOutputProtTransducer",
                    "isTrippedTecOutputProtOvertemp", "isTrippedTecOutputProtVoltage"}
    DOUBLE_GETTERS = {"measTemp", "measLdCurr", "measLdVolt"}
    SETTING_GETTERS = {"getLdCurrLimit", "getLdCurrSetpoint", "getTempSetpoint", "getTecCurrLimit", "getTempProtWindow"}

    def __init__(self, config, *, authorize_connection=False, authorize_writes=False, dll=None, on_event=None):
        config.require_hardware(writes=authorize_writes)
        if config.backend != "tl4000":
            raise NktError("Expected TL4000 backend")
        if not authorize_connection:
            raise NktError("CLD connection requires authorization")
        self.config, self.writes, self.on_event = config, authorize_writes is True, on_event
        if ct.sizeof(ct.c_void_p) != 8:
            raise NktError("TL4000_64 requires 64-bit Python")
        self.dll = dll if dll is not None else ct.CDLL(str(Path(config.dll_path).resolve()))
        self.session = ct.c_uint32()
        self.opened = False
        self.transcript = []
        signatures = {
            "init": [ct.c_char_p, ct.c_uint16, ct.c_uint16, ct.POINTER(ct.c_uint32)],
            "close": [ct.c_uint32],
            "identificationQuery": [ct.c_uint32] + [ct.c_char_p] * 4,
        }
        for name in self.SHORT_GETTERS: signatures[name] = [ct.c_uint32, ct.POINTER(ct.c_int16)]
        for name in self.BOOL_GETTERS: signatures[name] = [ct.c_uint32, ct.POINTER(ct.c_uint16)]
        for name in self.DOUBLE_GETTERS: signatures[name] = [ct.c_uint32, ct.POINTER(ct.c_double)]
        for name in self.SETTING_GETTERS: signatures[name] = [ct.c_uint32, ct.c_int16, ct.POINTER(ct.c_double)]
        for name in ("setLdCurrLimit", "setLdCurrSetpoint", "setTempSetpoint"):
            signatures[name] = [ct.c_uint32, ct.c_double]
        signatures["setLdOperatingMode"] = [ct.c_uint32, ct.c_int16]
        for name in ("switchLdOutput", "switchTecOutput"):
            signatures[name] = [ct.c_uint32, ct.c_uint16]
        for name, args in signatures.items():
            fn = getattr(self.dll, "TL4000_" + name)
            try: fn.argtypes, fn.restype = args, ct.c_int32
            except AttributeError: pass  # injected Python fake API
        # idQuery=true, reset=false. Manufacturer example's reset=true is not copied.
        try:
            self._call("init", config.address.encode("ascii"), ct.c_uint16(1), ct.c_uint16(0), ct.byref(self.session))
        except (Exception, KeyboardInterrupt):
            if self.session.value:
                try:
                    self._call("close", self.session)
                except (Exception, KeyboardInterrupt):
                    pass  # preserve init failure; close attempt remains in transcript
            raise
        self.opened = True

    def _call(self, name, *args):
        row = {"phase": "cld_api", "function": name, "captured_at_utc": utc_now()}
        self.transcript.append(row)
        try:
            status = getattr(self.dll, "TL4000_" + name)(*args)
            row["status"] = status
            if status != 0:
                raise NktError(f"TL4000 {name} returned {status}; operation unconfirmed")
        except (Exception, KeyboardInterrupt) as exc:
            row["error"] = str(exc)
            raise
        finally:
            if self.on_event:
                try: self.on_event(deepcopy(row))
                except (Exception, KeyboardInterrupt) as exc:
                    row["audit_error"] = str(exc)
                    if "error" not in row: raise

    def get(self, name):
        value = ct.c_int16() if name in self.SHORT_GETTERS else ct.c_uint16() if name in self.BOOL_GETTERS else ct.c_double()
        args = [self.session]
        if name in self.SETTING_GETTERS: args.append(ct.c_int16(0))
        self._call(name, *args, ct.byref(value))
        if name in self.BOOL_GETTERS:
            if value.value not in (0,1): raise NktError("Unknown CLD boolean")
            return bool(value.value)
        return value.value

    def set(self, name, value):
        if not self.writes: raise NktError("CLD write authorization required")
        if name.startswith("switch"): typed = ct.c_uint16(int(value))
        elif name == "setLdOperatingMode": typed = ct.c_int16(value)
        else: typed = ct.c_double(value)
        self._call(name, self.session, typed)

    def snapshot(self):
        strings = [ct.create_string_buffer(256) for _ in range(4)]
        self._call("identificationQuery", self.session, *strings)
        manufacturer, model, serial, firmware = [s.value.decode("ascii") for s in strings]
        if model != "CLD1010LP" or serial != self.config.expected_serial:
            raise NktError("Unexpected CLD model/serial")
        result = {"identity": [manufacturer, model, serial, firmware]}
        for name in sorted(self.SHORT_GETTERS | self.BOOL_GETTERS | self.DOUBLE_GETTERS | self.SETTING_GETTERS):
            result[name] = self.get(name)
        return result

    def close(self):
        if self.opened:
            self._call("close", self.session)
            self.opened = False


class VisaCld:
    """Translate the existing CLD primitives to documented, audited USB SCPI.

    The adapter opens one explicit resource without a reset or discovery. It
    keeps TL4000 enum values so both backends use the same point controller.
    """
    is_hardware = True
    QUERIES = {
        "getLdOperatingMode": "SOUR1:FUNC:MODE?",
        "getTecOperatingMode": "SOUR2:FUNC:MODE?",
        "getTempUnit": "UNIT:TEMP?",
        "getTempProtMode": "OUTP1:PROT:TEMP:MODE?",
        "getLdOutputState": "OUTP1:STAT?",
        "getTecOutputState": "OUTP2:STAT?",
        "getLdOutpAutoOnState": "OUTP1:PON:STAT?",
        "isTrippedLdOutputProtInterlock": "OUTP1:PROT:INTL:TRIP?",
        "isTrippedTecOutputProtCable": "OUTP2:PROT:CONN:TRIP?",
        "isTrippedTecOutputProtTransducer": "OUTP2:PROT:TRAN:TRIP?",
        "isTrippedTecOutputProtOvertemp": "OUTP2:PROT:OTEM:TRIP?",
        "isTrippedTecOutputProtVoltage": "OUTP2:PROT:COMP:TRIP?",
        "getLdCurrLimit": "SOUR1:CURR:LIM:AMPL?",
        "getLdCurrSetpoint": "SOUR1:CURR:LEV:IMM:AMPL?",
        "getTempSetpoint": "SOUR2:TEMP:SPO?",
        "getTecCurrLimit": "SOUR2:CURR:LIM:AMPL?",
        "getTempProtWindow": "SENS2:TEMP:PROT:WIND:AMPL?",
        "measTemp": "SENS2:TEMP:DATA?",
        "measLdCurr": "SENS3:CURR:DC:DATA?",
        "measLdVolt": "SENS4:VOLT:DC:DATA?",
    }
    ENUMS = {
        "getLdOperatingMode": {"CURR": 0, "CURRENT": 0, "POW": 1, "POWER": 1},
        "getTecOperatingMode": {"CURR": 0, "CURRENT": 0, "TEMP": 1, "TEMPERATURE": 1},
        "getTempUnit": {"C": 0, "CEL": 0, "CELSIUS": 0, "K": 1, "KELVIN": 1,
                        "F": 2, "FAR": 2, "FAHRENHEIT": 2},
        "getTempProtMode": {"OFF": 0, "ENAB": 1, "ENABLE": 1, "PROT": 2, "PROTECTION": 2},
    }
    SETTERS = {
        "setLdCurrLimit": ("SOUR1:CURR:LIM:AMPL", "getLdCurrLimit"),
        "setLdCurrSetpoint": ("SOUR1:CURR:LEV:IMM:AMPL", "getLdCurrSetpoint"),
        "setLdOperatingMode": ("SOUR1:FUNC:MODE", "getLdOperatingMode"),
        "setTempSetpoint": ("SOUR2:TEMP:SPO", "getTempSetpoint"),
        "switchLdOutput": ("OUTP1:STAT", "getLdOutputState"),
        "switchTecOutput": ("OUTP2:STAT", "getTecOutputState"),
    }

    def __init__(self, config, *, authorize_connection=False, authorize_writes=False,
                 on_event=None, manager_factory=None):
        config.require_hardware(writes=authorize_writes)
        if config.backend != "visa":
            raise NktError("Expected CLD VISA backend")
        if not config.address.upper().startswith("USB") or not config.address.upper().endswith("::INSTR"):
            raise NktError("CLD VISA backend requires an explicit USB INSTR resource")
        from .photoresponse_transport import visa_resources
        self.config, self.writes = config, authorize_writes is True
        self.opened = False
        # timeout_s bounds thermal settling; one USB transaction must remain
        # short enough for cleanup to proceed when a response is lost.
        self._resources = visa_resources({"cld": config.address}, timeout_ms=min(10000, max(1, int(config.timeout_s*1000))),
            authorize_connection=authorize_connection, authorize_writes=authorize_writes,
            on_event=on_event, manager_factory=manager_factory)
        self.transport = self._resources.__enter__()["cld"]
        self.transcript = self.transport.transcript
        self.opened = True

    def _identity(self):
        if not self.opened:
            raise NktError("CLD session closed")
        identity = [value.strip() for value in self.transport.query("*IDN?").split(",")]
        if (len(identity) != 4 or identity[0] != "Thorlabs" or identity[1] != "CLD1010LP"
                or identity[2] != self.config.expected_serial or not identity[3]):
            raise NktError("Unexpected CLD manufacturer/model/serial")
        return identity

    def get(self, name):
        if not self.opened or name not in self.QUERIES:
            raise NktError("Unknown CLD getter: " + name)
        raw = self.transport.query(self.QUERIES[name])
        if name in self.ENUMS:
            if raw.upper() not in self.ENUMS[name]:
                raise NktError("Unknown CLD enum for " + name + ": " + raw)
            return self.ENUMS[name][raw.upper()]
        if name in TL4000.BOOL_GETTERS or name == "isTrippedTecOutputProtCable":
            if raw.upper() not in ("0", "+0", "OFF", "1", "+1", "ON"):
                raise NktError("Unknown CLD boolean for " + name + ": " + raw)
            return raw.upper() in ("1", "+1", "ON")
        try:
            value = float(raw)
        except ValueError as exc:
            raise NktError("Invalid CLD numeric response for " + name) from exc
        if not math.isfinite(value):
            raise NktError("Non-finite CLD response for " + name)
        return value

    def _no_error(self):
        raw = self.transport.query("SYST:ERR?")
        code = raw.split(",", 1)[0].strip()
        if code not in ("0", "+0"):
            raise NktError("CLD instrument error: " + raw)

    @staticmethod
    def _same(actual, expected):
        if type(expected) in (bool, int):
            return actual == expected
        return math.isclose(actual, expected, abs_tol=1e-12, rel_tol=1e-7)

    def set(self, name, value):
        if not self.writes:
            raise NktError("CLD write authorization required")
        if not self.opened or name not in self.SETTERS:
            raise NktError("CLD session closed or setter unknown")
        command, getter = self.SETTERS[name]
        if name.startswith("switch"):
            if type(value) is not bool:
                raise NktError("CLD output state must be boolean")
            argument = str(int(value))
            if not value:
                # Always attempt a shutdown even when pending I/O is uncertain.
                self.transport.emergency_off(command + " 0")
                self._no_error()
                if self.get(getter):
                    raise NktError("CLD output-off readback mismatch")
                return
        elif name == "setLdOperatingMode":
            if type(value) is not int or value != 0:
                raise NktError("Only constant-current LD operation is supported")
            argument = "CURR"
        else:
            limit = self.config.max_current_a if name != "setTempSetpoint" else self.config.temperature_max_c
            minimum = self.config.temperature_min_c if name == "setTempSetpoint" else 0.
            if name == "setLdCurrLimit": minimum = 1e-9
            number(value, name, minimum, limit)
            argument = format(value, ".12g")
        self._identity()
        self._no_error()
        if name == "switchLdOutput":
            limit = self.get("getLdCurrLimit")
            number(limit, "LD current limit", 1e-9, self.config.max_current_a)
            number(self.get("getLdCurrSetpoint"), "LD current setpoint", 0., limit)
            if self.get("getLdOperatingMode") != 0:
                raise NktError("CLD LD output requires constant-current mode")
        elif name == "switchTecOutput":
            number(self.get("getTecCurrLimit"), "TEC current limit", .001, self.config.max_tec_current_a)
            if self.get("getTecOperatingMode") != 1:
                raise NktError("CLD TEC output requires temperature mode")
        if self._same(self.get(getter), value):
            return
        self.transport.write(command + " " + argument)
        self._no_error()
        actual = self.get(getter)
        if not self._same(actual, value) or (name == "setLdCurrLimit" and actual > self.config.max_current_a):
            raise NktError("CLD settings readback mismatch for " + name)

    def snapshot(self):
        return {"identity": self._identity(), **{name: self.get(name) for name in self.QUERIES}}

    def close(self):
        if self.opened:
            self.opened = False
            self._resources.__exit__(None, None, None)


class SimulatedCld:
    is_hardware = False
    def __init__(self, config):
        self.config = config
        self.state = {"identity": ["SIMULATED", "CLD1010LP", "SIMULATED", "0"],
            "getLdOperatingMode": 0, "getTecOperatingMode": 1, "getTempUnit": 0,
            "getTempProtMode": 2, "getLdOutputState": False, "getTecOutputState": False,
            "getLdOutpAutoOnState": False, "isTrippedLdOutputProtInterlock": False,
            "isTrippedTecOutputProtCable": False, "isTrippedTecOutputProtTransducer": False,
            "isTrippedTecOutputProtOvertemp": False, "isTrippedTecOutputProtVoltage": False,
            "getLdCurrLimit": config.max_current_a, "getLdCurrSetpoint": 0.,
            "getTempSetpoint": config.temperature_c, "getTecCurrLimit": config.max_tec_current_a,
            "getTempProtWindow": config.temperature_tolerance_c, "measTemp": config.temperature_c,
            "measLdCurr": 0., "measLdVolt": 0.}
    def snapshot(self): return deepcopy(self.state)
    def set(self, name, value):
        keys = {"setLdCurrLimit":"getLdCurrLimit", "setLdCurrSetpoint":"getLdCurrSetpoint",
                "setLdOperatingMode":"getLdOperatingMode", "setTempSetpoint":"getTempSetpoint",
                "switchLdOutput":"getLdOutputState", "switchTecOutput":"getTecOutputState"}
        self.state[keys[name]] = value
        self.state["measLdCurr"] = self.state["getLdCurrSetpoint"] if self.state["getLdOutputState"] else 0.
    def close(self): pass


class CldController:
    def __init__(self, config, backend, *, clock=time.monotonic, sleep=time.sleep, on_event=None):
        config.validate()
        self.config, self.backend, self.clock, self.sleep, self.on_event = config, backend, clock, sleep, on_event
        self.owned = self.closed = self.failed = False
        self.point = None
        self.initial_tec = None
        self.record = {"events": [], "last_confirmed_state": None, "state_current": False,
                       "cleanup": {"off_confirmed": False, "errors": []}}

    def capture(self, phase):
        self.record["state_current"] = False
        state = self.backend.snapshot()
        self.record["last_confirmed_state"] = deepcopy(state)
        self.record["state_current"] = True
        event = {"phase": phase, "captured_at_utc": utc_now(), "cld": state}
        self.record["events"].append(event)
        if self.on_event: self.on_event(event)
        return state

    def healthy(self, state):
        c = self.config
        if state["isTrippedLdOutputProtInterlock"] or state["getLdOutpAutoOnState"]:
            raise NktError("CLD interlock/auto-on prevents operation")
        faults = ("isTrippedTecOutputProtTransducer", "isTrippedTecOutputProtOvertemp",
                  "isTrippedTecOutputProtVoltage")
        # The CLD SCPI connection query is documented; firmware behavior remains
        # under commissioning. Keep an asserted flag fail-closed. The
        # TL4000 cable getter is documented only for TED/ITC, not CLD devices.
        if "isTrippedTecOutputProtCable" in state:
            faults += ("isTrippedTecOutputProtCable",)
        for name in faults:
            if type(state[name]) is not bool or state[name]:
                raise NktError("CLD TEC protection prevents operation: " + name)
        if state["getTempUnit"] != 0 or state["getTecOperatingMode"] != 1 or state["getTempProtMode"] != 2:
            raise NktError("CLD requires Celsius, constant-temperature TEC and LD temperature protection")
        number(state["getTecCurrLimit"], "TEC current limit", .001, c.max_tec_current_a)
        number(state["measTemp"], "diode temperature", c.temperature_min_c, c.temperature_max_c)
        number(state["measLdVolt"], "diode voltage", 0, c.max_voltage_v)
        number(state["measLdCurr"], "diode current", 0, c.max_current_a)
        number(state["getTempProtWindow"], "temperature protection", .001,
               min(c.temperature_c-c.temperature_min_c, c.temperature_max_c-c.temperature_c))

    def stable(self, state):
        return state["getTecOutputState"] and abs(state["measTemp"]-self.config.temperature_c) <= self.config.temperature_tolerance_c

    def begin(self, *, authorize_writes=False, confirm_manual_route=False):
        if self.owned or self.closed: raise NktError("CLD sessions are single-use")
        if self.backend.is_hardware:
            self.config.require_hardware(writes=True)
            if not authorize_writes or not confirm_manual_route: raise NktError("CLD writes/route authorization required")
        state = self.capture("cld_preflight")
        self.healthy(state)
        if state["getLdOutputState"]: raise NktError("CLD emission is already on; no takeover")
        self.owned = True
        self.initial_tec = state["getTecOutputState"]
        self.failed = True
        self.backend.set("setLdCurrSetpoint", 0.)
        self.backend.set("setLdCurrLimit", self.config.max_current_a)
        self.backend.set("setLdOperatingMode", 0)
        self.backend.set("setTempSetpoint", self.config.temperature_c)
        if not self.initial_tec: self.backend.set("switchTecOutput", True)
        start = self.clock()
        stable_since = None
        while self.clock() - start <= self.config.timeout_s:
            state = self.capture("cld_thermal_settle")
            self.healthy(state)
            if self.stable(state):
                if stable_since is None: stable_since = self.clock()
                if self.clock()-stable_since >= self.config.stable_dwell_s:
                    if state["getLdOutputState"] or state["getLdCurrSetpoint"] != 0:
                        raise NktError("CLD did not remain zero/off during thermal preparation")
                    self.failed = False
                    return
            else: stable_since = None
            self.sleep(self.config.poll_interval_s)
        raise TimeoutError("CLD TEC did not stabilize")

    def prepare_point(self, point):
        if not self.owned or self.closed or self.failed or point not in self.config.points:
            raise NktError("CLD point/session invalid")
        self.failed = True
        self.backend.set("switchLdOutput", False)
        if self.capture("cld_before_configure")["getLdOutputState"]: raise NktError("CLD off not confirmed")
        self.backend.set("setLdCurrSetpoint", point.current_a)
        self.point = point
        self.failed = False
        return self.verify_point(emitting=False)

    def verify_point(self, *, emitting, phase="cld_formal"):
        if not self.owned or self.closed or self.failed or self.point is None: raise NktError("CLD point not prepared")
        self.failed = True
        state = self.capture(phase)
        self.healthy(state)
        if state["getLdOutputState"] != emitting or not self.stable(state): raise NktError("CLD optical/thermal state changed")
        if state["getLdOperatingMode"] != 0 or state["getLdCurrLimit"] > self.config.max_current_a:
            raise NktError("CLD current control/limit changed")
        if not math.isclose(state["getLdCurrSetpoint"], self.point.current_a, abs_tol=self.config.current_tolerance_a, rel_tol=1e-7):
            raise NktError("CLD current setting changed")
        if abs(state["getTempSetpoint"]-self.config.temperature_c) > .001: raise NktError("CLD temperature target changed")
        self.failed = False
        return {"source": state, "filter": None, "nominal_wavelength_nm": self.point.wavelength_nm}

    def set_emission(self, enabled):
        if type(enabled) is not bool: raise NktError("emission must be boolean")
        if enabled: self.verify_point(emitting=False, phase="cld_before_emission")
        if not self.owned or self.closed or self.failed: raise NktError("CLD session not ready")
        self.failed = True
        self.backend.set("switchLdOutput", enabled)
        self.failed = False
        return self.verify_point(emitting=enabled)

    def finish(self):
        if self.closed: return self.record["cleanup"]
        result = self.record["cleanup"]
        try:
            if self.owned:
                try:
                    self.backend.set("switchLdOutput", False)
                    if self.capture("cld_cleanup_off")["getLdOutputState"]: raise NktError("CLD off not confirmed")
                except (Exception, KeyboardInterrupt) as exc:
                    self.record["state_current"] = False
                    result["errors"].append("LD off: " + str(exc))
                try:
                    self.backend.set("setLdCurrSetpoint", 0.)
                except (Exception, KeyboardInterrupt) as exc:
                    self.record["state_current"] = False
                    result["errors"].append("LD zero: " + str(exc))
                if self.initial_tec is False:
                    try: self.backend.set("switchTecOutput", False)
                    except (Exception, KeyboardInterrupt) as exc:
                        self.record["state_current"] = False
                        result["errors"].append("TEC off: " + str(exc))
                try:
                    final = self.capture("cld_cleanup_final")
                    if final["getLdOutputState"] or final["getLdCurrSetpoint"] != 0 or final["getTecOutputState"] != self.initial_tec:
                        raise NktError("CLD cleanup readback mismatch")
                    result["off_confirmed"] = True
                except (Exception, KeyboardInterrupt) as exc:
                    self.record["state_current"] = False
                    result["errors"].append(str(exc))
        finally:
            try: self.backend.close()
            except (Exception, KeyboardInterrupt) as exc: result["errors"].append("close: "+str(exc))
            self.closed = True
        return result
