"""Read-only accepted-results loader. Does not import any instrument adapter."""
from __future__ import annotations

import json
from pathlib import Path


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
        rows.append({"condition_id": condition["condition_id"], "attempt_index": condition["attempt_index"],
                     "accepted": accepted, "simulated": record["simulated"],
                     "requested_optical": condition["requested_optical"],
                     "requested_current_a": condition["requested_current_a"],
                     "requested_voltage_v": condition["requested_voltage_v"],
                     "excitation": condition["excitation"], **condition["result"]})
    return rows
