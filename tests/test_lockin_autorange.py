import unittest

from attodry_control.lockin_autorange import (
    AutorangeAction,
    AutorangePolicy,
    AutorangeState,
    decide_autorange,
)


POLICY = AutorangePolicy(0.01, 0.02, 0.85, 2, 1)


class LockinAutorangeTests(unittest.TestCase):
    def test_keeps_safe_ten_millivolt_range(self) -> None:
        decision = decide_autorange(
            POLICY, AutorangeState(0.01), amplitude_v=0.005384, overload=False
        )
        self.assertEqual(decision.action, AutorangeAction.KEEP)
        self.assertAlmostEqual(decision.occupancy, 0.5384)

    def test_widens_at_target_occupancy(self) -> None:
        decision = decide_autorange(
            POLICY, AutorangeState(0.01), amplitude_v=0.0085, overload=False
        )
        self.assertEqual(decision.action, AutorangeAction.WIDEN)
        self.assertEqual(decision.state, AutorangeState(0.02, 1, 0))

    def test_overload_widens_immediately(self) -> None:
        decision = decide_autorange(
            POLICY, AutorangeState(0.01), amplitude_v=0.001, overload=True
        )
        self.assertEqual(decision.action, AutorangeAction.WIDEN)

    def test_fails_closed_when_widest_range_is_insufficient(self) -> None:
        decision = decide_autorange(
            POLICY, AutorangeState(0.02), amplitude_v=0.017, overload=False
        )
        self.assertEqual(decision.action, AutorangeAction.FAIL)

    def test_requires_two_samples_before_narrowing(self) -> None:
        first = decide_autorange(
            POLICY, AutorangeState(0.02), amplitude_v=0.008, overload=False
        )
        second = decide_autorange(
            POLICY, first.state, amplitude_v=0.0084, overload=False
        )
        self.assertEqual(first.action, AutorangeAction.KEEP)
        self.assertEqual(second.action, AutorangeAction.NARROW)
        self.assertEqual(second.state, AutorangeState(0.01, 1, 0))

    def test_unfit_sample_resets_narrowing_counter(self) -> None:
        first = decide_autorange(
            POLICY, AutorangeState(0.02), amplitude_v=0.008, overload=False
        )
        reset = decide_autorange(
            POLICY, first.state, amplitude_v=0.009, overload=False
        )
        self.assertEqual(reset.state.stable_fit_samples, 0)

    def test_adjustment_limit_prevents_oscillation(self) -> None:
        decision = decide_autorange(
            POLICY, AutorangeState(0.02, 1, 1), amplitude_v=0.008, overload=False
        )
        self.assertEqual(decision.action, AutorangeAction.KEEP)
        self.assertEqual(decision.state.current_full_scale_v, 0.02)

    def test_rejects_non_finite_or_out_of_bound_inputs(self) -> None:
        with self.assertRaises(ValueError):
            decide_autorange(
                POLICY, AutorangeState(0.005), amplitude_v=0.001, overload=False
            )
        with self.assertRaises(ValueError):
            decide_autorange(
                POLICY, AutorangeState(0.01), amplitude_v=float("nan"), overload=False
            )


if __name__ == "__main__":
    unittest.main()
