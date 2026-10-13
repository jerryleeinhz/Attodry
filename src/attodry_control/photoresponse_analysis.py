"""Read-only accepted-results loader. Does not import any instrument adapter."""
from __future__ import annotations

import json
from pathlib import Path
from statistics import fmean


def load_results(path: str | Path, *, include_rejected=False) -> list[dict]:
    with Path(path).open(encoding="utf-8") as stream:
        record = json.load(stream)
    if record.get("schema_version") != 1 or record.get("measurement_type") != "dc_photoresponse":
        raise ValueError("Unsupported photoresponse record")
    cleanup = record.get("cleanup", {})
    clean = (record.get("completed") is True and record.get("outcome") == "completed"
             and cleanup.get("laser_off_confirmed") is True
             and cleanup.get("electrical_safe_confirmed") is True and not cleanup.get("errors"))
    rows = []
    for condition in record["conditions"]:
        accepted = clean and condition.get("accepted") is True
        if not accepted and not include_rejected:
            continue
        if condition.get("result") is None:
            continue
        environment = [s["environment_after"] for w in condition["windows"] for s in w["samples"]
                       if s.get("environment_after") is not None]
        actual = {"measurement_temperature_k": fmean(s["measurement_temperature_k"] for s in environment),
                  "measurement_bx_t": fmean(s["field"]["bx_t"] for s in environment),
                  "measurement_bz_t": fmean(s["field"]["bz_t"] for s in environment)} if environment else {
                      "measurement_temperature_k": None, "measurement_bx_t": None, "measurement_bz_t": None}
        rows.append({"condition_id": condition["condition_id"], "attempt_index": condition["attempt_index"],
                     "accepted": accepted, "simulated": record["simulated"],
                     "requested_optical": condition["requested_optical"],
                     "requested_current_a": condition["requested_current_a"],
                     "requested_voltage_v": condition["requested_voltage_v"],
                     "environment_index": condition.get("environment_index", 0),
                     "requested_environment": condition.get("requested_environment"),
                     "target_power_w": condition.get("target_power_w"),
                     "measured_power_w": condition.get("power", {}).get("formal_mean_power_w"),
                     "measurement_plane": condition.get("power", {}).get("measurement_plane"),
                     "combination_order": condition.get("combination_order"),
                     "axis_indices": condition.get("axis_indices", {}),
                     "excitation": condition["excitation"], **actual, **condition["result"]})
    return rows


def export_results(record_path, output_path):
    """Accepted-only CSV plus selection manifest, without touching raw records."""
    import csv
    import hashlib
    rows = load_results(record_path)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    flat = []
    for row in rows:
        values = {k:v for k,v in row.items() if not isinstance(v, dict)}
        values.update({"requested_optical_"+k:v for k,v in row["requested_optical"].items()})
        values.update({"mean_"+k:v for k,v in row["window_means"].items()})
        values.update({"axis_index_"+k:v for k,v in row["axis_indices"].items()})
        flat.append(values)
    fields = sorted({key for row in flat for key in row})
    with output.open("x",newline="",encoding="utf-8-sig") as stream:
        writer=csv.DictWriter(stream,fieldnames=fields)
        writer.writeheader()
        writer.writerows(flat)
    manifest={"input":str(Path(record_path).resolve()),
        "sha256":hashlib.sha256(Path(record_path).read_bytes()).hexdigest(),
        "accepted_only":True, "rows":len(flat),"export":str(output.resolve())}
    with output.with_suffix(".manifest.json").open("x",encoding="utf-8") as stream:
        json.dump(manifest,stream,indent=2)
    return manifest
