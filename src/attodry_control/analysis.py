from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, fields
import math
from pathlib import Path
import sqlite3
from typing import Sequence

from .models import LockinRole
from .transport import signed_resistance_ohm


@dataclass(frozen=True, slots=True)
class AnalysisRow:
    condition_id: str
    scan_id: str
    sequence_index: int
    attempt_index: int
    accepted: bool
    temperature_k: float
    bx_t: float
    bz_t: float
    field_magnitude_t: float
    angle_deg_from_z: float
    excitation_v: float
    frequency_hz: float
    gate_top_v: float
    gate_bottom_v: float
    role: LockinRole
    harmonic: int
    x_v: float
    y_v: float
    amplitude_v: float
    phase_deg: float
    locked: bool
    overload: bool

    def signed_resistance_ohm(self, current_a_rms: float) -> float:
        return signed_resistance_ohm(
            voltage_v=self.x_v,
            current_a_rms=current_a_rms,
        )


@dataclass(frozen=True, slots=True)
class GateMapData:
    top_voltages_v: tuple[float, ...]
    bottom_voltages_v: tuple[float, ...]
    values: tuple[tuple[float, ...], ...]
    value_label: str


@dataclass(frozen=True, slots=True)
class GateLeakageRow:
    condition_id: str
    scan_id: str
    sequence_index: int
    attempt_index: int
    accepted: bool
    gate_top_v: float
    gate_bottom_v: float
    top_leakage_a: float | None
    bottom_leakage_a: float | None
    top_leakage_limit_a: float
    bottom_leakage_limit_a: float
    safe_for_acceptance: bool


def load_analysis_rows(
    database: str | Path,
    run_id: str,
    *,
    accepted_only: bool = True,
) -> tuple[AnalysisRow, ...]:
    """Load long-form transport data through an enforced read-only connection."""

    path = Path(database).resolve()
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        predicate = "AND tr.accepted = 1" if accepted_only else ""
        records = connection.execute(
            f"""
            SELECT c.condition_id, c.scan_id, c.sequence_index, tr.attempt_index,
                   tr.accepted, c.temperature_k, c.bx_t, c.bz_t,
                   c.excitation_v, c.frequency_hz AS condition_frequency_hz,
                   c.gate_top_v, c.gate_bottom_v, tr.role, tr.harmonic,
                   tr.x_v, tr.y_v, tr.amplitude_v, tr.phase_deg,
                   tr.frequency_hz, tr.locked, tr.overload
            FROM transport_readings AS tr
            JOIN conditions AS c
              ON c.run_id = tr.run_id AND c.condition_id = tr.condition_id
            WHERE tr.run_id = ? {predicate}
            ORDER BY c.sequence_index, tr.attempt_index, tr.harmonic,
                     CASE tr.role WHEN 'xx' THEN 0 ELSE 1 END
            """,
            (run_id,),
        ).fetchall()
    finally:
        connection.close()
    return tuple(_analysis_row(record) for record in records)


def load_gate_leakage_rows(
    database: str | Path,
    run_id: str,
    *,
    accepted_only: bool = True,
) -> tuple[GateLeakageRow, ...]:
    path = Path(database).resolve()
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        predicate = "AND ss.accepted = 1" if accepted_only else ""
        records = connection.execute(
            f"""
            SELECT c.condition_id, c.scan_id, c.sequence_index, ss.attempt_index,
                   ss.accepted, c.gate_top_v, c.gate_bottom_v,
                   ss.gate_top_leakage_a, ss.gate_bottom_leakage_a,
                   ss.gate_top_leakage_limit_a,
                   ss.gate_bottom_leakage_limit_a,
                   ss.safe_for_acceptance
            FROM station_samples AS ss
            JOIN conditions AS c
              ON c.run_id = ss.run_id AND c.condition_id = ss.condition_id
            WHERE ss.run_id = ? {predicate}
            ORDER BY c.sequence_index, ss.attempt_index
            """,
            (run_id,),
        ).fetchall()
    finally:
        connection.close()
    return tuple(
        GateLeakageRow(
            condition_id=row["condition_id"],
            scan_id=row["scan_id"],
            sequence_index=int(row["sequence_index"]),
            attempt_index=int(row["attempt_index"]),
            accepted=bool(row["accepted"]),
            gate_top_v=float(row["gate_top_v"]),
            gate_bottom_v=float(row["gate_bottom_v"]),
            top_leakage_a=(
                None
                if row["gate_top_leakage_a"] is None
                else float(row["gate_top_leakage_a"])
            ),
            bottom_leakage_a=(
                None
                if row["gate_bottom_leakage_a"] is None
                else float(row["gate_bottom_leakage_a"])
            ),
            top_leakage_limit_a=float(row["gate_top_leakage_limit_a"]),
            bottom_leakage_limit_a=float(row["gate_bottom_leakage_limit_a"]),
            safe_for_acceptance=bool(row["safe_for_acceptance"]),
        )
        for row in records
    )


