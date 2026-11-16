"""Fresh native receiver snapshots: query budgets and fail-closed evidence."""
from collections import Counter
from types import SimpleNamespace
import unittest

from attodry_control.lockin_harmonics import HarmonicSensitivitySession
from attodry_control.sr830 import Sr830Error
from tests import test_external_lockin_points as external


class NativeSettingSnapshotTests(unittest.TestCase):
    def fixture(self):
        helper = external.ExternalLockinTests()
        fixture, _, session, clock, _, _ = helper.fixture(harmonics=(1,))
        self.addCleanup(helper.doCleanups)
        session.open()
        session.configure()
        fixture.xx.queries.clear()
        fixture.xy.queries.clear()
        return fixture, session, clock

    def test_one_fresh_native_snapshot_supplies_the_complete_compatibility_view(self):
        fixture, session, clock = self.fixture()
        writes = (list(fixture.xx.writes), list(fixture.xy.writes))
        waits = list(clock.waits)
        record = {}
        proof = session.harmonic_control.verify(
            *session.pair, session.harmonic_control.current_harmonics, record)
        values = proof["roles"]["lockin_xy"]
        counts = Counter(fixture.xy.queries)
        # Formerly each was queried twice; read_sensitivity also repeated IVMD.
        self.assertEqual({q: counts[q] for q in ("HARM?", "SCAL?", "OFLT?")},
                         {"HARM?": 1, "SCAL?": 1, "OFLT?": 1})
        self.assertEqual(counts["IVMD?"], 3)  # Preserve all native interpretation guards.
        self.assertEqual(fixture.xx.queries, ["HARM?", "SENS?", "RMOD?", "OFLT?"])
        self.assertEqual(values["model"], "SR865A")
        self.assertEqual((values["harmonic"], values["sensitivity_code"],
                          values["time_constant_code"], values["reserve_code"]),
                         (1, 6, 11, None))
        native = values["receiver_invariants"]
        self.assertEqual(values["sensitivity_full_scale_v"], native["sensitivity_full_scale_v"])
        self.assertEqual(values["time_constant_s"], native["time_constant_s"])
        self.assertTrue(native["raw"])
        self.assertEqual(native["source_output_policy"], "preserve_disconnected_receiver")
        self.assertEqual((fixture.xx.writes, fixture.xy.writes), writes)
        self.assertEqual(clock.waits, waits)

    def test_each_verification_is_fresh_and_drift_keeps_both_readbacks(self):
        fixture, session, _ = self.fixture()
        record = {}
        session.harmonic_control.verify(
            *session.pair, session.harmonic_control.current_harmonics, record)
        fixture.xy.responses["SCAL?"] = "7"
        with self.assertRaisesRegex(Sr830Error, "sensitivity_code"):
            session.harmonic_control.verify(
                *session.pair, session.harmonic_control.current_harmonics, record)
        readings = record["harmonic_setting_checks"]
        self.assertEqual([r["roles"]["lockin_xy"]["sensitivity_code"] for r in readings], [6, 7])
        self.assertEqual(fixture.xy.queries.count("SCAL?"), 2)
        self.assertTrue(record["harmonic_setting_errors"])

    def test_changed_harmonic_time_constant_and_native_invariants_still_reject(self):
        for command, changed in (("HARM?", "2"), ("OFLT?", "12"),
                                 ("IRNG?", "0"), ("ADVFILT?", "1")):
            with self.subTest(command=command):
                fixture, session, _ = self.fixture()
                fixture.xy.responses[command] = changed
                record = {}
                with self.assertRaises(Sr830Error):
                    session.harmonic_control.verify(
                        *session.pair, session.harmonic_control.current_harmonics, record)
                raw = record["harmonic_setting_checks"][0]["roles"]["lockin_xy"]["receiver_invariants"]["raw"]
                self.assertTrue(any(q["command"] == command and q["response"] == changed for q in raw))

    def test_native_failure_retains_completed_xx_and_partial_receiver_raw(self):
        fixture, session, _ = self.fixture()
        original = fixture.xy.query
        def query(command):
            if command == "IRNG?":
                raise OSError("injected native snapshot interruption")
            return original(command)
        fixture.xy.query = query
        record = {}
        with self.assertRaisesRegex(Sr830Error, "IRNG"):
            session.harmonic_control.verify(
                *session.pair, session.harmonic_control.current_harmonics, record)
        partial = record["harmonic_setting_checks"][0]["roles"]
        self.assertEqual(partial["lockin_xx"]["harmonic"], 1)
        self.assertIn("receiver_invariants_error", partial["lockin_xy"])
        raw = partial["lockin_xy"]["receiver_invariants_raw"]
        self.assertTrue(any(q.command == "IRNG?" and q.error for q in raw))
        self.assertTrue(record["harmonic_setting_errors"])

    def test_disconnected_source_drift_retains_native_evidence(self):
        fixture, session, _ = self.fixture()
        fixture.xy.responses["SOFF?"] = "0.8"
        record = {}
        with self.assertRaisesRegex(Sr830Error, "source/output state changed"):
            session.harmonic_control.verify(
                *session.pair, session.harmonic_control.current_harmonics, record)
        partial = record["harmonic_setting_checks"][0]["roles"]["lockin_xy"]
        self.assertEqual(partial["receiver_invariants_evidence"]["actual"]["source_offset_v"], .8)
        self.assertTrue(partial["receiver_invariants_raw"])

    def test_invalid_native_code_is_not_accepted_as_a_derived_setting(self):
        for command, changed in (("SCAL?", "999"), ("OFLT?", "999"), ("HARM?", "nan")):
            with self.subTest(command=command):
                fixture, session, _ = self.fixture()
                fixture.xy.responses[command] = changed
                record = {}
                with self.assertRaises(Sr830Error):
                    session.harmonic_control.verify(
                        *session.pair, session.harmonic_control.current_harmonics, record)
                partial = record["harmonic_setting_checks"][0]["roles"]["lockin_xy"]
                self.assertTrue(partial["receiver_invariants_raw"])
                self.assertIn("receiver_invariants_error", partial)

    def test_sr830_generic_adapter_retains_four_individual_reads(self):
        calls = []
        instrument = SimpleNamespace(
            read_harmonic=lambda: calls.append("harmonic") or 1,
            read_sensitivity=lambda: calls.append("sensitivity") or 20,
            read_reserve_mode=lambda: calls.append("reserve") or 1,
            read_time_constant=lambda: calls.append("time_constant") or 10)
        session = object.__new__(HarmonicSensitivitySession)
        session.configs = {"lockin_xx": SimpleNamespace(model="SR830")}
        audit = []
        reading = session._read({"lockin_xx": instrument}, audit)
        self.assertEqual(calls, ["harmonic", "sensitivity", "reserve", "time_constant"])
        values = reading["roles"]["lockin_xx"]
        self.assertEqual(values["sensitivity_full_scale_v"], .01)
        self.assertEqual(values["time_constant_s"], 1.0)
        self.assertNotIn("receiver_invariants", values)


if __name__ == "__main__":
    unittest.main()
