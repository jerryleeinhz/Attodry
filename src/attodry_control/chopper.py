"""Owned MC2000B command sessions; rotation state never certifies laser OFF.

Protocol: Thorlabs TTN102010-D02 Rev B, printed pp. 16-19. Real serial
commissioning remains separate from these offline-tested command contracts.
"""
from __future__ import annotations

from copy import deepcopy
import math
from threading import Lock
import time

from .chopper_config import BLADE_SPECS, HIGH_PRECISION, ChopperConfig, ChopperError


class ChopperSerialTransport:
    """115200 8N1, CR commands and prompt-delimited, single-owner replies."""
    _owners = set()
    _owners_lock = Lock()

    def __init__(self, config, *, authorized=False, factory=None, clock=time.monotonic,
                 sleep=time.sleep):
        config.require_hardware()
        if not authorized:
            raise ChopperError("Chopper serial connection requires explicit authorization")
        self.config, self.clock, self.sleep = config, clock, sleep
        self.owner_key = config.port.upper()
        self.closed = self.poisoned = False
        self.transcript = []
        self._completed_entry = None
        self._exchange_lock = Lock()
        with self._owners_lock:
            if self.owner_key in self._owners:
                raise ChopperError("Chopper serial port is already owned in this process")
            self._owners.add(self.owner_key)
        try:
            if factory is None:
                import serial
                factory = serial.Serial
            self.resource = factory(port=config.port, baudrate=115200, bytesize=8,
                                    parity="N", stopbits=1, timeout=config.io_timeout_s,
                                    write_timeout=config.io_timeout_s, xonxoff=False,
                                    rtscts=False, dsrdtr=False)
            # A power-up prompt is permitted, but never flush unidentified bytes.
            if self.resource.in_waiting:
                pending = self.resource.read(self.resource.in_waiting)
                self.transcript.append({"startup_bytes": repr(pending)})
                if pending.strip() != b">":
                    raise ChopperError("Unexpected chopper startup input; raw bytes retained")
        except BaseException:
            try:
                if hasattr(self, "resource"):
                    self.resource.close()
            except BaseException as close_error:
                self.transcript.append({"close_error": f"{type(close_error).__name__}: {close_error}"})
            finally:
                with self._owners_lock:
                    self._owners.discard(self.owner_key)
            raise

    def exchange(self, command, *, deadline=None):
        with self._exchange_lock:
            if self.closed or self.poisoned:
                raise ChopperError("Chopper serial session is closed or poisoned")
            entry = {"command": command, "raw_response": ""}
            self.transcript.append(entry)
            raw = bytearray()
            try:
                if self.resource.in_waiting:
                    pending = self.resource.read(self.resource.in_waiting)
                    if self._completed_entry is not None and all(
                            byte in b"\r\n \t" for byte in pending):
                        # A terminal can deliver the space after '>' separately.
                        # It belongs to the completed response, never this query.
                        previous = self._completed_entry
                        previous["raw_response"] += pending.decode("ascii")
                        previous.setdefault("late_suffixes", []).append({
                            "raw_bytes": repr(pending),
                            "observed_monotonic_s": self.clock(),
                            "before_command": command,
                        })
                    else:
                        entry["stale_bytes"] = repr(pending)
                        raise ChopperError("Unsolicited/stale chopper bytes; no new command sent")
                end = min(self.clock() + self.config.io_timeout_s,
                          deadline if deadline is not None else math.inf)
                if self.clock() >= end:
                    raise TimeoutError("Chopper command deadline exhausted")
                payload = (command + "\r").encode("ascii")
                delay = self.config.serial_inter_character_delay_s
                entry["serial_inter_character_delay_s"] = delay
                entry["bytes_written"] = 0
                # Pacing is opt-in. It cannot extend the command/shared deadline,
                # repair a bad reply or authorize retrying a partial command.
                if delay * (len(payload) - 1) >= end - self.clock():
                    raise TimeoutError("Chopper character pacing exceeds command deadline before write")
                for index, part in enumerate((bytes([byte]) for byte in payload) if delay else (payload,)):
                    if index:
                        self.sleep(delay)
                    remaining = end - self.clock()
                    if remaining <= 0:
                        raise TimeoutError("Chopper command deadline exhausted during write")
                    self.resource.write_timeout = remaining
                    written = self.resource.write(part)
                    entry["bytes_written"] += written
                    if written != len(part):
                        raise ChopperError("Incomplete chopper serial write")
                if self.clock() >= end:
                    raise TimeoutError("Chopper command deadline exhausted after write")
                while self.clock() < end and len(raw) < 4096:
                    self.resource.timeout = max(0.001, end - self.clock())
                    chunk = self.resource.read(1)
                    if not chunk:
                        raise TimeoutError("Incomplete/timeout chopper response")
                    raw.extend(chunk)
                    if chunk == b">":
                        # Some terminal implementations append whitespace after
                        # the prompt. Preserve it and reject any other suffix.
                        if self.resource.in_waiting:
                            suffix = self.resource.read(self.resource.in_waiting)
                            raw.extend(suffix)
                            if suffix.strip():
                                raise ChopperError("Unexpected bytes after chopper prompt")
                        entry["raw_response"] = raw.decode("ascii")
                        if self.clock() > end:
                            raise TimeoutError("Chopper response exceeded shared deadline")
                        self._completed_entry = entry
                        return entry["raw_response"]
                raise TimeoutError("Chopper response lacks a bounded prompt")
            except BaseException as exc:
                entry["raw_response"] = raw.decode("ascii", errors="backslashreplace")
                entry["error"] = f"{type(exc).__name__}: {exc}"
                self.poisoned = True
                raise

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.resource.close()
        finally:
            with self._owners_lock:
                self._owners.discard(self.owner_key)


