"""PEM200 Rev C protocol. Commands are acknowledged, never proof of optical output."""
from __future__ import annotations

import math
import re
import time
from threading import Lock

from .nkt_config import NktError, number
from .nkt_control import utc_now


class SerialTransport:
    """One owned serial session; a failed exchange poisons it until explicit close."""
    _owners = set()
    _lock = Lock()

    def __init__(self, config, *, authorize_connection=False, authorize_writes=False, factory=None):
        config.require_hardware(writes=authorize_writes)
        if not authorize_connection:
            raise NktError("PEM connection requires explicit authorization")
        self.owner_key = config.port.upper()
        self.closed = False
        with self._lock:
            if self.owner_key in self._owners:
                raise NktError("PEM serial port already owned in this process")
            self._owners.add(self.owner_key)
        try:
            if factory is None:
                try:
                    import serial
                except ImportError as exc:
                    raise NktError("PEM serial backend requires hardware extra (pyserial)") from exc
                factory = serial.Serial
            self.resource = factory(port=config.port, baudrate=250000, timeout=config.io_timeout_s,
                                    write_timeout=config.io_timeout_s)
        except (Exception, KeyboardInterrupt):
            with self._lock:
                self._owners.discard(self.owner_key)
            raise

    def exchange(self, command):
        if self.resource.in_waiting:
            raise NktError("Unsolicited/stale PEM input; session rejected without flushing evidence")
        data = (command + "\n").encode("ascii")
        if self.resource.write(data) != len(data):
            raise NktError("Incomplete PEM write")
        self.resource.flush()
        reply = self.resource.readline()
        if not reply.endswith(b"\n"):
            raise NktError("Incomplete/timeout PEM response")
        return reply.decode("ascii")

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.resource.close()
        finally:
            with self._lock:
                self._owners.discard(self.owner_key)


class SimulatedPemTransport:
    def __init__(self):
        self.amplitude_nm = 158.25
        self.frequency_hz = 50000.0
        self.stable = 1
        self.active = 0
        self.commands = []
        self.closed = False

    def exchange(self, command):
        self.commands.append(command)
        if command == "*IDN?":
            return "[IDN](Hinds PEM 200 controller SIMULATION)\n"
        if command == ":MOD:AMPR?":
            return "[AMPR](10,550)\n"
        if command == ":MOD:AMP?":
            return f"[AMP]({self.amplitude_nm})\n"
        if command == ":MOD:FREQ?":
            return f"[FREQUENCY]({self.frequency_hz})\n"
        if command == ":MOD:STABLE?":
            return f"[STABLE]({self.stable})\n"
        if command.startswith(":MOD:AMP "):
            self.amplitude_nm = float(command.split()[1])
            return f"[AMP]({self.amplitude_nm})\n"
        if command.startswith(":SYS:PEMO "):
            self.active = int(command.split()[1])
            return f"[PEMOUT]({self.active})\n"
        return "<SCPINOP>(unknown command)\n"

    def close(self):
        self.closed = True


