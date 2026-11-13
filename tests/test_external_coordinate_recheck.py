"""Bounded frequency re-observation before capture never retries formal data."""
from contextlib import closing
import unittest

from attodry_control.combination_store import open_readonly, validate_accepted_records
from attodry_control.sr830 import Sr830Error
from tests import test_combination_6221 as combination_fixture
from tests import test_external_frequency_qualification as reference_fixture


class ExternalCoordinateRecheckTests(unittest.TestCase):
    fixture = reference_fixture.ExternalFrequencyPendingTests.fixture
    ready = reference_fixture.ExternalFrequencyPendingTests.ready

    def prepared(self):
        result = self.ready()
        result[2].qualify()
        return result

    def counter(self, resource, command, values):
        query = resource.query
        seen = []
        def read(wire):
            answer = query(wire)
            if wire == command:
                answer = str(values[min(len(seen), len(values) - 1)])
                seen.append(answer)
            return answer
        resource.query = read
        return seen

    def pair(self, f, *, persistent=False):
        xx = [1216.92] if persistent else [1216.92, 1217.0]
        xy = [1217.08] if persistent else [1217.08, 1217.0]
        return (self.counter(f.xx, "FREQ?", xx), self.counter(f.xy, "FREQEXT?", xy))

    def proofs(self, events):
        return [p for kind, p in events if kind == "external_pre_capture_frequency_recheck"]

    def test_individually_in_tolerance_pair_mismatch_is_reobserved_before_capture(self):
        f, _, session, clock, events, _ = self.prepared()
        writes = (list(f.xx.writes), list(f.xy.writes))
        xx, xy = self.pair(f)
        read = session.sample_point()
        self.assertTrue(read["clean"], read["problems"])
        self.assertEqual(clock.waits, [4.5, 1.0])
        proof = self.proofs(events)[0]
        self.assertTrue(proof["completed"], proof)
        self.assertFalse(proof["formal_sample"])
        checks = proof["checks"]
        self.assertEqual([p["matched"] for p in checks], [False, True])
        self.assertEqual(checks[0]["frequency_readback_hz"], {"lockin_xx": 1216.92, "lockin_xy": 1217.08})
        self.assertIn("xx/xy frequency readbacks do not match", checks[0]["error"])
        self.assertTrue(checks[0]["observations"]["lockin_xy"]["raw"])
        self.assertEqual(xx, ["1216.92", "1217.0"])
        self.assertEqual(xy[:2], ["1217.08", "1217.0"])
        self.assertEqual(writes, (f.xx.writes, f.xy.writes))
        formal_index = next(i for i, (kind, _) in enumerate(events) if kind == "lockin_formal_pair")
        proof_index = next(i for i, (kind, _) in enumerate(events) if kind == "external_pre_capture_frequency_recheck")
        self.assertLess(proof_index, formal_index)

    def test_persistent_mismatch_gets_three_observations_and_no_formal_capture(self):
        f, _, session, clock, events, _ = self.prepared()
        xx, xy = self.pair(f, persistent=True)
        with self.assertRaisesRegex(Sr830Error, "frequency readbacks do not match"):
            session.sample_point()
        self.assertEqual(clock.waits, [4.5, 1.0, 1.0])
        self.assertEqual(len(xx), 3)
        self.assertEqual(len(xy), 3)
        proof = self.proofs(events)[0]
        self.assertFalse(proof["completed"])
        self.assertEqual(len(proof["checks"]), 3)
        self.assertFalse(any(kind == "lockin_formal_pair" for kind, _ in events))

    def test_failed_coordinate_proof_cannot_be_rearmed_after_counters_recover(self):
        f, _, session, clock, events, _ = self.prepared()
        query_xx, query_xy = f.xx.query, f.xy.query
        self.pair(f, persistent=True)
        with self.assertRaises(Sr830Error):
            session.sample_point()
        f.xx.query, f.xy.query = query_xx, query_xy
        before = (list(f.xx.queries), list(f.xy.queries), list(clock.waits))
        with self.assertRaisesRegex(Sr830Error, "cannot be reused|already|exhausted"):
            session.sample_point()
        self.assertEqual(before, (f.xx.queries, f.xy.queries, clock.waits))
        self.assertEqual(len(self.proofs(events)), 1)

    def test_successful_gate_cannot_open_another_recheck_for_same_sample_ordinal(self):
        f, _, session, clock, events, _ = self.prepared()
        self.pair(f)
        session._read_pre_capture_coordinates()
        self.assertEqual(session.sample_count, 0)
        self.assertEqual(clock.waits, [4.5, 1.0])
        self.pair(f, persistent=True)
        with self.assertRaises(Sr830Error):
            session._read_pre_capture_coordinates()
        self.assertEqual(clock.waits, [4.5, 1.0])
        self.assertEqual(len(self.proofs(events)), 1)

    def test_next_legitimate_sample_has_its_own_bounded_gate(self):
        f, _, session, clock, events, _ = self.prepared()
        self.pair(f)
        self.assertTrue(session.sample_point()["clean"])
        self.assertEqual(session.sample_count, 1)
        self.pair(f)
        self.assertTrue(session.sample_point()["clean"])
        self.assertEqual(session.sample_count, 2)
        self.assertEqual([p["sample_index"] for p in self.proofs(events)], [0, 1])
        self.assertEqual(clock.waits.count(1.0), 2)
        self.assertTrue(all(p["completed"] for p in self.proofs(events)))

    def test_source_context_cannot_change_during_coordinate_recheck(self):
        for kind in ("inactive", "target", "source_evidence"):
            with self.subTest(kind=kind):
                f, _, session, clock, events, _ = self.prepared()
                self.pair(f)
                sleep = clock.sleep
                def changed(seconds):
                    sleep(seconds)
                    if kind == "inactive":
                        session.source_active = False
                    elif kind == "target":
                        session.target_frequency_hz = 1218.0
                    else:
                        session.point_record["excitation_source"]["actual"]["excitation_frequency_hz"] = 1218.0
                session.base_sleep = changed
                with self.assertRaisesRegex(Sr830Error, "Source or point changed"):
                    session.sample_point()
                self.assertEqual(clock.waits, [4.5, 1.0])
                self.assertFalse(self.proofs(events)[0]["completed"])
                self.assertFalse(any(kind == "lockin_formal_pair" for kind, _ in events))

    def test_coordinate_audit_failure_prevents_capture_and_sample_reuse(self):
        f, _, session, clock, events, _ = self.prepared()
        self.pair(f)
        event = session.event
        def failed(kind, payload):
            if kind == "external_pre_capture_frequency_recheck":
                raise OSError("coordinate recheck audit failed")
            event(kind, payload)
        session.event = failed
        with self.assertRaisesRegex(OSError, "audit failed"):
            session.sample_point()
        proof = session.point_record["pre_capture_frequency_rechecks"][0]
        self.assertFalse(proof["completed"])
        self.assertFalse(any(kind == "lockin_formal_pair" for kind, _ in events))
        with self.assertRaisesRegex(Sr830Error, "cannot be reused"):
            session.sample_point()

    def test_initial_mismatch_audit_failure_consumes_budget_before_any_retry(self):
        f, _, session, clock, events, _ = self.prepared()
        query_xx, query_xy = f.xx.query, f.xy.query
        self.pair(f)
        event = session.event
        def failed(kind, payload):
            if kind == "external_reference_frequency_mismatch":
                raise OSError("initial mismatch audit failed")
            event(kind, payload)
        session.event = failed
        with self.assertRaisesRegex(OSError, "initial mismatch audit failed"):
            session.sample_point()
        self.assertEqual(clock.waits, [4.5])
        self.assertFalse(any(kind == "lockin_formal_pair" for kind, _ in events))
        f.xx.query, f.xy.query, session.event = query_xx, query_xy, event
        before = (list(f.xx.queries), list(f.xy.queries), list(clock.waits))
        with self.assertRaisesRegex(Sr830Error, "cannot be reused"):
            session.sample_point()
        self.assertEqual(before, (f.xx.queries, f.xy.queries, clock.waits))

    def test_normal_capture_keeps_existing_coordinate_and_snapshot_query_budget(self):
        f, _, session, clock, events, _ = self.prepared()
        before = (len(f.xx.queries), len(f.xy.queries), list(f.xx.writes), list(f.xy.writes))
        self.assertTrue(session.sample_point()["clean"])
        self.assertEqual(f.xx.queries[before[0]:].count("FREQ?"), 1)
        self.assertEqual(f.xx.queries[before[0]:].count("SNAP? 1,2,3,4,9"), 1)
        self.assertEqual(f.xy.queries[before[1]:].count("FREQEXT?"), 2)
        self.assertEqual(clock.waits, [4.5])
        self.assertEqual(before[2:], (f.xx.writes, f.xy.writes))
        self.assertEqual(self.proofs(events), [])

    def test_invalid_coordinate_values_are_not_retried(self):
        for value in ("nan", "inf", "not-a-number", "0", "1000000000"):
            with self.subTest(value=value):
                f, _, session, clock, events, _ = self.prepared()
                seen = self.counter(f.xy, "FREQEXT?", [value, 1217.0])
                with self.assertRaises((Sr830Error, ValueError)):
                    session.sample_point()
                self.assertEqual(seen, [value])
                self.assertEqual(clock.waits, [4.5])
                self.assertEqual(self.proofs(events), [])

    def test_ongoing_or_latched_native_fault_blocks_initial_reobservation(self):
        cases = (("xy", "CUROVLDSTAT?", "1"), ("xy", "CUROVLDSTAT?", "8"),
                 ("xy", "CUROVLDSTAT?", "16"), ("xy", "LIAS?", "1"),
                 ("xy", "LIAS?", "8"), ("xy", "LIAS?", "32"),
                 ("xy", "ERRS?", "1"), ("xy", "*ESR?", "128"), ("xx", "LIAS?", "1"))
        for role, command, value in cases:
            with self.subTest(role=role, command=command):
                f, _, session, clock, events, _ = self.prepared()
                self.pair(f)
                getattr(f, role).responses[command] = value
                with self.assertRaises(Sr830Error):
                    session.sample_point()
                self.assertEqual(clock.waits, [4.5])
                self.assertFalse(any(kind == "lockin_formal_pair" for kind, _ in events))

    def test_native_fault_between_reobservations_rejects_even_when_next_pair_matches(self):
        for role, command, value in (("xy", "CUROVLDSTAT?", "1"), ("xy", "LIAS?", "8"),
                                      ("xy", "ERRS?", "1"), ("xx", "LIAS?", "2")):
            with self.subTest(role=role, command=command):
                f, _, session, clock, events, _ = self.prepared()
                xx, xy = self.pair(f)
                sleep = clock.sleep
                def failed(seconds):
                    sleep(seconds)
                    getattr(f, role).responses[command] = value
                session.base_sleep = failed
                with self.assertRaises(Sr830Error):
                    session.sample_point()
                self.assertEqual(clock.waits, [4.5, 1.0])
                self.assertFalse(any(kind == "lockin_formal_pair" for kind, _ in events))

    def test_setting_change_during_reobservation_wait_is_not_accepted(self):
        for command, value in (("RSRC?", "0"), ("RTRG?", "2"), ("SCAL?", "9"),
                               ("IRNG?", "2"), ("HARM?", "2")):
            with self.subTest(command=command):
                f, _, session, clock, events, _ = self.prepared()
                self.pair(f)
                sleep = clock.sleep
                def changed(seconds):
                    sleep(seconds)
                    f.xy.responses[command] = value
                session.base_sleep = changed
                with self.assertRaises(Sr830Error):
                    session.sample_point()
                self.assertEqual(clock.waits, [4.5, 1.0])
                self.assertFalse(self.proofs(events)[0]["completed"])

    def test_transport_failure_on_reobservation_is_not_retried_again(self):
        f, _, session, clock, events, _ = self.prepared()
        self.pair(f)
        query = f.xy.query
        def failed(command):
            if command == "FREQEXT?" and len(clock.waits) > 1:
                raise OSError("coordinate transport failed")
            return query(command)
        f.xy.query = failed
        with self.assertRaisesRegex(Sr830Error, "FREQEXT|transport failed"):
            session.sample_point()
        self.assertEqual(clock.waits, [4.5, 1.0])
        self.assertFalse(self.proofs(events)[0]["completed"])

    def test_formal_snapshot_fault_never_opens_coordinate_reobservation(self):
        f, _, session, clock, events, _ = self.prepared()
        f.xx.snapshots = ["1e-6,2e-7,1.0198e-6,11.31,0"]
        before = f.xx.queries.count("SNAP? 1,2,3,4,9")
        with self.assertRaises((Sr830Error, ValueError)):
            session.sample_point()
        self.assertEqual(f.xx.queries.count("SNAP? 1,2,3,4,9") - before, 1)
        self.assertEqual(clock.waits, [4.5])
        self.assertEqual(self.proofs(events), [])


