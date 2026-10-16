"""DC photoresponse adapter for the shared unified plotting row interface."""
from contextlib import closing
import json
import math
from pathlib import Path
from statistics import fmean, stdev

from .combination_store import open_readonly
from .photoresponse_analysis import results_from_record


def list_photo_runs(path):
    with closing(open_readonly(path)) as connection:
        return tuple(dict(row) for row in connection.execute(
            "SELECT run_id, outcome AS status, created_utc AS created_at_utc FROM photo_runs "
            "ORDER BY created_utc DESC, run_id DESC"))


def load_photo_plot_rows(path, *, run_id=None, audit=False):
    path = Path(path).resolve()
    if path.suffix.lower() == ".json":
        record = json.loads(path.read_text(encoding="utf-8"))
        return _record_rows(record, path, audit=audit)
    with closing(open_readonly(path)) as connection:
        connection.execute("BEGIN")
        query = "SELECT run_id, config_json, record_json, outcome FROM photo_runs"
        parameters = () if run_id is None else (run_id,)
        if run_id is not None:
            query += " WHERE run_id=?"
        records = connection.execute(query + " ORDER BY created_utc, run_id", parameters).fetchall()
        if run_id is not None and not records:
            raise ValueError("Unknown photoresponse run")
        output = []
        for run in records:
            if run["record_json"]:
                record = json.loads(run["record_json"])
            elif audit:
                config = json.loads(run["config_json"])
                backend = config.get("nkt", {}).get("backend")
                record = {"schema_version": 1, "measurement_type": "dc_photoresponse",
                    "run_id": run["run_id"], "completed": False, "outcome": run["outcome"],
                    "simulated": backend == "simulation" if backend is not None else None,
                    "measurement_config": config, "cleanup": {},
                    "conditions": [json.loads(row[0]) for row in connection.execute(
                        "SELECT condition_json FROM photo_conditions WHERE run_id=? ORDER BY condition_id", (run["run_id"],))]}
            else:
                continue
            output.extend(_record_rows(record, path, audit=audit))
        return tuple(output)


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and abs(value) < 9e36 else None


