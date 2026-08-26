from __future__ import annotations

import csv
from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Mapping, Sequence

from .three_smu_config import SEMANTIC_ROLES


@dataclass(frozen=True, slots=True)
class ThreeSmuAnalysisRow:
    run_dir: Path
    accepted: bool
    clean: bool
    point_index: int
    repeat_index: int
    segment: str
    elapsed_s: float
    role: str
    coordinate: float
    timestamp: str
    source_setpoint: float
    voltage_v: float
    current_a: float
    resistance_ohm: float | None
    output_enabled: bool
    compliance_trip: bool
    status: str
    problems: str


@dataclass(frozen=True, slots=True)
class MapData:
    x_values: tuple[float, ...]
    y_values: tuple[float, ...]
    values: tuple[tuple[float, ...], ...]


@dataclass(frozen=True, slots=True)
class ThreeSmuRunSummary:
    run_dir: Path
    started_at: str | None
    status: str
    accepted: bool
    run_name: str


def resolve_run_dir(path: str | Path) -> Path:
    candidate = Path(path).expanduser().resolve()
    if candidate.is_file():
        candidate = candidate.parent
    if not (candidate / "metadata.json").is_file():
        raise ValueError(f"{candidate} does not contain metadata.json")
    if not (candidate / "data.csv").is_file():
        raise ValueError(f"{candidate} does not contain data.csv")
    return candidate


def discover_three_smu_runs(
    data_directory: str | Path,
    *,
    include_rejected: bool = False,
) -> tuple[ThreeSmuRunSummary, ...]:
    """List complete local or SSH-mounted runs; silently skip incomplete folders."""

    root = Path(data_directory).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"{root} is not a data directory")
    summaries: list[ThreeSmuRunSummary] = []
    for candidate in root.iterdir():
        if not candidate.is_dir():
            continue
        metadata_path = candidate / "metadata.json"
        data_path = candidate / "data.csv"
        if not metadata_path.is_file() or not data_path.is_file():
            continue
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        accepted = (
            metadata.get("status") == "completed" and metadata.get("accepted") is True
        )
        if not accepted and not include_rejected:
            continue
        summaries.append(
            ThreeSmuRunSummary(
                run_dir=candidate,
                started_at=metadata.get("started_at"),
                status=str(metadata.get("status", "unknown")),
                accepted=accepted,
                run_name=str(metadata.get("run_name", "")),
            )
        )
    return tuple(sorted(summaries, key=lambda item: item.started_at or "", reverse=True))


def load_three_smu_rows(
    path: str | Path,
    *,
    include_rejected: bool = False,
    include_problem: bool = False,
    segment: str | None = None,
    role: str | None = None,
) -> tuple[ThreeSmuAnalysisRow, ...]:
    """Load completed/accepted/clean formal samples unless audit flags opt in."""

    run_dir = resolve_run_dir(path)
    metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
    run_accepted = metadata.get("status") == "completed" and metadata.get("accepted") is True
    if not run_accepted and not include_rejected:
        return ()
    if role is not None and role not in SEMANTIC_ROLES:
        raise ValueError(f"role must be one of: {', '.join(SEMANTIC_ROLES)}")
    archived_active_roles = metadata.get("active_roles")
    if role is not None:
        selected_roles = (role,)
    elif isinstance(archived_active_roles, list) and all(
        item in SEMANTIC_ROLES for item in archived_active_roles
    ):
        selected_roles = tuple(archived_active_roles)
    else:
        selected_roles = SEMANTIC_ROLES
    rows: list[ThreeSmuAnalysisRow] = []
    with (run_dir / "data.csv").open(newline="", encoding="utf-8") as file:
        for record in csv.DictReader(file):
            clean = _bool(record["sample_clean"])
            if not clean and not include_problem:
                continue
            if segment is not None and record["segment"] != segment:
                continue
            for selected_role in selected_roles:
                if not record[f"{selected_role}_timestamp"].strip():
                    continue
                resistance = record[f"{selected_role}_resistance_ohm"].strip()
                rows.append(
                    ThreeSmuAnalysisRow(
                        run_dir=run_dir,
                        accepted=run_accepted and clean,
                        clean=clean,
                        point_index=int(record["point_index"]),
                        repeat_index=int(record["repeat_index"]),
                        segment=record["segment"],
                        elapsed_s=float(record["elapsed_s"]),
                        role=selected_role,
                        coordinate=float(record[f"{selected_role}_coordinate"]),
                        timestamp=record[f"{selected_role}_timestamp"],
                        source_setpoint=float(record[f"{selected_role}_source_setpoint"]),
                        voltage_v=float(record[f"{selected_role}_voltage_v"]),
                        current_a=float(record[f"{selected_role}_current_a"]),
                        resistance_ohm=None if resistance == "" else float(resistance),
                        output_enabled=_bool(record[f"{selected_role}_output_enabled"]),
                        compliance_trip=_bool(record[f"{selected_role}_compliance_trip"]),
                        status=record[f"{selected_role}_status"],
                        problems=record["problems"],
                    )
                )
    return tuple(rows)


