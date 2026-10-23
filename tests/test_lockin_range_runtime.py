"""Outer-condition sensitivity changes using owned fake lock-in transports."""
from copy import deepcopy
import math
import unittest

from tests import test_three_lockin as group_fixtures
from tests import test_photonics_lockin_points as pem_fixtures
from attodry_control.sr865a import Sr865aError


class LockinRangeRuntimeTests(unittest.TestCase):
    def make(self, *, configured=True):
        fixture = group_fixtures.ThreeLockinTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        session, manager, config = fixture.make(configured=configured)
        self.initial_scales = {"lockin_" + cfg.role.value: cfg.sensitivity_full_scale_v
                               for cfg in session.roles}
        self.fixture = fixture
        self.addCleanup(session.cleanup)
        return session, manager, config

    def profile(self, name="small_signal", **overrides):
        return {"name": name, "ranges": {**self.initial_scales, **overrides}}

    @staticmethod
    def counts(manager, attribute):
        return {name: len(getattr(resource, attribute)) for name, resource in manager.resources.items()}

    def test_no_profile_keeps_absent_metadata(self):
        session, _, _ = self.make()
        self.assertEqual(session.range_context(), {})
        self.assertNotIn("lockin_range_profile", session.sample_point()["status"])

    def test_changed_roles_only_write_sensitivity_and_preserve_frozen_configuration(self):
        session, manager, _ = self.make()
        original_config = session.config
        phase = {cfg.role.value: cfg.phase_shift_deg for cfg in session.roles}
        starts = self.counts(manager, "writes")
        result = session.apply_range_profile(self.profile(lockin_xx=.0002, lockin_aux=.0002))
        self.assertEqual(result["changed_roles"], ["lockin_xx", "lockin_aux"])
        self.assertTrue(result["verified"])
        self.assertEqual(result["confirmed_full_scales_v"], result["requested_full_scales_v"])
        self.assertIs(session.config, original_config)
        self.assertEqual(session.config.lockin_xx.sensitivity_full_scale_v, self.initial_scales["lockin_xx"])
        self.assertEqual(session.config.lockin_aux.sensitivity_full_scale_v, .001)
        self.assertEqual({cfg.role.value: cfg.phase_shift_deg for cfg in session.roles}, phase)
        writes = {name: resource.writes[starts[name]:] for name, resource in manager.resources.items()}
        self.assertEqual(writes["FAKE::XY"], [])
        self.assertEqual(len(writes["FAKE::XX"]), 1)
        self.assertTrue(writes["FAKE::XX"][0].startswith("SENS "))
        self.assertEqual(len(writes["FAKE::AUX"]), 1)
        self.assertTrue(writes["FAKE::AUX"][0].startswith("SCAL "))
        self.assertEqual(session.settling.pending, [])
        reading = session.sample_point()
        self.assertTrue(reading["valid_for_analysis"])
        self.assertEqual(reading["status"]["lockin_range_profile"], result)
        self.assertTrue(session.cleanup()["verified"])

    def test_same_ranges_verify_and_change_rule_without_setting_write_or_wait(self):
        session, manager, _ = self.make()
        starts = self.counts(manager, "writes")
        waits = list(self.fixture.clock.waits) if hasattr(self.fixture.clock, "waits") else self.fixture.clock()
        first = session.apply_range_profile(self.profile())
        second = session.apply_range_profile(self.profile(name="another_visit"))
        self.assertEqual(first["changed_roles"], [])
        self.assertEqual(second["rule_name"], "another_visit")
        self.assertEqual(starts, self.counts(manager, "writes"))
        after = list(self.fixture.clock.waits) if hasattr(self.fixture.clock, "waits") else self.fixture.clock()
        self.assertEqual(waits, after)
        self.assertTrue(second["verified"])

    def test_range_context_is_a_detached_snapshot_without_queries(self):
        session, manager, _ = self.make()
        session.apply_range_profile(self.profile())
        starts = self.counts(manager, "queries")
        context = session.range_context()
        context["requested_full_scales_v"]["lockin_xx"] = 123
        context["changed_roles"].append("bad")
        self.assertEqual(session.range_context()["requested_full_scales_v"]["lockin_xx"], self.initial_scales["lockin_xx"])
        self.assertEqual(session.range_context()["changed_roles"], [])
        self.assertEqual(starts, self.counts(manager, "queries"))

    def test_invalid_or_incomplete_profile_rejected_before_instrument_io(self):
        session, manager, _ = self.make()
        invalid = [None, {}, {"name": "", "ranges": self.profile()["ranges"]},
            {"name": "x", "ranges": {"lockin_xx": .001}},
            {**self.profile(), "extra": True}]
        for value in (True, 0, -.001, math.nan, math.inf, .000123, "0.001"):
            invalid.append(self.profile(lockin_aux=value))
        for profile in invalid:
            before = (self.counts(manager, "queries"), self.counts(manager, "writes"))
            with self.subTest(profile=profile), self.assertRaises(ValueError):
                session.apply_range_profile(profile)
            self.assertEqual(before, (self.counts(manager, "queries"), self.counts(manager, "writes")))
        self.assertFalse(session.failed)

    def test_unconfigured_and_pending_reference_transition_rejected_before_io(self):
        session, manager, _ = self.make(configured=False)
        with self.assertRaises(RuntimeError):
            session.apply_range_profile(self.profile())
        self.assertEqual(manager.opened, [])
        session.open()
        session.configure()
        session.reference_transition_pending = True
        before = self.counts(manager, "queries")
        with self.assertRaises(RuntimeError):
            session.apply_range_profile(self.profile())
        self.assertEqual(before, self.counts(manager, "queries"))

    def test_external_settings_change_is_detected_before_any_range_write(self):
        session, manager, _ = self.make()
        manager.resources["FAKE::XX"].responses["RMOD?"] = "0"
        before = self.counts(manager, "writes")
        with self.assertRaisesRegex(ValueError, "model-specific"):
            session.apply_range_profile(self.profile(lockin_aux=.0002))
        self.assertTrue(session.failed)
        self.assertEqual(before, self.counts(manager, "writes"))
        self.assertFalse(session.range_context()["verified"])

    def test_partial_write_failure_retains_confirmed_roles_and_blocks_retry_and_cleanup_certification(self):
        session, manager, _ = self.make()

        def fail_aux(resource, command):
            if command.startswith("SCAL "):
                raise TimeoutError("uncertain AUX write")

        manager.resources["FAKE::AUX"].on_write = fail_aux
        with self.assertRaises(Sr865aError):
            session.apply_range_profile(self.profile(lockin_xx=.0002, lockin_aux=.0002))
        context = session.range_context()
        self.assertEqual(context["confirmed_full_scales_v"], {
            "lockin_xx": .0002, "lockin_xy": self.initial_scales["lockin_xy"], "lockin_aux": None})
        self.assertEqual(context["attempted_roles"], ["lockin_xx", "lockin_aux"])
        self.assertFalse(context["verified"])
        self.assertTrue(session.failed)
        self.assertEqual({cfg.role.value: cfg.sensitivity_full_scale_v for cfg in session.roles},
                         {"xx": .0002, "xy": self.initial_scales["lockin_xy"], "aux": .001})
        before = self.counts(manager, "writes")
        with self.assertRaises(RuntimeError):
            session.apply_range_profile(self.profile())
        with self.assertRaises(RuntimeError):
            session.sample_point()
        self.assertEqual(before, self.counts(manager, "writes"))
        self.assertEqual(session.settling.pending[-1]["roles"], ["lockin_aux"])
        cleanup = session.cleanup()
        self.assertTrue(cleanup["source_protection_verified"])
        self.assertFalse(cleanup["verified"])
        self.assertTrue(cleanup["manual_verification_required"])
        self.assertTrue(any("SR865A write failed" in error for error in cleanup["errors"]))

    def test_settle_interrupt_preserves_failure_and_pending_range_change(self):
        session, _, _ = self.make()

        def interrupt(seconds):
            raise KeyboardInterrupt("range settling interrupted")

        session.sleep = interrupt
        with self.assertRaises(KeyboardInterrupt):
            session.apply_range_profile(self.profile(lockin_aux=.0002))
        self.assertTrue(session.failed)
        self.assertFalse(session.range_context()["verified"])
        self.assertTrue(session.settling.pending)
        waits = [payload for kind, payload in self.fixture.events if kind == "lockin_settling_wait"]
        self.assertFalse(waits[-1]["settling_waits"][-1]["completed"])
        self.assertFalse(session.cleanup()["verified"])

    def test_write_readback_mismatch_is_unknown_and_not_accepted(self):
        session, manager, _ = self.make()
        aux = manager.resources["FAKE::AUX"]

        def old_scale(resource, command):
            if command == "SCAL?":
                resource.responses[command] = "9"

        aux.on_query = old_scale
        with self.assertRaises(RuntimeError):
            session.apply_range_profile(self.profile(lockin_aux=.0002))
        self.assertIsNone(session.range_context()["confirmed_full_scales_v"]["lockin_aux"])
        self.assertTrue(session.failed)

    def test_audit_failure_after_range_write_keeps_failure_and_source_cleanup(self):
        session, manager, _ = self.make()
        old_event = session.event

        def fail_applied(kind, payload):
            old_event(kind, deepcopy(payload))
            if kind == "lockin_range_profile_applied":
                raise OSError("profile audit unavailable")

        session.event = fail_applied
        with self.assertRaisesRegex(OSError, "audit unavailable"):
            session.apply_range_profile(self.profile(lockin_xx=.0002))
        self.assertTrue(session.failed)
        self.assertFalse(session.range_context()["verified"])
        self.assertEqual(session.range_context()["confirmed_full_scales_v"]["lockin_xx"], .0002)
        self.assertFalse(session.cleanup()["verified"])
        self.assertEqual(manager.resources["FAKE::XX"].responses["SLVL?"], "0.004")

    def test_io_audit_failure_after_success_is_not_left_verified(self):
        session, _, _ = self.make()
        original_event = session.event

        def fail_io(kind, payload):
            if kind == "lockin_model_io" and payload["stage"] == "apply_range_profile":
                raise OSError("range I/O audit unavailable")
            original_event(kind, payload)

        session.event = fail_io
        with self.assertRaisesRegex(OSError, "I/O audit unavailable"):
            session.apply_range_profile(self.profile(lockin_aux=.0002))
        self.assertTrue(session.failed)
        self.assertFalse(session.range_context()["verified"])
        failed_events = [payload for kind, payload in self.fixture.events
                         if kind == "lockin_range_profile_failed"]
        self.assertTrue(failed_events)
        self.assertFalse(failed_events[-1]["verified"])
        cleanup = session.cleanup()
        self.assertTrue(cleanup["source_protection_verified"])
        self.assertFalse(cleanup["verified"])

    def test_dual_pem_session_consumes_owned_range_latch_before_next_source_point(self):
        fixture = pem_fixtures.PhotonicsPointTests(methodName="runTest")
        session, manager, _, _ = fixture.ready()
        self.addCleanup(session.close)
        self.addCleanup(session.cleanup)
        original = session.config
        result = session.apply_range_profile({"name": "temperature_visit_2", "ranges": {
            "lockin_xx": .01, "lockin_xy": .01}})
        self.assertTrue(result["verified"])
        self.assertIs(session.config, original)
        self.assertEqual(original.lockin_xx.sensitivity_full_scale_v, .001)
        session.set_point(.004)
        session.qualify()
        reading = session.sample_point()
        self.assertTrue(reading["clean"])
        self.assertEqual(reading["status"]["lockin_range_profile"]["rule_name"], "temperature_visit_2")
        self.assertEqual(manager.resources["FAKE::XX"].responses["SCAL?"], "6")
        self.assertFalse(session.failed)


if __name__ == "__main__":
    unittest.main()
