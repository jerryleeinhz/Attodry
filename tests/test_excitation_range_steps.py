"""6221 range syntax compiles offline to the same guarded source grid."""
from dataclasses import asdict
import unittest
from unittest.mock import patch

from attodry_control.excitation_6221_config import parse_excitation_6221
from attodry_control.keithley6221_ac import Keithley6221Ac
from test_keithley6221_ac import Fake6221, source_config


class ExcitationRangeStepTests(unittest.TestCase):
    def table(self, *, current=None, frequency=None):
        table = asdict(source_config(current_rms_points_a=[1e-7], frequency_points_hz=[200.]))
        for points, ranges, value in (
                ("current_rms_points_a", "current_rms_ranges_a", current),
                ("frequency_points_hz", "frequency_ranges_hz", frequency)):
            if value is not None:
                del table[points]
                table[ranges] = value
        return table

    def test_requested_current_and_frequency_steps_match_points_and_literal_lists(self):
        expected = source_config(current_rms_points_a=[index * 1e-7 for index in range(1, 12)],
                                 frequency_points_hz=list(range(100, 501, 50)))
        steps = parse_excitation_6221(self.table(
            current=[{"min": 1e-7, "max": 11e-7, "scale": "linear", "step": 1e-7}],
            frequency=[{"min": 100, "max": 500, "scale": "linear", "step": 50}]))
        points = parse_excitation_6221(self.table(
            current=[{"min": 1e-7, "max": 11e-7, "scale": "linear", "points": 11}],
            frequency=[{"min": 100, "max": 500, "scale": "linear", "points": 9}]))
        self.assertEqual(steps, points)
        # Literal decimals avoid introducing arithmetic noise in the oracle.
        self.assertEqual(steps.current_rms_points_a,
                         (1e-7, 2e-7, 3e-7, 4e-7, 5e-7, 6e-7, 7e-7, 8e-7, 9e-7, 1e-6, 1.1e-6))
        self.assertEqual(steps.frequency_points_hz, expected.frequency_points_hz)
        self.assertEqual(len(steps.grid()), 99)
        self.assertEqual(steps.grid()[0], {"excitation_current_rms_a": 1e-7, "excitation_frequency_hz": 100.})
        self.assertEqual(steps.grid()[11], {"excitation_current_rms_a": 1e-7, "excitation_frequency_hz": 150.})
        self.assertEqual(steps.grid()[-1], {"excitation_current_rms_a": 1.1e-6, "excitation_frequency_hz": 500.})

    def test_decimal_step_does_not_accumulate_binary_roundoff(self):
        for minimum, maximum, step, expected in (
                (.1, .3, .1, (.1, .2, .3)),
                (.001, .004, .001, (.001, .002, .003, .004)),
                (100., 100.3, .1, (100., 100.1, 100.2, 100.3))):
            with self.subTest(minimum=minimum):
                actual = parse_excitation_6221(self.table(frequency=[{
                    "min": minimum, "max": maximum, "scale": "linear", "step": step}]))
                self.assertEqual(actual.frequency_points_hz, expected)

    def test_mixed_step_and_points_segments_preserve_order_and_repeated_endpoints(self):
        actual = parse_excitation_6221(self.table(current=[
            {"min": 2e-7, "max": 4e-7, "scale": "linear", "step": 1e-7},
            {"min": 4e-7, "max": 5e-7, "scale": "linear", "points": 2},
            {"min": 1e-7, "max": 2e-7, "scale": "linear", "step": 1e-7}]))
        self.assertEqual(actual.current_rms_points_a, (2e-7, 3e-7, 4e-7, 4e-7, 5e-7, 1e-7, 2e-7))

    def test_linear_segments_require_exactly_one_step_or_points(self):
        segment = {"min": 100, "max": 500, "scale": "linear"}
        for bad in (segment, dict(segment, points=9, step=50), dict(segment, step=50, count=9)):
            with self.subTest(segment=bad), self.assertRaises(ValueError):
                parse_excitation_6221(self.table(frequency=[bad]))

    def test_invalid_steps_reject_for_both_coordinates(self):
        for coordinate, minimum, maximum in (("current", 1e-7, 11e-7), ("frequency", 100, 500)):
            for step in (True, False, "1", None, 0, -1, float("nan"), float("inf"), -float("inf")):
                with self.subTest(coordinate=coordinate, step=step), self.assertRaises(ValueError):
                    parse_excitation_6221(self.table(**{coordinate: [{
                        "min": minimum, "max": maximum, "scale": "linear", "step": step}]}))

    def test_nondividing_or_oversize_step_rejects_instead_of_changing_endpoints(self):
        for coordinate, minimum, maximum, steps in (
                ("current", 1e-7, 11e-7, (3e-7, 2e-6)),
                ("frequency", 100, 500, (60, 500))):
            for step in steps:
                with self.subTest(coordinate=coordinate, step=step), self.assertRaises(ValueError):
                    parse_excitation_6221(self.table(**{coordinate: [{
                        "min": minimum, "max": maximum, "scale": "linear", "step": step}]}))

    def test_logarithmic_ranges_remain_points_only(self):
        for segment in (
                {"min": 100, "max": 1000, "scale": "log", "step": 100},
                {"min": 100, "max": 1000, "scale": "log", "points": 3, "step": 100}):
            with self.subTest(segment=segment), self.assertRaises(ValueError):
                parse_excitation_6221(self.table(frequency=[segment]))
        actual = parse_excitation_6221(self.table(frequency=[{
            "min": 100, "max": 10000, "scale": "log", "points": 3}]))
        self.assertEqual(actual.frequency_points_hz[0], 100)
        self.assertAlmostEqual(actual.frequency_points_hz[1], 1000)
        self.assertEqual(actual.frequency_points_hz[-1], 10000)

    def test_step_counts_and_cartesian_limit_are_checked_before_allocation(self):
        cases = [
            self.table(frequency=[{"min": 100, "max": 500, "scale": "linear", "step": 1e-300}]),
            self.table(current=[{"min": 1e-7, "max": 11e-7, "scale": "linear", "step": 1e-300}]),
            self.table(frequency=[{"min": .001, "max": 50.001, "scale": "linear", "step": .001}] * 2),
        ]
        product = self.table(current=[{"min": 1e-7, "max": 5e-7, "scale": "linear", "step": 8e-12}])
        product["frequency_points_hz"] = [100, 500]
        cases.append(product)
        with patch("attodry_control.excitation_6221_config._expand_ranges",
                   side_effect=AssertionError("range allocation must not happen")):
            for table in cases:
                with self.subTest(table=table), self.assertRaisesRegex(ValueError, "100000"):
                    parse_excitation_6221(table)

    def test_exactly_100000_generated_points_remains_permitted(self):
        actual = parse_excitation_6221(self.table(frequency=[{
            "min": .001, "max": 100., "scale": "linear", "step": .001}]))
        self.assertEqual(len(actual.frequency_points_hz), 100000)
        self.assertEqual(actual.frequency_points_hz[0], .001)
        self.assertEqual(actual.frequency_points_hz[-1], 100.)

    def test_step_points_retain_existing_current_and_frequency_protections(self):
        unsafe = self.table(current=[{"min": 1e-7, "max": 11e-7, "scale": "linear", "step": 1e-7}])
        unsafe["max_current_peak_a"] = 1e-6
        with self.assertRaisesRegex(ValueError, "peak current"):
            parse_excitation_6221(unsafe)
        for segment in (
                {"min": .001, "max": 100001, "scale": "linear", "step": 1},
                {"min": 0, "max": 500, "scale": "linear", "step": 50}):
            with self.subTest(segment=segment), self.assertRaises(ValueError):
                parse_excitation_6221(self.table(frequency=[segment]))

    def test_list_points_and_step_forms_have_identical_fake_source_io(self):
        explicit = source_config(current_rms_points_a=[1e-7, 2e-7, 3e-7], frequency_points_hz=[100, 150])
        configurations = [explicit]
        for count_key, current_count, frequency_count in (("points", 3, 2), ("step", 1e-7, 50)):
            configurations.append(parse_excitation_6221(self.table(
                current=[{"min": 1e-7, "max": 3e-7, "scale": "linear", count_key: current_count}],
                frequency=[{"min": 100, "max": 150, "scale": "linear", count_key: frequency_count}])))
        traces = []
        for config in configurations:
            resource = Fake6221()
            driver = Keithley6221Ac(resource, config, authorize_writes=True)
            driver.preflight()
            driver.configure()
            for point in config.grid():
                driver.set_point(point["excitation_current_rms_a"], point["excitation_frequency_hz"])
                driver.readback()
            self.assertTrue(driver.cleanup()["verified"])
            traces.append((resource.commands, resource.queries))
        self.assertEqual(traces[0], traces[1])
        self.assertEqual(traces[0], traces[2])


if __name__ == "__main__":
    unittest.main()
