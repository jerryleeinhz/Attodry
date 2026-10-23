"""Read-only complex light subtraction against an archived optical power point.

Power is a requested plan coordinate, never the minimum measured signal/power.
One reference acquisition is shared by its target samples. Its uncertainty must
not shrink with the number of target repeats; overlap is explicitly accounted
for when the target is itself a subset of that reference acquisition.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
import re
import statistics

from .analysis_observations import channel_quality, finite, observation_id

_CHANNEL = re.compile(r"measured\.lockin_(xx|xy|aux)_h(\d+)_x_v$")
_DELTA = re.compile(r"measured\.lockin_(xx|xy|aux)_h(\d+)_power_delta_(x_v|y_v|amplitude_v|phase_deg)$")
_POWER = "optical_target_power_w"
_RANGES = {"sensitivity", "sensitivity_code", "sensitivity_full_scale_v"}
_NATIVE_READINGS = {"raw", "x_v", "y_v", "amplitude_v", "phase_deg",
                    "snapshot_frequency_hz", "frequency_hz", "lia_status", "error_status"}


def _json(value):
    return json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":"))


def _same_power(left, right):
    return finite(left) and finite(right) and math.isclose(left, right, rel_tol=1e-10, abs_tol=1e-18)


def _id(row):
    identity = observation_id(row)
    if any(identity.get(k) is None for k in ("source_path", "run_id", "condition_id", "sample_index")):
        raise ValueError("Power reference needs recorded source/run/condition/sample identities")
    return _json(identity)


def _light(row):
    if row.get("observation_kind") == "dark":
        return False
    source = (((row.get("status.optical") or {}).get("readback") or {}).get("nkt") or {}).get("source") or {}
    return not (source.get("emission_state") == 0 or source.get("getLdOutputState") is False)


def _power_dependent(row):
    """Only an archived known feedback actuator identifies a seed coordinate."""
    feedback = ((row.get("analysis_context") or {}).get("power_reference_plan") or {}).get("feedback") or {}
    excluded = {_POWER}
    if feedback and (feedback.get("target_mapping") not in ("paired", "cartesian")
                     or feedback.get("actuator") not in ("cld_current", "source_current", "source_power", "varia_nd")):
        raise ValueError("Unknown archived feedback control cannot identify power seed coordinates")
    if feedback.get("target_mapping") in ("paired", "cartesian"):
        if feedback.get("actuator") == "cld_current":
            excluded.add("optical_current_a")
        elif feedback.get("actuator") in ("source_current", "source_power"):
            excluded.add("optical_source_level_pct")
    return excluded


def _group(row):
    """Preserve non-power coordinates and repeated axis visits across powers."""
    _id(row)
    context = row.get("analysis_context") or {}
    plan = context.get("power_reference_plan") or {}
    order = plan.get("loop_order", row.get("loop_order"))
    if (not isinstance(order, (list, tuple)) or "optical" not in order
            or len(set(order)) != len(order)):
        raise ValueError("Power reference needs archived loop order and axis visit identities")
    if type(row.get("repeat_index")) is not int:
        raise ValueError("Power reference needs a recorded repeat_index")
    axes = {}
    for module in order:
        if module == "optical":
            continue
        index = row.get(f"axes.{module}.index")
        if type(index) is not int or index < 0:
            raise ValueError(f"Missing recorded {module} axis visit index")
        axes[module] = {key: row.get(f"axes.{module}.{key}") for key in ("index", "segment", "direction")}
    optical_index = row.get("axes.optical.index")
    if type(optical_index) is not int or optical_index < 0:
        raise ValueError("Missing recorded optical axis visit index")
    optical = [a for a in plan.get("axes", ()) if a.get("module") == "optical"]
    if optical:
        if len(optical) != 1 or optical_index >= len(optical[0].get("points", ())):
            raise ValueError("Optical visit index is outside the archived plan")
        current = optical[0]["points"][optical_index]
        if any(row.get("requested." + k) != v for k, v in current.get("values", {}).items()):
            raise ValueError("Requested optical coordinates disagree with archived plan")
        if any(row.get("axes.optical." + key) != current.get(key, default)
               for key, default in (("segment", "main"), ("direction", "ordered"))):
            raise ValueError("Optical visit labels disagree with archived plan")
    dependent = _power_dependent(row)
    requested = {k[10:]: v for k, v in row.items()
                 if k.startswith("requested.") and k[10:] not in dependent}
    scope = {"source_path": row["source_path"], "run_id": row["run_id"],
             "repeat_index": row["repeat_index"], "loop_order": order,
             "axes": axes, "requested": requested,
             "optical_segment": row.get("axes.optical.segment"),
             "optical_direction": row.get("axes.optical.direction"),
             "simulated": row.get("simulated")}
    from .combination_analysis import positioner_reference_context
    positioner = positioner_reference_context(row)
    if positioner is not None:
        scope["positioner"] = positioner
    return _json(scope)


def _planned_reference(row, mode, selected):
    plan = (row.get("analysis_context") or {}).get("power_reference_plan") or {}
    optical = [axis for axis in plan.get("axes", ()) if axis.get("module") == "optical"]
    if len(optical) != 1 or not optical[0].get("points"):
        if mode == "power_selected":
            return selected
        raise ValueError("Lowest-power reference needs the archived optical plan; use selected-power for legacy data")
    points = optical[0]["points"]
    index = row["axes.optical.index"]
    if index >= len(points):
        raise ValueError("Optical visit index is outside the archived plan")
    actual = {k[10:]: v for k, v in row.items() if k.startswith("requested.optical_")}
    current = points[index]
    if any(actual.get(k) != v for k, v in current.get("values", {}).items()):
        raise ValueError("Requested optical coordinates disagree with archived plan")
    def scope(point):
        return ({k: v for k, v in point.get("values", {}).items() if k not in _power_dependent(row)},
                point.get("segment", "main"), point.get("direction", "ordered"))
    choices = [point.get("values", {}).get(_POWER) for point in points if scope(point) == scope(current)]
    if not choices or any(not finite(p) or p <= 0 for p in choices):
        raise ValueError("Archived optical slice lacks positive nominal power coordinates")
    power = min(choices) if mode == "power_lowest" else selected
    if sum(_same_power(p, power) for p in choices) > 1:
        raise ValueError("Ambiguous reference power: multiple optical visits in archived plan slice")
    return power


def _verified_setting(setting, short, harmonic):
    """Require recorded physical configuration, not equality of missing fields."""
    native = setting.get("native_settings")
    model = setting.get("model")
    if not isinstance(native, dict):
        raise ValueError("Missing complete native formal settings")
    if (not isinstance(setting.get("identity"), str) or not setting["identity"]
            or model not in ("SR830", "SR865A")
            or setting.get("role") not in (short, "lockin_" + short)
            or not finite(setting.get("time_constant_s")) or setting["time_constant_s"] <= 0
            or type(setting.get("harmonic")) is not int or setting["harmonic"] != harmonic
            or not finite(setting.get("sensitivity_full_scale_v")) or setting["sensitivity_full_scale_v"] <= 0
            or setting.get("reference_source") not in ("internal", "external")
            or not finite(native.get("phase_shift_deg")) or not finite(native.get("sine_output_v"))):
        raise ValueError("Incomplete formal identity/reference/phase/input/filter settings evidence")
    if model == "SR830":
        codes = {"reference_mode": (0, 1), "reference_slope": (0, 1, 2),
                 "input_mode": (0, 1), "input_coupling": (0, 1), "shield_grounding": (0, 1),
                 "line_filter": (0, 1, 2, 3), "reserve_mode": (0, 1, 2),
                 "filter_slope": (0, 1, 2, 3), "time_constant": tuple(range(20)),
                 "sensitivity": tuple(range(27))}
        valid = all(type(native.get(k)) is int and native[k] in values for k, values in codes.items())
        valid &= setting.get("input_range_v_peak") is None
        valid &= native.get("reference_mode") == (1 if setting["reference_source"] == "internal" else 0)
    else:
        enums = {"reference_source": ("internal", "external"),
                 "external_reference_edge": ("sine", "rising", "falling"),
                 "input_mode": ("a", "a_minus_b"), "input_coupling": ("ac", "dc"),
                 "shield_grounding": ("float", "ground"),
                 "source_dc_mode": ("common", "difference"),
                 "sync_output_mode": ("blazex", "bipolar_sync", "unipolar_sync")}
        valid = all(native.get(k) in values for k, values in enums.items())
        valid &= (type(native.get("filter_slope_db_oct")) is int and native["filter_slope_db_oct"] in (6, 12, 18, 24)
                  and type(native.get("advanced_filter")) is bool and type(native.get("synchronous_filter")) is bool
                  and finite(setting.get("input_range_v_peak")) and setting["input_range_v_peak"] > 0
                  and finite(native.get("reference_input_impedance_ohm")) and native["reference_input_impedance_ohm"] > 0
                  and finite(native.get("source_offset_v"))
                  and native.get("reference_source") == setting["reference_source"]
                  and native.get("input_range_v_peak") == setting["input_range_v_peak"]
                  and native.get("sensitivity_full_scale_v") == setting["sensitivity_full_scale_v"]
                  and native.get("time_constant_s") == setting["time_constant_s"])
    if not valid:
        raise ValueError("Unknown or incomplete model-specific formal reference/input/filter settings")


def _formal(row, short, harmonic):
    hardware = (row.get("analysis_context") or {}).get("lockin")
    if not isinstance(hardware, dict) or not hardware:
        raise ValueError("Power reference needs archived lock-in configuration")
    formal, ranges = [], []
    status = row.get("status.lockin")
    if not isinstance(status, dict):
        raise ValueError("Missing formal lock-in status")
    for sample in status.get("samples", ()):
        if not isinstance(sample, dict):
            continue
        if "selected_harmonics" in sample:
            selected = (sample.get("selected_harmonics") or {}).get(short)
            before = (sample.get("settings_before") or {}).get(short)
            after = (sample.get("settings_after") or {}).get(short)
        else:
            role = "lockin_" + short
            reading = ((sample.get(role) or {}).get("reading") or {})
            selected = reading.get("harmonic", (sample.get("harmonics_by_role") or {}).get(role, sample.get("harmonic")))
            before = ((sample.get("settings_before") or {}).get("roles") or {}).get(role)
            after = ((sample.get("settings_after") or {}).get("roles") or {}).get(role)
        if type(selected) is not int or selected != harmonic:
            continue
        if sample.get("settings_verified") is not True or not isinstance(before, dict) or not isinstance(after, dict):
            raise ValueError("Missing verified formal settings for power reference")
        for setting in (before, after):
            _verified_setting(setting, short, harmonic)
        def signature(setting, *, keep_range):
            result = {k: v for k, v in setting.items() if k not in {"raw", "native_settings"}
                      and (keep_range or k not in _RANGES)}
            native = setting.get("native_settings")
            if isinstance(native, dict):
                result["native_settings"] = {k: v for k, v in native.items()
                    if k not in _NATIVE_READINGS and (keep_range or k not in _RANGES)}
            return result
        scale = before.get("sensitivity_full_scale_v")
        if not finite(scale) or scale <= 0 or before.get("harmonic") != harmonic:
            raise ValueError("Missing physical sensitivity or harmonic readback")
        if signature(before, keep_range=True) != signature(after, keep_range=True):
            raise ValueError("Formal settings changed during a sample")
        formal.append(signature(before, keep_range=False))
        ranges.append(scale)
    if not formal:
        raise ValueError("No formal reference/phase/settings evidence for power reference")
    if any(item != formal[0] for item in formal[1:]):
        raise ValueError("Multiple incompatible formal settings in one observation")
    # Same run archive and actual formal settings; only confirmed physical
    # sensitivity is allowed to differ between target and reference.
    optical = (row.get("analysis_context") or {}).get("optical")
    chopper = (((row.get("status.optical") or {}).get("readback") or {}).get("chopper"))
    if isinstance(chopper, dict):
        chopper = {k: chopper.get(k) for k in ("phase", "ref", "nharmonic", "dharmonic", "blade", "enable")}
    return _json({"hardware": hardware, "formal": formal[0], "optical": optical, "chopper": chopper}), ranges


def _good(row, prefix):
    return (row.get("accepted") is True and not row.get("_manual_excluded")
            and all(finite(row.get(prefix + metric)) and channel_quality(row, prefix + metric)[0] == "clear"
                    for metric in ("x_v", "y_v")))


def _covariance(vectors):
    n = len(vectors)
    if n < 2:
        return None
    means = [statistics.fmean(v[i] for v in vectors) for i in (0, 1)]
    return [[sum((v[i] - means[i]) * (v[j] - means[j]) for v in vectors) / (n * (n - 1))
             for j in (0, 1)] for i in (0, 1)]


def _time(row):
    value = row.get("timestamps.lockin", row.get("captured_at_utc", row.get("finished_at_utc")))
    if not isinstance(value, str):
        raise ValueError("Power reference needs recorded capture UTC")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("Power reference needs timezone-aware capture UTC")
    return value, parsed


def subtract_power_reference(rows, *, mode="power_lowest", reference_power_w=None):
    """Return copied light rows with separate power_delta columns and provenance.

    Missing/ambiguous references invalidate only the affected channel and never
    substitute a second-lowest power. Existing matched dark deltas are retained.
    The shared archived analysis_context object is read-only and is not copied
    per sample, avoiding multiplication of a large plan by its sample count.
    """
    if mode not in ("power_lowest", "power_selected"):
        raise ValueError("Power reference mode must be power_lowest or power_selected")
    if mode == "power_selected" and (not finite(reference_power_w) or reference_power_w <= 0):
        raise ValueError("Selected reference power must be finite and positive, in W")
    lights = [r for r in rows if _light(r)]
    groups, errors = {}, {}
    for row in lights:
        try:
            groups.setdefault(_group(row), []).append(row)
        except (ValueError, TypeError, KeyError) as exc:
            errors[id(row)] = str(exc)
    # Compute each baseline only once per physical/visit/formal channel context.
    baselines, formal_cache, plan_cache = {}, {}, {}
    def formal(row, short, harmonic):
        key = id(row), short, harmonic
        if key not in formal_cache:
            try:
                formal_cache[key] = _formal(row, short, harmonic)
            except (ValueError, TypeError, KeyError) as exc:
                formal_cache[key] = exc
        value = formal_cache[key]
        if isinstance(value, Exception):
            raise ValueError(str(value))
        return value
    output = []
    for row in lights:
        result = deepcopy({k: v for k, v in row.items() if k != "analysis_context"})
        if "analysis_context" in row:
            result["analysis_context"] = row["analysis_context"]
        for key in list(result):
            if _DELTA.fullmatch(key):
                del result[key]
        assessment = {"mode": mode, "method": "(light X+iY) - mean(reference-power light X+iY)",
                      "reference_power_w": reference_power_w, "channels": {}, "valid_for_analysis": True,
                      "uncertainty_assumption": "independent acquisitions except explicitly shared reference samples; unmeasured drift excluded"}
        channels = sorted({(match[1], int(match[2])) for key in row if (match := _CHANNEL.fullmatch(key))})
        for short, harmonic in channels:
            prefix = f"measured.lockin_{short}_h{harmonic}_"
            channel = {"valid_for_analysis": False, "difference_emitted": False, "phase_defined": False}
            assessment["channels"][f"{short}:h{harmonic}"] = channel
            try:
                if id(row) in errors:
                    raise ValueError(errors[id(row)])
                group = _group(row)
                if group not in plan_cache:
                    try:
                        plan_cache[group] = _planned_reference(row, mode, reference_power_w)
                    except (ValueError, KeyError, TypeError) as exc:
                        plan_cache[group] = exc
                power = plan_cache[group]
                if isinstance(power, Exception):
                    raise ValueError(str(power))
                channel["reference_power_w"] = power
                fingerprint, target_ranges = formal(row, short, harmonic)
                baseline_key = group, power, short, harmonic, fingerprint
                if baseline_key not in baselines:
                    candidates = [r for r in groups[group] if _same_power(r.get("requested." + _POWER), power)]
                    visits = {(r.get("condition_id"), r.get("axes.optical.index")) for r in candidates}
                    if len(visits) > 1:
                        baselines[baseline_key] = ValueError("Ambiguous reference power: more than one optical visit")
                    else:
                        usable, rejected, scales = [], [], []
                        for candidate in candidates:
                            try:
                                signature, ranges = formal(candidate, short, harmonic)
                                if signature != fingerprint:
                                    raise ValueError("Reference formal settings differ beyond sensitivity")
                                if not _good(candidate, prefix):
                                    raise ValueError("Reference sample rejected, manually excluded or channel quality invalid/unknown")
                                usable.append(candidate)
                                scales.append({"sample_id": observation_id(candidate), "full_scales_v": ranges})
                            except (ValueError, KeyError, TypeError) as exc:
                                rejected.append({"sample_id": observation_id(candidate), "reason": str(exc)})
                        ids = [observation_id(r) for r in usable]
                        tokens = [_id(r) for r in usable]
                        if len(set(tokens)) != len(tokens):
                            baselines[baseline_key] = ValueError("Duplicate reference sample identity")
                        else:
                            vectors = [(r[prefix + "x_v"], r[prefix + "y_v"]) for r in usable]
                            times = [_time(r)[0] for r in usable]
                            measured_powers = [r["measured.optical_power_w"] for r in usable if finite(r.get("measured.optical_power_w"))]
                            baselines[baseline_key] = {"reference_id": hashlib.sha256(_json([baseline_key, ids]).encode()).hexdigest(),
                                "reference_sample_ids": ids, "reference_count": len(usable),
                                "reference_candidate_count": len(candidates), "reference_rejections": rejected,
                                "reference_mean_x_v": statistics.fmean(v[0] for v in vectors) if vectors else None,
                                "reference_mean_y_v": statistics.fmean(v[1] for v in vectors) if vectors else None,
                                "reference_covariance_mean_xy_v2": _covariance(vectors),
                                "reference_full_scales_v": scales, "reference_times_utc": times,
                                "reference_samples": [{"sample_id": ids[i], "raw_x_v": vectors[i][0],
                                    "raw_y_v": vectors[i][1], "captured_at_utc": times[i],
                                    "confirmed_full_scales_v": scales[i]["full_scales_v"],
                                    **({"measured_optical_power_w": r["measured.optical_power_w"]}
                                       if finite(r.get("measured.optical_power_w")) else {})}
                                    for i, r in enumerate(usable)],
                                "reference_measured_power_mean_w": statistics.fmean(measured_powers) if measured_powers else None,
                                "reference_measured_power_sd_w": statistics.stdev(measured_powers) if len(measured_powers) > 1 else None}
                baseline = baselines[baseline_key]
                if isinstance(baseline, Exception):
                    raise ValueError(str(baseline))
                channel.update(deepcopy(baseline))
                channel["target_full_scales_v"] = target_ranges
                channel["target_is_reference"] = observation_id(row) in baseline["reference_sample_ids"]
                if not baseline["reference_count"]:
                    raise ValueError("No qualified reference samples at the required nominal power")
                if not _good(row, prefix):
                    raise ValueError("Target sample rejected, manually excluded or channel quality invalid/unknown")
                stamp, target_time = _time(row)
                ages = [(target_time - datetime.fromisoformat(t.replace("Z", "+00:00"))).total_seconds()
                        for t in baseline["reference_times_utc"]]
                channel.update(target_time_utc=stamp, reference_age_s=max(ages),
                               reference_age_min_s=min(ages), reference_max_distance_s=max(map(abs, ages)))
                x, y = (row[prefix + "x_v"] - baseline["reference_mean_x_v"],
                        row[prefix + "y_v"] - baseline["reference_mean_y_v"])
                result.update({prefix + "power_delta_x_v": x, prefix + "power_delta_y_v": y,
                               prefix + "power_delta_amplitude_v": math.hypot(x, y)})
                if x != 0 or y != 0:
                    result[prefix + "power_delta_phase_deg"] = math.degrees(math.atan2(y, x))
                channel.update(valid_for_analysis=True, difference_emitted=True, phase_defined=x != 0 or y != 0)
            except (ValueError, TypeError, KeyError, OverflowError) as exc:
                channel["reason"] = str(exc)
            assessment["valid_for_analysis"] &= channel["valid_for_analysis"]
        if not channels:
            assessment.update(valid_for_analysis=False, reason="No formal light X/Y channels")
        if not isinstance(result.get("status.lockin"), dict):
            result["status.lockin"] = {}
        result["status.lockin"]["power_reference"] = assessment
        output.append(result)
    return tuple(output)


def _summary(rows, column):
    match = _DELTA.fullmatch(column)
    if not match or not rows:
        return None
    short, harmonic, metric = match.groups()
    prefix = f"measured.lockin_{short}_h{harmonic}_power_delta_"
    channels = [(r.get("status.lockin") or {}).get("power_reference", {}).get("channels", {}).get(f"{short}:h{harmonic}", {}) for r in rows]
    if any(c.get("valid_for_analysis") is not True for c in channels):
        return None
    vectors = [(r.get(prefix + "x_v"), r.get(prefix + "y_v")) for r in rows]
    if any(not all(map(finite, v)) for v in vectors):
        return None
    identities = {c.get("reference_id") for c in channels}
    if len(identities) != 1 or None in identities:
        return None
    first = channels[0]
    if any(c.get("reference_covariance_mean_xy_v2") != first.get("reference_covariance_mean_xy_v2")
           or c.get("reference_sample_ids") != first.get("reference_sample_ids") for c in channels):
        raise ValueError("Conflicting provenance for one power reference")
    ids = [_id(r) for r in rows]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate target sample identity")
    reference_ids = {_json(i) for i in first["reference_sample_ids"]}
    overlap = len(set(ids) & reference_ids)
    n, nref = len(rows), len(reference_ids)
    complete_self = n == nref == overlap
    mean = [0., 0.] if complete_self else [statistics.fmean(v[i] for v in vectors) for i in (0, 1)]
    ref_cov = first.get("reference_covariance_mean_xy_v2")
    own_cov = _covariance(vectors)
    if complete_self:
        covariance = [[0., 0.], [0., 0.]]
    elif overlap == n and ref_cov is not None:
        covariance = [[v * (nref / n - 1) for v in line] for line in ref_cov]
    elif overlap == 0 and ref_cov is not None and own_cov is not None:
        covariance = [[own_cov[i][j] + ref_cov[i][j] for j in (0, 1)] for i in (0, 1)]
    else:
        covariance = None
    return metric, vectors, mean, covariance, ref_cov, complete_self, overlap


def _gradient(mean, metric):
    x, y = mean
    if metric == "x_v": return (1., 0.)
    if metric == "y_v": return (0., 1.)
    if x == y == 0: return None
    if metric == "amplitude_v": return (x / math.hypot(x, y), y / math.hypot(x, y))
    return (-y / (x*x + y*y) * 180 / math.pi, x / (x*x + y*y) * 180 / math.pi)


def _variance(covariance, gradient):
    if covariance is None or gradient is None:
        return None
    return max(0., sum(gradient[i] * covariance[i][j] * gradient[j] for i in (0, 1) for j in (0, 1)))


def power_reference_mean_variance(rows, column):
    """Shared reference-only term for independent targets; no division by nT.

    Overlapping reference/target acquisitions require covariance cancellation;
    use power_reference_statistics for that complete uncertainty instead.
    """
    info = _summary(rows, column)
    if info is None:
        return None
    metric, _, mean, _, reference, _, overlap = info
    return _variance(reference, _gradient(mean, metric)) if not overlap else None


def power_reference_statistics(rows, column):
    """Complex mean then R/phase, sample spread, and correlated mean uncertainty."""
    info = _summary(rows, column)
    if info is None:
        return {"mean": None, "sd": None, "sem": None, "reference_mean_variance": None,
                "uncertainty_reason": "Missing valid power-reference provenance or X/Y vectors"}
    metric, vectors, mean, covariance, ref_cov, complete_self, overlap = info
    if metric in ("x_v", "y_v"):
        index = 0 if metric == "x_v" else 1
        value, values = mean[index], [v[index] for v in vectors]
    elif metric == "amplitude_v":
        value, values = math.hypot(*mean), [math.hypot(*v) for v in vectors]
    else:
        value = math.degrees(math.atan2(mean[1], mean[0])) if mean != [0., 0.] else None
        values = [(math.degrees(math.atan2(v[1], v[0])) - value + 180) % 360 - 180
                  for v in vectors if v != (0, 0)] if value is not None else []
    sd = statistics.stdev(values) if len(values) > 1 else None
    gradient = _gradient(mean, metric)
    variance = (0. if complete_self and metric != "phase_deg" else _variance(covariance, gradient))
    return {"mean": value, "sd": sd, "sem": math.sqrt(variance) if variance is not None else None,
            "reference_mean_variance": _variance(ref_cov, gradient) if not overlap else None,
            "covariance_mean_xy_v2": covariance, "target_reference_overlap_count": overlap,
            "reference_self_complete": complete_self,
            "uncertainty_reason": None if variance is not None else
                "Undefined zero-vector phase/amplitude, insufficient repeats, or mixed overlapping target/reference samples"}
