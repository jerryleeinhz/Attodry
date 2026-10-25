"""Compact plain-text views; structured SQLite/JSON audit stays complete."""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
import shutil
import textwrap


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
    for role, settings in summary.get("smu", {}).items():
        hardware.append((role, settings["source_mode"], "AUTO", "-",
            _number(settings["max_abs_current_a"], "A") + " / " +
            _number(settings["max_abs_voltage_v"], "V") + " max"))
    lockin = summary.get("lockin", {})
    for role, settings in lockin.get("roles", {}).items():
        harmonics = lockin["harmonics_by_role"].get(role.removeprefix("lockin_"), [])
        hardware.append((role + " " + settings.get("model", "SR830"), ",".join(f"h{h}" for h in harmonics) or "no acquisition",
            settings["sensitivity_mode"] + " " + _number(settings["sensitivity_full_scale_v"], "V"),
            settings["reserve_mode"] or "n/a", f"TC {_number(settings['time_constant_s'], 's')}"))
        if settings.get("sr865a") is not None:
            capabilities = settings["sr865a"]
            hardware.append((role, "SR865A input", "IRNG " + _number(capabilities["input_range_v_peak"], "V peak"),
                "n/a", "Ref " + _number(capabilities["reference_input_impedance_ohm"], "ohm")))
            hardware.append((role, "SR865A diagnostics", "current status " +
                ("supported" if capabilities["current_status_supported"] else "unsupported"),
                "n/a", "SYNC " + capabilities["sync_output_mode"]))
        if settings["sensitivity_mode"] == "bounded_auto":
            hardware.append((role, "AUTO limits",
                _number(settings["autorange_min_full_scale_v"], "V") + " .. " +
                _number(settings["autorange_max_full_scale_v"], "V"), "-", "-"))
        for override in settings["harmonic_settings"]:
            hardware.append((role, f"h{override['harmonic']} override",
                _number(override["sensitivity_full_scale_v"], "V"),
                getattr(override["reserve_mode"], "value", override["reserve_mode"]) or "n/a",
                getattr(override["sensitivity_mode"], "value", override["sensitivity_mode"])))
            if override["sensitivity_mode"] == "bounded_auto":
                minimum = override.get("autorange_min_full_scale_v") or settings["autorange_min_full_scale_v"]
                maximum = override.get("autorange_max_full_scale_v") or settings["autorange_max_full_scale_v"]
                if minimum is not None and maximum is not None:
                    hardware.append((role, f"h{override['harmonic']} AUTO limits",
                        _number(minimum, "V") + " .. " + _number(maximum, "V"), "-", "-"))
    if hardware:
        lines += ["", *_table(("Instrument", "Measurement", "Sensitivity", "Reserve", "Protection/timing"),
                              hardware, width)]
    if lockin:
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
    if "lockin_range_rules" in summary:
        rows = []
        for item in summary["lockin_range_rules"]["condition_bindings"]:
            coordinates = ", ".join(f"{key}={value:.12g}" for key, value in item["requested"].items())
            visits = ", ".join(f"{key}#{value}" for key, value in item["outer_indices"].items())
            rows.append((item["condition_id"], visits, coordinates, item["rule_name"],
                         _number(item["full_scales_v"]["lockin_xx"], "V"),
                         _number(item["full_scales_v"]["lockin_xy"], "V")))
        lines += ["", "Planned Lock-in sensitivity rules:",
                  *_table(("Condition", "Outer visits", "Requested", "Rule", "XX", "XY"), rows, width)]
    if "field_transition_policy" in summary:
        policy = summary.get("field_readback_policy")
        if policy is not None and policy["mode"] != "vector":
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
                         + (f" | |B| <= {vector:.7g} T" if vector is not None else "")
                         + " | Nominal targets unchanged")
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
        if module == "lockin" and any(settings.get("model") == "SR865A"
                for settings in lockin.get("roles", {}).values()):
            text = text.replace("SENS/Reserve", "XX SENS/Reserve; XY SCAL")
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
              "Config: " + summary["config"],
              "Authorization: run command | XY disconnection declared in TOML when selected"]
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
             "lockin_wait": "Lock-in settling / sampling interval",
             "condition_qualification": "Condition qualification", "close": "Closing connections",
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
    executions = snapshot.get("executions") or []
    current_id = snapshot.get("current_execution_id")
    current = next((item for item in executions if item.get("execution_id") == current_id), None)
    if current is not None:
        plan = current.get("plan") or json.loads(current.get("plan_json") or "{}")
        index = current.get("sequence_index", 0)
        lines.append(f"Execution: {current_id} | Segment {index + 1}/{len(executions)}"
                     f" | {current.get('kind', '?')} | {current.get('status', '?')}")
        if plan.get("config_sha256"):
            lines.append("Execution config SHA256: " + plan["config_sha256"])
        if len(executions) > 1:
            previous = sum(item.get("accepted_conditions", 0) for item in executions
                           if item.get("execution_id") != current_id)
            count = current.get("accepted_conditions")
            lines.append(f"Recovery progress: reused {previous} accepted conditions"
                         + (f" | current execution accepted {count}" if count is not None else ""))
        for prior in executions:
            if prior.get("execution_id") != current_id and prior.get("status") in {"failed", "interrupted"}:
                lines.append(f"Retained history: {prior['execution_id']} | {prior['status']}"
                             + (" | " + prior["error"] if prior.get("error") else ""))
    attempt = snapshot.get("current_attempt")
    if attempt:
        lines.append(f"Attempt: {attempt['condition_id']} / {attempt['attempt_index']} / {attempt['status']}")
    event = snapshot.get("last_event")
    if event:
        lines.append(f"Last event: {event['created_at_utc']} | {event['event_type']}")
    if snapshot.get("error"):
        lines.append("Primary error: " + snapshot["error"])
    readings = []
    formal = snapshot.get("last_formal_measurement")
    if "last_formal_measurement" in snapshot:
        lines.append("Last formal measurement (accepted; stored, not live):")
        if formal and formal.get("attempt_status") == "accepted":
            lines.append(f"Condition: {formal['condition_id']} | Attempt: {formal['attempt_index']}"
                         f" | Sample: {formal['sample_index'] + 1}/{formal['expected_samples']} | Status: accepted")
            lines.append("Formal sample finished UTC: " + str(formal.get("captured_at_utc", "unknown")))
            selected_readings = formal.get("modules") or {}
        else:
            lines.append("Unavailable (no accepted formal measurement)")
            selected_readings = {}
    else:
        selected_readings = snapshot.get("last_recorded_readings", {})
        if selected_readings:
            lines.append("Last recorded raw candidates (not formal or live):")
    for module, reading in selected_readings.items():
        readings.append((module, reading.get("captured_at_utc", "?"),
            json.dumps({**reading.get("actual", {}), **reading.get("measurements", {})},
                       ensure_ascii=False), str(reading.get("clean", "unknown"))))
    if readings:
        lines += _table(("Module", "Captured UTC", "Readback", "Clean"), readings, width)
    ranges = snapshot.get("last_recorded_lockin_ranges")
    if ranges:
        values = ranges.get("confirmed_full_scales_v", {})
        lines.append("Last recorded Lock-in ranges (measurement profile"
                     + ("; superseded by cleanup" if ranges.get("superseded_by_cleanup") else "")
                     + "): " + str(ranges.get("rule_name", "?"))
                     + " | " + str(ranges.get("captured_at_utc", "?"))
                     + " | verified=" + str(ranges.get("verified", False)))
        for role in ("lockin_xx", "lockin_xy"):
            value = values.get(role)
            lines.append(role + ": " + (_number(value, "V") if value is not None else "unknown"))
    timing = snapshot.get("measurement_timing_summary")
    if timing:
        lines.append("Measurement timing (stored summary): " + str(timing.get("recorded_at_utc", "unknown")))
        lines.append("Scope: " + str(timing.get("scope", "recorded completed stages")))
        lines.append("Elapsed: " + _duration(timing.get("elapsed_s"))
                     + " | Exclusive stages: " + _duration(timing.get("exclusive_stage_duration_s"))
                     + " | Audit: " + _duration(timing.get("audit_duration_s"))
                     + " | Unattributed: " + _duration(timing.get("unattributed_duration_s")))
        stages = [(name, str(values.get("count", "?")), str(values.get("failed", "?")),
                   _duration(values.get("exclusive_duration_s")))
                  for name, values in (timing.get("stages") or {}).items()]
        if stages:
            lines.extend(_table(("Stage", "Count", "Failed", "Exclusive time"), stages, width))
        lines.append("Nested inclusive stage times are not additive.")
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
