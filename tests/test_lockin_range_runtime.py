"""Compiled range application on the production session, fake VISA only."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
import unittest

from attodry_control.combination_hardware import load_hardware_combination
from attodry_control.lockin_points import LockinPointSession
from attodry_control.sr830 import Sr830Error
from tests import test_combination_hardware as fixtures
from tests.test_lockin_settling import Clock
from tests import test_mixed_lockin_integration as mixed_fixtures


PROFILE = {"name": "low", "ranges": {"lockin_xx": .05, "lockin_xy": .002}}


class LockinRangeRuntimeTests(unittest.TestCase):
    def fixture(self, *, mode="excitation", mixed=False):
        if mixed:
            return mixed_fixtures.MixedLockinIntegrationTests.fixture(self, mode=mode)
        fixture = fixtures.ElectricalCombinationTests("test_only_requested_modules_open")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        if mode != "excitation":
            import re
            source = re.sub(r"(?m)^frequency_ranges = \[.*?^\]",
                            "frequency_points_hz = [17.777, 200.0]", fixture.base,
                            flags=re.DOTALL)
            fixture.write_config(order=("lockin",), source=source,
                                 extra=f'lockin_mode = "{mode}"\n')
        return fixture

    def session(self, fixture=None, *, mode="excitation", mixed=False, opened=True):
        fixture = fixture or self.fixture(mode=mode, mixed=mixed)
        config = load_hardware_combination(fixture.path).lockin
        clock, events = Clock(), []
        session = LockinPointSession(fixture.path, config,
            lambda kind, value: events.append((kind, deepcopy(value))),
            mode=mode, authorize_writes=True, manager_factory=lambda: fixture.manager,
            monotonic=clock.monotonic, sleep=clock.sleep)
        self.addCleanup(session.close)
        if opened:
            session.open()
            session.configure()
            fixture.xx.writes.clear()
            fixture.xy.writes.clear()
            fixture.xx.queries.clear()
            fixture.xy.queries.clear()
        return session, fixture, clock, events

    def point(self, session, index=0):
        point = session.grid[index]
        session.set_point(point.source_v_rms, point.frequency_hz, point_index=index)

    def test_complete_pair_is_validated_before_any_io(self):
        session, fixture, _, _ = self.session(opened=False)
        bad = [None, {}, {"name": "", "ranges": PROFILE["ranges"]},
               {"name": "x", "ranges": {"lockin_xx": .05}},
               {"name": "x", "ranges": {**PROFILE["ranges"], "lockin_aux": .01}}]
        for value in (True, 0, -1, float("nan"), float("inf"), "0.002", .123, .005):
            bad.append({"name": "x", "ranges": {"lockin_xx": .05, "lockin_xy": value}})
        for profile in bad:
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                session.apply_range_profile(profile)
        self.assertEqual(fixture.manager.opened, [])
        self.assertEqual(fixture.xx.queries + fixture.xy.queries, [])

    def test_native_unsupported_scale_is_rejected_even_if_allowed_by_station(self):
        session, fixture, _, _ = self.session(opened=False)
        safety = replace(session.config.lockin_safety,
            lockin_xy=replace(session.config.lockin_safety.lockin_xy,
                              allowed_fixed_full_scales_v=(.123,)))
        session.config = SimpleNamespace(**{**vars(session.config), "lockin_safety": safety})
        with self.assertRaises(ValueError):
            session.apply_range_profile({"name": "bad", "ranges": {
                "lockin_xx": .05, "lockin_xy": .123}})
        self.assertEqual(fixture.manager.opened, [])

    def test_profile_applies_only_scales_without_sleep_or_config_mutation(self):
        session, fixture, clock, events = self.session()
        config_before = deepcopy(vars(session.config))
        context = session.apply_range_profile(PROFILE)
        self.assertTrue(context["verified"])
        self.assertEqual(context["requested_full_scales_v"], PROFILE["ranges"])
        self.assertEqual(context["confirmed_full_scales_v"], PROFILE["ranges"])
        self.assertEqual(context["changed_roles"], ["lockin_xx", "lockin_xy"])
        self.assertEqual(fixture.xx.writes, ["SENS 22"])
        self.assertEqual(fixture.xy.writes, ["SENS 18"])
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(len(fixture.xx.queries + fixture.xy.queries), 18)
        self.assertEqual(vars(session.config), config_before)
        self.assertEqual(sum(k == "lockin_range_rule_write_attempt" for k, _ in events), 2)
        context["confirmed_full_scales_v"]["lockin_xy"] = 999
        self.assertEqual(session.range_context()["confirmed_full_scales_v"]["lockin_xy"], .002)

    def test_same_scales_under_next_rule_verify_without_writes_or_deadline_reset(self):
        session, fixture, clock, _ = self.session()
        session.apply_range_profile(PROFILE)
        self.point(session)
        session.qualify()
        deadline = dict(session.harmonic_control.settling.last_change)
        sleeps = list(clock.sleeps)
        fixture.xx.writes.clear()
        fixture.xy.writes.clear()
        context = session.apply_range_profile({**PROFILE, "name": "next"})
        session.qualify()
        self.assertEqual(context["changed_roles"], [])
        self.assertEqual(fixture.xx.writes + fixture.xy.writes, [])
        self.assertEqual(session.harmonic_control.settling.last_change, deadline)
        self.assertEqual(clock.sleeps, sleeps)
        self.assertEqual(session.point_record["lockin_range_rule"]["rule_name"], "next")

    def test_elapsed_other_work_counts_towards_one_final_settle(self):
        session, _, clock, _ = self.session()
        session.apply_range_profile(PROFILE)
        elapsed = session.args.settle_s / 2
        clock.now += elapsed
        self.point(session)
        session.qualify()
        self.assertEqual(clock.sleeps, [session.args.settle_s - elapsed])
        session.qualify()
        self.assertEqual(clock.sleeps, [session.args.settle_s - elapsed])

    def test_existing_harmonic_auto_and_segment_overrides_reject_before_io(self):
        from attodry_control.sr830_settings import SensitivityMode
        for kind in ("harmonic", "auto", "segment"):
            session, fixture, _, _ = self.session(opened=False)
            if kind == "harmonic":
                xx = replace(session.config.lockin_xx, harmonic_settings=(object(),))
                session.config = SimpleNamespace(**{**vars(session.config), "lockin_xx": xx})
            elif kind == "auto":
                xx = replace(session.config.lockin_xx, sensitivity_mode=SensitivityMode.BOUNDED_AUTO)
                session.config = SimpleNamespace(**{**vars(session.config), "lockin_xx": xx})
            else:
                point = session.grid[0]
                session.grid = (replace(point, range_spec=replace(point.range_spec,
                                                                  xx_full_scale_v=.05)),)
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "overrides|fixed mode"):
                session.apply_range_profile(PROFILE)
            self.assertEqual(fixture.manager.opened, [])

    def test_rule_change_with_unchanged_lockin_axis_updates_formal_context(self):
        session, _, _, _ = self.session()
        session.apply_range_profile(PROFILE)
        self.point(session)
        session.qualify()
        session.sample_point()
        next_profile = {"name": "high", "ranges": {"lockin_xx": .02, "lockin_xy": .01}}
        session.apply_range_profile(next_profile)
        session.qualify()
        result = session.sample_point()
        self.assertEqual(result["status"]["lockin_range_rule"]["rule_name"], "high")
        for sample in result["status"]["samples"]:
            self.assertEqual(sample["lockin_range_rule"]["rule_name"], "high")
            for role, scale in next_profile["ranges"].items():
                self.assertEqual(sample["settings_after"]["roles"][role]["sensitivity_full_scale_v"], scale)

    def test_harmonics_points_formal_context_and_cleanup_preserve_profile(self):
        session, fixture, _, events = self.session()
        session.apply_range_profile(PROFILE)
        for index in range(len(session.grid)):
            self.point(session, index)
            session.qualify()
            result = session.sample_point()
            self.assertEqual(result["status"]["lockin_range_rule"]["rule_name"], "low")
            for sample in result["status"]["samples"]:
                self.assertTrue(sample["settings_verified"])
                for role, scale in PROFILE["ranges"].items():
                    self.assertEqual(sample["settings_after"]["roles"][role]["sensitivity_full_scale_v"], scale)
                self.assertEqual(sample["lockin_range_rule"]["rule_name"], "low")
        self.assertEqual([v for v in fixture.xx.writes if v.startswith("SENS ")], ["SENS 22"])
        self.assertEqual([v for v in fixture.xy.writes if v.startswith("SENS ")], ["SENS 18"])
        self.assertTrue(any(k == "lockin_formal_pair" for k, _ in events))
        cleanup = session.cleanup()
        self.assertTrue(cleanup["verified"], cleanup)
        for role, resource in (("lockin_xx", fixture.xx), ("lockin_xy", fixture.xy)):
            self.assertEqual(int(resource.responses["SENS?"]),
                session.sensitivity_setup["ranges"][role]["configured_fixed_sensitivity_code"])

    def test_partial_second_role_failure_retains_evidence_and_blocks_reentry(self):
        session, fixture, _, events = self.session()
        original = fixture.xy.write
        def write(command):
            if command == "SENS 18":
                raise OSError("injected second-role write failure")
            return original(command)
        fixture.xy.write = write
        with self.assertRaisesRegex(OSError, "second-role"):
            session.apply_range_profile(PROFILE)
        context = session.range_context()
        self.assertFalse(context["verified"])
        self.assertEqual(context["confirmed_full_scales_v"], {"lockin_xx": .05})
        self.assertTrue(context["transitions"][-1]["write_attempted"])
        self.assertFalse(context["transitions"][-1]["write_returned"])
        self.assertIsNone(session.range_profile)
        writes = list(fixture.xx.writes + fixture.xy.writes)
        for call in (lambda: self.point(session), session.qualify, session.sample_point,
                     lambda: session.apply_range_profile(PROFILE)):
            with self.assertRaisesRegex(Sr830Error, "cannot continue"):
                call()
        self.assertEqual(fixture.xx.writes + fixture.xy.writes, writes)
        self.assertTrue(any(k == "lockin_range_rule_failed" for k, _ in events))
        self.assertTrue(session.cleanup()["verified"])

    def test_ignored_write_is_not_published_or_retried(self):
        session, fixture, _, events = self.session()
        original = fixture.xy.write
        fixture.xy.write = lambda command: None if command == "SENS 18" else original(command)
        with self.assertRaisesRegex(Sr830Error, "readback"):
            session.apply_range_profile(PROFILE)
        context = session.range_context()
        self.assertFalse(context["verified"])
        self.assertEqual(context["transitions"][-1]["readback_sensitivity_code"], 20)
        self.assertIsNone(session.range_profile)
        self.assertTrue(any(k == "lockin_range_rule_readback" and p["role"] == "lockin_xy"
                            for k, p in events))

    def test_later_readback_failure_retains_both_successful_writes_without_publish(self):
        session, fixture, _, _ = self.session()
        original, count = fixture.xy.query, 0
        def query(command):
            nonlocal count
            if command == "SENS?":
                count += 1
                if count == 3:
                    raise OSError("injected final verification failure")
            return original(command)
        fixture.xy.query = query
        with self.assertRaisesRegex(OSError, "final verification"):
            session.apply_range_profile(PROFILE)
        self.assertFalse(session.range_context()["verified"])
        self.assertEqual(session.range_context()["confirmed_full_scales_v"], PROFILE["ranges"])
        self.assertIsNone(session.range_profile)

    def test_frequency_transition_does_not_restore_baseline_scales(self):
        session, fixture, _, _ = self.session(mode="frequency")
        session.apply_range_profile(PROFILE)
        for index in range(len(session.grid)):
            self.point(session, index)
            session.qualify()
            result = session.sample_point()
            self.assertTrue(all(s["settings_verified"] for s in result["status"]["samples"]))
        self.assertTrue(any(c.startswith("FREQ ") for c in fixture.xx.writes))
        self.assertEqual([c for c in fixture.xx.writes if c.startswith("SENS ")], ["SENS 22"])
        self.assertEqual([c for c in fixture.xy.writes if c.startswith("SENS ")], ["SENS 18"])

    def test_native_receiver_uses_scal_without_input_reserve_or_source_changes(self):
        session, fixture, _, _ = self.session(mixed=True)
        session.apply_range_profile(PROFILE)
        self.assertEqual(fixture.xy.writes, ["SCAL 8"])
        self.point(session)
        session.qualify()
        result = session.sample_point()
        for sample in result["status"]["samples"]:
            self.assertEqual(sample["settings_after"]["roles"]["lockin_xy"]["sensitivity_full_scale_v"], .002)
        self.assertFalse(any(c.split()[0] in {"SENS", "RMOD", "IRNG", "SLVL", "SOFF", "REFM", "BLAZEX"}
                             for c in fixture.xy.writes))
        self.assertTrue(session.cleanup()["verified"])


if __name__ == "__main__":
    unittest.main()
