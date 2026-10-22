"""Post-qualification magnetic quality policy; real point code, fake DLL only."""
from dataclasses import replace
import unittest
from unittest.mock import patch

from attodry_control.attodry import AttoDryError, AttoDryTimeout, AttoDryDriver
from attodry_control.combination_analysis import load_combination_rows
from attodry_control.combination_hardware import load_hardware_combination
from attodry_control.cryostat_points import CryostatPointSession
from tests import test_combination_cryostat as fixture
from tests.optical_helpers import Clock


class MagneticQualityPolicyTests(unittest.TestCase):
    setUp = fixture.CryostatCombinationTests.setUp
    write_config = fixture.CryostatCombinationTests.write_config
    factory = fixture.CryostatCombinationTests.factory
    events = fixture.CryostatCombinationTests.events
    execute = fixture.CryostatCombinationTests.execute

    def config(self, policy="record_continue", order=("magnetic",)):
        self.write_config(order, extra='magnetic_deviation_policy = "' + policy + '"\n')
        return load_hardware_combination(self.path)

    def session(self, policy="record_continue"):
        config = self.config(policy)
        clock, evidence = Clock(), []
        session = CryostatPointSession(config.cryostat, config.temperature, config.magnetic,
            lambda kind, payload: evidence.append((kind, payload)), dll=self.dll,
            monotonic=clock, sleep=clock.sleep)
        session.open()
        self.addCleanup(session.close)
        session.apply("magnetic", config.plan.axes[0].points[0])
        session.qualify(("magnetic",))
        return session, evidence, clock

    def test_loader_default_enum_axis_and_snapshot(self):
        self.write_config(("magnetic",))
        config = load_hardware_combination(self.path)
        self.assertEqual(config.cryostat.magnetic_deviation_policy, "abort")
        self.assertTrue(config.cryostat.command_scoped_field)
        self.assertEqual(config.snapshot["field_limit_policy"], "command-scoped-configured-v1")
        config = self.config()
        self.assertEqual(config.snapshot["magnetic_deviation_policy"], "record_continue")
        self.assertEqual(config.snapshot["hardware"]["effective_cryostat"]["magnetic_deviation_policy"],
                         "record_continue")
        for value in ("continue", "", "RECORD_CONTINUE"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "magnetic_deviation_policy"):
                self.config(value)
        with self.assertRaisesRegex(ValueError, "requires the magnetic axis"):
            self.config(order=("temperature",))
        self.assertEqual(self.dll.events, [])

    def test_new_hardware_single_z_uses_configured_cap_without_instrument_access(self):
        source = self.base.replace('{ bx_t = 0.0, bz_t = 0.0 },',
                                   '{ bx_t = 0.0, bz_t = 5.0 },')
        self.write_config(("magnetic",), source=source)
        config = load_hardware_combination(self.path)
        self.assertEqual(config.magnetic.run.points[0].bz_t, 5.0)
        self.assertTrue(config.cryostat.command_scoped_field)
        self.assertEqual(config.cryostat.magnet.limits.hardware_z_max_t, 9.0)
        self.write_config(("magnetic",), source=source.replace(
            'hardware_z_max_t = 9.0', 'hardware_z_max_t = 3.0'))
        with self.assertRaises(ValueError):
            load_hardware_combination(self.path)
        source = self.base.replace('{ bx_t = 0.0, bz_t = 0.0 },',
                                   '{ bx_t = 2.2, bz_t = 2.2 },')
        self.write_config(("magnetic",), source=source)
        with self.assertRaises(ValueError):
            load_hardware_combination(self.path)
        self.assertEqual(self.dll.events, [])

    def test_target_warning_and_range_warning_continue_and_flatten(self):
        self.config(order=("magnetic", "smu"))
        self.path.write_text(self.path.read_text(encoding="utf-8").replace(
            'normal_end_field_policy = "hold"', 'normal_end_field_policy = "zero"'), encoding="utf-8")
        def modify(adapter):
            original = adapter.read
            def read():
                result = original()
                if adapter.read_count == 2:
                    self.dll.bx_t = .003
                return result
            adapter.read = read
        self.modify_adapter = modify
        result = self.execute("magnetic-warning")
        self.assertEqual(result["status"], "completed", result)
        rows = load_combination_rows(self.database, run_id="magnetic-warning")
        self.assertTrue(rows)
        row = rows[0]
        self.assertTrue(row["accepted"])
        self.assertTrue(row["magnetic_quality.warning"])
        self.assertEqual(row["magnetic_quality.policy"], "record_continue")
        self.assertIn("magnetic_outside_target_tolerance", row["magnetic_quality.quality_flags"])
        self.assertIn("magnetic_formal_range_exceeded", row["magnetic_quality.quality_flags"])
        self.assertGreater(row["actual.field_x_t"], 0)
        samples = row["status.magnetic"]["samples"]
        observation = next(x for x in samples if x["magnetic_assessment"]["quality_flags"])
        self.assertIn("captured_at_utc", observation)
        self.assertIsNotNone(observation["magnetic_assessment"]["qualification"])

    def test_range_only_uses_original_threshold_and_default_abort(self):
        for policy in ("record_continue", "abort"):
            with self.subTest(policy=policy):
                self.dll.bx_t = 0
                session, evidence, _ = self.session(policy)
                session.begin_sample()
                self.dll.bx_t = .001  # Below target tolerance, above window range.
                reading = session.read("magnetic")
                self.assertFalse(reading["status"]["magnetic_assessment"]["quality_flags"])
                if policy == "abort":
                    with self.assertRaisesRegex(AttoDryError, "Magnetic range exceeded"):
                        session.end_sample([reading])
                else:
                    session.end_sample([reading])
                    self.assertEqual(reading["status"]["magnetic_quality"]["quality_flags"],
                                     ["magnetic_formal_range_exceeded"])
                summary = next(p for k, p in reversed(evidence) if k == "environment_formal_window")
                self.assertTrue(summary["magnetic_quality"]["warning"])
                session.close()

    def test_target_default_abort_persists_quality_evidence(self):
        session, evidence, _ = self.session("abort")
        self.dll.bx_t = .003
        with self.assertRaisesRegex(AttoDryError, "Magnetic readback outside target tolerance"):
            session.capture("formal")
        observation = next(p for k, p in reversed(evidence) if k == "environment_sample")
        self.assertEqual(observation["magnetic_assessment"]["quality_flags"],
                         ["magnetic_outside_target_tolerance"])

    def test_missing_qualification_and_initial_timeout_cannot_continue(self):
        session, _, _ = self.session()
        session.field_qualification = None
        self.dll.bx_t = .003
        with self.assertRaisesRegex(AttoDryError, "no completed qualification"):
            session.capture("formal")
        self.dll.bx_t = 0
        self.config()
        with patch.object(AttoDryDriver, "wait_for_field", side_effect=AttoDryTimeout("injected initial timeout")):
            result = self.execute("initial-timeout")
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(load_combination_rows(self.database, run_id="initial-timeout"), ())

    def test_soft_warning_does_not_suppress_hard_or_unknown_state(self):
        session, evidence, _ = self.session()
        cases = [
            {"field_control": 0}, {"setpoint_x_t": .01},
            {"error_code": 35}, {"bz_t": 3.01},
            {"bx_t": float("nan")}, {"return_codes": {"get_field_x": 1}},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                self.dll.bx_t, self.dll.bz_t = .003, 0
                self.dll.setpoint_x_t = self.dll.setpoint_z_t = 0
                self.dll.field_control = 1
                self.dll.error_code = 0
                self.dll.return_codes = {}
                for key, value in changes.items():
                    setattr(self.dll, key, value)
                with self.assertRaises((AttoDryError, ValueError)):
                    session.capture("formal")
        self.dll.return_codes = {}
        self.dll.error_code = 0

    def test_policy_has_identical_healthy_io_and_wait_schedule(self):
        counts = []
        for policy in ("abort", "record_continue"):
            self.dll.bx_t = self.dll.bz_t = 0
            self.dll.setpoint_x_t = self.dll.setpoint_z_t = 0
            self.dll.field_control = 0
            self.dll.events.clear()
            session, _, clock = self.session(policy)
            session.begin_sample()
            reading = session.read("magnetic")
            session.end_sample([reading])
            counts.append((tuple(self.dll.events), clock()))
            session.close()
        self.assertEqual(counts[0], counts[1])

    def test_normal_nonzero_hold_warns_but_zero_hold_still_requires_actual_zero(self):
        self.base = self.base.replace('{ bx_t = 0.0, bz_t = 0.0 },',
                                      '{ bx_t = 0.01, bz_t = 0.0 },')
        session, _, _ = self.session()
        self.dll.bx_t = .013
        result = session.cleanup("magnetic", False)
        self.assertTrue(result["verified"])
        self.assertEqual(result["policy"], "hold")
        self.assertTrue(result["magnetic_quality"]["warning"])
        self.assertFalse(result["magnetic_quality"]["stability_certified"])
        session.close()
        self.base = self.base.replace('{ bx_t = 0.01, bz_t = 0.0 },',
                                      '{ bx_t = 0.0, bz_t = 0.0 },')
        self.dll.bx_t = self.dll.setpoint_x_t = 0
        session, _, _ = self.session()
        self.dll.bx_t = .003
        with self.assertRaisesRegex(AttoDryError, "Field hold readback outside tolerance"):
            session.cleanup("magnetic", False)


if __name__ == "__main__":
    unittest.main()
