"""Explicit acquisition exception for overloads; never erase the raw fault.

The acquisition overload policy applies after the existing settled recheck.
Separately, fully proven SR865A CH1/CH2 analog output scaling is record-only at
live decision gates. Input/digital-channel faults and incomplete evidence
retain their guards; neither rule erases raw quality flags.
"""
from __future__ import annotations

from collections.abc import Mapping
import re

OVERLOAD_POLICIES = ("abort", "continue_unselected", "record_continue")
OUTPUT_OVERLOAD_POLICY = "sr865a-analog-output-record-only-v1"


def _value(value, key):
    return value.get(key) if isinstance(value, Mapping) else getattr(value, key, None)


def _native_analog_output_proof(native):
    """Prove analog CH1/CH2 output scaling, with no other current/latched fault."""
    words = {key: _value(native, key) for key in (
        "current_status_raw", "lia_status_raw", "error_status_raw",
        "standard_event_status_raw", "unknown_status_bits")}
    if (any(type(value) is not int for value in words.values())
            or words["error_status_raw"] != 0
            or words["standard_event_status_raw"] != 0 or words["unknown_status_bits"] != 0):
        return False
    current, lia = words["current_status_raw"], words["lia_status_raw"]
    if (not 0 <= current <= 3 or not 0 <= lia <= 0xFFFF
            or lia & ~0x7083 or not (current | lia) & 0x3):
        return False
    expected = {"locked": True, "consumed_status_latches": True,
        "output_scale_overload_latched": bool(lia & 0x3), "input_overload": False,
        "output_scale_overload": bool(current & 0x3), "instrument_error": False,
        "reference_unlock_latched": False, "input_overload_latched": False,
        "filter_fault_latched": False, "configuration_changed_latched": False,
        "power_on_latched": False}
    if any(_value(native, key) is not value for key, value in expected.items()):
        return False
    raw = _value(native, "raw")
    if not isinstance(raw, (list, tuple)) or len(raw) != 4:
        return False
    for record, command, value in zip(raw, ("CUROVLDSTAT?", "LIAS?", "ERRS?", "*ESR?"),
                                      (current, lia, 0, 0)):
        response = _value(record, "response")
        has_error = "error" in record if isinstance(record, Mapping) else hasattr(record, "error")
        if (not has_error or _value(record, "command") != command or _value(record, "error") is not None
                or not isinstance(response, str) or re.fullmatch(r"[+]?\d+", response.strip()) is None
                or int(response.strip()) != value):
            return False
    return True


def nonblocking_native_analog_output(status):
    """Only fully evidenced native CH1/CH2 output scaling is record-only.

    Raw overload flags and measurement quality remain unchanged. In particular,
    unknown evidence, data-channel scaling and input/reference faults fail.
    """
    if (_value(status, "model") != "SR865A"
            or _value(status, "output_overload_policy") != OUTPUT_OVERLOAD_POLICY):
        return False
    expected = {"status_known": True, "output_overload": True,
        "input_or_reserve_overload": False, "filter_overload": False,
        "reference_unlocked": False, "frequency_range_changed": False,
        "time_constant_changed": False, "configuration_changed_latched": False,
        "power_on_latched": False, "instrument_error": False}
    if any(_value(status, key) is not value for key, value in expected.items()):
        return False
    native = _value(status, "native_status")
    return (type(_value(status, "raw")) is int
            and type(_value(status, "unknown_status_bits")) is int
            and _value(status, "unknown_status_bits") == 0
            and _value(status, "raw") == _value(native, "lia_status_raw")
            and _native_analog_output_proof(native))


def blocking_output_overload(status):
    """Decision view only; never rewrite the recorded output-overload flag."""
    if nonblocking_native_analog_output(status):
        return False
    if _value(status, "output_overload") is not False:
        return True
    if _value(status, "model") == "SR865A":
        native = _value(status, "native_status")
        # A forged clear aggregate must not mask contradictory native evidence.
        return any(type(_value(native, key)) is int and _value(native, key) & 0xF03
                   for key in ("current_status_raw", "lia_status_raw")) or any(
                       _value(native, key) is True for key in
                       ("output_scale_overload", "output_scale_overload_latched"))
    return False


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
