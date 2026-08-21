from dataclasses import replace
from datetime import UTC, datetime
import importlib.util
import json
import math
from pathlib import Path
import tempfile
import unittest

from attodry_control.analysis import AnalysisRow, GateLeakageRow
from attodry_control.models import LockinRole
from attodry_control.publication import (
    GateCalibration,
    PublicationDataError,
    _group_sweeps,
    generate_publication_plots,
    load_gate_calibration,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HAS_MATPLOTLIB = importlib.util.find_spec("matplotlib") is not None


def row(
    condition_id: str,
    sequence_index: int,
    *,
    temperature_k: float = 2.0,
    bx_t: float = 0.0,
    bz_t: float = 0.0,
    excitation_v: float = 0.004,
    frequency_hz: float = 17.777,
    top_v: float = 0.0,
    bottom_v: float = 0.0,
    harmonic: int = 1,
    role: LockinRole = LockinRole.XX,
    x_v: float = 1e-6,
    accepted: bool = True,
) -> AnalysisRow:
    magnitude = math.hypot(bx_t, bz_t)
    return AnalysisRow(
        condition_id=condition_id,
        scan_id=condition_id.split("-")[0],
        sequence_index=sequence_index,
        attempt_index=0,
        accepted=accepted,
        captured_at_utc=datetime(2026, 8, 20, tzinfo=UTC),
        temperature_k=temperature_k,
        bx_t=bx_t,
        bz_t=bz_t,
        field_magnitude_t=magnitude,
        angle_deg_from_z=math.degrees(math.atan2(bx_t, bz_t)),
        excitation_v=excitation_v,
        frequency_hz=frequency_hz,
        gate_top_v=top_v,
        gate_bottom_v=bottom_v,
        role=role,
        harmonic=harmonic,
        x_v=x_v,
        y_v=x_v * 0.1,
        amplitude_v=abs(x_v) * math.sqrt(1.01),
        phase_deg=5.0,
        phase_shift_deg=0.0,
        locked=True,
        overload=False,
    )


def publication_dataset() -> tuple[tuple[AnalysisRow, ...], tuple[GateLeakageRow, ...]]:
    rows: list[AnalysisRow] = []
    leakage: list[GateLeakageRow] = []
    sequence = 0

    for excitation in (0.004, 0.008, 0.012):
        sequence += 1
        for harmonic in (1, 2, 3):
            rows.append(
                row(
                    f"current-{sequence}",
                    sequence,
                    excitation_v=excitation,
                    harmonic=harmonic,
                    x_v=(excitation / 0.004) ** harmonic * 1e-7,
                )
            )

    for frequency in (10.0, 20.0, 40.0):
        sequence += 1
        rows.append(
            row(
                f"frequency-{sequence}",
                sequence,
                temperature_k=3.0,
                frequency_hz=frequency,
                x_v=frequency * 1e-9,
            )
        )

    for temperature in (4.0, 5.0, 6.0):
        sequence += 1
        rows.append(
            row(
                f"temperature-{sequence}",
                sequence,
                temperature_k=temperature,
                bz_t=0.05,
                x_v=temperature * 1e-7,
            )
        )

    for field in (0.1, 0.2, 0.3):
        sequence += 1
        for harmonic in (1, 2):
            rows.append(
                row(
                    f"field-{sequence}",
                    sequence,
                    temperature_k=7.0,
                    bz_t=field,
                    harmonic=harmonic,
                    x_v=field * harmonic * 1e-6,
                )
            )

    for angle in (0.0, 90.0, 180.0):
        sequence += 1
        radians = math.radians(angle)
        rows.append(
            row(
                f"angle-{sequence}",
                sequence,
                temperature_k=8.0,
                bx_t=0.5 * math.sin(radians),
                bz_t=0.5 * math.cos(radians),
                x_v=(angle + 1.0) * 1e-9,
            )
        )

    for temperature in (9.0, 10.0):
        for field in (0.4, 0.8):
            sequence += 1
            rows.append(
                row(
                    f"tf-{sequence}",
                    sequence,
                    temperature_k=temperature,
                    bz_t=field,
                    harmonic=2,
                    x_v=temperature * field * 1e-8,
                )
            )

    for top in (-1.0, 1.0):
        for bottom in (-2.0, 2.0):
            sequence += 1
            condition_id = f"gate-{sequence}"
            rows.append(
                row(
                    condition_id,
                    sequence,
                    temperature_k=11.0,
                    top_v=top,
                    bottom_v=bottom,
                    x_v=(1000.0 + 10.0 * top + bottom) * 4e-9,
                )
            )
            leakage.append(
                GateLeakageRow(
                    condition_id,
                    "gate",
                    sequence,
                    0,
                    True,
                    top,
                    bottom,
                    (2.0 + abs(top)) * 1e-10,
                    (2.0 + abs(bottom)) * 1e-10,
                    5e-9,
                    5e-9,
                    True,
                )
            )

    rows.append(replace(rows[0], condition_id="rejected", accepted=False))
    return tuple(rows), tuple(leakage)


class GateCalibrationTests(unittest.TestCase):
    def test_legacy_rows_are_not_inferred_to_share_a_scan(self) -> None:
        first = replace(row("legacy-a", 1), scan_id="legacy")
        second = replace(row("legacy-b", 2, excitation_v=0.008), scan_id="legacy")

        groups = _group_sweeps((first, second), "excitation")

        self.assertEqual(len(groups), 2)

    def test_calibration_converts_only_explicit_parameters(self) -> None:
        calibration = GateCalibration(0.01, 0.02, top_offset_v=0.1)
        density, displacement = calibration.convert(1.1, 1.0)
        self.assertGreater(density, 0.0)
        self.assertAlmostEqual(displacement, 0.005)

    def test_loader_rejects_unknown_fields(self) -> None:
        with tempfile.NamedTemporaryFile(
            dir=PROJECT_ROOT, suffix=".toml", mode="w", delete=False
        ) as file:
            path = Path(file.name)
            file.write(
                "[gate_calibration]\n"
                "top_capacitance_f_per_m2 = 0.01\n"
                "bottom_capacitance_f_per_m2 = 0.02\n"
                "assumed_dielectric = 3.0\n"
            )
        self.addCleanup(path.unlink)
        with self.assertRaisesRegex(PublicationDataError, "unknown"):
            load_gate_calibration(path)


@unittest.skipUnless(HAS_MATPLOTLIB, "matplotlib analysis extra is not installed")
class PublicationPlotTests(unittest.TestCase):
    def test_generates_supported_figures_and_auditable_skips(self) -> None:
        rows, leakage = publication_dataset()
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as directory:
            result = generate_publication_plots(
                rows,
                leakage,
                directory,
                total_series_resistance_ohm=4e6,
                gate_calibration=GateCalibration(0.01, 0.02),
                formats=("png", "pdf"),
            )
            manifest = json.loads(
                Path(result["manifest"]).read_text(encoding="utf-8")
            )
            statuses = {
                item["key"]: item["status"] for item in manifest["figures"]
            }
            for key in (
                "current_response",
                "harmonic_scaling",
                "frequency_response",
                "temperature_dependence",
                "field_dependence",
                "angle_dependence",
                "magnetochiral_gamma",
                "temperature_field_v2_over_b_map",
                "gate_resistance_map",
                "gate_leakage",
                "n_d_resistance_map",
            ):
                self.assertEqual(statuses[key], "generated", key)
            self.assertEqual(statuses["nernst_temperature_field_map"], "skipped")
            self.assertEqual(manifest["excluded_nonaccepted_transport_rows"], 1)
            self.assertTrue(Path(manifest["analysis_records_csv"]).is_file())
            self.assertTrue(Path(manifest["fit_summary_csv"]).is_file())
            current_outputs = next(
                item["outputs"]
                for item in manifest["figures"]
                if item["key"] == "current_response"
            )
            self.assertEqual({Path(path).suffix for path in current_outputs}, {".png", ".pdf"})


if __name__ == "__main__":
    unittest.main()
