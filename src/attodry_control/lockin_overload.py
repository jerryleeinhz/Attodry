"""Explicit acquisition status exceptions; never erase the raw fault.

Legacy acquisition applies this policy after its settled status recheck.
The explicitly opted-in photonics profile also records setup/transition
overloads and independently approved reference unlocks. Cleanup, communication
and all other checks remain fail closed.
"""
from __future__ import annotations

from collections.abc import Mapping

OVERLOAD_POLICIES = ("abort", "continue_unselected", "record_continue")
_FAULTS = {f"lockin_{role} {fault}{stage}": f"lockin_{role}"
           for role in ("xx", "xy")
           for fault in ("input/reserve overload", "filter overload", "output-scale overload")
           for stage in ("", " during harmonic transition", " during sensitivity transition")}
_UNLOCKS = {f"lockin_{role} reference unlocked": f"lockin_{role}"
            for role in ("xx", "xy")}
REFERENCE_UNLOCK_POLICIES = ("abort", "record_continue")


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


class PhotonicsStatusPolicy(OverloadPolicy):
    """Independent explicit exceptions for known photonics status faults.

    This is scoped to photonics; the standalone overload policy keeps its
    existing reference-unlock behavior. Native evidence is verified by the
    session and again when loading archived continued readings.
    """
    def __init__(self, mode="abort", reference_unlock_policy="abort", selected=None):
        super().__init__(mode, selected)
        if reference_unlock_policy not in REFERENCE_UNLOCK_POLICIES:
            raise ValueError(f"Unknown reference unlock policy: {reference_unlock_policy!r}")
        self.reference_unlock_policy = reference_unlock_policy

    def partition(self, problems):
        blocking, continued = [], []
        for problem in problems:
            allowed = (problem in _UNLOCKS and self.reference_unlock_policy == "record_continue")
            allowed |= not super().partition([problem])[0]
            (continued if allowed else blocking).append(problem)
        return blocking, continued

    def apply(self, record, problems):
        blocking, continued = self.partition(problems)
        record.update(overload_policy=self.mode,
                      reference_unlock_policy=self.reference_unlock_policy,
                      blocking_problems=list(blocking),
                      continued_overload_problems=[p for p in continued if p in _FAULTS],
                      continued_reference_unlock_problems=[p for p in continued if p in _UNLOCKS])
        return blocking


def overload_summary(value):
    """Count actual formal pairs, not duplicate transition/recheck audit views."""
    pairs = []
    def visit(item):
        if isinstance(item, Mapping):
            if item.get("schema_version") == "photonics-lockin-v1":
                # Model-independent formal samples; native/raw and transition
                # status inside them must not be counted as additional pairs.
                pairs.extend(item.get("samples", ()))
            elif item.get("sampling_policy") == "independent_roles" and "selected_roles" in item:
                pairs.append(item)
            else:
                for child in item.values():
                    visit(child)
        elif isinstance(item, (list, tuple)):
            for child in item:
                visit(child)
    visit(value)
    def overloaded(pair, role):
        if "selected_harmonics" in pair:
            status = pair.get("samples", {}).get(role.removeprefix("lockin_"), {}).get("status", {})
            # Current overload can have cleared while the consumed latch window
            # or a before/after bracket still invalidates this formal role.
            native = status.get("native_status", {})
            return (any(status.get(key) is True for key in ("input_overload", "output_scale_overload"))
                    or (isinstance(native, Mapping) and any(native.get(key) is True for key in
                        ("input_overload_latched", "output_scale_overload_latched")))
                    or any(_FAULTS.get(problem) == role for problem in pair.get("continued_overload_problems", ())))
        return any(pair[role].get("lia_status", {}).get(key) for key in (
            "input_or_reserve_overload", "filter_overload", "output_overload"))
    counts = {role: sum(overloaded(pair, role) for pair in pairs)
              for role in ("lockin_xx", "lockin_xy")}
    def unlocked(pair, role):
        if "selected_harmonics" in pair:
            status = pair.get("samples", {}).get(role.removeprefix("lockin_"), {}).get("status", {})
            native = status.get("native_status", {})
            return (status.get("locked") is False
                    or isinstance(native, Mapping) and native.get("reference_unlock_latched") is True
                    or isinstance(native, (list, tuple)) and bool(native)
                    and isinstance(native[0], Mapping) and native[0].get("reference_unlocked") is True
                    or any(_UNLOCKS.get(p) == role for p in pair.get("continued_reference_unlock_problems", ())))
        return (pair[role].get("lia_status", {}).get("reference_unlocked") is True
                or pair[role].get("reading", {}).get("locked") is False)
    unlock_counts = {role: sum(unlocked(pair, role) for pair in pairs) for role in counts}
    invalid = {role: sum(overloaded(pair, role)
                        or "selected_harmonics" in pair and any(unlocked(pair, other) for other in counts)
                        or pair.get("valid_for_analysis_by_role", {}).get(role) is False
                        or pair.get("samples", {}).get(role.removeprefix("lockin_"), {}).get(
                            "status", {}).get("validity") is False
                        for pair in pairs) for role in counts}
    return {"formal_pairs": len(pairs), "overloaded_pairs_by_role": counts,
            "unlocked_pairs_by_role": unlock_counts,
            "invalid_pairs_by_role": invalid,
            "continued_overload_pairs": sum(bool(p.get("continued_overload_problems")) for p in pairs),
            "continued_reference_unlock_pairs": sum(bool(p.get("continued_reference_unlock_problems")) for p in pairs),
            "data_quality": ("overload_and_reference_unlock_recorded" if any(counts.values()) and any(unlock_counts.values())
                             else "reference_unlock_recorded" if any(unlock_counts.values())
                             else "overload_recorded" if any(counts.values()) else "no_overload_recorded")}


