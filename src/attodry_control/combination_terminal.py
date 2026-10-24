"""Compact plain-text views; structured SQLite/JSON audit stays complete."""
from __future__ import annotations

import json
import math
import shutil
import textwrap
from datetime import datetime, timezone


def _number(value, unit=""):
    if value == 0:
        return "0" + (" " + unit if unit else "")
    for factor, prefix in ((1e9, "G"), (1e6, "M"), (1e3, "k"), (1, ""),
                           (1e-3, "m"), (1e-6, "u"), (1e-9, "n"), (1e-12, "p")):
        if abs(value) >= factor:
            return f"{value / factor:.6g}" + (" " + prefix + unit if unit else "")
    return f"{value:.6g}" + (" " + unit if unit else "")


def _table(headers, rows, width):
    rows = [[str(c) for c in row] for row in [headers, *rows]]
    widths = [max(len(row[i]) for row in rows) for i in range(len(headers))]
    available = max(len(headers) * 5, width - 3 * (len(headers) - 1))
    while sum(widths) > available:
        i = max(range(len(widths)), key=widths.__getitem__)
        widths[i] -= 1
    result = []
    for row in rows:
        wrapped = [textwrap.wrap(c, width=w, replace_whitespace=False) or [""]
                   for c, w in zip(row, widths)]
        for line in range(max(map(len, wrapped))):
            result.append(" | ".join((c[line] if line < len(c) else "").ljust(w)
                                    for c, w in zip(wrapped, widths)).rstrip())
    return result


def _coordinate(key, values, route=None):
    names = {"lockin_excitation_v_rms": ("U", "Vrms"),
             "lockin_frequency_hz": ("f", "Hz"),
             "field_x_t": ("Bx", "T"), "field_z_t": ("Bz", "T"),
             "temperature_k": ("T", "K")}
    names.update({"optical_wavelength_nm": ("lambda", "nm"),
                  **{"positioner_" + role + "_um": (role.upper(), "um") for role in "xyz"},
                  **{"positioner_" + role + "_offset_um": ("d" + role.upper() + " (frozen origin)", "um") for role in "xyz"},
                  "rotator_1_angle_deg": ("Rotator 1 angle", "deg"),
                  "rotator_2_angle_deg": ("Rotator 2 angle", "deg"),
                  "optical_bandwidth_nm": ("bandwidth", "nm"),
                  "optical_source_level_pct": ("source", "%"),
                  "optical_current_a": ("LD current", "A"),
                  "optical_nd_pct": ("ND", "%"),
                  "optical_target_power_w": ("power target", "W"),
                  "optical_pulse_picker_ratio": ("pulse picker", "")})
    name, unit = names.get(key, (key.rsplit("_", 1)[0], "V" if key.endswith("_v") else "A"))
    count = values["count"] if isinstance(values, dict) else len(values)
    if count > 4:
        minimum, maximum = ((values["min"], values["max"]) if isinstance(values, dict)
                            else (min(values), max(values)))
        if route:
            desc = " -> ".join(_number(v, unit) for v in route)
        else:
            desc = f"{_number(minimum, unit)} .. {_number(maximum, unit)}"
        return f"{name}: {desc} ({count} distinct; ordered grid)"
    desc = name + ": " + " -> ".join(_number(v, unit) for v in (route or values))
    return desc + (f" ({count} distinct; ordered grid)" if route and route != values else "")


def _cleanup_rows(cleanup):
    rows = []
    for action in cleanup.get("actions", []):
        result = action.get("result", {})
        # Hardware fault markers can coexist with successful individual resets.
        verified = result.get("verified")
        if verified is None and "manual_verification_required" in result:
            verified = not result["manual_verification_required"]
        rows.append((action.get("module", "?"),
                     "verified" if verified is True else "unverified" if verified is False else "unknown",
                     "yes" if action.get("failure_requires_manual_review")
                        or result.get("manual_verification_required") else "no",
                     result.get("error") or "; ".join(result.get("errors", []))
                        or ("not opened" if result.get("attempted") is False else "-")))
    return rows


