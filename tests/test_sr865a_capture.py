"""Injected SR865A capture fixtures; no VISA import or real resource opening."""
from dataclasses import asdict
import math
import struct
import unittest

from attodry_control.lockin_backend import create_lockin_backend
from attodry_control.models import LockinRole
from attodry_control.sr865a import (
    AuthorizationRequired, Sr865a, Sr865aCaptureError, Sr865aError,
)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class CaptureResource:
    resource_name = "GPIB0::8::INSTR"

    def __init__(self, clock):
        self.clock = clock
        self.responses = {
            "*IDN?": "Stanford_Research_Systems,SR865A,synthetic,V1.51",
            "RSRC?": "0", "HARM?": "1", "FREQINT?": "50000",
            "FREQDET?": "50000", "FREQEXT?": "50027", "IVMD?": "0",
            "CAPTURERATEMAX?": "1250000", "CAPTURECFG?": "0",
            "CAPTURELEN?": "8",
        }
        self.divider = 12
        self.events = []
        self.binary_calls = []
        self.start = None
        self.stopped = False
        self.byte_count = None
        self.active_before = False
        self.hold_stop = False
        self.fail_start = False
        self.bad_block = None
        self.bad_values = False
        self.ignore_setting = None
        self.fail_restore = False
        self.closed = False
        self.io_timeouts = []
        self.finish_after = None
        self.binary_error = None

    def rate(self):
        return float(self.responses["CAPTURERATEMAX?"]) / 2 ** self.divider

    def acquired(self):
        if self.byte_count is not None:
            return self.byte_count
        if self.start is None:
            return 0
        return min(int((self.clock() - self.start) * self.rate()) * 8,
                   int(self.responses["CAPTURELEN?"]) * 1024)

    def query(self, command):
        self.events.append(("query", command))
        self.io_timeouts.append((command, getattr(self, "timeout", None)))
        if command == "CAPTURESTAT?":
            if self.active_before:
                return "3"
            if self.start is None:
                return "0"
            if self.finish_after is not None and self.clock() - self.start >= self.finish_after:
                self.byte_count = int(self.responses["CAPTURELEN?"]) * 1024
                self.stopped = True
            full = self.acquired() >= int(self.responses["CAPTURELEN?"]) * 1024
            if (self.stopped and not self.hold_stop) or full:
                return "6" if full else "2"
            return "3"
        if command == "CAPTUREBYTES?":
            return str(self.acquired())
        if command == "CAPTURERATE?":
            return str(self.rate())
        result = self.responses[command]
        if isinstance(result, BaseException):
            raise result
        return result

    def write(self, command):
        self.events.append(("write", command))
        self.io_timeouts.append((command, getattr(self, "timeout", None)))
        if command == "CAPTURESTART 0,0":
            self.start = self.clock()
            self.stopped = False
            if self.fail_start:
                raise TimeoutError("START acknowledgement lost")
            return
        if command == "CAPTURESTOP":
            if self.byte_count is None:
                self.byte_count = self.acquired()
            self.stopped = True
            return
        name, value = command.split(" ", 1)
        if self.fail_restore and self.stopped and name.startswith("CAPTURE"):
            raise OSError("capture restore failed")
        if name == self.ignore_setting:
            return
        if name == "CAPTURERATE":
            self.divider = int(value)
        elif name == "FREQINT":
            requested = float(value)
            quantum = max(0.0001, 10 ** (math.floor(math.log10(requested)) - 5))
            actual = round(requested / quantum) * quantum
            self.responses["FREQINT?"] = str(actual)
            if self.responses["RSRC?"] == "0":
                self.responses["FREQDET?"] = str(actual * int(self.responses["HARM?"]))
        else:
            self.responses[name + "?"] = value
            if name == "RSRC" and value == "0":
                self.responses["FREQDET?"] = str(float(self.responses["FREQINT?"]) *
                                                   int(self.responses["HARM?"]))

    def query_binary_values(self, command, **kwargs):
        self.events.append(("query_binary", command))
        self.io_timeouts.append((command, getattr(self, "timeout", None)))
        self.binary_calls.append((command, kwargs))
        if self.binary_error is not None:
            raise self.binary_error
        offset, length = (int(value) for value in command.split(" ", 1)[1].split(","))
        if self.bad_block == offset:
            return [0.0] * (length * 256 - 1)
        pairs = [(offset * 128 + index) * 1e-6 for index in range(length * 128)]
        values = [value for x in pairs for value in (x, 2e-6)]
        if self.bad_values:
            values[0] = float("nan")
        # Match what a PyVISA IEEE little-endian float32 decoder returns.
        return list(struct.unpack("<" + "f" * len(values),
                                 struct.pack("<" + "f" * len(values), *values)))

    def close(self):
        self.closed = True


class InternalFrequencyTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.resource = CaptureResource(self.clock)
        self.driver = Sr865a(self.resource, LockinRole.XY)

    def test_frequency_permission_and_invalid_arguments_have_zero_io(self):
        for value in (False, "50000", math.nan, math.inf, 0, 4_000_000):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.driver.set_internal_frequency(value, authorized=True)
        with self.assertRaises(AuthorizationRequired):
            self.driver.set_internal_frequency(50_000)
        self.assertEqual(self.resource.events, [])

    def test_identity_and_internal_mode_are_required(self):
        with self.assertRaises(Sr865aError):
            self.driver.set_internal_frequency(50_000, authorized=True)
        self.assertEqual(self.resource.events, [])
        self.driver.query_identity()
        self.resource.responses["RSRC?"] = "1"
        with self.assertRaisesRegex(Sr865aError, "internal reference"):
            self.driver.set_internal_frequency(50_000, authorized=True)
        self.assertFalse(any(kind == "write" for kind, _ in self.resource.events))

    def test_documented_six_digit_rounding_is_not_arbitrary_hz_tolerance(self):
        self.driver.query_identity()
        actual = self.driver.set_internal_frequency(50_027.755, authorized=True)
        self.assertAlmostEqual(actual, 50_027.8)
        self.assertIn(("write", "FREQINT 50027.755"), self.resource.events)
        self.resource.ignore_setting = "FREQINT"
        with self.assertRaisesRegex(Sr865aError, "rounding"):
            self.driver.set_internal_frequency(50_027.9, authorized=True)

    def test_harmonic_limit_prevents_frequency_write(self):
        self.driver.query_identity()
        self.resource.responses["HARM?"] = "2"
        with self.assertRaisesRegex(ValueError, "4 MHz"):
            self.driver.set_internal_frequency(2_000_000, authorized=True)
        self.assertFalse(any(kind == "write" for kind, _ in self.resource.events))

    def test_detection_frequency_is_verified_even_without_frequency_write(self):
        self.driver.query_identity()
        self.resource.responses["FREQDET?"] = "49999"
        with self.assertRaisesRegex(Sr865aError, "detection frequency"):
            self.driver.set_internal_frequency(50_000, authorized=True)

    def test_unused_frequency_restore_authorization_and_boundaries_are_zero_io(self):
        with self.assertRaises(AuthorizationRequired):
            self.driver.restore_stored_internal_frequency(1000)
        for invalid in (0, 0.0009, 4_000_000.01, True, math.nan, math.inf, "1000"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.driver.restore_stored_internal_frequency(invalid, authorized=True)
        self.assertEqual(self.resource.events, [])

    def test_unused_frequency_restore_requires_external_and_never_switches_source(self):
        self.driver.query_identity()
        with self.assertRaisesRegex(Sr865aError, "external"):
            self.driver.restore_stored_internal_frequency(1000, authorized=True)
        self.assertFalse(any(kind == "write" for kind, _ in self.resource.events))
        self.resource.responses.update({"RSRC?": "1", "HARM?": "2", "FREQDET?": "100054"})
        self.assertEqual(self.driver.restore_stored_internal_frequency(1000, authorized=True), 1000)
        self.assertEqual(self.resource.responses["FREQDET?"], "100054")
        self.assertEqual(self.resource.responses["FREQEXT?"], "50027")
        self.assertEqual(self.resource.responses["RSRC?"], "1")
        self.assertEqual([command for kind, command in self.resource.events if kind == "write"], ["FREQINT 1000"])

    def test_unused_frequency_restore_accepts_four_mhz_storage_without_detecting_it(self):
        self.driver.query_identity()
        self.resource.responses["RSRC?"] = "1"
        self.assertEqual(self.driver.restore_stored_internal_frequency(4_000_000, authorized=True), 4_000_000)
        self.assertEqual(self.resource.responses["FREQDET?"], "50000")

    def test_unused_frequency_restore_verifies_readback(self):
        self.driver.query_identity()
        self.resource.responses["RSRC?"] = "1"
        self.resource.ignore_setting = "FREQINT"
        with self.assertRaisesRegex(Sr865aError, "rounding"):
            self.driver.restore_stored_internal_frequency(1000, authorized=True)


class BufferedCaptureTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.resource = CaptureResource(self.clock)
        self.driver = Sr865a(self.resource, LockinRole.XY)

    def capture(self, duration=2.1, rate=1000, **kwargs):
        self.driver.query_identity()
        return self.driver.capture_xy(duration, rate, timeout_s=duration + 10,
            authorized=True, clock=self.clock, sleep=self.clock.sleep, **kwargs)

    def test_unauthorized_capture_and_static_boundaries_perform_no_io(self):
        with self.assertRaises(AuthorizationRequired):
            self.driver.capture_xy(30, 1000, timeout_s=40)
        for duration, rate, timeout in ((0, 1000, 40), (30, 0, 40),
                                         (30, 1000, 30), (5000, 1000, 5001),
                                         (True, 1000, 40), (30, math.inf, 40)):
            with self.subTest(duration=duration, rate=rate), self.assertRaises(ValueError):
                self.driver.capture_xy(duration, rate, timeout_s=timeout, authorized=True)
        self.assertEqual(self.resource.events, [])

    def test_identity_and_binary_capability_precede_any_capture_io(self):
        with self.assertRaisesRegex(Sr865aError, "identity"):
            self.driver.capture_xy(1, 1000, timeout_s=10, authorized=True)
        self.assertEqual(self.resource.events, [])
        self.driver.query_identity()
        self.resource.query_binary_values = None
        before = list(self.resource.events)
        with self.assertRaisesRegex(Sr865aError, "binary-capable"):
            self.driver.capture_xy(1, 1000, timeout_s=10, authorized=True)
        self.assertEqual(self.resource.events, before)

    def test_rs232_is_rejected_without_capture_settings_or_start(self):
        self.resource.resource_name = "ASRL8::INSTR"
        with self.assertRaisesRegex(Sr865aError, "RS-232"):
            self.capture()
        self.assertEqual(self.resource.events, [("query", "*IDN?")])

    def test_existing_active_capture_is_not_stopped_or_reconfigured(self):
        self.resource.active_before = True
        with self.assertRaises(Sr865aCaptureError) as caught:
            self.capture()
        self.assertEqual(caught.exception.result.x_v, ())
        self.assertFalse(any(kind == "write" for kind, _ in self.resource.events))
        self.assertEqual(caught.exception.result.cleanup_errors, ())

    def test_capture_keeps_rate_xy_time_axis_padding_and_restores_own_settings(self):
        guards = []
        result = self.capture(on_poll=lambda: guards.append(self.clock()))
        self.assertTrue(result.completed)
        self.assertEqual(result.rate_divider, 10)
        self.assertAlmostEqual(result.actual_rate_hz, 1220.703125)
        self.assertEqual(result.valid_data_bytes, len(result.x_v) * 8)
        self.assertEqual(result.t_s[1], 1 / result.actual_rate_hz)
        self.assertAlmostEqual(result.r_v[0], 2e-6)
        self.assertEqual(result.downloaded_bytes, result.valid_data_bytes + result.padding_bytes)
        self.assertEqual(result.final_capture_status, 2)
        self.assertIsNotNone(result.started_at_utc)
        self.assertIsNotNone(result.stopped_at_utc)
        self.assertIn("host bounds", result.time_axis_source)
        self.assertEqual((self.resource.responses["CAPTURECFG?"],
                          self.resource.responses["CAPTURELEN?"], self.resource.divider), ("0", "8", 12))
        self.assertGreaterEqual(len(guards), 7)
        self.assertTrue(any(0.99 <= t <= 1.1 for t in guards))
        binary = self.resource.binary_calls[0][1]
        self.assertEqual(binary, dict(datatype="f", is_big_endian=False,
                                     header_fmt="ieee", expect_termination=False, container=list))
        capture_raw = [record for record in result.raw if record.command.startswith("CAPTUREGET")]
        self.assertEqual(len(bytes.fromhex(capture_raw[0].response)), result.downloaded_bytes)
        self.assertEqual(len(asdict(result)["x_v"]), len(result.x_v))

    def test_download_blocks_do_not_exceed_64_kib_or_requested_buffer(self):
        result = self.capture(duration=10)
        self.assertTrue(result.completed)
        calls = [call[0] for call in self.resource.binary_calls]
        self.assertEqual(calls[0], "CAPTUREGET? 0,64")
        self.assertEqual(len(calls), 2)
        for command in calls:
            offset, size = map(int, command.split(" ")[1].split(","))
            self.assertLessEqual(size, 64)
            self.assertLessEqual(offset + size, result.buffer_length_kib)

    def test_guard_fault_stops_owned_capture_and_keeps_true_partial_data(self):
        def guard():
            if self.clock() >= 1:
                raise ValueError("power guard failed")
        with self.assertRaises(Sr865aCaptureError) as caught:
            self.capture(duration=3, on_poll=guard)
        result = caught.exception.result
        self.assertFalse(result.completed)
        self.assertIn("power guard failed", result.error)
        self.assertGreater(len(result.x_v), 0)
        self.assertLess(len(result.x_v), math.ceil(3 * result.actual_rate_hz))
        self.assertEqual(result.cleanup_errors, ())
        self.assertTrue(self.resource.stopped)

    def test_stop_timeout_is_bounded_and_never_fakes_download_or_restore(self):
        self.resource.hold_stop = True
        with self.assertRaises(Sr865aCaptureError) as caught:
            self.capture(duration=0.1)
        result = caught.exception.result
        self.assertFalse(result.completed)
        self.assertTrue(any("capture stop" in error for error in result.cleanup_errors))
        self.assertTrue(any("configuration restore" in error for error in result.cleanup_errors))
        self.assertEqual(result.x_v, ())
        self.assertEqual(self.resource.binary_calls, [])
        self.assertLessEqual(self.clock(), 10.2)
        self.assertIsNone(result.stopped_at_utc)

    def test_lost_start_acknowledgement_still_attempts_owned_stop(self):
        self.resource.fail_start = True
        with self.assertRaises(Sr865aCaptureError) as caught:
            self.capture()
        self.assertIn(("write", "CAPTURESTOP"), self.resource.events)
        self.assertFalse(caught.exception.result.completed)
        self.assertEqual(caught.exception.result.valid_data_bytes, 0)

    def test_bad_block_length_keeps_preceding_complete_block_only(self):
        self.resource.bad_block = 64
        with self.assertRaises(Sr865aCaptureError) as caught:
            self.capture(duration=10)
        result = caught.exception.result
        self.assertIn("block length", result.error)
        self.assertEqual(len(result.x_v), 64 * 128)
        self.assertEqual(len(result.y_v), len(result.x_v))
        self.assertTrue(any(record.command == "CAPTUREGET? 64,32" for record in result.raw))

    def test_nonfinite_capture_payload_remains_raw_and_is_not_formal_data(self):
        self.resource.bad_values = True
        with self.assertRaises(Sr865aCaptureError) as caught:
            self.capture()
        result = caught.exception.result
        self.assertIn("Non-finite", result.error)
        self.assertEqual(result.x_v, ())
        record = next(record for record in result.raw if record.command.startswith("CAPTUREGET"))
        self.assertTrue(math.isnan(struct.unpack("<f", bytes.fromhex(record.response)[:4])[0]))

    def test_invalid_valid_byte_counts_are_not_downloaded(self):
        for count in (-1, 7, 1000000):
            self.setUp()
            self.resource.byte_count = count
            with self.subTest(count=count), self.assertRaises(Sr865aCaptureError) as caught:
                self.capture(duration=0.1)
            self.assertIn("byte count", caught.exception.result.error)
            self.assertEqual(self.resource.binary_calls, [])

    def test_runtime_rate_and_memory_limits_reject_before_settings_writes(self):
        for duration, rate in ((0.1, 2_000_000), (450, 1000)):
            self.setUp()
            with self.subTest(duration=duration), self.assertRaises(Sr865aCaptureError):
                self.capture(duration=duration, rate=rate)
            self.assertFalse(any(kind == "write" for kind, _ in self.resource.events))

    def test_restore_failure_is_a_failed_capture_with_retained_data(self):
        self.resource.fail_restore = True
        with self.assertRaises(Sr865aCaptureError) as caught:
            self.capture()
        result = caught.exception.result
        self.assertFalse(result.completed)
        self.assertGreater(len(result.x_v), 0)
        self.assertTrue(any("restore" in error for error in result.cleanup_errors))

    def test_configurable_poll_interval_validates_before_io_and_changes_cadence(self):
        for invalid in (0, 0.099, 5.1, True, math.nan):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.driver.capture_xy(3, 1000, timeout_s=10, authorized=True,
                                       poll_interval_s=invalid)
        self.assertEqual(self.resource.events, [])
        guards = []
        self.capture(duration=3, poll_interval_s=0.5, on_poll=lambda: guards.append(self.clock()))
        self.assertTrue(any(0.49 <= t <= 0.6 for t in guards))

    def test_oneshot_full_wrapped_flag_is_valid_and_retains_all_samples(self):
        self.resource.finish_after = 0.1
        result = self.capture(duration=0.2)
        self.assertTrue(result.completed)
        self.assertEqual(result.final_capture_status, 6)
        self.assertEqual(result.valid_data_bytes, result.buffer_length_kib * 1024)
        self.assertEqual(result.padding_bytes, 0)
        self.assertTrue(result.capture_owned)
        self.assertTrue(result.stop_verified)
        self.assertTrue(result.capture_configuration_restored)

    def test_pre_capture_guard_fault_does_not_stop_an_unowned_capture(self):
        def guard():
            raise RuntimeError("source not protected")
        with self.assertRaises(Sr865aCaptureError) as caught:
            self.capture(on_poll=guard)
        self.assertFalse(caught.exception.result.capture_owned)
        self.assertFalse(any(kind == "write" for kind, _ in self.resource.events))

    def test_ieee_parser_error_is_not_retried_or_labelled_as_zero_signal(self):
        self.resource.binary_error = ValueError("IEEE block header malformed")
        with self.assertRaises(Sr865aCaptureError) as caught:
            self.capture()
        result = caught.exception.result
        self.assertEqual(result.x_v, ())
        self.assertEqual(result.downloaded_bytes, 0)
        self.assertFalse(result.completed)
        self.assertTrue(result.stop_verified)
        self.assertEqual(len(self.resource.binary_calls), 1)
        self.assertTrue(any("IEEE block header malformed" in (record.error or "")
                            for record in result.raw))

    def test_capture_query_timeout_retains_failed_command_and_no_fabricated_partial(self):
        original_query = self.resource.query
        byte_queries = []
        def query(command):
            if command == "CAPTUREBYTES?":
                byte_queries.append(command)
                raise TimeoutError("valid byte query failed")
            return original_query(command)
        self.resource.query = query
        with self.assertRaises(Sr865aCaptureError) as caught:
            self.capture()
        result = caught.exception.result
        self.assertIsNone(result.valid_data_bytes)
        self.assertEqual(result.x_v, ())
        self.assertTrue(result.stop_verified)
        self.assertTrue(any(record.command == "CAPTUREBYTES?" and record.error
                            for record in result.raw))
        self.assertEqual(byte_queries, ["CAPTUREBYTES?"])

    def test_stop_and_timeout_cleanup_preserve_keyboard_interrupt_evidence(self):
        def guard():
            if self.clock() >= 1:
                raise KeyboardInterrupt()
        with self.assertRaises(Sr865aCaptureError) as caught:
            self.capture(duration=3, on_poll=guard)
        self.assertIsInstance(caught.exception.__cause__, KeyboardInterrupt)
        self.assertIn("KeyboardInterrupt", caught.exception.result.error)
        self.assertTrue(caught.exception.result.stop_verified)
        self.assertGreater(len(caught.exception.result.x_v), 0)

    def test_whole_capture_timeout_is_not_extended_by_guard_work(self):
        def guard():
            if self.clock() >= 1:
                self.clock.sleep(15)
        with self.assertRaises(Sr865aCaptureError) as caught:
            self.capture(duration=3, on_poll=guard)
        self.assertIn("deadline expired", caught.exception.result.error)
        self.assertTrue(caught.exception.result.stop_verified)
        self.assertFalse(caught.exception.result.completed)

    def test_transport_timeout_is_bounded_and_restored(self):
        self.resource.timeout = 10000
        result = self.capture()
        self.assertTrue(result.completed)
        during_capture = self.resource.io_timeouts[1:]
        self.assertTrue(all(0 < timeout <= 5000 for _, timeout in during_capture))
        self.assertEqual(self.resource.timeout, 10000)

    def test_unknown_initial_capture_status_is_rejected_without_writes(self):
        original_query = self.resource.query
        self.resource.query = lambda command: "8" if command == "CAPTURESTAT?" else original_query(command)
        with self.assertRaises(Sr865aCaptureError) as caught:
            self.capture()
        self.assertIn("Unknown capture", caught.exception.result.error)
        self.assertFalse(any(kind == "write" for kind, _ in self.resource.events))

    def test_bad_configuration_readback_restores_changed_configuration_without_start(self):
        self.resource.ignore_setting = "CAPTURECFG"
        with self.assertRaises(Sr865aCaptureError) as caught:
            self.capture()
        self.assertIn("CAPTURECFG readback mismatch", caught.exception.result.error)
        self.assertFalse(caught.exception.result.capture_owned)
        self.assertNotIn(("write", "CAPTURESTART 0,0"), self.resource.events)
        self.assertTrue(caught.exception.result.capture_configuration_restored)

    def test_nonfinite_padding_is_not_counted_as_a_real_sample(self):
        original_binary = self.resource.query_binary_values
        def query_binary(command, **kwargs):
            values = original_binary(command, **kwargs)
            offset = int(command.split(" ", 1)[1].split(",")[0])
            valid_count = max(0, self.resource.acquired() // 4 - offset * 256)
            for index in range(min(valid_count, len(values)), len(values)):
                values[index] = float("nan")
            return values
        self.resource.query_binary_values = query_binary
        result = self.capture()
        self.assertTrue(result.completed)
        self.assertGreater(result.padding_bytes, 0)
        self.assertTrue(all(math.isfinite(value) for value in result.x_v))

    def test_large_download_does_not_run_the_guard_for_every_fast_binary_block(self):
        original_binary = self.resource.query_binary_values
        def query_binary(command, **kwargs):
            values = original_binary(command, **kwargs)
            self.clock.sleep(0.005)
            return values
        self.resource.query_binary_values = query_binary
        guards = []
        result = self.capture(duration=3, rate=100000,
                              on_poll=lambda: guards.append((self.clock(), len(self.resource.binary_calls))))
        self.assertTrue(result.completed)
        self.assertGreater(len(self.resource.binary_calls), 30)
        during_download = [entry for entry in guards if 0 < entry[1] < len(self.resource.binary_calls)]
        self.assertEqual(during_download, [])
        self.assertLess(self.clock(), 3.3)

    def test_slow_download_guard_is_throttled_and_keeps_partial_on_fault(self):
        original_binary = self.resource.query_binary_values
        def query_binary(command, **kwargs):
            values = original_binary(command, **kwargs)
            self.clock.sleep(0.6)
            return values
        self.resource.query_binary_values = query_binary
        guards = []
        def guard():
            guards.append((self.clock(), len(self.resource.binary_calls)))
            if len(self.resource.binary_calls) >= 2:
                raise ValueError("guard during download")
        with self.assertRaises(Sr865aCaptureError) as caught:
            self.capture(duration=3, rate=100000, on_poll=guard)
        result = caught.exception.result
        self.assertEqual(len(self.resource.binary_calls), 2)
        self.assertEqual(len(result.x_v), 2 * 64 * 128)
        self.assertIn("guard during download", result.error)
        self.assertTrue(result.stop_verified)
        self.assertTrue(result.capture_configuration_restored)
        download_guards = [entry for entry in guards if entry[1] > 0]
        self.assertEqual(len(download_guards), 1)

    def test_throttled_download_keeps_the_original_operation_deadline(self):
        original_binary = self.resource.query_binary_values
        def query_binary(command, **kwargs):
            values = original_binary(command, **kwargs)
            self.clock.sleep(0.6)
            return values
        self.resource.query_binary_values = query_binary
        with self.assertRaises(Sr865aCaptureError) as caught:
            self.capture(duration=3, rate=100000)
        result = caught.exception.result
        self.assertIn("deadline expired", result.error)
        self.assertEqual(len(self.resource.binary_calls), 17)
        self.assertEqual(len(result.x_v), 17 * 64 * 128)
        self.assertLess(self.clock(), 13.3)
        self.assertTrue(result.stop_verified)


class CaptureBackendTests(unittest.TestCase):
    def test_capture_status_preflight_is_identity_checked_read_only(self):
        resource = CaptureResource(FakeClock())
        resource.active_before = True
        backend = create_lockin_backend(model="SR865A", role="lockin_xy", resource=resource)
        self.assertEqual(backend.read_capture_status(), 3)
        self.assertEqual(resource.events, [("query", "*IDN?"), ("query", "CAPTURESTAT?")])

    def test_external_to_internal_sets_safe_stored_frequency_before_source_switch(self):
        resource = CaptureResource(FakeClock())
        resource.responses.update({"RSRC?": "1", "FREQINT?": "1000000",
                                   "FREQDET?": "50027"})
        backend = create_lockin_backend(model="SR865A", role="lockin_xy", resource=resource)
        backend.read_identity()
        self.assertEqual(backend.configure_internal_reference(50000, authorize_writes=True), 50000)
        writes = [command for kind, command in resource.events if kind == "write"]
        self.assertEqual(writes, ["FREQINT 50000", "RSRC 0"])
        set_frequency = resource.events.index(("write", "FREQINT 50000"))
        switch_source = resource.events.index(("write", "RSRC 0"))
        self.assertIn(("query", "FREQINT?"), resource.events[set_frequency + 1:switch_source])

    def test_internal_configuration_does_not_write_reference_mode_again(self):
        resource = CaptureResource(FakeClock())
        resource.responses.update({"FREQINT?": "49999", "FREQDET?": "49999"})
        backend = create_lockin_backend(model="SR865A", role="lockin_xy", resource=resource)
        backend.read_identity()
        self.assertEqual(backend.configure_internal_reference(50000, authorize_writes=True), 50000)
        self.assertEqual([command for kind, command in resource.events if kind == "write"], ["FREQINT 50000"])

    def test_dual_chop_and_unknown_source_are_rejected_before_any_write(self):
        for source in ("2", "3", "4", "invalid"):
            resource = CaptureResource(FakeClock())
            resource.responses["RSRC?"] = source
            backend = create_lockin_backend(model="SR865A", role="lockin_xy", resource=resource)
            backend.read_identity()
            with self.subTest(source=source), self.assertRaises(RuntimeError):
                backend.configure_internal_reference(50000, authorize_writes=True)
            self.assertFalse(any(kind == "write" for kind, _ in resource.events))

    def test_facade_supports_internal_frequency_and_audited_binary_capture(self):
        clock = FakeClock()
        resource = CaptureResource(clock)
        backend = create_lockin_backend(model="SR865A", role="lockin_xy", resource=resource)
        self.assertEqual(resource.events, [])
        backend.read_identity()
        self.assertEqual(backend.configure_internal_reference(50000, authorize_writes=True), 50000)
        self.assertEqual(backend.read_internal_frequency(), 50000)
        result = backend.capture_xy(0.1, 1000, timeout_s=10, authorize_writes=True,
                                    clock=clock, sleep=clock.sleep)
        self.assertTrue(result.completed)
        raw = [entry for entry in backend.audit if entry.operation == "query_binary"]
        self.assertEqual(len(raw), 1)
        self.assertEqual(len(bytes.fromhex(raw[0].response)), result.downloaded_bytes)

    def test_old_transport_capability_is_checked_without_capture_io(self):
        clock = FakeClock()
        resource = CaptureResource(clock)
        resource.query_binary_values = None
        backend = create_lockin_backend(model="SR865A", role="lockin_xy", resource=resource)
        backend.read_identity()
        before = list(resource.events)
        with self.assertRaisesRegex(Sr865aError, "binary-capable"):
            backend.capture_xy(0.1, 1000, timeout_s=10, authorize_writes=True)
        self.assertEqual(resource.events, before)

    def test_sr830_buffer_methods_are_rejected_before_io(self):
        resource = CaptureResource(FakeClock())
        backend = create_lockin_backend(model="SR830", role="lockin_xx", resource=resource)
        for call in (lambda: backend.capture_xy(1, 1000, timeout_s=10),
                     lambda: backend.configure_internal_reference(50000),
                     lambda: backend.read_internal_frequency()):
            with self.assertRaises(ValueError):
                call()
        self.assertEqual(resource.events, [])


if __name__ == "__main__":
    unittest.main()
