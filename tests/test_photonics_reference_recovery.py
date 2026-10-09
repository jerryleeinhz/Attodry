"""Bounded whole-operation reference recovery on synthetic instruments only."""
import unittest
from dataclasses import replace

from attodry_control.photonics_lockin_config import load_photonics_lockin_config
from attodry_control.photonics_lockin_points import PhotonicsLockinPointSession
from attodry_control.reference_transients import ReferenceTransientError
from tests.photonics_lockin_helpers import (
    FakeManager, reversed_sine_document, reversed_sine_safety_document,
)


class ReferenceRecoveryTests(unittest.TestCase):
    def make(self, *, policy="wait_stable", timeout=45, settle_constants=15,
             overload_policy="record_continue", unlock_policy="record_continue", configure=True, time_constant=0.01):
        doc = reversed_sine_document()
        for role in ("lockin_xx", "lockin_xy"):
            doc[role]["time_constant_s"] = time_constant
        doc["photonics_lockin"].update(reference_transient_policy=policy,
            reference_recovery_timeout_s=timeout, reference_recovery_consecutive_good=3,
            settle_time_constants=settle_constants, overload_policy=overload_policy,
            reference_unlock_policy=unlock_policy)
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
        if configure:
            session.configure()
            session.set_point(0.004)
            session.qualify()
        now[0] = 0.0
        waits.clear()
        events.clear()
        for resource in manager.resources.values():
            resource.queries.clear()
            resource.writes.clear()
        return session, manager, now, waits, events

    def once_error(self, session, *, stage="test_operation"):
        calls = [0]
        def operation():
            calls[0] += 1
            if calls[0] == 1:
                raise ReferenceTransientError("injected frequency mismatch", kind="pair_frequency_mismatch",
                                              evidence={"reference_hz": {"xx": 50027, "xy": 50030}})
            return "complete"
        return session.run_reference_operation(stage, operation), calls

    def test_formal_range_latch_rejects_candidate_and_reads_fresh_whole_pair(self):
        for status in (16, 24, 17, 25):
            with self.subTest(status=status):
                session, manager, _, _, events = self.make()
                manager.resources["FAKE::XX"].responses["LIAS?"] = str(status)
                reading = session.sample_point()
                self.assertTrue(reading["clean"])
                self.assertEqual(session.sample_count, 1)
                self.assertEqual(len(reading["status"]["samples"]), 1)
                rejected = next(p for kind, p in events if kind == "lockin_rejected_reference_candidate")
                self.assertFalse(rejected["valid_for_analysis"])
                self.assertEqual(rejected["transient_kind"], "frequency_range_changed")
                raw = rejected["transient_evidence"]["sample"]["status"]["native_status"][0]
                self.assertEqual(raw["raw"], status)
                self.assertTrue(any(kind == "reference_recovery_recovered" for kind, _ in events))
                self.assertFalse(session.failed)
                self.assertTrue(all(resource.writes == [] for resource in manager.resources.values()))

    def test_default_abort_keeps_range_latch_terminal(self):
        session, manager, _, _, events = self.make(policy="abort")
        manager.resources["FAKE::XX"].responses["LIAS?"] = "16"
        with self.assertRaises(ReferenceTransientError):
            session.sample_point()
        self.assertEqual(session.sample_count, 0)
        self.assertTrue(session.failed)
        self.assertFalse(any(kind == "reference_recovery_started" for kind, _ in events))

    def test_range_cannot_mask_time_constant_trigger_unknown_error_or_unapproved_overload(self):
        for raw, errors, overload_policy in ((48, "0", "record_continue"), (80, "0", "record_continue"),
                (144, "0", "record_continue"), (16, "1", "record_continue"), (17, "0", "abort")):
            with self.subTest(raw=raw, errors=errors, overload_policy=overload_policy):
                session, manager, _, _, events = self.make(overload_policy=overload_policy)
                manager.resources["FAKE::XX"].responses.update({"LIAS?": str(raw), "ERRS?": errors})
                with self.assertRaises(ValueError):
                    session.sample_point()
                self.assertTrue(session.failed)
                self.assertEqual(session.sample_count, 0)
                self.assertFalse(any(kind == "reference_recovery_started" for kind, _ in events))

    def test_finite_positive_interval_excursion_waits_for_current_stability(self):
        session, manager, now, _, events = self.make()
        xx = manager.resources["FAKE::XX"]
        xx.responses["FREQ?"] = "48000"
        def drift(resource, command):
            if command == "FREQ?" and now[0] >= 2:
                resource.responses[command] = "50027"
        xx.on_query = drift
        self.assertEqual(session.read_coordinates()["lockin_frequency_hz"], 50027)
        polls = [p for kind, p in events if kind == "reference_recovery_poll"]
        self.assertGreaterEqual(sum(not p["stable"] for p in polls), 2)
        self.assertGreaterEqual(now[0], 4 + session.config.settle_s)
        self.assertFalse(session.failed)

    def test_pair_mismatch_waits_without_changing_frequency_tolerance_or_source(self):
        session, manager, now, _, events = self.make()
        xx = manager.resources["FAKE::XX"]
        xx.responses["FREQ?"] = "50030"
        def drift(resource, command):
            if command == "FREQ?" and now[0] >= 1:
                resource.responses[command] = "50027"
        xx.on_query = drift
        self.assertEqual(session.read_reference_frequencies(), {"xx": 50027, "xy": 50027})
        self.assertEqual(session.config.pair_tolerance_hz, 1)
        self.assertTrue(all(resource.writes == [] for resource in manager.resources.values()))
        started = next(p for kind, p in events if kind == "reference_recovery_started")
        self.assertEqual(started["kind"], "pair_frequency_mismatch")

    def test_zero_and_nonfinite_frequency_are_not_recovered(self):
        for value in (0, float("nan"), float("inf")):
            with self.subTest(value=value):
                session, _, _, _, events = self.make()
                def operation():
                    session._check_frequencies({"xx": value, "xy": 50027})
                with self.assertRaises(ValueError) as result:
                    session.run_reference_operation("invalid_frequency", operation)
                self.assertNotIsInstance(result.exception, ReferenceTransientError)
                self.assertFalse(any(kind == "reference_recovery_started" for kind, _ in events))

    def test_model_detection_limit_is_checked_before_interval_transient_classification(self):
        session, _, _, _, events = self.make()
        def operation():
            session._check_frequencies({"xx": 2_000_000, "xy": 50027})
        with self.assertRaises(ValueError) as caught:
            session.run_reference_operation("unsupported_model_frequency", operation)
        self.assertNotIsInstance(caught.exception, ReferenceTransientError)
        self.assertFalse(any(kind == "reference_recovery_started" for kind, _ in events))

    def test_configured_reference_wait_does_not_clear_time_constant_or_configuration_latch(self):
        for role, query, value in (("XX", "LIAS?", "32"), ("XX", "LIAS?", "48"),
                                   ("XY", "*ESR?", "64")):
            with self.subTest(role=role, query=query, value=value):
                session, manager, _, _, events = self.make()
                session.config = replace(session.config, reference_lock_timeout_s=45)
                session.reference_wait_pending = True
                manager.resources[f"FAKE::{role}"].responses[query] = value
                with self.assertRaisesRegex(ValueError, "unexpected settings change"):
                    session.wait_for_reference_lock()
                self.assertTrue(session.failed)
                observation = next(p for kind, p in events if kind == "lockin_reference_status"
                                   and p["role"] == "lockin_" + role.lower())
                self.assertIn("lockin_" + role.lower() + " unexpected settings change", observation["problems"])
                self.assertFalse(any(kind == "reference_recovery_started" for kind, _ in events))

    def test_configured_reference_wait_range_is_recovered_with_shared_deadline(self):
        session, manager, _, _, events = self.make()
        session.config = replace(session.config, reference_lock_timeout_s=45)
        session.reference_wait_pending = True
        manager.resources["FAKE::XX"].responses["LIAS?"] = "16"
        session.wait_for_reference_lock()
        self.assertFalse(session.failed)
        self.assertFalse(session.reference_wait_pending)
        self.assertTrue(any(kind == "reference_recovery_recovered" for kind, _ in events))

    def test_sr865a_detection_mismatch_keeps_partial_xx_and_retries_complete_pair(self):
        session, manager, _, _, events = self.make()
        xy = manager.resources["FAKE::XY"]
        reads = [0]
        def mismatch_once(resource, command):
            if command == "FREQDET?":
                reads[0] += 1
                resource.responses[command] = "100100" if reads[0] == 1 else "100054"
        xy.on_query = mismatch_once
        reading = session.sample_point()
        self.assertTrue(reading["clean"])
        self.assertEqual(session.sample_count, 1)
        rejected = next(p for kind, p in events if kind == "lockin_rejected_reference_candidate")
        self.assertEqual(rejected["transient_kind"], "detection_frequency_mismatch")
        self.assertIn("xx", rejected["transient_evidence"]["partial_samples_by_role"])
        self.assertTrue(rejected["transient_evidence"]["raw"])
        self.assertTrue(all(resource.writes == [] for resource in manager.resources.values()))

    def test_persistent_transient_expires_without_resetting_deadline(self):
        session, manager, now, _, events = self.make(timeout=4)
        manager.resources["FAKE::XX"].responses["FREQ?"] = "48000"
        with self.assertRaises(TimeoutError):
            session.read_coordinates()
        self.assertEqual(now[0], 4)
        self.assertTrue(session.failed)
        self.assertEqual(session.sample_count, 0)
        self.assertEqual(sum(kind == "reference_recovery_started" for kind, _ in events), 1)
        self.assertTrue(any(kind == "reference_recovery_failed" for kind, _ in events))

    def test_second_operation_fault_uses_same_deadline_and_rollback_checkpoint(self):
        session, _, now, _, events = self.make(timeout=4)
        calls = [0]
        def operation():
            calls[0] += 1
            raise ReferenceTransientError("still mismatched", kind="pair_frequency_mismatch", evidence={})
        with self.assertRaises(TimeoutError):
            session.run_reference_operation("whole_sample", operation)
        starts = [p for kind, p in events if kind == "reference_recovery_started"]
        self.assertEqual(len(starts), 2)
        self.assertEqual({p["deadline_monotonic"] for p in starts}, {4})
        self.assertEqual(calls[0], 2)
        self.assertEqual(now[0], 4)

    def test_outer_failure_after_completed_lockin_rolls_back_count_and_rejects_old_candidate(self):
        session, _, _, _, events = self.make()
        calls = [0]
        def operation():
            calls[0] += 1
            reading = session.sample_point()
            if calls[0] == 1:
                raise ReferenceTransientError("outer after-reference mismatch", kind="pair_frequency_mismatch", evidence={})
            return reading
        reading = session.run_reference_operation("whole_sample", operation)
        self.assertTrue(reading["clean"])
        self.assertEqual(session.sample_count, 1)
        rejected = next(p for kind, p in events if kind == "lockin_reference_operation_rejected")
        self.assertEqual(len(rejected["rejected_formal_candidates"]), 1)
        candidate = rejected["rejected_formal_candidates"][0]
        self.assertFalse(candidate["clean"])
        self.assertFalse(candidate["valid_for_analysis"])
        self.assertEqual(candidate["status"]["samples"][0]["valid_for_analysis_by_role"],
                         {"lockin_xx": False, "lockin_xy": False})

    def test_recovery_requires_locked_evidence_from_latest_pair_not_only_prior_status(self):
        session, manager, _, _, events = self.make()
        xx = manager.resources["FAKE::XX"]
        reads = [0]
        def unlock_second(resource, command):
            if command == "LIAS?":
                reads[0] += 1
                resource.responses[command] = "8" if reads[0] == 2 else "0"
        xx.on_query = unlock_second
        value, _ = self.once_error(session)
        self.assertEqual(value, "complete")
        polls = [p for kind, p in events if kind == "reference_recovery_poll"]
        self.assertFalse(polls[0]["stable"])
        self.assertEqual(polls[0]["consecutive_good"], 0)

    def test_transient_guard_fault_is_polled_but_hard_power_failure_is_terminal(self):
        for transient in (True, False):
            with self.subTest(transient=transient):
                session, _, _, _, events = self.make()
                calls = [0]
                def guard(deadline):
                    calls[0] += 1
                    if calls[0] == 1:
                        if transient:
                            raise ReferenceTransientError("PEM frequency mismatch", kind="pem_frequency_mismatch", evidence={})
                        raise ValueError("optical power check failed")
                session.reference_recovery_guard = guard
                if transient:
                    self.assertEqual(self.once_error(session)[0], "complete")
                    self.assertFalse(next(p for kind, p in events if kind == "reference_recovery_poll")["stable"])
                else:
                    with self.assertRaisesRegex(ValueError, "power check failed"):
                        self.once_error(session)
                    self.assertTrue(session.failed)
                    self.assertFalse(any(kind == "reference_recovery_recovered" for kind, _ in events))

    def test_settling_new_range_restarts_streak_and_full_filter_time(self):
        session, manager, now, _, events = self.make(settle_constants=100)
        xx = manager.resources["FAKE::XX"]
        injected = [False]
        def range_during_settle(resource, command):
            if command == "LIAS?" and now[0] >= 3 and not injected[0]:
                injected[0] = True
                resource.responses[command] = "16"
        xx.on_query = range_during_settle
        self.assertEqual(self.once_error(session)[0], "complete")
        polls = [p for kind, p in events if kind == "reference_recovery_poll"]
        failed = next(p for p in polls if p["transient"] is not None)
        self.assertEqual(failed["elapsed_s"], 3)
        self.assertEqual(failed["consecutive_good"], 0)
        self.assertGreaterEqual(now[0], 7)

    def test_guard_io_time_is_counted_in_filter_wait_and_deadline(self):
        session, _, now, _, events = self.make(timeout=10, settle_constants=100)
        calls = [0]
        def guard(deadline):
            calls[0] += 1
            now[0] += 0.4
        session.reference_recovery_guard = guard
        self.assertEqual(self.once_error(session)[0], "complete")
        self.assertEqual(calls[0], 4)
        self.assertAlmostEqual(now[0], 3.4)
        recovered = next(p for kind, p in events if kind == "reference_recovery_recovered")
        self.assertGreaterEqual(recovered["elapsed_s"], 3.4)

    def test_retry_filter_wait_uses_wall_time_and_one_guard_per_second(self):
        session, _, now, _, _ = self.make(timeout=10)
        calls = [0]
        operation_calls = [0]
        def guard(deadline):
            calls[0] += 1
            now[0] += 0.4
        session.reference_recovery_guard = guard
        def operation():
            operation_calls[0] += 1
            if operation_calls[0] == 1:
                raise ReferenceTransientError("test", kind="pair_frequency_mismatch", evidence={})
            started = now[0]
            session._wait(2)
            return now[0] - started
        self.assertAlmostEqual(session.run_reference_operation("whole_sample", operation), 2)
        self.assertLessEqual(calls[0], 7)

    def test_startup_before_harmonic_write_can_recover_without_reapplying_settings(self):
        session, manager, now, _, events = self.make(configure=False)
        xx = manager.resources["FAKE::XX"]
        xx.responses["FREQ?"] = "50030"
        def drift(resource, command):
            if command == "FREQ?" and now[0] >= 2:
                resource.responses[command] = "50027"
        xx.on_query = drift
        session.configure()
        self.assertTrue(session.configured)
        self.assertFalse(session.failed)
        self.assertEqual(manager.resources["FAKE::XY"].writes.count("HARM 2"), 1)
        self.assertEqual(manager.resources["FAKE::XY"].writes.count("RSRC 1"), 1)
        self.assertTrue(any(kind == "reference_recovery_recovered" for kind, _ in events))

    def test_second_optical_restore_with_real_filter_time_fits_shared_45_second_budget(self):
        for phase in ("protected_reference", "source_restored"):
            with self.subTest(phase=phase):
                session, manager, now, waits, events = self.make(time_constant=1.0)
                session.set_point(0.006)
                session.prepare_reference_transition()
                self.assertEqual(session.reference_transition_target, 0.006)
                xx = manager.resources["FAKE::XX"]
                self.assertEqual(xx.responses["SLVL?"], "0.004")
                now[0] = 0.0
                waits.clear()
                events.clear()
                for resource in manager.resources.values():
                    resource.writes.clear()
                injected = [False]
                if phase == "protected_reference":
                    xx.responses["FREQ?"] = "50030"
                def transient(resource, command):
                    if phase == "protected_reference" and command == "FREQ?" and now[0] >= 1:
                        resource.responses[command] = "50027"
                    if phase == "source_restored" and command == "LIAS?" and now[0] >= 30 and not injected[0]:
                        injected[0] = True
                        resource.responses[command] = "16"
                xx.on_query = transient
                session.restore_source_after_reference_transition()
                self.assertFalse(session.failed)
                self.assertFalse(session.reference_transition_pending)
                self.assertEqual(xx.responses["SLVL?"], "0.006")
                start = next(p for kind, p in events if kind == "reference_recovery_started")
                self.assertLess(now[0], start["deadline_monotonic"])
                self.assertEqual(sum(kind == "reference_recovery_started" for kind, _ in events), 1)
                reused = next(p for kind, p in events if kind == "lockin_reference_settling_reused")
                self.assertEqual(reused["settle_s"], 15)
                self.assertFalse(reused["source_settings_written"])
                # The backend avoids a duplicate write when the source was
                # already restored; the post-source dwell remains complete.
                self.assertEqual(xx.writes.count("SLVL 0.006"), 1)
                self.assertFalse(manager.resources["FAKE::XY"].writes)
                completed = next(p for kind, p in events if kind == "reference_recovery_recovered")
                self.assertGreaterEqual(now[0] - (start["deadline_monotonic"] - 45), 32)
                self.assertEqual(completed["settle_s"], 15)

    def test_reused_reference_dwell_still_rejects_new_range_in_fresh_probe(self):
        session, manager, now, _, events = self.make(time_constant=1.0)
        session.set_point(0.006)
        session.prepare_reference_transition()
        xx = manager.resources["FAKE::XX"]
        now[0] = 0.0
        events.clear()
        xx.responses["FREQ?"] = "50030"
        inject_new_range = [False]
        original_event = session.event
        def event(kind, payload):
            original_event(kind, payload)
            if kind == "reference_recovery_recovered" and not inject_new_range[0]:
                inject_new_range[0] = True
                xx.responses["LIAS?"] = "16"
        session.event = event
        def drift(resource, command):
            if command == "FREQ?" and now[0] >= 1:
                resource.responses[command] = "50027"
        xx.on_query = drift
        with self.assertRaises(TimeoutError):
            session.restore_source_after_reference_transition()
        starts = [p for kind, p in events if kind == "reference_recovery_started"]
        self.assertEqual(len(starts), 2)
        self.assertEqual({p["deadline_monotonic"] for p in starts}, {45})
        self.assertTrue(session.failed)
