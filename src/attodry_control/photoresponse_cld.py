"""CLD1010LP point controller and TL4000 binding.

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
        if self.backend not in ("simulation", "tl4000"):
            raise NktError("CLD backend must be simulation or tl4000")
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
        if self.backend != "tl4000":
            raise NktError("Expected TL4000 backend")
        for name in ("dll_path", "address", "expected_serial", "diode_model"):
            value = getattr(self, name)
            if not value or "CHANGE_ME" in value:
                raise NktError("Configure " + name + " before CLD connection")
        if writes and not self.diode_wiring_confirmed:
            raise NktError("Exact diode pinout/TEC/limits must be physically confirmed")

    def snapshot(self):
        result = asdict(self)
        result["output_directory"] = str(self.output_directory)
        return result


def load_cld_config(doc, path):
    table = dict(doc.get("cld1010", {}))
    fields = set(CldConfig.__dataclass_fields__) - {"points", "emit", "dwell_s"}
    keys(table, "cld1010", fields | {"current_points_a", "wavelength_nm"})
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
                    "isTrippedLdOutputProtInterlock"}
    DOUBLE_GETTERS = {"measTemp", "measLdCurr", "measLdVolt"}
    SETTING_GETTERS = {"getLdCurrLimit", "getLdCurrSetpoint", "getTempSetpoint", "getTecCurrLimit", "getTempProtWindow"}

    def __init__(self, config, *, authorize_connection=False, authorize_writes=False, dll=None, on_event=None):
        config.require_hardware(writes=authorize_writes)
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


class SimulatedCld:
    is_hardware = False
    def __init__(self, config):
        self.config = config
        self.state = {"identity": ["SIMULATED", "CLD1010LP", "SIMULATED", "0"],
            "getLdOperatingMode": 0, "getTecOperatingMode": 1, "getTempUnit": 0,
            "getTempProtMode": 2, "getLdOutputState": False, "getTecOutputState": False,
            "getLdOutpAutoOnState": False, "isTrippedLdOutputProtInterlock": False,
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
                    result["off_confirmed"] = True
                    self.backend.set("setLdCurrSetpoint", 0.)
                    if self.initial_tec is False: self.backend.set("switchTecOutput", False)
                    final = self.capture("cld_cleanup_final")
                    if final["getLdOutputState"] or final["getLdCurrSetpoint"] != 0 or final["getTecOutputState"] != self.initial_tec:
                        raise NktError("CLD cleanup readback mismatch")
                except (Exception, KeyboardInterrupt) as exc:
                    self.record["state_current"] = False
                    result["errors"].append(str(exc))
        finally:
            try: self.backend.close()
            except (Exception, KeyboardInterrupt) as exc: result["errors"].append("close: "+str(exc))
            self.closed = True
        return result
