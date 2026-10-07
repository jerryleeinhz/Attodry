"""Read-only, schema-adapted plotting for multidimensional experiment data.

The module normalizes known project records and simple numeric CSV tables to a
wide, unit-labelled row model.  It never imports a hardware driver or modifies
an input file.  Unknown CSV column units are deliberately left unspecified.
"""

from __future__ import annotations

import copy
import csv
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
import hashlib
import html
import json
import math
import os
from pathlib import Path
import re
import textwrap
from typing import Any, Iterable, Mapping, Sequence

from .analysis_observations import observation_id

from .scientific_plotting import (
    OKABE_ITO_ON_WHITE,
    PUBLICATION_RASTER_DPI,
    PUBLICATION_WIDE_FIGSIZE,
    SERIES_MARKERS,
    export_publication_figure_set,
    outside_legend,
    publication_style,
    style_axis,
)


_LOCKIN_SCANS = frozenset({"frequency", "excitation", "frequency_excitation"})
_SMU_ROLES = ("smu_bias", "gate_top", "gate_bottom")
_SCALAR_TYPES = (str, int, float, bool)
_IGNORED_DIRECTORIES = frozenset({
    ".git", ".ipynb_checkpoints", "__pycache__", ".venv", "venv",
    "analysis_output", "site-packages",
})
_COLUMN_LABELS = {
    "temperature_k": ("Temperature", "K"),
    "requested_temperature_k": ("Temperature target", "K"),
    "sample_measurement_temperature_k": ("Formal-window temperature", "K"),
    "measurement_temperature_k": ("Formal-window temperature", "K"),
    "stability_measurement_temperature_k": ("Stability-window temperature", "K"),
    "condition_measurement_temperature_k": ("Condition temperature", "K"),
    "field_x_t": ("Bx", "T"),
    "field_z_t": ("Bz", "T"),
    "lockin_frequency_hz": ("Lock-in frequency", "Hz"),
    "lockin_excitation_v_rms": ("Lock-in excitation", "V RMS"),
    "lockin_current_a_rms": ("Estimated excitation current", "A RMS"),
    "elapsed_s": ("Elapsed time", "s"),
    "sequence_index": ("Sequence index", ""),
    "sample_index": ("Sample index", ""),
    "repeat_index": ("Repeat index", ""),
}


@dataclass(frozen=True, slots=True)
class PlotSource:
    """One recognized project record or a generic CSV table."""

    path: Path
    kind: str
    label: str
    status: str = "unknown"
    sample_count: int | None = None


@dataclass(frozen=True, slots=True)
class _LoadedSource:
    rows: tuple[dict[str, Any], ...]
    signature: tuple


def discover_plot_sources(directory: str | Path) -> tuple[PlotSource, ...]:
    """Discover supported records recursively without opening instruments."""

    root = Path(directory).expanduser().resolve()
    if root.is_file():
        source = _classify_source(root, root.parent)
        return () if source is None else (source,)
    if not root.is_dir():
        raise ValueError(f"Data directory does not exist: {root}")

    found: dict[Path, PlotSource] = {}
    smu_directories: set[Path] = set()
    files = tuple(_iter_files(root))
    for metadata_path in (path for path in files if path.name == "metadata.json"):
        run_dir = metadata_path.parent
        if not (run_dir / "data.csv").is_file():
            continue
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(metadata, dict) or not (
            "active_roles" in metadata or "hardware" in metadata
        ):
            continue
        source = _source(
            run_dir, "three_smu", root,
            str(metadata.get("status", "unknown")),
        )
        found[source.path] = source
        smu_directories.add(run_dir.resolve())

    for path in files:
        if path.name == "metadata.json":
            continue
        if path.suffix.lower() in {".sqlite", ".sqlite3", ".db"}:
            if _is_combination_database(path):
                source = _source(path, "combination_sqlite", root, "available")
                found[source.path] = source
            continue
        if path.suffix.lower() in {".json", ".jsonl"}:
            source = _classify_source(path, root)
            if source is not None:
                found[source.path] = source
            continue
        if path.suffix.lower() != ".csv":
            continue
        if path.parent.resolve() in smu_directories:
            continue
        source = _classify_source(path, root)
        if source is not None:
            found[source.path] = source

    summary_keys = {
        _temperature_pair_key(source.path)
        for source in found.values()
        if source.kind == "temperature_excitation"
    }
    for source in tuple(found.values()):
        if (
            source.kind == "temperature_excitation_csv"
            and _temperature_pair_key(source.path) in summary_keys
        ):
            found.pop(source.path, None)

    return tuple(sorted(found.values(), key=lambda item: (item.label.casefold(), str(item.path))))


def _iter_files(root: Path) -> Iterable[Path]:
    """Walk the selected data tree without descending into environments/exports."""

    for directory, child_directories, filenames in os.walk(root, followlinks=False):
        child_directories[:] = [
            name for name in child_directories
            if name.casefold() not in _IGNORED_DIRECTORIES
        ]
        parent = Path(directory)
        for name in filenames:
            yield parent / name


def _temperature_pair_key(path: Path) -> tuple[Path, str]:
    for suffix in (
        "_temperature_excitation_summary.json",
        "_temperature_excitation_formal_samples.csv",
    ):
        if path.name.endswith(suffix):
            return path.parent.resolve(), path.name[:-len(suffix)]
    return path.parent.resolve(), path.name


def _source(path: Path, kind: str, root: Path, status: str = "available") -> PlotSource:
    resolved = path.resolve()
    try:
        label = str(resolved.relative_to(root))
    except ValueError:
        label = resolved.name
    return PlotSource(resolved, kind, label, status)


def _classify_source(path: Path, root: Path) -> PlotSource | None:
    resolved = path.resolve()
    suffix = resolved.suffix.lower()
    if suffix in {".sqlite", ".sqlite3", ".db"}:
        return _source(resolved, "combination_sqlite", root, "available") if _is_combination_database(resolved) else None
    if suffix in {".json", ".jsonl"}:
        try:
            from .commissioning_analysis import load_commissioning_file

            payload = load_commissioning_file(resolved)
        except (OSError, ValueError, json.JSONDecodeError, UnicodeError):
            return None
        if isinstance(payload, dict):
            if payload.get("scan") in _LOCKIN_SCANS:
                status = "completed" if payload.get("completed") is True else "rejected/incomplete"
                return _source(resolved, "lockin_sweep", root, status)
            if payload.get("command") == "temperature-excitation-scan":
                return _source(resolved, "temperature_excitation", root, "summary")
        return None
    if suffix == ".csv":
        if resolved.name.endswith("_temperature_excitation_formal_samples.csv"):
            return _source(resolved, "temperature_excitation_csv", root, "formal samples")
        return _source(resolved, "generic_csv", root, "quality unclassified")
    return None


def _is_combination_database(path: Path) -> bool:
    try:
        from .combination_store import open_readonly

        connection = open_readonly(path)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            connection.close()
    except Exception:
        return False
    return {"combination_runs", "combination_conditions", "combination_samples"}.issubset(tables)


def load_plot_sources(
    sources: Iterable[PlotSource | str | Path],
    *,
    include_audit: bool = False,
    run_ids_by_source: Mapping[str, Sequence[str]] | None = None,
) -> tuple[dict[str, Any], ...]:
    """Adapt selected project records and numeric CSV rows to one wide schema.

    Project loaders keep their accepted/clean default.  ``include_audit`` opts
    into rejected/problem records where those source formats support it.
    Omitting run_ids_by_source preserves the historical all-runs API;
    supplying it loads only explicitly listed SQLite runs (an empty list loads none).
    """

    result: list[dict[str, Any]] = []
    for item in sources:
        source = item if isinstance(item, PlotSource) else _classify_one(Path(item))
        if source is None:
            raise ValueError(f"Unsupported data source: {item}")
        if source.kind == "combination_sqlite":
            from .combination_analysis import load_combination_rows

            if run_ids_by_source is None:
                rows = load_combination_rows(source.path, audit=include_audit)
            else:
                rows = (
                    row for run_id in dict.fromkeys(run_ids_by_source.get(str(source.path), ()))
                    for row in load_combination_rows(source.path, run_id=run_id, audit=include_audit)
                )
            result.extend(_decorate(source, rows))
        elif source.kind == "three_smu":
            from .combination_analysis import load_legacy_three_smu

            rows = load_legacy_three_smu(source.path, audit=include_audit)
            result.extend(_decorate(source, rows))
        elif source.kind == "temperature_excitation":
            from .temperature_excitation_analysis import load_temperature_excitation_samples

            rows = load_temperature_excitation_samples(
                source.path, sample_statuses=None if include_audit else ("clean",)
            )
            result.extend(_decorate(source, (_temperature_row(row) for row in rows)))
        elif source.kind == "temperature_excitation_csv":
            from .temperature_excitation_analysis import load_temperature_excitation_samples

            rows = load_temperature_excitation_samples(
                source.path, sample_statuses=None if include_audit else ("clean",)
            )
            result.extend(_decorate(source, (_temperature_row(row) for row in rows)))
        elif source.kind == "lockin_sweep":
            from .commissioning_analysis import load_sweep_samples

            rows = load_sweep_samples(
                source.path,
                include_rejected=include_audit,
                sample_statuses=None if include_audit else ("clean",),
            )
            result.extend(_decorate(source, (_lockin_row(row) for row in rows)))
        elif source.kind == "generic_csv":
            result.extend(_decorate(source, _generic_csv_rows(source.path)))
        else:
            raise ValueError(f"Unsupported source kind: {source.kind}")
    return tuple(result)


