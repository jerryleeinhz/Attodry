from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from optical_helpers import EXAMPLE
from attodry_control.nkt_config import NktError, load_nkt_config
from attodry_control.optical_config import FeedbackConfig, WindowConfig, load_scan_config


class PowerScanConfigTests(unittest.TestCase):
    def feedback(self):
        return FeedbackConfig(1e-5, 1e-6, 5e-5, 10, 1, 0, 100, 1, False,
                              WindowConfig(1, 3, 0.1, 1e-6))

    def load(self, content):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text(content, encoding="utf-8")
            return load_scan_config(path, load_nkt_config(path))[0]

    def example(self):
        return EXAMPLE.read_text().replace('mode = "direct"', 'mode = "power_stabilized"')

    def test_existing_scalar_absolute_api_and_toml_remain_compatible(self):
        original = self.feedback()
        original.validate()
        for index in (0, 1, 9999):
            self.assertEqual(original.for_point(index, 10000), original)
        loaded = self.load(self.example()).feedback
        self.assertEqual(loaded.target_power_w, 0.001)
        self.assertEqual(loaded.target_tolerance_w, 0.00003)
        self.assertIsNone(loaded.target_powers_w)
        self.assertIsNone(loaded.target_tolerance_fraction)
        self.assertEqual(loaded.for_point(1, 2), loaded)

    def test_relative_tolerance_resolves_per_target_and_leaves_plan_immutable(self):
        config = replace(self.feedback(), target_power_w=None, target_tolerance_w=None,
                         target_powers_w=(1e-5, 2e-5), target_tolerance_fraction=0.10)
        config.validate()
        first, second = config.for_point(0, 2), config.for_point(1, 2)
        self.assertEqual(first.target_power_w, 1e-5)
        self.assertAlmostEqual(first.target_tolerance_w, 1e-6)
        self.assertEqual(second.target_power_w, 2e-5)
        self.assertAlmostEqual(second.target_tolerance_w, 2e-6)
        self.assertEqual(config.target_powers_w, (1e-5, 2e-5))
        for resolved in (first, second):
            self.assertIsNone(resolved.target_powers_w)
            self.assertIsNone(resolved.target_tolerance_fraction)
            resolved.validate()
        zero = replace(config, target_tolerance_fraction=0).for_point(0, 2)
        self.assertEqual(zero.target_tolerance_w, 0)

    def test_toml_list_and_fraction_load_as_strict_tuple(self):
        content = self.example().replace('target_power_w = 0.001',
                                        'target_powers_w = [0.001, 0.002]').replace(
            'target_tolerance_w = 0.00003', 'target_tolerance_fraction = 0.10')
        feedback = self.load(content).feedback
        self.assertEqual(feedback.target_powers_w, (0.001, 0.002))
        self.assertIsNone(feedback.target_power_w)
        self.assertIsNone(feedback.target_tolerance_w)
        self.assertAlmostEqual(feedback.for_point(1, 2).target_tolerance_w, 0.0002)
        # Scalar-relative and list-absolute remain independent supported choices.
        scalar = self.load(content.replace('target_powers_w = [0.001, 0.002]',
                                          'target_power_w = 0.001')).feedback
        self.assertAlmostEqual(scalar.for_point(1, 2).target_tolerance_w, 0.0001)
        absolute = self.load(content.replace('target_tolerance_fraction = 0.10',
                                            'target_tolerance_w = 0.00003')).feedback
        self.assertEqual(absolute.for_point(1, 2).target_tolerance_w, 0.00003)

    def test_missing_conflicting_and_unknown_target_or_tolerance_rejected(self):
        original = self.example()
        invalid = (
            original.replace('target_power_w = 0.001', ''),
            original.replace('target_tolerance_w = 0.00003', ''),
            original.replace('target_power_w = 0.001',
                             'target_power_w = 0.001\ntarget_powers_w = [0.001, 0.002]'),
            original.replace('target_tolerance_w = 0.00003',
                             'target_tolerance_w = 0.00003\ntarget_tolerance_fraction = 0.10'),
            original.replace('target_power_w = 0.001', 'target_power_ws = [0.001, 0.002]'),
        )
        for content in invalid:
            with self.subTest(content=content), self.assertRaises(NktError):
                self.load(content)
        for config in (replace(self.feedback(), target_power_w=None),
                       replace(self.feedback(), target_powers_w=(1e-5,)),
                       replace(self.feedback(), target_tolerance_w=None),
                       replace(self.feedback(), target_tolerance_fraction=0.10)):
            with self.subTest(config=config), self.assertRaises(NktError):
                config.validate()

    def test_list_shape_length_and_point_indices_are_checked(self):
        original = self.example()
        for raw in ('[]', '[0.001]', '[0.001, 0.002, 0.003]', '0.001', '"0.001, 0.002"', 'true'):
            with self.subTest(raw=raw), self.assertRaises(NktError):
                self.load(original.replace('target_power_w = 0.001', f'target_powers_w = {raw}'))
        for targets in ((), (1e-5,) * 10001, [1e-5], "1e-5"):
            with self.subTest(targets=type(targets)), self.assertRaises(NktError):
                replace(self.feedback(), target_power_w=None, target_powers_w=targets).validate()
        config = replace(self.feedback(), target_power_w=None, target_powers_w=(1e-5, 2e-5))
        for index, count in ((0, 1), (0, 3), (-1, 2), (2, 2), (True, 2),
                             (0, 0), (0, 10001), (0, True), (0.5, 2)):
            with self.subTest(index=index, count=count), self.assertRaises(NktError):
                config.for_point(index, count)

    def test_nonfinite_boolean_nonpositive_targets_and_invalid_tolerances_rejected(self):
        for target in (float("nan"), float("inf"), -float("inf"), True, False, 0, -1e-5):
            for config in (replace(self.feedback(), target_power_w=target),
                           replace(self.feedback(), target_power_w=None,
                                   target_powers_w=(1e-5, target))):
                with self.subTest(target=target, config=config), self.assertRaises(NktError):
                    config.validate()
        for tolerance in (float("nan"), float("inf"), True, -1, 1.01):
            config = replace(self.feedback(), target_tolerance_w=None,
                             target_tolerance_fraction=tolerance)
            with self.subTest(tolerance=tolerance), self.assertRaises(NktError):
                config.validate()
        for raw in ('[0.001, nan]', '[0.001, inf]', '[0.001, true]', '[0.001, 0.0]'):
            with self.subTest(raw=raw), self.assertRaises(NktError):
                self.load(self.example().replace('target_power_w = 0.001', f'target_powers_w = {raw}'))

    def test_all_target_upper_tolerance_bounds_checked_before_scan_construction(self):
        original = self.feedback()
        replace(original, target_power_w=5e-5, target_tolerance_w=0).validate()
        for config in (replace(original, target_power_w=5e-5),
                       replace(original, target_power_w=None, target_powers_w=(1e-5, 5e-5)),
                       replace(original, target_power_w=None, target_tolerance_w=None,
                               target_powers_w=(1e-5, 4.6e-5), target_tolerance_fraction=0.10),
                       replace(original, target_power_w=None, target_powers_w=(1e-5, 5.1e-5)),
                       replace(original, target_tolerance_w=1.1e-5)):
            with self.subTest(config=config), self.assertRaises(NktError):
                config.validate()
        accepted = replace(original, target_power_w=None, target_tolerance_w=None,
                           target_powers_w=(1e-5, 4.5e-5), target_tolerance_fraction=0.10)
        accepted.validate()
        accepted.for_point(1, 2).validate()
        content = self.example().replace('target_power_w = 0.001',
                                        'target_powers_w = [0.00001, 0.000046]').replace(
            'target_tolerance_w = 0.00003', 'target_tolerance_fraction = 0.10').replace(
            'max_power_w = 0.005', 'max_power_w = 0.00005')
        with self.assertRaises(NktError):
            self.load(content)


if __name__ == "__main__":
    unittest.main()