def launch_text(summary, width=None):
    width = max(40, width or shutil.get_terminal_size((110, 30)).columns)
    lines = [f"REAL combination scan | {summary['run_name'] or 'combination'}",
             "Order: " + " -> ".join(summary["outer_to_inner"]) +
             f" | Conditions: {summary['total_conditions']} | Groups/condition: {summary['samples_per_condition']}"
             + f" | Repeats: {summary['repeats']}"]
    axes = []
    for axis in summary["axes"]:
        for segment in axis["segments"]:
            axes.append((axis["module"], segment["segment"] + "/" + segment["direction"],
                         "; ".join(_coordinate(k, v, segment.get("routes", {}).get(k)) for k, v in segment["coordinates"].items()),
                         segment["points"]))
    lines += ["", *_table(("Axis", "Segment/direction", "Requested scan", "Points"), axes, width)]
    hardware = []
    positioner = summary.get("positioner")
    if positioner is not None:
        lines.append("Positioner: ANC350 RES | SDK 0/1/2 = X/Y/Z | " + positioner["mode"] +
            " | Z: " + positioner["z_mode"] + " | frame: " + positioner["coordinate_frame"])
        lines.append("Positioner arrival: " + positioner.get("arrival_policy", "stable_hold"))
        lines.append("Motion order: " + " -> ".join(positioner["move_order"]) +
            " | finish: " + positioner["finish"] + " | origin frozen once after fresh stable readback")
        for role, settings in positioner["axes"].items():
            lines.append(f"{role.upper()}: {settings['min_position_um']:g}..{settings['max_position_um']:g} um; "
                f"max move {settings['max_move_um']:g} um; tolerance {settings['position_tolerance_um']:g} um")
        if positioner.get("focus_plane"):
            lines.append("Focus plane: " + positioner["focus_plane"]["calibration_sha256"] +
                " | outside calibration convex hull: reject")
    for role, settings in summary.get("rotator", {}).items():
        zero = settings.get("optical_zero_deg")
        lines.append(f"{role}/{settings['label']}: {settings['controller']} + {settings['stage_model']} | "
                     f"Home: {settings['home_policy']} | Mechanical angle (deg) | Optical zero: "
                     + ("uncalibrated" if zero is None else f"{zero:g} deg"))
    optical = summary.get("optical")
    if optical is not None:
        lines.append("Laser source: " + optical["scan"].get("laser_source", "nkt"))
        lines.append("Unreachable optical target: " + summary.get("unreachable_target_policy", "abort"))
        chopper = optical.get("chopper")
        if chopper:
            lines.append("Chopper: " + chopper["model"] + " / " + chopper["backend"] + " | " +
                chopper["reference_source"] + " reference | " + chopper["blade"] + " " +
                chopper["reference_track"] + f" | N/D={chopper['harmonic_n']}/{chopper['harmonic_d']}" +
                " | Phase lock: operator declaration; no remote PLL query")
            if chopper.get("single_opening_per_rotation"):
                lines.append("Chopper beam: operator-declared one opening/rotation; "
                             "frequency derived from the unmodified outer sensor track")
        background = optical.get("background")
        if background and background.get("enabled"):
            lines.append(f"Dark background: {background.get('capture_mode', 'per_condition')}; "
                         f"{background['samples']} fresh readings/baseline; "
                         f"max age {background['max_age_s']:g} s; complex X+iY subtraction")
        if optical["scan"].get("max_power_retries", 0):
            lines.append(f"Power recovery: {optical['scan']['max_power_retries']} whole-window retries; "
                         f"{optical['scan']['power_retry_timeout_s']:g} s shared budget")
    if optical is not None and optical["scan"].get("feedback"):
        grid = optical.get("point_grid")
        if grid:
            lines.append(f"Optical power grid: {grid['target_mapping']} | Input rows: "
                         f"{grid['input_point_count']} | Expanded points: {grid['expanded_point_count']}")
        initial_mode = optical["scan"]["feedback"].get("initial_current_mode", "point")
        lines.append(f"Optical initial current: {initial_mode}" +
                     (" | Previous qualified readback within each input row; reset at new row"
                      if initial_mode == "previous" else " | Each point still requires power qualification"))
        lines.append("Optical target deviation: " + optical["scan"].get("target_deviation_policy", "abort") +
                     " | Target tolerance applies to initial tuning; analysis uses measured power")
        qualification = optical["scan"]["feedback"].get("qualification", "stable_window")
        lines.append("Optical power qualification: " + qualification +
                     (" | One fresh PM mean reaches target; variation is unqualified; no hold/window wait"
                      if qualification == "mean_target_only" else " | Rolling power window and hold"))
    for role, settings in summary.get("smu", {}).items():
        hardware.append((role, settings["source_mode"], "AUTO", "-",
            _number(settings["max_abs_current_a"], "A") + " / " +
            _number(settings["max_abs_voltage_v"], "V") + " max"))
        if settings.get("ramp"):
            ramp = settings["ramp"]
            lines.append(f"{role}: ramp <= {_number(ramp['max_step_v'], 'V')} per step / "
                         f"{_number(ramp['step_interval_s'], 's')} minimum interval; "
                         f"timeout {_number(ramp['timeout_s'], 's')}; "
                         f"actual V tolerance {_number(ramp['readback_tolerance_v'], 'V')}")
    if summary.get("illumination_policy") == "continuous_gate_scan":
        lines.append("Illumination: continuous_gate_scan | Optical qualification before gate loop; "
                     "light held during gate steps; fixed lock-in excitation reused")
    lockin = summary.get("lockin", {})
    for role, settings in lockin.get("roles", {}).items():
        harmonics = lockin["harmonics_by_role"].get(role.removeprefix("lockin_"), [])
        hardware.append((role + (" (" + settings["model"] + ")" if "model" in settings else ""),
            ",".join(f"h{h}" for h in harmonics) or "no acquisition",
            settings["sensitivity_mode"] + " " + _number(settings["sensitivity_full_scale_v"], "V"),
            settings["reserve_mode"], f"TC {_number(settings['time_constant_s'], 's')}"))
        if settings["sensitivity_mode"] == "bounded_auto":
            hardware.append((role, "AUTO limits",
                _number(settings["autorange_min_full_scale_v"], "V") + " .. " +
                _number(settings["autorange_max_full_scale_v"], "V"), "-", "-"))
        for override in settings["harmonic_settings"]:
            hardware.append((role, f"h{override['harmonic']} override",
                _number(override["sensitivity_full_scale_v"], "V"),
                getattr(override["reserve_mode"], "value", override["reserve_mode"]),
                getattr(override["sensitivity_mode"], "value", override["sensitivity_mode"])))
            if override["sensitivity_mode"] == "bounded_auto":
                minimum = override.get("autorange_min_full_scale_v") or settings["autorange_min_full_scale_v"]
                maximum = override.get("autorange_max_full_scale_v") or settings["autorange_max_full_scale_v"]
                if minimum is not None and maximum is not None:
                    hardware.append((role, f"h{override['harmonic']} AUTO limits",
                        _number(minimum, "V") + " .. " + _number(maximum, "V"), "-", "-"))
    ranges = summary.get("lockin_range_rules")
    if ranges:
        lines.append(f"Lock-in range rules: {len(ranges['rules'])} | "
                     f"Resolved outer visits: {len(ranges['preview'])} | Full mapping: describe-hardware")
        for rule in ranges["rules"]:
            scales = {role: rule[role + "_full_scale_v"] for role in
                      ("lockin_xx", "lockin_xy", "lockin_aux") if role + "_full_scale_v" in rule}
            lines.append("Range rule " + rule["name"] + ": " +
                         ", ".join(role + "=" + _number(value, "V") for role, value in scales.items()))
    if hardware:
        lines += ["", *_table(("Instrument", "Measurement", "Sensitivity", "Reserve", "Protection/timing"),
                              hardware, width)]
    if lockin:
        if lockin.get("reference_topology") == "internal_xx_xy":
            lines.append("Reference: XX internal -> TTL OUT -> XY REF IN" +
                         (" + AUX SR865A REF IN" if "lockin_aux" in lockin.get("roles", {}) else "") +
                         (" + chopper EXT REF INPUT" if optical and optical.get("chopper") else ""))
            lines.append("XX SINE OUT: " + ("connected to sample" if lockin.get("sample_excitation_connected", True)
                                           else "physically disconnected from sample; displayed voltage is instrument setting"))
            aux = lockin.get("roles", {}).get("lockin_aux")
            if aux:
                lines.append("AUX: " + aux["channel_label"] + " | external TTL / " + aux["external_reference_edge"] +
                             " | IRNG " + _number(aux["input_range_v_peak"], "V peak") +
                             " | REFZ " + _number(aux["reference_input_impedance_ohm"], "ohm") +
                             " | SINE OUT disconnected | common settle, sequential reads")
        lines.append("Lock-in: " + lockin["mode"] + " | Inner order: " +
                     " -> ".join(lockin["internal_order"]) +
                     f" | Settle: {lockin['settle_time_constants']} TC | Overload: {lockin['overload_policy']}")
        # Explicitly expose segment overrides, including f x U precedence.
        for name, points in lockin.get("segment_sensitivity_overrides", {}).items():
            distinct = list(dict.fromkeys(
                (p["segment_index"], p["xx_full_scale_v"], p["xy_full_scale_v"])
                for p in points))
            for segment, xx, xy in distinct:
                lines.append(f"{name} segment {segment}: XX " +
                    (_number(xx, "V") if xx is not None else "baseline") + " | XY " +
                    (_number(xy, "V") if xy is not None else "baseline"))
        if any(lockin.get("segment_sensitivity_overrides", {}).values()):
            lines.append("Segment SENS precedence: excitation > frequency > baseline; harmonic settings apply.")
        if lockin["skipped_harmonics_by_frequency"]:
            lines.append("Skipped harmonics: " + json.dumps(lockin["skipped_harmonics_by_frequency"]))
        if lockin.get("reference_topology") in {"pem_xx_xy", "pem_xy_xx_sine"}:
            source = lockin["source"]
            reference_path = ("PEM -> XY -> XX (XY SINE OUT)" if lockin["reference_topology"] == "pem_xy_xx_sine" else "PEM -> XX -> XY")
            lines.append("Reference: " + reference_path + " | Allowed: " +
                " .. ".join(_number(v, "Hz") for v in lockin["reference_bounds_hz"]))
            lines.append(f"PEM reference: {lockin.get('pem_reference_harmonic', 1)}f (hardware selected)"
                         " | Lock-in h1/h2 are relative to this external reference")
            for role, harmonics in lockin.get("pem_harmonics_by_role", {}).items():
                lines.append("lockin_" + role + " detection: " + ", ".join(
                    f"h{h} = PEM {pem_h}f (~{_number(frequency, 'Hz')})"
                    for h, pem_h, frequency in zip(lockin["harmonics_by_role"][role], harmonics,
                                                   lockin["expected_detection_hz_by_role"][role])))
            lines.append("Reference transient: " + lockin.get("reference_transient_policy", "abort") +
                f" | Recovery: {lockin.get('reference_recovery_timeout_s', 45):g} s total, "
                f"{lockin.get('reference_recovery_consecutive_good', 3)} consecutive good polls at 1 s")
            lines.append("XX source: " + source["wiring"] + " / " + source["load"] + " | " +
                source["amplitude_definition"] + " | DC " + source["dc_mode"] +
                " " + _number(source["dc_offset_v"], "V") + " | Cleanup <= " +
                _number(lockin["cleanup_source_voltage_v"], "V"))
            if lockin.get("reference_output") is not None:
                refout = lockin["reference_output"]
                lines.append("XY reference only: preserved " + _number(refout["amplitude_v_rms"], "V RMS setting") +
                    " / " + refout["wiring"] + " / " + refout["load"] + " -> XX REF IN; disconnected from sample")
            for role, settings in lockin["roles"].items():
                if settings["model"] == "SR865A":
                    lines.append(role + ": IRNG " + _number(settings["input_range_v_peak"], "V peak") +
                        " | REFZ " + _number(settings["reference_input_impedance_ohm"], "ohm") +
                        " | Sync " + str(settings["sync_output_mode"]))
    if "field_transition_policy" in summary:
        policy = summary.get("field_readback_policy")
        command_scoped = policy is not None and policy.get("command_scoped", False)
        if command_scoped:
            limits = policy["limits"]
            lines.append(f"Field commands: |Bx| <= {limits['hardware_x_max_t']:g} T"
                         f" | |Bz| <= {limits['hardware_z_max_t']:g} T"
                         f" | Both axes nonzero: |B| <= {limits['experiment_vector_max_t']:g} T"
                         f" | Transition: {summary['field_transition_policy']}")
            lines.append("Magnetic quality after qualification: " + summary.get("magnetic_deviation_policy", "abort"))
        elif policy is not None and policy["mode"] != "vector":
            axis = "X" if policy["mode"] == "single_x" else "Z"
            nominal = policy["limits"][f"hardware_{axis.lower()}_max_t"]
            lines.append(f"Field target: single {axis} | |B{axis.lower()}| <= {nominal:g} T"
                         f" | Inactive target: 0 T | Transition: {summary['field_transition_policy']}")
        else:
            lines.append(f"Field: resultant <= {summary['field_resultant_limit_t']} T | Transition: {summary['field_transition_policy']}")
        if policy is not None:
            axes = policy["axis_readback_limits_t"]
            vector = policy["vector_readback_limit_t"]
            lines.append(f"Field readback: {policy['mode']} | |Bx| <= {axes['x']:.7g} T"
                         f" | |Bz| <= {axes['z']:.7g} T"
                         + (f" | Both commanded axes nonzero: |B| <= {vector:.7g} T" if command_scoped
                            else f" | |B| <= {vector:.7g} T" if vector is not None else "")
                         + " | Nominal targets unchanged")
            if command_scoped:
                inactive = policy['inactive_axis_readback_limits_t']
                lines.append(f"Inactive-axis hard residual: |Bx| <= {inactive['x']:.7g} T"
                             f" or |Bz| <= {inactive['z']:.7g} T, according to command context")
            if "readback_tolerance_t" in policy:
                tolerance = _number(policy["readback_tolerance_t"], "T")
                lines.append(f"Field qualification: each axis error <= {tolerance}"
                             f" | Verified zero: |B| <= {tolerance} + stable dwell"
                             + (f" | Setpoint ACK: {_number(summary['field_setpoint_ack_tolerance_t'], 'T')}"
                                if "field_setpoint_ack_tolerance_t" in summary else ""))
    finish_names = {"zero_disable": "zero/OFF", "4mV_restore_ranges_h1": "4 mV/h1; restore SENS/Reserve",
                    "4mV_h1_restore_baseline_frequency_ranges_reserve":
                        "4 mV/h1; restore frequency/SENS/Reserve",
                    "normal_hold_failure_disable": "normal hold; disable control on failure"}
    finish = []
    for module in summary["outer_to_inner"]:
        policy = summary["cleanup"][module]
        text = finish_names.get(policy, policy)
        if module == "magnetic" and policy == "hold":
            last = next(a["last_target"] for a in summary["axes"] if a["module"] == module)
            text += " at " + ", ".join(_coordinate(k, [v]) for k, v in last.items())
        finish.append(module + "=" + text)
    lines.append("Normal finish (requested): " + " | ".join(finish))
    if "magnetic" in summary["outer_to_inner"]:
        lines.append("Field on failure: " + summary["cleanup"]["magnetic_failure"])
    if summary["note"]:
        lines.append("Note: " + summary["note"])
    lines += ["Run ID: " + summary["run_id"], "Database: " + summary["database"],
              "Source: " + summary.get("source_path", "unrecorded")
              + " | version " + summary.get("source_version", "unrecorded")
              + " | CLI SHA256 " + summary.get("source_entry_sha256", "unrecorded"),
              "Config: " + summary["config"],
              "Authorization: run command | XY disconnection declared in TOML when selected"
              + (" | configured optical operation authorized by run command" if "optical" in summary["outer_to_inner"] else "")]
    return "\n".join(part for line in lines for part in (textwrap.wrap(line, width=width,
        replace_whitespace=False, break_on_hyphens=False) or [""]))


