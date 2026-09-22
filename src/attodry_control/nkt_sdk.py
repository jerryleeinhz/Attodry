"""Small NKTPDLL adapter, verified against SDK 2.1.16.3027 C headers.

Only explicit open() loads the DLL. This module never scans buses, resets
interlocks, enables watchdogs, or starts the vendor's background monitor.
LLTF is a different SDK and is deliberately not mapped to Interbus registers.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
from pathlib import Path
from threading import Lock

from .nkt_config import NktConfig, NktError, tenths
from .nkt_control import SOURCE_FAULT_MASK, VARIA_FAULT_MASK, VARIA_MOVING_MASK, utc_now


class NktSdkError(RuntimeError):
    pass


class _CtypesApi:
    def __init__(self, path):
        if os.name != "nt" or ctypes.sizeof(ctypes.c_void_p) != 8:
            raise NktError("The selected runtime requires 64-bit Windows Python")
        path = Path(path).resolve(strict=True)
        if path.name.lower() != "nktpdll.dll":
            raise NktError("Select the vendor x64 NKTPDLL.dll explicitly")
        # cdecl signatures from NKTPDLL.h; no installed wrapper import side effects.
        self.dll = ctypes.CDLL(str(path))
        self.metadata = {"dll_path": str(path),
                         "dll_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                         "binding_header_sdk": "2.1.16.3027", "pointer_bits": 64}
        signatures = {
            "openPorts": [ctypes.c_char_p, ctypes.c_ubyte, ctypes.c_ubyte],
            "closePorts": [ctypes.c_char_p],
            "registerRead": [ctypes.c_char_p, ctypes.c_ubyte, ctypes.c_ubyte,
                             ctypes.c_void_p, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_short],
            "registerWriteU8": [ctypes.c_char_p, ctypes.c_ubyte, ctypes.c_ubyte,
                                ctypes.c_ubyte, ctypes.c_short],
            "registerWriteU16": [ctypes.c_char_p, ctypes.c_ubyte, ctypes.c_ubyte,
                                 ctypes.c_ushort, ctypes.c_short],
        }
        for name, args in signatures.items():
            function = getattr(self.dll, name)
            function.argtypes, function.restype = args, ctypes.c_ubyte

    def open(self, port):
        return self.dll.openPorts(port.encode("ascii"), 0, 0)

    def close(self, port):
        return self.dll.closePorts(port.encode("ascii"))

    def read(self, port, address, register):
        data = ctypes.create_string_buffer(255)
        size = ctypes.c_ubyte(255)
        code = self.dll.registerRead(port.encode("ascii"), address, register,
                                     data, ctypes.byref(size), -1)
        return code, data.raw[:size.value]

    def write(self, port, address, register, value, width):
        return getattr(self.dll, f"registerWriteU{width * 8}")(
            port.encode("ascii"), address, register, value, -1)


class NktSdk:
    is_hardware = True
    _owned_ports = set()
    _ownership_lock = Lock()

    def __init__(self, config: NktConfig, *, api=None, authorize_writes=False):
        self.config, self.api = config, api
        self.writable = authorize_writes
        self.opened = False
        self.last_partial_readback = {}
        self.runtime_metadata = {}
        self.command_log = []

    @staticmethod
    def _check(code, operation):
        if code != 0:
            raise NktSdkError(f"{operation}: NKTPDLL result={code}")

    def open(self, *, authorize_connection=False):
        if not authorize_connection:
            raise NktError("Explicit instrument connection authorization required")
        self.config.require_hardware(writes=self.writable)
        key = self.config.port.upper()
        with self._ownership_lock:
            if key in self._owned_ports:
                raise NktError("Port already owned by this process")
            self._owned_ports.add(key)
        open_attempted = False
        try:
            if self.api is None:
                self.api = _CtypesApi(self.config.dll_path)
            self.runtime_metadata = getattr(self.api, "metadata", {"transport": "injected test API"})
            open_attempted = True
            self._check(self.api.open(self.config.port), "openPorts(auto=0, live=0)")
            self.opened = True
        except BaseException as primary:
            if open_attempted:
                try:
                    self._check(self.api.close(self.config.port), "close after failed open")
                except BaseException as close_error:
                    # Unknown port state: keep process ownership until manual recovery.
                    raise NktSdkError(f"{primary}; cleanup failed: {close_error}") from primary
            with self._ownership_lock:
                self._owned_ports.discard(key)
            raise
        return self

    def close(self):
        if self.opened:
            # If close fails, retain ownership so another controller cannot reopen it.
            self._check(self.api.close(self.config.port), "closePorts")
            self.opened = False
            with self._ownership_lock:
                self._owned_ports.discard(self.config.port.upper())

    def _read(self, address, register):
        if not self.opened:
            raise NktError("Port has not been explicitly opened")
        code, value = self.api.read(self.config.port, address, register)
        self._check(code, f"read {address:#x}/{register:#x}")
        if not isinstance(value, bytes) or not value:
            raise NktSdkError("Empty or malformed register reply")
        self.last_partial_readback[f"{address:02x}/{register:02x}"] = value.hex()
        return value

    def _uint(self, address, register, widths=(2,)):
        raw = self._read(address, register)
        if len(raw) not in widths:
            raise NktSdkError(f"Unexpected register length at {register:#x}: {len(raw)}")
        return int.from_bytes(raw, "little")

    def _identity(self, address, module_type, expected_serial):
        actual_type = self._uint(address, 0x61, (1, 2))
        serial = self._read(address, 0x65).decode("ascii").rstrip("\x00 ")
        firmware = self._read(address, 0x64).hex()
        if actual_type != module_type or (expected_serial and serial != expected_serial):
            raise NktSdkError("Unexpected module type or serial number; no writes permitted")
        return {"module_type": actual_type, "serial": serial, "firmware_raw_hex": firmware}

    def read_source(self):
        c, a = self.config, self.config.source_address
        identity = self._identity(a, 0x60, c.source_serial)
        emission = self._uint(a, 0x30, (1,))
        setup = self._uint(a, 0x31)
        interlock = self._uint(a, 0x32)
        status = self._uint(a, 0x66)
        current = self._uint(a, 0x38) / 10
        power = self._uint(a, 0x37) / 10
        ratio = self._uint(a, 0x34, (1, 2))
        if current > 100 or power > 100 or ratio < 1:
            raise NktSdkError("Invalid source setting readback")
        control = {0: "current", 1: "power"}.get(setup, f"unsupported_setup_{setup}")
        return {"identity": identity, "emission_state": emission, "setup_raw": setup,
                "control": control, "level_pct": current if setup == 0 else power,
                "current_setpoint_pct": current, "power_setpoint_pct": power,
                "interlock": interlock, "status_bits": status,
                "pulse_picker_ratio": ratio}

    def read_filter(self):
        if self.config.mode == "broadband":
            return None
        if self.config.mode != "varia_bandpass":
            raise NktError("LLTF SDK not available")
        c, a = self.config, self.config.varia_address
        identity = self._identity(a, 0x68, c.varia_serial)
        low, high = self._uint(a, 0x34) / 10, self._uint(a, 0x33) / 10
        nd, status = self._uint(a, 0x32) / 10, self._uint(a, 0x66)
        monitor = self._uint(a, 0x13) / 10 if c.varia_monitor_present else None
        return {"kind": "varia", "identity": identity, "lower_edge_nm": low,
                "upper_edge_nm": high, "center_setpoint_nm": (low + high) / 2,
                "bandwidth_setpoint_nm": high - low, "nd_pct": nd,
                "status_bits": status, "moving": bool(status & VARIA_MOVING_MASK),
                "monitor_pct": monitor}

    def _write(self, address, register, value, width=2):
        if not self.opened or not self.writable:
            raise NktError("Setting writes were not authorized")
        self._send_write(address, register, value, width)
        if self._uint(address, register, (width,)) != value:
            raise NktSdkError(f"Setting readback mismatch at {register:#x}")

    def _send_write(self, address, register, value, width):
        entry = {"captured_at_utc": utc_now(), "address": address, "register": register,
                 "value": value, "width_bytes": width, "result": None, "error": None}
        self.command_log.append(entry)
        try:
            entry["result"] = self.api.write(self.config.port, address, register, value, width)
            self._check(entry["result"], f"write {address:#x}/{register:#x}")
        except BaseException as exc:
            entry["error"] = f"{type(exc).__name__}: {exc}"
            raise

    def set_emission(self, enabled):
        if not isinstance(enabled, bool) or not self.opened or not self.writable:
            raise NktError("Emission write not authorized or malformed")
        a = self.config.source_address
        if enabled:
            source = self.read_source()
            filt = self.read_filter()
            if source["interlock"] != 2 or source["status_bits"] & SOURCE_FAULT_MASK:
                raise NktError("Source is not ready for emission")
            if source["control"] != self.config.source_control or source["level_pct"] > self.config.max_level_pct:
                raise NktError("Source mode or output exceeds configured limits")
            if filt and (filt["moving"] or filt["status_bits"] & VARIA_FAULT_MASK):
                raise NktError("Filter is not ready for emission")
            if source["emission_state"] == 3 and source["status_bits"] & 1:
                return
        else:
            try:
                if self._uint(a, 0x30, (1,)) == 0 and not self._uint(a, 0x66) & 1:
                    return
            except Exception:
                pass  # An authorized emergency off still attempts the write.
        self._send_write(a, 0x30, 3 if enabled else 0, 1)
        # Emission has intermediate states: the controller waits and verifies.

    def configure(self, point):
        point.validate(self.config.mode, self.config.max_level_pct, self.config.allowed_pp_ratios)
        if not self.writable:
            raise NktError("Setting writes were not authorized")
        source = self.read_source()
        filt = self.read_filter()
        if source["emission_state"] != 0 or source["status_bits"] & (SOURCE_FAULT_MASK | 1) or source["interlock"] != 2:
            raise NktError("Settings require healthy confirmed emission off")
        if source["control"] != self.config.source_control:
            raise NktError("Control mode must already match; no implicit mode switch")
        if filt and (filt["moving"] or filt["status_bits"] & VARIA_FAULT_MASK):
            raise NktError("Filter not ready for settings")
        a = self.config.source_address
        if source["level_pct"] != point.source_level_pct:
            self._write(a, 0x38 if self.config.source_control == "current" else 0x37,
                        tenths(point.source_level_pct))
        if point.pulse_picker_ratio is not None and source["pulse_picker_ratio"] != point.pulse_picker_ratio:
            self._write(a, 0x34, point.pulse_picker_ratio,
                        1 if point.pulse_picker_ratio < 256 else 2)
        if filt:
            low, high = point.edges_nm
            a = self.config.varia_address
            # Avoid an inverted intermediate band when moving either direction.
            edges = [(0x34, low, filt["lower_edge_nm"]), (0x33, high, filt["upper_edge_nm"])]
            if low >= filt["upper_edge_nm"]:
                edges.reverse()
            for reg, target, actual in edges:
                if target != actual:
                    self._write(a, reg, tenths(target))
            if point.nd_pct != filt["nd_pct"]:
                self._write(a, 0x32, tenths(point.nd_pct))