class SimulatedChopperTransport:
    """Command-compatible fake; its phase-lock state is explicitly synthetic."""
    def __init__(self, config):
        self.config = config
        self.values = {"blade": BLADE_SPECS[config.blade][0], "ref": config.reference_code,
                       "nharmonic": 1, "dharmonic": 1, "phase": 0, "enable": 0,
                       "output": config.actual_output_code, "verbose": 0, "freq": 100}
        self.external_frequency_hz = 100.0
        self.commands = []
        self.closed = False

    def exchange(self, command, *, deadline=None):
        del deadline
        if self.closed:
            raise ChopperError("Simulation transport is closed")
        self.commands.append(command)
        if command == "id?":
            value = self.config.expected_idn or f"Thorlabs {self.config.model} SIMULATION"
        elif command == "input?":
            value = self.external_frequency_hz * self.values["nharmonic"] / self.values["dharmonic"]
        elif command == "refoutfreq?":
            blade = next(name for name, spec in BLADE_SPECS.items()
                         if spec[0] == self.values["blade"])
            _, inner_slots, outer_slots = BLADE_SPECS[blade]
            external = self.values["ref"] >= (2 if blade in HIGH_PRECISION else 1)
            control = (self.external_frequency_hz * self.values["nharmonic"] / self.values["dharmonic"]
                       if external else self.values["freq"])
            if outer_slots:
                outer = (control * outer_slots / inner_slots
                         if blade in HIGH_PRECISION and self.values["ref"] in (1, 3) else control)
                inner = outer * inner_slots / outer_slots
                outputs = {0: control, 1: outer, 2: inner, 3: outer + inner, 4: abs(outer - inner)}
            else:
                outputs = {0: control, 1: control}
            value = outputs.get(self.values["output"], 0) if self.values["enable"] else 0
        elif command.endswith("?") and command[:-1] in self.values:
            value = self.values[command[:-1]]
        elif "=" in command and command.split("=", 1)[0] in self.values:
            key, value = command.split("=", 1)
            self.values[key] = int(value)
            return f"{command}\r\n>"
        else:
            return f"{command}\r\nCommand error CMD_NOT_DEFINED\r\n>"
        return f"{command}\r\n{value}\r\n>"

    def close(self):
        self.closed = True


