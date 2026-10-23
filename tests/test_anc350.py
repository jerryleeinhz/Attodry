"""Vendor ABI/unit/ownership regressions using inert fake PE bytes and SDK."""
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from dataclasses import replace
import ctypes as C
import hashlib
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from attodry_control.anc350 import Anc350Device, Anc350Error, verified_library_bytes
from attodry_control.positioner_config import load_positioner_config
from tests.test_positioner_config import positioner_document


def pe_bytes(machine=0x8664, magic=0x20b):
    data = bytearray(256)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3c, 128)
    data[128:132] = b"PE\0\0"
    struct.pack_into("<H", data, 132, machine)
    struct.pack_into("<H", data, 152, magic)
    return bytes(data)


class FakeAnc350:
    def __init__(self, config):
        self.config, self.calls, self.codes = config, [], {}
        self.handle = 0x1234567887654321
        self.count, self.kind, self.connected = 1, 0, 0
        self.serial, self.address = config.controller_serial_number, "USB"
        self.null_handle = self.partial_boolean = self.nonfinite_position = False
        self.actuator_type = 0
        self.axes = {axis.index: {"position": 0., "enabled": 0, "moving": 0}
                     for axis in config.axes}
        self.hook = None

    def __getattr__(self, name):
        if not name.startswith("ANC_"):
            raise AttributeError(name)
        def function(*args):
            primitives = tuple(value.value if isinstance(value, C._SimpleCData) else value
                               if isinstance(value, (int, float)) else "pointer" for value in args)
            self.calls.append((name, *primitives))
            if self.hook:
                self.hook(name, args)
            if name == "ANC_discover":
                args[1]._obj.value = self.count
            elif name == "ANC_getDeviceInfo":
                args[1]._obj.value, args[2]._obj.value = self.kind, -1
                args[3].value, args[4].value = self.serial.encode(), self.address.encode()
                args[5]._obj.value = self.connected
            elif name == "ANC_connect":
                args[1]._obj.value = None if self.null_handle else self.handle
            elif name == "ANC_getFirmwareVersion":
                args[1]._obj.value = self.config.expected_firmware_raw
            elif name == "ANC_getDeviceConfig":
                args[1]._obj.value = 2
            elif name == "ANC_getAxisStatus":
                state = self.axes[args[1]]
                values = (1, state["enabled"], state["moving"], 0, 0, 0, 0)
                for pointer, value in zip(args[2:], values):
                    if self.partial_boolean:
                        C.cast(pointer, C.POINTER(C.c_ubyte))[0] = value
                    else:
                        pointer._obj.value = value
            elif name in ("ANC_getActuatorName", "ANC_getLutName"):
                axis = self.config.axes[args[1]]
                args[2].value = (axis.expected_actuator_name if name.endswith("ActuatorName")
                                 else axis.expected_lut_name).encode()
            elif name == "ANC_getActuatorType":
                args[2]._obj.value = self.actuator_type
            elif name == "ANC_getPosition":
                args[2]._obj.value = float("nan") if self.nonfinite_position else self.axes[args[1]]["position"]
            elif name == "ANC_getAmplitude":
                args[2]._obj.value = self.config.axes[args[1]].amplitude_v
            elif name == "ANC_getFrequency":
                args[2]._obj.value = self.config.axes[args[1]].frequency_hz
            elif name == "ANC_setAxisOutput":
                self.axes[args[1]]["enabled"] = args[2]
            elif name == "ANC_setTargetPosition":
                self.axes[args[1]]["position"] = args[2]
            elif name == "ANC_startAutoMove" and args[2] == 0:
                self.axes[args[1]]["moving"] = 0
            return self.codes.get(name, 0)
        return function


