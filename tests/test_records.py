from datetime import UTC, datetime, timedelta
import unittest

from attodry_control.models import (
    CryostatState,
    GateState,
    LockinReading,
    LockinRole,
    VectorField,
)
from attodry_control.records import (
    AcceptedTransportResult,
    AttemptRecord,
    AttemptStatus,
    ExperimentCondition,
    RawStationSample,
    RawTransportReading,
)


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def raw_reading(
    role: LockinRole,
    harmonic: int,
    *,
    locked: bool = True,
    overload: bool = False,
) -> RawTransportReading:
    return RawTransportReading(
        condition_id="condition-0001",
        attempt_index=0,
        captured_at_utc=NOW,
        reading=LockinReading(
            role=role,
            harmonic=harmonic,
            x_v=1e-6,
            y_v=2e-7,
            amplitude_v=1.0198e-6,
            phase_deg=11.31,
            frequency_hz=17.777,
            locked=locked,
            overload=overload,
        ),
    )


class RecordModelTests(unittest.TestCase):
    def test_condition_preserves_vector_and_gate_coordinates(self) -> None:
        condition = ExperimentCondition(
            condition_id="condition-0001",
            sequence_index=0,
            temperature_k=2.0,
            field=VectorField(1.8, 2.4),
            excitation_v=0.004,
            frequency_hz=17.777,
            gate_top_v=0.1,
            gate_bottom_v=-0.1,
        )

        self.assertAlmostEqual(condition.field.magnitude_t, 3.0)
        self.assertEqual(condition.gate_top_v, 0.1)

    def test_attempt_acceptance_contract_is_explicit(self) -> None:
        accepted = AttemptRecord(
            condition_id="condition-0001",
            attempt_index=0,
            started_at_utc=NOW,
            completed_at_utc=NOW + timedelta(seconds=1),
            status=AttemptStatus.ACCEPTED,
        )

        self.assertTrue(accepted.accepted)
        self.assertIsNone(accepted.rejection_reason)

    def test_rejected_attempt_requires_reason(self) -> None:
        with self.assertRaisesRegex(ValueError, "rejection_reason"):
            AttemptRecord(
                condition_id="condition-0001",
                attempt_index=0,
                started_at_utc=NOW,
                completed_at_utc=NOW,
                status=AttemptStatus.REJECTED,
            )

    def test_accepted_result_requires_matching_safe_raw_readings(self) -> None:
        readings = tuple(
            raw_reading(role, harmonic)
            for harmonic in (1, 2, 3)
            for role in (LockinRole.XX, LockinRole.XY)
        )

        result = AcceptedTransportResult(
            condition_id="condition-0001",
            attempt_index=0,
            readings=readings,
        )

        self.assertTrue(result.accepted)
        self.assertEqual(len(result.readings), 6)

    def test_accepted_result_rejects_unlocked_raw_reading(self) -> None:
        with self.assertRaisesRegex(ValueError, "unlocked"):
            AcceptedTransportResult(
                condition_id="condition-0001",
                attempt_index=0,
                readings=(raw_reading(LockinRole.XY, 1, locked=False),),
            )

    def test_raw_reading_rejects_naive_timestamp(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            RawTransportReading(
                condition_id="condition-0001",
                attempt_index=0,
                captured_at_utc=datetime(2026, 8, 20),
                reading=raw_reading(LockinRole.XX, 1).reading,
            )

    def test_station_sample_safety_requires_controls_outputs_and_leakage(self) -> None:
        field = VectorField(0.0, 0.0)
        cryostat = CryostatState(2.0, 2.0, 2.0, field, field, True, True, 0)
        bottom = GateState("bottom", -0.1, -0.1, 1e-10, 1e-8, True)
        sample = RawStationSample(
            "condition-0001",
            0,
            NOW,
            cryostat,
            GateState("top", 0.1, 0.1, 1e-10, 1e-8, True),
            bottom,
            5e-9,
            5e-9,
        )
        self.assertTrue(sample.safe_for_acceptance)
        unsafe = RawStationSample(
            "condition-0001",
            0,
            NOW,
            cryostat,
            GateState("top", 0.1, 0.1, 6e-9, 1e-8, True),
            bottom,
            5e-9,
            5e-9,
        )
        self.assertFalse(unsafe.safe_for_acceptance)


if __name__ == "__main__":
    unittest.main()
