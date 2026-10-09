"""Reference continuation is diagnostic; all tests use owned fake resources."""
import unittest

from attodry_control.lockin_overload import reading_allows_continuation
from attodry_control.photonics_lockin_config import load_photonics_lockin_config
from attodry_control.photonics_lockin_points import PhotonicsLockinPointSession
from tests.photonics_lockin_helpers import (
    FakeManager, reversed_sine_document, reversed_sine_safety_document,
)


class ReferenceUnlockContinuationTests(unittest.TestCase):
    def make(self, *, timeout=0, unlock_policy="record_continue", overload_policy="abort", xy_harmonics=None):
        doc = reversed_sine_document()
        if xy_harmonics is not None:
            doc["lockin_xy"]["harmonics"] = xy_harmonics
        doc["photonics_lockin"].update(reference_lock_timeout_s=timeout,
            reference_unlock_policy=unlock_policy, overload_policy=overload_policy)
        cfg = load_photonics_lockin_config("fake.toml", document=doc,
            safety_document=reversed_sine_safety_document())
        manager = FakeManager(doc)
        manager.resources["FAKE::XY"].responses["SLVL?"] = "0.2"
        now, waits, events = [0.0], [], []

        def sleep(seconds):
            waits.append(seconds)
            now[0] += seconds

        session = PhotonicsLockinPointSession("fake.toml", cfg,
            lambda kind, payload: events.append((kind, payload)),
            manager_factory=lambda: manager, sleep=sleep, clock=lambda: now[0])
        self.addCleanup(session.close)
        session.open()
        session.protect_source()
        return session, manager, now, waits, events

    def ready(self, **kwargs):
        session, manager, now, waits, events = self.make(**kwargs)
        session.configure()
        session.set_point(0.004)
        session.qualify()
        return session, manager, now, waits, events

    def assert_unlock_reading(self, reading, role):
        self.assertFalse(reading["clean"])
        self.assertFalse(reading["valid_for_analysis"])
        self.assertTrue(reading["reference_unlock_continuation"])
        self.assertFalse(reading["overload_continuation"])
        self.assertEqual(reading["problems"], [f"lockin_{role} reference unlocked"])
        entry = reading["status"]["samples"][0]
        self.assertEqual(entry["valid_for_analysis_by_role"],
                         {"lockin_xx": False, "lockin_xy": False})
        self.assertEqual(entry["continued_overload_problems"], [])
        self.assertEqual(entry["continued_reference_unlock_problems"], reading["problems"])
        self.assertEqual(entry["blocking_problems"], [])
        self.assertTrue(entry["settings_verified"])
        self.assertEqual(reading["status"]["reference_unlock_policy"], "record_continue")
        self.assertIn("lockin_xx_h1_x_v", reading["measurements"])
        self.assertIn("lockin_xy_h2_x_v", reading["measurements"])
        self.assertTrue(reading_allows_continuation(reading))

    def test_new_latched_unlock_after_settling_records_and_does_not_abort_configure(self):
        session, manager, now, _, events = self.make()
        xy = manager.resources["FAKE::XY"]
        ordinary_sleep = session.sleep

        def settled_unlock(seconds):
            ordinary_sleep(seconds)
            if seconds == session.config.settle_s:
                xy.responses["LIAS?"] = "8"

        session.sleep = settled_unlock
        session.configure()
        self.assertTrue(session.configured)
        self.assertFalse(session.failed)
        observations = [p for kind, p in events if kind == "lockin_raw_role"
                        and p["role"] == "lockin_xy" and p["formal_candidate"]]
        self.assertEqual(observations[-1]["sample"]["status"]["native_status"]["lia_status_raw"], 8)
        self.assertTrue(observations[-1]["sample"]["status"]["locked"])
        self.assertEqual(observations[-1]["continued_reference_unlock_problems"],
                         ["lockin_xy reference unlocked"])

    def test_formal_latched_unlock_bracket_invalidates_both_roles_and_is_consumed(self):
        session, manager, _, _, _ = self.ready()
        xy = manager.resources["FAKE::XY"]
        xy.responses["LIAS?"] = "8"
        reading = session.sample_point()
        self.assert_unlock_reading(reading, "xy")
        entry = reading["status"]["samples"][0]
        self.assertTrue(entry["samples"]["xy"]["status"]["validity"])
        self.assertEqual(entry["bracket_samples"]["before"]["xy"]["status"]["native_status"]["lia_status_raw"], 8)
        self.assertTrue(session.sample_point()["clean"])
        self.assertTrue(session.cleanup()["source_protection_verified"])

    def test_current_unlock_in_each_model_records_raw_and_continues(self):
        for role in ("XX", "XY"):
            with self.subTest(role=role):
                session, manager, _, _, _ = self.ready()
                resource = manager.resources[f"FAKE::{role}"]
                if role == "XY":
                    resource.responses["CUROVLDSTAT?"] = "8"
                else:
                    def unlocked(resource, command):
                        if command == "LIAS?":
                            resource.responses[command] = "8"
                    resource.on_query = unlocked
                reading = session.sample_point()
                self.assert_unlock_reading(reading, role.lower())
                self.assertFalse(reading["status"]["samples"][0]["samples"][role.lower()]["status"]["locked"])
                self.assertFalse(session.failed)

    def test_unlock_and_overload_categories_remain_independent(self):
        session, manager, _, _, _ = self.ready(overload_policy="record_continue")
        manager.resources["FAKE::XX"].responses["LIAS?"] = "1"
        manager.resources["FAKE::XY"].responses["CUROVLDSTAT?"] = "8"
        reading = session.sample_point()
        self.assertTrue(reading["overload_continuation"])
        self.assertTrue(reading["reference_unlock_continuation"])
        entry = reading["status"]["samples"][0]
        self.assertEqual(entry["continued_overload_problems"], ["lockin_xx input/reserve overload"])
        self.assertEqual(entry["continued_reference_unlock_problems"], ["lockin_xy reference unlocked"])
        self.assertEqual(entry["valid_for_analysis_by_role"],
                         {"lockin_xx": False, "lockin_xy": False})

    def test_expected_harmonic_transition_unlock_is_invalid_only_with_opt_in(self):
        for policy in ("abort", "record_continue"):
            with self.subTest(policy=policy):
                session, manager, _, _, _ = self.ready(unlock_policy=policy, xy_harmonics=[1, 2])
                xy = manager.resources["FAKE::XY"]
                def transition_unlock(resource, command):
                    if command == "HARM 2":
                        resource.responses["LIAS?"] = "8"
                xy.on_write = transition_unlock
                reading = session.sample_point()
                transition = reading["status"]["samples"][1]
                self.assertEqual(transition["bracket_samples"]["transition"]["xy"]
                                 ["status"]["native_status"]["lia_status_raw"], 8)
                self.assertEqual(reading["reference_unlock_continuation"], policy == "record_continue")
                self.assertEqual(transition["valid_for_analysis_by_role"],
                                 {"lockin_xx": policy == "abort", "lockin_xy": policy == "abort"})
                self.assertTrue(reading_allows_continuation(reading))

    def test_qualification_and_optical_reference_recovery_retain_unlock_without_abort(self):
        session, manager, _, _, events = self.ready()
        xy = manager.resources["FAKE::XY"]
        def latched_unlock(resource, command):
            if command == "LIAS?":
                resource.responses[command] = "8"
        xy.on_query = latched_unlock
        session.qualify()
        session.prepare_reference_transition()
        session.restore_source_after_reference_transition()
        self.assertFalse(session.reference_transition_pending)
        self.assertFalse(session.failed)
        self.assertEqual(manager.resources["FAKE::XX"].responses["SLVL?"], "0.004")
        observations = [p for kind, p in events if kind == "lockin_raw_role"
                        and p.get("continued_reference_unlock_problems")]
        self.assertGreaterEqual(len(observations), 4)
        self.assertTrue(all(p["blocking_problems"] == [] for p in observations))

    def test_formal_unlock_abort_default_is_preserved(self):
        session, manager, _, _, events = self.ready(unlock_policy="abort")
        manager.resources["FAKE::XY"].responses["LIAS?"] = "8"
        with self.assertRaisesRegex(ValueError, "reference unlocked"):
            session.sample_point()
        self.assertTrue(session.failed)
        self.assertTrue(any(kind == "lockin_raw_role" and "lockin_xy reference unlocked" in p["problems"]
                            for kind, p in events))

    def test_wait_expiration_continues_known_unlock_with_explicit_timeout_evidence(self):
        for role in ("XX", "XY"):
            with self.subTest(role=role):
                session, manager, now, waits, events = self.make(timeout=3)
                session.reference_wait_pending = True
                resource = manager.resources[f"FAKE::{role}"]
                def unlocked(resource, command):
                    if command == "LIAS?":
                        resource.responses[command] = "8"
                resource.on_query = unlocked
                if role == "XY":
                    resource.responses["CUROVLDSTAT?"] = "8"
                session.wait_for_reference_lock()
                self.assertEqual(waits, [1, 1, 1])
                self.assertEqual(now[0], 3)
                self.assertFalse(session.reference_wait_pending)
                self.assertFalse(session.failed)
                finished = next(p for kind, p in events if kind == "lockin_reference_wait_finished")
                self.assertTrue(finished["timed_out"])
                self.assertFalse(finished["locked"])
                self.assertEqual(finished["polls"], 3)
                self.assertEqual(finished["continued_reference_unlock_problems"],
                                 [f"lockin_{role.lower()} reference unlocked"])
                self.assertEqual(finished["remaining_status_by_role"][role.lower()]["status"]["locked"], False)
                self.assertTrue(any(kind == "lockin_reference_wait_timed_out" for kind, _ in events))

    def test_locked_live_sr865a_with_unlock_latch_does_not_wait_for_a_clean_latch(self):
        session, manager, _, waits, events = self.make(timeout=3)
        session.reference_wait_pending = True
        manager.resources["FAKE::XY"].responses["LIAS?"] = "8"
        session.wait_for_reference_lock()
        self.assertEqual(waits, [])
        observation = next(p for kind, p in events
                           if kind == "lockin_reference_status" and p["role"] == "lockin_xy")
        self.assertEqual(observation["continued_reference_unlock_problems"], ["lockin_xy reference unlocked"])

    def test_wait_expiration_does_not_relax_frequency_or_range(self):
        for frequency in ("50050", "0"):
            with self.subTest(frequency=frequency):
                session, manager, _, _, events = self.make(timeout=2)
                session.reference_wait_pending = True
                xy = manager.resources["FAKE::XY"]
                xy.responses.update({"CUROVLDSTAT?": "8", "FREQEXT?": frequency})
                with self.assertRaises((ValueError, RuntimeError)):
                    session.wait_for_reference_lock()
                self.assertTrue(session.failed)
                self.assertTrue(any(kind == "lockin_reference_wait_timed_out" for kind, _ in events))
                self.assertFalse(any(kind == "lockin_reference_wait_finished" for kind, _ in events))

    def test_slow_io_crossing_deadline_still_fails_under_continue_policy(self):
        session, manager, now, _, events = self.make(timeout=3)
        session.reference_wait_pending = True
        def slow(resource, command):
            if command == "CUROVLDSTAT?":
                now[0] += 4
                resource.responses[command] = "8"
        manager.resources["FAKE::XY"].on_query = slow
        with self.assertRaises(TimeoutError):
            session.wait_for_reference_lock()
        self.assertTrue(session.failed)
        self.assertTrue(any(kind == "lockin_reference_status" for kind, _ in events))
        self.assertFalse(any(kind == "lockin_reference_wait_finished" for kind, _ in events))

    def test_wait_deadline_cannot_excuse_persistent_settings_transition(self):
        session, manager, _, _, events = self.make(timeout=2)
        session.reference_wait_pending = True
        xy = manager.resources["FAKE::XY"]
        xy.responses["CUROVLDSTAT?"] = "8"
        def transition(resource, command):
            if command == "*ESR?":
                resource.responses[command] = "64"
        xy.on_query = transition
        with self.assertRaisesRegex(ValueError, "settings change"):
            session.wait_for_reference_lock()
        self.assertTrue(session.failed)
        self.assertFalse(any(kind == "lockin_reference_wait_finished" for kind, _ in events))

    def test_unknown_mixed_poll_fault_still_rejects_before_timeout(self):
        session, manager, _, waits, events = self.make(timeout=3, overload_policy="record_continue")
        session.reference_wait_pending = True
        manager.resources["FAKE::XY"].responses.update({"CUROVLDSTAT?": "8", "LIAS?": "40"})
        with self.assertRaisesRegex(ValueError, "synchronous filter"):
            session.wait_for_reference_lock()
        self.assertEqual(waits, [])
        observed = next(p for kind, p in events
                        if kind == "lockin_reference_status" and p["role"] == "lockin_xy")
        self.assertEqual(observed["continued_reference_unlock_problems"], ["lockin_xy reference unlocked"])
        self.assertEqual(observed["blocking_problems"], ["lockin_xy synchronous filter fault"])

    def test_mixed_unlock_with_unknown_error_power_on_or_filter_is_still_fatal(self):
        for role, command, value in (("XX", "LIAS?", "136"), ("XX", "ERRS?", "1"),
                ("XY", "LIAS?", "12"), ("XY", "LIAS?", "40"),
                ("XY", "LIAS?", "72"), ("XY", "*ESR?", "128"), ("XY", "ERRS?", "1")):
            with self.subTest(role=role, command=command, value=value):
                session, manager, _, _, events = self.ready(overload_policy="record_continue")
                resource = manager.resources[f"FAKE::{role}"]
                resource.responses["LIAS?"] = "8"
                resource.responses[command] = value
                with self.assertRaises(ValueError):
                    session.sample_point()
                self.assertTrue(session.failed)
                observation = next(p for kind, p in reversed(events)
                                   if kind == "lockin_raw_role" and p["role"] == f"lockin_{role.lower()}")
                self.assertTrue(observation["blocking_problems"])
                self.assertTrue(observation["continued_reference_unlock_problems"])
