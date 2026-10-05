from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from optical_helpers import EXAMPLE
from attodry_control.nkt_config import NktError, load_nkt_config
from attodry_control.optical_config import FeedbackConfig, WindowConfig, load_scan_config
from attodry_control.power_feedback import next_nd_pct, next_source_current_pct


def current_feedback():
    return FeedbackConfig(1e-5, 1e-6, 5e-5, 10, 1, None, None, None, None,
                          WindowConfig(1, 3, 0.1, 1e-6), actuator="source_current",
                          source_current_min_pct=0, source_current_max_pct=8,
                          source_current_step_pct=1, minimum_signal_power_w=1e-6)


class SourceCurrentPolicyTests(unittest.TestCase):
    def test_direction_uses_a_bounded_step_not_power_ratio(self):
        config = current_feedback()
        self.assertEqual(next_source_current_pct(4, 5e-6, config), 5)
        self.assertEqual(next_source_current_pct(4, 1.1e-6, config), 5)
        self.assertEqual(next_source_current_pct(4, 2e-5, config), 3)
        self.assertEqual(next_source_current_pct(4, 4.9e-5, config), 3)
        self.assertEqual(next_source_current_pct(0, 5e-6, config), 1)

    def test_target_tolerance_is_inclusive_and_current_bounds_are_not_overshot(self):
        config = current_feedback()
        for power in (9e-6, 1e-5, 1.1e-5):
            with self.subTest(power=power):
                self.assertEqual(next_source_current_pct(4, power, config), 4)
        self.assertEqual(next_source_current_pct(7.8, 5e-6, config), 8)
        self.assertEqual(next_source_current_pct(0.2, 2e-5, config), 0)
        self.assertEqual(next_source_current_pct(4, 5e-6, config, step_pct=0.1), 4.1)
        self.assertEqual(next_source_current_pct(4, 2e-5, config, step_pct=0.5), 3.5)

    def test_unreachable_invalid_signal_and_unrepresentable_requests_rejected(self):
        config = current_feedback()
        for current, power in ((8, 5e-6), (0, 2e-5), (4, 0), (4, -1e-6), (4, 1e-9),
                               (4, 5.1e-5), (4, float("nan")), (4, float("inf")),
                               (True, 5e-6), (4.01, 5e-6), (-0.1, 5e-6), (8.1, 5e-6)):
            with self.subTest(current=current, power=power), self.assertRaises(NktError):
                next_source_current_pct(current, power, config)
        for step in (0, 0.05, 0.15, 1.1, True, float("nan")):
            with self.subTest(step=step), self.assertRaises(NktError):
                next_source_current_pct(4, 5e-6, config, step_pct=step)

    def test_actuator_rules_cannot_be_silently_substituted(self):
        current = current_feedback()
        nd = FeedbackConfig(1e-5, 1e-6, 5e-5, 10, 1, 0, 80, 1, False,
                            current.window)
        self.assertEqual(nd.actuator, "varia_nd")
        self.assertEqual(next_nd_pct(20, 2e-5, nd), 21)
        with self.assertRaises(NktError):
            next_nd_pct(20, 2e-5, current)
        with self.assertRaises(NktError):
            next_source_current_pct(4, 2e-5, nd)

    def test_minimum_signal_rejects_positive_dark_noise_before_current_change(self):
        config = current_feedback()
        for current in (0, 4, 8):
            with self.subTest(current=current), self.assertRaisesRegex(NktError, "below minimum"):
                next_source_current_pct(current, 1e-9, config)
        self.assertEqual(next_source_current_pct(4, config.minimum_signal_power_w, config), 5)