def _photonics_status_evidence(sample, *, transition=False):
    """Validate decoded evidence without reinterpreting vendor status words."""
    if not isinstance(sample, Mapping):
        return None
    status = sample.get("status", {})
    if not isinstance(status, Mapping):
        return None
    if (type(status.get("locked")) is not bool
            or status.get("instrument_error") is not False
            or any(type(status.get(key)) is not bool for key in ("input_overload", "output_scale_overload"))):
        return None
    overloaded = status["input_overload"] or status["output_scale_overload"]
    unlocked = not status["locked"]
    changed = False
    native = status.get("native_status")
    if sample.get("model") == "SR830":
        if not isinstance(native, (list, tuple)) or len(native) != 2 or native[1] != 0:
            return None
        lia = native[0]
        flags = ("input_or_reserve_overload", "filter_overload", "output_overload",
                 "reference_unlocked", "frequency_range_changed", "time_constant_changed")
        if (not isinstance(lia, Mapping) or any(type(lia.get(key)) is not bool for key in flags)
                or type(lia.get("raw")) is not int or not 0 <= lia["raw"] <= 127):
            return None
        unlocked |= lia["reference_unlocked"]
        changed = lia["frequency_range_changed"] or lia["time_constant_changed"]
        overloaded |= any(lia[key] for key in flags[:3])
    elif sample.get("model") == "SR865A":
        flags = ("reference_unlock_latched", "input_overload_latched", "output_scale_overload_latched",
                 "filter_fault_latched", "configuration_changed_latched", "power_on_latched")
        if (not isinstance(native, Mapping) or any(type(native.get(key)) is not bool for key in flags)
                or type(native.get("unknown_status_bits")) is not int or native["unknown_status_bits"] != 0
                or native.get("consumed_status_latches") is not True):
            return None
        if native["filter_fault_latched"] or native["power_on_latched"]:
            return None
        unlocked |= native["reference_unlock_latched"]
        changed = native["configuration_changed_latched"]
        overloaded |= native["input_overload_latched"] or native["output_scale_overload_latched"]
    else:
        return None
    if changed and not transition:
        return None
    if (type(status.get("validity")) is not bool
            and not (transition and changed and status.get("validity") is None)):
        return None
    if (overloaded or unlocked) and status["validity"] is True:
        return None
    if not (status["validity"] is True or overloaded or unlocked or transition and changed):
        return None
    return {"overloaded": overloaded, "unlocked": unlocked}


