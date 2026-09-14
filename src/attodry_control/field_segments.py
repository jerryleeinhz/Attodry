"""Hardware-free, bounded expansion of ordered single-axis field segments."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import math

from .models import VectorField
from .safety import MagnetLimits, validate_vector_field


MAX_SEGMENT_POINTS = 10_000


@dataclass(frozen=True, slots=True)
class FieldSegment:
    minimum: float
    maximum: float
    direction: str
    count: int
    step: float | None

    def definition(self) -> dict[str, object]:
        return {
            "min": self.minimum, "max": self.maximum, "direction": self.direction,
            **({"step": self.step} if self.step is not None else {"points": self.count}),
        }


@dataclass(frozen=True, slots=True)
class FieldSegmentPlan:
    axis: str
    segments: tuple[FieldSegment, ...]
    points: tuple[VectorField, ...]
    point_segment_indices: tuple[int, ...]

    def metadata(self) -> dict[str, object]:
        return {
            "version": 1,
            "axis": self.axis,
            "segments": [segment.definition() for segment in self.segments],
            "point_segment_indices": list(self.point_segment_indices),
        }


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number in T.")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{label} must be a finite number in T.") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number in T.")
    return result


def expand_field_segments(
    axis: object, raw_segments: object, limits: MagnetLimits,
) -> FieldSegmentPlan:
    if axis not in ("x", "z"):
        raise ValueError("magnetic_field_run.axis must be 'x' or 'z'.")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise ValueError("magnetic_field_run.segments must be a non-empty array.")
    specs: list[FieldSegment] = []
    total = 0
    for index, raw in enumerate(raw_segments):
        label = f"magnetic_field_run.segments[{index}]"
        if not isinstance(raw, dict) or not {"min", "max"} <= raw.keys():
            raise ValueError(f"{label} requires min and max.")
        if raw.keys() - {"min", "max", "step", "points", "direction"}:
            raise ValueError(f"{label} has unknown fields.")
        if ("step" in raw) == ("points" in raw):
            raise ValueError(f"{label} requires exactly one of step or points.")
        low = _number(raw["min"], f"{label}.min")
        high = _number(raw["max"], f"{label}.max")
        if low >= high:
            raise ValueError(f"{label}.max must be greater than min; use direction for reverse scans.")
        direction = raw.get("direction", "ascending")
        if direction not in ("ascending", "descending"):
            raise ValueError(f"{label}.direction must be 'ascending' or 'descending'.")
        for value in (low, high):
            validate_vector_field(
                VectorField(value, 0.0) if axis == "x" else VectorField(0.0, value), limits,
            )
        step = None
        if "step" in raw:
            step = _number(raw["step"], f"{label}.step")
            if step <= 0:
                raise ValueError(f"{label}.step must be positive, including descending scans.")
            intervals = (Decimal(str(high)) - Decimal(str(low))) / Decimal(str(step))
            if intervals != intervals.to_integral_value() or intervals < 1:
                raise ValueError(f"{label}.step must divide max-min exactly so max is included.")
            if intervals >= MAX_SEGMENT_POINTS:
                raise ValueError(f"segments exceed {MAX_SEGMENT_POINTS} expanded points.")
            count = int(intervals) + 1
        else:
            count = raw["points"]
            if isinstance(count, bool) or not isinstance(count, int) or count < 2:
                raise ValueError(f"{label}.points must be an integer >= 2.")
        total += count
        if total > MAX_SEGMENT_POINTS:
            raise ValueError(f"segments exceed {MAX_SEGMENT_POINTS} expanded points.")
        specs.append(FieldSegment(low, high, direction, count, step))

    # Count and endpoint validation precede allocation. Preserve shared endpoints.
    points: list[VectorField] = []
    indices: list[int] = []
    for index, spec in enumerate(specs):
        low, high = Decimal(str(spec.minimum)), Decimal(str(spec.maximum))
        order = range(spec.count)
        if spec.direction == "descending":
            order = reversed(order)
        for offset in order:
            value = float(low + (high - low) * offset / (spec.count - 1))
            if offset == 0:
                value = spec.minimum
            elif offset == spec.count - 1:
                value = spec.maximum
            field = VectorField(value, 0.0) if axis == "x" else VectorField(0.0, value)
            points.append(validate_vector_field(field, limits))
            indices.append(index)
    return FieldSegmentPlan(axis, tuple(specs), tuple(points), tuple(indices))