class SourceCurrentConfigTests(unittest.TestCase):
    def example(self):
        content = EXAMPLE.read_text().replace('mode = "direct"', 'mode = "power_stabilized"')
        content = content.replace(', nd_pct = 20.0', '')
        for line in ('nd_min_pct = 0.0\n', 'nd_max_pct = 80.0\n', 'nd_step_pct = 5.0\n',
                     'increasing_nd_increases_power = false\n'):
            content = content.replace(line, '')
        return content.replace('[power_feedback]\n',
                               '[power_feedback]\nactuator = "source_current"\n'
                               'source_current_min_pct = 0.0\nsource_current_max_pct = 8.0\n'
                               'source_current_step_pct = 1.0\nminimum_signal_power_w = 0.000001\n')

    def load(self, content):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(content, encoding="utf-8")
            nkt = load_nkt_config(path)
            scan, pem, pm = load_scan_config(path, nkt)
            return nkt, scan, pem, pm

    def test_source_loader_does_not_require_unused_nd_fields_and_resolves_targets(self):
        nkt, scan, pem, pm = self.load(self.example())
        config = scan.feedback
        self.assertEqual(config.actuator, "source_current")
        self.assertTrue(all(point.nd_pct is None for point in nkt.points))
        for field in ('nd_min_pct', 'nd_max_pct', 'nd_step_pct', 'increasing_nd_increases_power'):
            self.assertIsNone(getattr(config, field))
        scan.validate(replace(nkt, backend="nkt_sdk"), pem, pm)
        # ND is deliberately unverified: source feedback still has a defined actuator.
        self.assertFalse(nkt.varia_nd_control_verified)
        plan = replace(current_feedback(), target_power_w=None, target_tolerance_w=None,
                       target_powers_w=(1e-5, 2e-5), target_tolerance_fraction=0.10)
        resolved = plan.for_point(1, 2)
        self.assertEqual(resolved.actuator, "source_current")
        self.assertEqual(resolved.source_current_max_pct, 8)
        self.assertAlmostEqual(resolved.target_tolerance_w, 2e-6)
        self.assertEqual(next_source_current_pct(4, 1e-5, resolved), 5)

    def test_mode_specific_missing_conflicting_and_unknown_fields_rejected(self):
        original = self.example()
        invalid = (
            original.replace('source_current_min_pct = 0.0\n', ''),
            original.replace('source_current_max_pct = 8.0\n', ''),
            original.replace('source_current_step_pct = 1.0\n', ''),
            original.replace('minimum_signal_power_w = 0.000001\n', ''),
            original.replace('actuator = "source_current"', 'actuator = "unknown"'),
            original.replace('source_current_step_pct = 1.0', 'source_current_step = 1.0'),
            original.replace('source_current_step_pct = 1.0', 'source_current_step_pct = true'),
            original.replace('source_current_step_pct = 1.0', 'source_current_step_pct = 0.15'),
            original.replace('source_current_min_pct = 0.0', 'source_current_min_pct = 9.0'),
            original.replace('actuator = "source_current"',
                             'actuator = "source_current"\nnd_min_pct = 0.0'),
            original.replace('actuator = "source_current"',
                             'actuator = "source_current"\nincreasing_nd_increases_power = false'),
        )
        for content in invalid:
            with self.subTest(content=content), self.assertRaises(NktError):
                self.load(content)
        nd_content = EXAMPLE.read_text().replace('mode = "direct"', 'mode = "power_stabilized"')
        with self.assertRaises(NktError):
            self.load(nd_content.replace('[power_feedback]',
                                        '[power_feedback]\nsource_current_min_pct = 0.0'))
        for field in ('nd_min_pct', 'nd_max_pct', 'nd_step_pct', 'increasing_nd_increases_power'):
            content = '\n'.join(line for line in nd_content.splitlines() if not line.startswith(field + ' ='))
            with self.subTest(field=field), self.assertRaises(NktError):
                self.load(content)

    def test_current_mode_initial_level_nd_preservation_and_source_cap_are_checked(self):
        nkt, scan, pem, pm = self.load(self.example())
        for changed in (replace(nkt, source_control="power"),
                        replace(nkt, max_level_pct=7, points=tuple(replace(p, source_level_pct=7) for p in nkt.points)),
                        replace(nkt, points=(replace(nkt.points[0], nd_pct=20),)),
                        replace(nkt, allowed_pp_ratios=(2,),
                                points=(replace(nkt.points[0], pulse_picker_ratio=2),)),
                        replace(nkt, points=(replace(nkt.points[0], source_level_pct=9),))):
            with self.subTest(changed=changed), self.assertRaises(NktError):
                scan.validate(changed, pem, pm)
        lower_bounded = replace(scan, feedback=replace(scan.feedback, source_current_min_pct=1))
        zero_initial = replace(nkt, points=(replace(nkt.points[0], source_level_pct=0),))
        with self.assertRaises(NktError):
            lower_bounded.validate(zero_initial, pem, pm)
        scan.validate(zero_initial, pem, pm)
        above_feedback_bound = replace(nkt, max_level_pct=9,
                                       points=(replace(nkt.points[0], source_level_pct=9),))
        with self.assertRaises(NktError):
            scan.validate(above_feedback_bound, pem, pm)

    def test_existing_nd_mode_and_50uw_upper_target_bound_remain_enforced(self):
        current = current_feedback()
        for field, value in (("source_current_min_pct", -0.1),
                             ("source_current_max_pct", 100.1),
                             ("source_current_step_pct", 0),
                             ("source_current_step_pct", float("nan")),
                             ("source_current_max_pct", True)):
            with self.subTest(field=field, value=value), self.assertRaises(NktError):
                replace(current, **{field: value}).validate()
        with self.assertRaises(NktError):
            replace(current, target_power_w=4.6e-5, target_tolerance_w=None,
                    target_tolerance_fraction=0.10).validate()
        replace(current, target_power_w=4.5e-5, target_tolerance_w=None,
                target_tolerance_fraction=0.10).validate()
        nkt = load_nkt_config(EXAMPLE)
        scan, pem, pm = load_scan_config(EXAMPLE, nkt)
        nd = replace(current, actuator="varia_nd", source_current_min_pct=None,
                     source_current_max_pct=None, source_current_step_pct=None,
                     minimum_signal_power_w=None,
                     nd_min_pct=0, nd_max_pct=80, nd_step_pct=1,
                     increasing_nd_increases_power=False)
        nd_scan = replace(scan, mode="power_stabilized", feedback=nd)
        with self.assertRaisesRegex(NktError, "unverified"):
            nd_scan.validate(replace(nkt, backend="nkt_sdk"), pem, pm)
        nd_scan.validate(replace(nkt, backend="nkt_sdk", varia_nd_control_verified=True), pem, pm)

    def test_signal_threshold_strictly_below_all_target_lower_bounds(self):
        current = current_feedback()
        for value in (None, 0, -1e-6, True, float("nan"), float("inf"), 9e-6, 1e-5):
            with self.subTest(value=value), self.assertRaises(NktError):
                replace(current, minimum_signal_power_w=value).validate()
        multi = replace(current, target_power_w=None, target_tolerance_w=None,
                        target_powers_w=(2e-5, 5e-6), target_tolerance_fraction=0.10)
        multi.validate()
        with self.assertRaises(NktError):
            replace(multi, minimum_signal_power_w=4.5e-6).validate()
        nd = FeedbackConfig(1e-5, 1e-6, 5e-5, 10, 1, 0, 80, 1, False,
                            current.window, minimum_signal_power_w=1e-6)
        with self.assertRaises(NktError):
            nd.validate()

    def test_optional_soft_threshold_loads_resolves_and_preserves_hard_limit(self):
        current = current_feedback()
        self.assertIsNone(current.reduce_above_power_w)
        current.validate()
        plan = replace(current, target_power_w=None, target_tolerance_w=None,
                       target_powers_w=(5e-6, 22.5e-6, 40e-6), target_tolerance_fraction=0.10,
                       max_power_w=500e-6, reduce_above_power_w=50e-6)
        plan.validate()
        resolved = plan.for_point(2, 3)
        self.assertEqual(resolved.reduce_above_power_w, 50e-6)
        self.assertEqual(resolved.max_power_w, 500e-6)
        self.assertAlmostEqual(resolved.target_tolerance_w, 4e-6)
        content = self.example().replace('target_power_w = 0.001', 'target_power_w = 0.00001').replace(
            'target_tolerance_w = 0.00003', 'target_tolerance_w = 0.000001').replace(
            'max_power_w = 0.005', 'max_power_w = 0.0005').replace(
            'actuator = "source_current"', 'actuator = "source_current"\nreduce_above_power_w = 0.00005')
        loaded = self.load(content)[1].feedback
        self.assertEqual(loaded.reduce_above_power_w, 50e-6)
        self.assertEqual(loaded.max_power_w, 500e-6)
        with self.assertRaises(NktError):
            next_source_current_pct(4, 501e-6, loaded)

    def test_soft_threshold_rejects_invalid_values_and_conflicting_targets(self):
        current = current_feedback()
        for value in (0, -1e-6, True, False, float("nan"), float("inf"), 50e-6, 51e-6, 10e-6):
            with self.subTest(value=value), self.assertRaises(NktError):
                replace(current, reduce_above_power_w=value).validate()
        replace(current, reduce_above_power_w=current.target_power_w + current.target_tolerance_w).validate()
        multi = replace(current, target_power_w=None, target_tolerance_w=None,
                        target_powers_w=(1e-5, 4e-5), target_tolerance_fraction=0.10,
                        reduce_above_power_w=3e-5)
        with self.assertRaisesRegex(NktError, "Target tolerance"):
            multi.validate()
        nd = FeedbackConfig(1e-5, 1e-6, 5e-5, 10, 1, 0, 80, 1, False,
                            current.window, reduce_above_power_w=3e-5)
        with self.assertRaisesRegex(NktError, "requires the source_current actuator"):
            nd.validate()


if __name__ == "__main__":
    unittest.main()