def export_csv(
    rows: Sequence[AnalysisRow],
    destination: str | Path,
    *,
    current_a_rms: float | None = None,
) -> None:
    names = [field.name for field in fields(AnalysisRow)]
    if current_a_rms is not None:
        names.append("signed_resistance_ohm")
    with Path(destination).open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=names)
        writer.writeheader()
        for row in rows:
            record = {
                name: (
                    getattr(row, name).value
                    if isinstance(getattr(row, name), LockinRole)
                    else getattr(row, name)
                )
                for name in (field.name for field in fields(AnalysisRow))
            }
            if current_a_rms is not None:
                record["signed_resistance_ohm"] = row.signed_resistance_ohm(
                    current_a_rms
                )
            writer.writerow(record)


def plot_transport_trace(
    rows: Sequence[AnalysisRow],
    destination: str | Path,
    *,
    x_axis: str = "field_magnitude_t",
    role: LockinRole = LockinRole.XX,
    harmonic: int = 1,
    current_a_rms: float | None = None,
) -> None:
    if x_axis not in {
        "temperature_k",
        "bx_t",
        "bz_t",
        "field_magnitude_t",
        "angle_deg_from_z",
        "gate_top_v",
        "gate_bottom_v",
    }:
        raise ValueError("Unsupported x_axis.")
    selected = [
        row for row in rows if row.role is role and row.harmonic == harmonic
    ]
    if not selected:
        raise ValueError("No rows match the requested role and harmonic.")
    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError(
            "Plotting requires the optional analysis dependency: "
            "python -m pip install -e '.[analysis]'"
        ) from exc
    x_values = [getattr(row, x_axis) for row in selected]
    if current_a_rms is None:
        y_values = [row.x_v for row in selected]
        y_label = "X voltage (V)"
    else:
        y_values = [row.signed_resistance_ohm(current_a_rms) for row in selected]
        y_label = "Signed resistance (ohm)"
    figure, axis = plt.subplots(figsize=(6.4, 4.2), constrained_layout=True)
    axis.plot(x_values, y_values, marker="o", linewidth=1.2)
    axis.set_xlabel(x_axis.replace("_", " "))
    axis.set_ylabel(y_label)
    axis.set_title(f"{role.value.upper()} harmonic {harmonic}")
    axis.grid(True, alpha=0.25)
    figure.savefig(destination, dpi=200)
    plt.close(figure)


def build_gate_map(
    rows: Sequence[AnalysisRow],
    *,
    role: LockinRole = LockinRole.XX,
    harmonic: int = 1,
    current_a_rms: float | None = None,
) -> GateMapData:
    selected = [
        row for row in rows if row.role is role and row.harmonic == harmonic
    ]
    if not selected:
        raise ValueError("No rows match the requested role and harmonic.")
    top_values = tuple(sorted({row.gate_top_v for row in selected}))
    bottom_values = tuple(sorted({row.gate_bottom_v for row in selected}))
    by_point: dict[tuple[float, float], float] = {}
    for row in selected:
        key = (row.gate_top_v, row.gate_bottom_v)
        if key in by_point:
            raise ValueError(f"Duplicate accepted gate-map point {key}.")
        by_point[key] = (
            row.x_v
            if current_a_rms is None
            else row.signed_resistance_ohm(current_a_rms)
        )
    expected = len(top_values) * len(bottom_values)
    if len(by_point) != expected:
        raise ValueError(
            "Gate map is not a complete rectangular top/bottom voltage grid."
        )
    return GateMapData(
        top_voltages_v=top_values,
        bottom_voltages_v=bottom_values,
        values=tuple(
            tuple(by_point[(top, bottom)] for bottom in bottom_values)
            for top in top_values
        ),
        value_label=("X voltage (V)" if current_a_rms is None else "Signed resistance (ohm)"),
    )