class Anc350Tests(unittest.TestCase):
    def setUp(self):
        unknown = patch("attodry_control.anc350._SDK_CONNECTION_UNKNOWN", False)
        unknown.start()
        self.addCleanup(unknown.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "fixture.dll"
        self.path.write_bytes(pe_bytes())
        document = positioner_document()
        document["positioner"].update(dll_path=str(self.path), dll_sha256=hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.config = load_positioner_config(document)
        self.sdk = FakeAnc350(self.config)
        self.events = []
        self.device = Anc350Device(self.config, dll_factory=lambda path: self.sdk,
            lease_factory=lambda serial: nullcontext(), on_event=lambda event: self.events.append(event))
        def finish():
            try:
                self.device.close()
            except Anc350Error:
                if not self.device.close_errors:
                    raise
        self.addCleanup(finish)

    def test_passive_construction_and_strict_usb_identity(self):
        self.assertEqual(self.sdk.calls, [])
        self.device.open()
        self.assertEqual(self.sdk.calls[0][0:2], ("ANC_discover", 1))
        self.assertEqual(self.device.handle.value, self.sdk.handle)
        self.assertFalse(any(call[0].startswith(("ANC_set", "ANC_start")) for call in self.sdk.calls))

    def test_state_units_and_non_truncated_64_bit_handle(self):
        self.sdk.axes[0]["position"] = 1.25e-6
        self.device.open()
        state = self.device.read_state()
        self.assertEqual(state["axes"]["x"]["position_um"], 1.25)
        self.assertTrue(all(call[1] == self.sdk.handle for call in self.sdk.calls
                            if call[0] not in ("ANC_discover", "ANC_getDeviceInfo", "ANC_connect")))

    def test_prepare_and_absolute_move_commands_have_explicit_units(self):
        self.device.open()
        self.device.prepare_axis("x")
        self.device.move_to("x", 12.5)
        writes = [call for call in self.sdk.calls if call[0].startswith(("ANC_set", "ANC_start"))]
        self.assertEqual([row[0] for row in writes], ["ANC_setTargetPosition", "ANC_setTargetGround", "ANC_setTargetRange",
             "ANC_setAxisOutput", "ANC_setTargetPosition", "ANC_startAutoMove"])
        self.assertEqual(writes[0][-1], 0.)
        self.assertEqual(writes[1][2:], (0, 0))
        self.assertAlmostEqual(writes[2][-1], .1e-6)
        self.assertEqual(writes[3][2:], (0, 1, 1))
        self.assertAlmostEqual(writes[4][-1], 12.5e-6)
        self.assertEqual(writes[5][2:], (0, 1, 0))

    def test_wrong_serial_never_connects(self):
        self.sdk.serial = "different"
        with self.assertRaisesRegex(Anc350Error, "identity"):
            self.device.open()
        self.assertFalse(any(row[0] == "ANC_connect" for row in self.sdk.calls))

    def test_wrong_type_interface_or_ownership_never_connects(self):
        for attribute, value in (("kind", 1), ("address", "127.0.0.1"), ("connected", -1)):
            with self.subTest(attribute=attribute):
                sdk = FakeAnc350(self.config)
                setattr(sdk, attribute, value)
                device = Anc350Device(self.config, dll_factory=lambda path: sdk, lease_factory=lambda serial: nullcontext())
                with self.assertRaises(Anc350Error):
                    device.open()
                self.assertFalse(any(row[0] == "ANC_connect" for row in sdk.calls))

    def test_partial_boolean_does_not_masquerade_as_false(self):
        self.sdk.partial_boolean = True
        with self.assertRaisesRegex(Anc350Error, "32-bit boolean"):
            self.device.open()
        self.assertEqual(sum(call[0] == "ANC_disconnect" for call in self.sdk.calls), 1)

    def test_nonfinite_position_and_angular_actuator_rejected(self):
        self.sdk.nonfinite_position = True
        with self.assertRaisesRegex(Anc350Error, "finite"):
            self.device.open()

    def test_firmware_or_preset_mismatch_closes_independently(self):
        self.sdk.actuator_type = 1
        with self.assertRaisesRegex(Anc350Error, "preset/LUT"):
            self.device.open()
        self.assertEqual(sum(call[0] == "ANC_disconnect" for call in self.sdk.calls), 1)

    def test_connect_error_with_returned_handle_still_disconnects(self):
        self.sdk.codes["ANC_connect"] = 3
        with self.assertRaisesRegex(Anc350Error, "ANC_connect"):
            self.device.open()
        self.assertEqual(sum(call[0] == "ANC_disconnect" for call in self.sdk.calls), 1)

    def test_null_handle_and_zero_devices_rejected(self):
        self.sdk.null_handle = True
        with self.assertRaisesRegex(Anc350Error, "null handle"):
            self.device.open()
        self.assertFalse(any(call[0] == "ANC_disconnect" for call in self.sdk.calls))

    def test_hash_and_x86_rejected_before_factory(self):
        with self.assertRaisesRegex(Anc350Error, "SHA256"):
            verified_library_bytes(self.path, "0" * 64)
        self.path.write_bytes(pe_bytes(0x14c, 0x10b))
        with self.assertRaisesRegex(Anc350Error, "AMD64"):
            verified_library_bytes(self.path, hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.assertEqual(self.sdk.calls, [])

    def test_own_normal_stop_once_and_never_output_off(self):
        self.device.open()
        self.assertFalse(self.device.stop("x"))
        self.device.prepare_axis("x")
        self.device.move_to("x", 1)
        self.assertTrue(self.device.stop("x"))
        self.assertFalse(self.device.stop("x"))
        self.device.close()
        stops = [row for row in self.sdk.calls if row[0] == "ANC_startAutoMove" and row[-2] == 0]
        self.assertEqual(len(stops), 1)
        self.assertFalse(any(row[0] == "ANC_setAxisOutput" and row[-2] == 0 for row in self.sdk.calls))

    def test_uncertain_target_is_not_reissued(self):
        self.device.open()
        self.device.prepare_axis("x")
        self.sdk.codes["ANC_setTargetPosition"] = 1
        with self.assertRaises(Anc350Error):
            self.device.move_to("x", 1)
        with self.assertRaises(Anc350Error):
            self.device.move_to("x", 1)
        self.device.stop("x")
        self.assertEqual(sum(row[0] == "ANC_setTargetPosition" for row in self.sdk.calls), 2)  # preload + uncertain move, no replay

    def test_failed_audit_cannot_block_owned_stop_or_disconnect(self):
        self.device.open()
        self.device.prepare_axis("x")
        self.device.move_to("x", 1)
        self.device.on_event = lambda event: (_ for _ in ()).throw(RuntimeError("disk full"))
        self.device.stop("x")
        self.assertEqual(self.device.read_state_independent()["axes"]["x"]["moving"], False)
        self.device.close()
        self.assertTrue(self.device.audit_errors)
        self.assertEqual(sum(call[0] == "ANC_disconnect" for call in self.sdk.calls), 1)

    def test_process_sdk_owner_and_os_lease_released_after_failure(self):
        entered = []
        @contextmanager
        def lease(serial):
            entered.append("enter")
            try:
                yield
            finally:
                entered.append("exit")
        self.device.lease_factory = lease
        self.device.open()
        other = Anc350Device(self.config, dll_factory=lambda path: self.sdk, lease_factory=lease)
        with self.assertRaisesRegex(Anc350Error, "owns this process"):
            other.open()
        self.device.close()
        self.assertEqual(entered, ["enter", "exit"])

    def test_lease_failure_happens_before_vendor_discovery(self):
        @contextmanager
        def fail(serial):
            raise ValueError("occupied")
            yield
        self.device.lease_factory = fail
        with self.assertRaisesRegex(ValueError, "occupied"):
            self.device.open()
        self.assertEqual(self.sdk.calls, [])

    def test_initial_enabled_output_rejected_without_any_setter(self):
        self.sdk.axes[1]["enabled"] = 1
        with self.assertRaisesRegex(Anc350Error, "initial output"):
            self.device.open()
        self.assertFalse(any(call[0].startswith(("ANC_set", "ANC_start")) for call in self.sdk.calls))

    def test_prepare_replaces_retained_target_before_output_enable(self):
        self.sdk.axes[0]["position"] = 3e-6
        retained = {"target": 100e-6}
        def retained_auto(name, args):
            if name == "ANC_setTargetPosition":
                retained["target"] = args[2]
            if name == "ANC_setAxisOutput" and args[2] == 1:
                self.assertAlmostEqual(retained["target"], 3e-6)
        self.sdk.hook = retained_auto
        self.device.open()
        self.device.prepare_axis("x")

    def test_lower_layer_fresh_read_protects_max_step_and_disabled_output(self):
        self.device.open()
        self.device.prepare_axis("x")
        self.device.config = replace(self.config, axes=tuple(replace(axis, max_move_um=1) for axis in self.config.axes))
        before = len(self.sdk.calls)
        with self.assertRaisesRegex(Anc350Error, "step exceeds"):
            self.device.move_to("x", 2)
        self.assertFalse(any(call[0].startswith(("ANC_set", "ANC_start")) for call in self.sdk.calls[before:]))
        self.sdk.axes[0]["enabled"] = 0
        with self.assertRaisesRegex(Anc350Error, "enabled stationary"):
            self.device.move_to("x", .5)

    def test_partial_open_disconnect_failure_is_persisted_without_retry(self):
        self.sdk.codes.update(ANC_connect=3, ANC_disconnect=2)
        with self.assertRaisesRegex(Anc350Error, "ANC_connect"):
            self.device.open()
        self.assertTrue(any("ANC_disconnect" in error for error in self.device.close_errors))
        with self.assertRaisesRegex(Anc350Error, "ANC_disconnect"):
            self.device.close()
        self.assertEqual(sum(call[0] == "ANC_disconnect" for call in self.sdk.calls), 1)
        before = len(self.sdk.calls)
        other = Anc350Device(self.config, dll_factory=lambda path: self.sdk,
                             lease_factory=lambda serial: nullcontext())
        with self.assertRaisesRegex(Anc350Error, "manual review required"):
            other.open()
        self.assertEqual(len(self.sdk.calls), before)