def _monitor_targets(requested):
    parts = []
    for key, value in requested.items():
        if key == "temperature_k":
            parts.append(f"target {value:.6g} K")
        else:
            parts.append(_coordinate(key, [value]))
    return "; ".join(parts)


def _seconds_since(timestamp, now=None):
    if not timestamp:
        return None
    try:
        recorded = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if recorded.tzinfo is None:
            return None
        seconds = ((now or datetime.now(timezone.utc)) - recorded).total_seconds()
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 and math.isfinite(seconds) else None


def _duration(seconds):
    if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
        return "unknown"
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def _record_age(timestamp, now=None):
    seconds = _seconds_since(timestamp, now)
    return _duration(seconds) if seconds is not None else None


def _phase_name(phase):
    names = {"preflight": "Startup checks", "axis_apply": "Applying axis target",
             "axis_qualification": "Axis qualification", "dark_acquisition": "Dark acquisition",
             "optical_tuning": "Optical power qualification", "lockin_settling": "Lock-in settling / sampling interval",
             "formal_sampling": "Formal sampling", "condition_complete": "Condition complete",
             "cleanup": "Cleanup"}
    name = names.get(phase.get("phase"), phase.get("phase") or "Unavailable")
    if phase.get("module"):
        name = phase["module"] + " -> " + name
    if phase.get("sample_index") is not None:
        name += f" | sample {phase['sample_index'] + 1}"
    return name