def _record_rows(record, path, *, audit):
    selected = {r["condition_id"]: r for r in results_from_record(
        record, include_rejected=audit, include_dark=True)}
    config = record["measurement_config"]
    quantity = config.get("quantity", "voltage_v")
    rows = []
    for condition in record["conditions"]:
        result = selected.get(condition["condition_id"])
        if result is None and not audit:
            continue
        result = result or {}
        samples = [s for w in condition.get("windows", []) for s in w["samples"]]
        valid_samples = [s for s in samples if s.get("valid") is True]
        measured_samples = (valid_samples if len(condition.get("windows", [])) == 1 else
            [s for w in condition.get("windows", []) if w["phase"] == "light" for s in w["samples"] if s.get("valid") is True])
        optical_state = condition.get("optical_state", "dark/light/dark")
        row = {"run_id": record.get("run_id", path.stem), "condition_id": condition["condition_id"],
            "attempt_index": condition["attempt_index"], "repeat_index": condition.get("repeat_index", 0),
            "power_attempt": condition.get("power_attempt"),
            "sequence_index": len(rows), "sample_index": 0,
            "accepted": result.get("accepted", False), "clean": result.get("accepted", False),
            "simulated": record.get("simulated"), "run_status": record.get("outcome"),
            "measurement_type": "dc_photoresponse", "observation_unit": "condition mean; not an independent detector sample",
            "optical_state": optical_state, "quantity": quantity,
            "requested.optical_state": optical_state,
            "requested.optical_target_power_w": condition.get("target_power_w"),
            "actual.optical_power_w": _number(result.get("measured_power_w")),
            "requested.bias_current_a": condition.get("requested_current_a"),
            "requested.bias_voltage_v": condition.get("requested_voltage_v"),
            "excitation": condition.get("excitation"), "axis_indices": condition.get("axis_indices"),
            "combination_order": condition.get("combination_order"),
            "dark_condition_id": result.get("dark_condition_id"), "baseline_age_s": result.get("baseline_age_s"),
            "difference_method": result.get("method"), "baseline_drift_corrected": result.get("baseline_drift_corrected"),
            "measurement_plane": result.get("measurement_plane", condition.get("power", {}).get("measurement_plane")),
            "config_sha256": record.get("invocation", {}).get("config_sha256"),
            "run_error": record.get("error"), "sample_status": "clean" if result.get("accepted") else "audit",
            "status.photoresponse": {"valid_samples": len(valid_samples), "samples": len(samples),
                "errors": [s["error"] for s in samples if s.get("error")], "outcome": record.get("outcome")},
            "raw.detector_readings": [s.get("detector_last_raw_reading") for s in samples if "detector_last_raw_reading" in s]}
        for key, value in condition["requested_optical"].items():
            row["requested.optical_" + key] = value
        for name in ("dark", "light"):
            row[f"measured.{name}_{quantity}"] = _number(result.get(name + "_mean"))
        # Legacy interpolated-dark records use window_means instead of light_mean.
        if "window_means" in result:
            row[f"measured.light_{quantity}"] = _number(result["window_means"].get("light"))
            row[f"measured.dark_{quantity}"] = _number(result.get("dark_at_light"))
        row["measured.delta_" + quantity] = _number(result.get("delta_" + quantity))
        values = [_number(s.get(quantity)) for s in measured_samples]
        values = [v for v in values if v is not None]
        row["detector_sample_count"] = len(values)
        row["measured.detector_mean_" + quantity] = fmean(values) if values else None
        row["measured.detector_within_window_sd_" + quantity] = stdev(values) if len(values) > 1 else None
        for source_key, destination in (("level_pct", "optical_source_level_pct"),
                                       ("getLdCurrSetpoint", "optical_current_setpoint_a")):
            values = [_number((s.get("optical_after") or {}).get("source", {}).get(source_key)) for s in measured_samples]
            values = [v for v in values if v is not None]
            row["actual." + destination] = fmean(values) if values else None
        sources = [(s.get("electrical_after") or {}).get("source") or {} for s in measured_samples]
        unit = {"dc_current":"a", "dc_voltage":"v"}.get(condition.get("excitation"))
        if unit:
            values = [_number(s.get("setpoint")) for s in sources]
            values = [v for v in values if v is not None]
            row["actual.bias_" + ("current_a" if unit == "a" else "voltage_v")] = fmean(values) if values else None
            # 6221 is a source: readback is its setting, never measured sample current.
            row["measured.source_current_a"] = None
        row["status.source"] = sources[-1] if sources else None
        if optical_state == "dark":
            row["measured.dark_" + quantity] = _number(result.get("dark_mean"))
        environment = condition.get("requested_environment") or {}
        for source, destination in (("temperature_k", "temperature_k"), ("bx_t", "field_x_t"), ("bz_t", "field_z_t")):
            row["requested." + destination] = environment.get(source)
        for source, destination in (("measurement_temperature_k", "temperature_k"),
                                    ("measurement_bx_t", "field_x_t"), ("measurement_bz_t", "field_z_t")):
            row["actual." + destination] = result.get(source)
        for role in ("smu_bias", "gate_top", "gate_bottom"):
            requested = (condition.get("requested_smu") or {}).get("coordinates", {}).get(role)
            hardware = ((config.get("smu") or {}).get("hardware") or {}).get(role) or {}
            suffix = {"voltage":"_v", "current":"_a"}.get(hardware.get("source_mode"), "_coordinate")
            row["requested." + role + suffix] = requested
            row["actual." + role + suffix] = result.get(role + "_source_setpoint")
            for suffix in ("voltage_v", "current_a", "leakage_current_a"):
                if role + "_" + suffix in result:
                    row["measured." + role + "_" + suffix] = result[role + "_" + suffix]
        rows.append(row)
        if audit:
            for attempt in condition.get("power_attempts", []):
                if attempt["valid"] or not attempt["windows"]:
                    continue
                # Failed windows are separate audit observations, never mixed
                # into the successful window's means, differences or statistics.
                rejected = {**condition, "windows": attempt["windows"], "power_attempts": [],
                            "power_attempt": attempt["point_attempt"], "accepted": False, "result": None}
                failed_rows = _record_rows({**record, "conditions": [rejected]}, path, audit=True)
                for failed in failed_rows:
                    failed.update(power_retry_reason=attempt.get("error"), sequence_index=len(rows))
                    rows.append(failed)
    return tuple(rows)
