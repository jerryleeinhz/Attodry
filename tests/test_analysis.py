from dataclasses import replace
from datetime import UTC, datetime
import csv
import math
from pathlib import Path
import tempfile
import unittest

from attodry_control.analysis import (
    build_gate_map,
    export_csv,
    load_analysis_rows,
    load_gate_leakage_rows,
)
from attodry_control.models import (
    CryostatState,
    GateState,
    LockinReading,
    LockinRole,
    VectorField,
)
from attodry_control.records import (
    AttemptRecord,
    AttemptStatus,
    ExperimentCondition,
    RawStationSample,
    RawTransportReading,
)
from attodry_control.storage import RunStore


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


class AnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.NamedTemporaryFile(
            dir=PROJECT_ROOT, suffix=".sqlite", delete=False
        )
        temporary.close()
        self.path = Path(temporary.name)
        self.csv_path = self.path.with_suffix(".csv")
        self.addCleanup(self._cleanup_files)
        with RunStore(self.path) as store:
            store.create_run("analysis-run", {}, created_at_utc=NOW)
            store.register_condition(
                "analysis-run",
                ExperimentCondition(
                    condition_id="c1",
                    sequence_index=0,
                    temperature_k=2.0,
                    field=VectorField(1.0, 1.0),
                    excitation_v=0.004,
                    frequency_hz=17.777,
                    gate_top_v=0.2,
                    gate_bottom_v=-0.3,
                ),
            )
            rejected = store.start_attempt(
                "analysis-run", "c1", started_at_utc=NOW
            )
            store.append_raw_reading(
                "analysis-run",
                self._raw(LockinRole.XX, 1, rejected, locked=False),
            )
            store.complete_attempt(
                "analysis-run",
                AttemptRecord(
                    "c1",
                    rejected,
                    NOW,
                    NOW,
                    AttemptStatus.REJECTED,
                    "unlocked",
                ),
            )
            accepted = store.start_attempt(
                "analysis-run", "c1", started_at_utc=NOW
            )
            field = VectorField(1.0, 1.0)
            store.append_station_sample(
                "analysis-run",
                RawStationSample(
                    "c1",
                    accepted,
                    NOW,
                    CryostatState(
                        2.0, 2.0, 2.0, field, field, True, True, 0
                    ),
                    GateState("top", 0.2, 0.2, 1e-10, 1e-8, True),
                    GateState("bottom", -0.3, -0.3, 2e-10, 1e-8, True),
                    5e-9,
                    5e-9,
                ),
            )
            for harmonic in (1, 2, 3):
                for role in (LockinRole.XX, LockinRole.XY):
                    store.append_raw_reading(
                        "analysis-run", self._raw(role, harmonic, accepted)
                    )
            store.complete_attempt(
                "analysis-run",
                AttemptRecord(
                    "c1", accepted, NOW, NOW, AttemptStatus.ACCEPTED
                ),
            )

    def _cleanup_files(self) -> None:
        for path in (
            self.path,
            Path(str(self.path) + "-shm"),
            Path(str(self.path) + "-wal"),
            self.csv_path,
        ):
            if path.exists():
                path.unlink()

    @staticmethod
    def _raw(
        role: LockinRole,
        harmonic: int,
        attempt_index: int,
        *,
        locked: bool = True,
    ) -> RawTransportReading:
        x_v = (-2e-6 if role is LockinRole.XX else 1e-7) / harmonic
        return RawTransportReading(
            condition_id="c1",
            attempt_index=attempt_index,
            captured_at_utc=NOW,
            reading=LockinReading(
                role=role,
                harmonic=harmonic,
                x_v=x_v,
                y_v=0.0,
                amplitude_v=abs(x_v),
                phase_deg=180.0 if x_v < 0 else 0.0,
                frequency_hz=17.777,
                locked=locked,
                overload=False,
            ),
        )

    def test_default_loader_excludes_rejected_and_adds_vector_metadata(self) -> None:
        rows = load_analysis_rows(self.path, "analysis-run")
        self.assertEqual(len(rows), 6)
        self.assertTrue(all(row.accepted for row in rows))
        self.assertAlmostEqual(rows[0].field_magnitude_t, math.sqrt(2))
        self.assertAlmostEqual(rows[0].angle_deg_from_z, 45.0)

    def test_rejected_rows_require_explicit_audit_mode(self) -> None:
        rows = load_analysis_rows(
            self.path, "analysis-run", accepted_only=False
        )
        self.assertEqual(len(rows), 7)
        self.assertEqual(sum(not row.accepted for row in rows), 1)

    def test_gate_leakage_loader_is_read_only_and_accepted_only_by_default(self) -> None:
        accepted = load_gate_leakage_rows(self.path, "analysis-run")
        audit = load_gate_leakage_rows(
            self.path, "analysis-run", accepted_only=False
        )
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(audit), 1)
        self.assertEqual(accepted[0].top_leakage_a, 1e-10)
        self.assertEqual(accepted[0].bottom_leakage_a, 2e-10)
        self.assertTrue(accepted[0].safe_for_acceptance)

    def test_csv_resistance_requires_and_uses_explicit_current(self) -> None:
        rows = load_analysis_rows(self.path, "analysis-run")
        export_csv(rows, self.csv_path, current_a_rms=1e-9)
        with self.csv_path.open(newline="", encoding="utf-8") as file:
            records = list(csv.DictReader(file))
        xx_h1 = next(
            row for row in records if row["role"] == "xx" and row["harmonic"] == "1"
        )
        self.assertAlmostEqual(float(xx_h1["signed_resistance_ohm"]), -2000.0)

    def test_gate_map_reorders_serpentine_points_into_rectangular_matrix(self) -> None:
        base = next(
            row
            for row in load_analysis_rows(self.path, "analysis-run")
            if row.role is LockinRole.XX and row.harmonic == 1
        )
        rows = (
            replace(base, gate_top_v=-1.0, gate_bottom_v=-2.0, x_v=1.0),
            replace(base, gate_top_v=-1.0, gate_bottom_v=2.0, x_v=2.0),
            replace(base, gate_top_v=1.0, gate_bottom_v=2.0, x_v=4.0),
            replace(base, gate_top_v=1.0, gate_bottom_v=-2.0, x_v=3.0),
        )
        gate_map = build_gate_map(rows)
        self.assertEqual(gate_map.top_voltages_v, (-1.0, 1.0))
        self.assertEqual(gate_map.bottom_voltages_v, (-2.0, 2.0))
        self.assertEqual(gate_map.values, ((1.0, 2.0), (3.0, 4.0)))


if __name__ == "__main__":
    unittest.main()
