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

from attodry_control.anc350 import Anc350Device, Anc350Error, verified_library_bytes, configure_anc350_signatures
from attodry_control.positioner_config import load_positioner_config
from attodry_control.positioner_points import PositionerPointSession
from tests.test_positioner_config import positioner_document
from tests.test_positioner_points import FakeClock


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
        self.axes = {axis.index: {"position": 0., "enabled": 0, "moving": 0,
                                 "amplitude": axis.amplitude_v, "frequency": axis.frequency_hz,
                                 "sensor_connected": 1, "sensor_error": 0, "eot_forward": 0, "eot_backward": 0}
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
                values = (state["sensor_connected"], state["enabled"], state["moving"], 0,
                          state["eot_forward"], state["eot_backward"], state["sensor_error"])
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
                args[2]._obj.value = self.axes[args[1]]["amplitude"]
            elif name == "ANC_getFrequency":
                args[2]._obj.value = self.axes[args[1]]["frequency"]
            elif name == "ANC_setAmplitude":
                self.axes[args[1]]["amplitude"] = args[2]
            elif name == "ANC_setFrequency":
                self.axes[args[1]]["frequency"] = args[2]
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

    def test_default_point_session_bridge_keeps_native_call_names_through_cleanup(self):
        self.different_drive()
        clock, events = FakeClock(), []
        session = PositionerPointSession(self.config,
            event_sink=lambda event_type, payload: events.append((event_type, payload)),
            clock=clock, sleep=clock.sleep)
        self.addCleanup(session.close)
        # Keep the production PointSession -> Anc350Device callback bridge;
        # substitute only the DLL and ownership boundary with inert fakes.
        def fake_backed_device(config, **kwargs):
            return Anc350Device(config, dll_factory=lambda path: self.sdk,
                lease_factory=lambda serial: nullcontext(), **kwargs)
        with patch("attodry_control.anc350.Anc350Device", side_effect=fake_backed_device):
            self.assertIsNone(session.device_factory)
            session.preflight()
            self.assertFalse(any(call[0].startswith(("ANC_set", "ANC_start")) for call in self.sdk.calls))
            session.begin()
            session.set_point(self.config.points[1])
            self.assertEqual(session.verify(), {"positioner_x_um": 10., "positioner_y_um": 10., "positioner_z_um": 0.})
            self.assertTrue(session.cleanup()["verified"])
            self.assertEqual(session.close(), [])
        raw = [payload for event_type, payload in events if event_type == "positioner_raw"]
        for name in ("ANC_discover", "ANC_connect", "ANC_setAmplitude", "ANC_setFrequency",
                     "ANC_setTargetPosition", "ANC_startAutoMove", "ANC_disconnect"):
            records = [record for record in raw if record["name"] == name]
            self.assertTrue(any(record["phase"] == "before" for record in records), name)
            self.assertTrue(any(record["phase"] == "after" and record["code"] == 0 for record in records), name)
        self.assertEqual(sum(call[0] == "ANC_connect" for call in self.sdk.calls), 1)
        self.assertEqual(sum(call[0] == "ANC_disconnect" for call in self.sdk.calls), 1)

    def test_point_session_native_bridge_audit_failure_blocks_before_sdk_call(self):
        ownership = []
        @contextmanager
        def lease(serial):
            ownership.append("acquired")
            try:
                yield
            finally:
                ownership.append("released")
        def unavailable_audit(event_type, payload):
            if event_type == "positioner_raw" and payload["phase"] == "before":
                raise OSError("raw audit unavailable")
        session = PositionerPointSession(self.config, event_sink=unavailable_audit)
        self.addCleanup(session.close)
        def fake_backed_device(config, **kwargs):
            return Anc350Device(config, dll_factory=lambda path: self.sdk,
                lease_factory=lease, **kwargs)
        with patch("attodry_control.anc350.Anc350Device", side_effect=fake_backed_device):
            with self.assertRaisesRegex(OSError, "raw audit unavailable"):
                session.preflight()
        self.assertEqual(self.sdk.calls, [])
        self.assertEqual(ownership, ["acquired", "released"])

    def test_state_units_and_non_truncated_64_bit_handle(self):
        self.sdk.axes[0]["position"] = 1.25e-6
        self.device.open()
        state = self.device.read_state()
        self.assertEqual(state["axes"]["x"]["position_um"], 1.25)
        self.assertTrue(all(call[1] == self.sdk.handle for call in self.sdk.calls
                            if call[0] not in ("ANC_discover", "ANC_getDeviceInfo", "ANC_connect")))

    def test_prepare_and_absolute_move_commands_have_explicit_units(self):
        self.device.open()
        self.device.initialize_drive()
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
        self.device.initialize_drive()
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
        self.device.initialize_drive()
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
        self.device.initialize_drive()
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

    def test_initial_enabled_output_is_observed_without_any_setter(self):
        self.sdk.axes[1]["enabled"] = 1
        self.device.open()
        self.assertTrue(self.device.last_confirmed["axes"]["y"]["output_enabled"])
        self.assertFalse(any(call[0].startswith(("ANC_set", "ANC_start")) for call in self.sdk.calls))
        with self.assertRaisesRegex(Anc350Error, "output must be OFF"):
            self.device.initialize_drive()

    def test_startup_output_off_stops_old_auto_before_disabling_and_reads_back(self):
        for row in self.sdk.axes.values():
            row["enabled"] = 1
        self.different_drive()
        self.device.open()
        before = len(self.sdk.calls)
        state = self.device.initialize_outputs_off(guard=lambda: None)
        calls = self.sdk.calls[before:]
        writes = [call for call in calls if call[0].startswith(("ANC_set", "ANC_start"))]
        self.assertEqual([(call[0], call[2:]) for call in writes],
            [(name, (axis, 0, flag)) for axis in range(3)
             for name, flag in (("ANC_startAutoMove", 0), ("ANC_setAxisOutput", 1))])
        self.assertEqual(len(calls), 52 + 9 * 3)
        self.assertTrue(all(not row["output_enabled"] and not row["moving"] for row in state["axes"].values()))
        for call in writes:
            index = calls.index(call)
            self.assertEqual([row[0] for row in calls[index-3:index]], ["ANC_getAxisStatus"] * 3)
            if call[0] == "ANC_setAxisOutput":
                self.assertEqual(calls[index+1][0:3], ("ANC_getAxisStatus", self.sdk.handle, call[2]))
        with self.assertRaisesRegex(Anc350Error, "cannot be retried"):
            self.device.initialize_outputs_off(guard=lambda: None)
        self.device.initialize_drive()
        self.device.prepare_axis("x")

    def test_already_off_startup_makes_no_stop_or_output_write(self):
        self.device.open()
        before = len(self.sdk.calls)
        self.device.initialize_outputs_off(guard=lambda: None)
        self.assertEqual(len(self.sdk.calls) - before, 52)
        self.assertFalse(any(call[0].startswith(("ANC_set", "ANC_start")) for call in self.sdk.calls))

    def test_output_off_ack_without_readback_never_continues_or_retries(self):
        self.sdk.axes[0]["enabled"] = 1
        self.device.open()
        self.sdk.ANC_setAxisOutput = lambda *args: 0
        with self.assertRaisesRegex(Anc350Error, "output must be OFF"):
            self.device.initialize_outputs_off(guard=lambda: None)
        self.assertTrue(self.sdk.axes[0]["enabled"])
        self.assertTrue(any(event.get("name") == "startup_output_off" and
            event.get("phase") == "readback" and event["actual"]["output_enabled"] for event in self.events))
        with self.assertRaises(Anc350Error):
            self.device.initialize_outputs_off(guard=lambda: None)
        with self.assertRaises(Anc350Error):
            self.device.initialize_drive()
        self.assertEqual(self.drive_writes(), [])

    def test_startup_off_rejects_fault_or_motion_before_any_stop_or_off(self):
        for field in ("moving", "sensor_error", "eot_forward", "eot_backward", "sensor_connected"):
            with self.subTest(field=field):
                self.device.close()
                self.sdk = FakeAnc350(self.config)
                self.device = Anc350Device(self.config, dll_factory=lambda path: self.sdk,
                    lease_factory=lambda serial: nullcontext())
                self.device.open()
                self.sdk.axes[0]["enabled"] = 1
                self.sdk.axes[2][field] = 0 if field == "sensor_connected" else 1
                with self.assertRaises(Anc350Error):
                    self.device.initialize_outputs_off(guard=lambda: None)
                self.assertFalse(any(call[0].startswith(("ANC_set", "ANC_start")) for call in self.sdk.calls))

    def test_read_only_open_still_rejects_moving_axis_without_stop(self):
        self.sdk.axes[2].update(enabled=1, moving=1)
        with self.assertRaisesRegex(Anc350Error, "stationary"):
            self.device.open()
        self.assertFalse(any(call[0].startswith(("ANC_set", "ANC_start")) for call in self.sdk.calls))

    def test_startup_output_stop_or_off_failure_has_no_next_command_or_retry(self):
        for name in ("ANC_startAutoMove", "ANC_setAxisOutput"):
            for mode in ("code", "communication"):
                with self.subTest(name=name, mode=mode):
                    self.device.close()
                    self.sdk = FakeAnc350(self.config)
                    for row in self.sdk.axes.values():
                        row["enabled"] = 1
                    self.device = Anc350Device(self.config, dll_factory=lambda path: self.sdk,
                        lease_factory=lambda serial: nullcontext())
                    self.device.open()
                    if mode == "code":
                        self.sdk.codes[name] = 3
                    else:
                        def fail(function, args):
                            if function == name:
                                raise OSError("reply lost")
                        self.sdk.hook = fail
                    with self.assertRaises(Anc350Error):
                        self.device.initialize_outputs_off(guard=lambda: None)
                    writes = [call for call in self.sdk.calls if call[0].startswith(("ANC_set", "ANC_start"))]
                    self.assertEqual([row[0] for row in writes],
                        ["ANC_startAutoMove"] if name == "ANC_startAutoMove" else
                        ["ANC_startAutoMove", "ANC_setAxisOutput"])
                    with self.assertRaises(Anc350Error):
                        self.device.initialize_outputs_off(guard=lambda: None)
                    with self.assertRaises(Anc350Error):
                        self.device.initialize_drive()
                    with self.assertRaises(Anc350Error):
                        self.device.prepare_axis("x")
                    self.assertEqual(sum(call[0] == name for call in self.sdk.calls), 1)

    def test_output_release_audit_or_late_native_result_cannot_continue(self):
        for name in ("ANC_startAutoMove", "ANC_setAxisOutput"):
            for failure in ("before", "after", "deadline"):
                with self.subTest(name=name, failure=failure):
                    self.device.close()
                    self.sdk = FakeAnc350(self.config)
                    for row in self.sdk.axes.values():
                        row["enabled"] = 1
                    self.events = []
                    self.device = Anc350Device(self.config, dll_factory=lambda path: self.sdk,
                        lease_factory=lambda serial: nullcontext(), on_event=self.events.append)
                    self.device.open()
                    clock = FakeClock()
                    def audit(record):
                        self.events.append(record)
                        if record['name'] == name and record['phase'] == failure:
                            raise OSError("output audit unavailable")
                    self.device.on_event = audit
                    def late(function, args):
                        if function == name and failure == "deadline":
                            clock.now = 10.
                    self.sdk.hook = late
                    def checkpoint():
                        if clock.now > 1.:
                            raise Anc350Error("output deadline")
                    with self.assertRaises((Anc350Error, OSError)):
                        self.device.initialize_outputs_off(guard=lambda: None, checkpoint=checkpoint)
                    writes = [call for call in self.sdk.calls if call[0].startswith(("ANC_set", "ANC_start"))]
                    self.assertTrue(all(call[2] == 0 for call in writes))
                    self.assertEqual(sum(call[0] == name for call in writes), int(failure != "before"))
                    if failure == "deadline":
                        self.assertTrue(any(event['name'] == name and event['phase'] == 'after' for event in self.events))
                    with self.assertRaises(Anc350Error):
                        self.device.initialize_drive()
                    with self.assertRaises(Anc350Error):
                        self.device.initialize_outputs_off(guard=lambda: None)

    def test_output_release_rechecks_dark_guard_between_stop_and_off(self):
        self.sdk.axes[0]["enabled"] = 1
        self.device.open()
        def guard():
            if any(call[0] == "ANC_startAutoMove" for call in self.sdk.calls):
                raise RuntimeError("dark evidence lost")
        with self.assertRaisesRegex(RuntimeError, "dark evidence lost"):
            self.device.initialize_outputs_off(guard=guard)
        writes = [call for call in self.sdk.calls if call[0].startswith(("ANC_set", "ANC_start"))]
        self.assertEqual([call[0] for call in writes], ["ANC_startAutoMove"])
        self.assertTrue(self.sdk.axes[0]["enabled"])

    def test_output_release_checks_all_axis_identity_and_position_before_writes(self):
        self.sdk.axes[0]["enabled"] = 1
        self.device.open()
        self.sdk.axes[2]["position"] = (self.config.axes[2].max_position_um + 1) * 1e-6
        with self.assertRaisesRegex(Anc350Error, "position/sensor/EOT"):
            self.device.initialize_outputs_off(guard=lambda: None)
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
        self.device.initialize_drive()
        self.device.prepare_axis("x")

    def test_lower_layer_fresh_read_protects_max_step_and_disabled_output(self):
        self.device.open()
        self.device.initialize_drive()
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

    def different_drive(self):
        for state in self.sdk.axes.values():
            state.update(amplitude=1., frequency=2.)

    def drive_writes(self):
        return [call for call in self.sdk.calls if call[0] in ("ANC_setAmplitude", "ANC_setFrequency")]

    def test_drive_setter_abi_uses_pointer_handle_uint32_axis_double_and_int32_result(self):
        callback = C.CFUNCTYPE(C.c_int32, C.c_void_p, C.c_uint32, C.c_double)
        for name in ("ANC_setAmplitude", "ANC_setFrequency"):
            setattr(self.sdk, name, callback(lambda handle, axis, value: 0))
        configure_anc350_signatures(self.sdk)
        for name in ("ANC_setAmplitude", "ANC_setFrequency"):
            self.assertEqual(getattr(self.sdk, name).argtypes, [C.c_void_p, C.c_uint32, C.c_double])
            self.assertIs(getattr(self.sdk, name).restype, C.c_int32)
        self.assertEqual(self.sdk.calls, [])

    def test_initial_drive_difference_is_read_only_and_public_read_remains_strict(self):
        self.different_drive()
        self.device.open()
        state = self.device._read_initial_state()
        self.assertEqual(state["axes"]["x"]["amplitude_v"], 1.)
        with self.assertRaisesRegex(Anc350Error, "actual=.*requested="):
            self.device.read_state()
        with self.assertRaisesRegex(Anc350Error, "completed startup"):
            self.device.prepare_axis("x")
        self.assertFalse(any(call[0].startswith(("ANC_set", "ANC_start")) for call in self.sdk.calls))

    def test_startup_sets_each_changed_drive_once_with_raw_audit_and_no_persistent_save(self):
        self.different_drive()
        self.device.open()
        before = len(self.sdk.calls)
        state = self.device.initialize_drive()
        writes = self.drive_writes()
        expected = [(name, self.sdk.handle, axis.index, value) for axis in self.config.axes
                    for name, value in (("ANC_setAmplitude", axis.amplitude_v), ("ANC_setFrequency", axis.frequency_hz))]
        self.assertEqual(writes, expected)
        # 26 baseline reads + 6 * (3 status + setter + getter) + 26 final reads.
        self.assertEqual(len(self.sdk.calls) - before, 82)
        for call in writes:
            index = self.sdk.calls.index(call)
            self.assertEqual([row[0] for row in self.sdk.calls[index-3:index]], ["ANC_getAxisStatus"] * 3)
        for name in ("ANC_setAmplitude", "ANC_setFrequency"):
            rows = [event for event in self.events if event["name"] == name]
            self.assertEqual([row["phase"] for row in rows], ["before", "after", "readback"] * 3)
            self.assertTrue(all(row["code"] == 0 for row in rows if row["phase"] == "after"))
        self.assertEqual(state["axes"]["z"]["frequency_hz"], self.config.axes[2].frequency_hz)
        self.assertFalse(any(call[0] == "ANC_saveParams" for call in self.sdk.calls))
        with self.assertRaisesRegex(Anc350Error, "cannot be retried"):
            self.device.initialize_drive()
        with self.assertRaisesRegex(Anc350Error, "scope has ended"):
            self.device._read_initial_state()
        self.assertEqual(self.drive_writes(), expected)

    def test_matched_startup_settings_are_not_rewritten(self):
        self.device.open()
        before = len(self.sdk.calls)
        self.device.initialize_drive()
        self.assertEqual(len(self.sdk.calls) - before, 52)
        self.assertEqual(self.drive_writes(), [])
        self.device.prepare_axis("x")

    def test_all_axes_must_be_healthy_off_stationary_before_any_startup_setter(self):
        self.different_drive()
        self.device.open()
        for field in ("enabled", "moving", "sensor_error", "eot_forward", "eot_backward", "sensor_connected"):
            with self.subTest(field=field):
                self.device.close()
                self.sdk = FakeAnc350(self.config)
                self.different_drive()
                self.device = Anc350Device(self.config, dll_factory=lambda path: self.sdk,
                    lease_factory=lambda serial: nullcontext())
                self.device.open()
                self.sdk.axes[2][field] = 0 if field == "sensor_connected" else 1
                with self.assertRaises(Anc350Error):
                    self.device.initialize_drive()
                self.assertEqual(self.drive_writes(), [])

    def test_initial_identity_mismatch_prevents_drive_settings(self):
        self.different_drive()
        self.device.open()
        self.sdk.actuator_type = 1
        with self.assertRaisesRegex(Anc350Error, "preset/LUT"):
            self.device.initialize_drive()
        self.assertEqual(self.drive_writes(), [])

    def test_output_change_between_drive_setters_stops_partial_configuration(self):
        self.different_drive()
        self.device.open()
        def enable_other_axis(name, args):
            if name == "ANC_setAmplitude":
                self.sdk.axes[2]["enabled"] = 1
        self.sdk.hook = enable_other_axis
        with self.assertRaisesRegex(Anc350Error, "all axes healthy, output OFF"):
            self.device.initialize_drive()
        self.assertEqual([call[0] for call in self.drive_writes()], ["ANC_setAmplitude"])
        with self.assertRaises(Anc350Error):
            self.device.initialize_drive()
        with self.assertRaises(Anc350Error):
            self.device.prepare_axis("x")
        self.assertFalse(any(call[0] in ("ANC_setAxisOutput", "ANC_setTargetPosition", "ANC_startAutoMove") for call in self.sdk.calls))

    def test_startup_readback_mismatch_records_actual_and_never_retries(self):
        self.different_drive()
        self.device.open()
        self.sdk.ANC_setAmplitude = lambda *args: 0  # ACK without changing register.
        with self.assertRaisesRegex(Anc350Error, "requested=.*actual=.*no retry"):
            self.device.initialize_drive()
        raw = [event for event in self.events if event.get("phase") == "readback"]
        self.assertEqual(raw[0]["actual"], 1.)
        self.assertEqual(raw[0]["requested"], self.config.axes[0].amplitude_v)
        with self.assertRaises(Anc350Error):
            self.device.initialize_drive()
        self.assertFalse(any(call[0] in ("ANC_setFrequency", "ANC_setAxisOutput") for call in self.sdk.calls))

    def test_startup_setter_communication_failure_has_no_retry_or_rollback(self):
        self.different_drive()
        self.device.open()
        def fail(name, args):
            if name == "ANC_setAmplitude":
                raise OSError("reply lost")
        self.sdk.hook = fail
        with self.assertRaisesRegex(Anc350Error, "communication failed; no retry"):
            self.device.initialize_drive()
        with self.assertRaises(Anc350Error):
            self.device.initialize_drive()
        self.assertEqual([call[0] for call in self.drive_writes()], ["ANC_setAmplitude"])
        self.assertFalse(any(call[0] == "ANC_setAxisOutput" for call in self.sdk.calls))

    def test_startup_audit_failure_before_or_after_setter_never_continues(self):
        for phase, expected in (("before", 0), ("after", 1)):
            with self.subTest(phase=phase):
                self.device.close()
                self.sdk = FakeAnc350(self.config)
                self.different_drive()
                self.device = Anc350Device(self.config, dll_factory=lambda path: self.sdk,
                    lease_factory=lambda serial: nullcontext())
                self.device.open()
                def fail(record):
                    if record["name"] == "ANC_setAmplitude" and record["phase"] == phase:
                        raise OSError("audit unavailable")
                self.device.on_event = fail
                with self.assertRaisesRegex(OSError, "audit unavailable"):
                    self.device.initialize_drive()
                with self.assertRaises(Anc350Error):
                    self.device.initialize_drive()
                self.assertEqual(len(self.drive_writes()), expected)
                with self.assertRaises(Anc350Error):
                    self.device.prepare_axis("x")

    def test_late_startup_setter_cannot_enable_output_or_issue_next_setting(self):
        self.different_drive()
        self.device.open()
        clock = FakeClock()
        def late(name, args):
            if name == "ANC_setAmplitude":
                clock.now = 10.
        self.sdk.hook = late
        def checkpoint():
            if clock.now > 1.:
                raise Anc350Error("startup deadline")
        with self.assertRaisesRegex(Anc350Error, "deadline"):
            self.device.initialize_drive(checkpoint=checkpoint)
        self.assertEqual([call[0] for call in self.drive_writes()], ["ANC_setAmplitude"])
        self.assertTrue(any(event["name"] == "ANC_setAmplitude" and event["phase"] == "after" for event in self.events))
        with self.assertRaises(Anc350Error):
            self.device.prepare_axis("x")

    def test_runtime_drive_drift_is_rejected_without_reconfiguration(self):
        self.device.open()
        self.device.initialize_drive()
        self.device.prepare_axis("x")
        self.sdk.axes[1]["frequency"] += 1.
        before = len(self.sdk.calls)
        with self.assertRaisesRegex(Anc350Error, "no automatic repair"):
            self.device.move_to("x", 1.)
        self.assertFalse(any(call[0].startswith(("ANC_set", "ANC_start")) for call in self.sdk.calls[before:]))

    def test_optical_guard_is_rechecked_between_startup_settings(self):
        self.different_drive()
        self.device.open()
        guards = []
        def guard():
            guards.append(len(self.drive_writes()))
            if self.drive_writes():
                raise RuntimeError("optical OFF evidence lost")
        with self.assertRaisesRegex(RuntimeError, "OFF evidence lost"):
            self.device.initialize_drive(guard=guard)
        self.assertEqual(guards, [0, 0, 1])
        self.assertEqual([call[0] for call in self.drive_writes()], ["ANC_setAmplitude"])
        with self.assertRaises(Anc350Error):
            self.device.prepare_axis("x")

    def test_post_write_getter_failure_retains_partial_setting_and_blocks_outputs(self):
        self.different_drive()
        self.device.open()
        def fail_readback(name, args):
            if name == "ANC_getAmplitude" and self.drive_writes():
                raise OSError("readback lost")
        self.sdk.hook = fail_readback
        with self.assertRaisesRegex(Anc350Error, "readback lost"):
            self.device.initialize_drive()
        self.assertEqual(self.sdk.axes[0]["amplitude"], self.config.axes[0].amplitude_v)
        self.assertEqual([call[0] for call in self.drive_writes()], ["ANC_setAmplitude"])
        with self.assertRaises(Anc350Error):
            self.device.initialize_drive()
        with self.assertRaises(Anc350Error):
            self.device.prepare_axis("x")
