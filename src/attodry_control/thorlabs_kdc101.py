"""Owned KDC101 APT/D2XX adapter; import never loads a DLL or opens USB.

APT replies are correlated by message ID, source, destination and channel.
Kinesis cached position/status getters are deliberately not used. Controller
and persisted PRM1/MZ8 settings still require separate real commissioning.
"""
from __future__ import annotations

from copy import deepcopy
import ctypes as ct
from datetime import datetime, timezone
import math
import os
import re
import struct
import time
from pathlib import Path

from .rotator_config import RotatorError


KINESIS_HEADER_URL = (
    "https://github.com/Thorlabs/Motion_Control_Examples/blob/main/"
    "C%2B%2B/Kinesis/KCube/KDC101/Thorlabs.MotionControl.KCube.DCServo.h"
)
STATUS_PROTOCOL_URL = (
    "https://www.thorlabs.com/Software/Motion%20Control/"
    "APT_Communications_Protocol.pdf"
)
# APT issue40 p39, PRM1-Z8 row. Thorlabs' official KDC101 ctypes example
# independently uses the rounded1919.64186 value for this same mechanism.
# These are model-specific physical constants, not operator-adjustable factors.
PRM1Z8_COUNTS_PER_DEGREE = 1919.6418578623391
_SERVO_CYCLE_S = 2048 / 6_000_000
_VELOCITY_SCALE = PRM1Z8_COUNTS_PER_DEGREE * _SERVO_CYCLE_S * 65536
_ACCELERATION_SCALE = PRM1Z8_COUNTS_PER_DEGREE * _SERVO_CYCLE_S ** 2 * 65536
FTDI_HEADER_URL = "https://ftdichip.com/Support/Knowledgebase/ftd2xx_h.htm"


class Kdc101Error(RotatorError):
    """An unverified controller state, communication error or rejected command."""


def decode_status_bits(bits: int) -> dict:
    """Decode KDC101-applicable flags, rejecting unknown and contradictory bits.

    Hardware home switches are normal during homing; they remain visible for
    the controller's move policy instead of being silently called faults.
    """
    if type(bits) is not int or not 0 <= bits <= 0xFFFFFFFF:
        raise Kdc101Error("KDC101 status must be an unsigned 32-bit integer")
    names = {
        0x00000001: "cw_hardware_limit", 0x00000002: "ccw_hardware_limit",
        0x00000004: "cw_software_limit", 0x00000008: "ccw_software_limit",
        0x00000010: "moving_cw", 0x00000020: "moving_ccw",
        0x00000040: "jogging_cw", 0x00000080: "jogging_ccw",
        0x00000100: "connected", 0x00000200: "homing",
        0x00000400: "homed", 0x00004000: "position_error",
        0x00100000: "digital_input_1", 0x00200000: "digital_input_2",
        # Generic APT protocol: P_MOT_SB_POWEROK. KDC101's applicability
        # table/header calls it reserved, but firmware on serial27264719
        # returns it. Preserve this defined informational bit; do not use
        # its presence/absence to certify power, motion readiness or health.
        # All other unknown bits and the independent fault checks stay strict.
        0x10000000: "power_ok_indication",
        # Generic APT ACTIVE is observed on this KDC101 during Home. Count it
        # as motion even if direction/homing bits momentarily disappear.
        0x20000000: "active",
        0x40000000: "controller_error", 0x80000000: "enabled",
    }
    known = sum(names)
    unknown = bits & ~known
    if unknown:
        raise Kdc101Error(f"Unknown KDC101 status bits 0x{unknown:08x}; raw=0x{bits:08x}")
    flags = {name: bool(bits & mask) for mask, name in names.items()}
    if (flags["moving_cw"] or flags["jogging_cw"]) and (
        flags["moving_ccw"] or flags["jogging_ccw"]):
        raise Kdc101Error(f"Contradictory KDC101 movement flags; raw=0x{bits:08x}")
    faults = [name for name in ("position_error", "controller_error") if flags[name]]
    if not flags["connected"]:
        faults.append("motor_not_connected")
    return {
        **flags,
        "moving": bool(bits & 0x200002F0),
        "fault": bool(faults), "faults": faults, "raw_status_bits": bits,
    }