def plot_gate_map(
    rows: Sequence[AnalysisRow],
    destination: str | Path,
    *,
    role: LockinRole = LockinRole.XX,
    harmonic: int = 1,
    current_a_rms: float | None = None,
) -> None:
    gate_map = build_gate_map(
        rows,
        role=role,
        harmonic=harmonic,
        current_a_rms=current_a_rms,
    )
    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError(
            "Plotting requires the optional analysis dependency: "
            "python -m pip install -e '.[analysis]'"
        ) from exc
    figure, axis = plt.subplots(figsize=(6.0, 4.8), constrained_layout=True)
    image = axis.pcolormesh(
        gate_map.bottom_voltages_v,
        gate_map.top_voltages_v,
        gate_map.values,
        shading="nearest",
    )
    axis.set_xlabel("Bottom gate (V)")
    axis.set_ylabel("Top gate (V)")
    axis.set_title(f"{role.value.upper()} harmonic {harmonic}")
    figure.colorbar(image, ax=axis, label=gate_map.value_label)
    figure.savefig(destination, dpi=200)
    plt.close(figure)


def _analysis_row(record: sqlite3.Row) -> AnalysisRow:
    bx_t = float(record["bx_t"])
    bz_t = float(record["bz_t"])
    return AnalysisRow(
        condition_id=record["condition_id"],
        scan_id=record["scan_id"],
        sequence_index=int(record["sequence_index"]),
        attempt_index=int(record["attempt_index"]),
        accepted=bool(record["accepted"]),
        temperature_k=float(record["temperature_k"]),
        bx_t=bx_t,
        bz_t=bz_t,
        field_magnitude_t=math.hypot(bx_t, bz_t),
        angle_deg_from_z=math.degrees(math.atan2(bx_t, bz_t)),
        excitation_v=float(record["excitation_v"]),
        frequency_hz=float(record["frequency_hz"]),
        gate_top_v=float(record["gate_top_v"]),
        gate_bottom_v=float(record["gate_bottom_v"]),
        role=LockinRole(record["role"]),
        harmonic=int(record["harmonic"]),
        x_v=float(record["x_v"]),
        y_v=float(record["y_v"]),
        amplitude_v=float(record["amplitude_v"]),
        phase_deg=float(record["phase_deg"]),
        locked=bool(record["locked"]),
        overload=bool(record["overload"]),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only accepted-attempt transport export and plotting."
    )
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--csv", type=Path)
    parser.add_argument("--plot", type=Path)
    parser.add_argument("--gate-map", type=Path)
    parser.add_argument("--publication-dir", type=Path)
    parser.add_argument("--total-series-resistance-ohm", type=float)
    parser.add_argument("--gate-calibration", type=Path)
    parser.add_argument(
        "--format",
        dest="formats",
        action="append",
        choices=("png", "pdf"),
        help="Publication output format; repeat for both. Default: png and pdf.",
    )
    parser.add_argument(
        "--x-axis",
        default="field_magnitude_t",
        choices=(
            "temperature_k",
            "bx_t",
            "bz_t",
            "field_magnitude_t",
            "angle_deg_from_z",
            "gate_top_v",
            "gate_bottom_v",
        ),
    )
    parser.add_argument("--role", choices=("xx", "xy"), default="xx")
    parser.add_argument("--harmonic", type=int, choices=(1, 2, 3), default=1)
    parser.add_argument("--current-a-rms", type=float)
    parser.add_argument(
        "--include-rejected",
        action="store_true",
        help="Explicit audit mode; default analysis contains accepted attempts only.",
    )
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rows = load_analysis_rows(
        args.database,
        args.run_id,
        accepted_only=not args.include_rejected,
    )
    if (
        args.csv is None
        and args.plot is None
        and args.gate_map is None
        and args.publication_dir is None
    ):
        raise SystemExit(
            "At least one of --csv, --plot, --gate-map, or --publication-dir is required."
        )
    if args.csv is not None:
        export_csv(rows, args.csv, current_a_rms=args.current_a_rms)
    if args.plot is not None:
        plot_transport_trace(
            rows,
            args.plot,
            x_axis=args.x_axis,
            role=LockinRole(args.role),
            harmonic=args.harmonic,
            current_a_rms=args.current_a_rms,
        )
    if args.gate_map is not None:
        plot_gate_map(
            rows,
            args.gate_map,
            role=LockinRole(args.role),
            harmonic=args.harmonic,
            current_a_rms=args.current_a_rms,
        )
    if args.publication_dir is not None:
        from .publication import (
            generate_publication_plots,
            load_gate_calibration,
        )

        calibration = (
            None
            if args.gate_calibration is None
            else load_gate_calibration(args.gate_calibration)
        )
        leakage = load_gate_leakage_rows(
            args.database,
            args.run_id,
            accepted_only=not args.include_rejected,
        )
        result = generate_publication_plots(
            rows,
            leakage,
            args.publication_dir,
            total_series_resistance_ohm=args.total_series_resistance_ohm,
            gate_calibration=calibration,
            formats=tuple(args.formats or ("png", "pdf")),
        )
        print(result["manifest"])
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    raise SystemExit(run(argv))


if __name__ == "__main__":
    main()
