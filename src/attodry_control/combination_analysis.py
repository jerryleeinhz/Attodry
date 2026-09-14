"""Read-only common table and regrouping, independent of acquisition loop order."""
from __future__ import annotations

from contextlib import closing
import csv
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
from typing import Mapping, Sequence

from .combination_store import SCHEMA_VERSION, open_readonly


def _flatten(prefix: str, value: Mapping) -> dict:
    result = {}
    for key, item in value.items():
        name = f"{prefix}.{key}" if prefix else key
        if isinstance(item, Mapping):
            result.update(_flatten(name, item))
        else:
            result[name] = item
    return result


def load_combination_rows(path: str | Path, *, run_id: str | None = None,
                          audit: bool = False) -> tuple[dict, ...]:
    """One wide row per formal sample. Audit opt-in exposes failed run samples.

    Raw partial reads remain in combination_events, not invented complete rows.
    """
    with closing(open_readonly(path)) as connection:
        connection.execute("BEGIN")
        rows = connection.execute("""
            SELECT r.schema_version, r.status AS run_status, r.cleanup_json,
                   c.context_json, a.status AS attempt_status, s.*
            FROM combination_samples s
            JOIN combination_runs r USING(run_id)
            JOIN combination_conditions c USING(run_id, condition_id)
            JOIN combination_attempts a USING(run_id, condition_id, attempt_index)
            WHERE (? IS NULL OR r.run_id=?)
            ORDER BY r.created_at_utc, c.sequence_index, s.attempt_index, s.sample_index
        """, (run_id, run_id)).fetchall()
    output = []
    for row in rows:
        if row["schema_version"] != SCHEMA_VERSION:
            raise ValueError("Unsupported combination schema")
        sample = json.loads(row["payload_json"])
        cleanup = json.loads(row["cleanup_json"] or "{}")
        accepted = (row["attempt_status"] == "accepted" and row["run_status"] == "completed"
                    and cleanup.get("clean") is True and sample.get("clean") is True)
        if not audit and not accepted:
            continue
        context = json.loads(row["context_json"])
        record = {
            "schema_version": SCHEMA_VERSION, "source_path": str(Path(path).resolve()),
            "run_id": row["run_id"], "condition_id": row["condition_id"],
            "attempt_index": row["attempt_index"], "sample_index": row["sample_index"],
            "accepted": accepted, "run_status": row["run_status"],
            "attempt_status": row["attempt_status"], "clean": sample["clean"],
            "sequence_index": context["sequence_index"], "repeat_index": context["repeat_index"],
            "loop_order": context["loop_order"],
            "started_at_utc": sample["started_at_utc"], "finished_at_utc": sample["finished_at_utc"],
            "simulated": sample["simulated"],
            **_flatten("axes", context["axes"]), **_flatten("requested", context["requested"]),
            **_flatten("actual", sample["actual"]), **_flatten("measured", sample["measurements"]),
        }
        for reading in sample["reads"]:
            record[f"timestamps.{reading['module']}"] = reading["captured_at_utc"]
            record[f"status.{reading['module']}"] = reading.get("status")
        output.append(record)
    return tuple(output)


def load_legacy_three_smu(path: str | Path, *, audit: bool = False) -> tuple[dict, ...]:
    """Adapt archived CSV plus metadata; never read today's hardware TOML."""
    from .three_smu_analysis import load_three_smu_rows, resolve_run_dir

    directory = resolve_run_dir(path)
    metadata = json.loads((directory / "metadata.json").read_text(encoding="utf-8"))
    grouped: dict[tuple, dict] = {}
    for row in load_three_smu_rows(directory, include_rejected=audit, include_problem=audit):
        key = row.point_index, row.repeat_index, row.segment
        record = grouped.setdefault(key, {
            "source_path": str(directory), "run_id": str(directory),
            "condition_id": f"legacy-point-{row.point_index}",
            "sequence_index": row.point_index, "sample_index": row.repeat_index,
            "repeat_index": 0, "attempt_index": None, "accepted": row.accepted,
            "clean": row.clean, "run_status": metadata.get("status"),
            "axes.smu.segment": row.segment, "axes.smu.direction": None,
            "axes.smu.index": row.point_index, "loop_order": ["smu"],
            "legacy_schema": f"three-smu-{metadata.get('schema_version', 'unknown')}",
            "simulated": None,
        })
        record["accepted"] = record["accepted"] and row.accepted
        record["clean"] = record["clean"] and row.clean
        mode = metadata.get("hardware", {}).get(row.role, {}).get("source_mode")
        suffix = {"voltage": "_v", "current": "_a"}.get(mode)
        if suffix is not None:
            record["requested." + row.role + suffix] = row.coordinate
            record["actual." + row.role + suffix] = row.source_setpoint
        # Unknown old source mode stays unknown, never guessed from a field name.
        record["legacy." + row.role + ".coordinate"] = row.coordinate
        for name in ("voltage_v", "current_a", "resistance_ohm"):
            record[f"measured.{row.role}_{name}"] = getattr(row, name)
        record["timestamps." + row.role] = row.timestamp
        record["status." + row.role] = {
            "status": row.status, "output_enabled": row.output_enabled,
            "compliance_trip": row.compliance_trip, "problems": row.problems}
    return tuple(grouped.values())


