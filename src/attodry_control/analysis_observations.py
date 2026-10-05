"""Offline channel qualification and statistics of repeated formal observations."""
from __future__ import annotations

from collections import Counter
import math
import re
import statistics
from typing import Mapping, Sequence


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def observation_id(row):
    return {k: row[k] for k in (
        "source_path", "run_id", "condition_id", "attempt_index", "sample_index",
        "repeat_index", "row_index", "role", "harmonic",
    ) if k in row}


def channel_quality(row: Mapping, column: str) -> tuple[str, tuple[str, ...]]:
    """Use only the selected channel's formal status, never transition probes.

    Output overload is a conservative analysis exclusion even though acquisition
    can accept it. Missing status is explicitly unknown, not an inferred pass.
    """
    match = re.fullmatch(r"measured\.(lockin_(xx|xy))_h(\d+)_(x_v|y_v|amplitude_v|phase_deg)", column)
    if not match:
        return "unclassified", ()
    role, short_role, order, metric = match.groups()
    harmonic = int(order)
    issues = []
    evidence = []
    status = row.get("status.lockin") or {}
    if status.get("schema_version") == "photonics-lockin-v1":
        return _photonics_channel_quality(row, column, role, short_role, harmonic, metric, status)
    for sample in status.get("samples", ()):
        reading = sample.get(role) or {}
        values = reading.get("reading") or {}
        actual_harmonic = values.get("harmonic", (sample.get("harmonics_by_role") or {}).get(role, sample.get("harmonic")))
        if actual_harmonic != harmonic:
            continue
        selected = sample.get("selected_roles")
        if selected is not None and short_role not in selected and role not in selected:
            issues.append("not_selected_channel")
            continue
        if sample.get("settings_verified") is False:
            issues.append("settings_unverified")
        evidence.append((reading, sample))
    # Legacy long-row adapters retain role-specific status on that one row.
    if not evidence and row.get("role") in (role, short_role) and row.get("harmonic") == harmonic:
        legacy = row.get("status." + role, row)
        if "lia_status_raw" in legacy or "error_status" in legacy:
            evidence.append(({"lia_status": {"raw": legacy.get("lia_status_raw")},
                              "error_status": legacy.get("error_status")}, {}))
    confirmed_status = False
    for reading, sample in evidence:
        if sample.get("valid_for_analysis_by_role", {}).get(role) is False:
            issues.append("recorded_invalid_for_analysis")
        if sample.get("problems_by_role", {}).get(role):
            issues.append("formal_problem")
        lia = reading.get("lia_status") or {}
        raw = lia.get("raw")
        confirmed_status |= type(raw) is int
        for name, mask in (("input_or_reserve_overload", 1), ("filter_overload", 2),
                           ("output_overload", 4), ("reference_unlocked", 8)):
            if lia.get(name) is True or (type(raw) is int and raw & mask):
                issues.append(name)
        if reading.get("error_status") not in (None, 0):
            issues.append("instrument_error")
        values = reading.get("reading") or {}
        if values.get("locked") is False:
            issues.append("reference_unlocked")
        if values.get("overload") is True and not any("overload" in s for s in issues):
            issues.append("overload")
        for stage in ("settings_before", "settings_after"):
            settings = (sample.get(stage) or {}).get("roles", {}).get(role, {})
            full_scale = settings.get("sensitivity_full_scale_v")
            if finite(full_scale) and full_scale > 0:
                readings = [row.get(f"measured.{role}_h{harmonic}_{field}") for field in ("x_v", "y_v", "amplitude_v")]
                if any(finite(v) and abs(v) > full_scale for v in readings):
                    issues.append("exceeds_full_scale")
    unique = tuple(dict.fromkeys(issues))
    return ("flagged" if unique else "clear" if confirmed_status else "unknown"), unique


def _photonics_channel_quality(row, column, role, short_role, harmonic, metric, status):
    """Read archived normalized evidence; vendor status words are audit only.

    Each formal entry has its own role/harmonic selection. Companion probes,
    settings transitions and today's hardware configuration cannot qualify it.
    This function does not change the outer accepted-run/attempt/cleanup filter.
    """
    issues = []
    selected_count = 0
    companion_seen = False
    all_known = True
    for entry in status.get("samples", ()) or ():
        if not isinstance(entry, Mapping):
            all_known = False
            continue
        selection = entry.get("selected_harmonics") or {}
        readings = entry.get("samples") or {}
        if not isinstance(selection, Mapping) or not isinstance(readings, Mapping):
            all_known = False
            continue
        reading = readings.get(short_role) or {}
        if not isinstance(reading, Mapping):
            all_known = False
            continue
        selected_harmonic = selection.get(short_role)
        if type(selected_harmonic) is not int or selected_harmonic != harmonic:
            if selected_harmonic == harmonic:
                issues.append("invalid_harmonic_selection")
            companion_seen |= reading.get("harmonic") == harmonic
            continue
        selected_count += 1
        if type(reading.get("harmonic")) is not int or reading.get("harmonic") != harmonic:
            issues.append("harmonic_readback_mismatch")
        if reading.get("role") != short_role:
            issues.append("role_readback_mismatch")
        normalized = reading.get("status") or {}
        if not isinstance(normalized, Mapping):
            normalized = {}
        if normalized.get("validity") is False:
            issues.append("recorded_invalid_for_analysis")
        if normalized.get("locked") is False:
            issues.append("reference_unlocked")
        for flag in ("input_overload", "output_scale_overload", "instrument_error"):
            if normalized.get(flag) is True:
                issues.append(flag)
        known = (normalized.get("validity") is True and normalized.get("locked") is True
                 and all(normalized.get(name) is False for name in
                         ("input_overload", "output_scale_overload", "instrument_error")))
        known &= (isinstance(reading.get("model"), str) and bool(reading["model"])
                  and normalized.get("observation") in
                  ("latched_interval", "instantaneous", "instantaneous_and_latched"))
        # Do not manufacture phase=0 when the adapter recorded zero X/Y.
        if metric == "phase_deg" and reading.get("phase_deg") is None:
            known = False
            if finite(row.get(column)):
                issues.append("undefined_phase")
        if not finite(reading.get(metric)) or not finite(row.get(column)):
            known = False
        scales = []
        for stage in ("settings_before", "settings_after"):
            settings = entry.get(stage) or {}
            settings = settings.get(short_role) if isinstance(settings, Mapping) else None
            if not isinstance(settings, Mapping):
                known = False
                continue
            if settings.get("model") != reading.get("model") or settings.get("role") != short_role:
                issues.append("settings_identity_mismatch")
            full_scale = settings.get("sensitivity_full_scale_v")
            if not finite(full_scale) or full_scale <= 0:
                known = False
                continue
            scales.append(full_scale)
            amplitudes = [reading.get(field) for field in ("x_v", "y_v", "amplitude_v")]
            amplitudes += [row.get(f"measured.{role}_h{harmonic}_{field}")
                           for field in ("x_v", "y_v", "amplitude_v")]
            if any(finite(value) and abs(value) > full_scale for value in amplitudes):
                issues.append("exceeds_full_scale")
        if len(scales) == 2 and scales[0] != scales[1]:
            issues.append("settings_changed")
        all_known &= known
    if not selected_count and companion_seen:
        issues.append("not_selected_channel")
    unique = tuple(dict.fromkeys(issues))
    return ("flagged" if unique else "clear" if selected_count and all_known else "unknown"), unique


