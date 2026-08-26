from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import math
from typing import Iterable

from .models import VectorField
from .safety import MagnetLimits, validate_vector_field


@dataclass(frozen=True, slots=True)
class TemperatureFieldPoint:
    temperature_k: float
    field: VectorField


@dataclass(frozen=True, slots=True)
class GatePoint:
    top_v: float
    bottom_v: float


def voltage_scan(start_v: float, stop_v: float, step_v: float) -> tuple[float, ...]:
    return _inclusive_scan(start_v, stop_v, step_v, "voltage")


def frequency_scan(
    start_hz: float, stop_hz: float, step_hz: float
) -> tuple[float, ...]:
    values = _inclusive_scan(start_hz, stop_hz, step_hz, "frequency")
    if any(value <= 0 for value in values):
        raise ValueError("Frequency scan values must be positive.")
    return values


def temperature_scan_points(
    start_k: float, stop_k: float, step_k: float
) -> tuple[float, ...]:
    """Return an inclusive, ascending temperature plan without float drift."""

    values = _inclusive_scan(start_k, stop_k, step_k, "temperature")
    if any(value <= 0 for value in values):
        raise ValueError("Temperature scan values must be positive.")
    if len(values) > 1 and step_k <= 0:
        raise ValueError("Temperature scans must be ascending.")
    return values


def field_grid(
    *,
    bx_values: Iterable[float],
    bz_values: Iterable[float],
    limits: MagnetLimits = MagnetLimits(),
) -> tuple[VectorField, ...]:
    bx = _finite_values(bx_values, "bx_values")
    bz = _finite_values(bz_values, "bz_values")
    return tuple(
        validate_vector_field(VectorField(bx_t, bz_t), limits)
        for bx_t in bx
        for bz_t in bz
    )


def temperature_field_grid(
    *,
    temperatures_k: Iterable[float],
    fields: Iterable[VectorField],
    limits: MagnetLimits = MagnetLimits(),
) -> tuple[TemperatureFieldPoint, ...]:
    temperatures = _finite_values(temperatures_k, "temperatures_k")
    if any(value <= 0 for value in temperatures):
        raise ValueError("Temperature values must be positive.")
    checked_fields = tuple(validate_vector_field(field, limits) for field in fields)
    if not checked_fields:
        raise ValueError("fields cannot be empty.")
    return tuple(
        TemperatureFieldPoint(temperature, field)
        for temperature in temperatures
        for field in checked_fields
    )


def gate_grid(
    *,
    top_values_v: Iterable[float],
    bottom_values_v: Iterable[float],
    serpentine: bool = False,
) -> tuple[GatePoint, ...]:
    top = _finite_values(top_values_v, "top_values_v")
    bottom = _finite_values(bottom_values_v, "bottom_values_v")
    points: list[GatePoint] = []
    for index, top_v in enumerate(top):
        row = reversed(bottom) if serpentine and index % 2 else bottom
        points.extend(GatePoint(top_v, bottom_v) for bottom_v in row)
    return tuple(points)


def paired_gate_scan(
    *, top_values_v: Iterable[float], bottom_values_v: Iterable[float]
) -> tuple[GatePoint, ...]:
    top = _finite_values(top_values_v, "top_values_v")
    bottom = _finite_values(bottom_values_v, "bottom_values_v")
    if len(top) != len(bottom):
        raise ValueError("Paired gate scans require the same number of values.")
    return tuple(GatePoint(top_v, bottom_v) for top_v, bottom_v in zip(top, bottom))


def _inclusive_scan(
    start: float, stop: float, step: float, name: str
) -> tuple[float, ...]:
    for value, label in ((start, "start"), (stop, "stop"), (step, "step")):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{name} {label} must be finite.")
    if step == 0:
        raise ValueError(f"{name} step cannot be zero.")
    if start < stop and step < 0 or start > stop and step > 0:
        raise ValueError(f"{name} step direction does not reach the stop value.")
    if start == stop:
        return (float(start),)
    try:
        decimal_start = Decimal(str(start))
        decimal_stop = Decimal(str(stop))
        decimal_step = Decimal(str(step))
        count = (decimal_stop - decimal_start) / decimal_step
    except (InvalidOperation, ZeroDivisionError) as exc:
        raise ValueError(f"Invalid {name} scan values.") from exc
    integral_count = count.to_integral_value()
    if count != integral_count or integral_count < 0:
        raise ValueError(f"{name} step must land exactly on the stop value.")
    values = tuple(
        float(decimal_start + decimal_step * index)
        for index in range(int(integral_count) + 1)
    )
    return tuple(0.0 if value == 0 else value for value in values)


def _finite_values(values: Iterable[float], name: str) -> tuple[float, ...]:
    converted = tuple(values)
    if not converted:
        raise ValueError(f"{name} cannot be empty.")
    for value in converted:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{name} must contain only finite numbers.")
    return tuple(float(value) for value in converted)