def load_legacy_temperature_lockin(path: str | Path) -> tuple[dict, ...]:
    """Adapt completed/clean legacy summary or CSV; missing IDs/times stay absent.

    Role/harmonic rows remain separate because legacy temperature contexts may
    differ between reads. Do not invent simultaneous multi-harmonic samples.
    """
    from .temperature_excitation_analysis import load_temperature_excitation_samples

    output = []
    for row in load_temperature_excitation_samples(path):
        role = "lockin_" + row.role
        output.append({
            "source_path": row.source_path, "run_id": row.source_path,
            "condition_id": f"legacy-T{row.temperature_index}-L{row.point_index}",
            "sequence_index": None, "sample_index": row.sample_index,
            "repeat_index": 0, "attempt_index": None, "accepted": True, "clean": True,
            "legacy_schema": "temperature-excitation", "simulated": None,
            "loop_order": ["temperature", "lockin"],
            "axes.temperature.index": row.temperature_index,
            "axes.lockin.index": row.point_index, "role": role, "harmonic": row.harmonic,
            "requested.temperature_k": row.requested_temperature_k,
            "actual.temperature_k": row.sample_measurement_temperature_k,
            "requested.lockin_excitation_v_rms": row.source_v_rms,
            "actual.lockin_excitation_v_rms": row.source_readback_v_rms,
            "actual.lockin_frequency_hz": row.frequency_hz,
            "measured.lockin_current_a_rms": row.current_a_rms,
            f"measured.{role}_h{row.harmonic}_x_v": row.x_v,
            f"measured.{role}_h{row.harmonic}_y_v": row.y_v,
            f"measured.{role}_h{row.harmonic}_amplitude_v": row.amplitude_v,
            f"measured.{role}_h{row.harmonic}_phase_deg": row.phase_deg,
            "status." + role: {"lia_status_raw": row.lia_status_raw,
                              "error_status": row.error_status, "problems": row.problems},
        })
    return tuple(output)


@dataclass(frozen=True)
class SelectedSeries:
    group: tuple[tuple[str, object], ...]
    rows: tuple[dict, ...]


def _coordinate_for_x(x: str, requested: set[str]) -> str | None:
    name = x.split(".", 1)[-1]
    if name in requested:
        return name
    for role in ("smu_bias", "gate_top", "gate_bottom"):
        if name.startswith(role + "_"):
            return next((key for key in (role + "_v", role + "_a") if key in requested), None)
    if name == "lockin_current_a_rms":
        return "lockin_excitation_v_rms"
    return None


def select_series(rows: Sequence[dict], *, x: str, y: str,
                  group_by: Sequence[str] = (), filters: Mapping | None = None,
                  sort_x: bool = False, audit: bool = False) -> tuple[SelectedSeries, ...]:
    """Keep every observation; no averaging, pivot, interpolation, or deduplication.

    Non-plotted requested coordinates and run/repeat/segment/direction remain
    grouping keys. Fixed requested values group noisy actual readbacks reliably.
    The returned group explicitly exposes these automatically protected keys.
    """
    filters = dict(filters or {})
    available = {key for row in rows for key in row}
    unknown = {x, y, *group_by, *filters} - available
    if unknown and rows:
        raise ValueError(f"Unknown columns: {sorted(unknown)}")
    selected = [row for row in rows if (audit or row.get("accepted") is True)
                and all(row.get(key) == value for key, value in filters.items())]
    requested = {key[10:] for row in selected for key in row if key.startswith("requested.")}
    x_coordinate = _coordinate_for_x(x, requested)
    covered = {key.split(".", 1)[-1] for key in (*group_by, *filters)}
    protected = ["run_id", "simulated", "repeat_index", "sample_index", "role", "harmonic"]
    protected += sorted({key for row in selected for key in row if
                         key.startswith("axes.") and key.endswith((".segment", ".direction"))})
    protected += sorted("requested." + key for key in requested
                        if key != x_coordinate and key not in covered)
    # Literal point revisits on non-X axes are different histories.
    x_module = next((m for m, keys in {
        "temperature": {"temperature_k"}, "magnetic": {"field_x_t", "field_z_t"},
        "smu": {r + s for r in ("smu_bias", "gate_top", "gate_bottom") for s in ("_v", "_a")},
        "lockin": {"lockin_excitation_v_rms", "lockin_frequency_hz"},
    }.items() if x_coordinate in keys), None)
    protected += sorted({key for row in selected for key in row
                         if key.startswith("axes.") and key.endswith(".index")
                         and key.split(".")[1] != x_module})
    grouping = list(dict.fromkeys([*group_by, *protected]))
    groups: dict[tuple, list] = {}
    for row in selected:
        # Missing channels remain missing, not zero. Scatter plots never bridge gaps.
        if any(type(row.get(key)) not in (int, float) or not math.isfinite(row[key])
               for key in (x, y)):
            continue
        key = tuple((name, row.get(name)) for name in grouping if name in available)
        groups.setdefault(key, []).append(dict(row))
    return tuple(SelectedSeries(key, tuple(sorted(values, key=lambda r: r[x])
                                         if sort_x else values)) for key, values in groups.items())


