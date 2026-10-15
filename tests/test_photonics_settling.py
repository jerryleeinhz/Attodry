"""Detector deadlines with fake clocks and SR830/SR865A transports only."""
import unittest

from attodry_control.lockin_settling import LockinSettling
from attodry_control.photonics_lockin_config import load_photonics_lockin_config
from attodry_control.photonics_lockin_points import PhotonicsLockinPointSession
from attodry_control.reference_transients import ReferenceTransientError
from tests.optical_helpers import Clock
from tests.photonics_lockin_helpers import (
    FakeManager, reversed_sine_document, reversed_sine_safety_document,
)


class PhotonicsSettlingTests(unittest.TestCase):
    def make(self, *, xx_harmonics=(1,), xy_harmonics=(1,), xy_tc=1.0):
        doc = reversed_sine_document()
        doc["lockin_xx"].update(time_constant_s=1.0, harmonics=list(xx_harmonics))
        doc["lockin_xy"].update(time_constant_s=xy_tc, harmonics=list(xy_harmonics))
        doc["photonics_lockin"]["settle_time_constants"] = 15.0
        cfg = load_photonics_lockin_config("fake.toml", document=doc,
            safety_document=reversed_sine_safety_document())
        manager, clock, events, waits = FakeManager(doc), Clock(), [], []
        manager.resources["FAKE::XY"].responses["SLVL?"] = "0.2"
        def sleep(seconds):
            waits.append(seconds)
            clock.sleep(seconds)
        session = PhotonicsLockinPointSession("fake.toml", cfg,
            lambda kind, payload: events.append((kind, payload)),
            manager_factory=lambda: manager, clock=clock, sleep=sleep)
        self.addCleanup(session.close)
        session.open()
        session.configure()
        session.set_point(.004)
        session.qualify()
        waits.clear()
        events.clear()
        return session, manager, clock, events, waits

    def test_requalification_rechecks_settings_without_duplicate_sleep(self):
        session, manager, _, events, waits = self.make()
        queries = len(manager.resources["FAKE::XX"].queries)
        session.qualify()
        session.qualify()
        self.assertEqual(waits, [])
        self.assertGreater(len(manager.resources["FAKE::XX"].queries), queries)
        self.assertEqual(sum(k == "lockin_point_qualification" for k, _ in events), 2)
        manager.resources["FAKE::XX"].responses["ICPL?"] = "1"
        with self.assertRaisesRegex(ValueError, "settings"):
            session.qualify()

    def test_other_module_time_only_counts_after_last_signal_change(self):
        session, _, clock, events, waits = self.make()
        clock.sleep(100)
        session.disturbed("optical.tuning_completed")
        clock.sleep(5)
        session.qualify()
        self.assertEqual(waits, [10])
        audit = next(p for k, p in events if k == "lockin_settling_wait")["settling_waits"][0]
        self.assertEqual(audit["already_elapsed_s_by_role"]["lockin_xx"], 5)
        self.assertEqual(audit["required_s_by_role"]["lockin_xx"], 15)
        self.assertTrue(audit["completed"])

    def test_formal_entry_cannot_skip_signal_settling_without_qualify(self):
        session, _, clock, events, waits = self.make()
        session.disturbed("smu.settings_applied")
        start = clock()
        reading = session.sample_point()
        self.assertTrue(reading["clean"])
        self.assertEqual(waits, [15])
        self.assertEqual(clock() - start, 15)
        wait_index = next(i for i, (k, _) in enumerate(events) if k == "lockin_settling_wait")
        formal_index = next(i for i, (k, _) in enumerate(events) if k == "lockin_formal_pair")
        self.assertLess(wait_index, formal_index)

    def test_different_role_constants_share_one_wait_and_role_changes_are_independent(self):
        session, _, _, _, waits = self.make(xy_tc=.3)
        session.disturbed("harmonic", roles=("lockin_xy",))
        session.qualify()
        self.assertAlmostEqual(sum(waits), 4.5)
        waits.clear()
        session.disturbed("magnetic.qualified")
        session.qualify()
        self.assertAlmostEqual(sum(waits), 15)

    def test_only_real_source_write_restarts_both_detectors(self):
        session, manager, _, _, waits = self.make()
        session.set_point(.006)
        session.qualify()
        self.assertEqual(waits, [15])
        waits.clear()
        count = len(manager.resources["FAKE::XX"].writes)
        session.set_point(.006)
        session.qualify()
        self.assertEqual(waits, [])
        self.assertEqual(len(manager.resources["FAKE::XX"].writes), count)

    def test_noop_transition_cannot_consume_new_range_event_as_owned_setting_change(self):
        session, manager, _, _, _ = self.make()
        resource = manager.resources["FAKE::XX"]
        polls = [0]
        def inject(resource, command):
            if command == "LIAS?":
                polls[0] += 1
                if polls[0] == 2:  # After the clean pre-source bracket.
                    resource.responses[command] = "16"
        resource.on_query = inject
        with self.assertRaises(ReferenceTransientError):
            session.set_point(.004)
        self.assertTrue(session.failed)

    def test_harmonic_cycle_waits_after_each_real_switch_and_not_for_unchanged_role(self):
        session, manager, _, events, waits = self.make(xx_harmonics=(1,), xy_harmonics=(1, 2), xy_tc=.3)
        xx = manager.resources["FAKE::XX"]
        before = len(xx.queries)
        session.sample_point()
        self.assertAlmostEqual(sum(waits), 4.5)
        changes = [p for k, p in events if k == "lockin_settling_changed" and p["reason"] == "harmonic"]
        self.assertEqual([p["roles"] for p in changes], [["lockin_xy"]])
        # Only settings/status reads remain on the unchanged XX role; no setter's
        # extra reference-frequency authorization query is issued for it.
        self.assertFalse(any(c.startswith("HARM ") for c in xx.writes))
        self.assertGreater(len(xx.queries), before)
        waits.clear()
        session.sample_point()
        self.assertAlmostEqual(sum(waits), session.config.sample_interval_s + 9)

    def test_interrupted_wait_is_recorded_incomplete_and_protection_still_runs(self):
        session, manager, _, events, _ = self.make()
        session.set_point(.006)
        session.disturbed("temperature.qualified")
        session.sleep = lambda _: (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            session.qualify()
        audit = [p for k, p in events if k == "lockin_settling_wait"][-1]["settling_waits"][0]
        self.assertFalse(audit["completed"])
        self.assertTrue(session.cleanup()["source_protection_verified"])
        self.assertEqual(manager.resources["FAKE::XX"].responses["SLVL?"], "0.004")

    def test_audit_failure_cannot_replace_primary_wait_interrupt(self):
        session, _, _, _, _ = self.make()
        session.disturbed("smu.settings_applied")
        session.sleep = lambda _: (_ for _ in ()).throw(KeyboardInterrupt("primary interrupt"))
        session.event = lambda *_: (_ for _ in ()).throw(OSError("audit disk failed"))
        with self.assertRaisesRegex(KeyboardInterrupt, "primary interrupt") as caught:
            session.qualify()
        self.assertTrue(any("Settling audit failed" in note for note in caught.exception.__notes__))


class SharedSettlingTests(unittest.TestCase):
    def test_new_change_restarts_deadline_even_after_an_old_completed_wait(self):
        clock = Clock()
        settling = LockinSettling(monotonic=clock, sleep=clock.sleep)
        settling.changed("gate")
        settling.wait({"lockin_xx": 15, "lockin_xy": 3}, {})
        clock.sleep(5)
        settling.changed("gate")
        clock.sleep(2)
        audit = settling.wait({"lockin_xx": 15, "lockin_xy": 3}, {})
        self.assertEqual(audit["sleep_requested_s"], 13)