def plot_bias_iv(rows: Sequence[ThreeSmuAnalysisRow]):
    selected = _role_rows(rows, "smu_bias")
    return _line_plot(
        [row.source_setpoint for row in selected],
        [row.current_a for row in selected],
        "Bias source setpoint",
        "Bias current (A)",
        "Bias I-V",
    )


def plot_gate_transfer(
    rows: Sequence[ThreeSmuAnalysisRow],
    *,
    gate_role: str = "gate_top",
):
    if gate_role not in {"gate_top", "gate_bottom"}:
        raise ValueError("gate_role must be gate_top or gate_bottom")
    gates = _role_rows(rows, gate_role)
    bias_by_point = {
        (row.point_index, row.repeat_index): row
        for row in _role_rows(rows, "smu_bias")
    }
    current = [
        bias_by_point[(row.point_index, row.repeat_index)].current_a for row in gates
    ]
    return _line_plot(
        [row.coordinate for row in gates],
        current,
        f"{gate_role} setpoint",
        "Bias current (A)",
        "Gate transfer",
    )


def plot_time_trace(
    rows: Sequence[ThreeSmuAnalysisRow],
    *,
    role: str = "smu_bias",
    field: str = "current_a",
):
    selected = _role_rows(rows, role)
    return _line_plot(
        [row.elapsed_s for row in selected],
        [_numeric_field(row, field) for row in selected],
        "Elapsed time (s)",
        field,
        f"{role} time trace",
    )


def plot_gate_leakage(rows: Sequence[ThreeSmuAnalysisRow]):
    selected = [
        row for row in rows if row.role in {"gate_top", "gate_bottom"}
    ]
    if not selected:
        raise ValueError("No gate rows available")
    plt = _pyplot()
    figure, axis = plt.subplots(figsize=(6.4, 4.2), constrained_layout=True)
    for role in ("gate_top", "gate_bottom"):
        role_rows = [row for row in selected if row.role == role]
        if role_rows:
            axis.plot(
                [row.elapsed_s for row in role_rows],
                [abs(row.current_a) for row in role_rows],
                marker="o",
                label=role,
            )
    axis.set_xlabel("Elapsed time (s)")
    axis.set_ylabel("|Gate leakage| (A)")
    axis.set_yscale("log")
    axis.grid(True, alpha=0.25)
    axis.legend()
    return figure