class Pem:
    def __init__(self, config, transport, *, is_hardware=False, authorize_writes=False,
                 clock=time.monotonic, sleep=time.sleep):
        config.validate()
        self.config, self.transport = config, transport
        self.is_hardware, self.authorize_writes = is_hardware, authorize_writes
        self.clock, self.sleep = clock, sleep
        self.transcript, self.last_confirmed_state = [], {}
        self.state_current = False
        self.poisoned = False
        self.owned = False
        self.closed = False
        self.target_nm = None
        self.output_command = None

    def _exchange(self, command, identifier, writes=False):
        if writes and self.is_hardware and not self.authorize_writes:
            raise NktError("PEM setting write not authorized")
        if self.closed or self.poisoned:
            raise NktError("PEM session closed or response uncertain; no automatic retry")
        entry = {"command": command, "started_at_utc": utc_now(), "reply": None}
        self.transcript.append(entry)
        self.state_current = False
        try:
            reply = self.transport.exchange(command)
            entry["reply"] = reply
            match = re.fullmatch(r"\[([A-Z]+)\]\(([^\r\n]*)\)\r?\n", reply)
            if not match or match[1] != identifier:
                raise NktError(f"PEM error/mismatched response to {command}: {reply!r}")
            return match[2]
        except (Exception, KeyboardInterrupt) as exc:
            self.poisoned = True
            entry["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            entry["finished_at_utc"] = utc_now()

    def identify(self):
        identity = self._exchange("*IDN?", "IDN")
        # Rev C's example and the commissioned V01 firmware use different word
        # orders. Preserve the complete identity and still require exact expected_idn.
        if not identity.startswith(("Hinds PEM 200 controller", "Hinds PEM controller 200")):
            self._reject("Unexpected PEM controller identity")
        if self.config.expected_idn and identity != self.config.expected_idn:
            self._reject("PEM firmware identity mismatch")
        return identity

    def _reject(self, message):
        self.poisoned = True
        self.state_current = False
        if self.transcript:
            self.transcript[-1]["validation_error"] = message
        raise NktError(message)

    def _parsed(self, command, identifier, parser, *, writes=False):
        value = self._exchange(command, identifier, writes=writes)
        try:
            return parser(value)
        except (ValueError, OverflowError) as exc:
            self._reject(f"Invalid PEM {identifier} value: {exc}")

    def read_amplitude_range_nm(self):
        def parse(value):
            fields = value.split(",")
            if len(fields) != 2:
                raise NktError("Malformed PEM amplitude range")
            low = number(float(fields[0]), "PEM range low", 0, 1e6)
            high = number(float(fields[1]), "PEM range high", low, 1e6)
            return low, high
        return self._parsed(":MOD:AMPR?", "AMPR", parse)

    def read_peak_retardance_nm(self):
        return self._parsed(":MOD:AMP?", "AMP", lambda s: number(float(s), "PEM amplitude", 0, 1e6))

    def read_modulation_frequency_hz(self):
        return self._parsed(":MOD:FREQ?", "FREQUENCY", lambda s: number(float(s), "PEM frequency", 1, 1e7))

    def read_stable(self):
        value = self._exchange(":MOD:STABLE?", "STABLE")
        if value not in ("0", "1"):
            self._reject("Malformed PEM stable flag")
        return value == "1"

    def capture(self):
        self.state_current = False
        state = {"identity": self.identify(), "amplitude_range_nm": self.read_amplitude_range_nm(),
                 "amplitude_nm": self.read_peak_retardance_nm(),
                 "frequency_hz": self.read_modulation_frequency_hz(), "stable": self.read_stable(),
                 "captured_at_utc": utc_now(), "output_state": "unavailable",
                 "last_output_command": self.output_command}
        self.last_confirmed_state = state
        self.state_current = True
        return state

    def preflight(self):
        if self.is_hardware:
            self.config.require_hardware(writes=True)
            if not self.authorize_writes:
                raise NktError("PEM write authorization required before takeover")
        state = self.capture()
        self._frequency(state)
        # Ownership is limited to this explicitly authorized controller; output
        # state cannot be queried, and the active state is never inferred here.
        self.owned = True
        return state

    def _frequency(self, state):
        number(state["frequency_hz"], "PEM frequency outside confirmed head range",
               self.config.frequency_min_hz, self.config.frequency_max_hz)

    def set_peak_retardance_nm(self, value):
        if not self.owned:
            raise NktError("PEM preflight/ownership required")
        number(value, "PEM amplitude", self.config.amplitude_min_nm, self.config.amplitude_max_nm)
        low, high = self.read_amplitude_range_nm()
        number(value, "device PEM amplitude", low, high)
        reply = self._parsed(f":MOD:AMP {value:.10g}", "AMP",
                             lambda s: number(float(s), "PEM amplitude ACK", 0, 1e6), writes=True)
        if not math.isclose(reply, value, rel_tol=0, abs_tol=self.config.amplitude_tolerance_nm):
            self._reject("PEM set acknowledgement differs from target")
        self.target_nm = value

    def set_modulation(self, enabled):
        if not self.owned or type(enabled) is not bool:
            raise NktError("PEM ownership and boolean output command required")
        reply = self._exchange(f":SYS:PEMO {int(enabled)}", "PEMOUT", writes=True)
        if reply != str(int(enabled)):
            self._reject("PEM output command acknowledgement mismatch")
        self.output_command = enabled

    def verify_ready(self):
        state = self.capture()
        self._frequency(state)
        if self.target_nm is None or not math.isclose(state["amplitude_nm"], self.target_nm,
                     rel_tol=0, abs_tol=self.config.amplitude_tolerance_nm) or not state["stable"]:
            raise NktError("PEM amplitude/stability no longer ready")
        if self.output_command is not True:
            raise NktError("PEM activation has not been acknowledged")
        return state

    def prepare(self, wavelength_nm, waves, *, deadline=None):
        if deadline is not None and self.clock() >= deadline:
            raise NktError("PEM total deadline expired before preparation")
        target = self.config.target(wavelength_nm, waves)
        if not self.owned:
            raise NktError("PEM preflight/ownership required")
        low, high = self.read_amplitude_range_nm()
        number(target, "device PEM amplitude", low, high)
        current = self.read_peak_retardance_nm()
        number(current, "stored device PEM amplitude", low, high)
        number(current, "stored PEM amplitude", self.config.amplitude_min_nm, self.config.amplitude_max_nm)
        if deadline is not None and self.clock() >= deadline:
            raise NktError("PEM total deadline expired before activation")
        # On commissioned V01, AMP timed out while disabled; an explicit probe
        # with activation before AMP completed. Validate both amplitudes first;
        # joint scans keep the laser off throughout this preparation.
        self.set_modulation(True)
        if deadline is not None and self.clock() >= deadline:
            raise NktError("PEM total deadline expired before amplitude setting")
        self.set_peak_retardance_nm(target)
        until = self.clock() + self.config.settle_timeout_s
        if deadline is not None:
            until = min(until, deadline)
        while True:
            state = self.capture()
            self._frequency(state)
            if self.clock() > until:
                raise NktError("PEM settle timeout")
            if math.isclose(state["amplitude_nm"], target, rel_tol=0,
                            abs_tol=self.config.amplitude_tolerance_nm) and state["stable"]:
                return {"wavelength_nm": wavelength_nm, "requested_amplitude_nm": target,
                        "readback": state}
            if self.clock() >= until:
                raise NktError("PEM settle timeout")
            self.sleep(min(self.config.poll_interval_s, until - self.clock()))

    def disable_modulation(self):
        self.set_modulation(False)
        return {"disable_acknowledged": True, "physical_off_confirmed": False,
                "physical_off_evidence": "unavailable in Rev C protocol"}

    def finish(self):
        result = {"action": self.config.finish_action, "disable_acknowledged": False,
                  "physical_off_confirmed": False, "errors": []}
        try:
            if self.owned and self.config.finish_action == "disable_ack":
                result.update(self.disable_modulation())
        except (Exception, KeyboardInterrupt) as exc:
            result["errors"].append(f"disable: {type(exc).__name__}: {exc}")
        finally:
            try:
                self.close()
            except (Exception, KeyboardInterrupt) as exc:
                result["errors"].append(f"close: {exc}")
        return result

    def close(self):
        if not self.closed:
            self.closed = True
            self.state_current = False
            self.transport.close()