def _column_label(column: str) -> str:
    names = {
        "temperature_k": "Temperature (K)", "field_x_t": "Bx (T)", "field_z_t": "Bz (T)",
        "lockin_excitation_v_rms": "Excitation (V RMS)", "lockin_frequency_hz": "Frequency (Hz)",
        "lockin_current_a_rms": "Excitation current (A RMS)",
    }
    for role in ("smu_bias", "gate_top", "gate_bottom"):
        label = role.replace("_", " ").capitalize()
        names.update({role + "_voltage_v": label + " voltage (V)",
                      role + "_current_a": label + " current (A)",
                      role + "_v": label + " source (V)",
                      role + "_a": label + " source (A)"})
    prefix, _, key = column.partition(".")
    suffix = " target" if prefix == "requested" else " readback" if prefix == "actual" else ""
    if key in names:
        return names[key] + suffix
    return column.replace("axes.", "").replace(".", " ").replace("_", " ")


def plot_series(series: Sequence[SelectedSeries], *, x: str, y: str,
                title: str = ""):
    """Raw-observation scatter, redundant color/marker; no implied interpolation."""
    if not series:
        raise ValueError("No finite selected observations")
    from .scientific_plotting import publication_style, OKABE_ITO_ON_WHITE, SERIES_MARKERS
    with publication_style():
        import matplotlib.pyplot as plt
        figure, axis = plt.subplots(figsize=(9.2, 4.8), layout="constrained")
        varying = {key for item in series for key, value in item.group
                   if len({str(dict(other.group).get(key)) for other in series}) > 1}
        for index, item in enumerate(series):
            label = ", ".join(f"{_column_label(k)}={v}" for k, v in item.group
                              if k in varying) or "observations"
            axis.scatter([r[x] for r in item.rows], [r[y] for r in item.rows],
                         facecolors="none", linewidths=1.2,
                         edgecolors=OKABE_ITO_ON_WHITE[index % len(OKABE_ITO_ON_WHITE)],
                         marker=SERIES_MARKERS[index % len(SERIES_MARKERS)], label=label)
        axis.set(xlabel=_column_label(x), ylabel=_column_label(y),
                 title=title or "Raw observations (no averaging)")
        if all(row.get("simulated") is True for item in series for row in item.rows):
            axis.set_title((title + " — " if title else "") + "SIMULATED DATA — raw observations")
        axis.legend(loc="center left", bbox_to_anchor=(1.02, 0.5))
    return figure, axis


def export_selection(directory: str | Path, series: Sequence[SelectedSeries], *,
                     x: str, y: str, group_by: Sequence[str] = (),
                     filters: Mapping | None = None, sort_x: bool = False,
                     audit: bool = False, excluded_sample_ids: Sequence = ()) -> Path:
    """New directory only; selected data and exact sample IDs accompany the plot."""
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=False)
    rows = [{**row, "selection_series_index": index}
            for index, item in enumerate(series) for row in item.rows]
    fields = sorted({key for row in rows for key in row})
    with (target / "selected_samples.csv").open("x", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    fingerprint = hashlib.sha256(json.dumps(rows, sort_keys=True, ensure_ascii=False,
                                           allow_nan=False).encode()).hexdigest()
    manifest = {
        "analysis_contract": "combination-v1", "x": x, "y": y, "group_by": list(group_by),
        "filters": dict(filters or {}), "sort_x": sort_x, "audit": audit,
        "aggregation": "none", "missing": "omitted from scatter, never filled with zero",
        "source_paths": sorted({r["source_path"] for r in rows if "source_path" in r}),
        "selection_sha256": fingerprint, "selected_count": len(rows),
        "excluded_sample_ids": list(excluded_sample_ids),
        "analysis_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "series": [{"group": item.group, "sample_ids": [
            {k: row.get(k) for k in ("run_id", "condition_id", "attempt_index", "sample_index",
                                    "role", "harmonic")} for row in item.rows]} for item in series],
    }
    (target / "selection_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    return target
