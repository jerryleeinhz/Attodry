"""Compact plain-text views; structured SQLite/JSON audit stays complete."""
from __future__ import annotations

import json
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


def snapshot_text(snapshot, width=None):
    width = max(40, width or shutil.get_terminal_size((110, 30)).columns)
    lines = [f"Status: {snapshot['status']} | Accepted: {snapshot.get('accepted_conditions', '?')}/"
             f"{snapshot.get('total_conditions', '?')} | Process liveness: {snapshot.get('process_liveness', 'unknown')}",
             "Run ID: " + snapshot.get("run_id", "?")]
    attempt = snapshot.get("current_attempt")
    if attempt:
        lines.append(f"Attempt: {attempt['condition_id']} / {attempt['attempt_index']} / {attempt['status']}")
    event = snapshot.get("last_event")
    if event:
        lines.append(f"Last event: {event['created_at_utc']} | {event['event_type']}")
    if snapshot.get("error"):
        lines.append("Primary error: " + snapshot["error"])
    readings = []
    for module, reading in snapshot.get("last_recorded_readings", {}).items():
        readings.append((module, reading.get("captured_at_utc", "?"),
            json.dumps({**reading.get("actual", {}), **reading.get("measurements", {})},
                       ensure_ascii=False), str(reading.get("clean", "unknown"))))
    if readings:
        lines += ["Last recorded values (not live queries):",
                  *_table(("Module", "Captured UTC", "Readback", "Clean"), readings, width)]
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
