"""Hardware-blocked, byte-level KDC101/D2XX response-ownership tests."""
import ctypes as ct
from dataclasses import replace
import struct
import unittest
from unittest.mock import patch

from attodry_control.rotator_config import RotatorDeviceConfig
from attodry_control.thorlabs_kdc101 import (
    D2xxTransport, Kdc101Device, Kdc101Error, PRM1Z8_COUNTS_PER_DEGREE,
    decode_status_bits,
)


def payload_reply(message, payload, *, source=0x50, destination=0x81):
    return struct.pack("<HHBB", message, len(payload), destination, source) + payload


def short_reply(message, first=1, second=0):
    return struct.pack("<HBBBB", message, first, second, 0x01, 0x50)


class FakeD2xx:
    """Deterministic fake hardware; no SDK, DLL loader or real USB access."""
    def __init__(self):
        self.calls, self.writes, self.queue = [], [], bytearray()
        self.codes = {}
        self.serial, self.model, self.channels = "27000001", "KDC101", 1
        self.position, self.status = 0, 0x80000500
        self.read_short = self.write_short = False
        self.response_transform = None
        self.ignore_enable = False
        self.closed = 0
        self.velocity = (0, 1466, 429416)
        self.home_params = (2, 1, 42941, 0)
        self.complete_immediately = True
        self.no_response = False

    def _code(self, name, *args):
        self.calls.append((name, *args))
        return self.codes.get(name, 0)

    def FT_OpenEx(self, serial, flags, handle):
        result = self._code("FT_OpenEx", serial.value.decode(), flags)
        if result == 0:
            handle._obj.value = 99
        return result

    def FT_Close(self, handle):
        self.closed += 1
        return self._code("FT_Close")

    def FT_SetBaudRate(self, handle, value):
        return self._code("FT_SetBaudRate", value)

    def FT_SetDataCharacteristics(self, handle, *values):
        return self._code("FT_SetDataCharacteristics", *values)

    def FT_SetFlowControl(self, handle, *values):
        return self._code("FT_SetFlowControl", *values)

    def FT_SetRts(self, handle):
        return self._code("FT_SetRts")

    def FT_SetTimeouts(self, handle, *values):
        return self._code("FT_SetTimeouts", *values)

    def FT_GetQueueStatus(self, handle, count):
        count._obj.value = len(self.queue)
        return self._code("FT_GetQueueStatus")

    def _identity(self):
        value = bytearray(84)
        struct.pack_into("<I", value, 0, int(self.serial))
        value[4:12] = self.model.encode().ljust(8, b"\0")
        struct.pack_into("<H", value, 12, 27)
        struct.pack_into("<I", value, 14, 0x01020304)
        struct.pack_into("<H", value, 82, self.channels)
        return bytes(value)

    def _complete(self, message):
        self.status &= ~0x2F0
        if message == 0x0444:
            self.status |= 0x400
            self.position = 0
            return short_reply(message)
        return payload_reply(message, struct.pack("<HiHHI", 1, self.position, 0, 0, self.status))

    def FT_Write(self, handle, buffer, size, count):
        result = self._code("FT_Write", size)
        data = ct.string_at(buffer, size)
        self.writes.append(data)
        count._obj.value = size - 1 if self.write_short else size
        if result or self.write_short or self.no_response:
            return result
        message = int.from_bytes(data[:2], "little")
        response = b""
        if message == 0x0005:
            response = payload_reply(0x0006, self._identity())
        elif message == 0x0429:
            response = payload_reply(0x042A, struct.pack("<HI", 1, self.status))
        elif message == 0x0411:
            response = payload_reply(0x0412, struct.pack("<Hi", 1, self.position))
        elif message == 0x0211:
            response = short_reply(0x0212, 1, 1 if self.status & 0x80000000 else 2)
        elif message == 0x0210:
            if not self.ignore_enable:
                self.status |= 0x80000000
        elif message == 0x0414:
            response = payload_reply(0x0415, struct.pack("<Hiii", 1, *self.velocity))
        elif message == 0x0441:
            response = payload_reply(0x0442, struct.pack("<HHHIi", 1, *self.home_params))
        elif message in (0x0443, 0x0453, 0x0465):
            if message == 0x0453:
                self.position = struct.unpack_from("<i", data, 8)[0]
                self.status |= 0x10
                completion = 0x0464
            elif message == 0x0443:
                self.status |= 0x200
                completion = 0x0444
            else:
                completion = 0x0466
            if self.complete_immediately:
                response = self._complete(completion)
        else:
            raise AssertionError(f"Unexpected fake command {data.hex()}")
        if self.response_transform:
            response = self.response_transform(message, response)
        self.queue.extend(response)
        return result

    def FT_Read(self, handle, buffer, size, count):
        result = self._code("FT_Read", size)
        take = min(len(self.queue), size - 1 if self.read_short else size)
        data = bytes(self.queue[:take])
        del self.queue[:take]
        ct.memmove(buffer, data, take)
        count._obj.value = take
        return result