def _monitor_progress_lines(snapshot, now=None):
    lines = []
    progress = snapshot.get("progress") or {}
    axes = progress.get("axes", [])
    if axes and axes[0].get("total_repeats", 1) > 1:
        repeat = axes[0].get("repeat_index")
        lines.append(f"Repeat: {repeat + 1 if repeat is not None else '-'}/{axes[0]['total_repeats']}")
    for axis in axes:
        module = axis["module"]
        name = {"temperature": "Temperature", "optical": "Optical", "lockin": "Lock-in",
                "magnetic": "Magnetic", "smu": "SMU", "rotator": "Rotator", "positioner": "Positioner"}.get(module, module)
        current = axis.get("current_point")
        lines.append(f"{name} point: {current if current is not None else '-'}/{axis['total_points']}"
                     + f" | Fully completed in scope: {axis['completed_points']}/{axis['total_points']}"
                     + (" | " + _monitor_targets(axis["requested"]) if axis.get("requested") else ""))
        if axis.get("scope"):
            lines.append("Point scope: " + ", ".join(
                f"{outer} point {index + 1}" for outer, index in axis["scope"].items()))
    condition = progress.get("current_condition")
    if condition:
        lines.append(f"Formal samples in attempt: {condition['formal_samples']}/"
                     f"{condition['expected_formal_samples']} | {condition['status']}"
                     + (" (provisional until accepted)" if condition["status"] == "started" else ""))
    phase = snapshot.get("phase") or {}
    if phase.get("status") == "terminal":
        lines.append("Current phase: " + str(phase.get("phase", snapshot["status"])) + " (terminal)")
        if phase.get("last_recorded"):
            lines.append("Last recorded phase: " + _phase_name(phase["last_recorded"]))
    elif phase.get("status") == "recorded":
        lines.append("Current phase (last recorded): " + _phase_name(phase))
        started = phase.get("started_at_utc")
        if started:
            age = _record_age(started, now)
            lines.append("Phase started: " + started + (" | Elapsed: " + age if age else ""))
        updated = phase.get("last_update_at_utc")
        if updated:
            age = _record_age(updated, now)
            lines.append("Last database event: " + updated + (" | Age: " + age if age else ""))
    elif phase:
        lines.append("Current phase: unavailable (no explicit phase record)")
    return lines


