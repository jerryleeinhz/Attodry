"""Integrated dual SR830 deadlines; fake transports and a monotonic clock."""
from dataclasses import replace
import re
import unittest

from attodry_control.combination_hardware import load_electrical_combination
from attodry_control.lockin_points import LockinPointSession
from attodry_control.sr830 import Sr830Error
from attodry_control.sr830_settings import SensitivityMode
from tests.optical_helpers import Clock
from tests import test_combination_hardware as fixtures
from tests.test_lockin_harmonics import FIXED


class InternalLockinSettlingTests(unittest.TestCase):
    setUp = fixtures.ElectricalCombinationTests.setUp
    write_config = fixtures.ElectricalCombinationTests.write_config

    def make(self, *, xy_tc=.3, harmonics=(1,), extra="", mode="excitation", auto=False):
        source = self.base.replace("excitation_xy_harmonics = [1, 2, 3]",
                                  "excitation_xy_harmonics = " + str(list(harmonics)))
        source = source.replace("settle_time_constants = 5.0", "settle_time_constants = 15.0")
        if mode != "excitation":
            source = re.sub(r"(?m)^frequency_ranges = \[.*?^\]",
                "frequency_points_hz = [17.777, 1000]", source, flags=re.S)
            source = source.replace("combined_xy_harmonics = [1, 2, 3]", "combined_xy_harmonics = [1]")
        self.write_config(order=("lockin",), source=source + extra)
        cfg = load_electrical_combination(self.path).lockin
        cfg.lockin_xy = replace(cfg.lockin_xy, time_constant_s=xy_tc)
        if auto:
            cfg.lockin_xy = replace(cfg.lockin_xy, sensitivity_mode=SensitivityMode.BOUNDED_AUTO,
                autorange_min_full_scale_v=.001, autorange_max_full_scale_v=.01,
                autorange_target_occupancy=.85, autorange_stable_samples=2,
                autorange_full_scales_v=(.001, .01))
            self.xy.responses["SENS?"] = "17"
        self.clock, self.events, self.waits = Clock(), [], []
        def sleep(seconds):
            self.waits.append(seconds)
            self.clock.sleep(seconds)
        session = LockinPointSession(self.path, cfg,
            lambda kind, payload: self.events.append((kind, payload)),
            manager_factory=lambda: self.manager, monotonic=self.clock, sleep=sleep, mode=mode)
        self.addCleanup(session.close)
        session.open()
        session.configure()
        session.set_point(.004)
        session.qualify()
        self.waits.clear()
        self.events.clear()
        return session

    def test_requalification_keeps_fresh_verification_without_waiting_again(self):
        session = self.make()
        before = len(self.xx.queries)
        session.qualify()
        session.qualify()
        self.assertEqual(self.waits, [])
        self.assertGreater(len(self.xx.queries), before)
        self.xx.responses["SENS?"] = "23"
        with self.assertRaisesRegex(Sr830Error, "setting readback"):
            session.qualify()

    def test_unchanged_harmonic_entry_reads_one_fresh_setting_set_per_role(self):
        session = self.make()
        before = len(self.xx.queries) + len(self.xy.queries)
        session.harmonic_control.enter(*session.pair, harmonic={"lockin_xx": 1, "lockin_xy": 1},
                                      settle_s=session.args.settle_s, record=session.point_record)
        self.assertEqual(len(self.xx.queries) + len(self.xy.queries) - before, 8)
        self.assertEqual(self.waits, [])

    def test_other_axis_time_counts_only_after_last_change(self):
        session = self.make()
        self.clock.sleep(100)
        session.disturbed("optical.tuning_completed")
        self.clock.sleep(2)
        session.qualify()
        self.assertEqual(self.waits, [2.5])
        audit = session.point_record["settling_waits"][-1]
        self.assertEqual(audit["required_s_by_role"], {"lockin_xx": 4.5, "lockin_xy": 4.5})
        self.assertEqual(audit["already_elapsed_s_by_role"]["lockin_xx"], 2)

    def test_role_constants_share_one_wait_and_own_changes_are_independent(self):
        session = self.make(xy_tc=.1)
        session.disturbed("xy.range", roles=("lockin_xy",))
        session.qualify()
        self.assertEqual(self.waits, [1.5])
        self.waits.clear()
        session.disturbed("smu.settings_applied")
        session.qualify()
        self.assertEqual(self.waits, [4.5])

    def test_formal_entry_cannot_skip_settling(self):
        session = self.make()
        session.disturbed("temperature.qualified")
        reading = session.sample_point()
        self.assertTrue(reading["valid_for_analysis"])
        self.assertEqual(self.waits, [4.5])
        self.assertEqual(set(reading["measurements"]), {
            f"lockin_{role}_h1_{metric}" for role in ("xx", "xy")
            for metric in ("x_v", "y_v", "amplitude_v", "phase_deg")})

    def test_only_actual_source_write_restarts_both_roles(self):
        session = self.make()
        session.set_point(.008)
        self.clock.sleep(1)
        session.qualify()
        self.assertEqual(self.waits, [3.5])
        self.waits.clear()
        count = len(self.xx.writes)
        session.set_point(.008)
        session.qualify()
        self.assertEqual(self.waits, [])
        self.assertEqual(len(self.xx.writes), count)

    def test_dark_readiness_checks_fresh_settings_without_sleep_write_or_latch_consumption(self):
        session = self.make()
        session.set_point(.008)
        before = (len(self.xx.writes), len(self.xy.writes), self.clock(),
                  dict(session.settling.last_change), session.sample_count)
        xx_status = self.xx.queries.count("LIAS?")
        xy_status = self.xy.queries.count("LIAS?")
        ready = session.verify_ready()
        self.assertTrue(ready["ready"])
        self.assertFalse(ready["settling_performed"])
        self.assertFalse(ready["status_latches_consumed"])
        self.assertEqual(ready["actual"]["lockin_excitation_v_rms"], .008)
        self.assertEqual(self.waits, [])
        self.assertEqual(before, (len(self.xx.writes), len(self.xy.writes), self.clock(),
                                  session.settling.last_change, session.sample_count))
        self.assertEqual((self.xx.queries.count("LIAS?"), self.xy.queries.count("LIAS?")),
                         (xx_status, xy_status))
        session.qualify()
        self.assertEqual(self.waits, [4.5])

    def test_light_change_after_readiness_keeps_new_deadline_for_formal_entry(self):
        session = self.make()
        session.set_point(.008)
        session.verify_ready()
        self.clock.sleep(20)
        session.disturbed("optical.tuning_completed")
        pending = list(session.settling.pending)
        self.clock.sleep(1)
        session.verify_ready()
        self.assertEqual(session.settling.pending, pending)
        self.assertEqual(self.waits, [])
        self.assertTrue(session.sample_point()["valid_for_analysis"])
        self.assertEqual(self.waits, [3.5])

    def test_readiness_cannot_hide_setting_source_or_deadline_faults(self):
        session = self.make()
        session.wait_deadline = self.clock()
        with self.assertRaisesRegex(TimeoutError, "readiness"):
            session.verify_ready()
        session.wait_deadline = None
        self.xx.responses["SENS?"] = "23"
        with self.assertRaisesRegex(Sr830Error, "setting readback"):
            session.verify_ready()
        self.xx.responses["SENS?"] = "26"
        self.xx.responses["SLVL?"] = "6"
        with self.assertRaises(ValueError):
            session.verify_ready()
        self.assertFalse([p for k, p in self.events if k == "lockin_readiness"][-1]["ready"])
        self.assertEqual(self.waits, [])

    def test_delayed_xy_is_only_unqualified_observation_until_formal_pair_check(self):
        for xy_hz in (0, 100000):
            session = self.make(harmonics=(1, 2))
            session.set_point(.008)
            self.xy.frequency_scale = xy_hz / session.target_frequency_hz
            ready = session.verify_ready()
            self.assertTrue(ready["ready"])
            self.assertFalse(ready["companion_reference_qualified"])
            self.assertTrue(ready["formal_check_deferred"])
            self.assertEqual(ready["reference_hz"]["lockin_xy"], xy_hz)
            self.assertEqual(session.point_harmonics, (1, 2))
            self.assertEqual(self.waits, [])
            self.assertEqual(session.read_coordinates(verify_companion=False)["lockin_frequency_hz"],
                             session.target_frequency_hz)
            with self.subTest(xy_hz=xy_hz), self.assertRaises(Sr830Error):
                session.read_coordinates()
            with self.assertRaises(Sr830Error):
                session.qualify()
            self.assertEqual(self.waits, [4.5])
            self.xy.frequency_scale = 1
            session.qualify()
            self.assertEqual(self.waits, [4.5])
            session.protect_source()

    def test_unqualified_xy_still_rejects_impossible_model_readback(self):
        session = self.make()
        for xy_hz in (-1, 102001):
            self.xy.frequency_scale = xy_hz / session.target_frequency_hz
            with self.subTest(xy_hz=xy_hz), self.assertRaisesRegex(Sr830Error, "Invalid reference"):
                session.verify_ready()

    def test_harmonic_and_target_settings_share_deadline_without_normal_bridge(self):
        session = self.make(harmonics=(1, 2), extra=FIXED)
        before = len(self.xy.writes)
        reading = session.sample_point()
        self.assertTrue(reading["valid_for_analysis"])
        self.assertEqual(self.waits, [4.5])
        writes = self.xy.writes[before:]
        self.assertEqual(writes, ["HARM 2", "RMOD 1", "SENS 18"])
        self.assertFalse(any(t["stage"] == "bridge" for t in reading["status"]["harmonic_settings_transitions"]))

    def test_real_frequency_change_and_source_share_final_deadline(self):
        session = self.make(mode="frequency_excitation")
        session.set_point(.008, 1000)
        self.assertEqual(self.waits, [])
        session.qualify()
        self.assertEqual(self.waits, [4.5])
        self.assertEqual(session.read_coordinates()["lockin_frequency_hz"], 1000)
        self.waits.clear()
        count = len(self.xx.writes)
        session.set_point(.008, 1000)
        session.qualify()
        self.assertEqual(len(self.xx.writes), count)
        self.assertEqual(self.waits, [])

    def test_segment_range_change_restarts_only_changed_detector(self):
        session = self.make(xy_tc=.1)
        session.grid = tuple(replace(p, range_spec=replace(p.range_spec, xy_full_scale_v=.002))
                             if p.source_v_rms == .004 else p for p in session.grid)
        session.set_point(.004)
        session.qualify()
        self.assertEqual(self.waits, [1.5])
        self.assertEqual(self.xy.responses["SENS?"].strip(), "18")

    def test_failed_range_transition_retains_attempt_and_audit_cannot_mask_source_failure(self):
        session = self.make()
        session.grid = tuple(replace(p, range_spec=replace(p.range_spec, xy_full_scale_v=.002))
                             if p.source_v_rms == .004 else p for p in session.grid)
        self.xy.fail_write = "SENS 18"
        with self.assertRaisesRegex(OSError, "injected VISA"):
            session.set_point(.004)
        audit = [p for k, p in self.events if k == "lockin_point_transition"][-1]
        self.assertEqual(audit["range_transition"]["changed"]["lockin_xy"]["target_sensitivity_code"], 18)
        self.xy.fail_write = None
        self.xx.fail_write = "SLVL 0.008"
        session.event = lambda *_: (_ for _ in ()).throw(OSError("audit disk"))
        with self.assertRaisesRegex(OSError, "injected VISA") as caught:
            session.set_point(.008)
        self.assertIn("Transition audit failed", str(caught.exception.__notes__))

    def test_autorange_widening_requires_new_full_role_settle(self):
        session = self.make(auto=True)
        self.xy.snapshots = [".0009,0,.0009,0,17.777"]
        reading = session.sample_point()
        self.assertTrue(reading["valid_for_analysis"])
        self.assertEqual(self.waits, [4.5])
        self.assertIn("SENS 20", self.xy.writes)
        audits = reading["status"]["harmonic_autorange"]
        self.assertEqual(audits[0]["settling_waits"][0]["sleep_requested_s"], 4.5)

    def test_guard_is_called_during_wait_and_deadline_leaves_incomplete_audit(self):
        session = self.make()
        session.disturbed("optical.tuning_completed")
        guards = []
        session.wait_guard = lambda: guards.append(self.clock())
        session.wait_deadline = self.clock() + 2
        with self.assertRaisesRegex(TimeoutError, "operation deadline"):
            session.qualify()
        self.assertEqual(self.waits, [1, 1])
        self.assertGreaterEqual(len(guards), 3)
        self.assertFalse(session.point_record["settling_waits"][-1]["completed"])
        session.wait_guard = None
        session.wait_deadline = None
        self.assertTrue(session.cleanup()["source_protection_verified"])

    def test_guard_communication_time_counts_toward_detector_deadline(self):
        session = self.make()
        session.disturbed("optical.tuning_completed")
        session.wait_guard = lambda: self.clock.sleep(.25)
        started = self.clock()
        session.qualify()
        self.assertLess(sum(self.waits), 4.5)
        self.assertGreaterEqual(self.clock() - started, 4.5)
        self.assertLess(self.clock() - started, 4.76)

    def test_unknown_or_unowned_status_is_not_cleared_as_a_normal_transition(self):
        for status, text in (("128", "unknown LIAS"), ("16", "unexpected frequency"),
                             ("32", "time constant"), ("8", "reference unlocked")):
            session = self.make()
            session.disturbed("smu.settings_applied")
            self.xx.responses["LIAS?"] = status
            with self.subTest(status=status), self.assertRaisesRegex(Sr830Error, text):
                session.qualify()
            self.assertIn("blocking_problems", session.point_record["settling_transition_windows"][-1])
            self.xx.responses["LIAS?"] = "0"

    def test_formal_overload_is_preserved_and_rejected_after_transient_window(self):
        session = self.make()
        self.xy.responses["LIAS?"] = "1"
        with self.assertRaises(Sr830Error):
            session.sample_point()
        recorded = [p for k, p in self.events if k == "lockin_point_samples"][-1]
        self.assertTrue(recorded["samples"])
        self.assertFalse(recorded["samples"][0]["valid_for_analysis_by_role"]["lockin_xy"])

    def test_protection_requires_actual_readback_and_skips_equal_setting(self):
        session = self.make()
        count = len(self.xx.writes)
        self.assertTrue(session.protect_source()["source_protection_verified"])
        self.assertEqual(len(self.xx.writes), count)
        self.xx.responses["SLVL?"] = ".008"
        self.xx.sine_output_transform = lambda _: .008
        with self.assertRaisesRegex(Sr830Error, "not confirmed"):
            session.protect_source()
        self.assertFalse([p for k, p in self.events if k == "lockin_source_protection"][-1]["verified"])

    def test_interruption_and_failed_audit_preserve_primary_failure(self):
        session = self.make()
        session.disturbed("magnetic.qualified")
        session.sleep = lambda _: (_ for _ in ()).throw(KeyboardInterrupt("operator"))
        session.event = lambda *_: (_ for _ in ()).throw(OSError("audit disk"))
        with self.assertRaisesRegex(KeyboardInterrupt, "operator") as caught:
            session.qualify()
        self.assertIn("Settling audit failed", str(caught.exception.__notes__))


if __name__ == "__main__":
    unittest.main()