def _finite(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise Kdc101Error(f"KDC101 {field} must be finite")
    return float(value)


def configure_d2xx_signatures(library):
    """FTDI's Windows API is stdcall; DWORD is always 32 bits."""
    handle, dword = ct.c_void_p, ct.c_uint32
    output_count = ct.POINTER(dword)
    signatures = {
        "FT_OpenEx": ([ct.c_void_p, dword, ct.POINTER(handle)], dword),
        "FT_Close": ([handle], dword),
        "FT_SetBaudRate": ([handle, dword], dword),
        "FT_SetDataCharacteristics": ([handle, ct.c_ubyte, ct.c_ubyte, ct.c_ubyte], dword),
        "FT_SetFlowControl": ([handle, ct.c_uint16, ct.c_ubyte, ct.c_ubyte], dword),
        "FT_SetRts": ([handle], dword),
        "FT_SetTimeouts": ([handle, dword, dword], dword),
        "FT_GetQueueStatus": ([handle, output_count], dword),
        "FT_Read": ([handle, ct.c_void_p, dword, output_count], dword),
        "FT_Write": ([handle, ct.c_void_p, dword, output_count], dword),
    }
    for name, (argtypes, restype) in signatures.items():
        try:
            function = getattr(library, name)
        except AttributeError as error:
            raise Kdc101Error(f"Required D2XX symbol missing: {name}") from error
        if isinstance(function, ct._CFuncPtr):
            function.argtypes, function.restype = argtypes, restype


class D2xxTransport:
    """One exclusive USB handle, no reset, purge, retry or background reader."""
    def __init__(self, dll_directory="", *, library=None, timeout_s=2.0, clock=time.monotonic):
        self.dll_directory = Path(dll_directory or "C:/Program Files/Thorlabs/Kinesis")
        self.library, self.clock = library, clock
        self.timeout_s = _finite(timeout_s, "USB timeout")
        if not 0 < self.timeout_s <= 30:
            raise Kdc101Error("USB timeout must be within (0, 30] seconds")
        self.handle = ct.c_void_p()
        self._dll_search = None
        self.opened = False

    def _call(self, name, *args):
        try:
            code = getattr(self.library, name)(*args)
        except Exception as error:
            raise Kdc101Error(f"{name} communication failure: {error}") from error
        if isinstance(code, ct._SimpleCData):
            code = code.value
        if type(code) is not int or code != 0:
            raise Kdc101Error(f"{name} failed with D2XX status {code!r}")

    def open(self, serial_number):
        if self.opened:
            raise Kdc101Error("D2XX handle is already open")
        if not isinstance(serial_number, str) or not re.fullmatch(r"27\d{6}", serial_number):
            raise Kdc101Error("Explicit KDC101 serial number starting with 27 is required")
        if self.library is None:
            if os.name != "nt":
                raise Kdc101Error("KDC101 D2XX hardware backend requires Windows")
            path = self.dll_directory / "ftd2xx.dll"
            if not path.is_file():
                raise Kdc101Error(f"D2XX library not found: {path}")
            self._dll_search = os.add_dll_directory(str(self.dll_directory.resolve()))
            try:
                self.library = ct.WinDLL(str(path.resolve()))
            except Exception:
                self._dll_search.close()
                self._dll_search = None
                raise
        try:
            configure_d2xx_signatures(self.library)
            serial = ct.create_string_buffer(serial_number.encode("ascii"))
            self._call("FT_OpenEx", serial, 1, ct.byref(self.handle))  # FT_OPEN_BY_SERIAL_NUMBER
            if not self.handle.value:
                raise Kdc101Error("FT_OpenEx returned a null handle")
            self.opened = True
            # APT's documented USB virtual UART:115200,8N1,RTS/CTS. These
            # configure the owned USB transport, not motor position or motion.
            self._call("FT_SetBaudRate", self.handle, 115200)
            self._call("FT_SetDataCharacteristics", self.handle, 8, 0, 0)
            self._call("FT_SetFlowControl", self.handle, 0x0100, 0, 0)
            self._call("FT_SetRts", self.handle)
            self._timeouts(self.timeout_s)
            if self.pending_bytes():
                raise Kdc101Error("Unread USB data at open; response ownership is ambiguous")
        except BaseException as primary:
            try:
                self.close()
            except BaseException as cleanup:
                primary.add_note(f"D2XX open cleanup also failed: {cleanup}")
            raise

    def _timeouts(self, remaining):
        milliseconds = max(1, int(min(self.timeout_s, remaining) * 1000))
        self._call("FT_SetTimeouts", self.handle, milliseconds, milliseconds)

    def pending_bytes(self):
        if not self.opened:
            raise Kdc101Error("D2XX handle is not open")
        count = ct.c_uint32()
        self._call("FT_GetQueueStatus", self.handle, ct.byref(count))
        return count.value

    def write(self, data, deadline):
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise Kdc101Error("APT write deadline expired")
        self._timeouts(remaining)
        count, buffer = ct.c_uint32(), ct.create_string_buffer(data, len(data))
        self._call("FT_Write", self.handle, buffer, len(data), ct.byref(count))
        if count.value != len(data):
            raise Kdc101Error(f"Partial APT write: {count.value}/{len(data)} bytes; no retry")

    def read(self, size, deadline):
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise Kdc101Error("APT response deadline expired")
        self._timeouts(remaining)
        count, buffer = ct.c_uint32(), ct.create_string_buffer(size)
        self._call("FT_Read", self.handle, buffer, size, ct.byref(count))
        if count.value != size:
            raise Kdc101Error(f"Partial/timed-out APT response: {count.value}/{size} bytes; no retry")
        if self.clock() > deadline:
            raise Kdc101Error("APT response arrived after deadline")
        return buffer.raw[:size]

    def close(self):
        try:
            if self.opened:
                self.opened = False
                self._call("FT_Close", self.handle)
        finally:
            self.handle = ct.c_void_p()
            if self._dll_search is not None:
                self._dll_search.close()
                self._dll_search = None


def _short(message, first=0, second=0):
    return struct.pack("<HBBBB", message, first, second, 0x50, 0x01)


def _long(message, data):
    return struct.pack("<HHBB", message, len(data), 0xD0, 0x01) + data


class Kdc101Device:
    """Single PRM1/MZ8 axis; asynchronous motion, synchronous fresh readbacks.

    All real construction is passive. Only ``open`` loads D2XX/opens USB;
    ``prepare`` may enable the motor. No velocity, home parameters, stage type,
    encoder zero or EEPROM settings are written by this adapter.
    """
    def __init__(self, config, *, library=None, transport=None, clock=time.monotonic,
                 on_event=None):
        self.config, self.clock, self.on_event = config, clock, on_event
        if hasattr(config, "validate"):
            config.validate()
        if config.stage_model != "PRM1/MZ8":
            raise Kdc101Error("Only the confirmed PRM1/MZ8 stage calibration is supported")
        if not re.fullmatch(r"27\d{6}", config.serial_number):
            raise Kdc101Error("Configure the explicit eight-digit KDC101 serial before connection")
        self.transport = transport or D2xxTransport(getattr(config, "dll_directory", ""),
                                                   library=library, clock=clock)
        self.opened = self.prepared = self.poisoned = False
        self.identity = None
        self.last_state = None
        self.transcripts = []
        self._pending_motion = None
        self._sequence = 0

    def _record(self, **entry):
        value = {"utc": datetime.now(timezone.utc).isoformat(),
                 "role": self.config.role, **entry}
        self.transcripts.append(deepcopy(value))
        if self.on_event is not None:
            # The sink owns the complete durable audit. Keep only a diagnostic
            # tail here, instead of duplicating a potentially long scan in RAM.
            del self.transcripts[:-128]
            self.on_event(deepcopy(value))

    def _require_open(self, *, healthy=True):
        if not self.opened:
            raise Kdc101Error("KDC101 is not open")
        if healthy and self.poisoned:
            raise Kdc101Error("KDC101 response ownership is uncertain; reconnect/retry is forbidden")

    def open(self):
        if self.opened:
            raise Kdc101Error("KDC101 is already open")
        try:
            self.transport.open(self.config.serial_number)
            self.opened = True
            reply = self._query(0x0005, 0x0006, length=84, channel=False)
            data = reply["payload"]
            serial = str(struct.unpack_from("<I", data)[0])
            model = data[4:12].split(b"\0", 1)[0].decode("ascii", errors="strict").strip()
            channels = struct.unpack_from("<H", data, 82)[0]
            if serial != self.config.serial_number or model != "KDC101" or channels != 1:
                raise Kdc101Error(f"KDC101 identity mismatch: serial={serial}, model={model!r}, channels={channels}")
            self.identity = {"serial_number": serial, "controller_model": model,
                             "firmware_version": struct.unpack_from("<I", data, 14)[0],
                             "num_channels": channels}
        except BaseException as primary:
            try:
                # The transport also owns the DLL-search handle, even when
                # failure happened before a usable USB handle was returned.
                self.transport.close()
            except BaseException as cleanup:
                primary.add_note(f"KDC101 open cleanup also failed: {cleanup}")
            self.opened = self.prepared = False
            raise
        return dict(self.identity)

    def _packet(self, deadline):
        header = self.transport.read(6, deadline)
        message, first, second, destination, source = struct.unpack("<HBBBB", header)
        if (destination & 0x7F) != 0x01 or source != 0x50:
            raise Kdc101Error(f"Unexpected APT response address: {header.hex()}")
        payload = b""
        if destination & 0x80:
            size = first | (second << 8)
            if not 1 <= size <= 84:
                raise Kdc101Error(f"Unexpected APT payload length {size}")
            payload = self.transport.read(size, deadline)
        self._record(direction="rx", message_id=message, raw_hex=(header + payload).hex())
        if message in (0x0080, 0x0081):
            raise Kdc101Error(f"APT hardware error response 0x{message:04x}: {(header + payload).hex()}")
        return {"message": message, "first": first, "second": second,
                "payload": payload, "header": header}

    def _motion_message(self, packet):
        message, payload = packet["message"], packet["payload"]
        expected = {"home": (0x0444,), "move": (0x0464,),
                    "stop": (0x0444, 0x0464, 0x0466)}.get(self._pending_motion, ())
        if message not in expected:
            raise Kdc101Error(f"Unexpected/late APT message 0x{message:04x}")
        if message == 0x0444:
            if payload or packet["first"] != 1 or packet["second"] != 0:
                raise Kdc101Error("Malformed or wrong-channel APT homed message")
        else:
            if len(payload) != 14 or struct.unpack_from("<H", payload)[0] != 1:
                raise Kdc101Error("Malformed or wrong-channel APT movement completion")
            status = decode_status_bits(struct.unpack_from("<I", payload, 10)[0])
            if status["fault"] or status["moving"]:
                raise Kdc101Error("APT movement completion contains fault/moving state")
        # A pending stop may legitimately receive the preceding move's completion.
        if self._pending_motion != "stop" or message == 0x0466:
            self._pending_motion = None

    def _drain_motion_messages(self, deadline):
        while self.transport.pending_bytes():
            self._motion_message(self._packet(deadline))

    def _query(self, request, expected, *, length=None, channel=True):
        self._require_open()
        deadline = self.clock() + self.transport.timeout_s
        try:
            self._drain_motion_messages(deadline)
            self._sequence += 1
            data = _short(request, 1 if channel else 0)
            self._record(direction="tx", message_id=request, sequence=self._sequence, raw_hex=data.hex())
            self.transport.write(data, deadline)
            while True:
                packet = self._packet(deadline)
                if packet["message"] == expected:
                    if length is not None and len(packet["payload"]) != length:
                        raise Kdc101Error(f"Wrong APT reply length for 0x{expected:04x}")
                    if channel:
                        actual = (struct.unpack_from("<H", packet["payload"])[0]
                                  if packet["payload"] else packet["first"])
                        if actual != 1:
                            raise Kdc101Error("APT response channel does not match owned channel1")
                    return packet
                self._motion_message(packet)
        except BaseException as error:
            self.poisoned = True
            self._record(error=str(error), request=request)
            raise

    def _command(self, data, *, motion=None, emergency=False):
        self._require_open(healthy=not emergency)
        deadline = self.clock() + self.transport.timeout_s
        try:
            if not emergency:
                self._drain_motion_messages(deadline)
            self._record(direction="tx", message_id=int.from_bytes(data[:2], "little"), raw_hex=data.hex())
            self.transport.write(data, deadline)
            if motion is not None:
                self._pending_motion = motion
        except BaseException as error:
            self.poisoned = True
            self._record(error=str(error), command_hex=data.hex())
            raise

    def read_state(self):
        """Bracket one newly requested position with newly requested status bits."""
        self._require_open()
        started = datetime.now(timezone.utc).isoformat()
        sequence = self._sequence
        before = self._query(0x0429, 0x042A, length=6)["payload"]
        position = self._query(0x0411, 0x0412, length=6)["payload"]
        after = self._query(0x0429, 0x042A, length=6)["payload"]
        try:
            first = decode_status_bits(struct.unpack_from("<I", before, 2)[0])
            last = decode_status_bits(struct.unpack_from("<I", after, 2)[0])
            counts = struct.unpack_from("<i", position, 2)[0]
            angle = _finite(counts / PRM1Z8_COUNTS_PER_DEGREE, "angle")
            faults = sorted(set(first["faults"] + last["faults"]))
            state = {**self.identity, "stage_model": self.config.stage_model,
                "stage_identity_source": "operator-declared PRM1/MZ8; not self-identifying",
                "angle_deg": angle, "position_counts": counts,
                "homed": first["homed"] and last["homed"],
                "homing": first["homing"] or last["homing"],
                "moving": first["moving"] or last["moving"] or self._pending_motion is not None,
                "motion_pending": self._pending_motion,
                "enabled": first["enabled"] and last["enabled"],
                "fault": bool(faults), "faults": faults,
                "raw_status_bits": last["raw_status_bits"],
                "raw_status_bits_before": first["raw_status_bits"],
                "status_flags": last, "fresh": True,
                "utc_start": started, "utc": datetime.now(timezone.utc).isoformat(),
                "request_sequences": list(range(sequence + 1, self._sequence + 1)),
                "calibration_counts_per_degree": PRM1Z8_COUNTS_PER_DEGREE}
        except BaseException:
            self.poisoned = True
            raise
        self.last_state = state
        return dict(state)

    @staticmethod
    def _healthy_stationary(state):
        if state["fault"] or state["moving"]:
            raise Kdc101Error("KDC101 must be healthy and stationary before configuration/motion")

    def prepare(self):
        self.prepared = False
        state = self.read_state()
        self._healthy_stationary(state)
        self._motion_parameters()
        if not state["enabled"]:
            self._command(_short(0x0210, 1, 1))
        reply = self._query(0x0211, 0x0212)
        if reply["payload"] or reply["second"] != 1:
            raise Kdc101Error("KDC101 enable readback was not confirmed")
        state = self.read_state()
        self._healthy_stationary(state)
        if not state["enabled"]:
            raise Kdc101Error("KDC101 status did not confirm channel enabled")
        self.prepared = True
        return state

    def _motion_parameters(self):
        # Retain commissioned device settings; only read and reject unsafe or
        # malformed persisted values. No implicit reset to factory velocities.
        velocity = self._query(0x0414, 0x0415, length=14)["payload"]
        home = self._query(0x0441, 0x0442, length=14)["payload"]
        minimum, acceleration, maximum = struct.unpack_from("<iii", velocity, 2)
        direction, switch, home_velocity, offset = struct.unpack_from("<HHIi", home, 2)
        maximum_deg_s = maximum / _VELOCITY_SCALE
        home_deg_s = home_velocity / _VELOCITY_SCALE
        # Native velocity parameters are nearest-integer encodings of deg/s,
        # as required by APT issue40 p39. Compare the encoded25deg/s limit.
        maximum_native = round(25 * _VELOCITY_SCALE)
        if minimum != 0 or acceleration <= 0 or not 0 < maximum <= maximum_native:
            raise Kdc101Error("Persisted PRM1/MZ8 velocity/acceleration is unqualified")
        if direction not in (1, 2) or switch not in (1, 4) or not 0 < home_velocity <= maximum_native:
            raise Kdc101Error("Persisted PRM1/MZ8 home settings are unqualified")
        if not 0 <= offset / PRM1Z8_COUNTS_PER_DEGREE <= 360:
            raise Kdc101Error("Persisted PRM1/MZ8 home offset exceeds one revolution")
        result = {"max_velocity_deg_s": maximum_deg_s,
                  "acceleration_deg_s2": acceleration / _ACCELERATION_SCALE,
                  "home_velocity_deg_s": home_deg_s,
                  "home_direction": direction, "home_limit_switch": switch,
                  "home_offset_deg": offset / PRM1Z8_COUNTS_PER_DEGREE}
        self._record(persisted_motion_parameters=result)
        return result

    def home(self):
        if not self.prepared:
            raise Kdc101Error("Prepare KDC101 before home")
        state = self.read_state()
        self._healthy_stationary(state)
        if not state["enabled"]:
            raise Kdc101Error("KDC101 channel is disabled")
        self._motion_parameters()
        self._command(_short(0x0443, 1), motion="home")

    def move_to(self, angle_deg):
        if not self.prepared:
            raise Kdc101Error("Prepare KDC101 before move")
        target = _finite(angle_deg, "target angle")
        low, high = self.config.min_angle_deg, self.config.max_angle_deg
        if not low <= target <= high or not 0 <= target <= 360:
            raise Kdc101Error("KDC101 target angle exceeds the declared mechanical range")
        state = self.read_state()
        self._healthy_stationary(state)
        if not state["enabled"] or not state["homed"]:
            raise Kdc101Error("KDC101 must be enabled and homed before absolute motion")
        self._motion_parameters()
        # Choose the nearest encoder position inside the declared interval.
        # Rounding an upper-bound target must not command a count beyond it.
        minimum_count = math.ceil(low * PRM1Z8_COUNTS_PER_DEGREE)
        maximum_count = math.floor(high * PRM1Z8_COUNTS_PER_DEGREE)
        if minimum_count > maximum_count:
            raise Kdc101Error("Declared mechanical interval contains no encoder position")
        count = min(maximum_count, max(minimum_count, round(target * PRM1Z8_COUNTS_PER_DEGREE)))
        effective_angle = count / PRM1Z8_COUNTS_PER_DEGREE
        if abs(effective_angle - target) > self.config.position_tolerance_deg:
            raise Kdc101Error("Encoder quantization exceeds configured position tolerance")
        if not -(2 ** 31) <= count < 2 ** 31:
            raise Kdc101Error("KDC101 target overflows the signed encoder count")
        self._record(requested_angle_deg=target, commanded_angle_deg=effective_angle,
                     commanded_position_counts=count)
        self._command(_long(0x0453, struct.pack("<Hi", 1, count)), motion="move")

    def stop(self):
        # Stop remains best effort after an uncertain read; never use this as
        # proof that physical motion ended or that the laser is blocked.
        self._command(_short(0x0465, 1, 2), motion="stop", emergency=True)

    def close(self):
        if not self.opened:
            return
        self.opened = self.prepared = False
        self.transport.close()
