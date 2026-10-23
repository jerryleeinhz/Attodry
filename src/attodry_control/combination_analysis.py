"""Read-only common table and regrouping, independent of acquisition loop order."""
from __future__ import annotations

from contextlib import closing
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
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


def list_combination_runs(path: str | Path) -> tuple[dict, ...]:
    """Read run identities/status only; never decode formal sample payloads."""
    with closing(open_readonly(path)) as connection:
        rows = connection.execute(
            "SELECT run_id, schema_version, created_at_utc, status FROM combination_runs "
            "ORDER BY created_at_utc, run_id"
        ).fetchall()
    return tuple(dict(row) for row in rows)


@dataclass(frozen=True, slots=True)
class CombinationSnapshot:
    """Materialized rows and provenance from one short SQLite read transaction."""

    rows: tuple[dict, ...]
    metadata: dict


def _snapshot_json_value(value):
    """Preserve nonfinite raw readings while giving their digest standard JSON."""
    if isinstance(value, Mapping):
        return {key: _snapshot_json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_snapshot_json_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return {"__nonfinite_float__": "nan" if math.isnan(value) else "inf" if value > 0 else "-inf"}
    return value


def _snapshot_digest(value) -> str:
    return hashlib.sha256(json.dumps(_snapshot_json_value(value), sort_keys=True, ensure_ascii=False,
                                     allow_nan=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def _lockin_range_bindings(plan):
    schedule = plan.get("lockin_range_rules")
    if not schedule:
        return None
    bindings = {}
    for item in schedule.get("condition_bindings", ()):
        condition_id = item["condition_id"]
        if condition_id in bindings:
            raise ValueError("Formal sample lacks its unique archived Lock-in range binding")
        bindings[condition_id] = item
    return bindings


def _lockin_range_columns(bindings, condition_id, reading):
    """Expose archived condition ranges without inferring missing actual settings."""
    if bindings is None:
        return {}
    if condition_id not in bindings:
        raise ValueError("Formal sample lacks its unique archived Lock-in range binding")
    binding = bindings[condition_id]
    scales = binding["full_scales_v"]
    status = (reading.get("status") or {}).get("lockin_range_rule") or {}
    if ((status.get("rule_name") is not None and status["rule_name"] != binding["rule_name"])
            or (status.get("requested_full_scales_v") is not None
                and status["requested_full_scales_v"] != scales)):
        raise ValueError("Formal Lock-in range rule differs from its archived condition binding")
    return {
        "lockin_range_configuration": json.dumps(scales, sort_keys=True, allow_nan=False,
                                                  separators=(",", ":")),
        "lockin_range.rule_name": binding["rule_name"],
        "lockin_range.verified": status.get("verified"),
        **_flatten("lockin_range.requested_full_scales_v", scales),
        **_flatten("lockin_range.confirmed_full_scales_v", status.get("confirmed_full_scales_v") or {}),
    }


def load_combination_snapshot(path: str | Path, *, run_ids: Sequence[str] | None = None,
                              audit: bool = False, preview: bool = False) -> CombinationSnapshot:
    """Read all selected runs and their provenance at one committed DB revision.

    The connection closes before decoding payloads or rendering. Concurrent WAL
    appends do not invalidate these already-materialized rows. ``preview`` adds
    only clean formal samples from accepted attempts of active runs; they retain
    ``accepted=False`` and are labelled ``preview=True``. Audit retains its
    historical opt-in meaning. Raw partial events never become formal rows.
    """
    selected_ids = None if run_ids is None else tuple(dict.fromkeys(run_ids))
    if selected_ids is not None and any(not isinstance(value, str) for value in selected_ids):
        raise ValueError("Run IDs must be strings")
    clause = "" if selected_ids is None else (
        " WHERE run_id IN (" + ",".join("?" for _ in selected_ids) + ")"
        if selected_ids else " WHERE 0"
    )
    parameters = () if selected_ids is None else selected_ids
    with closing(open_readonly(path)) as connection:
        connection.execute("BEGIN")
        run_rows = connection.execute(
            "SELECT run_id, schema_version, created_at_utc, status, error, cleanup_json "
            "FROM combination_runs" + clause + " ORDER BY created_at_utc, run_id", parameters,
        ).fetchall()
        where = clause.replace("run_id IN", "s.run_id IN")
        rows = connection.execute(f"""
            SELECT r.schema_version, r.status AS run_status, r.error AS run_error,
                   r.cleanup_json, r.plan_json,
                   c.context_json, a.status AS attempt_status, s.*
            FROM combination_samples s
            JOIN combination_runs r USING(run_id)
            JOIN combination_conditions c USING(run_id, condition_id)
            JOIN combination_attempts a USING(run_id, condition_id, attempt_index)
            {where}
            ORDER BY r.created_at_utc, r.run_id, c.sequence_index, s.attempt_index, s.sample_index
        """, parameters).fetchall()
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        executions, attempt_executions = {}, {}
        execution_tables = {"combination_executions", "combination_attempt_executions"}
        if tables & execution_tables and not execution_tables <= tables:
            raise ValueError("Incomplete execution provenance schema")
        if execution_tables <= tables:
            for execution in connection.execute(
                    "SELECT * FROM combination_executions" + clause, parameters):
                executions.setdefault(execution["run_id"], {})[execution["execution_id"]] = dict(execution)
            for mapping in connection.execute(
                    "SELECT * FROM combination_attempt_executions" + clause, parameters):
                key = mapping["run_id"], mapping["condition_id"], mapping["attempt_index"]
                attempt_executions[key] = mapping["execution_id"]
    output = []
    plan_cache = {}
    cleanup_by_run = {}
    runs = []
    for run in run_rows:
        if run["schema_version"] not in {1, SCHEMA_VERSION}:
            raise ValueError("Unsupported combination schema")
        cleanup = json.loads(run["cleanup_json"] or "{}")
        cleanup_by_run[run["run_id"]] = cleanup
        runs.append({"run_id": run["run_id"], "schema_version": run["schema_version"],
                     "created_at_utc": run["created_at_utc"], "status": run["status"],
                     "error": run["error"], "cleanup": _snapshot_json_value(cleanup),
                     "final": run["status"] == "completed" and cleanup.get("clean") is True})
    source_path = str(Path(path).resolve())
    for row in rows:
        if row["schema_version"] not in {1, SCHEMA_VERSION}:
            raise ValueError("Unsupported combination schema")
        sample = json.loads(row["payload_json"])
        cleanup = cleanup_by_run[row["run_id"]]
        accepted = (row["attempt_status"] == "accepted" and row["run_status"] == "completed"
                    and cleanup.get("clean") is True and sample.get("clean") is True)
        preliminary = (preview and row["run_status"] == "active"
                       and row["attempt_status"] == "accepted" and sample.get("clean") is True)
        if not audit and not accepted and not preliminary:
            continue
        context = json.loads(row["context_json"])
        run_executions = executions.get(row["run_id"], {})
        attempt_key = row["run_id"], row["condition_id"], row["attempt_index"]
        if run_executions or attempt_key in attempt_executions:
            execution_id = attempt_executions.get(attempt_key)
            if execution_id not in run_executions:
                raise ValueError("Formal sample lacks its archived execution/configuration")
            execution = run_executions[execution_id]
            plan_json = execution["plan_json"]
            provenance = {
                "execution_id": execution_id, "execution_index": execution["sequence_index"],
                "execution_kind": execution["kind"], "execution_status": execution["status"],
                "execution_error": execution["error"],
                "execution_cleanup": json.loads(execution["cleanup_json"] or "{}"),
            }
        else:
            # Schema-1 records remain readable without writing a migration. No
            # historical execution identity is invented by this read-only path.
            plan_json = row["plan_json"]
            provenance = {"execution_id": None, "execution_index": 0,
                "execution_kind": "legacy_unsegmented", "execution_status": row["run_status"],
                "execution_error": row["run_error"], "execution_cleanup": cleanup}
        plan_key = row["run_id"], provenance["execution_id"]
        if plan_key not in plan_cache:
            execution_plan = json.loads(plan_json)
            plan_sha256 = hashlib.sha256(json.dumps(execution_plan, sort_keys=True,
                ensure_ascii=False, allow_nan=False).encode()).hexdigest()
            plan_cache[plan_key] = execution_plan, plan_sha256, _lockin_range_bindings(execution_plan)
        execution_plan, plan_sha256, range_bindings = plan_cache[plan_key]
        config_sha256 = execution_plan.get("config_sha256")
        provenance.update(config_sha256=config_sha256, plan_sha256=plan_sha256,
            configuration_revision=config_sha256 or plan_sha256,
            source_sha256=execution_plan.get("source_sha256", {}))
        record = {
            "schema_version": row["schema_version"], "source_path": source_path,
            "run_id": row["run_id"], "condition_id": row["condition_id"],
            "attempt_index": row["attempt_index"], "sample_index": row["sample_index"],
            "accepted": accepted, "run_status": row["run_status"], "run_error": row["run_error"],
            "preview": bool(preliminary),
            "attempt_status": row["attempt_status"], "clean": sample["clean"],
            "sequence_index": context["sequence_index"], "repeat_index": context["repeat_index"],
            "loop_order": context["loop_order"],
            "started_at_utc": sample["started_at_utc"], "finished_at_utc": sample["finished_at_utc"],
            "simulated": sample["simulated"],
            **provenance,
            **_flatten("axes", context["axes"]), **_flatten("requested", context["requested"]),
            **_flatten("actual", sample["actual"]), **_flatten("measured", sample["measurements"]),
        }
        for reading in sample["reads"]:
            record[f"timestamps.{reading['module']}"] = reading["captured_at_utc"]
            record[f"status.{reading['module']}"] = reading.get("status")
            if reading["module"] == "lockin":
                record.update(_lockin_range_columns(range_bindings, row["condition_id"], reading))
        output.append(record)
    materialized = tuple(output)
    for run in runs:
        run["row_count"] = sum(row["run_id"] == run["run_id"] for row in materialized)
    metadata = {
        "schema": "combination-analysis-snapshot-v1", "source_path": source_path,
        "run_ids": list(selected_ids) if selected_ids is not None else [run["run_id"] for run in runs],
        "runs": runs, "row_count": len(materialized), "rows_sha256": _snapshot_digest(materialized),
        "audit": bool(audit), "preview": bool(preview),
    }
    metadata["snapshot_id"] = _snapshot_digest(metadata)
    metadata["loaded_at_utc"] = datetime.now(timezone.utc).isoformat()
    return CombinationSnapshot(materialized, metadata)


def load_combination_rows(path: str | Path, *, run_id: str | None = None,
                          audit: bool = False) -> tuple[dict, ...]:
    """Compatibility API retaining the final-only default and audit opt-in."""
    return load_combination_snapshot(path, run_ids=None if run_id is None else (run_id,),
                                     audit=audit).rows


def execution_provenance(rows: Sequence[Mapping]) -> tuple[dict, ...]:
    """Compact archived segment provenance for exports; never use today's TOML."""
    segments = {}
    fields = ("source_path", "run_id", "execution_id", "execution_index", "execution_kind",
              "execution_status", "execution_error", "execution_cleanup", "configuration_revision",
              "config_sha256", "plan_sha256", "source_sha256")
    for row in rows:
        if "execution_kind" not in row:
            continue
        key = tuple(row.get(name) for name in
                    ("source_path", "run_id", "execution_id", "configuration_revision"))
        segment = segments.setdefault(key, {name: row.get(name) for name in fields})
        segment["selected_row_count"] = segment.get("selected_row_count", 0) + 1
        if "lockin_range_configuration" in row:
            configurations = segment.setdefault("lockin_range_configurations", [])
            configuration = json.loads(row["lockin_range_configuration"])
            if configuration not in configurations:
                configurations.append(configuration)
    return tuple(segments.values())


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
            "status." + role: {"model": row.model, "lia_status": row.recorded_lia_status,
                              "lia_status_raw": row.lia_status_raw,
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
    protected = ["run_id", "configuration_revision", "lockin_range_configuration", "simulated", "repeat_index", "sample_index", "role", "harmonic"]
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
        "analysis_contract": "combination-v2-execution-provenance", "x": x, "y": y, "group_by": list(group_by),
        "filters": dict(filters or {}), "sort_x": sort_x, "audit": audit,
        "aggregation": "none", "missing": "omitted from scatter, never filled with zero",
        "source_paths": sorted({r["source_path"] for r in rows if "source_path" in r}),
        "selection_sha256": fingerprint, "selected_count": len(rows),
        "excluded_sample_ids": list(excluded_sample_ids),
        "configuration_grouping": "different archived configuration revisions remain separate series",
        "execution_segments": execution_provenance(rows),
        "analysis_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "series": [{"group": item.group, "sample_ids": [
            {k: row.get(k) for k in ("run_id", "condition_id", "attempt_index", "sample_index",
                                    "execution_id", "configuration_revision", "config_sha256",
                                    "role", "harmonic")} for row in item.rows]} for item in series],
    }
    (target / "selection_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    return target
