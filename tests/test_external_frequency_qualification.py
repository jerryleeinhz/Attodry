"""External-reference counter drift stays inside bounded early qualification."""
from contextlib import closing
import unittest

from attodry_control.combination_store import open_readonly, validate_accepted_records
from attodry_control.sr830 import Sr830Error
from tests import test_combination_6221 as combination_fixture
from tests import test_external_lockin_points as detector_fixture


class ExternalFrequencyPendingTests(unittest.TestCase):
    fixture = detector_fixture.ExternalLockinTests.fixture

    def ready(self):
        f, config, session, clock, events, table = self.fixture(harmonics=(1,))
        session.open()
        session.configure()
        f.frequency["hz"] = 1217.0
        session.source_changed(2.6e-7, 1217.0, source_record={
            "source_kind": "keithley6221_ac", "current_basis": "source_setting_readback",
            "actual": {"excitation_current_rms_a": 2.6e-7, "excitation_frequency_hz": 1217.0}})
        return f, config, session, clock, events, table

    def counter(self, resource, values, *, after_read=None):
        query = resource.query
        seen = []
        def read(command):
            result = query(command)
            if command == "FREQEXT?":
                result = str(values[min(len(seen), len(values) - 1)])
                seen.append(result)
                if after_read:
                    after_read(len(seen))
            return result
        resource.query = read
        return seen

    def proof(self, events):
        return [p for k, p in events if k == "external_reference_requalification"][-1]

    def test_1217_13_then_target_is_observed_again_before_complete_settle_and_capture(self):
        f, _, session, clock, events, _ = self.ready()
        writes = (list(f.xx.writes), list(f.xy.writes))
        seen = self.counter(f.xy, [1217.13, 1217.0])
        session.qualify()
        proof = self.proof(events)
        self.assertTrue(proof["completed"], proof)
        self.assertTrue(proof["frequency_pending_observed"])
        self.assertEqual(len(proof["polls"]), 2)
        checks = proof["frequency_checks"]
        self.assertEqual([p["matched"] for p in checks], [False, True])
        self.assertEqual(checks[0]["requested_frequency_hz"], 1217.0)
        self.assertEqual(checks[0]["frequency_readback_hz"]["lockin_xy"], 1217.13)
        self.assertIn("captured_at_utc", checks[0])
        raw = checks[0]["observations"]["lockin_xy"]["raw"]
        self.assertEqual(next(r["response"] for r in raw if r["command"] == "FREQEXT?"), "1217.13")
        self.assertTrue(all("completed_at_utc" in row for row in raw))
        self.assertEqual(clock.waits, [0.1, 4.5])
        self.assertEqual(seen, ["1217.13", "1217.0", "1217.0"])
        self.assertEqual(writes, (f.xx.writes, f.xy.writes))
        self.assertFalse(any(k == "lockin_formal_pair" for k, _ in events))
        self.assertTrue(session.sample_point()["clean"])
        self.assertFalse(any(w.startswith(("*CLS", "*RST")) for w in f.xx.writes + f.xy.writes))

    def test_persistent_mismatch_uses_existing_30_second_poll_bound_without_formal_data(self):
        f, _, session, clock, events, _ = self.ready()
        values = [1217.13]
        seen = self.counter(f.xy, values)
        with self.assertRaisesRegex(Sr830Error, "timeout"):
            session.qualify()
        proof = self.proof(events)
        self.assertFalse(proof["completed"])
        self.assertLessEqual(clock.now, 30.000001)
        self.assertLessEqual(len(proof["polls"]), 301)
        self.assertGreater(len(seen), 1)
        self.assertTrue(all(not p["matched"] for p in proof["frequency_checks"]))
        self.assertFalse(any(k == "lockin_formal_pair" for k, _ in events))
        before = (list(f.xx.queries), list(f.xy.queries), list(clock.waits))
        values[0] = 1217.0
        with self.assertRaisesRegex(Sr830Error, "cannot be reused"):
            session.qualify()
        self.assertEqual(before, (f.xx.queries, f.xy.queries, clock.waits))

    def test_matching_counter_returned_after_deadline_cannot_certify_qualification(self):
        f, _, session, clock, events, _ = self.ready()
        def late_return(n):
            if n == 2:
                clock.now = 30.1
        self.counter(f.xy, [1217.13, 1217.0], after_read=late_return)
        with self.assertRaisesRegex(Sr830Error, "timeout"):
            session.qualify()
        proof = self.proof(events)
        self.assertFalse(proof["completed"])
        self.assertTrue(proof["frequency_checks"][-1]["matched"])
        self.assertEqual(clock.waits, [0.1])
        self.assertFalse(any(k == "lockin_formal_pair" for k, _ in events))

    def test_pending_proof_audit_failure_prevents_capture_or_reuse(self):
        f, _, session, clock, events, _ = self.ready()
        self.counter(f.xy, [1217.13, 1217.0])
        event = session.event
        def failed_event(kind, payload):
            if kind == "external_reference_requalification":
                raise OSError("frequency qualification audit failed")
            event(kind, payload)
        session.event = failed_event
        with self.assertRaisesRegex(OSError, "audit failed"):
            session.qualify()
        self.assertFalse(session.point_record["reference_requalification"]["completed"])
        self.assertFalse(any(k == "lockin_formal_pair" for k, _ in events))
        with self.assertRaisesRegex(Sr830Error, "cannot be reused"):
            session.qualify()

    def test_unchanged_success_path_keeps_two_coordinate_reads_and_one_shared_wait(self):
        f, _, session, clock, events, _ = self.ready()
        before = (len(f.xy.queries), list(f.xx.writes), list(f.xy.writes))
        session.qualify()
        self.assertEqual(f.xy.queries[before[0]:].count("FREQEXT?"), 2)
        self.assertEqual(clock.waits, [4.5])
        self.assertEqual(before[1:], (f.xx.writes, f.xy.writes))
        self.assertFalse(self.proof(events).get("frequency_pending_observed", False))
        session.qualify()
        self.assertEqual(clock.waits, [4.5])

    def test_in_tolerance_frequency_needs_no_pending_wait(self):
        f, _, session, clock, events, _ = self.ready()
        self.counter(f.xy, [1217.12])
        session.qualify()
        self.assertEqual(clock.waits, [4.5])
        self.assertFalse(self.proof(events).get("frequency_pending_observed", False))

    def test_invalid_or_impossible_counter_readbacks_fail_immediately(self):
        for value in ("nan", "inf", "not-a-number", "0", "-1", "1000000000"):
            with self.subTest(value=value):
                f, _, session, clock, events, _ = self.ready()
                self.counter(f.xy, [value])
                with self.assertRaises((Sr830Error, ValueError)):
                    session.qualify()
                self.assertEqual(clock.waits, [])
                self.assertEqual(len(self.proof(events)["polls"]), 1)
                self.assertFalse(self.proof(events).get("frequency_pending_observed", False))

    def test_transport_failure_is_not_retried_as_a_frequency_mismatch(self):
        f, _, session, clock, events, _ = self.ready()
        query = f.xy.query
        def failed(command):
            if command == "FREQEXT?":
                raise OSError("frequency transport failed")
            return query(command)
        f.xy.query = failed
        with self.assertRaisesRegex(Sr830Error, "frequency transport failed|FREQEXT"):
            session.qualify()
        self.assertEqual(clock.waits, [])
        self.assertFalse(self.proof(events).get("frequency_pending_observed", False))

    def test_initial_mismatch_with_nonreference_status_cannot_open_pending_window(self):
        cases = (("xy", "CUROVLDSTAT?", "1"), ("xy", "CUROVLDSTAT?", "16"),
                 ("xy", "LIAS?", "1"), ("xy", "LIAS?", "16"),
                 ("xy", "LIAS?", "32"), ("xy", "ERRS?", "1"),
                 ("xy", "*ESR?", "128"), ("xx", "LIAS?", "1"))
        for role, command, value in cases:
            with self.subTest(role=role, command=command, value=value):
                f, _, session, clock, events, _ = self.ready()
                getattr(f, role).responses[command] = value
                self.counter(f.xy, [1217.13, 1217.0])
                with self.assertRaises(Sr830Error):
                    session.qualify()
                self.assertEqual(clock.waits, [])

    def test_fault_between_pending_polls_is_not_consumed_even_if_frequency_recovers(self):
        cases = (("xy", "CUROVLDSTAT?", "1"), ("xy", "CUROVLDSTAT?", "16"),
                 ("xy", "LIAS?", "1"), ("xy", "LIAS?", "32"),
                 ("xy", "ERRS?", "1"), ("xy", "*ESR?", "64"),
                 ("xy", "CUROVLDSTAT?", "4"), ("xx", "LIAS?", "2"))
        for role, command, value in cases:
            with self.subTest(role=role, command=command, value=value):
                f, _, session, clock, events, _ = self.ready()
                self.counter(f.xy, [1217.13, 1217.0])
                sleep = clock.sleep
                def changed(seconds):
                    sleep(seconds)
                    getattr(f, role).responses[command] = value
                session.base_sleep = changed
                with self.assertRaises(Sr830Error):
                    session.qualify()
                self.assertEqual(clock.waits, [0.1])
                self.assertFalse(self.proof(events)["completed"])
                self.assertFalse(any(k == "lockin_formal_pair" for k, _ in events))

    def test_mode_edge_or_settings_drift_between_pending_polls_still_rejects(self):
        for command, value in (("RSRC?", "0"), ("RTRG?", "2"),
                               ("SCAL?", "9"), ("IRNG?", "2"), ("HARM?", "2")):
            with self.subTest(command=command):
                f, _, session, clock, events, _ = self.ready()
                self.counter(f.xy, [1217.13, 1217.0])
                sleep = clock.sleep
                def changed(seconds):
                    sleep(seconds)
                    f.xy.responses[command] = value
                session.base_sleep = changed
                with self.assertRaises(Sr830Error):
                    session.qualify()
                self.assertEqual(clock.waits, [0.1])
                self.assertFalse(self.proof(events)["completed"])

    def test_persistent_unlock_remains_bounded_even_after_frequency_pending(self):
        f, _, session, clock, events, _ = self.ready()
        self.counter(f.xy, [1217.13, 1217.0])
        sleep = clock.sleep
        def unlocked(seconds):
            sleep(seconds)
            f.xy.responses["CUROVLDSTAT?"] = "8"
        session.base_sleep = unlocked
        with self.assertRaises(Sr830Error):
            session.qualify()
        self.assertLessEqual(clock.now, 30.000001)
        self.assertFalse(self.proof(events)["completed"])

    def test_postsettle_frequency_drift_has_no_pending_retry(self):
        f, _, session, clock, events, _ = self.ready()
        seen = self.counter(f.xy, [1217.0, 1217.13])
        with self.assertRaisesRegex(Sr830Error, "1217.13"):
            session.qualify()
        self.assertEqual(clock.waits, [4.5])
        self.assertEqual(seen, ["1217.0", "1217.13"])
        self.assertFalse(self.proof(events).get("frequency_pending_observed", False))

    def test_formal_frequency_drift_does_not_reopen_reference_qualification(self):
        f, _, session, clock, events, _ = self.ready()
        session.qualify()
        self.counter(f.xy, [1217.13])
        with self.assertRaisesRegex(Sr830Error, "1217.13"):
            session.sample_point()
        self.assertEqual(clock.waits, [4.5])
        self.assertEqual(len([p for k, p in events if k == "external_reference_requalification"]), 1)