def build_map(
    rows: Sequence[ThreeSmuAnalysisRow],
    *,
    x_role: str,
    y_role: str,
    value_role: str = "smu_bias",
    value_field: str = "current_a",
    fixed_coordinates: Mapping[str, float] | None = None,
) -> MapData:
    if x_role == y_role:
        raise ValueError("x_role and y_role must be distinct")
    fixed_coordinates = dict(fixed_coordinates or {})
    for role, value in fixed_coordinates.items():
        if role not in SEMANTIC_ROLES:
            raise ValueError(f"fixed coordinate role must be one of: {', '.join(SEMANTIC_ROLES)}")
        if role in {x_role, y_role}:
            raise ValueError("fixed coordinate roles cannot be a plotted axis")
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError("fixed coordinate values must be finite")
    by_key = {
        (row.point_index, row.repeat_index, row.role): row for row in rows
    }
    point_keys = sorted(
        {(row.point_index, row.repeat_index) for row in rows if row.role == value_role}
    )
    if not point_keys:
        raise ValueError(f"No rows for value_role {value_role}")
    points: dict[tuple[float, float], float] = {}
    for point_key in point_keys:
        try:
            x_row = by_key[(*point_key, x_role)]
            y_row = by_key[(*point_key, y_role)]
            value_row = by_key[(*point_key, value_role)]
        except KeyError as exc:
            raise ValueError("Map roles are not all present at every point") from exc
        if any(
            not math.isclose(
                by_key[(*point_key, role)].coordinate,
                float(value),
                rel_tol=0.0,
                abs_tol=1e-12,
            )
            for role, value in fixed_coordinates.items()
        ):
            continue
        coordinate = (x_row.coordinate, y_row.coordinate)
        if coordinate in points:
            raise ValueError(f"Duplicate map coordinate {coordinate}")
        points[coordinate] = _numeric_field(value_row, value_field)
    x_values = tuple(sorted({point[0] for point in points}))
    y_values = tuple(sorted({point[1] for point in points}))
    if not points:
        raise ValueError("No map points remain after applying fixed_coordinates")
    if len(points) != len(x_values) * len(y_values):
        raise ValueError("Map is not a complete rectangular grid")
    return MapData(
        x_values=x_values,
        y_values=y_values,
        values=tuple(
            tuple(points[(x_value, y_value)] for x_value in x_values)
            for y_value in y_values
        ),
    )


def plot_map(map_data: MapData, *, value_label: str = "Bias current (A)"):
    plt = _pyplot()
    figure, axis = plt.subplots(figsize=(6.0, 4.8), constrained_layout=True)
    image = axis.pcolormesh(
        map_data.x_values,
        map_data.y_values,
        map_data.values,
        shading="nearest",
    )
    axis.set_xlabel("X source setpoint")
    axis.set_ylabel("Y source setpoint")
    figure.colorbar(image, ax=axis, label=value_label)
    return figure


def _role_rows(
    rows: Sequence[ThreeSmuAnalysisRow],
    role: str,
) -> list[ThreeSmuAnalysisRow]:
    selected = [row for row in rows if row.role == role]
    if not selected:
        raise ValueError(f"No rows for role {role}")
    return selected


def _numeric_field(row: ThreeSmuAnalysisRow, field: str) -> float:
    if field not in {"source_setpoint", "voltage_v", "current_a", "resistance_ohm"}:
        raise ValueError("Unsupported numeric field")
    value = getattr(row, field)
    if value is None:
        raise ValueError(f"{field} contains an undefined value")
    return float(value)


def _line_plot(
    x_values: list[float],
    y_values: list[float],
    x_label: str,
    y_label: str,
    title: str,
):
    if not x_values:
        raise ValueError("No rows available")
    plt = _pyplot()
    figure, axis = plt.subplots(figsize=(6.4, 4.2), constrained_layout=True)
    axis.plot(x_values, y_values, marker="o", linewidth=1.2)
    axis.set_xlabel(x_label)
    axis.set_ylabel(y_label)
    axis.set_title(title)
    axis.grid(True, alpha=0.25)
    return figure


def _pyplot():
    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError(
            "Plotting requires: python -m pip install -e '.[analysis]'"
        ) from exc
    return plt


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}
