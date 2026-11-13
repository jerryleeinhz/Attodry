"""Recovered native output latches require one clean, complete new window."""
from contextlib import closing
from dataclasses import replace
import unittest

from attodry_control.combination_store import open_readonly, validate_accepted_records
from attodry_control.sr830 import Sr830Error
from tests import test_combination_6221 as combination_fixture
from tests import test_external_lockin_points as detector_fixture


class RecoveredOutputLatchTests(unittest.TestCase):
    fixture = detector_fixture.ExternalLockinTests.fixture
    start = detector_fixture.ExternalLockinTests.start

    def ready(self, policy="record_continue"):
        fixture = self.fixture(harmonics=(1,))
        f, config, session, clock, events, _ = fixture
        config.lockin_acquisition = replace(config.lockin_acquisition, overload_policy=policy)
        self.start(f, session)
        query = f.xy.query
        def read_to_clear(command):
            answer = query(command)
            if command == "LIAS?":
                f.xy.responses[command] = "0"
            return answer
        f.xy.query = read_to_clear
        return fixture

    def inject(self, f, session, clock, *, candidate=None, recheck=None):
        """Apply events during the first and the optional fresh settling windows."""
        sleep = clock.sleep
        calls = []
        def changed(seconds):
            sleep(seconds)
            calls.append(seconds)
            if len(calls) == 1:
                f.xy.responses.update({"CUROVLDSTAT?": "0", "LIAS?": "1537"})
                if candidate:
                    candidate()
            elif len(calls) == 2 and recheck:
                recheck()
        session.base_sleep = changed
        return calls

    def proofs(self, events):
        return [payload for kind, payload in events
                if kind == "external_output_latch_requalification"]

    def test_recovered_1537_is_archived_then_one_fresh_full_window_precedes_formal_data(self):
        f, _, session, clock, events, _ = self.ready()
        writes = (list(f.xx.writes), list(f.xy.writes))
        self.inject(f, session, clock)
        session.qualify()
        self.assertEqual(clock.waits, [4.5, 4.5])
        self.assertEqual(writes, (f.xx.writes, f.xy.writes))
        proof = self.proofs(events)[0]
        self.assertTrue(proof["completed"], proof)
        self.assertFalse(proof["formal_sample"])
        self.assertEqual(proof["required_settle_s"], 4.5)
        native = proof["candidate"]["roles"]["lockin_xy"]["lia_status"]["native_status"]
        self.assertEqual(native["current_status_raw"], 0)
        self.assertEqual(native["lia_status_raw"], 1537)
        self.assertEqual(next(r["response"] for r in native["raw"] if r["command"] == "LIAS?"), "1537")
        self.assertIn("observed_at_utc", native)
        self.assertEqual(proof["recheck"]["roles"]["lockin_xy"]["lia_status"]["raw"], 0)
        self.assertFalse(any(kind == "lockin_formal_pair" for kind, _ in events))
        self.assertTrue(session.sample_point()["clean"])
        self.assertFalse(any(command.startswith(("*CLS", "*RST"))
                             for command in f.xx.writes + f.xy.writes))

    def test_normal_status_has_identical_queries_writes_and_waits_under_each_policy(self):
        traces = []
        for policy in ("record_continue", "abort", "continue_unselected"):
            f, _, session, clock, events, _ = self.ready(policy)
            before = (len(f.xx.queries), len(f.xy.queries), len(f.xx.writes), len(f.xy.writes))
            session.qualify()
            traces.append((f.xx.queries[before[0]:], f.xy.queries[before[1]:],
                           f.xx.writes[before[2]:], f.xy.writes[before[3]:], list(clock.waits)))
            self.assertEqual(self.proofs(events), [])
        self.assertEqual(traces[0], traces[1])
        self.assertEqual(traces[0], traces[2])

    def test_abort_and_continue_unselected_do_not_recheck_recovered_output_latch(self):
        for policy in ("abort", "continue_unselected"):
            with self.subTest(policy=policy):
                f, _, session, clock, events, _ = self.ready(policy)
                self.inject(f, session, clock)
                with self.assertRaisesRegex(Sr830Error, "output overload"):
                    session.qualify()
                self.assertEqual(clock.waits, [4.5])
                self.assertEqual(self.proofs(events), [])

    def test_present_output_input_or_unknown_state_never_enters_recheck(self):
        for current in ("1", "16", "8", "4", "not-a-number"):
            with self.subTest(current=current):
                f, _, session, clock, events, _ = self.ready()
                self.inject(f, session, clock,
                            candidate=lambda: f.xy.responses.update({"CUROVLDSTAT?": current}))
                with self.assertRaises((Sr830Error, ValueError)):
                    session.qualify()
                self.assertEqual(clock.waits, [4.5])
                self.assertEqual(self.proofs(events), [])

    def test_other_latches_errors_and_xx_fault_cannot_be_reclassified_as_output_history(self):
        cases = ({"LIAS?": str(1537 | bit)} for bit in (8, 16, 32, 128, 0x8000))
        cases = list(cases) + [{"ERRS?": "1"}, {"*ESR?": "128"}, {"*ESR?": "64"}]
        for extra in cases:
            with self.subTest(extra=extra):
                f, _, session, clock, events, _ = self.ready()
                self.inject(f, session, clock, candidate=lambda: f.xy.responses.update(extra))
                with self.assertRaises(Sr830Error):
                    session.qualify()
                self.assertEqual(clock.waits, [4.5])
                self.assertEqual(self.proofs(events), [])
        f, _, session, clock, events, _ = self.ready()
        self.inject(f, session, clock, candidate=lambda: f.xx.responses.update({"LIAS?": "1"}))
        with self.assertRaises(Sr830Error):
            session.qualify()
        self.assertEqual(self.proofs(events), [])

    def test_repeated_latch_gets_no_third_window_even_if_qualify_is_called_again(self):
        f, _, session, clock, events, _ = self.ready()
        self.inject(f, session, clock,
                    recheck=lambda: f.xy.responses.update({"LIAS?": "1537"}))
        with self.assertRaisesRegex(Sr830Error, "output overload"):
            session.qualify()
        self.assertEqual(clock.waits, [4.5, 4.5])
        proof = self.proofs(events)[0]
        self.assertFalse(proof["completed"])
        self.assertTrue(proof["error"])
        f.xy.responses["LIAS?"] = "1537"
        with self.assertRaises(Sr830Error):
            session.qualify()
        self.assertEqual(clock.waits, [4.5, 4.5])
        self.assertEqual(len(self.proofs(events)), 1)
        f.xy.responses["LIAS?"] = "0"
        with self.assertRaisesRegex(Sr830Error, "cannot be reused"):
            session.qualify()
        self.assertEqual(clock.waits, [4.5, 4.5])

    def test_final_recheck_rejects_input_or_filter_latch_despite_record_continue(self):
        for resource_name, value in (("xy", "16"), ("xy", "32"), ("xx", "1"), ("xx", "2")):
            with self.subTest(resource=resource_name, value=value):
                f, _, session, clock, events, _ = self.ready()
                self.inject(f, session, clock,
                            recheck=lambda: getattr(f, resource_name).responses.update({"LIAS?": value}))
                with self.assertRaises(Sr830Error):
                    session.qualify()
                self.assertFalse(self.proofs(events)[0]["completed"])
                self.assertFalse(any(kind == "lockin_formal_pair" for kind, _ in events))

    def test_current_overload_unlock_or_error_during_recheck_fails(self):
        for extra in ({"CUROVLDSTAT?": "1"}, {"CUROVLDSTAT?": "8"}, {"ERRS?": "1"}):
            with self.subTest(extra=extra):
                f, _, session, clock, events, _ = self.ready()
                self.inject(f, session, clock, recheck=lambda: f.xy.responses.update(extra))
                with self.assertRaises(Sr830Error):
                    session.qualify()
                self.assertFalse(self.proofs(events)[0]["completed"])

    def test_recheck_verifies_frequency_and_settings_after_the_new_wait(self):
        for kind in ("frequency", "sensitivity", "input_range", "harmonic"):
            with self.subTest(kind=kind):
                f, _, session, clock, events, _ = self.ready()
                def changed():
                    if kind == "frequency":
                        f.frequency["hz"] = 200
                    else:
                        command = {"sensitivity": "SCAL?", "input_range": "IRNG?", "harmonic": "HARM?"}[kind]
                        f.xy.responses[command] = str(int(f.xy.responses[command]) + 1)
                self.inject(f, session, clock, recheck=changed)
                with self.assertRaises(Sr830Error):
                    session.qualify()
                self.assertFalse(self.proofs(events)[0]["completed"])

    def test_transport_and_audit_failure_prevent_formal_capture(self):
        for kind in ("transport", "audit"):
            with self.subTest(kind=kind):
                f, _, session, clock, events, _ = self.ready()
                if kind == "transport":
                    query = f.xy.query
                    def failed(command):
                        if len(clock.waits) >= 2 and command == "CUROVLDSTAT?":
                            raise OSError("recheck transport failed")
                        return query(command)
                    f.xy.query = failed
                else:
                    event = session.event
                    def failed_event(name, payload):
                        if name == "external_output_latch_requalification":
                            raise OSError("recheck audit failed")
                        event(name, payload)
                    session.event = failed_event
                self.inject(f, session, clock)
                with self.assertRaises((Sr830Error, OSError)):
                    session.qualify()
                self.assertFalse(any(name == "lockin_formal_pair" for name, _ in events))
                self.assertFalse(session.point_record["output_latch_requalification"]["completed"])
                f.xy.responses.update({"CUROVLDSTAT?": "0", "LIAS?": "0"})
                with self.assertRaisesRegex(Sr830Error, "cannot be reused"):
                    session.qualify()

    def test_unchanged_source_cannot_open_a_new_output_latch_recovery_window(self):
        f, _, session, clock, events, _ = self.ready()
        session.qualify()
        session.source_changed(1e-6, 17.777, index=1)
        f.xy.responses["LIAS?"] = "1537"
        with self.assertRaisesRegex(Sr830Error, "output overload"):
            session.qualify()
        self.assertEqual(clock.waits, [4.5])
        self.assertEqual(self.proofs(events), [])

    def test_each_real_source_transition_can_have_one_independent_recheck(self):
        f, _, session, clock, events, _ = self.ready()
        self.inject(f, session, clock)
        session.qualify()
        session.source_changed(2e-6, 17.777, index=1)
        self.inject(f, session, clock)
        session.qualify()
        session.qualify()
        self.assertEqual(clock.waits, [4.5, 4.5, 4.5, 4.5])
        self.assertEqual(len(self.proofs(events)), 2)
        self.assertTrue(all(p["completed"] for p in self.proofs(events)))

    def test_formal_output_fault_after_successful_recheck_is_still_blocking(self):
        f, _, session, clock, events, _ = self.ready()
        self.inject(f, session, clock)
        session.qualify()
        f.xy.responses["LIAS?"] = "1"
        with self.assertRaises(Sr830Error):
            session.sample_point()
        self.assertEqual(len(self.proofs(events)), 1)

    def test_cleanup_output_latch_after_successful_recheck_is_still_blocking(self):
        f, _, session, clock, events, _ = self.ready()
        self.inject(f, session, clock)
        session.qualify()
        session.source_stopped({"verified": True, "output_off_confirmed": True})
        sleep = clock.sleep
        def cleanup_latch(seconds):
            sleep(seconds)
            f.xy.responses["LIAS?"] = "1"
        session.base_sleep = cleanup_latch
        result = session.cleanup()
        self.assertFalse(result["verified"])
        self.assertTrue(any("output overload" in p for p in result["errors"]))
        self.assertEqual(len(self.proofs(events)), 1)


