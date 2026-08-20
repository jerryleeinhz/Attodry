import unittest

from attodry_control.stability import StabilityCriteria, TimedValue, evaluate_stability


CRITERIA = StabilityCriteria(
    tolerance=0.01,
    stable_range=0.004,
    dwell_s=10.0,
    minimum_samples=3,
)


class StabilityTests(unittest.TestCase):
    def test_continuous_dwell_window_is_stable(self) -> None:
        samples = [
            TimedValue(0.0, 1.999),
            TimedValue(5.0, 2.001),
            TimedValue(10.0, 2.000),
        ]

        self.assertTrue(evaluate_stability(samples, 2.0, CRITERIA))

    def test_out_of_tolerance_sample_breaks_stability(self) -> None:
        samples = [
            TimedValue(0.0, 2.0),
            TimedValue(5.0, 2.02),
            TimedValue(10.0, 2.0),
        ]

        self.assertFalse(evaluate_stability(samples, 2.0, CRITERIA))

    def test_excessive_range_breaks_stability(self) -> None:
        samples = [
            TimedValue(0.0, 1.997),
            TimedValue(5.0, 2.003),
            TimedValue(10.0, 2.0),
        ]

        self.assertFalse(evaluate_stability(samples, 2.0, CRITERIA))

    def test_insufficient_dwell_is_not_stable(self) -> None:
        samples = [
            TimedValue(1.0, 2.0),
            TimedValue(5.0, 2.0),
            TimedValue(10.0, 2.0),
        ]

        self.assertFalse(evaluate_stability(samples, 2.0, CRITERIA))

    def test_non_monotonic_samples_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "time ordered"):
            evaluate_stability(
                [TimedValue(1.0, 2.0), TimedValue(0.5, 2.0)],
                2.0,
                CRITERIA,
            )


if __name__ == "__main__":
    unittest.main()