def device(**changes):
    fake = FakeD2xx()
    config = replace(RotatorDeviceConfig(role="rotator_1", serial_number="27000001"), **changes)
    return Kdc101Device(config, library=fake), fake


def command_ids(fake):
    return [int.from_bytes(value[:2], "little") for value in fake.writes]


class Kdc101DriverTests(unittest.TestCase):
    def test_observed_homing_active_bit_is_motion_and_cannot_qualify_ready(self):
        state = decode_status_bits(0xB0300324)
        self.assertTrue(state["active"])
        self.assertTrue(state["homing"])
        self.assertTrue(state["moving"])
        self.assertFalse(state["homed"])
        self.assertFalse(state["fault"])
        driver, fake = device()
        fake.status = 0xB0300500  # ACTIVE remains motion even without direction bits.
        driver.open()
        self.assertTrue(driver.read_state()["moving"])
        with self.assertRaisesRegex(Kdc101Error, "healthy and stationary"):
            driver.prepare()
        self.assertNotIn(0x0210, command_ids(fake))
        self.assertNotIn(0x0443, command_ids(fake))
        self.assertNotIn(0x0453, command_ids(fake))

    def test_observed_power_indication_is_preserved_without_qualifying_home(self):
        # Actual KDC101 reply: generic APT POWEROK bit is informational only.
        flags = decode_status_bits(0x90300104)
        self.assertTrue(flags["power_ok_indication"])
        self.assertTrue(flags["connected"])
        self.assertTrue(flags["enabled"])
        self.assertFalse(flags["homed"])
        self.assertFalse(flags["moving"])
        self.assertFalse(flags["fault"])
        self.assertEqual(flags["raw_status_bits"], 0x90300104)

    def test_power_indication_cannot_mask_motor_fault_or_unknown_bits(self):
        for fault in (0x00004000, 0x40000000):
            with self.subTest(fault=fault):
                state = decode_status_bits(0x90300104 | fault)
                self.assertTrue(state["fault"])
        with self.assertRaisesRegex(Kdc101Error, "Unknown"):
            decode_status_bits(0x90300104 | 0x00000800)
        self.assertIn("motor_not_connected", decode_status_bits(0x90000400)["faults"])

    def test_constructor_is_passive_and_open_only_identifies(self):
        with patch("ctypes.CDLL", side_effect=AssertionError("No real DLL")):
            driver, fake = device()
            self.assertEqual(fake.calls, [])
            identity = driver.open()
        self.assertEqual(identity["serial_number"], "27000001")
        self.assertEqual(command_ids(fake), [0x0005])
        self.assertIn(("FT_OpenEx", "27000001", 1), fake.calls)
        self.assertIn(("FT_SetDataCharacteristics", 8, 0, 0), fake.calls)
        self.assertFalse(any("Purge" in call[0] or "Reset" in call[0] for call in fake.calls))
        driver.close()
        driver.close()
        self.assertEqual(fake.closed, 1)

    def test_identity_serial_model_channel_mismatch_closes(self):
        for field, value in (("serial", "27000002"), ("model", "TDC001"), ("channels", 2)):
            with self.subTest(field=field):
                driver, fake = device()
                setattr(fake, field, value)
                with self.assertRaisesRegex(Kdc101Error, "identity mismatch"):
                    driver.open()
                self.assertEqual(fake.closed, 1)
                self.assertFalse(driver.opened)

    def test_declared_stage_and_explicit_serial_are_required(self):
        for changes in ({"serial_number": "CHANGE_ME"}, {"stage_model": "PRM1/M"}):
            with self.subTest(changes=changes), self.assertRaises(Exception):
                device(**changes)

    def test_fresh_state_is_three_correlated_requests_not_cached(self):
        driver, fake = device()
        driver.open()
        fake.position = 191964
        before = len(fake.writes)
        state = driver.read_state()
        self.assertEqual(command_ids(fake)[before:], [0x0429, 0x0411, 0x0429])
        self.assertAlmostEqual(state["angle_deg"], 191964 / PRM1Z8_COUNTS_PER_DEGREE)
        self.assertEqual(state["position_counts"], 191964)
        self.assertEqual(state["request_sequences"], [2, 3, 4])
        self.assertTrue(state["fresh"])
        fake.position = -191964
        self.assertLess(driver.read_state()["angle_deg"], 0)
        self.assertIn("operator-declared", state["stage_identity_source"])

    def test_prepare_only_enables_and_retains_motion_parameters(self):
        driver, fake = device()
        fake.status &= ~0x80000000
        driver.open()
        state = driver.prepare()
        self.assertTrue(state["enabled"])
        self.assertTrue(driver.prepared)
        ids = command_ids(fake)
        self.assertEqual(ids.count(0x0210), 1)
        self.assertEqual(set(ids), {0x0005, 0x0429, 0x0411, 0x0414, 0x0441, 0x0210, 0x0211})
        self.assertTrue(any("persisted_motion_parameters" in row for row in driver.transcripts))
        before = ids.count(0x0210)
        driver.prepare()
        self.assertEqual(command_ids(fake).count(0x0210), before)

    def test_enable_readback_must_match(self):
        driver, fake = device()
        fake.status &= ~0x80000000
        fake.ignore_enable = True
        driver.open()
        with self.assertRaisesRegex(Kdc101Error, "enable readback"):
            driver.prepare()
        self.assertFalse(driver.prepared)

    def test_unsafe_persisted_parameters_block_before_enable_or_motion(self):
        for field, values in (("velocity", (0, 1466, 2_000_000)),
                              ("velocity", (1, 1466, 429416)),
                              ("velocity", (0, 0, 429416)),
                              ("home_params", (3, 1, 42941, 0)),
                              ("home_params", (2, 2, 42941, 0)),
                              ("home_params", (2, 1, 0, 0)),
                              ("home_params", (2, 1, 42941, -1))):
            with self.subTest(field=field, values=values):
                driver, fake = device()
                setattr(fake, field, values)
                driver.open()
                with self.assertRaises(Kdc101Error):
                    driver.prepare()
                self.assertNotIn(0x0210, command_ids(fake))
                self.assertNotIn(0x0453, command_ids(fake))

    def test_move_has_exact_native_count_and_completion_never_replaces_readback(self):
        driver, fake = device()
        driver.open()
        driver.prepare()
        driver.move_to(90)
        expected = struct.pack("<HHBBHi", 0x0453, 6, 0xD0, 1, 1, round(90 * PRM1Z8_COUNTS_PER_DEGREE))
        self.assertIn(expected, fake.writes)
        before = len(fake.writes)
        state = driver.read_state()
        self.assertFalse(state["moving"])
        self.assertEqual(command_ids(fake)[before:], [0x0429, 0x0411, 0x0429])
        self.assertAlmostEqual(state["angle_deg"], 90, places=3)
        self.assertTrue(any(row.get("message_id") == 0x0464 for row in driver.transcripts))

    def test_home_is_explicit_and_absolute_moves_require_homed(self):
        driver, fake = device()
        fake.status &= ~0x400
        driver.open()
        with self.assertRaisesRegex(Kdc101Error, "Prepare"):
            driver.home()
        driver.prepare()
        self.assertNotIn(0x0443, command_ids(fake))
        with self.assertRaisesRegex(Kdc101Error, "homed"):
            driver.move_to(10)
        driver.home()
        state = driver.read_state()
        self.assertTrue(state["homed"])
        self.assertFalse(state["moving"])
        self.assertIn(bytes.fromhex("430401005001"), fake.writes)
        self.assertNotIn(0x0410, command_ids(fake))

    def test_missing_completion_keeps_motion_pending_despite_stationary_bits(self):
        driver, fake = device()
        driver.open()
        driver.prepare()
        fake.complete_immediately = False
        driver.move_to(10)
        fake.status &= ~0x2F0
        state = driver.read_state()
        self.assertTrue(state["moving"])
        self.assertEqual(state["motion_pending"], "move")
        with self.assertRaisesRegex(Kdc101Error, "stationary"):
            driver.move_to(20)

    def test_invalid_angles_never_send_a_move(self):
        driver, fake = device()
        driver.open()
        driver.prepare()
        for value in (-1, 361, float("nan"), float("inf"), True):
            with self.subTest(value=value), self.assertRaises(Kdc101Error):
                driver.move_to(value)
        self.assertNotIn(0x0453, command_ids(fake))

    def test_boundaries_quantize_inward_and_preserve_actual_encoder_position(self):
        driver, fake = device()
        driver.open()
        driver.prepare()
        for target in (0, 360, 0):
            driver.move_to(target)
            state = driver.read_state()
            self.assertGreaterEqual(state["angle_deg"], 0)
            self.assertLessEqual(state["angle_deg"], 360)
            self.assertLess(abs(target - state["angle_deg"]), 1 / PRM1Z8_COUNTS_PER_DEGREE)
        entries = [row for row in driver.transcripts if row.get("requested_angle_deg") == 360]
        self.assertEqual(len(entries), 1)
        self.assertLessEqual(entries[0]["commanded_angle_deg"], 360)

    def test_quantization_does_not_relax_tighter_requested_tolerance(self):
        driver, fake = device(position_tolerance_deg=1e-10)
        driver.open()
        driver.prepare()
        with self.assertRaisesRegex(Kdc101Error, "quantization"):
            driver.move_to(360)
        self.assertNotIn(0x0453, command_ids(fake))

    def test_status_bracket_preserves_motion_and_homing_uncertainty(self):
        driver, fake = device()
        driver.open()
        n = 0
        def transform(message, reply):
            nonlocal n
            if message == 0x0429:
                n += 1
                return payload_reply(0x042A, struct.pack("<HI", 1, 0x80000510 if n == 1 else 0x80000500))
            return reply
        fake.response_transform = transform
        self.assertTrue(driver.read_state()["moving"])

    def test_native_faults_remain_visible_and_block_prepare(self):
        for mask in (0x4000, 0x40000000):
            with self.subTest(mask=mask):
                driver, fake = device()
                fake.status |= mask
                driver.open()
                self.assertTrue(driver.read_state()["fault"])
                with self.assertRaisesRegex(Kdc101Error, "healthy"):
                    driver.prepare()

    def test_unknown_or_contradictory_status_poison_owner(self):
        for mask in (0x800, 0x01000000, 0x30, 0x90):
            with self.subTest(mask=mask):
                driver, fake = device()
                fake.status |= mask
                driver.open()
                with self.assertRaises(Kdc101Error):
                    driver.read_state()
                self.assertTrue(driver.poisoned)
                before = len(fake.writes)
                with self.assertRaisesRegex(Kdc101Error, "uncertain"):
                    driver.read_state()
                self.assertEqual(before, len(fake.writes))
                driver.stop()
                self.assertEqual(fake.writes[-1], bytes.fromhex("650401025001"))

    def test_wrong_channel_address_payload_or_message_poison_without_retry(self):
        changes = (
            lambda req, reply: payload_reply(0x042A, struct.pack("<HI", 2, 0x80000500)),
            lambda req, reply: payload_reply(0x042A, struct.pack("<HI", 1, 0x80000500), source=0x22),
            lambda req, reply: payload_reply(0x042A, struct.pack("<HI", 1, 0x80000500), destination=0x82),
            lambda req, reply: payload_reply(0x042A, b"\1\0"),
            lambda req, reply: payload_reply(0x0412, struct.pack("<Hi", 1, 0)),
        )
        for transform in changes:
            with self.subTest(transform=transform):
                driver, fake = device()
                driver.open()
                fake.response_transform = transform
                before = len(fake.writes)
                with self.assertRaises(Kdc101Error):
                    driver.read_state()
                self.assertTrue(driver.poisoned)
                self.assertEqual(len(fake.writes), before + 1)

    def test_unowned_late_response_is_not_cleared_or_used(self):
        driver, fake = device()
        driver.open()
        fake.queue.extend(payload_reply(0x042A, struct.pack("<HI", 1, 0x80000500)))
        before = len(fake.writes)
        with self.assertRaisesRegex(Kdc101Error, "Unexpected/late"):
            driver.read_state()
        self.assertEqual(before, len(fake.writes))
        self.assertTrue(driver.poisoned)

    def test_partial_read_timeout_and_d2xx_error_keep_last_confirmed_state(self):
        for failure in ("partial", "timeout", "error"):
            with self.subTest(failure=failure):
                driver, fake = device()
                driver.open()
                old = driver.read_state()
                if failure == "partial":
                    fake.read_short = True
                elif failure == "timeout":
                    fake.no_response = True
                else:
                    fake.codes["FT_Read"] = 4
                with self.assertRaises(Kdc101Error):
                    driver.read_state()
                self.assertEqual(driver.last_state, old)
                self.assertTrue(driver.poisoned)
                driver.close()
                self.assertEqual(fake.closed, 1)

    def test_partial_write_is_not_retried(self):
        driver, fake = device()
        driver.open()
        fake.write_short = True
        before = len(fake.writes)
        with self.assertRaisesRegex(Kdc101Error, "Partial APT write"):
            driver.read_state()
        self.assertEqual(len(fake.writes), before + 1)

    def test_hardware_error_frame_is_preserved_and_stops_owner(self):
        driver, fake = device()
        driver.open()
        fake.response_transform = lambda req, reply: short_reply(0x0080, 4, 0)
        with self.assertRaisesRegex(Kdc101Error, "hardware error"):
            driver.read_state()
        self.assertTrue(any(row.get("message_id") == 0x0080 for row in driver.transcripts))

    def test_open_rejects_preexisting_data_without_purge(self):
        driver, fake = device()
        fake.queue.extend(b"stale")
        with self.assertRaisesRegex(Kdc101Error, "Unread USB"):
            driver.open()
        self.assertEqual(fake.writes, [])
        self.assertEqual(fake.closed, 1)

    def test_each_transport_return_code_is_checked(self):
        for function in ("FT_OpenEx", "FT_SetBaudRate", "FT_SetDataCharacteristics",
                         "FT_SetFlowControl", "FT_SetRts", "FT_SetTimeouts", "FT_GetQueueStatus"):
            with self.subTest(function=function):
                driver, fake = device()
                fake.codes[function] = 4
                with self.assertRaisesRegex(Kdc101Error, function):
                    driver.open()
                self.assertFalse(driver.opened)

    def test_early_open_or_signature_failure_releases_dll_search_owner(self):
        for missing in (False, True):
            with self.subTest(missing=missing):
                fake = FakeD2xx()
                if missing:
                    fake.FT_Read = None
                    # Model an absent exported symbol, not a callable that fails later.
                    class MissingLibrary:
                        def __getattr__(self, name):
                            if name == "FT_Read":
                                raise AttributeError(name)
                            return getattr(fake, name)
                    library = MissingLibrary()
                else:
                    fake.codes["FT_OpenEx"] = 4
                    library = fake
                transport = D2xxTransport(library=library)
                class SearchOwner:
                    closed = 0
                    def close(self):
                        self.closed += 1
                owner = SearchOwner()
                transport._dll_search = owner
                driver = Kdc101Device(RotatorDeviceConfig(role="rotator_1", serial_number="27000001"),
                                      transport=transport)
                with self.assertRaises(Kdc101Error):
                    driver.open()
                self.assertEqual(owner.closed, 1)
                self.assertIsNone(transport._dll_search)
                self.assertFalse(driver.opened)

    def test_close_failure_propagates(self):
        driver, fake = device()
        driver.open()
        fake.codes["FT_Close"] = 4
        with self.assertRaisesRegex(Kdc101Error, "FT_Close"):
            driver.close()
        self.assertFalse(driver.opened)

    def test_status_decoder_covers_home_limits_and_missing_motor(self):
        self.assertFalse(decode_status_bits(0x80000703)["fault"])
        self.assertTrue(decode_status_bits(0x80000703)["homing"])
        self.assertEqual(decode_status_bits(0x80000400)["faults"], ["motor_not_connected"])
        for value in (-1, 2 ** 32, True):
            with self.subTest(value=value), self.assertRaises(Kdc101Error):
                decode_status_bits(value)

    def test_manufacturer_25_degree_velocity_limit_uses_integer_protocol_encoding(self):
        from attodry_control.thorlabs_kdc101 import _VELOCITY_SCALE
        driver, fake = device()
        maximum = round(25 * _VELOCITY_SCALE)
        fake.velocity = (0, 1466, maximum)
        driver.open()
        driver.prepare()
        fake.velocity = (0, 1466, maximum + 1)
        with self.assertRaisesRegex(Kdc101Error, "velocity"):
            driver.move_to(10)
        self.assertNotIn(0x0453, command_ids(fake))

    def test_full_rotator_session_accepts_inward_quantized_360_and_zero(self):
        from attodry_control.rotator_config import RotatorConfig
        from attodry_control.rotator_points import RotatorPointSession
        class Clock:
            value = 0.0
            def __call__(self):
                return self.value
            def sleep(self, delay):
                self.value += delay
        clock, fake = Clock(), FakeD2xx()
        config = RotatorDeviceConfig(role="rotator_1", serial_number="27000001", settle_s=.1)
        points = ({"rotator_1_angle_deg": 360.0}, {"rotator_1_angle_deg": 0.0})
        session = RotatorPointSession(RotatorConfig((config,), points),
            device_factory=lambda cfg: Kdc101Device(cfg, library=fake, clock=clock),
            clock=clock, sleep=clock.sleep)
        session.preflight()
        session.begin(guard=lambda: None)
        for target in points:
            session.set_point(target, guard=lambda: None)
            actual = session.verify()["rotator_1_angle_deg"]
            self.assertTrue(0 <= actual <= 360)
            self.assertLess(abs(actual - target["rotator_1_angle_deg"]), .1)
        self.assertTrue(session.cleanup()["verified"])
        self.assertEqual(session.close(), [])
        self.assertEqual(fake.closed, 1)

    def test_streamed_audit_keeps_bounded_diagnostic_tail_without_extra_usb(self):
        audit, streamed_fake = [], FakeD2xx()
        config = RotatorDeviceConfig(role="rotator_1", serial_number="27000001")
        streamed = Kdc101Device(config, library=streamed_fake, on_event=audit.append)
        standalone, standalone_fake = device()
        for driver in (streamed, standalone):
            driver.open()
            for _ in range(75):
                driver.read_state()
        self.assertEqual(command_ids(streamed_fake), command_ids(standalone_fake))
        self.assertEqual(len(audit), len(standalone.transcripts))
        self.assertGreater(len(audit), 128)
        self.assertEqual(streamed.transcripts, audit[-128:])
        for expected, actual in zip(standalone.transcripts, audit):
            self.assertEqual({k: v for k, v in expected.items() if k != "utc"},
                             {k: v for k, v in actual.items() if k != "utc"})
        audit[-1]["raw_hex"] = "callback mutation"
        self.assertNotEqual(streamed.transcripts[-1]["raw_hex"], "callback mutation")
        streamed._record(nested={"value": 1})
        audit[-1]["nested"]["value"] = 2
        self.assertEqual(streamed.transcripts[-1]["nested"]["value"], 1)

    def test_streamed_audit_failure_blocks_command_and_poisons_owner(self):
        fake = FakeD2xx()
        config = RotatorDeviceConfig(role="rotator_1", serial_number="27000001")
        driver = Kdc101Device(config, library=fake, on_event=lambda event: None)
        driver.open()
        before = len(fake.writes)
        def fail(event):
            raise RuntimeError("audit unavailable")
        driver.on_event = fail
        with self.assertRaisesRegex(RuntimeError, "audit unavailable"):
            driver.read_state()
        self.assertEqual(len(fake.writes), before)
        self.assertTrue(driver.poisoned)


if __name__ == "__main__":
    unittest.main()