class CoordinateRecheckCombinationTests(unittest.TestCase):
    def ready(self):
        helper = combination_fixture.Combination6221Tests("test_full_grid_native_pairs_no_detector_source_writes_and_cleanup")
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        text = helper.text.replace('frequency_points_hz = [17.777, 200.0]', 'frequency_points_hz = [20.0, 1217.0]')
        text = text.replace('xx_harmonics = [1, 2]', 'xx_harmonics = [1]')
        text = text.replace('xy_harmonics = [1, 2]', 'xy_harmonics = [1]')
        helper.path.write_text(text, encoding="utf-8")
        counts = {"xx": 0, "xy": 0}
        for role, command in (("xx", "FREQ?"), ("xy", "FREQEXT?")):
            resource = getattr(helper, role)
            query = resource.query
            def read(wire, *, role=role, command=command, query=query):
                answer = query(wire)
                if wire == command and helper.frequency["hz"] == 1217.0:
                    counts[role] += 1
                    if counts[role] == 3:
                        return "1216.92" if role == "xx" else "1217.08"
                return answer
            resource.query = read
        return helper, counts

    def test_pair_counter_recheck_is_archived_before_valid_formal_source_brackets(self):
        helper, counts = self.ready()
        result = helper.execute()
        self.assertEqual(result["status"], "completed", result)
        proofs = [p for k, p in helper.events() if k == "external_pre_capture_frequency_recheck"]
        self.assertEqual(len(proofs), 1)
        self.assertTrue(proofs[0]["completed"])
        self.assertEqual(proofs[0]["checks"][0]["frequency_readback_hz"],
                         {"lockin_xx": 1216.92, "lockin_xy": 1217.08})
        with closing(open_readonly(helper.database)) as db:
            self.assertEqual(validate_accepted_records(db, "current-run")["accepted_conditions"], 4)
        self.assertEqual(helper.source.commands.count("SOUR:WAVE:INIT"), 4)

    def test_source_compliance_during_coordinate_wait_still_rejects_data(self):
        helper, counts = self.ready()
        sleep = helper.clock.sleep
        def compliance(seconds):
            sleep(seconds)
            if seconds == 1.0 and counts["xx"] >= 3:
                helper.source.state["STAT:MEAS:EVEN"] = "8"
        helper.clock.sleep = compliance
        result = helper.execute()
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("compliance", result["error"])
        with closing(open_readonly(helper.database)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM combination_attempts WHERE status='accepted'").fetchone()[0], 2)
        self.assertEqual(helper.source.state["OUTP:STAT"], "OFF")


if __name__ == "__main__":
    unittest.main()
