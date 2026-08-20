from datetime import UTC, datetime
import unittest
from unittest.mock import patch

from attodry_control.cleanup import (
    CleanupAction,
    cleanup_after_failure,
    cleanup_after_normal_completion,
)
from attodry_control.config import load_config
from attodry_control.models import LockinRole, VectorField
from attodry_control.records import AttemptStatus, ExperimentCondition
from attodry_control.simulation import SimulationStation


def condition(field: VectorField = VectorField(0.0, 0.0)) -> ExperimentCondition:
    return ExperimentCondition(
        condition_id="condition-0001",
        sequence_index=0,
        temperature_k=2.0,
        field=field,
        excitation_v=0.004,
        frequency_hz=17.777,
        gate_top_v=0.1,
        gate_bottom_v=-0.1,
    )


class SimulationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.station = SimulationStation.from_config(load_config("config/simulation.toml"))

    def test_complete_attempt_returns_six_accepted_raw_readings(self) -> None:
        outcome = self.station.run_attempt(
            condition(), attempt_index=0, now=lambda: datetime.now(UTC)
        )

        self.assertEqual(outcome.attempt.status, AttemptStatus.ACCEPTED)
        self.assertTrue(outcome.attempt.accepted)
        self.assertEqual(len(outcome.raw_readings), 6)
        self.assertEqual(len(outcome.station_samples), 1)
        self.assertTrue(outcome.station_samples[0].safe_for_acceptance)
        self.assertIsNotNone(outcome.accepted_result)
        self.assertIsNone(outcome.cleanup)

    def test_temperature_timeout_rejects_attempt_and_runs_cleanup(self) -> None:
        self.station.cryostat.temperature_stuck = True

        outcome = self.station.run_attempt(
            condition(), attempt_index=0, now=lambda: datetime.now(UTC)
        )

        self.assertEqual(outcome.attempt.status, AttemptStatus.REJECTED)
        self.assertIn("timeout", outcome.attempt.rejection_reason.lower())
        self.assertIsNotNone(outcome.cleanup)
        self.assertTrue(outcome.cleanup.field_zero_confirmed)

    def test_unlock_is_retained_raw_then_rejected(self) -> None:
        self.station.lockin_xy.locked = False

        outcome = self.station.run_attempt(
            condition(), attempt_index=0, now=lambda: datetime.now(UTC)
        )

        self.assertEqual(outcome.attempt.status, AttemptStatus.REJECTED)
        self.assertEqual(len(outcome.raw_readings), 2)
        self.assertFalse(outcome.raw_readings[-1].reading.locked)
        self.assertIsNone(outcome.accepted_result)

    def test_overload_is_retained_raw_then_rejected(self) -> None:
        self.station.lockin_xx.overload = True

        outcome = self.station.run_attempt(
            condition(), attempt_index=0, now=lambda: datetime.now(UTC)
        )

        self.assertEqual(outcome.attempt.status, AttemptStatus.REJECTED)
        self.assertEqual(len(outcome.raw_readings), 1)
        self.assertTrue(outcome.raw_readings[0].reading.overload)

    def test_gate_leakage_fails_closed_and_rejects_attempt(self) -> None:
        self.station.gate_top.injected_leakage_current_a = (
            self.station.gate_top.leakage_limit_a * 2
        )

        outcome = self.station.run_attempt(
            condition(), attempt_index=0, now=lambda: datetime.now(UTC)
        )

        self.assertEqual(outcome.attempt.status, AttemptStatus.REJECTED)
        top_state = self.station.gate_top.read_state()
        self.assertEqual(top_state.voltage_read_v, 0.0)
        self.assertFalse(top_state.output_enabled)

    def test_cleanup_order_is_electrical_then_magnet_then_disconnect(self) -> None:
        report = cleanup_after_failure(
            lockin_xx=self.station.lockin_xx,
            lockin_xy=self.station.lockin_xy,
            gate_top=self.station.gate_top,
            gate_bottom=self.station.gate_bottom,
            cryostat=self.station.cryostat,
            last_confirmed_cryostat_state=self.station.cryostat.read_state(),
        )

        actions = [event.action for event in report.events]
        expected_prefix = [
            CleanupAction.LOCKIN_XX_MINIMUM,
            CleanupAction.GATE_TOP_ZERO,
            CleanupAction.GATE_BOTTOM_ZERO,
            CleanupAction.GATE_TOP_DISABLE,
            CleanupAction.GATE_BOTTOM_DISABLE,
            CleanupAction.FIELD_ZERO_REQUEST,
            CleanupAction.FIELD_ZERO_VERIFY,
            CleanupAction.FINAL_STATE_RECORDED,
        ]
        self.assertEqual(actions[: len(expected_prefix)], expected_prefix)
        disconnect_index = actions.index(CleanupAction.DISCONNECT_LOCKIN_XX)
        self.assertGreater(disconnect_index, actions.index(CleanupAction.FINAL_STATE_RECORDED))

    def test_cleanup_does_not_infer_zero_after_readback_failure(self) -> None:
        self.station.cryostat.ensure_field_control(True)
        self.station.cryostat.set_vector_field(VectorField(1.0, 0.0))
        self.station.cryostat.wait_for_field(max_polls=2)
        last_confirmed = self.station.cryostat.read_state()
        self.station.cryostat.fail_next("read_state")

        report = cleanup_after_failure(
            lockin_xx=self.station.lockin_xx,
            lockin_xy=self.station.lockin_xy,
            gate_top=self.station.gate_top,
            gate_bottom=self.station.gate_bottom,
            cryostat=self.station.cryostat,
            last_confirmed_cryostat_state=last_confirmed,
        )

        self.assertFalse(report.field_zero_confirmed)
        self.assertEqual(report.last_confirmed_cryostat_state.field, VectorField(1.0, 0.0))
        verify = next(
            event
            for event in report.events
            if event.action is CleanupAction.FIELD_ZERO_VERIFY
        )
        self.assertFalse(verify.succeeded)

    def test_semantic_lockin_roles_are_fixed(self) -> None:
        self.assertEqual(self.station.lockin_xx.role, LockinRole.XX)
        self.assertEqual(self.station.lockin_xy.role, LockinRole.XY)
        self.assertTrue(self.station.lockin_xx.sine_output_connected)
        self.assertFalse(self.station.lockin_xy.sine_output_connected)

    def test_keyboard_interrupt_runs_cleanup_then_is_reraised(self) -> None:
        with patch.object(
            self.station.lockin_xx,
            "read_harmonic",
            side_effect=KeyboardInterrupt,
        ):
            with self.assertRaises(KeyboardInterrupt) as caught:
                self.station.run_attempt(
                    condition(), attempt_index=0, now=lambda: datetime.now(UTC)
                )

        report = caught.exception.cleanup_report
        self.assertTrue(report.field_zero_confirmed)
        self.assertEqual(
            report.events[0].action,
            CleanupAction.LOCKIN_XX_MINIMUM,
        )

    def test_normal_hold_cleanup_makes_electrical_outputs_safe_without_zeroing_field(self) -> None:
        self.station.cryostat.ensure_field_control(True)
        self.station.cryostat.set_vector_field(VectorField(0.2, 0.3))
        self.station.cryostat.wait_for_field(max_polls=2)
        self.station.gate_top.enable_output()
        self.station.gate_top.set_voltage(0.2)
        last = self.station.cryostat.read_state()

        report = cleanup_after_normal_completion(
            lockin_xx=self.station.lockin_xx,
            lockin_xy=self.station.lockin_xy,
            gate_top=self.station.gate_top,
            gate_bottom=self.station.gate_bottom,
            cryostat=self.station.cryostat,
            last_confirmed_cryostat_state=last,
            zero_field=False,
        )

        self.assertTrue(report.succeeded)
        self.assertFalse(report.field_zero_required)
        self.assertEqual(report.last_confirmed_cryostat_state.field, VectorField(0.2, 0.3))
        self.assertNotIn(
            CleanupAction.FIELD_ZERO_REQUEST,
            [event.action for event in report.events],
        )

    def test_normal_hold_read_failure_is_not_reported_as_success(self) -> None:
        last = self.station.cryostat.read_state()
        self.station.cryostat.fail_next("read_state")
        report = cleanup_after_normal_completion(
            lockin_xx=self.station.lockin_xx,
            lockin_xy=self.station.lockin_xy,
            gate_top=self.station.gate_top,
            gate_bottom=self.station.gate_bottom,
            cryostat=self.station.cryostat,
            last_confirmed_cryostat_state=last,
            zero_field=False,
        )
        self.assertFalse(report.succeeded)
        self.assertIs(report.last_confirmed_cryostat_state, last)


if __name__ == "__main__":
    unittest.main()
