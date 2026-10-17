"""Read-only dark subtraction for normalized Combination observations.

An independent dark run is operator-selected, never guessed from a name or
from a zero/missing power. Its optical OFF evidence and formal settings must
match. Reuse does not establish phase continuity or correct unmeasured drift.
"""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
import re
import statistics

from .analysis_observations import channel_quality, finite, observation_id

_CHANNEL = re.compile(r"measured\.lockin_(xx|xy)_h(\d+)_x_v$")


def _dark(row):
    source = ((row.get("status.optical") or {}).get("readback") or {}).get("nkt", {}).get("source", {})
    return (source.get("emission_state") == 0 or
            source.get("getLdOutputState") is False)


def _context(row, role, harmonic):
    provenance = row.get("analysis_context") or {}
    hardware = provenance.get("lockin")
    if not hardware:
        raise ValueError("Dark matching needs archived lock-in configuration, not today's TOML")
    formal = []
    for sample in (row.get("status.lockin") or {}).get("samples", ()):
        reading = (sample.get(role) or {}).get("reading", {})
        selected = reading.get("harmonic", (sample.get("harmonics_by_role") or {}).get(role, sample.get("harmonic")))
        if selected != harmonic:
            continue
        before = (sample.get("settings_before") or {}).get("roles", {}).get(role)
        after = (sample.get("settings_after") or {}).get("roles", {}).get(role)
        if not before or before != after or sample.get("settings_verified") is not True:
            raise ValueError("Missing or changed formal settings for dark matching")
        formal.append(before)
    if not formal:
        raise ValueError("No formal reference/phase/settings evidence for dark matching")
    # Same serialized channel hardware plus complete formal settings. Different
    # optical powers are intended; different electrical/environment targets are not.
    # Output directories, notes and safety allowlist choices do not describe the
    # measured channel. Actual formal ranges/TC still match exactly below.
    selected_hardware = ({key:hardware.get(key) for key in
        (role, "sample_excitation_connected", "excitation_definition", "harmonics_by_role")}
        if role in hardware else hardware)
    chopper = ((row.get("status.optical") or {}).get("readback") or {}).get("chopper")
    chopper = ({key:chopper.get(key) for key in ("phase", "ref", "nharmonic", "dharmonic", "blade", "enable")}
               if isinstance(chopper, dict) else None)
    context = dict(hardware=selected_hardware, optical=provenance.get("optical"), chopper=chopper,
        conditions={k:v for k,v in row.items() if k.startswith("requested.") and
                    not k.startswith("requested.optical_")},
        formal=formal, simulated=row.get("simulated"))
    return json.dumps(context, sort_keys=True, allow_nan=False, default=str)