class FrequencyPendingCombinationTests(unittest.TestCase):
    def ready(self):
        helper = combination_fixture.Combination6221Tests("test_full_grid_native_pairs_no_detector_source_writes_and_cleanup")
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        text = helper.text.replace('frequency_points_hz = [17.777, 200.0]',
                                   'frequency_points_hz = [20.0, 1217.0]')
        text = text.replace('xx_harmonics = [1, 2]', 'xx_harmonics = [1]')
        text = text.replace('xy_harmonics = [1, 2]', 'xy_harmonics = [1]')
        helper.path.write_text(text, encoding="utf-8")
        query = helper.xy.query
        injected = []
        def read(command):
            result = query(command)
            if command == "FREQEXT?" and helper.frequency["hz"] == 1217.0 and not injected:
                injected.append(True)
                return "1217.13"
            return result
        helper.xy.query = read
        return helper, injected

    def test_multifrequency_archive_records_pending_readback_then_valid_formal_samples(self):
        helper, injected = self.ready()
        result = helper.execute()
        self.assertTrue(injected)
        self.assertEqual(result["status"], "completed", result)
        proofs = [p for k, p in helper.events() if k == "external_reference_requalification"
                  and p.get("frequency_pending_observed")]
        self.assertEqual(len(proofs), 1)
        self.assertEqual([p["matched"] for p in proofs[0]["frequency_checks"]], [False, True])
        self.assertEqual(proofs[0]["frequency_checks"][0]["frequency_readback_hz"]["lockin_xy"], 1217.13)
        with closing(open_readonly(helper.database)) as db:
            self.assertEqual(validate_accepted_records(db, "current-run")["accepted_conditions"], 4)
        self.assertEqual(helper.source.commands.count("SOUR:WAVE:INIT"), 4)
        self.assertFalse(any(w.startswith(("*CLS", "*RST")) for w in helper.xx.writes + helper.xy.writes))

    def test_compliance_during_pending_wait_is_not_accepted_as_formal_data(self):
        helper, injected = self.ready()
        sleep = helper.clock.sleep
        def compliance(seconds):
            sleep(seconds)
            if injected and seconds <= 0.100001:
                helper.source.state["STAT:MEAS:EVEN"] = "8"
        helper.clock.sleep = compliance
        result = helper.execute()
        self.assertTrue(injected)
        self.assertEqual(result["status"], "failed")
        self.assertIn("compliance", result["error"])
        with closing(open_readonly(helper.database)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM combination_attempts WHERE status='accepted'").fetchone()[0], 2)
        self.assertEqual(helper.source.state["OUTP:STAT"], "OFF")


if __name__ == "__main__":
    unittest.main()
