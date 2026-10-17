"""Explicit one-mean tuning, with no claim of power-variation qualification."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from optical_helpers import EXAMPLE, ROOT, devices, scanner
from attodry_control.nkt_config import NktError, load_nkt_config
from attodry_control.nkt_control import SimulatedNkt
from attodry_control.optical_config import load_scan_config
from attodry_control.optical_points import OpticalPointSession, load_optical_point_config
from attodry_control.power_feedback import MeanPowerTarget


class MeanPowerPolicyTests(unittest.TestCase):
    def sample(self, sequence=1, start=1, end=2, power=1e-5):
        return {"sequence": sequence, "started_monotonic_s": start,
                "finished_monotonic_s": end, "power_w": power, "valid": True, "unit": "W"}

    def test_one_mean_is_target_evidence_and_never_stability_evidence(self):
        evidence = MeanPowerTarget(epoch_s=0, max_power_w=1e-3)
        first = evidence.add(self.sample())
        second = evidence.add(self.sample(2, 3, 4, 9e-4))
        self.assertEqual(first["count"], 1)
        self.assertEqual(second["mean_power_w"], 9e-4)
        self.assertEqual(second["sequences"], [2])
        self.assertFalse(second["power_window_stable"])
        self.assertNotIn("std_power_w", second)
        self.assertIn("not assessed", second["reason"])

    def test_mean_still_rejects_invalid_stale_duplicate_overlapping_and_hard_power(self):
        invalid = ({"valid": False}, {"unit": "V"}, {"power_w": float("nan")},
                   {"power_w": -1e-5}, {"power_w": 0.002}, {"sequence": True},
                   {"started_monotonic_s": -1}, {"finished_monotonic_s": 0})
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(NktError):
                MeanPowerTarget(epoch_s=0, max_power_w=1e-3).add({**self.sample(), **change})
        for sample in (self.sample(1, 3, 4), self.sample(0, 3, 4), self.sample(2, 1, 3)):
            evidence = MeanPowerTarget(epoch_s=0, max_power_w=1e-3)
            evidence.add(self.sample())
            with self.subTest(sample=sample), self.assertRaises(NktError):
                evidence.add(sample)

    def test_loader_is_explicit_and_legacy_default_unchanged(self):
        original = EXAMPLE.read_text(encoding="utf-8").replace('mode = "direct"', 'mode = "power_stabilized"')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "scan.toml"
            for mode in (None, "stable_window", "mean_target_only", "unknown", True):
                suffix = "" if mode is None else '\nqualification = ' + (
                    "true" if mode is True else '"' + mode + '"')
                path.write_text(original.replace("[power_feedback]", "[power_feedback]" + suffix), encoding="utf-8")
                if mode in ("unknown", True):
                    with self.subTest(mode=mode), self.assertRaisesRegex(NktError, "qualification"):
                        load_scan_config(path, load_nkt_config(path))
                else:
                    scan, _, _ = load_scan_config(path, load_nkt_config(path))
                    self.assertEqual(scan.feedback.qualification, mode or "stable_window")


class MeanPowerExecutionTests(unittest.TestCase):
    def source_session(self, provider=None, *, policy="record_continue", **feedback_changes):
        clock, _, _, _, pem, meter = devices()
        cfg = load_optical_point_config(ROOT / "config/optical_source_current_simulation.toml")
        feedback = replace(cfg.scan.feedback, qualification="mean_target_only", **feedback_changes)
        cfg = replace(cfg, scan=replace(cfg.scan, feedback=feedback, target_deviation_policy=policy))
        backend = SimulatedNkt(cfg.nkt)
        pem.config, meter.config = cfg.pem, cfg.pm
        meter.resource.power_provider = lambda: (provider(backend) if provider else feedback._targets()[0])
        session = OpticalPointSession(cfg, backend_factory=lambda _: backend, pem_factory=lambda _: pem,
            meter_factory=lambda _: meter, clock=clock, sleep=clock.sleep)
        self.addCleanup(session.close)
        session.open()
        session.configure()
        session.set_point(0)
        return session, backend, meter, clock

    def test_single_value_reaches_target_without_hold_or_window_wait_and_full_pm_checks(self):
        session, backend, meter, clock = self.source_session(hold_s=2)
        before = clock()
        session.qualify()
        result = session.row["feedback_result"]
        self.assertEqual(sum(sample["phase"] == "tuning" for sample in session.row["power_samples"]), 1)
        self.assertEqual(result["count"], 1)
        self.assertTrue(result["target_reached"])
        self.assertFalse(result["power_window_stable"])
        self.assertIn("hold_s", result["ignored_qualification_fields"])
        self.assertIn("window.min_samples", result["ignored_qualification_fields"])
        self.assertNotIn("window.sample_interval_s", result["ignored_qualification_fields"])
        self.assertEqual(result["iteration_interval_s"], session.feedback.window.sample_interval_s)
        self.assertLess(clock() - before, 2)
        session.begin_sample()
        row = session.read()
        session.end_sample([row])
        guards = row["status"]["sample_brackets"]
        self.assertEqual([g["phase"] for g in guards], ["before_electrical", "formal", "after_electrical"])
        for guard in guards:
            self.assertEqual(guard["power_sample"]["before"]["average_count"], meter.config.average_count)
            self.assertEqual(guard["power_sample"]["after"]["average_count"], meter.config.average_count)
            self.assertEqual(guard["power_target_assessment"]["qualification"], "mean_target_only")
        self.assertTrue(session.cleanup()["off_confirmed"])
        self.assertEqual(backend.source["emission_state"], 0)

    def test_mean_formal_ignores_relative_window_but_records_target_deviation(self):
        cfg = load_optical_point_config(ROOT / "config/optical_source_current_simulation.toml")
        strict_window = replace(cfg.scan.feedback.window, max_peak_to_peak_fraction=0,
                                max_drift_fraction_per_s=0, max_peak_to_peak_w=0)
        session, backend, meter, clock = self.source_session(window=strict_window)
        session.qualify()
        meter.resource.power_provider = lambda: session.feedback.target_power_w * 1.2
        writes = len(backend.commands)
        session.begin_sample()
        row = session.read()
        session.end_sample([row])
        assessment = row["status"]["power_target_assessment"]
        self.assertFalse(assessment["target_in_tolerance"])
        self.assertTrue(assessment["target_deviation_continued"])
        self.assertNotIn("stability_window", assessment)
        self.assertFalse(any(c[0] in {"emission", "configure"} for c in backend.commands[writes:]))
        self.assertTrue(session.cleanup()["off_confirmed"])

    def test_mean_formal_abort_policy_and_pm_setting_drift_still_fail_closed(self):
        for fault in ("target", "pm_setting"):
            session, backend, meter, clock = self.source_session(policy="abort")
            session.qualify()
            if fault == "target":
                meter.resource.power_provider = lambda: session.feedback.target_power_w * 1.2
            else:
                def changed():
                    meter.resource.values["SENS:AVER:COUN?"] = str(meter.config.average_count + 1)
                    return session.feedback.target_power_w
                meter.resource.power_provider = changed
            with self.subTest(fault=fault), self.assertRaises(NktError):
                session.begin_sample()
            self.assertFalse(session.qualified)
            self.assertTrue(session.cleanup()["off_confirmed"])

    def test_minimum_signal_hard_limit_and_shared_deadline_survive_mean_mode(self):
        for fault in ("dark", "high", "late"):
            session, backend, meter, clock = self.source_session()
            if fault == "dark":
                meter.resource.power_provider = lambda: session.feedback.minimum_signal_power_w / 2
            elif fault == "high":
                meter.resource.power_provider = lambda: session.feedback.max_power_w * 2
            else:
                def late():
                    clock.value += session.feedback.timeout_s + 1
                    return session.feedback.target_power_w
                meter.resource.power_provider = late
            with self.subTest(fault=fault), self.assertRaises(NktError):
                session.qualify()
            self.assertFalse(session.qualified)
            self.assertTrue(session.cleanup()["off_confirmed"])

    def test_adjustment_keeps_bounded_reversal_and_reduction_evidence(self):
        session, backend, meter, clock = self.source_session()
        values = iter((0.5, 1.5, 1.0))
        meter.resource.power_provider = lambda: session.feedback.target_power_w * next(values, 1.0)
        session.qualify()
        changes = session.row["source_current_iterations"]
        self.assertEqual(len(changes), 2)
        self.assertTrue(changes[-1]["direction_reversed"])
        for change in changes:
            self.assertLessEqual(abs(change["to_source_current_pct"] - change["from_source_current_pct"]),
                                 session.feedback.source_current_step_pct)
        self.assertTrue(session.row["feedback_result"]["target_reached"])
        self.assertTrue(session.cleanup()["off_confirmed"])

    def test_narrow_tuning_tolerance_is_not_replaced_by_the_wider_formal_band(self):
        session, backend, meter, clock = self.source_session(tuning_tolerance_fraction=0.02)
        factors = iter((0.95, 1.0))
        meter.resource.power_provider = lambda: session.feedback.target_power_w * next(factors, 1.0)
        session.qualify()
        first, last = session.row["feedback_windows"]
        self.assertTrue(first["target_in_tolerance"])
        self.assertFalse(first["target_in_tuning_tolerance"])
        self.assertFalse(first["target_reached"])
        self.assertTrue(last["target_reached"])
        self.assertEqual(len(session.row["source_current_iterations"]), 1)
        self.assertTrue(session.cleanup()["off_confirmed"])

    def test_each_correction_preserves_iteration_spacing_but_target_does_not_add_it(self):
        session, backend, meter, clock = self.source_session()
        interval = 0.2
        cfg = replace(session.feedback, window=replace(session.feedback.window, duration_s=0.2,
                                                     sample_interval_s=interval))
        session.feedback = cfg
        session.row["feedback_config"]["window"]["sample_interval_s"] = interval
        times = []
        values = iter((0.5, 1.5, 1.0))
        def power():
            times.append(clock())
            return cfg.target_power_w * next(values, 1.0)
        meter.resource.power_provider = power
        session.engine.nkt.start_emission(session.point)
        point, result, evidence = session.engine._stabilize(session.row, session.point,
            session.wavelength, clock() + cfg.timeout_s, cfg)
        self.assertEqual(len(times), 3)
        self.assertGreaterEqual(times[1] - times[0] + 1e-9, interval)
        self.assertGreaterEqual(times[2] - times[1] + 1e-9, interval)
        self.assertAlmostEqual(clock(), times[-1])
        self.assertEqual(result["iteration_interval_s"], interval)
        session.point = point
        self.assertTrue(session.cleanup()["off_confirmed"])

    def test_reduction_threshold_acts_on_the_single_mean_and_never_needs_window(self):
        cfg = load_optical_point_config(ROOT / "config/optical_source_current_simulation.toml")
        target = cfg.scan.feedback._targets()[0]
        session, backend, meter, clock = self.source_session(
            target_power_w=target, target_powers_w=None, reduce_above_power_w=target * 2,
            source_current_min_pct=0)
        values = iter((target * 3, target))
        meter.resource.power_provider = lambda: next(values, target)
        session.qualify()
        change = session.row["source_current_iterations"][0]
        self.assertLess(change["to_source_current_pct"], change["from_source_current_pct"])
        self.assertTrue(change["window"]["above_reduce_threshold"])
        self.assertFalse(change["window"]["power_window_stable"])
        self.assertEqual(change["window"]["count"], 1)
        self.assertTrue(session.cleanup()["off_confirmed"])

    def test_unreachable_current_and_late_audit_cannot_promote_a_single_mean(self):
        session, backend, meter, clock = self.source_session()
        meter.resource.power_provider = lambda: session.feedback.target_power_w / 2
        with self.assertRaisesRegex(NktError, "unreachable"):
            session.qualify()
        self.assertFalse(session.qualified)
        self.assertTrue(session.cleanup()["off_confirmed"])
        session, backend, meter, clock = self.source_session()
        event = session.engine.on_event
        def delayed(item):
            if item["phase"] == "feedback_window":
                clock.value += session.feedback.timeout_s + 1
            if event:
                event(item)
        session.engine.on_event = delayed
        with self.assertRaisesRegex(NktError, "timeout"):
            session.qualify()
        self.assertFalse(session.qualified)
        self.assertTrue(session.cleanup()["off_confirmed"])

    def test_standalone_mean_does_not_requalify_forever_on_false_stability_flag(self):
        scan = scanner(mode="power_stabilized", provider=lambda _: 0.001)
        scan.config = replace(scan.config, feedback=replace(scan.config.feedback,
                              qualification="mean_target_only", hold_s=2))
        result = scan.run()
        self.assertTrue(result["completed"], result["error"])
        for row in result["points"]:
            self.assertFalse(row["feedback_result"]["power_window_stable"])
            self.assertTrue(row["feedback_result"]["target_reached"])
            self.assertEqual(sum(s["phase"] == "tuning" for s in row["power_samples"]), 1)
        self.assertTrue(result["cleanup"]["nkt"]["off_confirmed"])


if __name__ == "__main__":
    unittest.main()