def environment_refresh_active(snapshot):
    """Only text ages need periodic refresh; JSON remains evidence based."""
    phase = snapshot.get("phase") or {}
    return (snapshot.get("status") == "active" and phase.get("status") == "recorded"
            and phase.get("module") in {"temperature", "magnetic"}
            and phase.get("phase") in {"axis_apply", "axis_qualification"})


def _environment_value(value, unit):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return f"{value:.6f} {unit}"
    return "unavailable"


def _field_value(field):
    if not isinstance(field, dict):
        return "unavailable"
    bx, bz = field.get("bx_t"), field.get("bz_t")
    if any(not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v)
           for v in (bx, bz)):
        return "unavailable"
    return f"Bx={bx:+.6f} T | Bz={bz:+.6f} T | |B|={math.hypot(bx, bz):.6f} T"


def _environment_lines(snapshot, now=None):
    environments = snapshot.get("environment") or {}
    if not environments:
        return []
    lines = ["Latest environment readback (stored; not a live query):"]
    phase = snapshot.get("phase") or {}
    for module, entry in environments.items():
        captured = entry.get("captured_at_utc")
        age = _record_age(captured, now)
        available = (snapshot.get("status") == "active" and entry.get("status") == "available"
                     and age is not None)
        if module == "temperature":
            lines.append("Latest temperature: " + (_environment_value(entry.get("actual_temperature_k"), "K")
                         if available else "unavailable"))
            lines.append("Target temperature: " + _environment_value(entry.get("target_temperature_k"), "K")
                         + " (archived plan)")
        elif module == "magnetic":
            lines.append("Latest field: " + (_field_value(entry.get("actual_field_t"))
                         if available else "unavailable"))
            lines.append("Target field: " + _field_value(entry.get("target_field_t")) + " (archived plan)")
            if available and entry.get("commanded_waypoint_t"):
                lines.append("Commanded waypoint: " + _field_value(entry["commanded_waypoint_t"]) + " (recorded)")
        else:
            continue
        if available:
            lines.append("Captured UTC: " + captured + " | Age: " + age)
            lines.append("Source condition: " + str(entry.get("source_condition_id", "unavailable"))
                         + " | Attempt: " + str(entry.get("source_attempt_index", "unavailable")))
        else:
            lines.append("Captured UTC: unavailable | Age: unknown")
            lines.append("Readback evidence: " + str(entry.get("reason") or "unavailable or invalid timestamp"))
        active = (environment_refresh_active(snapshot) and phase.get("module") == module)
        if active:
            started = entry.get("qualification_started_at_utc")
            label = "Qualification elapsed" if phase.get("phase") == "axis_qualification" else "Axis entry elapsed"
            if phase.get("phase") == "axis_apply":
                started = entry.get("visit_started_at_utc")
            lines.append(label + ": " + (_record_age(started, now) or "unknown")
                         + " (wall-clock estimate)")
            qualification = entry.get("qualification") or {}
            if qualification.get("acceptance_mode") is not None:
                lines.append("Acceptance mode: " + str(qualification["acceptance_mode"]))
            if module == "temperature" and qualification.get("tolerance") is not None:
                lines.append("Target tolerance: +/-" + _environment_value(qualification["tolerance"], "K"))
            if qualification.get("stable_range") is not None:
                lines.append("Stable range limit: " + _environment_value(qualification["stable_range"], "K" if module == "temperature" else "T"))
            lines.append("Stable window accumulated: " + _duration(qualification.get("stable_window_elapsed_s"))
                         + " / required " + _duration(qualification.get("dwell_s")))
            lines.append("Qualification timeout limit: " + _duration(qualification.get("wait_timeout_s")))
            lines.append("Qualification timeout remaining: " + _duration(qualification.get("timeout_remaining_s")))
            subwait = entry.get("subwait") or {}
            if module == "magnetic" and available and subwait:
                lines.append("Field subphase: " + str(subwait.get("phase", "unknown")) + " (recorded)")
                lines.append("Sub-wait elapsed at capture: " + _duration(subwait.get("elapsed_s")))
                lines.append("Sub-wait timeout remaining at capture: " + _duration(subwait.get("timeout_remaining_s")))
                lines.append("Sub-wait captured UTC: " + str(subwait.get("captured_at_utc", "unavailable")))
    status = snapshot.get("status")
    terminal = (snapshot.get("phase") or {}).get("status") == "terminal"
    continuation = "enabled" if status == "active" else "disabled (terminal)" if terminal else "unknown"
    lines.append("Automatic continuation: " + continuation)
    has_optical = any(axis.get("module") == "optical" for axis in
                      (snapshot.get("progress") or {}).get("axes", []))
    lines.append("Time to optical activation: " + ("unknown" if status == "active" and has_optical
                 else "not applicable (terminal)" if terminal else "not applicable (no optical axis)"
                 if not has_optical else "unknown"))
    lines.append("The display does not establish a pause or a safe optical-alignment window.")
    if status == "active":
        lines.append("After qualification the program continues automatically; process liveness is not established by these records.")
    return lines


