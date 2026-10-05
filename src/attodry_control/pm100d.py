"""Thin PM100D VISA/SCPI driver, based on Thorlabs 17654-D02 Rev F §6.4."""
from __future__ import annotations

import csv
import math
import time
from threading import Lock

from .nkt_config import NktError, number
from .nkt_control import utc_now


class Pm100d:
    _owners = set()
    _lock = Lock()

    def __init__(self, config, resource, *, is_hardware=False, authorize_settings=False,
                 authorize_measurement=False, manager=None, owner_key=None, clock=time.monotonic):
        config.validate()
        self.config, self.resource, self.manager = config, resource, manager
        self.owner_key = owner_key
        self.is_hardware = is_hardware
        self.authorize_settings, self.authorize_measurement = authorize_settings, authorize_measurement
        self.clock = clock
        self.transcript = []
        self.last_confirmed_state = {}
        self.last_sample = None
        self.state_current = False
        self.poisoned = self.closed = False
        self.sensor = None
        self.identity = None
        self.target_nm = None
        self.sequence = 0

    @classmethod
    def open(cls, config, *, authorize_connection=False, authorize_settings=False,
             authorize_measurement=False, manager_factory=None, **kwargs):
        config.require_hardware(writes=authorize_settings)
        if not authorize_connection:
            raise NktError("PM100D connection requires explicit authorization")
        owner_key = config.resource.upper()
        with cls._lock:
            if owner_key in cls._owners:
                raise NktError("PM100D VISA resource already owned in this process")
            cls._owners.add(owner_key)
        manager = resource = None
        try:
            if manager_factory is None:
                try:
                    import pyvisa
                except ImportError as exc:
                    raise NktError("PM100D requires the existing PyVISA hardware extra") from exc
                manager_factory = pyvisa.ResourceManager
            manager = manager_factory()
            # VISA VI_EXCLUSIVE_LOCK=1: no shared monitor session is requested.
            resource = manager.open_resource(config.resource, access_mode=1)
            resource.timeout = math.ceil(config.io_timeout_s * 1000)
            resource.read_termination = resource.write_termination = "\n"
            return cls(config, resource, is_hardware=True, authorize_settings=authorize_settings,
                       authorize_measurement=authorize_measurement, manager=manager,
                       owner_key=owner_key, **kwargs)
        except (Exception, KeyboardInterrupt) as exc:
            for opened in (resource, manager):
                if opened is not None:
                    try:
                        opened.close()
                    except (Exception, KeyboardInterrupt) as cleanup_exc:
                        exc.add_note(f"PM100D open cleanup: {cleanup_exc}")
            with cls._lock:
                cls._owners.discard(owner_key)
            raise

    def _io(self, command, kind="query"):
        if kind == "setting" and self.is_hardware and not self.authorize_settings:
            raise NktError("PM100D setting write not authorized")
        if kind == "measurement" and self.is_hardware and not self.authorize_measurement:
            raise NktError("READ? triggers acquisition and requires measurement authorization")
        if self.closed or self.poisoned:
            raise NktError("PM100D session uncertain/closed; no automatic retry")
        entry = {"command": command, "kind": kind, "started_at_utc": utc_now(), "reply": None}
        self.transcript.append(entry)
        self.state_current = False
        try:
            if kind == "setting":
                self.resource.write(command)
                return None
            value = self.resource.query(command)
            entry["reply"] = value
            if not isinstance(value, str) or not value.strip() or "\n" in value.strip() or "\r" in value.strip():
                raise NktError("Incomplete/multiple PM100D replies")
            return value.strip()
        except (Exception, KeyboardInterrupt) as exc:
            self.poisoned = True
            entry["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            entry["finished_at_utc"] = utc_now()

    def _parse_reply(self, command, parser, kind="query"):
        reply = self._io(command, kind)
        try:
            return parser(reply)
        except (Exception, KeyboardInterrupt) as exc:
            # A complete line can still contain an invalid or contradictory
            # response. Preserve its evidence and prohibit subsequent I/O.
            self.poisoned = True
            self.state_current = False
            self.transcript[-1]["error"] = f"{type(exc).__name__}: {exc}"
            raise

    def _float(self, command):
        def parse(reply):
            value = float(reply)
            if not math.isfinite(value):
                raise NktError(f"Nonfinite PM100D response: {command}")
            return value
        return self._parse_reply(command, parse)

    def _int(self, command):
        def parse(reply):
            value = float(reply)
            if not math.isfinite(value) or value != int(value) or not 0 <= value <= 0xFFFFFFFF:
                raise NktError(f"Malformed PM100D integer: {command}")
            return int(value)
        return self._parse_reply(command, parse)

    def identify(self):
        def parse(reply):
            fields = next(csv.reader([reply], skipinitialspace=True, strict=True))
            if (len(fields) != 4 or fields[0].upper() != "THORLABS" or fields[1] != "PM100D"
                    or not fields[2] or not fields[3]):
                raise NktError("Unexpected PM100D identity")
            if self.config.expected_serial and fields[2] != self.config.expected_serial:
                raise NktError("PM100D serial mismatch")
            result = dict(zip(("manufacturer", "model", "serial", "firmware"), fields))
            if self.identity is not None and result != self.identity:
                raise NktError("PM100D identity changed within session")
            return result
        result = self._parse_reply("*IDN?", parse)
        self.identity = result
        return result

    def read_sensor(self):
        def parse(reply):
            fields = next(csv.reader([reply], skipinitialspace=True, strict=True))
            if len(fields) != 6 or not fields[0] or not fields[1]:
                raise NktError("PM100D sensor missing/unknown")
            result = {"model": fields[0], "serial": fields[1], "calibration": fields[2],
                      "type": int(fields[3]), "subtype": int(fields[4]), "flags": int(fields[5])}
            if any(result[key] < 0 for key in ("type", "subtype", "flags")):
                raise NktError("PM100D sensor codes/bitmap must be nonnegative integers")
            if not result["flags"] & 1 or not result["flags"] & 32:
                raise NktError("Sensor must report power and wavelength-correction capability")
            if self.config.expected_sensor_serial and fields[1] != self.config.expected_sensor_serial:
                raise NktError("PM100D sensor serial mismatch")
            if self.sensor is not None and result != self.sensor:
                raise NktError("PM100D sensor changed within session")
            return result
        result = self._parse_reply("SYST:SENS:IDN?", parse)
        self.sensor = result
        return result

    def capture(self):
        self.state_current = False
        mode = self._io("CONF?").strip('"').upper()
        state = {"identity": self.identify(), "sensor": self.read_sensor(), "mode": mode,
                 "unit": self._io("SENS:POW:UNIT?").upper(),
                 "wavelength_nm": self._float("SENS:CORR:WAV?"),
                 "wavelength_min_nm": self._float("SENS:CORR:WAV? MIN"),
                 "wavelength_max_nm": self._float("SENS:CORR:WAV? MAX"),
                 "average_count": self._int("SENS:AVER:COUN?"),
                 "auto_range": self._int("SENS:POW:RANG:AUTO?"),
                 "range_w": self._float("SENS:POW:RANG?"),
                 "relative": self._int("SENS:POW:REF:STAT?"),
                 "correction_loss_db": self._float("SENS:CORR:LOSS:INP:MAGN?"),
                 "questionable_condition": self._int("STAT:QUES:COND?"),
                 "captured_at_utc": utc_now(), "error_queue": "not consumed"}
        self.last_confirmed_state = state
        self.state_current = True
        return state

    def _check(self, state, wavelength_nm, *, settings=True):
        # Reject all questionable bits; Rev F lists this register but does not
        # specify individual masks. No undocumented "saturation bit" is invented.
        if state["questionable_condition"] != 0:
            raise NktError("PM100D questionable condition; reject acquisition")
        if state["mode"] not in ("POW", "POWER", "POW:DC", "POWER:DC") or state["unit"] != "W":
            raise NktError("PM100D must report power mode and W")
        if state["relative"] != 0 or state["correction_loss_db"] != 0:
            raise NktError("PM100D relative/user-loss correction would change detector W")
        number(wavelength_nm, "PM wavelength", state["wavelength_min_nm"], state["wavelength_max_nm"])
        if not math.isclose(state["wavelength_nm"], wavelength_nm, rel_tol=0,
                            abs_tol=self.config.wavelength_tolerance_nm):
            raise NktError("PM100D wavelength correction mismatch")
        number(state["range_w"], "PM range_w", 1e-15, 1e6)
        if state["auto_range"] not in (0, 1):
            raise NktError("Invalid PM autorange flag")
        if settings and (state["average_count"] != self.config.average_count or
                         state["auto_range"] != int(self.config.auto_range)):
            raise NktError("PM100D average/range settings mismatch")
        if not self.config.auto_range and not math.isclose(state["range_w"], self.config.range_w,
                                                          rel_tol=1e-6, abs_tol=0):
            raise NktError("PM100D fixed range mismatch")

    def prepare(self, wavelength_nm):
        if wavelength_nm is None:
            raise NktError("Broadband PM calibration strategy unavailable")
        state = self.capture()
        number(wavelength_nm, "PM wavelength", state["wavelength_min_nm"], state["wavelength_max_nm"])
        if self.is_hardware and self.authorize_settings:
            self.config.require_hardware(writes=True)
        if not self.is_hardware or self.authorize_settings:
            if state["questionable_condition"]:
                raise NktError("PM100D questionable state before settings")
            changes = [(state["mode"] not in ("POW", "POWER", "POW:DC", "POWER:DC"), "CONF:POW"),
                       (state["unit"] != "W", "SENS:POW:UNIT W"),
                       (state["relative"] != 0, "SENS:POW:REF:STAT 0"),
                       (state["correction_loss_db"] != 0, "SENS:CORR:LOSS:INP:MAGN 0"),
                       (state["wavelength_nm"] != wavelength_nm, f"SENS:CORR:WAV {wavelength_nm:.10g}"),
                       (state["average_count"] != self.config.average_count,
                        f"SENS:AVER:COUN {self.config.average_count}"),
                       (state["auto_range"] != int(self.config.auto_range),
                        f"SENS:POW:RANG:AUTO {int(self.config.auto_range)}")]
            if not self.config.auto_range:
                changes.append((state["range_w"] != self.config.range_w,
                                f"SENS:POW:RANG {self.config.range_w:.10g}"))
            for needed, command in changes:
                if needed:
                    self._io(command, "setting")
                    def complete(reply):
                        if reply != "1":
                            raise NktError("PM100D setting completion not acknowledged")
                    self._parse_reply("*OPC?", complete)
            state = self.capture()
        self._check(state, wavelength_nm)
        self.target_nm = wavelength_nm
        return state

    def verify_ready(self, wavelength_nm):
        state = self.capture()
        self._check(state, wavelength_nm)
        return state

    def read_power_w(self, wavelength_nm):
        if self.is_hardware and not self.authorize_measurement:
            raise NktError("New power acquisition requires explicit measurement authorization")
        self.prepare(wavelength_nm)
        return self.read_sample(wavelength_nm)["power_w"]

    def read_sample(self, wavelength_nm):
        if self.is_hardware and not self.authorize_measurement:
            raise NktError("New power acquisition requires explicit measurement authorization")
        if wavelength_nm is None:
            raise NktError("Broadband PM calibration strategy unavailable")
        before = self.verify_ready(wavelength_nm)
        start = self.clock()
        started_utc = utc_now()
        value = self._parse_reply("READ?", lambda reply: number(float(reply), "PM power W", 0,
                                                              self.config.max_power_w), "measurement")
        end = self.clock()
        if end < start:
            raise NktError("Monotonic clock reversed during PM acquisition")
        after = self.verify_ready(wavelength_nm)
        if value >= after["range_w"]:
            raise NktError("PM100D at/above measurement range; saturation possible")
        self.sequence += 1
        sample = {"sequence": self.sequence, "power_w": value, "unit": "W", "valid": True,
                  "started_monotonic_s": start, "finished_monotonic_s": end,
                  "started_at_utc": started_utc, "finished_at_utc": utc_now(),
                  "measurement_plane": self.config.measurement_plane,
                  "before": before, "after": after, "freshness": "READ? new acquisition"}
        self.last_sample = sample
        return sample

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.state_current = False
        errors = []
        for resource in (self.resource, self.manager):
            if resource is not None:
                try:
                    resource.close()
                except (Exception, KeyboardInterrupt) as exc:
                    errors.append(f"{type(exc).__name__}: {exc}")
        with self._lock:
            self._owners.discard(self.owner_key)
        if errors:
            raise NktError("; ".join(errors))


class SimulatedPmResource:
    """SCPI fake with explicitly simulated power provider (no physical calibration)."""
    def __init__(self, power_provider=None):
        self.values = {"CONF?": "POW", "*IDN?": "THORLABS,PM100D,SIMULATION,SIMULATION",
                       "SYST:SENS:IDN?": '"SIMULATED SENSOR","SIMULATION","uncalibrated",1,0,33',
                       "SENS:POW:UNIT?": "W", "SENS:CORR:WAV?": "633",
                       "SENS:CORR:WAV? MIN": "400", "SENS:CORR:WAV? MAX": "1100",
                       "SENS:AVER:COUN?": "1", "SENS:POW:RANG:AUTO?": "1",
                       "SENS:POW:RANG?": "1", "SENS:POW:REF:STAT?": "0",
                       "SENS:CORR:LOSS:INP:MAGN?": "0", "STAT:QUES:COND?": "0", "*OPC?": "1"}
        self.power_provider = power_provider or (lambda: 0.001)
        self.commands = []
        self.closed = False

    def query(self, command):
        self.commands.append(("query", command))
        if command == "READ?":
            return str(self.power_provider())
        return self.values[command]

    def write(self, command):
        self.commands.append(("write", command))
        if command == "CONF:POW":
            self.values["CONF?"] = "POW"
        else:
            name, value = command.rsplit(" ", 1)
            self.values[name + "?"] = value

    def close(self):
        self.closed = True
