"""Explicit acquisition exception for overloads; never erase the raw fault.

This policy is applied only after the existing settled status recheck. Setup,
cleanup, communication and all non-overload checks remain fail closed.
"""
from __future__ import annotations

from collections.abc import Mapping

OVERLOAD_POLICIES = ("abort", "continue_unselected", "record_continue")
_FAULTS = {f"lockin_{role} {fault}{stage}": f"lockin_{role}"
           for role in ("xx", "xy")
           for fault in ("input/reserve overload", "filter overload")
           for stage in ("", " during harmonic transition", " during sensitivity transition")}


class OverloadPolicy:
    def __init__(self, mode="abort", selected=None):
        if mode not in OVERLOAD_POLICIES:
            raise ValueError(f"Unknown overload policy: {mode!r}")
        self.mode = mode
        self.selected = selected or {"lockin_xx": (1,), "lockin_xy": (1,)}

    def partition(self, problems):
        blocking, continued = [], []
        for problem in problems:
            role = _FAULTS.get(problem)
            allowed = role is not None and (
                self.mode == "record_continue" or
                (self.mode == "continue_unselected" and not self.selected.get(role, (1,))))
            (continued if allowed else blocking).append(problem)
        return blocking, continued

    def apply(self, record, problems):
        blocking, continued = self.partition(problems)
        record["overload_policy"] = self.mode
        record["blocking_problems"] = list(blocking)
        record["continued_overload_problems"] = list(continued)
        return blocking

    def annotate_sample(self, sample):
        """Per-role quality retains own faults and every non-continuable fault."""
        problems = sample["problems"]
        blocking = self.apply(sample, problems)
        continued = sample["continued_overload_problems"]
        per_role = sample["problems_by_role"] = {}
        valid = sample["valid_for_analysis_by_role"] = {}
        for role in ("lockin_xx", "lockin_xy"):
            own = [p for p in continued if _FAULTS[p] == role]
            per_role[role] = list(dict.fromkeys([*blocking, *own]))
            status = sample[role].get("lia_status", {})
            flagged = any(status.get(k) for k in (
                "input_or_reserve_overload", "filter_overload", "output_overload"))
            valid[role] = not per_role[role] and not flagged
        return blocking


def overload_summary(value):
    """Count actual formal pairs, not duplicate transition/recheck audit views."""
    pairs = []
    def visit(item):
        if isinstance(item, Mapping):
            if item.get("sampling_policy") == "independent_roles" and "selected_roles" in item:
                pairs.append(item)
            else:
                for child in item.values():
                    visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)
    visit(value)
    counts = {role: sum(any(pair[role].get("lia_status", {}).get(k) for k in (
        "input_or_reserve_overload", "filter_overload", "output_overload"))
        for pair in pairs) for role in ("lockin_xx", "lockin_xy")}
    return {"formal_pairs": len(pairs), "overloaded_pairs_by_role": counts,
            "continued_overload_pairs": sum(bool(p.get("continued_overload_problems")) for p in pairs),
            "data_quality": "overload_recorded" if any(counts.values()) else "no_overload_recorded"}


def reading_allows_continuation(reading):
    """Combination acquisition may retain invalid, explicitly opted-in data.

    This exception cannot excuse another module's fault or an incomplete read.
    Raw per-role overload validity still controls analysis independently.
    """
    if reading.get("clean") is True and not reading.get("problems"):
        return True
    if reading.get("module") != "lockin" or not reading.get("overload_continuation"):
        return False
    if not reading.get("problems") or OverloadPolicy("record_continue").partition(reading["problems"])[0]:
        return False
    samples = reading.get("status", {}).get("samples", ())
    return bool(samples) and all(
        s.get("overload_policy") == "record_continue"
        and s.get("blocking_problems") == [] and s.get("settings_verified") is True
        and not OverloadPolicy("record_continue").partition(s.get("problems", ()))[0]
        for s in samples)
