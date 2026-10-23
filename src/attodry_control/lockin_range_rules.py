"""Offline compilation of outer scan coordinates to fixed lock-in sensitivities.

Rules select existing planned points only. No measured values, priorities,
implicit defaults or device operations participate in resolution.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
import itertools
import math
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .combination_scan import CombinationPlan


ROLES = ("lockin_xx", "lockin_xy", "lockin_aux")


@dataclass(frozen=True)
class LockinRangeRule:
    name: str
    match: tuple[tuple[str, tuple[float, ...]], ...]
    point_indices: tuple[tuple[str, tuple[int, ...]], ...]
    ranges: tuple[tuple[str, float], ...]

    def snapshot(self) -> dict:
        return {"name": self.name,
                "match": {key: list(values) for key, values in self.match},
                "point_indices": {key: list(values) for key, values in self.point_indices},
                **{role + "_full_scale_v": value for role, value in self.ranges}}


@dataclass(frozen=True)
class LockinRangeSelection:
    rule_name: str
    outer_indices: tuple[int, ...]
    coordinates: tuple[tuple[str, float], ...]
    ranges: tuple[tuple[str, float], ...]

    @property
    def full_scales_v(self) -> dict[str, float]:
        return dict(self.ranges)

    @property
    def requested(self) -> dict[str, float]:
        return dict(self.coordinates)

    def snapshot(self, outer_modules: tuple[str, ...]) -> dict:
        return {"outer_indices": dict(zip(outer_modules, self.outer_indices)),
                "requested": self.requested, "rule_name": self.rule_name,
                "full_scales_v": self.full_scales_v}


@dataclass(frozen=True)
class LockinRangeSchedule:
    outer_modules: tuple[str, ...]
    outer_point_counts: tuple[int, ...]
    rules: tuple[LockinRangeRule, ...]
    entries: tuple[LockinRangeSelection, ...]

    def resolve(self, indices: Mapping[str, int] | tuple[int, ...]) -> LockinRangeSelection:
        """Resolve planned zero-based visits, ignoring unrelated mapping keys."""
        if isinstance(indices, Mapping):
            if any(module not in indices for module in self.outer_modules):
                raise ValueError("Lock-in range resolution requires every outer module index")
            ordered = tuple(indices[module] for module in self.outer_modules)
        elif isinstance(indices, tuple):
            ordered = indices
        else:
            raise ValueError("Lock-in range indices must be a mapping or tuple")
        if len(ordered) != len(self.outer_modules):
            raise ValueError("Lock-in range index count does not match the outer axes")
        offset = 0
        for index, count in zip(ordered, self.outer_point_counts):
            if type(index) is not int or not 0 <= index < count:
                raise ValueError("Lock-in range point index is outside the planned axis")
            offset = offset * count + index
        return self.entries[offset]

    def preview(self) -> list[dict]:
        return [entry.snapshot(self.outer_modules) for entry in self.entries]

    def snapshot(self) -> dict:
        return {"contract": "outer-condition-lockin-range-v1",
                "outer_modules": list(self.outer_modules),
                "rules": [rule.snapshot() for rule in self.rules],
                "preview": self.preview()}


def _finite_positive(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value > 0


def compile_lockin_range_rules(
    raw_rules: object,
    plan: CombinationPlan,
    allowed_full_scales_by_role: Mapping[str, Iterable[float]],
) -> LockinRangeSchedule:
    """Validate complete rules and bind each existing outer visit exactly once.

    The caller supplies the intersection of each active instrument's supported
    and station-allowed voltage sensitivity full scales. XX and XY are required;
    AUX is included only when enabled. Omitted configuration is handled by the
    caller; an explicitly empty rule list is an error.
    """
    plan.validate()
    modules = tuple(axis.module for axis in plan.axes)
    if "lockin" not in modules:
        raise ValueError("lockin_range_rules requires an active lockin axis")
    outer_axes = plan.axes[:modules.index("lockin")]
    if not outer_axes:
        raise ValueError("lockin_range_rules requires an axis outside lockin")
    outer_modules = tuple(axis.module for axis in outer_axes)
    if (not isinstance(allowed_full_scales_by_role, Mapping)
            or set(allowed_full_scales_by_role) not in (set(ROLES[:2]), set(ROLES))):
        raise ValueError("Lock-in range roles must be XX/XY and optionally enabled AUX")
    allowed = {}
    for role in ROLES:
        if role not in allowed_full_scales_by_role:
            continue
        values = tuple(allowed_full_scales_by_role[role])
        if not values or any(not _finite_positive(value) for value in values):
            raise ValueError(f"Invalid supported/allowed full scales for {role}")
        allowed[role] = frozenset(values)
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ValueError("lockin_range_rules must be a nonempty array of tables")

    coordinate_owner = {key: axis.module for axis in outer_axes for key in axis.points[0].values}
    coordinate_values = {key: {point.values[key] for point in axis.points}
                         for axis in outer_axes for key in axis.points[0].values}
    point_counts = {axis.module: len(axis.points) for axis in outer_axes}
    expected_range_keys = {role + "_full_scale_v" for role in allowed}
    permitted_keys = {"name", "match", "point_indices"} | expected_range_keys
    rules = []
    names = set()
    for number, raw in enumerate(raw_rules):
        label = f"lockin_range_rules[{number}]"
        if not isinstance(raw, dict) or set(raw) - permitted_keys:
            raise ValueError(f"{label} has unsupported fields or is not a table")
        name = raw.get("name")
        if not isinstance(name, str) or not name.strip() or name in names:
            raise ValueError(f"{label}.name must be nonempty and unique")
        names.add(name)
        if not expected_range_keys <= set(raw):
            raise ValueError(f"{label} must specify every active lock-in full scale")
        ranges = []
        for role, scales in allowed.items():
            value = raw[role + "_full_scale_v"]
            if not _finite_positive(value) or value not in scales:
                raise ValueError(f"{label}.{role}_full_scale_v is not supported and allowed")
            ranges.append((role, float(value)))
        raw_match = raw.get("match", {})
        raw_indices = raw.get("point_indices", {})
        if not isinstance(raw_match, dict) or not isinstance(raw_indices, dict):
            raise ValueError(f"{label}.match and point_indices must be tables")
        if not raw_match and not raw_indices:
            raise ValueError(f"{label} must select outer coordinates or point_indices")
        match = []
        for key, values in raw_match.items():
            if key not in coordinate_owner:
                raise ValueError(f"{label}.match.{key} is not an active coordinate before lockin")
            if (not isinstance(values, list) or not values
                    or any(type(value) not in (int, float) or not math.isfinite(value)
                           for value in values) or len(set(values)) != len(values)):
                raise ValueError(f"{label}.match.{key} must be a nonempty unique finite array")
            if not set(values) <= coordinate_values[key]:
                raise ValueError(f"{label}.match.{key} contains a value absent from the plan")
            match.append((key, tuple(float(value) for value in values)))
        indices = []
        for module, values in raw_indices.items():
            if module not in point_counts:
                raise ValueError(f"{label}.point_indices.{module} is not an axis before lockin")
            if (not isinstance(values, list) or not values
                    or any(type(value) is not int or not 0 <= value < point_counts[module]
                           for value in values) or len(set(values)) != len(values)):
                raise ValueError(f"{label}.point_indices.{module} requires unique planned indices")
            indices.append((module, tuple(values)))
        rules.append(LockinRangeRule(name, tuple(match), tuple(indices), tuple(ranges)))

    entries = []
    used = set()
    for indices in itertools.product(*(range(len(axis.points)) for axis in outer_axes)):
        visit = dict(zip(outer_modules, indices))
        requested = {key: value for axis, index in zip(outer_axes, indices)
                     for key, value in axis.points[index].values.items()}
        matches = [rule for rule in rules
                   if all(requested[key] in values for key, values in rule.match)
                   and all(visit[module] in values for module, values in rule.point_indices)]
        if len(matches) != 1:
            kind = "unmatched" if not matches else "ambiguous"
            raise ValueError(f"Lock-in range rules leave {kind} outer visit {visit}")
        rule = matches[0]
        used.add(rule.name)
        entries.append(LockinRangeSelection(rule.name, indices, tuple(requested.items()), rule.ranges))
    unused = names - used
    if unused:
        raise ValueError(f"Unreachable lock-in range rules: {sorted(unused)}")
    return LockinRangeSchedule(outer_modules, tuple(point_counts.values()),
                               tuple(rules), tuple(entries))
