"""Hardware-free scan expansion shared by modular Combination inputs.

Range syntax follows the integration/Three-SMU min/max/scale contract. Inclusive
linear steps reuse the project's decimal implementation; no values are silently
deduplicated, sorted, rounded or assigned a hardware meaning here.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from .nkt_config import NktError, integer, keys, number
from .scans import _inclusive_scan


MAX_GRID_POINTS = 10000


@dataclass(frozen=True)
class GridValue:
    value: float
    segment: str = "main"
    direction: str = "ordered"
    segment_index: int = 0
    point_index: int = 0


def expand_ranges(ranges, name, *, maximum_points=MAX_GRID_POINTS):
    """Expand ordered inclusive linear/log segments, retaining visit identities."""
    if type(ranges) is not list or not ranges:
        raise NktError(f"{name} must be a nonempty array of range tables")
    expanded = []
    for index, segment in enumerate(ranges):
        label = f"{name}[{index}]"
        keys(segment, label, {"min", "max", "scale"}, {"points", "step", "name"})
        low = number(segment["min"], label + ".min", -1e300, 1e300)
        high = number(segment["max"], label + ".max", -1e300, 1e300)
        if high <= low:
            raise NktError(f"{label}.max must be greater than min; use an explicit list for return visits")
        scale = segment["scale"]
        if scale not in {"linear", "log"}:
            raise NktError(f"{label}.scale must be linear or log")
        if ("points" in segment) == ("step" in segment):
            raise NktError(f"{label} requires exactly one of points or step")
        if "step" in segment:
            if scale != "linear":
                raise NktError(f"{label}: log ranges require points and forbid step")
            step = number(segment["step"], label + ".step", 1e-300, 1e300)
            ratio = (high - low) / step
            if not math.isfinite(ratio) or ratio + 1 > maximum_points - len(expanded):
                raise NktError(f"{name} exceeds the {maximum_points} point budget")
            try:
                values = _inclusive_scan(low, high, step, label)
            except ValueError as exc:
                raise NktError(str(exc)) from exc
        else:
            count = integer(segment["points"], label + ".points", 2, maximum_points)
            if len(expanded) + count > maximum_points:
                raise NktError(f"{name} exceeds the {maximum_points} point budget")
            if scale == "log":
                if low <= 0:
                    raise NktError(f"{label}: log endpoints must be positive")
                start, stop = math.log(low), math.log(high)
                values = tuple(math.exp(start + (stop-start)*i/(count-1)) for i in range(count))
            else:
                values = tuple(low + (high-low)*i/(count-1) for i in range(count))
            values = (low, *values[1:-1], high)
        segment_name = segment.get("name", f"segment-{index}")
        if type(segment_name) is not str or not segment_name.strip():
            raise NktError(f"{label}.name must be a nonempty string")
        expanded.extend(GridValue(v, segment_name, "ascending", index, i)
                        for i, v in enumerate(values))
    return tuple(expanded)


def expand_grid(table, points_key, ranges_key, name, *, maximum_points=MAX_GRID_POINTS):
    """Require one explicit list or one range source for a scalar coordinate."""
    if (points_key in table) == (ranges_key in table):
        raise NktError(f"{name} requires exactly one of {points_key} or {ranges_key}")
    if ranges_key in table:
        return expand_ranges(table[ranges_key], name + "." + ranges_key,
                             maximum_points=maximum_points)
    values = table[points_key]
    if type(values) is not list or not 1 <= len(values) <= maximum_points:
        raise NktError(f"{name}.{points_key} requires 1..{maximum_points} explicit points")
    return tuple(GridValue(number(v, f"{name}.{points_key}[{i}]", -1e300, 1e300),
                           point_index=i) for i, v in enumerate(values))
