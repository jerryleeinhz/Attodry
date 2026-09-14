"""Read-only, mode-independent data preparation and plotting for Three-SMU runs.

Both the live Notebook stream and archived ``data.csv`` records become the same
sample-wide objects here.  This keeps plotting independent of instruments and
lets a user choose axes across semantic roles, for example bias I versus a gate
coordinate.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
import json
import math
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .three_smu_analysis import resolve_run_dir
from .three_smu_config import SEMANTIC_ROLES
from .three_smu_stream import _sample_payload
from .three_smu import ThreeSmuSample


@dataclass(frozen=True, slots=True)
class ThreeSmuPlotReading:
    coordinate: float | None
    source_setpoint: float | None
    voltage_v: float | None
    current_a: float | None
    resistance_ohm: float | None
    output_enabled: bool | None
    compliance_trip: bool | None
    status: str | None

    @property
    def conductance_s(self) -> float | None:
        if self.resistance_ohm is None or not math.isfinite(self.resistance_ohm):
            return None
        if self.resistance_ohm == 0:
            return None
        return 1.0 / self.resistance_ohm


@dataclass(frozen=True, slots=True)
class ThreeSmuPlotSample:
    point_index: int
    repeat_index: int
    segment: str
    elapsed_s: float
    readings: Mapping[str, ThreeSmuPlotReading]
    clean: bool
    problems: str = ""


_GLOBAL_AXES = {
    "point_index": "Point index",
    "repeat_index": "Repeat",
    "elapsed_s": "Elapsed time (s)",
}
_ROLE_FIELDS = (
    ("coordinate", "requested coordinate"),
    ("source_setpoint", "source setpoint readback"),
    ("voltage_v", "voltage U (V)"),
    ("current_a", "current I (A)"),
    ("resistance_ohm", "resistance R (Ω)"),
    ("conductance_s", "conductance G (S)"),
)


def live_payload_to_plot_sample(payload: Mapping[str, Any]) -> ThreeSmuPlotSample:
    """Convert a CLI live-stream ``sample`` event without hardware access."""

    coordinates = payload.get("coordinates", {})
    wire_readings = payload.get("readings", {})
    readings = {
        role: ThreeSmuPlotReading(
            coordinate=_finite_or_none(coordinates.get(role)),
            source_setpoint=_finite_or_none(reading.get("source_setpoint")),
            voltage_v=_finite_or_none(reading.get("voltage_v")),
            current_a=_finite_or_none(reading.get("current_a")),
            resistance_ohm=_finite_or_none(reading.get("resistance_ohm")),
            output_enabled=_optional_bool(reading.get("output_enabled")),
            compliance_trip=_optional_bool(reading.get("compliance_trip")),
            status=_optional_text(reading.get("status")),
        )
        for role, reading in wire_readings.items()
        if role in SEMANTIC_ROLES and isinstance(reading, Mapping)
    }
    return ThreeSmuPlotSample(
        point_index=int(payload["point_index"]),
        repeat_index=int(payload["repeat_index"]),
        segment=str(payload["segment"]),
        elapsed_s=float(payload["elapsed_s"]),
        readings=readings,
        clean=bool(payload.get("clean", False)),
        problems="; ".join(map(str, payload.get("problems", ()))),
    )


def plot_sample_from_formal_sample(sample: ThreeSmuSample) -> ThreeSmuPlotSample:
    """Use the same wire contract for CLI callbacks and fake-instrument tests."""

    return live_payload_to_plot_sample(_sample_payload(sample))


def load_three_smu_plot_samples(
    path: str | Path,
    *,
    include_rejected: bool = False,
    include_problem: bool = False,
) -> tuple[ThreeSmuPlotSample, ...]:
    """Load wide formal samples; completed/accepted/clean remains the default."""

    run_dir = resolve_run_dir(path)
    metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
    accepted = metadata.get("status") == "completed" and metadata.get("accepted") is True
    if not accepted and not include_rejected:
        return ()
    active_roles = metadata.get("active_roles")
    roles = (
        tuple(active_roles)
        if isinstance(active_roles, list) and all(role in SEMANTIC_ROLES for role in active_roles)
        else SEMANTIC_ROLES
    )
    samples: list[ThreeSmuPlotSample] = []
    with (run_dir / "data.csv").open(newline="", encoding="utf-8") as file:
        for record in csv.DictReader(file):
            clean = _csv_bool(record.get("sample_clean", ""))
            if not clean and not include_problem:
                continue
            readings: dict[str, ThreeSmuPlotReading] = {}
            for role in roles:
                if not record.get(f"{role}_timestamp", "").strip():
                    continue
                readings[role] = ThreeSmuPlotReading(
                    coordinate=_finite_or_none(record.get(f"{role}_coordinate")),
                    source_setpoint=_finite_or_none(record.get(f"{role}_source_setpoint")),
                    voltage_v=_finite_or_none(record.get(f"{role}_voltage_v")),
                    current_a=_finite_or_none(record.get(f"{role}_current_a")),
                    resistance_ohm=_finite_or_none(record.get(f"{role}_resistance_ohm")),
                    output_enabled=_optional_bool(record.get(f"{role}_output_enabled")),
                    compliance_trip=_optional_bool(record.get(f"{role}_compliance_trip")),
                    status=_optional_text(record.get(f"{role}_status")),
                )
            samples.append(
                ThreeSmuPlotSample(
                    point_index=int(record["point_index"]),
                    repeat_index=int(record["repeat_index"]),
                    segment=record["segment"],
                    elapsed_s=float(record["elapsed_s"]),
                    readings=readings,
                    clean=clean,
                    problems=record.get("problems", ""),
                )
            )
    return tuple(samples)


def axis_options(samples: Sequence[ThreeSmuPlotSample]) -> tuple[tuple[str, str], ...]:
    """Return only numeric axes that are present in the selected data."""

    options = list((label, key) for key, label in _GLOBAL_AXES.items())
    roles = [
        role
        for role in SEMANTIC_ROLES
        if any(role in sample.readings for sample in samples)
    ]
    for role in roles:
        for field, label in _ROLE_FIELDS:
            key = f"{role}.{field}"
            if any(axis_value(sample, key) is not None for sample in samples):
                options.append((f"{role}: {label}", key))
    return tuple(options)


def axis_value(sample: ThreeSmuPlotSample, key: str) -> float | None:
    if key in _GLOBAL_AXES:
        return float(getattr(sample, key))
    try:
        role, field = key.split(".", 1)
    except ValueError:
        return None
    reading = sample.readings.get(role)
    if reading is None or field not in {item[0] for item in _ROLE_FIELDS}:
        return None
    value = getattr(reading, field)
    return None if value is None or not math.isfinite(value) else float(value)


def categorical_options(
    samples: Sequence[ThreeSmuPlotSample],
    key: str,
) -> tuple[tuple[str, str], ...]:
    if key == "segment":
        return tuple((item, item) for item in sorted({sample.segment for sample in samples}))
    if key == "repeat_index":
        return tuple((str(item + 1), str(item)) for item in sorted({sample.repeat_index for sample in samples}))
    values = sorted({axis_value(sample, key) for sample in samples if axis_value(sample, key) is not None})
    return tuple((f"{value:g}", repr(value)) for value in values)


def select_samples(
    samples: Iterable[ThreeSmuPlotSample],
    *,
    segment: str | None = None,
    repeat_index: int | None = None,
    slice_axis: str | None = None,
    slice_value: float | None = None,
) -> tuple[ThreeSmuPlotSample, ...]:
    selected = []
    for sample in samples:
        if segment is not None and sample.segment != segment:
            continue
        if repeat_index is not None and sample.repeat_index != repeat_index:
            continue
        if slice_axis is not None and slice_value is not None:
            value = axis_value(sample, slice_axis)
            if value is None or not math.isclose(value, slice_value, rel_tol=0.0, abs_tol=1e-12):
                continue
        selected.append(sample)
    return tuple(selected)


def plot_xy(
    samples: Sequence[ThreeSmuPlotSample],
    *,
    x_axis: str,
    y_axis: str,
    series_axis: str | None = None,
    scatter: bool = False,
    x_log: bool = False,
    y_log: bool = False,
):
    """Draw ordered line/scatter data, keeping forward/reverse segments separate."""

    plt = _pyplot()
    figure, axis = plt.subplots(figsize=(6.8, 4.4), constrained_layout=True)
    segments = {sample.segment for sample in samples}
    groups: dict[tuple[object, ...], list[tuple[float, float]]] = {}
    for sample in samples:
        x_value = axis_value(sample, x_axis)
        y_value = axis_value(sample, y_axis)
        if x_value is None or y_value is None:
            continue
        key: tuple[object, ...] = ()
        if series_axis is not None:
            series_value: object = sample.segment if series_axis == "segment" else (
                sample.repeat_index if series_axis == "repeat_index" else axis_value(sample, series_axis)
            )
            if series_value is None:
                continue
            key += (series_axis, series_value)
        if len(segments) > 1 and series_axis != "segment":
            key += ("segment", sample.segment)
        groups.setdefault(key, []).append((x_value, y_value))
    if not groups:
        raise ValueError("No finite samples remain for the selected axes and filters")
    for key, values in groups.items():
        label = _group_label(key)
        x_values, y_values = zip(*values, strict=True)
        if scatter:
            axis.scatter(x_values, y_values, label=label)
        else:
            axis.plot(x_values, y_values, marker="o", linewidth=1.2, label=label)
    axis.set_xlabel(axis_label(x_axis))
    axis.set_ylabel(axis_label(y_axis))
    axis.grid(True, alpha=0.25)
    if x_log:
        axis.set_xscale("log")
    if y_log:
        axis.set_yscale("log")
    if len(groups) > 1:
        axis.legend()
    return figure


def plot_map(
    samples: Sequence[ThreeSmuPlotSample],
    *,
    x_axis: str,
    y_axis: str,
    color_axis: str,
    x_log: bool = False,
    y_log: bool = False,
):
    """Draw a live-safe colour map: incomplete scan coordinates remain visible."""

    plt = _pyplot()
    points = [
        (axis_value(sample, x_axis), axis_value(sample, y_axis), axis_value(sample, color_axis))
        for sample in samples
    ]
    points = [point for point in points if all(value is not None for value in point)]
    if not points:
        raise ValueError("No finite samples remain for the selected map axes and filters")
    figure, axis = plt.subplots(figsize=(6.4, 4.8), constrained_layout=True)
    image = axis.scatter(
        [point[0] for point in points],
        [point[1] for point in points],
        c=[point[2] for point in points],
        marker="s",
        s=70,
    )
    axis.set_xlabel(axis_label(x_axis))
    axis.set_ylabel(axis_label(y_axis))
    axis.grid(True, alpha=0.25)
    if x_log:
        axis.set_xscale("log")
    if y_log:
        axis.set_yscale("log")
    figure.colorbar(image, ax=axis, label=axis_label(color_axis))
    return figure


def axis_label(key: str) -> str:
    if key in _GLOBAL_AXES:
        return _GLOBAL_AXES[key]
    for label, candidate in axis_options_from_key(key):
        if candidate == key:
            return label
    return key


def axis_options_from_key(key: str) -> tuple[tuple[str, str], ...]:
    if "." not in key:
        return ()
    role, field = key.split(".", 1)
    return tuple(
        (f"{role}: {label}", f"{role}.{candidate}")
        for candidate, label in _ROLE_FIELDS
        if candidate == field
    )


def default_plot_specs(
    samples: Sequence[ThreeSmuPlotSample],
    mode: str | None,
) -> tuple[dict[str, str], ...]:
    """Small useful defaults; every resulting card remains freely editable."""

    roles = [role for role in SEMANTIC_ROLES if any(role in item.readings for item in samples)]
    if not roles:
        return ({"type": "line", "x": "elapsed_s", "y": "point_index"},)
    response = "smu_bias.current_a" if "smu_bias" in roles else f"{roles[0]}.current_a"
    if mode == "bias_iv" and "smu_bias" in roles:
        return ({"type": "line", "x": "smu_bias.coordinate", "y": response},)
    if mode == "top_gate_transfer" and "gate_top" in roles:
        return ({"type": "line", "x": "gate_top.coordinate", "y": response},)
    if mode == "bottom_gate_transfer" and "gate_bottom" in roles:
        return ({"type": "line", "x": "gate_bottom.coordinate", "y": response},)
    if mode == "paired_gate":
        return tuple(
            {"type": "line", "x": f"{role}.coordinate", "y": response}
            for role in ("gate_top", "gate_bottom")
            if role in roles
        ) or ({"type": "line", "x": "elapsed_s", "y": response},)
    if mode == "multi_smu_map" and len(roles) >= 2:
        x_role, y_role = (
            ("gate_top", "gate_bottom")
            if {"gate_top", "gate_bottom"}.issubset(roles)
            else (roles[0], roles[1])
        )
        specs = [{"type": "map", "x": f"{x_role}.coordinate", "y": f"{y_role}.coordinate", "z": response}]
        if "smu_bias" in roles:
            gate_roles = [role for role in roles if role != "smu_bias"]
            if gate_roles:
                specs.append({"type": "line", "x": "smu_bias.coordinate", "y": response, "series": f"{gate_roles[0]}.coordinate"})
        return tuple(specs)
    return ({"type": "line", "x": "elapsed_s", "y": response},)


def _group_label(key: tuple[object, ...]) -> str:
    if not key:
        return "samples"
    labels = []
    for name, value in zip(key[::2], key[1::2], strict=True):
        if name == "repeat_index":
            labels.append(f"repeat {int(value) + 1}")
        elif name == "segment":
            labels.append(f"segment {value}")
        else:
            labels.append(f"{axis_label(str(name))}={float(value):g}")
    return "; ".join(labels)


def _finite_or_none(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _optional_bool(value: object) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _csv_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _pyplot():
    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError("Plotting requires: python -m pip install -e '.[analysis]'") from exc
    return plt
