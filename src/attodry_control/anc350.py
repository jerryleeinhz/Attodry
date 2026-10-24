"""ANC350 RES v4 USB adapter. Construction/import performs no device I/O.

The reviewed vendor anc350res.h/ancdecl.h declare Windows stdcall, 32-bit
integers and pointer handles; linear positions are double metres. Preset and
LUT names are configuration readbacks, not independently sensed stage models.
Station/controller OS leases belong to the calling orchestration layer. The
process-wide SDK mutex below also prevents discover while another SDK handle
is alive, as required by the vendor's non-thread-safe discovery API.
"""
from __future__ import annotations

import ctypes as C
from copy import deepcopy
import hashlib
import math
import os
from pathlib import Path
import struct
import threading

from .positioner_config import PositionerError


class Anc350Error(PositionerError):
    """Unknown identity/state, failed communication or rejected operation."""


I, U, H, D = C.c_int32, C.c_uint32, C.c_void_p, C.c_double
_SDK_OWNER = threading.Lock()
_SDK_CONNECTION_UNKNOWN = False
_STATUS = ("sensor_connected", "output_enabled", "moving", "target_reached",
           "eot_forward", "eot_backward", "sensor_error")
_SIGNATURES = {
    "ANC_discover": [I, C.POINTER(U)],
    "ANC_getDeviceInfo": [U, C.POINTER(I), C.POINTER(I), C.POINTER(C.c_char),
                          C.POINTER(C.c_char), C.POINTER(I)],
    "ANC_connect": [U, C.POINTER(H)], "ANC_disconnect": [H],
    "ANC_getFirmwareVersion": [H, C.POINTER(I)],
    "ANC_getDeviceConfig": [H, C.POINTER(U)],
    "ANC_getAxisStatus": [H, U] + [C.POINTER(I)] * 7,
    "ANC_getActuatorName": [H, U, C.POINTER(C.c_char)],
    "ANC_getActuatorType": [H, U, C.POINTER(I)],
    "ANC_getLutName": [H, U, C.POINTER(C.c_char)],
    "ANC_getPosition": [H, U, C.POINTER(D)],
    "ANC_getAmplitude": [H, U, C.POINTER(D)],
    "ANC_getFrequency": [H, U, C.POINTER(D)],
    "ANC_setAmplitude": [H, U, D],
    "ANC_setFrequency": [H, U, D],
    "ANC_setTargetGround": [H, U, I],
    "ANC_setTargetRange": [H, U, D],
    "ANC_setAxisOutput": [H, U, I, I],
    "ANC_setTargetPosition": [H, U, D],
    "ANC_startAutoMove": [H, U, I, I],
}


def configure_anc350_signatures(library):
    for name, arguments in _SIGNATURES.items():
        function = getattr(library, name, None)
        if not callable(function):
            raise Anc350Error("Required ANC350 symbol missing: " + name)
        if isinstance(function, C._CFuncPtr):
            function.argtypes, function.restype = arguments, I