def subtract_dark(rows, *, mode="raw", dark_run_id="", phase_confirmed=False, max_age_s=600.0):
    if mode not in ("raw", "matched", "separate"):
        raise ValueError("Photoresponse mode must be raw, matched or separate")
    if mode == "raw":
        return tuple(rows)
    if mode == "matched":
        if not any((r.get("status.lockin") or {}).get("background") for r in rows):
            raise ValueError("No recorded matched dark background; select an independent dark run")
        return tuple(deepcopy(r) for r in rows)
    if not phase_confirmed:
        raise ValueError("Confirm unchanged wiring and continuous reference/phase for the independent dark run")
    if not finite(max_age_s) or max_age_s <= 0:
        raise ValueError("Dark maximum age must be finite and positive")
    dark_rows = [r for r in rows if r.get("run_id") == dark_run_id and not r.get("_manual_excluded")]
    if not dark_rows or len({r.get("source_path") for r in dark_rows}) != 1:
        raise ValueError("Select exactly one loaded dark run from one source")
    if not all(_dark(r) for r in dark_rows):
        raise ValueError("Selected dark run lacks recorded laser-OFF evidence")
    light_rows = [r for r in rows if r.get("run_id") != dark_run_id]
    if not light_rows:
        raise ValueError("Select at least one light run as well as the dark run")
    output = []
    dark_contexts = {}
    for row in light_rows:
        result = deepcopy(row)
        # Never leave stale deltas from a different selected background.
        for key in list(result):
            if re.match(r"measured\.lockin_(xx|xy)_h\d+_delta_", key):
                del result[key]
        assessment = dict(method="independent run: (light X+iY) - mean(dark X+iY)",
            dark_run_id=dark_run_id, phase_continuity_operator_confirmed=True,
            phase_evidence="operator declaration; archived formal settings are checked, missing phase readback is not inferred",
            max_age_s=max_age_s, channels={}, valid_for_analysis=True)
        for key in row:
            match = _CHANNEL.fullmatch(key)
            if not match:
                continue
            short, harmonic = match[1], int(match[2])
            prefix = f"measured.lockin_{short}_h{harmonic}_"
            channel = dict(valid_for_analysis=False, difference_emitted=False, phase_defined=False)
            assessment["channels"][f"{short}:h{harmonic}"] = channel
            try:
                fingerprint = _context(row, "lockin_"+short, harmonic)
                cache_key = short, harmonic
                if cache_key not in dark_contexts:
                    groups, rejected = {}, []
                    for candidate in dark_rows:
                        try:
                            key_context = _context(candidate, "lockin_"+short, harmonic)
                            groups.setdefault(key_context, []).append(candidate)
                        except (ValueError, KeyError, TypeError) as exc:
                            rejected.append({"sample_id": observation_id(candidate), "reason": str(exc)})
                    dark_contexts[cache_key] = groups, rejected
                groups, rejected = dark_contexts[cache_key]
                matched = groups.get(fingerprint, [])
                usable = [r for r in matched if r.get("accepted") is True and
                    all(channel_quality(r, prefix+m)[0] == "clear" and finite(r.get(prefix+m))
                        for m in ("x_v", "y_v"))]
                channel.update(dark_candidate_count=len(matched), dark_count=len(usable),
                               dark_excluded_count=len(matched)-len(usable),
                               dark_context_rejections=rejected,
                               dark_other_context_count=len(dark_rows)-len(matched)-len(rejected))
                if not usable:
                    raise ValueError("No qualified dark samples with matching conditions/settings")
                # Use the oldest sample: a long dark window cannot be made younger
                # merely by its final reading. Do not take a future dark implicitly.
                light_time = datetime.fromisoformat(row["finished_at_utc"])
                ages = [(light_time-datetime.fromisoformat(r["finished_at_utc"])).total_seconds() for r in usable]
                channel["age_s"] = max(ages)
                if min(ages) < 0 or max(ages) > max_age_s:
                    raise ValueError("Independent dark is newer than light or exceeds maximum age")
                vectors = [(r[prefix+"x_v"],r[prefix+"y_v"]) for r in usable]
                n = len(vectors)
                means = [statistics.fmean(v[i] for v in vectors) for i in (0,1)]
                cov = ([[sum((v[i]-means[i])*(v[j]-means[j]) for v in vectors)/(n*(n-1))
                         for j in (0,1)] for i in (0,1)] if n > 1 else None)
                ids = [observation_id(r) for r in usable]
                baseline_id = hashlib.sha256(json.dumps(ids, sort_keys=True).encode()).hexdigest()
                channel.update(baseline_id=baseline_id, dark_sample_ids=ids,
                    dark_mean_x_v=means[0], dark_mean_y_v=means[1],
                    dark_covariance_mean_xy_v2=cov)
                if not all(channel_quality(row, prefix+m)[0] == "clear" and finite(row.get(prefix+m))
                           for m in ("x_v","y_v")):
                    raise ValueError("Light channel is invalid or missing")
                x,y = row[prefix+"x_v"]-means[0],row[prefix+"y_v"]-means[1]
                result.update({prefix+"delta_x_v":x,prefix+"delta_y_v":y,
                               prefix+"delta_amplitude_v":math.hypot(x,y)})
                if x != 0 or y != 0:
                    result[prefix+"delta_phase_deg"] = math.degrees(math.atan2(y,x))
                channel.update(valid_for_analysis=True,difference_emitted=True,phase_defined=x!=0 or y!=0)
            except (ValueError, KeyError, TypeError, OverflowError) as exc:
                channel["reason"] = str(exc)
            assessment["valid_for_analysis"] &= channel["valid_for_analysis"]
        result.setdefault("status.lockin", {})["background"] = assessment
        output.append(result)
    return tuple(output)


def dark_mean_variance(rows, column):
    """Shared dark contribution to a mean; never divide common error by light n.

    X/Y are exact linear propagation. Magnitude/phase use first-order gradients;
    at zero amplitude these uncertainties are undefined. Samples are assumed
    independent within dark acquisition; unmeasured temporal drift is excluded.
    """
    match = re.fullmatch(r"measured\.lockin_(xx|xy)_h(\d+)_delta_(x_v|y_v|amplitude_v|phase_deg)", column)
    if not match or not rows:
        return None
    short,h,metric = match.groups()
    groups = {}
    for row in rows:
        bg = (row.get("status.lockin") or {}).get("background", {})
        channel = bg.get("channels", {}).get(f"{short}:h{h}", {})
        cov = channel.get("dark_covariance_mean_xy_v2")
        identity = channel.get("baseline_id", bg.get("baseline_id"))
        if (not identity or not isinstance(cov, (list, tuple)) or len(cov) != 2
                or any(not isinstance(c, (list, tuple)) or len(c) != 2 for c in cov)
                or any(not finite(v) for c in cov for v in c)):
            return None
        prefix = f"measured.lockin_{short}_h{h}_delta_"
        x,y = row.get(prefix+"x_v"),row.get(prefix+"y_v")
        if not finite(x) or not finite(y):
            return None
        if metric == "x_v": gradient = (1,0)
        elif metric == "y_v": gradient = (0,1)
        elif x == y == 0: return None
        elif metric == "amplitude_v": gradient = (x/math.hypot(x,y), y/math.hypot(x,y))
        else: gradient = (-y/(x*x+y*y)*180/math.pi, x/(x*x+y*y)*180/math.pi)
        old = groups.setdefault(identity, [cov, [0.,0.]])
        if old[0] != cov:
            raise ValueError("Conflicting covariance for one dark baseline")
        for i in (0,1): old[1][i] += gradient[i]/len(rows)
    return sum(sum(g[i]*cov[i][j]*g[j] for i in (0,1) for j in (0,1))
               for cov,g in groups.values())
