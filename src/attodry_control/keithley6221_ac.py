"""Single-owner injected-transport Keithley 6221 sine waveform adapter.

SCPI/status: 622x-901-01 Rev B sections 7, 11 and 14. No reset, speculative
WAVE:ARM? query, DC-current measurement claim, live-update shortcut or auto retry.
Every changed point is stopped, verified OFF, programmed, armed and initiated.
External detector settling belongs to the combination acquisition coordinator.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from functools import wraps
import math
import re
import time

from .excitation_6221_config import Excitation6221Config
from .excitation_quantization import validate_quantized_amplitude


class Keithley6221Error(RuntimeError):
    pass


def _operation(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        if self.faulted:
            raise Keithley6221Error("6221 session faulted; cleanup and a fresh session are required")
        try:
            return method(self, *args, **kwargs)
        except BaseException:
            self.faulted = True
            self.ready = False
            raise
    return wrapped


def _number(raw):
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise Keithley6221Error(f"Malformed numeric response: {raw!r}") from exc
    if not math.isfinite(value):
        raise Keithley6221Error(f"Nonfinite response: {raw!r}")
    return value


def _word(raw):
    value = str(raw).strip()
    # FORM:SREG may be decimal, hexadecimal, octal or binary. Do not write it.
    if re.fullmatch(r"[+]?\d+", value):
        result = int(value)
    elif re.fullmatch(r"#[Hh][0-9a-fA-F]+", value):
        result = int(value[2:], 16)
    elif re.fullmatch(r"#[Bb][01]+", value):
        result = int(value[2:], 2)
    elif re.fullmatch(r"#[Qq][0-7]+", value):
        result = int(value[2:], 8)
    else:
        raise Keithley6221Error(f"Malformed status word: {raw!r}")
    if result > 65535:
        raise Keithley6221Error("6221 status word exceeds 16 bits")
    return result


def _flag(raw):
    value = str(raw).strip().upper()
    if value not in {"0", "1", "OFF", "ON"}:
        raise Keithley6221Error(f"Unknown boolean response: {raw!r}")
    return value in {"1", "ON"}


def _interlock_closed(raw):
    # Rev B Table 14-2: despite TRIPped in the query name, 1 means CLOSED.
    # Do not substitute MEAS:COND bit 1 (D04 can report COND=0 while closed).
    if not isinstance(raw, str) or raw.strip() not in {"0", "1"}:
        raise Keithley6221Error(f"Unknown 6221 interlock response: {raw!r}")
    return raw.strip() == "1"


def _infinite_wave_duration(raw):
    """Decode only DUR:TIME, never a scientific measurement/overflow value."""
    if not isinstance(raw, str):
        return False
    value = raw.strip().upper()
    if value in {"INF", "INFINITY", "+INF", "+INFINITY"}:
        return True
    # D04 /700x returns +9.9e37 after a successful DUR:TIME INF command.
    # SCPI numeric infinity is exact; nearby overflow/NaN/negative sentinels fail.
    if not re.fullmatch(r"[+]?(?:\d+(?:\.\d*)?|\.\d+)(?:E[+-]?\d+)?", value):
        return False
    try:
        return Decimal(value) == Decimal("9.9e37")
    except InvalidOperation:
        return False


_SETTING_ABS_TOLERANCES = {
    "current_peak_a": 1e-15,
    "frequency_hz": 1e-9,
    "offset_a": 1e-14,
    "phase_marker_phase_deg": 1e-14,
    "compliance_voltage_v": 1e-14,
}


def _setting_matches(key, value, expected):
    """Use the same readback tolerances for live and archived evidence."""
    # Observed D04 AMPL? and FREQ? serialize seven significant digits. Accept
    # that exact rounded value separately from the existing narrow tolerance;
    # serialization precision does not define a hardware programming step.
    if (key in {"current_peak_a", "frequency_hz"} and type(value) in (int, float)
            and math.isfinite(value) and math.isfinite(expected)
            and value == float(format(expected, ".6e"))):
        return True
    return (type(value) in (int, float) and math.isfinite(value)
            and math.isclose(value, expected, rel_tol=1e-7,
                             abs_tol=_SETTING_ABS_TOLERANCES[key]))


class Keithley6221Ac:
    def __init__(self, resource, config: Excitation6221Config, event=None, *,
                 authorize_writes=False, monotonic=None, sleep=None):
        if not isinstance(config, Excitation6221Config):
            raise TypeError("6221 requires a validated Excitation6221Config")
        config.require_output_contract()
        self.resource, self.config = resource, config
        self.event = event or (lambda kind, payload: None)
        self.monotonic = monotonic or time.monotonic
        self.sleep = sleep or time.sleep
        self.authorize_writes = authorize_writes is True
        self.preflight_passed = self.owned = self.ready = self.faulted = False
        self.current_point = None
        self.last_confirmed_state = None
        self.last_change_monotonic_s = None
        self._cleanup_result = None

    def _emit(self, kind, payload):
        self.event(kind, {"source_kind": "keithley6221_ac",
                         "captured_at_utc": datetime.now(timezone.utc).isoformat(), **payload})

    def _query(self, command):
        self._emit("excitation_query_attempt", {"command": command})
        try:
            raw = self.resource.query(command)
        except BaseException as exc:
            try:
                self._emit("excitation_query_failed", {"command": command, "error": str(exc)})
            except BaseException as audit_error:
                exc.add_note("6221 failed-query audit: " + str(audit_error))
            raise
        self._emit("excitation_query_return", {"command": command, "raw": raw})
        return str(raw).strip()

    def _write(self, command, *, cleanup_errors=None):
        if not self.authorize_writes:
            raise Keithley6221Error("6221 writes require explicit authorization")
        try:
            self._emit("excitation_command_attempt", {"command": command})
        except BaseException as exc:
            if cleanup_errors is None:
                raise
            cleanup_errors.append("command-attempt audit: " + str(exc))
        self.resource.write(command)
        self.last_change_monotonic_s = self.monotonic()
        try:
            self._emit("excitation_command_return", {"command": command})
        except BaseException as exc:
            if cleanup_errors is None:
                raise
            cleanup_errors.append("command-return audit: " + str(exc))

    def _error(self):
        raw = self._query("SYST:ERR?")
        match = re.fullmatch(r"([+-]?\d+),\s*(\S.*)", raw)
        if match is None:
            raise Keithley6221Error("Malformed 6221 error queue response: " + raw)
        code = int(match.group(1))
        if code:
            raise Keithley6221Error("6221 instrument error: " + raw)
        return {"code": code, "raw": raw}

    def _read_connections(self):
        shield = self._query("OUTP:ISH?").strip().upper()
        if shield not in {"OLOW", "GUAR", "GUARD"}:
            raise Keithley6221Error("Unknown 6221 inner shield response: " + shield)
        earth = _flag(self._query("OUTP:LTE?"))
        return {"inner_shield_raw": shield, "inner_shield": "output_low" if shield == "OLOW" else "guard",
                "output_low_earth_connected": earth,
                "output_low_grounding": "earth" if earth else "floating"}

    def _verify_connections(self, state):
        if (state["inner_shield"] != self.config.inner_shield
                or state["output_low_grounding"] != self.config.output_low_grounding):
            raise Keithley6221Error("6221 output connection differs from configuration")

    def _core(self):
        state = {"source_kind": "keithley6221_ac", "current_basis": "source_setting_readback",
                 "captured_at_utc": datetime.now(timezone.utc).isoformat(),
                 "output_enabled": _flag(self._query("OUTP:STAT?")),
                 "dc_current_setpoint_a": _number(self._query("SOUR:CURR?")),
                 "compliance_voltage_v": _number(self._query("SOUR:CURR:COMP?")),
                 "operation_condition": _word(self._query("STAT:OPER:COND?")),
                 "measurement_condition": _word(self._query("STAT:MEAS:COND?")),
                 # Owned acquisition consumes and archives this latch, including
                 # a compliance excursion that cleared before the next read.
                 # Rev B section 11 / Table 14-7: events latch independently;
                 # ENAB gates the summary bit, not this event register. This
                 # model does not document programmable PTR/NTR filters.
                 "measurement_event": (_word(self._query("STAT:MEAS:EVEN?"))
                                       if self.authorize_writes else None),
                 "questionable_condition": _word(self._query("STAT:QUES:COND?")),
                 "status_contract": "6221-direct-interlock-v1",
                 "interlock_raw": self._query("OUTP:INT:TRIP?")}
        state.update(self._read_connections())
        state["wave_started"] = bool(state["operation_condition"] & 16)
        state["compliance_tripped"] = bool(state["measurement_condition"] & 8)
        state["interlock_asserted"] = _interlock_closed(state["interlock_raw"])
        self._emit("excitation_raw_state", deepcopy(state))
        state["error_queue"] = self._error()
        return state

    @staticmethod
    def _healthy(state, *, active=False, stopping=False):
        # Only known idle/wave status is relevant to this waveform owner. Other
        # sweep/delta/calibration/RS232/unknown modes cannot be taken over.
        if state["operation_condition"] & ~(1024 | 128 | 16):
            raise Keithley6221Error("6221 unknown or incompatible operation state")
        if state["measurement_condition"] & ~2 or state["questionable_condition"]:
            raise Keithley6221Error("6221 compliance, unsafe or unknown condition")
        if state["measurement_event"] is not None and state["measurement_event"] & ~2:
            raise Keithley6221Error("6221 latched compliance, unsafe or unknown event; current condition may be clear")
        if not state["interlock_asserted"]:
            raise Keithley6221Error("6221 interlock is not asserted")
        # ARM clears the prior DC source value (Rev B section 7, item 12).
        # The sine offset is a separate, explicitly verified waveform setting.
        dc_current = state.get("dc_current_setpoint_a")
        if type(dc_current) not in (int, float) or not math.isfinite(dc_current) or dc_current != 0:
            raise Keithley6221Error("6221 exact known DC zero is required")
        if active:
            if (state["measurement_event"] is None
                    or not state["output_enabled"] or not state["wave_started"]
                    or state["operation_condition"] & 128):
                raise Keithley6221Error("6221 waveform start/output was not confirmed")
        elif not stopping and (state["output_enabled"] or state["wave_started"]):
            raise Keithley6221Error("6221 output OFF, inactive waveform and exact DC zero are required")

    def _confirm_stopped(self, audit):
        """Read-only bounded ACK after our ABOR; never repeat a stop command.

        Only output/wave transition flags may lag. Compliance (including its
        event latch), interlock, DC zero, unknown bits and I/O errors fail now.
        Preflight still requires an immediately known inactive source.
        """
        deadline = self.monotonic() + 2.0
        for poll in range(21):
            state = self._core()
            audit.append(state)
            self._healthy(state, stopping=True)
            if not state['output_enabled'] and not state['wave_started']:
                return state
            remaining = deadline - self.monotonic()
            if remaining <= 0 or poll == 20:
                raise Keithley6221Error('6221 stop acknowledgement timeout; OFF not certified')
            self.sleep(min(0.1, remaining))

    @_operation
    def preflight(self):
        if self.owned:
            raise Keithley6221Error("6221 preflight cannot re-enter an owned session")
        identity = self._query("*IDN?")
        parts = [part.strip() for part in identity.split(",")]
        if (len(parts) != 4 or "KEITHLEY" not in parts[0].upper()
                or parts[1].upper() not in {"6221", "MODEL 6221"}
                or not all(parts[2:]) or self.config.expected_idn not in identity):
            raise Keithley6221Error("Unexpected 6221 identity: " + identity)
        state = self._core()
        state["identity"] = identity
        self._healthy(state)
        trigger_line = _word(self._query("TRIG:OLIN?"))
        if not 1 <= trigger_line <= 6 or trigger_line == self.config.phase_marker_line:
            raise Keithley6221Error("6221 phase marker conflicts with the trigger output line")
        self.preflight_passed = True
        self.last_confirmed_state = deepcopy(state)
        return state

    def _require_authorized(self):
        if not self.authorize_writes:
            raise Keithley6221Error("6221 writes require explicit authorization")
        if not self.preflight_passed:
            raise Keithley6221Error("6221 configuration requires verified preflight")

    def _setting(self, command, value):
        argument = format(value, ".17g") if type(value) in (float, int) else value
        self._write(command + " " + argument)
        self._error()

    @_operation
    def configure(self):
        self._require_authorized()
        if self.owned:
            raise Keithley6221Error("6221 configuration cannot be replayed")
        self._healthy(self._core())
        self.owned = True  # Cleanup applies even if the first write is uncertain.
        self._write("SOUR:WAVE:ABOR")
        self._healthy(self._core())
        self._setting("OUTP:ISH", "OLOW" if self.config.inner_shield == "output_low" else "GUAR")
        self._setting("OUTP:LTE", "OFF" if self.config.output_low_grounding == "floating" else "ON")
        self._verify_connections(self._read_connections())
        for command, value in (
            ("SOUR:CURR:COMP", self.config.compliance_voltage_v),
            ("SOUR:WAVE:FUNC", "SIN"), ("SOUR:WAVE:RANG", "BEST"),
            ("SOUR:WAVE:OFFS", self.config.offset_a),
            ("SOUR:WAVE:EXTR", "OFF"), ("SOUR:WAVE:EXTR:ILIN", 0),
            ("SOUR:WAVE:PMAR:OLIN", self.config.phase_marker_line),
            ("SOUR:WAVE:PMAR", self.config.phase_marker_phase_deg),
            ("SOUR:WAVE:PMAR:STAT", "ON" if self.config.phase_marker_enabled else "OFF"),
            ("SOUR:WAVE:DUR:TIME", "INF"),
        ):
            self._setting(command, value)
        state = self._read_settings(active=False)
        self.ready = True
        return state

    def _read_settings(self, *, active):
        state = self._core()
        state.update(
            waveform=self._query("SOUR:WAVE:FUNC?").upper(),
            range_mode=self._query("SOUR:WAVE:RANG?").upper(),
            offset_a=_number(self._query("SOUR:WAVE:OFFS?")),
            external_trigger_enabled=_flag(self._query("SOUR:WAVE:EXTR?")),
            external_trigger_line=_word(self._query("SOUR:WAVE:EXTR:ILIN?")),
            phase_marker_enabled=_flag(self._query("SOUR:WAVE:PMAR:STAT?")),
            phase_marker_line=_word(self._query("SOUR:WAVE:PMAR:OLIN?")),
            trigger_output_line=_word(self._query("TRIG:OLIN?")),
            phase_marker_phase_deg=_number(self._query("SOUR:WAVE:PMAR?")),
            duration_time_s=self._query("SOUR:WAVE:DUR:TIME?"),
            current_peak_a=_number(self._query("SOUR:WAVE:AMPL?")),
            frequency_hz=_number(self._query("SOUR:WAVE:FREQ?")))
        self._emit("excitation_raw_settings", deepcopy(state))
        state["error_queue"] = self._error()
        self._healthy(state, active=active)
        self._verify_connections(state)
        if (state["waveform"] not in {"SIN", "SINUSOID"} or state["range_mode"] != "BEST"
                or state["external_trigger_enabled"] or state["external_trigger_line"] != 0
                or not state["phase_marker_enabled"]
                or state["phase_marker_line"] != self.config.phase_marker_line
                or not 1 <= state["trigger_output_line"] <= 6
                or state["trigger_output_line"] == self.config.phase_marker_line):
            raise Keithley6221Error("6221 waveform/reference configuration changed")
        for key, expected in (("offset_a", self.config.offset_a),
                              ("phase_marker_phase_deg", self.config.phase_marker_phase_deg),
                              ("compliance_voltage_v", self.config.compliance_voltage_v)):
            if not _setting_matches(key, state[key], expected):
                raise Keithley6221Error("6221 setting mismatch: " + key)
        if not _infinite_wave_duration(state["duration_time_s"]):
            raise Keithley6221Error("6221 waveform duration is not infinite")
        if active:
            current, frequency = self.current_point
            state["amplitude_quantization"] = self.config.amplitude_setting(current)
            try:
                validate_quantized_amplitude(state, current, self.config.offset_a,
                                             self.config.max_current_peak_a)
            except ValueError as exc:
                raise Keithley6221Error("6221 current/frequency setting differs from this condition: " + str(exc)) from exc
            if not _setting_matches("frequency_hz", state["frequency_hz"], frequency):
                raise Keithley6221Error("6221 current/frequency setting differs from this condition")
            self.config.validate_point(current, state["frequency_hz"])
            state["actual"] = {"excitation_current_rms_a": state["current_peak_a"] / math.sqrt(2),
                               "excitation_frequency_hz": state["frequency_hz"]}
        self.last_confirmed_state = deepcopy(state)
        return state

    @_operation
    def set_point(self, current_rms_a, frequency_hz):
        self._require_authorized()
        if not self.ready:
            raise Keithley6221Error("6221 is not configured")
        current, frequency, _ = self.config.validate_point(current_rms_a, frequency_hz)
        if current not in self.config.current_rms_points_a or frequency not in self.config.frequency_points_hz:
            raise Keithley6221Error("6221 point is outside the archived grid")
        if self.current_point == (current, frequency):
            return self._read_settings(active=True)
        quantization = self.config.amplitude_setting(current)
        # No assumption that amplitude/frequency updates preserve waveform phase
        # or settled state. A later commissioning may optimize this explicit path.
        self._read_settings(active=self.current_point is not None)
        self._write("SOUR:WAVE:ABOR")
        self._confirm_stopped([])
        self._emit("excitation_amplitude_quantized", quantization)
        self._setting("SOUR:WAVE:AMPL", quantization["programmed_current_peak_scpi"])
        self._setting("SOUR:WAVE:FREQ", frequency)
        self._write("SOUR:WAVE:ARM")
        self._error()
        self.current_point = (current, frequency)
        self._write("SOUR:WAVE:INIT")
        return self._read_settings(active=True)

    @_operation
    def verify_stopped(self):
        """Fresh strict OFF proof; no transitional exception and no writes."""
        return self._read_settings(active=False)

    @_operation
    def stop_output(self):
        """Pause an owned waveform with strict OFF proof, keeping setup reusable."""
        self._require_authorized()
        if not self.ready or not self.owned:
            raise Keithley6221Error("6221 is not configured for a reusable output stop")
        # Read and archive faults before ABOR can remove a current compliance
        # condition. This transition never turns a known fault into permission
        # for another condition; terminal cleanup remains independent.
        before = self._read_settings(active=self.current_point is not None)
        proof = {"attempted": True, "verified": False,
                 "output_off_confirmed": False, "errors": [],
                 "before": deepcopy(before), "final": None,
                 "stop_confirmation": []}
        if self.current_point is not None:
            self._write("SOUR:WAVE:ABOR")
            self._write("OUTP:STAT OFF")
            final = self._confirm_stopped(proof["stop_confirmation"])
            self._healthy(final)
            self._verify_connections(final)
            if not _setting_matches("compliance_voltage_v", final["compliance_voltage_v"],
                                    self.config.compliance_voltage_v):
                raise Keithley6221Error("6221 setting mismatch: compliance_voltage_v")
        else:
            final = before
        proof.update(verified=True, output_off_confirmed=True, final=deepcopy(final))
        self.last_confirmed_state = deepcopy(final)
        self.current_point = None
        self._emit("excitation_output_stopped", deepcopy(proof))
        return proof

    @_operation
    def readback(self):
        if not self.ready or self.current_point is None:
            raise Keithley6221Error("6221 has no active condition")
        return self._read_settings(active=True)

    def cleanup(self):
        """One cleanup attempt; unknown transport state never certifies OFF."""
        if self._cleanup_result is not None:
            return deepcopy(self._cleanup_result)
        self.ready = False
        result = {"attempted": self.owned, "verified": not self.owned,
                  "output_off_confirmed": False,
                  "errors": [], "final": None, "last_confirmed": deepcopy(self.last_confirmed_state)}
        self._cleanup_result = result
        if not self.owned:
            return deepcopy(result)
        for command in ("SOUR:WAVE:ABOR", "OUTP:STAT OFF"):
            try:
                self._write(command, cleanup_errors=result["errors"])
            except BaseException as exc:
                result["errors"].append(command + ": " + str(exc))
        try:
            state = self._confirm_stopped(result.setdefault('stop_confirmation', []))
            result["final"] = state
            self._healthy(state)
            result["output_off_confirmed"] = True
            self.last_confirmed_state = deepcopy(state)
        except BaseException as exc:
            result["errors"].append("zero/OFF verification: " + str(exc))
        result["verified"] = not result["errors"]
        self.faulted = True  # A cleaned session cannot restart output.
        return deepcopy(result)


def validate_active_state(state, config, current_rms_a, frequency_hz, *, require_direct_interlock=False,
                          require_quantization=False):
    """Independently validate archived source evidence without instrument I/O."""
    if not isinstance(config, Excitation6221Config) or not isinstance(state, dict):
        raise ValueError("6221 source evidence/config is missing")
    current, frequency, peak = config.validate_point(current_rms_a, frequency_hz)
    if config.inner_shield is not None:
        config.require_output_contract()
        raw = state.get("inner_shield_raw")
        earth = state.get("output_low_earth_connected")
        if (raw not in {"OLOW", "GUAR", "GUARD"} or type(earth) is not bool
                or state.get("inner_shield") != ("output_low" if raw == "OLOW" else "guard")
                or state.get("output_low_grounding") != ("earth" if earth else "floating")
                or state.get("inner_shield") != config.inner_shield
                or state.get("output_low_grounding") != config.output_low_grounding):
            raise ValueError("6221 archived output connection is missing or differs")
    if state.get("source_kind") != "keithley6221_ac" or state.get("current_basis") != "source_setting_readback":
        raise ValueError("6221 source evidence has an unknown contract")
    for name in ("operation_condition", "measurement_condition", "measurement_event", "questionable_condition"):
        if type(state.get(name)) is not int or not 0 <= state[name] <= 65535:
            raise ValueError("6221 raw status is missing: " + name)
    for name in ("output_enabled", "wave_started", "compliance_tripped", "interlock_asserted",
                 "external_trigger_enabled", "phase_marker_enabled"):
        if type(state.get(name)) is not bool:
            raise ValueError("6221 decoded flag is missing: " + name)
    contract = state.get("status_contract")
    if contract is not None or require_direct_interlock:
        if contract != "6221-direct-interlock-v1":
            raise ValueError("6221 direct interlock evidence contract is missing or unknown")
        interlock = _interlock_closed(state.get("interlock_raw"))
    else:
        # Preserve the original meaning of v1/v2 archives, without inventing
        # direct-query evidence or relaxing their original acceptance criteria.
        interlock = bool(state["measurement_condition"] & 2)
    if (state["wave_started"] != bool(state["operation_condition"] & 16)
            or state["compliance_tripped"] != bool(state["measurement_condition"] & 8)
            or state["interlock_asserted"] != interlock):
        raise ValueError("6221 decoded flags contradict the raw status")
    Keithley6221Ac._healthy(state, active=True)
    queue = state.get("error_queue")
    if (not isinstance(queue, dict) or type(queue.get("code")) is not int
            or queue.get("code") != 0 or not isinstance(queue.get("raw"), str)
            or not re.fullmatch(r"[+]?(?:0),\s*\S.*", queue["raw"].strip())):
        raise ValueError("6221 clear raw error queue is not recorded")
    if (state.get("waveform") not in {"SIN", "SINUSOID"} or state.get("range_mode") != "BEST"
            or state["external_trigger_enabled"] or type(state.get("external_trigger_line")) is not int
            or state.get("external_trigger_line") != 0
            or not state["phase_marker_enabled"] or state.get("phase_marker_line") != config.phase_marker_line):
        raise ValueError("6221 archived waveform/reference configuration differs")
    if type(state.get("phase_marker_line")) is not int:
        raise ValueError("6221 marker line evidence is not an integer")
    trigger_line = state.get("trigger_output_line")
    if (type(trigger_line) is not int or not 1 <= trigger_line <= 6
            or trigger_line == config.phase_marker_line):
        raise ValueError("6221 archived trigger output conflicts with the phase marker")
    actual = state.get("actual")
    if not isinstance(actual, dict) or set(actual) != {"excitation_current_rms_a", "excitation_frequency_hz"}:
        raise ValueError("6221 source setting coordinates are missing")
    expected = {"current_peak_a": peak, "frequency_hz": frequency, "offset_a": config.offset_a,
                "compliance_voltage_v": config.compliance_voltage_v,
                "phase_marker_phase_deg": config.phase_marker_phase_deg}
    quantized = require_quantization or "amplitude_quantization" in state
    if quantized:
        validate_quantized_amplitude(state, current, config.offset_a, config.max_current_peak_a)
        del expected["current_peak_a"]
    for key, target in expected.items():
        value = state.get(key)
        if not _setting_matches(key, value, target):
            raise ValueError("6221 archived source setting differs: " + key)
    for key, expected_value in (("excitation_current_rms_a", state["current_peak_a"] / math.sqrt(2)),
                                ("excitation_frequency_hz", state["frequency_hz"])):
        value = actual[key]
        if (type(value) not in (int, float) or not math.isfinite(value)
                or not math.isclose(value, expected_value, rel_tol=1e-12, abs_tol=1e-18)):
            raise ValueError("6221 coordinates contradict source settings: " + key)
    config.validate_point(current if quantized else actual["excitation_current_rms_a"],
                          actual["excitation_frequency_hz"])
    if not _infinite_wave_duration(state.get("duration_time_s")):
        raise ValueError("6221 duration evidence is not infinite")
    return True