class OutputLatchCombinationTests(unittest.TestCase):
    def ready(self):
        helper = combination_fixture.Combination6221Tests("test_full_grid_native_pairs_no_detector_source_writes_and_cleanup")
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        query = helper.xy.query
        def read_to_clear(command):
            value = query(command)
            if command == "LIAS?":
                helper.xy.responses[command] = "0"
            return value
        helper.xy.query = read_to_clear
        original_sleep = helper.clock.sleep
        injected = []
        def sleep(seconds):
            original_sleep(seconds)
            if seconds >= 4 and helper.frequency["hz"] == 200.0 and not injected:
                helper.xy.responses.update({"CUROVLDSTAT?": "0", "LIAS?": "1537"})
                injected.append(True)
        helper.clock.sleep = sleep
        return helper, injected

    def test_frequency_transition_recheck_persists_raw_proof_with_valid_formal_archive(self):
        helper, injected = self.ready()
        result = helper.execute()
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(injected)
        events = helper.events()
        proofs = [p for k, p in events if k == "external_output_latch_requalification"]
        self.assertEqual(len(proofs), 1)
        self.assertTrue(proofs[0]["completed"])
        self.assertEqual(proofs[0]["candidate"]["roles"]["lockin_xy"]["lia_status"]["raw"], 1537)
        with closing(open_readonly(helper.database)) as db:
            self.assertEqual(validate_accepted_records(db, "current-run")["accepted_conditions"], 4)
        self.assertTrue(result["cleanup"]["clean"])
        self.assertEqual(helper.source.commands.count("SOUR:WAVE:INIT"), 4)
        self.assertFalse(any(c.startswith(("*CLS", "*RST")) for c in helper.xx.writes + helper.xy.writes))

    def test_source_compliance_during_output_latch_recheck_still_rejects_condition(self):
        helper, injected = self.ready()
        sleep = helper.clock.sleep
        after_candidate = []
        def compliance(seconds):
            already = bool(injected)
            sleep(seconds)
            if already and seconds >= 4 and not after_candidate:
                helper.source.state["STAT:MEAS:EVEN"] = "8"
                after_candidate.append(True)
        helper.clock.sleep = compliance
        result = helper.execute()
        self.assertTrue(after_candidate)
        self.assertEqual(result["status"], "failed")
        self.assertIn("compliance", result["error"])
        with closing(open_readonly(helper.database)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM combination_attempts WHERE status='accepted'").fetchone()[0], 2)
        self.assertEqual(helper.source.state["OUTP:STAT"], "OFF")


if __name__ == "__main__":
    unittest.main()
