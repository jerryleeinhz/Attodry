import unittest

from attodry_control.photonics_lockin_config import load_photonics_lockin_config
from attodry_control.photonics_lockin_points import PhotonicsLockinPointSession
from tests.photonics_lockin_helpers import FakeManager, reversed_sine_document, reversed_sine_safety_document


class ReferencePollingTests(unittest.TestCase):
    def make(self, timeout=45, overload_policy="abort"):
        doc = reversed_sine_document()
        doc["photonics_lockin"]["reference_lock_timeout_s"] = timeout
        doc["photonics_lockin"]["overload_policy"] = overload_policy
        cfg = load_photonics_lockin_config("fake.toml", document=doc, safety_document=reversed_sine_safety_document())
        manager = FakeManager(doc)
        manager.resources["FAKE::XY"].responses["SLVL?"] = "0.2"
        now, waits, events = [0.0], [], []

        def sleep(seconds):
            waits.append(seconds)
            now[0] += seconds

        session = PhotonicsLockinPointSession("fake.toml", cfg,
            lambda kind, payload: events.append((kind, payload)), manager_factory=lambda: manager,
            sleep=sleep, clock=lambda: now[0])
        self.addCleanup(session.close)
        session.open()
        session.protect_source()
        session.reference_wait_pending = True
        for resource in manager.resources.values():
            resource.queries.clear()
            resource.writes.clear()
        return session, manager, now, waits, events

    def test_already_locked_passes_immediately_without_setting_writes_or_snapshots(self):
        session, manager, _, waits, events = self.make()
        session.wait_for_reference_lock()
        self.assertEqual(waits, [])
        self.assertFalse(session.reference_wait_pending)
        for resource in manager.resources.values():
            self.assertEqual(resource.writes, [])
            self.assertFalse(any(query.startswith("SNAP") for query in resource.queries))
        finished = [p for k, p in events if k == "lockin_reference_wait_finished"]
        self.assertEqual(finished[0]["polls"], 1)
        self.assertEqual(finished[0]["elapsed_s"], 0)

    def test_both_roles_must_lock_and_history_is_preserved_each_second(self):
        session, manager, now, waits, events = self.make()
        xx, xy = manager.resources.values()

        def xx_state(resource, command):
            if command == "LIAS?":
                resource.responses[command] = "8" if now[0] < 2 else "0"

        def xy_state(resource, command):
            if command == "CUROVLDSTAT?":
                resource.responses[command] = "8" if now[0] < 1 else "0"

        xx.on_query, xy.on_query = xx_state, xy_state
        session.wait_for_reference_lock()
        self.assertEqual(waits, [1, 1])
        statuses = [p for k, p in events if k == "lockin_reference_status"]
        self.assertEqual(len(statuses), 6)
        self.assertFalse(statuses[0]["status"]["locked"])
        self.assertTrue(statuses[-1]["status"]["locked"])
        self.assertTrue(any(k == "lockin_model_io" and p["stage"] == "reference_poll" for k, p in events))

    def test_zero_frequency_while_unlocked_waits_until_timeout_without_sample_io(self):
        session, manager, now, waits, events = self.make()
        xy = manager.resources["FAKE::XY"]
        xy.responses.update({"CUROVLDSTAT?": "8", "FREQEXT?": "0", "FREQDET?": "0"})
        with self.assertRaises(TimeoutError):
            session.wait_for_reference_lock()
        self.assertEqual(now[0], 45)
        self.assertEqual(waits, [1] * 45)
        self.assertNotIn("FREQEXT?", xy.queries)
        self.assertNotIn("FREQDET?", xy.queries)
        self.assertTrue(session.failed)
        self.assertTrue(session.reference_wait_pending)
        self.assertFalse(any(k == "lockin_reference_wait_finished" for k, _ in events))

    def test_one_second_cadence_includes_query_time(self):
        session, manager, now, waits, _ = self.make()
        def slow_state(resource, command):
            if command == "CUROVLDSTAT?":
                resource.responses[command] = "8" if now[0] < 1 else "0"
                now[0] += 0.2
        manager.resources["FAKE::XY"].on_query = slow_state
        session.wait_for_reference_lock()
        self.assertEqual(waits, [0.8])
        self.assertAlmostEqual(now[0], 1.2)

    def test_same_model_identity_change_rejects_before_consuming_role_status(self):
        session, manager, _, waits, events = self.make()
        xy = manager.resources["FAKE::XY"]
        xy.responses["*IDN?"] = "Stanford_Research_Systems,SR865A,replaced,v1.00"
        with self.assertRaisesRegex(ValueError, "identity changed"):
            session.wait_for_reference_lock()
        self.assertNotIn("LIAS?", xy.queries)
        self.assertEqual(waits, [])
        self.assertTrue(session.failed)
        self.assertTrue(session.reference_wait_pending)
        self.assertFalse(any(k == "lockin_reference_wait_finished" for k, _ in events))

    def test_new_sr865a_faults_abort_without_polling_until_clear(self):
        for command, value in (("CUROVLDSTAT?", "16"), ("LIAS?", "16"),
                ("LIAS?", "1"), ("LIAS?", "32"), ("LIAS?", "4"),
                ("*ESR?", "128"), ("ERRS?", "1")):
            with self.subTest(command=command, value=value):
                session, manager, _, waits, events = self.make()
                manager.resources["FAKE::XY"].responses[command] = value
                with self.assertRaises(ValueError):
                    session.wait_for_reference_lock()
                self.assertEqual(waits, [])
                self.assertTrue(session.failed)
                recorded = [p for k, p in events if k == "lockin_reference_status" and p["role"] == "lockin_xy"]
                self.assertEqual(len(recorded), 1)
                self.assertFalse(any(k == "lockin_reference_wait_finished" for k, _ in events))

    def test_sr830_overload_or_unknown_status_aborts_and_retains_evidence(self):
        for value in ("1", "2", "4", "128"):
            with self.subTest(value=value):
                session, manager, _, waits, events = self.make()
                manager.resources["FAKE::XX"].responses["LIAS?"] = value
                with self.assertRaises(ValueError):
                    session.wait_for_reference_lock()
                self.assertEqual(waits, [])
                self.assertTrue(any(k == "lockin_reference_status" for k, _ in events))

    def test_late_clean_status_is_not_accepted(self):
        session, manager, now, _, events = self.make()

        def slow(resource, command):
            if command == "CUROVLDSTAT?":
                now[0] += 46

        manager.resources["FAKE::XY"].on_query = slow
        with self.assertRaises(TimeoutError):
            session.wait_for_reference_lock()
        self.assertTrue(any(k == "lockin_reference_status" and p["elapsed_s"] == 46 for k, p in events))
        self.assertTrue(session.reference_wait_pending)

    def test_frequency_read_time_counts_against_timeout(self):
        session, manager, now, _, events = self.make()

        def slow(resource, command):
            if command == "FREQEXT?":
                now[0] += 46

        manager.resources["FAKE::XY"].on_query = slow
        with self.assertRaises(TimeoutError):
            session.wait_for_reference_lock()
        self.assertFalse(any(k == "lockin_reference_wait_finished" for k, _ in events))

    def test_interrupt_and_wait_failure_keep_pending_and_protected_excitation(self):
        for failure in (KeyboardInterrupt(), RuntimeError("audit unavailable")):
            with self.subTest(failure=type(failure).__name__):
                session, manager, _, _, _ = self.make()
                manager.resources["FAKE::XY"].responses["CUROVLDSTAT?"] = "8"
                def stop(seconds):
                    raise failure
                session.sleep = stop
                with self.assertRaises(type(failure)):
                    session.wait_for_reference_lock()
                self.assertTrue(session.failed)
                self.assertTrue(session.reference_wait_pending)
                self.assertEqual(manager.resources["FAKE::XX"].responses["SLVL?"], "0.004")

    def test_status_audit_failure_prevents_acceptance_and_preserves_model_io(self):
        session, _, _, _, events = self.make()
        def broken(kind, payload):
            if kind == "lockin_reference_status":
                raise RuntimeError("status audit unavailable")
            events.append((kind, payload))
        session.event = broken
        with self.assertRaisesRegex(RuntimeError, "status audit unavailable"):
            session.wait_for_reference_lock()
        self.assertTrue(session.failed)
        self.assertTrue(session.reference_wait_pending)
        self.assertTrue(any(k == "lockin_model_io" and p["stage"] == "reference_wait" for k, p in events))

    def test_zero_timeout_retains_legacy_transition_guard_without_extra_queries(self):
        session, manager, _, waits, events = self.make(timeout=0)
        session.wait_for_reference_lock()
        self.assertFalse(session.reference_wait_pending)
        self.assertEqual(waits, [])
        self.assertFalse(any(k == "lockin_reference_status" for k, _ in events))
        self.assertTrue(all(not r.queries for r in manager.resources.values()))

    def test_record_continue_accepts_only_overloads_for_either_model(self):
        for role, command, value, problem in (
                ("XX", "LIAS?", "1", "input/reserve overload"),
                ("XX", "LIAS?", "2", "filter overload"),
                ("XX", "LIAS?", "4", "output-scale overload"),
                ("XY", "LIAS?", "16", "input/reserve overload"),
                ("XY", "LIAS?", "1", "output-scale overload"),
                ("XY", "CUROVLDSTAT?", "16", "input/reserve overload"),
                ("XY", "CUROVLDSTAT?", "2", "output-scale overload")):
            with self.subTest(role=role, command=command, value=value):
                session, manager, _, waits, events = self.make(overload_policy="record_continue")
                manager.resources[f"FAKE::{role}"].responses[command] = value
                session.wait_for_reference_lock()
                self.assertEqual(waits, [])
                self.assertFalse(session.failed)
                observation = next(p for k, p in events if k == "lockin_reference_status"
                                   and p["role"] == "lockin_" + role.lower())
                self.assertEqual(observation["continued_overload_problems"],
                                 [f"lockin_{role.lower()} {problem}"])
                self.assertEqual(observation["blocking_problems"], [])
                self.assertFalse(observation["status"]["validity"])

    def test_record_continue_overload_plus_unlock_still_waits_without_frequency_queries(self):
        for role in ("XX", "XY"):
            with self.subTest(role=role):
                session, manager, now, waits, events = self.make(overload_policy="record_continue")
                resource = manager.resources[f"FAKE::{role}"]

                def polling_state(resource, command):
                    if command == "LIAS?":
                        resource.responses[command] = str((1 if role == "XX" else 16) | (8 if now[0] < 2 else 0))
                    if role == "XY" and command == "CUROVLDSTAT?":
                        resource.responses[command] = "8" if now[0] < 2 else "0"
                    if command in ("FREQ?", "FREQEXT?", "FREQDET?"):
                        self.assertGreaterEqual(now[0], 2)

                resource.on_query = polling_state
                session.wait_for_reference_lock()
                self.assertEqual(waits, [1, 1])
                self.assertTrue(any(p.get("continued_overload_problems") for k, p in events
                                    if k == "lockin_reference_status"))

    def test_record_continue_cannot_mask_mixed_poll_faults(self):
        for role, command, value in (("XX", "ERRS?", "1"), ("XX", "LIAS?", "129"),
                ("XY", "ERRS?", "1"), ("XY", "LIAS?", "48"), ("XY", "LIAS?", "80"),
                ("XY", "LIAS?", "20"), ("XY", "*ESR?", "128")):
            with self.subTest(role=role, command=command, value=value):
                session, manager, _, waits, events = self.make(overload_policy="record_continue")
                resource = manager.resources[f"FAKE::{role}"]
                resource.responses["LIAS?"] = "1" if role == "XX" else "16"
                resource.responses[command] = value
                with self.assertRaises(ValueError):
                    session.wait_for_reference_lock()
                self.assertTrue(session.failed)
                self.assertEqual(waits, [])
                self.assertFalse(any(k == "lockin_reference_wait_finished" for k, _ in events))
                self.assertFalse(any(query.startswith("FREQ") for query in resource.queries))