def snapshot_text(snapshot, width=None, *, now=None):
    width = max(40, width or shutil.get_terminal_size((110, 30)).columns)
    lines = [f"Status: {snapshot['status']} | Accepted conditions: {snapshot.get('accepted_conditions', '?')}/"
             f"{snapshot.get('total_conditions', '?')} | Process liveness: {snapshot.get('process_liveness', 'unknown')}",
             "Run ID: " + snapshot.get("run_id", "?")]
    lines.extend(_monitor_progress_lines(snapshot, now))
    lines.extend(_environment_lines(snapshot, now))
    range_profile = snapshot.get("lockin_range_profile")
    if range_profile:
        lines.append("Lock-in range: " + str(range_profile.get("rule_name", "?")) + " | " +
            ", ".join(role + "=" + (_number(value, "V") if value is not None else "unknown")
                      for role, value in range_profile.get("confirmed_full_scales_v", {}).items()) +
            " | Verified: " + str(range_profile.get("verified")))
    if snapshot.get("skipped_conditions"):
        lines.append("Skipped conditions: " + str(snapshot["skipped_conditions"]) + " (not measured)")
    attempt = snapshot.get("current_attempt")
    if attempt:
        lines.append(f"Attempt: {attempt['condition_id']} / {attempt['attempt_index']} / {attempt['status']}")
    event = snapshot.get("last_event")
    if event:
        lines.append(f"Last event: {event['created_at_utc']} | {event['event_type']}")
    if snapshot.get("error"):
        lines.append("Primary error: " + snapshot["error"])
    if snapshot.get("dark_background"):
        background = snapshot["dark_background"]
        lines.append("Dark stage (recorded): " + str(background.get("stage"))
                     + " | " + str(background.get("capture_mode"))
                     + " | valid=" + str(background.get("valid_for_analysis", "pending")))
    recovery = snapshot.get("reference_recovery")
    if recovery:
        payload = recovery["payload"]
        lines.append("Reference recovery (recorded): " + recovery["event_type"] +
            " | " + str(payload.get("stage", "unknown")) +
            (f" | stable {payload.get('consecutive_good', 0)}/{payload['required_good']}"
             if 'required_good' in payload else '')
            + f" | elapsed {payload.get('elapsed_s', 0):.1f} s"
            + f" | remaining {payload.get('remaining_s', payload.get('timeout_s', 0)):.1f} s"
            + " | " + recovery["created_at_utc"])
    formal = snapshot.get("last_formal_measurement")
    formal_contract = "last_formal_measurement" in snapshot
    if formal_contract:
        lines.append("Last formal measurement (accepted; stored, not live):")
        if formal and formal.get("attempt_status") == "accepted":
            sample = formal.get("sample_index")
            lines.append("Condition: " + str(formal.get("condition_id", "unavailable"))
                         + " | Attempt: " + str(formal.get("attempt_index", "unavailable"))
                         + f" | Sample: {sample + 1 if isinstance(sample, int) else '?'}/"
                         + str(formal.get("expected_samples", "?")) + " | Status: accepted")
            lines.append("Formal sample finished UTC: " + str(formal.get("captured_at_utc", "unavailable")))
            selected_readings = formal.get("modules") or {}
        else:
            lines.append("Unavailable (no accepted formal measurement)")
            selected_readings = {}
    else:
        selected_readings = snapshot.get("last_recorded_readings", {})
    readings = []
    for module, reading in selected_readings.items():
        values = {**reading.get("actual", {}), **reading.get("measurements", {})}
        if module == "lockin":
            background = (reading.get("status") or {}).get("background")
            if background:
                lines.append("Dark background (recorded): " + str(background.get("baseline_id"))
                    + f" | age {background.get('age_s', '?')} s"
                    + f" | valid={background.get('valid_for_analysis')}"
                    + f" | expired={background.get('expired', False)}")
        if module == "optical":
            status = reading.get("status") or {}
            if status.get("power_target_w") is not None:
                # Legacy actual.optical_target_power_w held measured watts.
                # Display the retained requested target without rewriting data.
                values["optical_target_power_w"] = status["power_target_w"]
            assessment = status.get("power_target_assessment")
            if assessment:
                values["target_in_tolerance"] = assessment["target_in_tolerance"]
                values["target_deviation_w"] = assessment["target_deviation_w"]
        readings.append((module, reading.get("captured_at_utc", "?"),
            json.dumps(values, ensure_ascii=False), str(reading.get("clean", "unknown"))))
    if readings:
        if not formal_contract:
            lines.append("Last recorded raw candidates (not formal or live):")
        lines += _table(("Module", "Captured UTC", "Readback", "Clean"), readings, width)
    cleanup = snapshot.get("cleanup")
    if cleanup is not None:
        lines.append(f"Cleanup overall: clean={cleanup.get('clean')} | Manual verification: "
                     f"{cleanup.get('manual_verification_required', 'unknown')}")
        rows = _cleanup_rows(cleanup)
        if rows:
            lines += _table(("Module", "Reset", "Review", "Error"), rows, width)
        if cleanup.get("errors"):
            lines.append("Cleanup errors: " + "; ".join(cleanup["errors"]))
    quality = snapshot.get("overload_summary")
    if quality:
        lines.append("Data quality: " + json.dumps(quality, ensure_ascii=False))
    return "\n".join(part for line in lines for part in (textwrap.wrap(line, width=width,
        replace_whitespace=False, break_on_hyphens=False) or [""]))
