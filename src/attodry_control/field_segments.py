"""Hardware-free expansion of ordered field and fixed-magnitude angle segments."""
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

    def definition(self, *, angle: bool = False) -> dict[str, object]:
        minimum_key = "min_deg" if angle else "min"
        maximum_key = "max_deg" if angle else "max"
        step_key = "step_deg" if angle else "step"
        return {
            minimum_key: self.minimum, maximum_key: self.maximum,
            "direction": self.direction,
            **({step_key: self.step} if self.step is not None else {"points": self.count}),
        }


@dataclass(frozen=True, slots=True)
class FieldSegmentPlan:
    axis: str
    segments: tuple[FieldSegment, ...]
    points: tuple[VectorField, ...]
    point_segment_indices: tuple[int, ...]
    magnitude_t: float | None = None
    angles_deg: tuple[float, ...] | None = None

    def metadata(self) -> dict[str, object]:
        angle = self.axis == "angle_deg_from_z"
        return {
            "version": 2 if angle else 1,
            "axis": self.axis,
            "segments": [segment.definition(angle=angle) for segment in self.segments],
            "point_segment_indices": list(self.point_segment_indices),
            **({"magnitude_t": self.magnitude_t, "angles_deg": list(self.angles_deg)}
               if angle and self.angles_deg is not None else {}),
        }


def _number(value: object, label: str, *, unit: str = "T") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number in {unit}.")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{label} must be a finite number in {unit}.") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number in {unit}.")
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


def expand_angle_segments(
    magnitude_t: object, raw_segments: object, limits: MagnetLimits,
) -> FieldSegmentPlan:
    """Expand ordered angle ranges into exact X/Z targets at one magnitude."""

    magnitude = _number(magnitude_t, "magnetic_field_run.magnitude_t")
    if magnitude <= 0.0:
        raise ValueError("magnetic_field_run.magnitude_t must be positive.")
    if magnitude > limits.experiment_vector_max_t:
        raise ValueError(
            "magnetic_field_run.magnitude_t exceeds the configured vector-field limit."
        )
    if not isinstance(raw_segments, list) or not raw_segments:
        raise ValueError("magnetic_field_run.angle_segments must be a non-empty array.")
    specs: list[FieldSegment] = []
    total = 0
    for index, raw in enumerate(raw_segments):
        label = f"magnetic_field_run.angle_segments[{index}]"
        if not isinstance(raw, dict) or not {"min_deg", "max_deg"} <= raw.keys():
            raise ValueError(f"{label} requires min_deg and max_deg.")
        if raw.keys() - {"min_deg", "max_deg", "step_deg", "points", "direction"}:
            raise ValueError(f"{label} has unknown fields.")
        if ("step_deg" in raw) == ("points" in raw):
            raise ValueError(f"{label} requires exactly one of step_deg or points.")
        low = _number(raw["min_deg"], f"{label}.min_deg", unit="degrees")
        high = _number(raw["max_deg"], f"{label}.max_deg", unit="degrees")
        if low >= high:
            raise ValueError(f"{label}.max_deg must exceed min_deg; use direction for reverse scans.")
        direction = raw.get("direction", "ascending")
        if direction not in ("ascending", "descending"):
            raise ValueError(f"{label}.direction must be 'ascending' or 'descending'.")
        step = None
        if "step_deg" in raw:
            step = _number(raw["step_deg"], f"{label}.step_deg", unit="degrees")
            if step <= 0.0:
                raise ValueError(f"{label}.step_deg must be positive.")
            intervals = (Decimal(str(high)) - Decimal(str(low))) / Decimal(str(step))
            if intervals != intervals.to_integral_value() or intervals < 1:
                raise ValueError(f"{label}.step_deg must divide max_deg-min_deg exactly.")
            if intervals >= MAX_SEGMENT_POINTS:
                raise ValueError(f"angle segments exceed {MAX_SEGMENT_POINTS} expanded points.")
            count = int(intervals) + 1
        else:
            count = raw["points"]
            if isinstance(count, bool) or not isinstance(count, int) or count < 2:
                raise ValueError(f"{label}.points must be an integer >= 2.")
        total += count
        if total > MAX_SEGMENT_POINTS:
            raise ValueError(f"angle segments exceed {MAX_SEGMENT_POINTS} expanded points.")
        specs.append(FieldSegment(low, high, direction, count, step))

    points: list[VectorField] = []
    angles: list[float] = []
    indices: list[int] = []
    for index, spec in enumerate(specs):
        low, high = Decimal(str(spec.minimum)), Decimal(str(spec.maximum))
        order = range(spec.count)
        if spec.direction == "descending":
            order = reversed(order)
        for offset in order:
            angle = float(low + (high - low) * offset / (spec.count - 1))
            if offset == 0:
                angle = spec.minimum
            elif offset == spec.count - 1:
                angle = spec.maximum
            field = VectorField.from_polar(magnitude, angle)
            points.append(validate_vector_field(field, limits))
            angles.append(angle)
            indices.append(index)
    return FieldSegmentPlan(
        "angle_deg_from_z", tuple(specs), tuple(points), tuple(indices),
        magnitude, tuple(angles),
    )
