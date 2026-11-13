"""Read-only 6221 SQLite adapter for the existing commissioning plots and fits."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, replace
import hashlib
import json
import math
from pathlib import Path

from .analysis_observations import channel_quality
from .combination_analysis import load_combination_snapshot, lockin_channel_labels
from .combination_store import open_readonly
from .commissioning_analysis import CommissioningSample, _commissioning_sample


@dataclass(frozen=True)
class CurrentSweepSnapshot:
    groups: dict[str, tuple[CommissioningSample, ...]]
    metadata: dict


def list_6221_runs(database: str | Path) -> tuple[dict, ...]:
    """Catalog explicit run IDs without reading sample histories or live config."""
    with closing(open_readonly(database)) as db:
        runs = db.execute("SELECT run_id,status,plan_json FROM combination_runs "
                          "ORDER BY created_at_utc DESC,run_id").fetchall()
    return tuple({"run_id": r["run_id"], "status": r["status"]} for r in runs
                 if json.loads(r["plan_json"]).get("excitation_source") == "keithley6221")


def _positive(value, name):
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ValueError(f"Missing or invalid archived {name}; no requested-value fallback.")
    return float(value)


def load_6221_sweep(database: str | Path, run_id: str, *, preview=False,
                    include_rejected=False, include_problem=False) -> CurrentSweepSnapshot:
    """Materialize one run; default keeps final clean accepted selected channels.

    Each group has one archived configuration/contract, requested frequency,
    repeat and outer-axis visit. Recovery segments with the same configuration
    can share a curve, while their original execution/attempt IDs remain on rows.
    No imaginary SINE OUT voltage, resistor or measured current is created.
    """
    snapshot = load_combination_snapshot(database, run_ids=(run_id,),
                                         audit=include_rejected, preview=preview)
    if not snapshot.metadata["runs"]:
        raise ValueError("Selected combination run does not exist.")
    groups, descriptions, exclusions = {}, {}, []
    for record in snapshot.rows:
        if record.get("excitation_source") != "keithley6221":
            raise ValueError("Selected run is not a 6221 excitation scan.")
        current = _positive(record.get("actual.excitation_current_rms_a"), "6221 RMS current readback")
        frequency = _positive(record.get("actual.excitation_frequency_hz"), "6221 frequency readback")
        requested_current = _positive(record.get("requested.excitation_current_rms_a"), "requested RMS current")
        requested_frequency = _positive(record.get("requested.excitation_frequency_hz"), "requested frequency")
        status = record.get("status.lockin") or {}
        pairs = status.get("samples")
        if not isinstance(pairs, list) or not pairs:
            raise ValueError("Formal combination sample lacks archived detector pairs.")
        outer = {k: v for k, v in record.items() if k.startswith("axes.")
                 and not k.startswith(("axes.excitation.", "axes.lockin."))}
        binding = {"run_id": run_id, "configuration_revision": record.get("configuration_revision"),
                   "measurement_contract": record.get("measurement_contract"),
                   "requested_frequency_hz": requested_frequency,
                   "repeat_index": record.get("repeat_index"), "outer_visit": outer,
                   "lockin_range_configuration": record.get("lockin_range_configuration")}
        group_key = hashlib.sha256(json.dumps(binding, sort_keys=True, allow_nan=False).encode()).hexdigest()[:16]
        descriptions[group_key] = binding
        labels = lockin_channel_labels(record)
        seen = set()
        for pair in pairs:
            roles = pair.get("selected_roles")
            if not isinstance(roles, list) or not roles:
                raise ValueError("6221 formal pair lacks selected-role evidence.")
            for selected_role in roles:
                role = selected_role.removeprefix("lockin_")
                if role not in ("xx", "xy"):
                    raise ValueError("Unknown selected detector role.")
                instrument = pair.get("lockin_" + role) or {}
                harmonic = (instrument.get("reading") or {}).get("harmonic")
                identity = (role, harmonic, pair.get("sample_index"))
                if identity in seen:
                    raise ValueError("Duplicate formal detector identity in one sample.")
                seen.add(identity)
                # Qualify this selected pair rather than an unselected companion
                # in another harmonic's bracket. Reuse shared native status rules.
                evidence = dict(record, **{"status.lockin": {"samples": [pair]}})
                quality, issues = channel_quality(evidence, f"measured.lockin_{role}_h{harmonic}_amplitude_v")
                issues = list(issues)
                if pair.get("settings_verified") is not True:
                    issues.append("settings_unverified")
                if quality == "unknown":
                    issues.append("unknown_formal_status")
                if issues and not include_problem:
                    exclusions.append({"condition_id": record["condition_id"],
                        "attempt_index": record["attempt_index"], "sample_index": record["sample_index"],
                        "role": role, "harmonic": harmonic, "reasons": list(dict.fromkeys(issues))})
                    continue
                point = {"point_index": record["sequence_index"], "target_frequency_hz": requested_frequency,
                         "actual_frequency_hz": frequency, "source_v_rms": None}
                row = _commissioning_sample(path=Path(database).resolve(),
                    record_status=("completed" if record["accepted"] else "other" if record["preview"] else "rejected"),
                    scan_type="excitation", point=point, sample=pair, role=role,
                    problems=(), recorded_excitation_path=None)
                if row.statuses != ("clean",) and not include_problem:
                    exclusions.append({"condition_id": record["condition_id"],
                        "attempt_index": record["attempt_index"], "sample_index": record["sample_index"],
                        "role": role, "harmonic": harmonic, "reasons": list(row.statuses)})
                    continue
                if issues:
                    row = replace(row, problems=tuple(dict.fromkeys((*row.problems, *issues))),
                                  statuses=tuple(dict.fromkeys((*[v for v in row.statuses if v != "clean"], "problem"))))
                row = replace(row, excitation_source="keithley6221", excitation_current_rms_a=current,
                    requested_current_rms_a=requested_current, run_id=run_id,
                    execution_id=record["execution_id"], condition_id=record["condition_id"],
                    attempt_index=record["attempt_index"], configuration_revision=record["configuration_revision"],
                    channel_label=labels.get(role), combination_sample_index=record["sample_index"])
                groups.setdefault(group_key, []).append(row)
    metadata = dict(snapshot.metadata, current_basis="6221_rms_setting_readback",
        current_is_measured=False, group_bindings=descriptions, quality_exclusions=exclusions,
        include_problem=bool(include_problem), include_rejected=bool(include_rejected),
        selected_channel_rows=sum(map(len, groups.values())))
    return CurrentSweepSnapshot({k: tuple(v) for k, v in groups.items()}, metadata)