class ChopperSession:
    """Prepare with laser OFF; keep rotation through lit/dark sample windows.

    The caller owns optical shutdown and must perform it before cleanup(stop).
    Readbacks prove enable/configuration/frequency, not physical PLL phase lock.
    """
    def __init__(self, config: ChopperConfig, *, authorized=False, transport=None,
                 clock=time.monotonic, sleep=time.sleep):
        config.validate()
        self.config, self.authorized, self.transport = config, authorized, transport
        self.clock, self.sleep = clock, sleep
        self.transcript = []
        self.last_confirmed_state = {}
        self.state_current = self.poisoned = self.opened = self.closed = self.owned = False
        self.target_frequency_hz = self.reference_frequency_hz = None
        self.identity = None
        self._deadline = None
        self.prepared = False
        self.prepared_phase_deg = None

    def _exchange(self, command):
        if self.closed or self.poisoned or self.transport is None:
            raise ChopperError("Chopper session is closed, unopened or poisoned")
        if "=" in command and self.config.backend != "simulation" and not self.authorized:
            raise ChopperError("Chopper setting writes require explicit authorization")
        if self._deadline is not None and self.clock() >= self._deadline:
            raise TimeoutError("Chopper shared readiness deadline exhausted before exchange")
        entry = {"command": command, "started_monotonic_s": self.clock()}
        self.transcript.append(entry)
        try:
            reply = self.transport.exchange(command, deadline=self._deadline)
            entry["reply"] = reply
            if self._deadline is not None and self.clock() > self._deadline:
                raise TimeoutError("Chopper reply exceeded shared readiness deadline")
            if not isinstance(reply, str) or not reply.rstrip().endswith(">"):
                raise ChopperError("Chopper response lacks terminal prompt")
            lines = [line.strip() for line in reply.rstrip()[:-1].replace("\r", "\n").split("\n")
                     if line.strip()]
            if lines and lines[0] == command:
                lines.pop(0)  # Only the exact optional echo belongs to this request.
            if any("error" in line.lower() for line in lines):
                raise ChopperError("Chopper reported command error")
            if command.endswith("?") and len(lines) != 1:
                raise ChopperError("Ambiguous/missing chopper query response")
            if "=" in command and lines:
                raise ChopperError("Unexpected chopper setting acknowledgement")
            return lines[0] if lines else ""
        except BaseException as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
            self.poisoned, self.state_current = True, False
            raise
        finally:
            entry["finished_monotonic_s"] = self.clock()

    def _query(self, command, low, high, *, integer=False):
        value = self._exchange(command + "?")
        try:
            result = float(value)
            if not math.isfinite(result) or not low <= result <= high:
                raise ValueError("out of range")
            if integer and result != int(result):
                raise ValueError("not integer")
            return int(result) if integer else result
        except ValueError as exc:
            self.poisoned, self.state_current = True, False
            self.transcript[-1]["validation_error"] = f"Invalid {command} value: {value!r}"
            raise ChopperError(f"Invalid chopper {command} readback: {value!r}") from exc

    def open(self, *, include_transcript=True):
        if self.opened:
            if self.closed or self.poisoned:
                raise ChopperError("Cannot reopen a closed/poisoned chopper session")
            return self.snapshot(include_transcript=include_transcript)
        if self.config.backend == "manual":
            self.opened = True
            return self.snapshot(include_transcript=include_transcript)
        if self.config.backend == "serial" and not self.authorized:
            raise ChopperError("Chopper serial connection requires explicit authorization")
        if self.config.backend == "serial":
            self.config.require_hardware()
        if self.transport is None:
            self.transport = (SimulatedChopperTransport(self.config) if self.config.backend == "simulation"
                              else ChopperSerialTransport(self.config, authorized=self.authorized,
                                                          clock=self.clock, sleep=self.sleep))
        self.identity = self._exchange("id?")
        if self.config.model not in self.identity or (
                self.config.expected_idn and self.identity != self.config.expected_idn):
            self.poisoned, self.state_current = True, False
            raise ChopperError("Chopper identity mismatch; no settings written")
        if self._query("verbose", 0, 1, integer=True) != 0:
            self.poisoned = True
            raise ChopperError("Chopper verbose mode must already be 0; asynchronous status is unsupported")
        state = self._capture()
        self._check_blade(state)
        self.opened = self.owned = True
        return self.snapshot(include_transcript=include_transcript)

    def _capture(self):
        state = {key: self._query(key, low, high, integer=True)
                 for key, low, high in (("blade", 0, 14), ("ref", 0, 3),
                     ("nharmonic", 1, 15), ("dharmonic", 1, 15), ("phase", 0, 360),
                     ("output", 0, 4), ("enable", 0, 1), ("freq", 1, 1000000))}
        state["input_hz"] = self._query("input", 0, 1e6)
        state["refout_frequency_hz"] = self._query("refoutfreq", 0, 1e6)
        state["captured_monotonic_s"] = self.clock()
        state["phase_lock_verified_by_query"] = False
        state["native_refout_frequency_hz"] = state["refout_frequency_hz"]
        state["derived_beam_frequency_hz"] = (
            self.config.beam_frequency_from_refout(state["refout_frequency_hz"])
            if state["blade"] == BLADE_SPECS[self.config.blade][0]
            and state["output"] == self.config.actual_output_code else None)
        self.last_confirmed_state, self.state_current = state, True
        return state

    def _check_blade(self, state):
        if state["blade"] != BLADE_SPECS[self.config.blade][0]:
            self.state_current = False
            raise ChopperError("Chopper blade readback differs from confirmed installed blade")
        max_reference = 3 if self.config.blade in {"MC1F2", "MC1F10HP", "MC1F2P10"} else 1
        max_output = 4 if self.config.blade.startswith("MC2F") else (
            2 if BLADE_SPECS[self.config.blade][2] else 1)
        if state["ref"] > max_reference or state["output"] > max_output:
            self.state_current = False
            raise ChopperError("Unknown blade-dependent chopper reference/output state")

    def _write_verified(self, key, value, state, on_signal_change):
        if state[key] == value:
            return
        self.state_current = False
        self._exchange(f"{key}={int(value)}")
        # Notify after the actual accepted change, before verification/waits.
        if on_signal_change is not None:
            on_signal_change()
        actual = self._query(key, 0, 1000000, integer=True)
        if actual != value:
            raise ChopperError(f"Chopper {key} setting readback mismatch")
        state[key] = actual

    def prepare(self, reference_frequency_hz, on_signal_change=None, *, deadline=None):
        target = self.config.target_frequency(reference_frequency_hz)
        if self.config.backend != "simulation" and not self.config.manual_phase_lock_confirmed:
            raise ChopperError("Confirm front-panel chopper phase lock for this run; "
                               "this driver has not commissioned remote PLL readback")
        if (self.prepared and self.reference_frequency_hz == reference_frequency_hz
                and self.target_frequency_hz == target):
            previous_deadline = self._deadline
            self._deadline = deadline
            try:
                return self.verify_ready()
            finally:
                self._deadline = previous_deadline
        self.prepared = False
        self.reference_frequency_hz = float(reference_frequency_hz)
        self.target_frequency_hz = target
        if self.config.backend == "manual":
            self.open(include_transcript=False)
            self.last_confirmed_state = {"manual_declared": True,
                "run_enabled_declared": self.config.run_enabled,
                "target_frequency_hz": target, "frequency_verified": False,
                "manual_phase_lock_confirmed": True, "phase_lock_verified_by_query": False}
            self.state_current = True
            self.prepared = True
            return self.snapshot(include_transcript=False)
        previous_deadline = self._deadline
        self._deadline = min(self.clock() + self.config.ready_timeout_s,
                             deadline if deadline is not None else math.inf)
        try:
            if isinstance(self.transport, SimulatedChopperTransport):
                self.transport.external_frequency_hz = self.reference_frequency_hz
            self.open(include_transcript=False)
            if isinstance(self.transport, SimulatedChopperTransport):
                self.transport.external_frequency_hz = self.reference_frequency_hz
            state = self._capture()
            self._check_blade(state)
            self.prepared_phase_deg = self.config.phase_deg if self.config.phase_deg is not None else state["phase"]
            settings = {"ref": self.config.reference_code, "nharmonic": self.config.harmonic_n,
                        "dharmonic": self.config.harmonic_d,
                        "output": self.config.actual_output_code}
            if self.config.phase_deg is not None:
                settings["phase"] = self.config.phase_deg
            if self.config.reference_source == "internal":
                settings["freq"] = self.reference_frequency_hz
            if any(state[key] != value for key, value in settings.items()) and state["enable"]:
                self._write_verified("enable", 0, state, on_signal_change)
            for key, value in settings.items():
                self._write_verified(key, value, state, on_signal_change)
            self._write_verified("enable", int(self.config.run_enabled), state, on_signal_change)
            good = 0
            while self.clock() < self._deadline:
                state = self._capture()
                self._assert_settings(state)
                good = good + 1 if self._frequency_good(state) else 0
                if good >= self.config.consecutive_good:
                    state["frequency_verified"] = self.config.run_enabled
                    state["manual_phase_lock_confirmed"] = self.config.manual_phase_lock_confirmed
                    self.prepared = True
                    if on_signal_change is not None:
                        on_signal_change()
                    return self.snapshot(include_transcript=False)
                self.sleep(min(self.config.poll_interval_s, max(0, self._deadline - self.clock())))
            raise TimeoutError("Chopper frequency readiness timeout")
        finally:
            self._deadline = previous_deadline

    def _assert_settings(self, state):
        self._check_blade(state)
        expected = {"ref": self.config.reference_code, "nharmonic": self.config.harmonic_n,
                    "dharmonic": self.config.harmonic_d, "output": self.config.actual_output_code,
                    "enable": int(self.config.run_enabled)}
        if self.prepared_phase_deg is not None:
            expected["phase"] = self.prepared_phase_deg
        if self.config.reference_source == "internal":
            expected["freq"] = self.reference_frequency_hz
        for key, value in expected.items():
            if state[key] != value:
                self.state_current = False
                raise ChopperError(f"Chopper unexpected {key} settings change")

    def _frequency_good(self, state):
        if not self.config.run_enabled:
            return True
        tolerance = self.config.frequency_tolerance_hz
        if self.config.reference_source == "external" and abs(
                state["input_hz"] - self.config.expected_input_frequency(self.reference_frequency_hz)) > tolerance:
            return False
        expected_refout = self.config.expected_refout_frequency(self.reference_frequency_hz)
        return abs(state["refout_frequency_hz"] - expected_refout) <= tolerance

    def verify_ready(self):
        if self.target_frequency_hz is None or not self.opened or self.closed or self.poisoned:
            raise ChopperError("Chopper has no current prepared state")
        if self.config.backend == "manual":
            return self.snapshot(include_transcript=False)
        state = self._capture()
        self._assert_settings(state)
        if not self._frequency_good(state):
            self.state_current = False
            raise ChopperError("Chopper frequency readback left the qualified interval")
        state["frequency_verified"] = self.config.run_enabled
        state["manual_phase_lock_confirmed"] = self.config.manual_phase_lock_confirmed
        return self.snapshot(include_transcript=False)

    def cleanup(self):
        self.prepared = False
        result = {"verified": False, "rotation_stopped": False, "optical_off_confirmed": False,
                  "manual_verification_required": False, "errors": []}
        if self.transport is None or self.config.backend == "manual":
            result["manual_verification_required"] = self.config.backend == "manual"
            result["verified"] = self.config.backend != "manual"
            return result
        if not self.owned:
            result["ownership_not_acquired"] = True
            result["manual_verification_required"] = True
            return result
        try:
            if self.config.finish_rotation == "stop":
                state = {"enable": self._query("enable", 0, 1, integer=True)}
                self._write_verified("enable", 0, state, None)
                result["rotation_stopped"] = self._query("enable", 0, 1, integer=True) == 0
                result["verified"] = result["rotation_stopped"]
            else:
                result["preserved_enable_readback"] = self._query("enable", 0, 1, integer=True)
                result["verified"] = True
        except BaseException as exc:
            result["errors"].append(f"{type(exc).__name__}: {exc}")
            result["manual_verification_required"] = True
        return result

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.state_current = self.prepared = False
        if self.transport is not None:
            self.transport.close()

    def snapshot(self, *, include_transcript=True):
        """Copy current state; full raw history remains the public default.

        Frequent preparation/guard returns use counts instead of repeatedly
        copying all previous requests. This does not trim either raw transcript.
        """
        state = {"backend": self.config.backend, "identity": self.identity,
            "state_current": self.state_current, "poisoned": self.poisoned,
            "reference_frequency_hz": self.reference_frequency_hz,
            "target_frequency_hz": self.target_frequency_hz,
            "single_opening_per_rotation": self.config.single_opening_per_rotation,
            "beam_geometry_source": ("operator_single_opening_per_rotation"
                if self.config.single_opening_per_rotation else "manufacturer_blade_geometry"),
            "last_confirmed_state": self.last_confirmed_state}
        if self.reference_frequency_hz is not None:
            control, nominal, refout, beam = self.config._frequencies(self.reference_frequency_hz)
            state.update(expected_input_frequency_hz=control,
                         nominal_track_frequency_hz=nominal,
                         expected_refout_frequency_hz=refout,
                         expected_beam_frequency_hz=beam)
        transport_transcript = getattr(self.transport, "transcript", [])
        if include_transcript:
            state.update(transcript=self.transcript, transport_transcript=transport_transcript)
        else:
            state.update(transcript_count=len(self.transcript),
                         transport_transcript_count=len(transport_transcript))
        return deepcopy(state)