def _classify_one(path: Path) -> PlotSource | None:
    resolved = path.expanduser().resolve()
    if resolved.is_dir() and (resolved / "metadata.json").is_file():
        return _source(resolved, "three_smu", resolved.parent)
    if resolved.is_file():
        return _classify_source(resolved, resolved.parent)
    return None


def _decorate(source: PlotSource, rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for index, raw in enumerate(rows):
        row = dict(raw)
        row.setdefault("source_path", str(source.path))
        row.setdefault("source_name", source.path.name)
        row.setdefault("source_kind", source.kind)
        row.setdefault("run_id", source.path.stem)
        row.setdefault("row_index", index)
        if "sample_status" not in row:
            problems = row.get("problems")
            row["sample_status"] = (
                "problem" if problems else "clean" if row.get("clean", True) else "problem"
            )
        output.append(row)
    return output


def _temperature_row(row: Any) -> dict[str, Any]:
    role = f"lockin_{row.role}"
    return {
        "run_id": row.source_path,
        "condition_id": f"T{row.temperature_index}-L{row.point_index}",
        "sequence_index": row.temperature_index,
        "sample_index": row.sample_index,
        "repeat_index": 0,
        "accepted": not bool(row.problems),
        "clean": not bool(row.problems),
        "sample_status": "; ".join(row.statuses),
        "role": role,
        "harmonic": row.harmonic,
        "axes.temperature.index": row.temperature_index,
        "axes.lockin.index": row.point_index,
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
        "problems": row.problems,
        "lia_status_raw": row.lia_status_raw,
        "error_status": row.error_status,
        "status." + role: {"model": row.model, "lia_status": row.recorded_lia_status,
                          "lia_status_raw": row.lia_status_raw, "error_status": row.error_status},
    }


def _lockin_row(row: Any) -> dict[str, Any]:
    role = f"lockin_{row.role}"
    return {
        "run_id": row.source_path,
        "condition_id": f"{row.scan_type}-point-{row.point_index}",
        "sequence_index": row.point_index,
        "sample_index": row.sample_index,
        "repeat_index": 0,
        "accepted": row.record_status == "completed" and row.statuses == ("clean",),
        "clean": row.statuses == ("clean",),
        "sample_status": "; ".join(row.statuses),
        "scan_type": row.scan_type,
        "role": role,
        "harmonic": row.harmonic,
        "axes.lockin.index": row.point_index,
        "requested.lockin_frequency_hz": row.target_frequency_hz,
        "actual.lockin_frequency_hz": row.actual_frequency_hz,
        "requested.lockin_excitation_v_rms": row.source_v_rms,
        "actual.lockin_excitation_v_rms": row.sine_output_v_rms,
        "measured.lockin_current_a_rms": row.nominal_current_a_rms,
        "measured.lockin_reference_frequency_hz": row.reference_frequency_hz,
        f"measured.{role}_h{row.harmonic}_x_v": row.x_v,
        f"measured.{role}_h{row.harmonic}_y_v": row.y_v,
        f"measured.{role}_h{row.harmonic}_amplitude_v": row.amplitude_v,
        f"measured.{role}_h{row.harmonic}_phase_deg": row.phase_deg,
        "lia_status_raw": row.lia_status_raw,
        "error_status": row.error_status,
        "status." + role: {"model": row.model, "lia_status": row.recorded_lia_status,
                          "lia_status_raw": row.lia_status_raw, "error_status": row.error_status},
        "problems": row.problems,
    }


def _generic_csv_rows(path: Path) -> tuple[dict[str, Any], ...]:
    """Treat every CSV column as data without guessing unit or field meaning."""

    rows: list[dict[str, Any]] = []
    with path.open("r", newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames:
            raise ValueError(f"CSV has no header: {path}")
        for index, raw in enumerate(reader):
            row: dict[str, Any] = {
                "run_id": path.stem,
                "row_index": index,
                "sequence_index": index,
                "sample_status": "unclassified",
                "accepted": None,
            }
            for key, value in raw.items():
                if key is None:
                    continue
                row[f"csv.{key}"] = _csv_scalar(value)
            rows.append(row)
    return tuple(rows)


def _csv_scalar(value: str | None) -> Any:
    if value is None or not value.strip():
        return None
    stripped = value.strip()
    try:
        number = float(stripped)
    except ValueError:
        return stripped
    return number if math.isfinite(number) else None


def _is_numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def numeric_columns(rows: Sequence[Mapping[str, Any]]) -> tuple[tuple[str, str], ...]:
    """Return numeric columns with at least one finite value, labelled with units."""

    keys = sorted({key for row in rows for key in row})
    return tuple(
        (column_label(key), key)
        for key in keys
        if any(_is_numeric(row.get(key)) for row in rows)
    )


def scalar_columns(rows: Sequence[Mapping[str, Any]]) -> tuple[tuple[str, str], ...]:
    keys = sorted({key for row in rows for key in row})
    return tuple(
        (column_label(key), key)
        for key in keys
        if any(_scalar(row.get(key)) for row in rows)
    )


def column_values(rows: Sequence[Mapping[str, Any]], key: str) -> tuple[Any, ...]:
    values = { _value_key(row.get(key)): row.get(key) for row in rows if _scalar(row.get(key)) }
    return tuple(sorted(values.values(), key=_sort_value))


def column_label(key: str) -> str:
    """Readable variable label with actual/requested and channel identity."""

    prefix, dot, suffix = key.partition(".")
    status = {"requested": "target", "actual": "readback", "measured": "measured"}
    if prefix == "csv" and dot:
        return f"{suffix} (unit unspecified)"
    if prefix in {"requested", "actual", "measured", "axes"}:
        if prefix == "axes":
            name = suffix.replace(".", " ").replace("_", " ")
            return name
        label, unit = _known_label(suffix)
        qualifier = status.get(prefix, "")
        text = f"{qualifier} {label}".strip()
        return f"{text} ({unit})" if unit else text
    names = {
        "role": "Lock-in role",
        "harmonic": "Harmonic order",
        "repeat_index": "Repeat index",
        "sample_index": "Sample index",
        "sequence_index": "Sequence index",
        "elapsed_s": "Elapsed time (s)",
        "segment": "Scan segment",
        "direction": "Scan direction",
        "sample_status": "Sample status",
    }
    return names.get(key, key.replace("_", " ").replace(".", " "))


def _known_label(name: str) -> tuple[str, str]:
    if name in _COLUMN_LABELS:
        return _COLUMN_LABELS[name]
    harmonic = re.fullmatch(r"(lockin_(?:xx|xy))_h([123])_(x_v|y_v|amplitude_v|phase_deg)", name)
    if harmonic:
        channel, order, field = harmonic.groups()
        channel_label = "Lock-in XX" if channel.endswith("xx") else "Lock-in XY"
        field_name, unit = {
            "x_v": ("X", "V"), "y_v": ("Y", "V"),
            "amplitude_v": ("R", "V"), "phase_deg": ("Phase", "deg"),
        }[field]
        return f"{channel_label} h{order} {field_name}", "V RMS" if field == "amplitude_v" else unit
    smu = re.fullmatch(r"(smu_bias|gate_top|gate_bottom)_(.+)", name)
    if smu:
        role, field = smu.groups()
        role_label = {"smu_bias": "SMU bias", "gate_top": "Top gate", "gate_bottom": "Bottom gate"}[role]
        field_map = {
            "voltage_v": ("Voltage", "V"), "current_a": ("Current", "A"),
            "resistance_ohm": ("Resistance", "Ω"), "conductance_s": ("Conductance", "S"),
            "v": ("Voltage source", "V"), "a": ("Current source", "A"),
        }
        field_label, unit = field_map.get(field, (field.replace("_", " "), ""))
        return f"{role_label} {field_label}", unit
    suffix_units = {
        "_v_rms": "V RMS", "_current_a_rms": "A RMS", "_voltage_v": "V",
        "_current_a": "A", "_resistance_ohm": "Ω", "_conductance_s": "S",
        "_frequency_hz": "Hz", "_temperature_k": "K", "_field_x_t": "T",
        "_field_z_t": "T", "_phase_deg": "deg", "_elapsed_s": "s",
        "_voltage_v_rms": "V RMS", "_current_a_rms": "A RMS",
    }
    unit = next((value for suffix, value in suffix_units.items() if name.endswith(suffix)), "")
    return name.replace("_", " "), unit


def _scalar(value: Any) -> bool:
    return isinstance(value, _SCALAR_TYPES) and not (isinstance(value, float) and not math.isfinite(value))


def _value_key(value: Any) -> tuple[str, Any]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return ("number", float(value))
    return (type(value).__name__, value)


def _sort_value(value: Any) -> tuple[int, Any]:
    if isinstance(value, bool):
        return (2, str(value))
    if isinstance(value, (int, float)):
        return (0, float(value))
    return (1, str(value).casefold())


def _filter_rows(
    rows: Sequence[Mapping[str, Any]], filters: Mapping[str, Sequence[Any]] | None
) -> tuple[dict[str, Any], ...]:
    selected = []
    for row in rows:
        if all(
            any(row.get(key) == value for value in values)
            for key, values in (filters or {}).items()
        ):
            selected.append(dict(row))
    return tuple(selected)


def _dimension_module(key: str) -> str | None:
    if key.startswith("axes."):
        parts = key.split(".")
        return parts[1] if len(parts) > 2 else None
    bare = key.split(".", 1)[-1] if key.startswith(("requested.", "actual.")) else key
    if bare in {"temperature_k", "requested_temperature_k"}:
        return "temperature"
    if bare in {"field_x_t", "field_z_t", "bx_t", "bz_t"}:
        return "magnetic"
    if any(bare.startswith(role + "_") for role in _SMU_ROLES):
        return "smu"
    if bare.startswith("lockin_"):
        return "lockin"
    if bare.startswith("csv."):
        return "csv"
    return None


def _dimension_token(key: str) -> str | None:
    if key.startswith(("requested.", "actual.")):
        return key.split(".", 1)[1]
    if key.startswith("axes.") and key.endswith(".index"):
        return key
    if key.startswith("csv."):
        return key
    return None


def _condition_columns(rows: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    keys = {key for row in rows for key in row}
    condition = {
        key for key in keys
        if key.startswith(("requested.", "axes."))
        and not key.endswith((".segment", ".direction"))
    }
    condition.update(
        key for key in keys
        if key in {"role", "harmonic", "scan_type", "source_kind"}
    )
    return tuple(sorted(condition))


def _coordinate_coverage(keys: Iterable[str]) -> tuple[set[str], set[str]]:
    tokens: set[str] = set()
    modules: set[str] = set()
    for key in keys:
        module = _dimension_module(key)
        if module:
            modules.add(module)
        token = _dimension_token(key)
        if token:
            tokens.add(token)
    return tokens, modules


def _unresolved_dimensions(
    rows: Sequence[Mapping[str, Any]],
    *,
    plot_keys: Sequence[str],
    filter_keys: Iterable[str],
    group_keys: Iterable[str],
) -> list[tuple[str, int]]:
    tokens, modules = _coordinate_coverage((*plot_keys, *group_keys))
    from .combination_analysis import _coordinate_for_x
    requested = {k[10:] for r in rows for k in r if k.startswith("requested.")}
    for key in (*plot_keys, *group_keys):
        coordinate = _coordinate_for_x(key, requested)
        if coordinate:
            tokens.add(coordinate)
            module = _dimension_module("requested." + coordinate)
            if module:
                modules.add(module)
    covered_keys = set(plot_keys) | set(group_keys)
    fixed_by_filter = set()
    for key in filter_keys:
        if len({_value_key(v) for v in column_values(rows, key)}) == 1:
            fixed_by_filter.add(key)
            token = _dimension_token(key)
            if token:
                tokens.add(token)
    unresolved = []
    for key in _condition_columns(rows):
        values = column_values(rows, key)
        if len(values) <= 1 or key in fixed_by_filter:
            continue
        if key in covered_keys:
            continue
        token = _dimension_token(key)
        module = _dimension_module(key)
        if token in tokens:
            continue
        if key.startswith("axes.") and module in modules:
            continue
        unresolved.append((key, len(values)))
    return unresolved


def _sample_token(row):
    return json.dumps(observation_id(row), sort_keys=True, ensure_ascii=False)


def _canonical_sample_token(token):
    """Migrate old positional tokens only when a recorded formal identity exists."""
    try:
        identity = json.loads(token)
    except (TypeError, ValueError):
        return token
    return _sample_token(identity) if isinstance(identity, Mapping) else token


def _audit_title(rows):
    flags = []
    if any(r.get("accepted") is False for r in rows):
        flags.append("AUDIT: rejected/problem records")
    if any(r.get("simulated") is True for r in rows):
        flags.append("SIMULATED" if all(r.get("simulated") is True for r in rows) else "MIXED simulated/measured")
    return " · ".join(flags) + "\n" if flags else ""


def _draw_curve(axis, rows, spec, x, y):
    from .analysis_observations import qualify_observations, repeat_statistics, observation_id
    mode = spec.get("statistics", "raw")
    kept, quality = qualify_observations([r for r in rows if not r.get("_manual_excluded")], (x, y), spec.get("quality_policy", "exclude"))
    token = lambda row: json.dumps(observation_id(row), sort_keys=True)
    kept_ids = {token(row) for row in kept}
    # Retain excluded/missing positions as NaN gaps; never join across them.
    masked = [{**row, y: None} if token(row) not in kept_ids else row for row in rows]
    groups = _curve_groups(masked, spec.get("group_by"), separate_samples=mode == "raw")
    summaries = []
    valid = [row for row in kept if _is_numeric(row.get(x)) and _is_numeric(row.get(y))]
    for index, (identity, group) in enumerate(groups.items()):
        points = repeat_statistics(group, x=x, y=y, mode=mode)
        summaries.extend({**p, "group": dict(identity), "channel": y} for p in points)
        label = _series_label(identity, bool(identity))
        color = OKABE_ITO_ON_WHITE[index % len(OKABE_ITO_ON_WHITE)]
        xs = [p["x"] if _is_numeric(p["x"]) else float("nan") for p in points]
        ys = [p["y"] if _is_numeric(p["y"]) else float("nan") for p in points]
        errors = [p["error"] if _is_numeric(p["error"]) else float("nan") for p in points]
        style = spec.get("curve_style", "points")
        if style not in ("points", "line", "line+points"):
            raise ValueError("Unknown curve style")
        axis.errorbar(xs, ys, yerr=errors if mode != "raw" else None, color=color,
                      marker=SERIES_MARKERS[index % len(SERIES_MARKERS)] if style != "line" else None,
                      linestyle="-" if style != "points" else "none", markersize=4,
                      capsize=2, linewidth=1, label=label)
    axis.set(xlabel=column_label(x), ylabel=column_label(y))
    for name, key in (("x", x), ("y", y)):
        scale = spec.get(name + "_scale", "linear")
        if scale not in ("linear", "log"):
            raise ValueError("Axis scale must be linear or log.")
        if scale == "log":
            if any(r[key] <= 0 for r in valid):
                raise ValueError(f"Log {name.upper()} requires positive values; use a linear axis for signed data.")
            if name == "y" and any(_is_numeric(p["y"]) and _is_numeric(p["error"]) and p["y"]-p["error"] <= 0 for p in summaries):
                raise ValueError("Log Y would clip an error interval crossing zero; use a linear axis.")
            getattr(axis, "set_" + name + "scale")("log")
    note = "Raw observations" if mode == "raw" else "Mean ± " + ("SD" if mode == "mean_sd" else "SEM") + " (within condition)"
    ns = [p["n"] for p in summaries if p["n"] > 0]
    if mode != "raw" and ns:
        note += f" · n={min(ns)}" + (f"–{max(ns)}" if min(ns) != max(ns) else "")
    quality_note = f"excluded {quality['excluded_quality_count']}/{len(rows)}; unknown status {quality['unknown_status_count']}"
    if quality["quality_policy"] == "include" and quality["flagged_row_count"]:
        quality_note = f"AUDIT: {quality['flagged_row_count']} flagged samples included"
    axis.set_title((str(spec.get("title", "")).strip() + "\n" if spec.get("title") else "") + note + "\n" + quality_note, fontsize=10)
    if not valid:
        axis.text(.5, .5, "No qualified observations", ha="center", va="center", transform=axis.transAxes)
        measured_x = [r[x] for r in rows if _is_numeric(r.get(x))]
        if measured_x and min(measured_x) != max(measured_x):
            axis.set_xlim(min(measured_x), max(measured_x))
    style_axis(axis)
    legend_labels = []
    if spec.get("show_legend", True) and (len(groups) > 1 or spec.get("group_by")):
        handles, labels = axis.get_legend_handles_labels()
        legend_labels = labels
        columns = max(1, math.ceil(len(labels) / 16))
        wrapped = [textwrap.fill(label, width=44, break_on_hyphens=False) for label in labels]
        # Reserve real figure space: large legends must not shrink the data panel.
        entries_per_column = math.ceil(len(wrapped) / columns)
        column_heights = [sum(len(label.splitlines()) * 7 * 1.2 + 7 * .4
                              for label in wrapped[start:start + entries_per_column]) / 72
                          for start in range(0, len(wrapped), entries_per_column)]
        grid = axis.get_subplotspec().get_gridspec()
        width, height = axis.figure.get_size_inches()
        axis.figure.set_size_inches(max(width, (7 + 3.2 * columns) * grid.ncols),
                                    max(height, (max(column_heights, default=0) + 1.5) * grid.nrows, 5.5))
        outside_legend(axis, handles, wrapped, ncols=columns, fontsize=7,
                       columnspacing=1.1)
    report = {"manual_excluded_sample_ids": list(spec.get("excluded_sample_ids", ())), **quality, "selected_row_count": len(rows), "plotted_row_count": len(valid),
              "omitted_missing_or_nonfinite_count": len(kept)-len(valid),
              "displayed_point_count": sum(_is_numeric(p["y"]) for p in summaries),
              "statistics": mode, "legend_labels": legend_labels,
              "show_legend": bool(spec.get("show_legend", True)),
              "replication_unit": "formal repeats within the same source/run/condition/attempt",
              "uncertainty": "sample SD (ddof=1); SEM=SD/sqrt(n), independence assumed; undefined for n<2",
              "undefined_error_point_count": sum(p['n'] == 1 for p in summaries) if mode != 'raw' else 0,
              "source_paths": sorted({str(r.get("source_path")) for r in rows if r.get("source_path")})}
    return {"rows": tuple(valid), "summary_rows": summaries, "report": report}


def render_plot(rows: Sequence[Mapping[str, Any]], spec: Mapping[str, Any]):
    """Render qualified observations; preserve raw provenance and explicit gaps."""
    from .analysis_observations import qualify_observations
    mode = spec.get("mode", "curve")
    if mode not in ("curve", "xy_z", "field", "heatmap"):
        raise ValueError("Plot mode must be curve, xy_z, field or heatmap")
    x, y = spec.get("x"), spec.get("y")
    if not x or (mode != "field" and not y):
        raise ValueError("Choose the required axes first.")
    filters = spec.get("filters") or {}
    filtered = _filter_rows(rows, filters)
    excluded_ids = {_canonical_sample_token(token) for token in spec.get("excluded_sample_ids", ())}
    selected = tuple({**r, "_manual_excluded": True} if _sample_token(r) in excluded_ids else r for r in filtered)
    eligible = tuple(r for r in selected if not r.get("_manual_excluded"))
    if not eligible:
        raise ValueError("No data rows remain after the selected filters.")
    if mode == "heatmap":
        from .plotting_heatmap import render_heatmap
        return render_heatmap(selected, spec)
    z = spec.get("z") if mode == "xy_z" else None
    plot_keys = [k for k in (x, y, z) if k]
    if mode == "field":
        field = spec.get("field_component", "amplitude_v")
        if field not in ("amplitude_v", "x_v", "y_v", "phase_deg"):
            raise ValueError("Unsupported channel component")
        plot_keys = [x] + [f"measured.lockin_{r}_h{h}_{field}" for r in ("xx", "xy") for h in (1, 2)]
    group_keys = [spec["group_by"]] if spec.get("group_by") else []
    if mode != "xy_z":
        group_keys += ["source_path", "run_id", "segment", "direction", "repeat_index", "role", "harmonic", "scan_type", "source_kind"]
    unresolved = _unresolved_dimensions(eligible, plot_keys=plot_keys, filter_keys=filters, group_keys=group_keys)
    if unresolved:
        raise ValueError("Unresolved varying conditions: " + ", ".join(column_label(k) for k, _ in unresolved) + ". Choose a grouping variable or fixed-condition filter.")
    with publication_style():
        import matplotlib as mpl
        import matplotlib.pyplot as plt
        figure = None
        try:
            if mode == "field":
                figure, axes = plt.subplots(2, 2, figsize=(12, 8), layout="constrained")
                reports, raw, summary = {}, [], []
                for axis, key in zip(axes.flat, plot_keys[1:]):
                    panel = _draw_curve(axis, selected, {**spec, "title": column_label(key)}, x, key)
                    reports[key] = panel["report"]
                    raw.extend({**r, "plot_channel": key} for r in panel["rows"])
                    summary.extend(panel["summary_rows"])
                figure.suptitle(_audit_title(selected) + (spec.get("title") or "Vxx / Vxy · h1 / h2"))
                report = {"manual_excluded_sample_ids": list(spec.get("excluded_sample_ids", ())), "mode": mode, "channels": reports, "selected_row_count": len(selected),
                          "plotted_row_count": len(raw), "excluded_quality_count": sum(r['excluded_quality_count'] for r in reports.values()),
                          "omitted_missing_or_nonfinite_count": sum(r['omitted_missing_or_nonfinite_count'] for r in reports.values()),
                          "statistics": spec.get("statistics", "raw"),
                          "source_paths": sorted({p for r in reports.values() for p in r['source_paths']})}
                return figure, {"rows": tuple(raw), "summary_rows": summary, "report": report}
            figure, axis = plt.subplots(figsize=PUBLICATION_WIDE_FIGSIZE, layout="constrained")
            if mode == "curve":
                data = _draw_curve(axis, selected, spec, x, y)
                axis.set_title(_audit_title(selected) + axis.get_title())
                data["report"]["mode"] = mode
                return figure, data
            if spec.get("statistics", "raw") != "raw":
                raise ValueError("XY–Z shows raw observations; choose Raw statistics.")
            if not z:
                raise ValueError("Choose Color Z first.")
            qualified, quality = qualify_observations(eligible, (x, y, z), spec.get("quality_policy", "exclude"))
            valid = [r for r in qualified if all(_is_numeric(r.get(k)) for k in (x, y, z))]
            if not valid:
                raise ValueError("No qualified finite numeric samples for the chosen axes.")
            for name, key in (("x", x), ("y", y)):
                scale = spec.get(name + "_scale", "linear")
                if scale not in ("linear", "log"):
                    raise ValueError("Axis scale must be linear or log.")
                if scale == "log" and any(r[key] <= 0 for r in valid):
                    raise ValueError("Log axes require positive coordinates.")
                getattr(axis, "set_" + name + "scale")(scale)
            minimum, maximum = min(r[z] for r in valid), max(r[z] for r in valid)
            if minimum == maximum:
                pad = abs(minimum)*.05 or 1
                minimum, maximum = minimum-pad, maximum+pad
            norm = mpl.colors.TwoSlopeNorm(0, vmin=minimum, vmax=maximum) if minimum < 0 < maximum else mpl.colors.Normalize(minimum, maximum)
            artist = axis.scatter([r[x] for r in valid], [r[y] for r in valid], c=[r[z] for r in valid], cmap="RdBu_r", norm=norm)
            figure.colorbar(artist, ax=axis).set_label(column_label(z))
            overlap = len(valid)-len({(r[x],r[y]) for r in valid})
            axis.set(xlabel=column_label(x), ylabel=column_label(y), title=(spec.get("title") or "Measured XY observations") + f"\n{overlap} overlapping repeats; excluded {quality['excluded_quality_count']}")
            if quality['quality_policy'] == 'include' and quality['flagged_row_count']:
                axis.set_title(axis.get_title() + " · AUDIT: flagged included")
            axis.set_title(_audit_title(selected) + axis.get_title())
            style_axis(axis)
            return figure, {"rows": tuple(valid), "summary_rows": [], "report": {"manual_excluded_sample_ids": list(spec.get("excluded_sample_ids", ())), **quality, "mode": mode,
                "selected_row_count": len(selected), "plotted_row_count": len(valid),
                "omitted_missing_or_nonfinite_count": len(qualified)-len(valid), "statistics": "raw",
                "duplicate_xy_observation_count": overlap, "color_normalization": {"vmin": minimum, "vmax": maximum, "vcenter": 0 if minimum < 0 < maximum else None},
                "source_paths": sorted({str(r['source_path']) for r in selected if r.get('source_path')})}}
        except Exception:
            if figure is not None:
                plt.close(figure)
            raise

def _curve_groups(
    rows: Sequence[Mapping[str, Any]], group_by: str | None, *, separate_samples=True
) -> dict[tuple[tuple[str, Any], ...], list[Mapping[str, Any]]]:
    automatic_keys = [
        "source_path", "simulated", "run_id", "segment", "direction", "repeat_index", "role",
        "harmonic", "scan_type", "source_kind",
    ]
    if separate_samples:
        automatic_keys.append("sample_index")
    automatic_keys += sorted({k for r in rows for k in r if k.startswith("axes.") and k.endswith((".segment", ".direction"))})
    automatic_keys = [
        key for key in automatic_keys
        if len({_value_key(row.get(key)) for row in rows if _scalar(row.get(key))}) > 1
    ]
    keys = list(dict.fromkeys(([group_by] if group_by else []) + automatic_keys))
    result: dict[tuple[tuple[str, Any], ...], list[Mapping[str, Any]]] = {}
    for row in rows:
        identity = tuple((key, row.get(key)) for key in keys)
        result.setdefault(identity, []).append(row)
    return result


def _series_label(identity: tuple[tuple[str, Any], ...], show: bool) -> str:
    if not show:
        return "observations"
    parts = []
    for key, value in identity:
        if key == "run_id":
            parts.append(Path(str(value)).name)
        elif key == "repeat_index" and isinstance(value, (int, float)):
            parts.append(f"repeat {int(value) + 1}")
        elif key == "harmonic":
            parts.append(f"h{value}")
        elif key == "role":
            parts.append(str(value).replace("lockin_", "").upper())
        elif key in {"segment", "direction"}:
            parts.append(f"{column_label(key)}={value}")
        else:
            parts.append(f"{column_label(key)}={_format_value(value)}")
    return ", ".join(parts) or "observations"


def _format_value(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def export_plot_bundle(
    directory: str | Path,
    rendered: Sequence[Mapping[str, Any]],
) -> Path:
    """Export all rendered figures and their exact selected samples to a new folder."""

    if not rendered:
        raise ValueError("Render at least one plot before exporting.")
    base = Path(directory).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    destination = base / timestamp
    destination.mkdir(exist_ok=False)

    csv_rows: list[dict[str, Any]] = []
    plot_manifest = []
    summary_rows = []
    for index, item in enumerate(rendered, start=1):
        slug = f"plot_{index:02d}"
        with publication_style():
            paths = export_publication_figure_set(
                item["figure"], destination / slug, formats=("png", "pdf", "svg")
            )
        summary_rows.extend({**row, "plot_id": slug} for row in item["data"].get("summary_rows", ()))
        selected_rows = tuple(item["data"]["rows"])
        csv_rows.extend(
            {**dict(row), "plot_id": slug}
            for row in selected_rows
        )
        plot_manifest.append({
            "plot_id": slug,
            "spec": _json_safe(item["spec"]),
            "report": _json_safe(item["data"]["report"]),
            "figure_files": [path.name for path in paths],
        })

    all_keys = sorted({key for row in csv_rows for key in row})
    with (destination / "selected_samples.csv").open(
        "x", newline="", encoding="utf-8"
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=all_keys)
        writer.writeheader()
        for row in csv_rows:
            writer.writerow({key: _csv_export_value(row.get(key)) for key in all_keys})
    summary_keys = sorted({key for row in summary_rows for key in row})
    with (destination / "plotted_statistics.csv").open("x", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=summary_keys)
        writer.writeheader()
        for row in summary_rows:
            writer.writerow({key: _csv_export_value(row.get(key)) for key in summary_keys})
    digest = hashlib.sha256(
        json.dumps(_json_safe(csv_rows), sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    manifest = {
        "schema": "unified-plot-export-v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "raster_dpi": PUBLICATION_RASTER_DPI,
        "vector_formats": ["pdf", "svg"],
        "figure_count": len(plot_manifest),
        "selected_sample_rows": len(csv_rows),
        "selected_samples_sha256": digest,
        "aggregation": "per plot specification; plotted_statistics.csv records n, SD, SEM and sample identities",
        "interpolation": "none",
        "plots": plot_manifest,
        "input_sources": sorted({
            source for plot in plot_manifest for source in plot["report"]["source_paths"]
        }),
        "statistics_source_sha256": hashlib.sha256(Path(__file__).with_name("analysis_observations.py").read_bytes()).hexdigest(),
        "plotting_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "heatmap_source_sha256": hashlib.sha256(Path(__file__).with_name("plotting_heatmap.py").read_bytes()).hexdigest(),
    }
    (destination / "plot_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, allow_nan=False),
        encoding="utf-8",
    )
    return destination


def _csv_export_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(_json_safe(value), ensure_ascii=False, sort_keys=True)
    return value


def _json_safe(value: Any) -> Any:
    if is_dataclass(value):
        return _json_safe(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


class _CheckboxSourceList:
    def __init__(self, widgets: Any, on_change: Any) -> None:
        self.widgets = widgets
        self.on_change = on_change
        self._controls: dict[str, Any] = {}
        self._labels: dict[str, str] = {}
        self._updating = False
        self.widget = widgets.VBox(
            [], layout=widgets.Layout(width="100%", max_height="250px", overflow="auto")
        )

    @property
    def value(self) -> tuple[str, ...]:
        return tuple(path for path, control in self._controls.items() if control.value)

    def set_sources(self, sources: Sequence[PlotSource], selected: Iterable[str] = ()) -> None:
        keep = set(map(str, selected))
        self._updating = True
        controls: dict[str, Any] = {}
        labels: dict[str, str] = {}
        children = []
        for source in sources:
            path = str(source.path)
            label = f"{source.label}  ·  {source.kind}  ·  {source.status}"
            checkbox = self.widgets.Checkbox(
                value=path in keep, indent=False,
                layout=self.widgets.Layout(width="24px", min_width="24px", flex="0 0 24px"),
            )
            checkbox.observe(self._changed, names="value")
            text = self.widgets.HTML(
                value=f"<span style='white-space:normal; overflow-wrap:anywhere'>{html.escape(label)}</span>",
                layout=self.widgets.Layout(width="100%"),
            )
            controls[path] = checkbox
            labels[path] = label
            children.append(self.widgets.HBox(
                [checkbox, text],
                layout=self.widgets.Layout(width="100%", align_items="flex-start", flex="0 0 auto", min_height="30px"),
            ))
        self._controls, self._labels = controls, labels
        self.widget.children = tuple(children) if children else (
            self.widgets.HTML("<i>No supported records found in this directory.</i>"),
        )
        self._updating = False

    def _changed(self, _change: Any) -> None:
        if not self._updating:
            self.on_change()


class UnifiedPlotDashboard:
    """Notebook UI for independent, multi-source curve and XY–Z plot cards."""

    def __init__(self, data_directory: str | Path) -> None:
        try:
            import ipywidgets as widgets
        except ImportError as exc:
            raise RuntimeError("The notebook UI requires ipywidgets>=8,<9.") from exc
        self.widgets = widgets
        self.catalog: tuple[PlotSource, ...] = ()
        self._source_by_path: dict[str, PlotSource] = {}
        self._cache: dict[tuple, _LoadedSource] = {}
        self._run_catalog: dict[str, tuple[dict, ...]] = {}
        self.cards: list[dict[str, Any]] = []
        self._next_card_id = 1

        layout = widgets.Layout(width="100%")
        self.directory = widgets.Text(
            value=str(Path(data_directory).expanduser()), description="Data directory:",
            layout=widgets.Layout(width="100%"),
        )
        self.output_directory = widgets.Text(
            value=str(Path(data_directory).expanduser().resolve().parent / "analysis_output" / "unified_plotting"),
            description="Export folder:", layout=widgets.Layout(width="100%"),
        )
        self.configuration_path = widgets.Text(
            value="plot_setup.json", description="Setup file:", layout=widgets.Layout(width="100%")
        )
        self.include_audit = widgets.Checkbox(
            value=False, description="Include rejected/problem records where supported"
        )
        self.status = widgets.HTML(value="Ready to browse records.")
        self.card_box = widgets.VBox([], layout=layout)
        self.refresh_button = widgets.Button(description="Refresh records", icon="refresh")
        self.add_curve_button = widgets.Button(description="New curve plot", icon="plus")
        self.add_field_button = widgets.Button(description="Four lock-in channels", icon="plus")
        self.add_map_button = widgets.Button(description="New XY–Z scatter", icon="plus")
        self.add_heatmap_button = widgets.Button(description="New heatmap", icon="plus")
        self.render_button = widgets.Button(description="Render all plots", button_style="primary")
        self.export_button = widgets.Button(description="Export figures + data", icon="download")
        self.save_button = widgets.Button(description="Save setup", icon="save")
        self.load_button = widgets.Button(description="Load setup", icon="upload")
        for button in (self.refresh_button, self.add_curve_button, self.add_map_button, self.add_heatmap_button,
                       self.add_field_button, self.render_button, self.export_button,
                       self.save_button, self.load_button):
            button.layout.width = "auto"
            button.layout.min_width = "120px"
        self.refresh_button.on_click(self._refresh)
        self.include_audit.observe(self._audit_changed, names="value")
        self.add_curve_button.on_click(lambda _button: self.add_plot("curve"))
        self.add_field_button.on_click(lambda _button: self.add_plot("field"))
        self.add_map_button.on_click(lambda _button: self.add_plot("xy_z"))
        self.add_heatmap_button.on_click(lambda _button: self.add_plot("heatmap"))
        self.render_button.on_click(self._render_all)
        self.export_button.on_click(self._export)
        self.save_button.on_click(self._save_setup)
        self.load_button.on_click(self._load_setup)
        self.widget = widgets.VBox([
            widgets.HTML("<h2>Unified multidimensional plotting</h2>"),
            self.directory,
            widgets.HBox([self.refresh_button, self.include_audit]),
            self.status,
            self.card_box,
            widgets.HBox([self.add_curve_button, self.add_map_button, self.add_heatmap_button, self.add_field_button, self.render_button], layout=widgets.Layout(flex_flow="row wrap")),
            self.output_directory,
            widgets.HBox([self.export_button]),
            self.configuration_path,
            widgets.HBox([self.save_button, self.load_button]),
        ], layout=layout)
        self._refresh()

    def add_plot(self, mode: str, saved: Mapping[str, Any] | None = None) -> None:
        if mode not in {"curve", "xy_z", "field", "heatmap"}:
            raise ValueError("Plot mode must be curve, xy_z, field or heatmap")
        widgets = self.widgets
        card_id = self._next_card_id
        self._next_card_id += 1
        default_spec = dict(saved or {})
        title = widgets.Text(
            value=str(default_spec.get("title", "")), description="Title:",
            layout=widgets.Layout(width="100%"),
        )
        checklist = _CheckboxSourceList(widgets, lambda: self._card_changed(card))
        runs = widgets.SelectMultiple(options=[], value=(), description="Run IDs:", rows=5,
                                      layout=widgets.Layout(width="100%"))
        axis = lambda description: widgets.Dropdown(options=[], description=description, layout=widgets.Layout(width="100%"))
        x_axis, y_axis = axis("X:"), axis("Y:")
        z_axis = axis("Color Z:")
        group_axis = widgets.Dropdown(
            options=[("None", "")], description="Stack/group:", disabled=mode in {"xy_z", "heatmap"},
            layout=widgets.Layout(width="100%")
        )
        show_legend = widgets.Checkbox(value=bool(default_spec.get("show_legend", True)),
                                      description="Show curve legend", disabled=mode in {"xy_z", "heatmap"})
        curve_style = widgets.Dropdown(
            options=[("Line + points", "line+points"), ("Points only", "points"), ("Lines only", "line")],
            value=str(default_spec.get("curve_style", "line+points")), description="Style:",
        )
        x_scale = widgets.Dropdown(
            options=[("Linear X", "linear"), ("Log X", "log")],
            value=str(default_spec.get("x_scale", "linear")), description="X scale:",
        )
        y_scale = widgets.Dropdown(
            options=[("Linear Y", "linear"), ("Log Y", "log")],
            value=str(default_spec.get("y_scale", "linear")), description="Y scale:",
        )
        statistics = widgets.Dropdown(
            options=[("Raw observations", "raw"), ("Mean ± SD", "mean_sd"), ("Mean ± SEM", "mean_sem")],
            value=default_spec.get("statistics", "mean_sd" if mode == "field" else "raw"), description="Statistics:",
            disabled=mode == "xy_z")
        quality = widgets.Dropdown(options=[("Exclude flagged channels", "exclude"), ("AUDIT: include flagged", "include")],
                                  value=default_spec.get("quality_policy", "exclude"), description="Quality:", layout=widgets.Layout(width="360px"))
        component = widgets.Dropdown(options=[("R (V RMS)", "amplitude_v"), ("X (V)", "x_v"), ("Y (V)", "y_v"), ("Phase (deg)", "phase_deg")],
                                     value=default_spec.get("field_component", "amplitude_v"), description="Component:")
        excluded = widgets.SelectMultiple(options=[], description="Exclude IDs:", rows=4, layout=widgets.Layout(width="100%"))
        filter_box = widgets.VBox([])
        add_filter_button = widgets.Button(description="Add fixed-condition filter", icon="filter")
        add_filter_button.layout.width = "auto"
        remove_button = widgets.Button(description="Delete this plot", button_style="danger", icon="trash")
        output = widgets.Output(layout=widgets.Layout(width="100%"))
        figure_image = widgets.Image(format="png", layout=widgets.Layout(width="100%"))
        render_card_button = widgets.Button(description="Render this plot", button_style="primary")
        card: dict[str, Any] = {
            "id": card_id, "mode": mode, "title": title, "sources": checklist,
            "runs": runs, "saved_runs": default_spec.get("run_ids_by_source"),
            "legacy_runs": saved is not None and "run_ids_by_source" not in default_spec,
            "legacy_run_filter": default_spec.get("filters", {}).get("run_id"),
            "x": x_axis, "y": y_axis, "z": z_axis, "group": group_axis,
            "curve_style": curve_style, "x_scale": x_scale, "y_scale": y_scale,
            "show_legend": show_legend,
            "filter_box": filter_box, "filters": [], "output": output,
            "figure": None, "rendered": None, "updating": False,
            "statistics": statistics, "quality": quality, "component": component, "excluded": excluded,
            "image": figure_image,
        }
        card["widget"] = widgets.VBox([
            widgets.HTML(f"<h3>Plot {card_id}: {'Four lock-in channels' if mode == 'field' else 'Curve / stacked' if mode == 'curve' else 'Grid heatmap' if mode == 'heatmap' else 'XY–Z color scatter'}</h3>"),
            title,
            widgets.HTML("<b>Choose one or more data sources</b>"),
            checklist.widget,
            runs,
            widgets.HTML("SQLite: select Run IDs before samples load (Ctrl selects multiple). "
                         "Loaded runs are cached until Refresh records; changing plot options reuses that snapshot."),
            widgets.HBox([x_axis, component if mode == "field" else y_axis], layout=widgets.Layout(width="100%")),
            group_axis if mode in {"curve", "field"} else z_axis,
            show_legend,
            widgets.HTML("Heatmap: recorded requested grid when available; runs and sweep segments are separate panels. Gray means missing/excluded. Raw requires one sample per cell; choose Mean for formal repeats. No interpolation." if mode == "heatmap" else "XY–Z scatter retains measured coordinates. Use a segment filter to view one hysteresis branch." if mode == "xy_z" else "Curve legends remain visible for more than 12 traces."),
            widgets.HBox([statistics, quality], layout=widgets.Layout(width="100%")),
            widgets.HTML("SD: scatter of formal repeats; SEM: SD/√n assumes independent repeats. Missing status stays unknown. n and exclusions are exported."),
            excluded,
            widgets.HBox([curve_style, x_scale, y_scale], layout=widgets.Layout(width="100%")),
            widgets.HTML("<b>Fix other changing conditions to avoid mixing dimensions</b>"),
            add_filter_button,
            filter_box,
            widgets.HBox([render_card_button, remove_button]),
            figure_image,
            output,
        ], layout=widgets.Layout(width="100%", border="1px solid #c8c8c8", padding="12px", margin="12px 0"))
        remove_button.on_click(lambda _button, target=card: self._remove_card(target))
        render_card_button.on_click(lambda _button, target=card: self._render_card(target))
        add_filter_button.on_click(lambda _button, target=card: self._add_filter(target))
        for control in (runs, x_axis, y_axis, z_axis, group_axis, curve_style, x_scale, y_scale, title, statistics, quality, component, excluded, show_legend):
            control.observe(lambda _change, target=card: self._card_changed(target), names="value")
        self.cards.append(card)
        self.card_box.children = tuple(item["widget"] for item in self.cards)
        checklist.set_sources(self.catalog, default_spec.get("source_paths", ()))
        self._update_card_options(card)
        for key, value in default_spec.get("filters", {}).items():
            self._add_filter(card, str(key), value)
        self._update_card_options(card)
        card["updating"] = True
        for key in ("x", "y", "z"):
            saved_value = default_spec.get(key)
            if saved_value in {value for _, value in card[key].options}:
                card[key].value = saved_value
        saved_group = default_spec.get("group_by")
        if saved_group in {value for _, value in card["group"].options}:
            card["group"].value = saved_group
        card["excluded"].value = tuple(dict.fromkeys(
            _canonical_sample_token(v) for v in default_spec.get("excluded_sample_ids", ())
            if _canonical_sample_token(v) in {value for _, value in excluded.options}
        ))
        card["updating"] = False

    def _remove_card(self, card: dict[str, Any]) -> None:
        if card in self.cards:
            if card.get("figure") is not None:
                self._close_figure(card["figure"])
            self.cards.remove(card)
            self.card_box.children = tuple(item["widget"] for item in self.cards)

    def _refresh(self, _button: Any = None) -> None:
        self._cache.clear()
        self._run_catalog.clear()
        for card in self.cards:
            self._invalidate_card(card)
        try:
            root = Path(self.directory.value).expanduser().resolve()
            catalog = discover_plot_sources(root)
            self.catalog = catalog
            self._source_by_path = {str(item.path): item for item in catalog}
            missing = set()
            for card in self.cards:
                selected = card["sources"].value
                missing.update(set(selected) - self._source_by_path.keys())
                card["sources"].set_sources(catalog, selected)
                self._update_card_options(card)
            self.status.value = f"Found {len(catalog)} supported data sources under {html.escape(str(root))}."
            if missing:
                self.status.value += " Previously selected sources are unavailable: " + html.escape(", ".join(sorted(missing)))
        except Exception as exc:
            self.catalog = ()
            self._source_by_path = {}
            for card in self.cards:
                card["sources"].set_sources(())
                self._update_card_options(card)
            self.status.value = f"<span style='color:#a40000'>{html.escape(str(exc))}</span>"

    def _audit_changed(self, _change: Any) -> None:
        self._cache.clear()
        for card in self.cards:
            self._invalidate_card(card)
            self._update_card_options(card)

    def _invalidate_card(self, card: dict[str, Any]) -> None:
        if card.get("figure") is not None:
            self._close_figure(card["figure"])
        card["figure"] = None
        card["rendered"] = None
        card["image"].value = b""
        card["output"].clear_output(wait=False)

    def _update_run_options(self, card: dict[str, Any]) -> None:
        from .combination_analysis import list_combination_runs

        options = []
        for path in card["sources"].value:
            source = self._source_by_path.get(path)
            if source is None:
                raise ValueError(f"Selected source is unavailable: {path}")
            if source.kind != "combination_sqlite":
                continue
            if path not in self._run_catalog:
                self._run_catalog[path] = list_combination_runs(source.path)
            options.extend(
                (f"{source.label} · {run['run_id']} · {run['status']} · {run['created_at_utc']}",
                 (path, run["run_id"])) for run in self._run_catalog[path]
            )
        selected = set(card["runs"].value)
        saved = card.pop("saved_runs", None)
        if saved is not None:
            selected = {(path, run_id) for path, run_ids in saved.items() for run_id in run_ids}
        elif card.pop("legacy_runs", False):
            # An old setup intentionally selected a whole database, or a run-id filter.
            keep = card.get("legacy_run_filter")
            selected = {value for _, value in options if keep is None or value[1] in keep}
        card["runs"].options = options
        card["runs"].value = tuple(value for _, value in options if value in selected)
        card["runs"].disabled = not options

    def _source_runs(self, card: dict[str, Any]) -> tuple[tuple[PlotSource, str | None], ...]:
        result = []
        for path in card["sources"].value:
            source = self._source_by_path.get(path)
            if source is None:
                raise ValueError(f"Selected source is unavailable: {path}")
            if source.kind == "combination_sqlite":
                result.extend((source, run_id) for selected_path, run_id in card["runs"].value
                              if selected_path == path)
            else:
                result.append((source, None))
        return tuple(result)

    def _selected_rows(self, card: dict[str, Any]) -> tuple[dict[str, Any], ...]:
        include_audit = bool(self.include_audit.value)
        result = []
        for source, run_id in self._source_runs(card):
            path = str(source.path)
            cache_key = (path, run_id, include_audit)
            if cache_key not in self._cache:
                signature = _source_signature(source.path)
                runs = {path: [run_id]} if run_id is not None else None
                rows = load_plot_sources([source], include_audit=include_audit, run_ids_by_source=runs)
                if _source_signature(source.path) != signature:
                    raise ValueError("A source changed while loading. Refresh records and try again.")
                self._cache[cache_key] = _LoadedSource(rows, signature)
            result.extend(copy.deepcopy(row) for row in self._cache[cache_key].rows)
        return tuple(result)

    def _loaded_signatures(self, card: dict[str, Any]) -> dict[str, tuple]:
        signatures = {}
        for source, run_id in self._source_runs(card):
            path = str(source.path)
            loaded = self._cache[(path, run_id, bool(self.include_audit.value))]
            if path in signatures and signatures[path] != loaded.signature:
                raise ValueError("Runs were loaded from different source snapshots. Refresh records.")
            signatures[path] = loaded.signature
        return signatures

    def _update_card_options(self, card: dict[str, Any]) -> None:
        if card.get("updating"):
            return
        card["updating"] = True
        try:
            self._update_run_options(card)
            rows = self._selected_rows(card)
            axes = numeric_columns(rows)
            scalars = scalar_columns(rows)
            for key in ("x", "y", "z"):
                control = card[key]
                old = control.value
                control.options = axes
                control.value = old if old in {value for _, value in axes} else (axes[0][1] if axes else None)
                if old is None and card["mode"] == "field" and key == "x":
                    preferred = next((k for k in ("actual.field_x_t", "actual.field_z_t", "requested.field_x_t", "requested.field_z_t") if k in {v for _, v in axes} and len(column_values(rows, k)) > 1), None)
                    if preferred:
                        control.value = preferred
            group = card["group"]
            old_group = group.value
            group.options = [("None", "")] + list(scalars)
            group.value = old_group if old_group in {value for _, value in scalars} else ""
            for filter_row in card["filters"]:
                self._update_filter_row(card, filter_row, force=True)
            old_excluded = card["excluded"].value
            card["excluded"].options = [(f"{r.get('source_name', '')} · condition {r.get('condition_id', r.get('row_index'))} · sample {r.get('sample_index', '')} {r.get('role', '')} {r.get('harmonic', '')}", _sample_token(r)) for r in rows]
            available_ids = {v for _, v in card["excluded"].options}
            card["excluded"].value = tuple(v for v in old_excluded if v in available_ids)
        except Exception as exc:
            card["output"].clear_output(wait=False)
            with card["output"]:
                print(f"Could not load selected data: {exc}")
        finally:
            card["updating"] = False

    def _card_changed(self, card: dict[str, Any]) -> None:
        if card.get("updating"):
            return
        self._invalidate_card(card)
        self._update_card_options(card)

    def _add_filter(
        self, card: dict[str, Any], key: str | None = None, values: Sequence[Any] | None = None
    ) -> None:
        widgets = self.widgets
        field = widgets.Dropdown(options=[("Choose dimension", "")], value="", description="Dimension:", layout=widgets.Layout(width="45%"))
        choices = widgets.SelectMultiple(options=[], value=(), rows=5, description="Keep:", layout=widgets.Layout(width="55%"))
        remove = widgets.Button(description="Remove", icon="trash", layout=widgets.Layout(width="100px"))
        row: dict[str, Any] = {"field": field, "choices": choices, "remove": remove, "saved_values": tuple(values) if values is not None else None, "last_key": None}
        row["widget"] = widgets.VBox([
            widgets.HBox([field, remove], layout=widgets.Layout(width="100%")), choices,
        ], layout=widgets.Layout(width="100%", padding="6px", border="1px solid #dedede"))
        card["filters"].append(row)
        field.observe(lambda _change, target=card, entry=row: self._update_filter_row(target, entry), names="value")
        field.observe(lambda _change, target=card: self._card_changed(target), names="value")
        choices.observe(lambda _change, target=card: self._card_changed(target), names="value")
        remove.on_click(lambda _button, target=card, entry=row: self._remove_filter(target, entry))
        card["filter_box"].children = tuple(item["widget"] for item in card["filters"])
        self._update_filter_row(card, row, preferred_key=key)

    def _remove_filter(self, card: dict[str, Any], row: dict[str, Any]) -> None:
        if row in card["filters"]:
            card["filters"].remove(row)
            card["filter_box"].children = tuple(item["widget"] for item in card["filters"])
            self._card_changed(card)

    def _update_filter_row(self, card, row, preferred_key=None, *, force=False):
        if card.get("updating") and not force:
            return
        was_updating = card["updating"]
        card["updating"] = True
        try:
            rows = self._selected_rows(card)
            options = scalar_columns(rows)
            field = row["field"]
            key = preferred_key or field.value
            field.options = [("Choose dimension", "")] + list(options)
            field.value = key if key in {k for _, k in options} else ""
            key = field.value
            values = column_values(rows, key) if key else ()
            saved = row.get("saved_values")
            if saved is not None:
                keep = saved
            elif row["last_key"] != key:
                keep = values
            else:
                keep = row["choices"].value
            row["choices"].options = [(_format_value(v), v) for v in values]
            row["choices"].value = tuple(v for v in values if v in keep)
            row["last_key"], row["saved_values"] = key, None
        finally:
            card["updating"] = was_updating

    def _card_spec(self, card: dict[str, Any]) -> dict[str, Any]:
        filters: dict[str, list[Any]] = {}
        for row in card["filters"]:
            key = row["field"].value
            if key:
                filters[key] = list(row["choices"].value)
        group = card["group"].value
        return {
            "mode": card["mode"], "title": card["title"].value,
            "x": card["x"].value, "y": card["y"].value, "z": card["z"].value,
            "group_by": group or None,
            "curve_style": card["curve_style"].value,
            "show_legend": bool(card["show_legend"].value),
            "x_scale": card["x_scale"].value, "y_scale": card["y_scale"].value,
            "filters": filters,
            "statistics": card["statistics"].value, "quality_policy": card["quality"].value,
            "field_component": card["component"].value,
            "excluded_sample_ids": list(card["excluded"].value),
            "run_ids_by_source": {
                path: [run_id for selected_path, run_id in card["runs"].value if selected_path == path]
                for path in card["sources"].value
                if self._source_by_path[path].kind == "combination_sqlite"
            },
            "include_audit": bool(self.include_audit.value),
        }

    def _render_card(self, card: dict[str, Any]) -> None:
        self._invalidate_card(card)
        card["output"].clear_output(wait=False)
        try:
            rows = self._selected_rows(card)
            spec = self._card_spec(card)
            spec["source_paths"] = list(card["sources"].value)
            spec["source_signatures"] = self._loaded_signatures(card)
            if any(_source_signature(Path(path)) != signature
                   for path, signature in spec["source_signatures"].items()):
                raise ValueError("A source changed since loading. Refresh records before rendering.")
            figure, data = render_plot(rows, spec)
            card["figure"] = figure
            card["rendered"] = {"figure": figure, "data": data, "spec": spec}
            import io
            image_bytes = io.BytesIO()
            figure.savefig(image_bytes, format="png", dpi=130, bbox_inches="tight")
            card["image"].value = image_bytes.getvalue()
            with card["output"]:
                report = data["report"]
                note = f"Displayed {report['plotted_row_count']} samples; omitted {report['omitted_missing_or_nonfinite_count']} with missing/non-finite axes."
                if report.get("duplicate_xy_observation_count"):
                    note += f" {report['duplicate_xy_observation_count']} XY observations overlap; all are retained."
                print(note + f" Quality exclusions: {report.get('excluded_quality_count', 0)}. Counts in four-channel mode are channel observations.")
                self._close_figure(figure)
        except Exception as exc:
            with card["output"]:
                print(f"Plot not rendered: {exc}")

    def _render_all(self, _button: Any = None) -> None:
        if not self.cards:
            self.status.value = "Add a plot card first."
            return
        for card in self.cards:
            self._render_card(card)
        self.status.value = "Rendering finished. Review each card for unresolved conditions or missing data."

    def _export(self, _button: Any = None) -> None:
        try:
            if any(card.get("rendered") is None for card in self.cards):
                raise ValueError("Render every plot card successfully before exporting.")
            for card in self.cards:
                expected = card["rendered"]["spec"]["source_signatures"]
                if any(_source_signature(Path(p)) != signature for p, signature in expected.items()):
                    self._invalidate_card(card)
                    raise ValueError("A source changed after rendering. Refresh and render again before export.")
            destination = export_plot_bundle(
                self.output_directory.value,
                [card["rendered"] for card in self.cards],
            )
            self.status.value = f"Exported figures, selected samples, and manifest to {html.escape(str(destination))}."
        except Exception as exc:
            self.status.value = f"<span style='color:#a40000'>{html.escape(str(exc))}</span>"

    def _configuration(self) -> dict[str, Any]:
        return {
            "schema": "unified-plot-setup-v1",
            "data_directory": str(Path(self.directory.value).expanduser()),
            "include_audit": bool(self.include_audit.value),
            "plots": [
                {**self._card_spec(card), "source_paths": list(card["sources"].value)}
                for card in self.cards
            ],
        }

    def _save_setup(self, _button: Any = None) -> None:
        try:
            path = Path(self.configuration_path.value).expanduser()
            if not path.is_absolute():
                path = Path(self.directory.value).expanduser().resolve() / path
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("x", encoding="utf-8") as stream:
                json.dump(self._configuration(), stream, indent=2, ensure_ascii=False, allow_nan=False)
            self.status.value = f"Saved setup to {html.escape(str(path.resolve()))}."
        except FileExistsError:
            self.status.value = "Setup file already exists; choose another name to avoid overwriting it."
        except Exception as exc:
            self.status.value = f"<span style='color:#a40000'>{html.escape(str(exc))}</span>"

    def _load_setup(self, _button: Any = None) -> None:
        try:
            path = Path(self.configuration_path.value).expanduser()
            if not path.is_absolute():
                path = Path(self.directory.value).expanduser().resolve() / path
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("schema") != "unified-plot-setup-v1":
                raise ValueError("Unsupported plot setup file.")
            catalog = discover_plot_sources(payload["data_directory"])
            available = {str(source.path) for source in catalog}
            missing = {p for spec in payload.get("plots", []) for p in spec.get("source_paths", [])} - available
            if missing:
                raise ValueError("Setup sources are missing: " + ", ".join(sorted(missing)))
            if any(spec.get("mode") not in ("curve", "xy_z", "field", "heatmap") for spec in payload.get("plots", [])):
                raise ValueError("Unsupported plot mode in setup.")
            from .combination_analysis import list_combination_runs
            run_catalog = {}
            for spec in payload.get("plots", []):
                runs_by_source = spec.get("run_ids_by_source")
                if runs_by_source is None:
                    continue  # Old setups retain their whole-database/run-filter selection.
                if not isinstance(runs_by_source, Mapping):
                    raise ValueError("Invalid run selection in setup.")
                for source_path, run_ids in runs_by_source.items():
                    if source_path not in spec.get("source_paths", ()) or not isinstance(run_ids, list):
                        raise ValueError("Invalid run selection in setup.")
                    source = next(item for item in catalog if str(item.path) == source_path)
                    if source.kind != "combination_sqlite" or any(not isinstance(value, str) for value in run_ids):
                        raise ValueError("Invalid run selection in setup.")
                    if source_path not in run_catalog:
                        run_catalog[source_path] = {run["run_id"] for run in list_combination_runs(source.path)}
                    if set(run_ids) - run_catalog[source_path]:
                        raise ValueError("Setup run IDs are missing: " + ", ".join(sorted(set(run_ids) - run_catalog[source_path])))
            self.directory.value = str(payload["data_directory"])
            self.include_audit.value = bool(payload.get("include_audit", False))
            self._refresh()
            for card in list(self.cards):
                self._remove_card(card)
            for spec in payload.get("plots", []):
                mode = spec.get("mode")
                self.add_plot(mode, spec)
            self.status.value = f"Loaded {len(self.cards)} plot cards from {html.escape(str(path.resolve()))}."
        except Exception as exc:
            self.status.value = f"<span style='color:#a40000'>{html.escape(str(exc))}</span>"

    @staticmethod
    def _close_figure(figure: Any) -> None:
        try:
            import matplotlib.pyplot as plt
            plt.close(figure)
        except ImportError:
            pass


def _source_signature(path: Path) -> tuple:
    """Include SQLite WAL and archived run-folder metadata in cache identity."""
    candidates = (path / "data.csv", path / "metadata.json") if path.is_dir() else (path, Path(str(path) + "-wal"))
    return tuple((str(p), p.stat().st_size, p.stat().st_mtime_ns) for p in candidates if p.is_file())
