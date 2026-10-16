"""Compact plain-text views; structured SQLite/JSON audit stays complete."""
from __future__ import annotations

import json
import shutil
import textwrap
import math
import time


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
                  "optical_bandwidth_nm": ("bandwidth", "nm"),
                  "optical_source_level_pct": ("source", "%"),
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
    if summary.get("measurement_type") == "dc_photoresponse":
        return _photoresponse_launch_text(summary, width)
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
    optical = summary.get("optical")
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
              "Authorization: run command | XY disconnection declared in TOML when selected"
              + (" | optical emission/route explicitly confirmed" if "optical" in summary["outer_to_inner"] else "")]
    return "\n".join(part for line in lines for part in (textwrap.wrap(line, width=width,
        replace_whitespace=False, break_on_hyphens=False) or [""]))


def snapshot_text(snapshot, width=None):
    width = max(40, width or shutil.get_terminal_size((110, 30)).columns)
    lines = [f"Status: {snapshot['status']} | Accepted: {snapshot.get('accepted_conditions', '?')}/"
             f"{snapshot.get('total_conditions', '?')} | Process liveness: {snapshot.get('process_liveness', 'unknown')}",
             "Run ID: " + snapshot.get("run_id", "?")]
    if snapshot.get("measurement_type") == "dc_photoresponse":
        lines.append(f"Collected: {snapshot.get('collected_conditions', 0)}/{snapshot.get('total_conditions', '?')}"
                     " | Whole-run acceptance requires completion and verified cleanup")
        lines.append("Phase: " + snapshot.get("phase", "unknown")
                     + f" | Phase elapsed: {snapshot.get('phase_elapsed_s')} s"
                     + f" | Last event age: {snapshot.get('last_event_age_s')} s")
    attempt = snapshot.get("current_attempt")
    if attempt:
        lines.append(f"Attempt: {attempt['condition_id']} / {attempt['attempt_index']} / {attempt['status']}")
        if "optical_state" in attempt:
            target = attempt.get("target_power_w")
            lines.append("Optical: " + str(attempt["optical_state"]) +
                         (" | Target: " + _number(target, "W") if target is not None else ""))
    event = snapshot.get("last_event")
    if event:
        lines.append(f"Last event: {event['created_at_utc']} | {event['event_type']}")
    if snapshot.get("error"):
        lines.append("Primary error: " + snapshot["error"])
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
    readings = []
    for module, reading in snapshot.get("last_recorded_readings", {}).items():
        values = {**reading.get("actual", {}), **reading.get("measurements", {})}
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


def _photoresponse_launch_text(summary, width):
    config = summary["measurement_config"]
    plan = config["combination"]
    power = config.get("power")
    detector = (config.get("hardware") or {}).get("detector") or {}
    points = config["nkt"]["points"]
    lines = ["REAL" if not summary.get("simulated") else "SIMULATION"]
    lines[0] += " combination scan | DC photoresponse | " + config["nkt"]["run_name"]
    lines += ["Order: " + " -> ".join(plan["order"]),
        f"Conditions: {math.prod(plan['axis_lengths'].values())} | Samples/window: {config['samples_per_window']}",
        "Selected modules: " + ", ".join(plan["selected_modules"]),
        f"Mode: {config['mode']} | Detector: {config['detector']} | Excitation: {config['excitation']}"]
    if detector:
        lines.append(f"Detector channel: {detector.get('channel', 1)} | Range: {detector.get('voltage_range_v', 'n/a')} V"
                     f" | NPLC: {detector.get('nplc', 'n/a')}")
    optical = [(str(i + 1), str(p.get("wavelength_nm", "broadband")), str(p.get("bandwidth_nm", "n/a")),
                str(p.get("source_level_pct", p.get("current_a")))) for i, p in enumerate(points)]
    lines += _table(("Optical point", "Wavelength/nm", "Bandwidth/nm", "Source % or LD A"), optical, width)
    if power:
        targets = power.get("power_points") or power["feedback"].get("target_powers_w") or [power["feedback"].get("target_power_w")]
        lines.append("Power points: " + " -> ".join("dark" if v == "dark" else _number(v, "W") for v in targets))
        feedback = power["feedback"]
        lines.append("Power stability: peak-to-peak <= " + _number(feedback["window"]["max_peak_to_peak_w"], "W")
                     + f" | Window {feedback['window']['duration_s']:g} s | Actuator: {feedback['actuator']}")
        lines.append("Power measurement plane: " + power["meter"]["measurement_plane"])
    lines += ["Limits: " + _number(config["max_abs_voltage_v"], "V") + " / " + _number(config["max_abs_current_a"], "A"),
        "Run ID: " + summary["run_id"], "Database: " + summary["database"],
        "Config: " + summary["config"], "Config SHA256: " + summary["config_sha256"],
        "Authorization: run command | selected modules and configured route"]
    return "\n".join(lines)


class PhotoresponseProgressPrinter:
    """Small human view of persisted events; no extra hardware queries."""
    def __init__(self, config, *, enabled=True):
        from .photoresponse_monitor import initial_progress
        self.progress = initial_progress(config)
        self.enabled = enabled
        self.last_power_print = -math.inf

    def _write(self, message, *, error=False):
        if not self.enabled:
            return
        import sys
        try:
            print(message, file=sys.stderr if error else sys.stdout, flush=True)
        except OSError:
            self.enabled = False  # Persisted audit remains authoritative.

    def __call__(self, event):
        from .photoresponse_monitor import update_progress
        update_progress(self.progress, event)
        kind = event.get("phase")
        attempt = self.progress.get("current_attempt") or {}
        if kind == "condition_start":
            self.last_power_print = -math.inf
            target = attempt.get("target_power_w")
            self._write(f"Condition {attempt['condition_id']} | {attempt.get('optical_state')}" +
                        (" | Target " + _number(target, "W") if target is not None else ""))
        elif kind == "phase_start" and event["operation_phase"] != "electrical_state":
            if event["operation_phase"] != "settle" or event.get("requested_s", 0) > 0:
                self._write("Phase: " + event["operation_phase"] +
                            (f" | remaining {event['requested_s']:.3g} s" if "requested_s" in event else ""))
        elif kind == "power_sample" and time.monotonic() - self.last_power_print >= 2:
            self.last_power_print = time.monotonic()
            power = self.progress["last_recorded_readings"]["pm100d"]["actual"]["power_w"]
            if power is not None:
                self._write("PM100D: " + _number(power, "W") + " | " + event["evidence"]["phase"])
        elif kind == "raw_sample":
            sample = event["sample"]
            values = [key + "=" + _number(sample[key], "V" if key == "voltage_v" else "A")
                      for key in ("voltage_v", "current_a") if sample.get("valid") is True
                      and type(sample.get(key)) in (int, float) and math.isfinite(sample[key])]
            self._write("Sample " + event.get("window", "") + " | " + (", ".join(values) or
                        "invalid: " + str(sample.get("error") or sample.get("detector_last_raw_reading"))),
                        error=sample.get("valid") is not True)
        elif kind == "condition_complete":
            result = event["condition"].get("result") or {}
            delta = result.get("delta_" + result.get("quantity", ""))
            self._write(f"Collected {self.progress['collected_conditions']}/{self.progress['total_conditions']}"
                        + (" | Light - dark: " + _number(delta, "V" if result["quantity"] == "voltage_v" else "A")
                           if delta is not None else "") + " | final acceptance pending")
        elif kind == "run_error":
            self._write("Primary error: " + str(event["error"]), error=True)