def reading_allows_continuation(reading):
    """Combination acquisition may retain invalid, explicitly opted-in data.

    This exception cannot excuse another module's fault or an incomplete read.
    Known reference unlocks require their own policy and invalidate both roles.
    Raw per-role validity still controls analysis independently.
    """
    if reading.get("clean") is True and not reading.get("problems"):
        return True
    if (reading.get("module") != "lockin" or not (
            reading.get("overload_continuation") is True
            or reading.get("reference_unlock_continuation") is True)):
        return False
    if not reading.get("problems"):
        return False
    archived_status = reading.get("status", {})
    if not isinstance(archived_status, Mapping):
        return False
    samples = archived_status.get("samples", ())
    if not isinstance(samples, (list, tuple)):
        return False
    if archived_status.get("schema_version") == "photonics-lockin-v1":
        # Photonics has an explicit role-validity contract, including overloads
        # seen only in before/after brackets. Missing evidence is not approval.
        for sample in samples:
            if not isinstance(sample, Mapping):
                return False
            try:
                policy = PhotonicsStatusPolicy(sample.get("overload_policy", "abort"),
                                               sample.get("reference_unlock_policy", "abort"))
            except ValueError:
                return False
            if any(field in archived_status and archived_status[field] != getattr(policy, attribute)
                   for field, attribute in (("overload_policy", "mode"),
                                            ("reference_unlock_policy", "reference_unlock_policy"))):
                return False
            if (sample.get("settings_verified") is not True or sample.get("blocking_problems") != []
                    or policy.partition(sample.get("problems", ()))[0]):
                return False
            # Preserve old overload-only archives, but never infer a new unlock
            # exception from their old overload policy or a reading-level flag.
            overloads = sample.get("continued_overload_problems", ())
            unlocks = sample.get("continued_reference_unlock_problems", ())
            if (any(p not in _FAULTS for p in overloads)
                    or any(p not in _UNLOCKS for p in unlocks)
                    or policy.partition([*overloads, *unlocks])[0]
                    or any(p not in sample.get("problems", ()) for p in [*overloads, *unlocks])):
                return False
            allowed_problems = set(policy.partition(sample.get("problems", ()))[1])
            if allowed_problems != set(overloads) | set(unlocks):
                return False
            valid = sample.get("valid_for_analysis_by_role", {})
            if not isinstance(valid, Mapping):
                return False
            if any(type(valid.get(role)) is not bool for role in ("lockin_xx", "lockin_xy")):
                return False
            if any(valid[_FAULTS[problem]] is not False for problem in overloads):
                return False
            if unlocks and any(valid.values()):
                return False
            if overloads and reading.get("overload_continuation") is not True:
                return False
            if unlocks and reading.get("reference_unlock_continuation") is not True:
                return False
            if unlocks and archived_status.get("reference_unlock_policy") != "record_continue":
                return False
            observations = [(False, sample.get("samples", {}))]
            brackets = sample.get("bracket_samples", {})
            if not isinstance(brackets, Mapping):
                return False
            observations.extend((stage == "transition", pair) for stage, pair in brackets.items())
            observed_unlocks = set()
            for transition, pair in observations:
                if not isinstance(pair, Mapping):
                    return False
                for role in ("xx", "xy"):
                    native = pair.get(role, {})
                    if (not isinstance(native, Mapping) or native.get("role") != role
                            or type(native.get("harmonic")) is not int or native["harmonic"] < 1):
                        return False
                    evidence = _photonics_status_evidence(native, transition=transition)
                    if evidence is None:
                        return False
                    if evidence["overloaded"] and (policy.mode != "record_continue" or valid["lockin_" + role]):
                        return False
                    if evidence["overloaded"] and not any(_FAULTS.get(p) == "lockin_" + role for p in overloads):
                        return False
                    if evidence["unlocked"]:
                        # Legacy expected harmonic transition unlocks remain
                        # scoped to that probe. Explicit new continuation
                        # records every unlock and invalidates the whole pair.
                        if policy.reference_unlock_policy == "record_continue":
                            observed_unlocks.add(f"lockin_{role} reference unlocked")
                            if (f"lockin_{role} reference unlocked" not in unlocks or any(valid.values())):
                                return False
                        elif not transition:
                            return False
                    if not transition and native["status"]["validity"] is False and valid["lockin_" + role]:
                        return False
            if set(unlocks) != observed_unlocks:
                return False
        declared = set(p for sample in samples for p in sample.get("problems", ()))
        if set(reading["problems"]) != declared:
            return False
        recorded_unlock = any(s.get("continued_reference_unlock_problems") for s in samples)
        recorded_overload = any(s.get("continued_overload_problems") for s in samples)
        if (reading.get("reference_unlock_continuation") is True) != recorded_unlock:
            return False
        if (reading.get("overload_continuation") is True) != recorded_overload:
            return False
        return bool(samples)
    # Standalone legacy overload continuation is unchanged and never gains the
    # photonics reference exception, even if a malformed archive adds its flag.
    if (not reading.get("overload_continuation")
            or OverloadPolicy("record_continue").partition(reading["problems"])[0]):
        return False
    return bool(samples) and all(
        s.get("overload_policy") == "record_continue"
        and s.get("blocking_problems") == [] and s.get("settings_verified") is True
        and not OverloadPolicy("record_continue").partition(s.get("problems", ()))[0]
        for s in samples)
