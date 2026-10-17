from dataclasses import replace
from pathlib import Path
import unittest

from attodry_control.chopper_config import (
    BLADE_SPECS, ChopperConfig, ChopperError, load_chopper_config,
)
from attodry_control.chopper import (
    ChopperSession, ChopperSerialTransport, SimulatedChopperTransport,
)


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def sleep(self, duration):
        self.value += duration


def config(**kwargs):
    return replace(ChopperConfig(backend="simulation", blade="MC1F10"), **kwargs)


class ConfigTests(unittest.TestCase):
    def test_disabled_and_absent_are_hardware_free_but_unknown_keys_reject(self):
        self.assertIsNone(load_chopper_config({}, Path(".")))
        self.assertIsNone(load_chopper_config({"chopper": {"enabled": False,
            "blade": "CHANGE_ME", "port": "CHANGE_ME"}}, Path(".")))
        with self.assertRaises(ChopperError):
            load_chopper_config({"chopper": {"enabled": "false"}}, Path("."))
        with self.assertRaises(ValueError):
            load_chopper_config({"chopper": {"enabled": False, "harmonc_n": 1}}, Path("."))

    def test_complete_loader_and_boundaries(self):
        result = load_chopper_config({"chopper": {"enabled": True,
            "backend": "simulation", "blade": "MC1F10", "harmonic_n": 15,
            "harmonic_d": 15}}, Path("not-a-real-directory"))
        self.assertEqual(result.validate_reference_frequency(100), 100)
        for field, value in (("blade", ""), ("reference_track", "inner"),
                ("reference_source", "sine"), ("harmonic_n", True), ("harmonic_d", 16),
                ("phase_deg", 0.5), ("frequency_tolerance_hz", float("nan")),
                ("run_enabled", 1), ("consecutive_good", 0), ("finish_rotation", "laser_off")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                config(**{field: value}).validate()

    def test_all_manufacturer_blade_indices_and_track_limits(self):
        self.assertEqual(sorted(row[0] for row in BLADE_SPECS.values()), list(range(15)))
        for blade, (_, inner, outer) in BLADE_SPECS.items():
            for track, slots in (("inner", inner), ("outer", outer)) if outer else (("single", inner),):
                d = config(blade=blade, reference_track=track)
                # Dual blades always lock their outer track; HP selects the track.
                source_min = outer * 2 if blade.startswith("MC2F") else slots * 2
                source_max = outer * 100 if blade.startswith("MC2F") else slots * 100
                self.assertAlmostEqual(d.target_frequency(source_min), slots * 2)
                self.assertAlmostEqual(d.target_frequency(source_max), slots * 100)
                with self.assertRaises(ValueError):
                    d.target_frequency(source_max + 1)

    def test_manufacturer_reference_and_actual_output_mappings(self):
        for source, track, code in (("internal", "outer", 0), ("internal", "inner", 1),
                                    ("external", "outer", 2), ("external", "inner", 3)):
            d = config(blade="MC1F10HP", reference_source=source, reference_track=track)
            self.assertEqual(d.reference_code, code)
            self.assertEqual(d.actual_output_code, 2 if track == "inner" else 1)
        self.assertEqual(config(reference_source="external").reference_code, 1)
        self.assertEqual(config(reference_source="internal").reference_code, 0)

    def test_harmonics_are_optical_frequency_not_detector_harmonic(self):
        self.assertEqual(config(harmonic_n=2).target_frequency(100), 200)
        self.assertEqual(config(harmonic_d=2).target_frequency(100), 50)
        self.assertEqual(config(blade="MC2F330", reference_track="inner").target_frequency(100), 10)
        with self.assertRaises(ChopperError):
            config(reference_source="internal", harmonic_n=2).validate()
        with self.assertRaises(ChopperError):
            config(reference_source="internal").target_frequency(100.5)

    def test_real_identity_and_explicit_port_required(self):
        with self.assertRaises(ChopperError):
            config(backend="serial").require_hardware()
        config(backend="serial", port="COM99", expected_idn="Thorlabs MC2000B 1.0").require_hardware()


class SessionTests(unittest.TestCase):
    def session(self, **kwargs):
        self.clock = Clock()
        cfg = config(**kwargs)
        self.transport = SimulatedChopperTransport(cfg)
        return ChopperSession(cfg, transport=self.transport,
                              clock=self.clock, sleep=self.clock.sleep)

    def test_run_exact_transcript_and_cleanup_is_not_optical_off(self):
        d = self.session(phase_deg=90)
        changes = []
        snapshot = d.prepare(100, lambda: changes.append(self.clock()))
        self.assertEqual(self.transport.commands[:2], ["id?", "verbose?"])
        writes = [cmd for cmd in self.transport.commands if "=" in cmd]
        self.assertEqual(writes, ["phase=90", "enable=1"])
        self.assertEqual(self.clock(), 2)
        self.assertTrue(snapshot["last_confirmed_state"]["frequency_verified"])
        self.assertFalse(snapshot["last_confirmed_state"]["phase_lock_verified_by_query"])
        self.assertEqual(len(changes), 3)
        result = d.cleanup()
        self.assertTrue(result["verified"])
        self.assertTrue(result["rotation_stopped"])
        self.assertFalse(result["optical_off_confirmed"])
        d.close()
        self.assertTrue(self.transport.closed)

    def test_same_preparation_reuses_no_writes_no_repeated_stability_wait(self):
        d = self.session()
        d.prepare(100)
        first_count = len(self.transport.commands)
        before = self.clock()
        d.prepare(100)
        self.assertEqual(self.clock(), before)
        self.assertEqual(len(self.transport.commands) - first_count, 10)
        self.assertFalse(any("=" in cmd for cmd in self.transport.commands[first_count:]))

    def test_guard_returns_copy_only_state_and_full_snapshot_retains_raw_history(self):
        d = self.session()
        result = d.prepare(100)
        self.assertNotIn("transcript", result)
        self.assertEqual(result["transcript_count"], len(d.transcript))
        result["last_confirmed_state"]["phase"] = 999
        self.assertEqual(d.last_confirmed_state["phase"], 0)
        class DoNotCopyHistory:
            def __deepcopy__(self, memo):
                raise AssertionError("A routine chopper guard traversed accumulated raw history")
        d.transcript[0]["history_copy_probe"] = DoNotCopyHistory()
        self.transport.transcript = [{"raw_response": "partial response preserved",
                                      "history_copy_probe": DoNotCopyHistory()}]
        before = len(self.transport.commands)
        guarded = d.verify_ready()
        self.assertEqual(len(self.transport.commands) - before, 10)
        self.assertNotIn("transport_transcript", guarded)
        self.assertEqual(guarded["transport_transcript_count"], 1)
        # Both unchanged reuse and a new frequency must avoid traversing history.
        d.prepare(100)
        revised = d.prepare(101)
        self.assertEqual(revised["target_frequency_hz"], 101)
        self.assertEqual(revised["transcript_count"], len(d.transcript))
        del d.transcript[0]["history_copy_probe"]
        del self.transport.transcript[0]["history_copy_probe"]
        full = d.snapshot()
        self.assertEqual(full["transcript"], d.transcript)
        self.assertIsNot(full["transcript"], d.transcript)
        self.assertEqual(full["transport_transcript"][0]["raw_response"], "partial response preserved")
        self.assertEqual(full["transcript"][0]["command"], "id?")
        self.assertNotIn("transcript_count", full)

    def test_standby_before_reference_setting_and_fresh_readback_after_write(self):
        d = self.session(harmonic_n=2)
        self.transport.values.update(enable=1, nharmonic=1)
        d.prepare(100)
        commands = self.transport.commands
        index = commands.index("nharmonic=2")
        self.assertLess(commands.index("enable=0"), index)
        self.assertEqual(commands[index + 1], "nharmonic?")
        self.assertGreater(commands.index("enable=1"), index)

    def test_unknown_blade_and_identity_never_write_or_cleanup_takeover(self):
        for identity_bad in (True, False):
            d = self.session(expected_idn="Thorlabs MC2000B 1.0")
            if identity_bad:
                exchange = self.transport.exchange
                self.transport.exchange = lambda cmd, **kw: (
                    "id?\r\nOther MC2000B 9.0\r\n>" if cmd == "id?" else exchange(cmd, **kw))
            else:
                self.transport.values["blade"] = 2
            with self.subTest(identity=identity_bad), self.assertRaises(ChopperError):
                d.prepare(100)
            result = d.cleanup()
            self.assertFalse(result["verified"])
            self.assertFalse(any("=" in cmd for cmd in self.transport.commands))
            d.close()

    def test_settings_drift_and_frequency_drift_fail_and_retain_raw(self):
        for key, value in (("ref", 0), ("output", 0), ("phase", 99), ("enable", 0),
                           ("nharmonic", 2)):
            d = self.session(phase_deg=0)
            d.prepare(100)
            self.transport.values[key] = value
            with self.subTest(key=key), self.assertRaises(ChopperError):
                d.verify_ready()
            self.assertFalse(d.state_current)
            self.assertTrue(d.transcript[-1]["reply"].endswith(">"))
        d = self.session()
        d.prepare(100)
        self.transport.external_frequency_hz = 110
        with self.assertRaisesRegex(ChopperError, "frequency readback"):
            d.verify_ready()

    def test_readiness_timeout_remains_bounded_and_cleanup_works(self):
        d = self.session(ready_timeout_s=3)
        exchange = self.transport.exchange
        self.transport.exchange = lambda cmd, **kw: (
            "refoutfreq?\r\n0\r\n>" if cmd == "refoutfreq?" else exchange(cmd, **kw))
        with self.assertRaises(TimeoutError):
            d.prepare(100)
        self.assertEqual(self.clock(), 3)
        self.assertFalse(d.poisoned)
        self.assertTrue(d.cleanup()["verified"])

    def test_outer_operation_deadline_shortens_readiness_without_restart(self):
        d = self.session(ready_timeout_s=30)
        with self.assertRaises(TimeoutError):
            d.prepare(100, deadline=0.5)
        self.assertEqual(self.clock(), 0.5)
        self.assertEqual(self.transport.commands.count("enable=1"), 1)
        self.assertTrue(d.cleanup()["verified"])

    def test_malformed_or_multiple_response_poison_and_prevent_more_commands(self):
        for reply in ("id?\r\nMC2000B\r\nextra\r\n>", "id?\r\nMC2000B", "garbage\r\n>",
                      "id?\r\nCommand error CMD_NOT_DEFINED\r\n>"):
            d = self.session()
            self.transport.exchange = lambda cmd, **kw: reply
            with self.subTest(reply=reply), self.assertRaises(ChopperError):
                d.open()
            count = len(d.transcript)
            self.assertTrue(d.poisoned)
            self.assertFalse(d.cleanup()["verified"])
            self.assertEqual(len(d.transcript), count)

    def test_nonfinite_readback_poison_and_preserve_previous_confirmed(self):
        d = self.session()
        d.prepare(100)
        last = d.last_confirmed_state
        exchange = self.transport.exchange
        self.transport.exchange = lambda cmd, **kw: (
            "input?\r\nNaN\r\n>" if cmd == "input?" else exchange(cmd, **kw))
        with self.assertRaises(ChopperError):
            d.verify_ready()
        self.assertIs(d.last_confirmed_state, last)
        self.assertTrue(d.poisoned)
        self.assertFalse(d.cleanup()["verified"])

    def test_no_auto_connection_no_port_scanning_and_manual_evidence_honest(self):
        with self.assertRaises(ChopperError):
            ChopperSession(config(backend="serial", port="COM99",
                expected_idn="MC2000B 1.0")).open()
        d = self.session(backend="manual")
        with self.assertRaisesRegex(ChopperError, "phase lock"):
            d.prepare(100)
        d = self.session(backend="manual", manual_phase_lock_confirmed=True)
        result = d.prepare(100)
        self.assertEqual(self.transport.commands, [])
        self.assertFalse(result["last_confirmed_state"]["frequency_verified"])
        self.assertTrue(result["last_confirmed_state"]["manual_phase_lock_confirmed"])
        self.assertTrue(d.cleanup()["manual_verification_required"])

    def test_stop_setting_does_not_claim_modulation_is_present(self):
        d = self.session(run_enabled=False)
        result = d.prepare(100)
        self.assertEqual(self.transport.values["enable"], 0)
        self.assertFalse(result["last_confirmed_state"]["frequency_verified"])
        self.assertTrue(d.cleanup()["rotation_stopped"])

    def test_preserve_finishes_without_disabling_rotation(self):
        d = self.session(finish_rotation="preserve")
        d.prepare(100)
        before = len(self.transport.commands)
        result = d.cleanup()
        self.assertTrue(result["verified"])
        self.assertFalse(result["rotation_stopped"])
        self.assertEqual(self.transport.commands[before:], ["enable?"])

    def test_omitted_phase_preserves_initial_phase_and_detects_later_drift(self):
        d = self.session()
        self.transport.values["phase"] = 123
        d.prepare(100)
        self.assertFalse(any(cmd.startswith("phase=") for cmd in self.transport.commands))
        self.transport.values["phase"] = 124
        with self.assertRaisesRegex(ChopperError, "phase settings change"):
            d.prepare(100)

    def test_invalid_current_blade_dependent_codes_fail_before_any_write(self):
        for key, value in (("ref", 3), ("output", 4)):
            d = self.session()
            self.transport.values[key] = value
            with self.subTest(key=key), self.assertRaisesRegex(ChopperError, "Unknown blade-dependent"):
                d.open()
            self.assertFalse(d.cleanup()["verified"])
            self.assertFalse(any("=" in cmd for cmd in self.transport.commands))

    def test_unacknowledged_write_poison_does_not_claim_safe_cleanup(self):
        d = self.session()
        exchange = self.transport.exchange
        self.transport.exchange = lambda cmd, **kw: (
            "enable=1\r\nunexpected\r\n>" if cmd == "enable=1" else exchange(cmd, **kw))
        with self.assertRaisesRegex(ChopperError, "acknowledgement"):
            d.prepare(100)
        count = len(d.transcript)
        result = d.cleanup()
        self.assertFalse(result["verified"])
        self.assertTrue(result["manual_verification_required"])
        self.assertEqual(len(d.transcript), count)

    def test_readback_mismatch_fails_but_known_channel_still_allows_stop(self):
        d = self.session(phase_deg=90)
        exchange = self.transport.exchange
        def ignore_phase(command, **kwargs):
            if command == "phase=90":
                return "phase=90\r\n>"
            return exchange(command, **kwargs)
        self.transport.exchange = ignore_phase
        with self.assertRaisesRegex(ChopperError, "readback mismatch"):
            d.prepare(100)
        self.assertFalse(d.poisoned)
        self.assertTrue(d.cleanup()["verified"])

    def test_existing_verbose_mode_rejects_without_silently_disabling_it(self):
        d = self.session()
        self.transport.values["verbose"] = 1
        with self.assertRaisesRegex(ChopperError, "verbose mode"):
            d.prepare(100)
        self.assertEqual(self.transport.commands, ["id?", "verbose?"])
        self.assertFalse(d.cleanup()["verified"])

    def test_shared_deadline_also_bounds_unchanged_reuse_before_any_query(self):
        d = self.session()
        d.prepare(100)
        count = len(d.transcript)
        with self.assertRaisesRegex(TimeoutError, "before exchange"):
            d.prepare(100, deadline=self.clock())
        self.assertEqual(len(d.transcript), count)
        self.assertTrue(d.cleanup()["verified"])


class FakeSerial:
    def __init__(self, replies=None, startup=b"", **kwargs):
        self.kwargs, self.replies = kwargs, replies or {}
        self.buffer = bytearray(startup)
        self.writes = []
        self.closed = False
        self.timeout = self.write_timeout = 0

    @property
    def in_waiting(self):
        return len(self.buffer)

    def write(self, payload):
        self.writes.append(payload)
        self.buffer.extend(self.replies.get(payload, b""))
        return len(payload)

    def read(self, count):
        data = bytes(self.buffer[:count])
        del self.buffer[:count]
        return data

    def close(self):
        self.closed = True


class SerialTests(unittest.TestCase):
    def transport(self, **kwargs):
        self.serial = FakeSerial(**kwargs)
        return ChopperSerialTransport(config(backend="serial", port="COM999",
            expected_idn="MC2000B 1.0"), authorized=True,
            factory=lambda **kw: (self.serial.kwargs.update(kw) or self.serial))

    def test_protocol_115200_8n1_cr_prompt_and_raw_transcript(self):
        d = self.transport(replies={b"enable?\r": b"enable?\r\n1\r\n> "})
        try:
            self.assertEqual(d.exchange("enable?"), "enable?\r\n1\r\n> ")
            self.assertEqual(self.serial.writes, [b"enable?\r"])
            self.assertEqual(self.serial.kwargs["baudrate"], 115200)
            self.assertEqual(self.serial.kwargs["bytesize"], 8)
            self.assertEqual(self.serial.kwargs["parity"], "N")
            self.assertEqual(self.serial.kwargs["stopbits"], 1)
            self.assertFalse(self.serial.kwargs["xonxoff"])
            self.assertTrue(d.transcript[-1]["raw_response"].endswith("> "))
        finally:
            d.close()

    def test_stale_bytes_prevent_command_and_are_retained(self):
        d = self.transport()
        self.serial.buffer.extend(b"old\r\n>")
        try:
            with self.assertRaisesRegex(ChopperError, "stale"):
                d.exchange("enable?")
            self.assertEqual(self.serial.writes, [])
            self.assertIn("old", d.transcript[-1]["stale_bytes"])
            self.assertTrue(d.poisoned)
        finally:
            d.close()

    def test_late_terminal_whitespace_belongs_to_completed_response(self):
        d = self.transport(replies={b"id?\r": b"id?\rMC2000B 4.16\r>",
                                    b"verbose?\r": b"verbose?\r0\r>"})
        try:
            first_reply = d.exchange("id?")
            suffix = b" \r\n\t"
            self.serial.buffer.extend(suffix)  # Arrives after exchange returned.
            self.assertEqual(d.exchange("verbose?"), "verbose?\r0\r>")
            first = d.transcript[0]
            self.assertEqual(first["raw_response"], first_reply + suffix.decode("ascii"))
            self.assertEqual(first["late_suffixes"][0]["raw_bytes"], repr(suffix))
            self.assertEqual(first["late_suffixes"][0]["before_command"], "verbose?")
            self.assertIsInstance(first["late_suffixes"][0]["observed_monotonic_s"], float)
            self.assertEqual(self.serial.writes, [b"id?\r", b"verbose?\r"])
            self.assertFalse(d.poisoned)
            self.assertNotIn("stale_bytes", d.transcript[1])
        finally:
            d.close()

    def test_late_nonwhitespace_poison_keeps_raw_and_sends_no_next_command(self):
        for suffix in (b" noise\r\n", b"\x0b", b"\x0c", b" >"):
            d = self.transport(replies={b"id?\r": b"id?\rMC2000B 4.16\r>"})
            try:
                first_reply = d.exchange("id?")
                self.serial.buffer.extend(suffix)
                with self.subTest(suffix=suffix), self.assertRaisesRegex(ChopperError, "stale"):
                    d.exchange("verbose?")
                self.assertEqual(d.transcript[0]["raw_response"], first_reply)
                self.assertEqual(d.transcript[1]["stale_bytes"], repr(suffix))
                self.assertEqual(self.serial.writes, [b"id?\r"])
                self.assertTrue(d.poisoned)
            finally:
                d.close()

    def test_first_exchange_whitespace_is_unowned_and_rejected(self):
        d = self.transport()
        self.serial.buffer.extend(b" ")
        try:
            with self.assertRaisesRegex(ChopperError, "stale"):
                d.exchange("id?")
            self.assertEqual(d.transcript[0]["stale_bytes"], "b' '")
            self.assertEqual(self.serial.writes, [])
            self.assertTrue(d.poisoned)
        finally:
            d.close()

    def test_short_reply_timeout_poison_blocks_late_response_reassignment(self):
        d = self.transport(replies={b"enable?\r": b"enable?\r\n1\r\n"})
        try:
            with self.assertRaises(TimeoutError):
                d.exchange("enable?")
            self.serial.buffer.extend(b">")
            with self.assertRaises(ChopperError):
                d.exchange("input?")
            self.assertEqual(self.serial.writes, [b"enable?\r"])
            self.assertIn("1", d.transcript[-1]["raw_response"])
        finally:
            d.close()

    def test_startup_prompt_only_allowed_and_owner_released_on_bad_startup(self):
        with self.assertRaises(ChopperError):
            self.transport(startup=b"unknown\r\n>")
        d = self.transport(startup=b"\r\n>")
        try:
            with self.assertRaisesRegex(ChopperError, "already owned"):
                self.transport()
        finally:
            d.close()

    def test_empty_authorization_never_constructs_resource(self):
        calls = []
        with self.assertRaises(ChopperError):
            ChopperSerialTransport(config(backend="serial", port="COM999",
                expected_idn="MC2000B 1.0"), factory=lambda **kw: calls.append(kw))
        self.assertEqual(calls, [])

    def test_partial_write_and_unknown_prompt_suffix_poison(self):
        for partial in (True, False):
            d = self.transport(replies={b"enable?\r": b"1\r\n>later"})
            try:
                if partial:
                    self.serial.write = lambda payload: 1
                with self.subTest(partial=partial), self.assertRaises(ChopperError):
                    d.exchange("enable?")
                self.assertTrue(d.poisoned)
                self.assertIn("error", d.transcript[-1])
            finally:
                d.close()


if __name__ == "__main__":
    unittest.main()