def _finite(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise Anc350Error(label + " must be finite")
    return float(value)


def verified_library_bytes(path, expected_sha256):
    """Pin actual PE bytes and machine type before any vendor code is loaded."""
    data = Path(path).read_bytes()
    if hashlib.sha256(data).hexdigest() != expected_sha256.lower():
        raise Anc350Error("ANC350 DLL SHA256 differs from the configured identity")
    if len(data) < 64 or data[:2] != b"MZ":
        raise Anc350Error("ANC350 library is not a Windows PE file")
    offset = struct.unpack_from("<I", data, 0x3c)[0]
    if offset + 26 > len(data) or data[offset:offset + 4] != b"PE\0\0":
        raise Anc350Error("ANC350 library PE header is incomplete")
    if (struct.unpack_from("<H", data, offset + 4)[0] != 0x8664 or
            struct.unpack_from("<H", data, offset + 24)[0] != 0x20b):
        raise Anc350Error("ANC350 library must be the pinned AMD64/PE32+ build")
    if C.sizeof(H) != 8:
        raise Anc350Error("ANC350 Win64 library requires a 64-bit Python runtime")
    return data


class Anc350Device:
    """One owned controller. One startup drive configuration; no runtime repair.

    ``dll_factory`` is an offline injection seam; production uses WinDLL only
    after file hash/bitness verification. Successful motion retains feedback
    and output. ``close`` only disconnects and never claims to stop motion.
    """
    def __init__(self, config, *, on_event=None, dll_factory=None, lease_factory=None):
        config.validate()
        self.config, self.on_event = config, on_event
        self.dll_factory, self.library = dll_factory, None
        self.lease_factory, self._lease = lease_factory, None
        self.handle, self._search = H(), None
        self._lock_owned = self._open_attempted = self.opened = False
        self._failed_write = False
        self._prepared, self._motion_owned, self._stop_attempted = set(), set(), set()
        self.last_confirmed = None
        self.identity = {}
        self.audit_errors = []
        self.close_errors = []
        self._independent_audit = False
        self._output_off_attempted = False
        self._drive_attempted = self._drive_initialized = False
        self._startup_checkpoint = None

    def _audit(self, record, *, independent=False):
        independent = independent or self._independent_audit
        if self.on_event is None:
            return
        try:
            self.on_event(deepcopy(record))
        except Exception as exc:
            if not independent:
                raise
            self.audit_errors.append(str(exc))

    def _call(self, name, *args, independent=False):
        checkpoint = self._startup_checkpoint if not independent else None
        if checkpoint is not None:
            checkpoint()
        arguments = [value.value if isinstance(value, C._SimpleCData) else
                     value if isinstance(value, (int, float, str)) else {"output_pointer": True}
                     for value in args]
        self._audit({"phase": "before", "name": name, "arguments": arguments}, independent=independent)
        if checkpoint is not None:
            checkpoint()
        try:
            result = getattr(self.library, name)(*args)
        except Exception as exc:
            self._audit({"phase": "error", "name": name, "error": str(exc)}, independent=True)
            raise Anc350Error(name + " communication failed; no retry: " + str(exc)) from exc
        if isinstance(result, C._SimpleCData):
            result = result.value
        self._audit({"phase": "after", "name": name, "code": result}, independent=independent)
        if type(result) is not int or result != 0:
            raise Anc350Error(name + " returned " + repr(result) + "; no retry")
        if checkpoint is not None:
            checkpoint()

    def _require_open(self):
        if not self.opened or not self.handle.value:
            raise Anc350Error("ANC350 connection is not open")

    def _axis(self, role):
        matches = [axis for axis in self.config.axes if axis.role == role]
        if len(matches) != 1:
            raise Anc350Error("Unknown positioner role: " + str(role))
        return matches[0]

    def _text(self, function, index):
        # A filled buffer must contain a terminating NUL and a nonempty ASCII value.
        buffer = C.create_string_buffer(b"\xff" * 255, 256)
        self._call(function, self.handle, index, buffer)
        raw = buffer.raw
        end = raw.find(b"\0")
        if end <= 0 or end == 255:
            raise Anc350Error(function + " returned an empty/incomplete string")
        try:
            return raw[:end].decode("ascii", errors="strict")
        except UnicodeError as exc:
            raise Anc350Error(function + " returned unknown text") from exc

    def _number(self, function, index):
        value = D(float("nan"))
        self._call(function, self.handle, index, C.byref(value))
        return _finite(value.value, function)

    def _status(self, index, *, independent=False):
        values = [I(-1) for _ in _STATUS]
        self._call("ANC_getAxisStatus", self.handle, index,
                   *[C.byref(value) for value in values], independent=independent)
        if any(value.value not in (0, 1) for value in values):
            raise Anc350Error("ANC350 incomplete/unknown 32-bit boolean status")
        return dict(zip(_STATUS, [bool(value.value) for value in values]))

    def open(self):
        global _SDK_CONNECTION_UNKNOWN
        if self._open_attempted:
            raise Anc350Error("ANC350 open is one attempt per device instance")
        self._open_attempted = True
        if _SDK_CONNECTION_UNKNOWN:
            raise Anc350Error("An earlier ANC350 connection/disconnect is unknown in this process; manual review required before a new SDK session")
        if not _SDK_OWNER.acquire(blocking=False):
            raise Anc350Error("Another ANC350 SDK session owns this process")
        self._lock_owned = True
        try:
            if _SDK_CONNECTION_UNKNOWN:
                raise Anc350Error("An earlier ANC350 connection/disconnect is unknown in this process; manual review required before a new SDK session")
            path = Path(self.config.dll_path).resolve(strict=True)
            if self.lease_factory is None:
                from .hardware_lease import controller_hardware_lease
                lease = controller_hardware_lease(self.config.controller_serial_number)
            else:
                lease = self.lease_factory(self.config.controller_serial_number)
            lease.__enter__()
            self._lease = lease
            verified_library_bytes(path, self.config.dll_sha256.lower())
            if self.dll_factory is None:
                if os.name != "nt":
                    raise Anc350Error("ANC350 hardware backend requires Windows")
                self._search = os.add_dll_directory(str(path.parent))
                self.library = C.WinDLL(str(path), winmode=0x1100)
            else:
                self.library = self.dll_factory(str(path))
            configure_anc350_signatures(self.library)
            count = U(0xffffffff)
            self._call("ANC_discover", 1, C.byref(count))  # USB only, no LAN discovery
            if count.value != 1:
                raise Anc350Error("Expected exactly one discoverable USB ANC350")
            kind, hardware, connected = I(-1), I(-1), I(-1)
            serial, address = C.create_string_buffer(256), C.create_string_buffer(256)
            self._call("ANC_getDeviceInfo", 0, C.byref(kind), C.byref(hardware),
                       serial, address, C.byref(connected))
            self.identity = {"serial_number": serial.value.decode("ascii", errors="strict"),
                             "controller_model": "ANC350 RES", "type_raw": kind.value,
                             "hardware_id_raw": hardware.value,
                             "interface": address.value.decode("ascii", errors="strict")}
            if (kind.value != 0 or connected.value != 0 or self.identity["interface"] != "USB" or
                    self.identity["serial_number"] != self.config.controller_serial_number):
                raise Anc350Error("ANC350 identity/type/interface/ownership mismatch")
            self._call("ANC_connect", 0, C.byref(self.handle))
            if not self.handle.value:
                _SDK_CONNECTION_UNKNOWN = True
                raise Anc350Error("ANC350 connect returned a null handle")
            self.opened = True
            self._read_initial_state(allow_enabled=True)
        except BaseException as primary:
            try:
                self.close()
            except BaseException as cleanup:
                primary.add_note("Independent ANC350 open cleanup failed: " + str(cleanup))
            raise

    def _capture_state(self, *, verify_drive, allow_enabled=False):
        self._require_open()
        firmware, features = I(-1), U(0xffffffff)
        self._call("ANC_getFirmwareVersion", self.handle, C.byref(firmware))
        self._call("ANC_getDeviceConfig", self.handle, C.byref(features))
        if firmware.value != self.config.expected_firmware_raw or features.value == 0xffffffff:
            raise Anc350Error("ANC350 firmware identity/features unknown or changed")
        state = dict(self.identity, firmware_raw=firmware.value,
                     feature_flags_raw=features.value, axes={})
        for axis in self.config.axes:
            initial = self._status(axis.index)
            kind = I(-1)
            name = self._text("ANC_getActuatorName", axis.index)
            self._call("ANC_getActuatorType", self.handle, axis.index, C.byref(kind))
            lut = self._text("ANC_getLutName", axis.index)
            if (kind.value != 0 or name != axis.expected_actuator_name or
                    lut != axis.expected_lut_name):
                raise Anc350Error(axis.role + ": linear preset/LUT identity mismatch")
            position = self._number("ANC_getPosition", axis.index) * 1e6
            amplitude = self._number("ANC_getAmplitude", axis.index)
            frequency = self._number("ANC_getFrequency", axis.index)
            if amplitude < 0 or frequency < 0:
                raise Anc350Error(axis.role + f": invalid negative drive readback: {amplitude} V/{frequency} Hz")
            final = self._status(axis.index)
            if not verify_drive and (initial["moving"] or final["moving"]):
                raise Anc350Error(axis.role + ": initial axes must be stationary")
            if not verify_drive and not allow_enabled and initial["output_enabled"]:
                raise Anc350Error(axis.role + ": initial output must be OFF and stationary")
            # Motion/target bits can transition during polling. Sensor/output/fault
            # transitions across a single readback never certify a healthy snapshot.
            for key in ("sensor_connected", "output_enabled", "sensor_error", "eot_forward", "eot_backward"):
                if initial[key] != final[key]:
                    raise Anc350Error(axis.role + ": status changed during readback: " + key)
            if verify_drive and (not math.isclose(amplitude, axis.amplitude_v, rel_tol=1e-9, abs_tol=1e-9) or
                    not math.isclose(frequency, axis.frequency_hz, rel_tol=1e-9, abs_tol=1e-9)):
                raise Anc350Error(axis.role + ": amplitude/frequency changed; no automatic repair; "
                                  f"actual={amplitude} V/{frequency} Hz, "
                                  f"requested={axis.amplitude_v} V/{axis.frequency_hz} Hz")
            state["axes"][axis.role] = dict(final, axis_index=axis.index,
                actuator_name=name, actuator_type_raw=kind.value, lut_name=lut,
                position_um=position, amplitude_v=amplitude, frequency_hz=frequency)
        self.last_confirmed = deepcopy(state)
        return state

    def read_state(self):
        """Normal snapshots always enforce the configured drive settings."""
        return self._capture_state(verify_drive=True)

    def _initial_axes_safe(self, state, *, allow_enabled=False):
        for axis in self.config.axes:
            row = state["axes"][axis.role]
            if row["moving"] or (row["output_enabled"] and not allow_enabled):
                raise Anc350Error(axis.role + ": initial output must be OFF and stationary")
            if (not row["sensor_connected"] or row["sensor_error"] or row["eot_forward"] or row["eot_backward"] or
                    not axis.min_position_um <= row["position_um"] <= axis.max_position_um):
                raise Anc350Error(axis.role + ": initial axis position/sensor/EOT unavailable")

    def _read_initial_state(self, *, allow_enabled=False):
        """Read-only startup scope; ON observation ends when release starts."""
        if self._drive_attempted or (allow_enabled and self._output_off_attempted):
            raise Anc350Error("ANC350 initial read scope has ended")
        state = self._capture_state(verify_drive=False, allow_enabled=allow_enabled)
        self._initial_axes_safe(state, allow_enabled=allow_enabled)
        return state

    def initialize_outputs_off(self, *, guard, checkpoint=None):
        """Release held ON axes once, before freezing a new stationary origin.

        The controller lease and all identities are already checked. A normal
        auto-off command clears retained feedback before output OFF; neither
        command is retried. No target or drive setting is changed here.
        """
        self._require_open()
        if (self._output_off_attempted or self._drive_attempted or self._failed_write or
                self._prepared or not callable(guard)):
            raise Anc350Error("ANC350 startup output OFF requires a guard and cannot be retried")
        self._output_off_attempted = True
        self._startup_checkpoint = checkpoint
        try:
            guard()
            baseline = self._capture_state(verify_drive=False, allow_enabled=True)
            self._initial_axes_safe(baseline, allow_enabled=True)
            self._audit({"name": "startup_output_off", "phase": "before", "actual": baseline})
            for axis in self.config.axes:
                if not baseline["axes"][axis.role]["output_enabled"]:
                    continue
                for name, flag in (("ANC_startAutoMove", 0), ("ANC_setAxisOutput", 1)):
                    guard()
                    for checked in self.config.axes:
                        row = self._status(checked.index)
                        if (not row["sensor_connected"] or row["moving"] or row["sensor_error"] or
                                row["eot_forward"] or row["eot_backward"]):
                            raise Anc350Error(checked.role + ": startup output OFF requires healthy stationary axes")
                    self._call(name, self.handle, axis.index, 0, flag)
                actual = self._status(axis.index)
                self._audit({"name": "startup_output_off", "phase": "readback",
                             "axis": axis.role, "requested_output_enabled": False, "actual": actual})
                if (actual["output_enabled"] or actual["moving"] or not actual["sensor_connected"] or
                        actual["sensor_error"] or actual["eot_forward"] or actual["eot_backward"]):
                    raise Anc350Error(axis.role + ": startup output must be OFF and stationary; readback failed")
            final = self._read_initial_state()
            self._audit({"name": "startup_output_off", "phase": "after", "actual": final})
            if checkpoint is not None:
                checkpoint()
            return final
        except BaseException:
            self._failed_write = True
            raise
        finally:
            self._startup_checkpoint = None

    def initialize_drive(self, *, guard=None, checkpoint=None):
        """Set differing TOML drive values once, before any output preparation.

        No retry, rollback, preset/LUT change or persistent flash save is made.
        Every setter requires fresh healthy OFF/stationary status for all axes.
        """
        self._require_open()
        if self._drive_attempted or self._failed_write or self._prepared:
            raise Anc350Error("ANC350 startup drive initialization cannot be retried")
        self._drive_attempted = True
        self._startup_checkpoint = checkpoint
        try:
            if guard is not None:
                guard()
            baseline = self._capture_state(verify_drive=False)
            self._initial_axes_safe(baseline)
            requested = {axis.role: {"amplitude_v": axis.amplitude_v, "frequency_hz": axis.frequency_hz}
                         for axis in self.config.axes}
            self._audit({"name": "drive_initialization", "phase": "before",
                         "requested": requested, "actual": baseline})
            for axis in self.config.axes:
                for field, setter, getter in (("amplitude_v", "ANC_setAmplitude", "ANC_getAmplitude"),
                                               ("frequency_hz", "ANC_setFrequency", "ANC_getFrequency")):
                    value = getattr(axis, field)
                    if math.isclose(baseline["axes"][axis.role][field], value, rel_tol=1e-9, abs_tol=1e-9):
                        continue
                    if guard is not None:
                        guard()
                    for checked in self.config.axes:
                        status = self._status(checked.index)
                        if (not status["sensor_connected"] or status["output_enabled"] or status["moving"] or
                                status["sensor_error"] or status["eot_forward"] or status["eot_backward"]):
                            raise Anc350Error(checked.role + ": drive initialization requires all axes healthy, output OFF and stationary")
                    self._call(setter, self.handle, axis.index, value)
                    actual = self._number(getter, axis.index)
                    self._audit({"name": setter, "phase": "readback", "axis": axis.role,
                                 "field": field, "requested": value, "actual": actual})
                    if not math.isclose(actual, value, rel_tol=1e-9, abs_tol=1e-9):
                        raise Anc350Error(f"{axis.role}: startup {field} readback mismatch; requested={value}, actual={actual}; no retry")
            final = self.read_state()
            self._initial_axes_safe(final)
            self._audit({"name": "drive_initialization", "phase": "after",
                         "requested": requested, "actual": final})
            if checkpoint is not None:
                checkpoint()
            self._drive_initialized = True
            return final
        except BaseException:
            self._failed_write = True
            raise
        finally:
            self._startup_checkpoint = None

    def read_state_independent(self):
        """Fresh cleanup evidence even if the durable audit sink has failed."""
        self._independent_audit = True
        try:
            return self.read_state()
        finally:
            self._independent_audit = False

    def prepare_axis(self, role):
        self._require_open()
        if not self._drive_initialized:
            raise Anc350Error("ANC350 prepare requires completed startup drive initialization")
        if self._failed_write or role in self._prepared:
            raise Anc350Error("ANC350 prepare cannot be retried")
        axis = self._axis(role)
        state = self.read_state()["axes"][role]
        if (not state["sensor_connected"] or state["output_enabled"] or state["moving"] or state["sensor_error"] or
                state["eot_forward"] or state["eot_backward"]):
            raise Anc350Error(role + ": preparation requires output OFF and healthy stationary axis")
        self._prepared.add(role)
        self._motion_owned.add(role)
        try:
            # The API has no auto-enable or previous-target getter. While output
            # is confirmed OFF, replace any retained target with fresh actual
            # before enabling it. A target write makes this our owned motion.
            self._call("ANC_setTargetPosition", self.handle, axis.index, state["position_um"] * 1e-6)
            # ACK evidence only: the API has no matching getters. Keep feedback
            # active at target and enable EOT automatic output deactivation.
            self._call("ANC_setTargetGround", self.handle, axis.index, 0)
            self._call("ANC_setTargetRange", self.handle, axis.index, axis.position_tolerance_um * 1e-6)
            self._call("ANC_setAxisOutput", self.handle, axis.index, 1, 1)
        except BaseException:
            self._failed_write = True
            raise

    def move_to(self, role, absolute_um):
        self._require_open()
        if self._failed_write or role not in self._prepared:
            raise Anc350Error("ANC350 move requires preparation and known command state")
        axis = self._axis(role)
        target = _finite(absolute_um, "absolute position")
        if not axis.min_position_um <= target <= axis.max_position_um:
            raise Anc350Error(role + ": target outside configured soft limits")
        state = self.read_state()["axes"][role]
        if (not state["sensor_connected"] or not state["output_enabled"] or state["moving"] or
                state["sensor_error"] or state["eot_forward"] or state["eot_backward"]):
            raise Anc350Error(role + ": move requires a healthy enabled stationary axis")
        if (not axis.min_position_um <= state["position_um"] <= axis.max_position_um or
                abs(target - state["position_um"]) > axis.max_move_um):
            raise Anc350Error(role + ": fresh actual-to-target step exceeds configured limit")
        self._motion_owned.add(role)  # Even a failed target write can have acted.
        try:
            self._call("ANC_setTargetPosition", self.handle, axis.index, target * 1e-6)
            self._call("ANC_startAutoMove", self.handle, axis.index, 1, 0)
        except BaseException:
            self._failed_write = True
            raise

    def stop(self, role):
        self._require_open()
        if role not in self._motion_owned or role in self._stop_attempted:
            return False
        self._stop_attempted.add(role)
        self._motion_owned.discard(role)
        axis = self._axis(role)
        # Audit failures must never block this independent, owned normal stop.
        self._call("ANC_startAutoMove", self.handle, axis.index, 0, 0, independent=True)
        return True

    def close(self):
        global _SDK_CONNECTION_UNKNOWN
        errors = []
        try:
            if self.handle.value and self.library is not None:
                try:
                    self._call("ANC_disconnect", self.handle, independent=True)
                except BaseException as exc:
                    errors.append(str(exc))
                    # Discovery is forbidden while any SDK device is connected.
                    # An unknown disconnect cannot be repaired by a new object.
                    _SDK_CONNECTION_UNKNOWN = True
        finally:
            self.handle, self.opened = H(), False
            if self._search is not None:
                try:
                    self._search.close()
                except BaseException as exc:
                    errors.append("ANC350 DLL search close failed: " + str(exc))
                self._search = None
            if self._lock_owned:
                _SDK_OWNER.release()
                self._lock_owned = False
            if self._lease is not None:
                try:
                    self._lease.__exit__(None, None, None)
                except BaseException as exc:
                    errors.append("ANC350 controller lease release failed: " + str(exc))
                self._lease = None
        for error in errors:
            if error not in self.close_errors:
                self.close_errors.append(error)
        if self.close_errors:
            raise Anc350Error("; ".join(self.close_errors))