def qualify_observations(rows: Sequence[Mapping], columns: Sequence[str], policy="exclude"):
    if policy not in ("exclude", "include"):
        raise ValueError("Quality policy must be exclude or include.")
    kept, excluded, flagged = [], [], []
    unknown = 0
    for row in rows:
        findings = {key: channel_quality(row, key) for key in columns}
        issues = {key: list(reasons) for key, (_, reasons) in findings.items() if reasons}
        unknown += any(state == "unknown" for state, _ in findings.values())
        if issues:
            entry = {"sample": observation_id(row), "reasons": issues}
            flagged.append(entry)
            if policy == "exclude":
                excluded.append(entry)
                continue
        kept.append(dict(row))
    counts = Counter(reason for item in flagged for reasons in item["reasons"].values() for reason in set(reasons))
    return tuple(kept), {"quality_policy": policy, "flagged_row_count": len(flagged),
                         "excluded_quality_count": len(excluded), "unknown_status_count": unknown,
                         "quality_reason_counts": dict(counts), "flagged_samples": flagged,
                         "excluded_samples": excluded}


def repeat_statistics(rows: Sequence[Mapping], *, x: str, y: str, mode="mean_sd"):
    """Aggregate only identical formal condition identities, preserving revisits.

    Scalar mean/SD use ddof=1. Phase uses a circular mean and sample SD of
    wrapped angular deviations. SEM assumes independent repeats; no CI claim.
    n=1 has undefined SD/SEM. Antipodal phase means remain undefined.
    """
    if mode not in ("raw", "mean_sd", "mean_sem"):
        raise ValueError("Statistics must be raw, mean_sd or mean_sem.")
    if mode == "raw":
        return tuple({"x": r.get(x), "y": r.get(y), "n": 1, "sd": None, "sem": None,
                      "error": None, "sample_ids": [observation_id(r)]} for r in rows)
    groups = {}
    for row in rows:
        if row.get("condition_id") is None:
            raise ValueError("Repeat statistics require a recorded condition_id; use Raw observations for this source.")
        identity_keys = ["source_path", "run_id", "condition_id", "attempt_index", "repeat_index", "role", "harmonic"]
        identity_keys += sorted(k for k in row if k.startswith(("requested.", "axes.")))
        identity = tuple((k, repr(row.get(k))) for k in identity_keys)
        groups.setdefault(identity, []).append(row)
    points = []
    for group in groups.values():
        valid = [r for r in group if finite(r.get(x)) and finite(r.get(y))]
        if not valid:
            xs = [r[x] for r in group if finite(r.get(x))]
            points.append({"x": statistics.fmean(xs) if xs else None, "y": None,
                           "n": 0, "sd": None, "sem": None, "error": None,
                           "sample_ids": [observation_id(r) for r in group]})
            continue
        values = [float(r[y]) for r in valid]
        n = len(values)
        circular = y.endswith("phase_deg")
        if circular:
            cosine = statistics.fmean(math.cos(math.radians(v)) for v in values)
            sine = statistics.fmean(math.sin(math.radians(v)) for v in values)
            mean = math.degrees(math.atan2(sine, cosine)) if math.hypot(cosine, sine) > 1e-12 else None
            residuals = [(v - mean + 180) % 360 - 180 for v in values] if mean is not None else []
            sd = math.sqrt(sum(v*v for v in residuals)/(n-1)) if n > 1 and residuals else None
        else:
            mean = statistics.fmean(values)
            sd = statistics.stdev(values) if n > 1 else None
        sem = sd / math.sqrt(n) if sd is not None else None
        points.append({"x": statistics.fmean(r[x] for r in valid), "y": mean, "n": n,
                       "x_min": min(r[x] for r in valid), "x_max": max(r[x] for r in valid),
                       "sd": sd, "sem": sem, "error": sd if mode == "mean_sd" else sem,
                       "phase_statistics": "circular mean; wrapped residual sample SD" if circular else None,
                       "sample_ids": [observation_id(r) for r in valid]})
    return tuple(points)
