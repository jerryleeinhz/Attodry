"""Offline rectilinear heatmaps with recorded identities and explicit gaps."""
from __future__ import annotations

import json
import math
from pathlib import Path
import textwrap
from typing import Any, Mapping, Sequence


MISSING_COLOR = "#D0D0D0"


def _edges(coordinates: Sequence[float]) -> tuple[float, ...]:
    """Arithmetic midpoint boundaries, including half-spacing outer edges."""
    if len(coordinates) < 2:
        raise ValueError("Heatmaps require at least two distinct X and Y coordinates in every panel; use scatter for a one-dimensional selection.")
    edges = (coordinates[0] - (coordinates[1] - coordinates[0]) / 2,
             *(a / 2 + b / 2 for a, b in zip(coordinates, coordinates[1:])),
             coordinates[-1] + (coordinates[-1] - coordinates[-2]) / 2)
    if any(not math.isfinite(value) for value in edges):
        raise ValueError("Heatmap cell edges are non-finite; use scatter for these coordinates.")
    return edges


def _coordinate_column(rows: Sequence[Mapping[str, Any]], key: str) -> str:
    requested = "requested." + key[7:] if key.startswith("actual.") else key
    return requested if any(requested in row for row in rows) else key


def _coordinate_label(key: str, column_label: Any) -> str:
    label = column_label(key)
    return "Requested " + label.removeprefix("target ") if key.startswith("requested.") else label


def _facet_keys(rows: Sequence[Mapping[str, Any]], value_key: Any) -> tuple[str, ...]:
    keys = ["source_path", "run_id", "repeat_index", "simulated", "role", "harmonic",
            "segment", "direction", "scan_type", "source_kind"]
    keys += sorted({key for row in rows for key in row
                    if key.startswith("axes.") and key.endswith((".segment", ".direction"))})
    return tuple(key for key in keys if len({value_key(row.get(key)) for row in rows}) > 1)


def _condition_identity(rows: Sequence[Mapping[str, Any]]) -> set[tuple]:
    # Axis indices distinguish revisits even if their numeric coordinates agree.
    keys = ["source_path", "run_id", "condition_id", "attempt_index", "repeat_index", "role", "harmonic"]
    keys += sorted({key for row in rows for key in row if key.startswith(("requested.", "axes."))})
    return {tuple((key, repr(row.get(key))) for key in keys) for row in rows}


def _facet_label(identity: tuple, column_label: Any) -> str:
    parts = []
    for key, value in identity:
        if key in {"source_path", "run_id"}:
            parts.append(f"{column_label(key)}={Path(str(value)).name}")
        elif key == "repeat_index" and type(value) in (int, float):
            parts.append(f"repeat {int(value) + 1}")
        elif key == "simulated":
            parts.append("SIMULATED" if value is True else "measured" if value is False else "simulation status unknown")
        else:
            parts.append(f"{column_label(key)}={value}")
    return textwrap.fill(" · ".join(parts), width=68)


def _color_scale(column: str, values: Sequence[float], mpl: Any):
    signed = column.endswith(("_x_v", "_y_v", "_phase_deg", "_voltage_v", "_current_a")) or any(v < 0 for v in values)
    if signed:
        bound = max((abs(value) for value in values), default=0.) or 1.
        norm = mpl.colors.TwoSlopeNorm(vcenter=0., vmin=-bound, vmax=bound)
        name = "RdBu_r"
        report = {"type": "TwoSlopeNorm", "vmin": -bound, "vmax": bound, "vcenter": 0.}
    else:
        minimum, maximum = (min(values), max(values)) if values else (0., 1.)
        if minimum == maximum:
            pad = abs(minimum) * .05 or 1.
            minimum, maximum = max(0., minimum - pad), maximum + pad
        norm = mpl.colors.Normalize(vmin=minimum, vmax=maximum)
        name = "viridis"
        report = {"type": "Normalize", "vmin": minimum, "vmax": maximum, "vcenter": None}
    report["empty"] = not bool(values)
    return mpl.colormaps[name].with_extremes(bad=MISSING_COLOR), norm, report


def render_heatmap(rows: Sequence[Mapping[str, Any]], spec: Mapping[str, Any]):
    """Render selected rows on exact recorded grids, without filling missing data.

    The caller applies fixed-condition filters. Manually excluded rows remain in
    this selection with ``_manual_excluded`` so their coordinates remain visible.
    Actual axes with archived requested counterparts use those requested grids;
    raw readbacks and source rows are retained unchanged in the export.
    """
    from .analysis_observations import finite, observation_id, qualify_observations, repeat_statistics
    from .scientific_plotting import publication_style, style_axis
    from .unified_plotting import _audit_title, _unresolved_dimensions, _value_key, column_label

    x, y, z = (spec.get(key) for key in ("x", "y", "z"))
    if not all((x, y, z)):
        raise ValueError("Choose X, Y and Color Z first.")
    if not rows:
        raise ValueError("No data rows remain after the selected filters.")
    mode = spec.get("statistics", "raw")
    if mode not in ("raw", "mean_sd", "mean_sem"):
        raise ValueError("Statistics must be raw, mean_sd or mean_sem.")
    coordinate_x, coordinate_y = _coordinate_column(rows, x), _coordinate_column(rows, y)
    if coordinate_x == coordinate_y:
        raise ValueError("Choose two different grid coordinates for X and Y.")
    facet_keys = _facet_keys(rows, _value_key)
    unresolved = _unresolved_dimensions(rows, plot_keys=(coordinate_x, coordinate_y),
                                        filter_keys=spec.get("filters") or {}, group_keys=facet_keys)
    if unresolved:
        raise ValueError("Unresolved varying conditions: " + ", ".join(column_label(key) for key, _ in unresolved)
                         + ". Choose a fixed-condition filter before rendering a heatmap.")

    manual_tokens = set(spec.get("excluded_sample_ids", ()))
    manual = {i for i, row in enumerate(rows) if row.get("_manual_excluded")
              or json.dumps(observation_id(row), sort_keys=True, ensure_ascii=False) in manual_tokens}
    eligible = [{**row, "_heatmap_row_index": i} for i, row in enumerate(rows) if i not in manual]
    qualified, quality = qualify_observations(eligible, (x, y, z), spec.get("quality_policy", "exclude"))
    qualified_indices = {row["_heatmap_row_index"] for row in qualified}
    coordinate_valid = {i for i, row in enumerate(rows) if finite(row.get(coordinate_x)) and finite(row.get(coordinate_y))}
    valid_indices = qualified_indices & coordinate_valid
    valid_indices = {i for i in valid_indices if finite(rows[i].get(z))}
    valid = tuple(row for i, row in enumerate(rows) if i in valid_indices)

    groups: dict[tuple, list[int]] = {}
    for i, row in enumerate(rows):
        identity = tuple((key, row.get(key)) for key in facet_keys)
        groups.setdefault(identity, []).append(i)
    panels, summaries = [], []
    for identity, indices in groups.items():
        xs = tuple(sorted({float(rows[i][coordinate_x]) for i in indices if finite(rows[i].get(coordinate_x))}))
        ys = tuple(sorted({float(rows[i][coordinate_y]) for i in indices if finite(rows[i].get(coordinate_y))}))
        x_edges, y_edges = _edges(xs), _edges(ys)
        cells: dict[tuple, list[int]] = {}
        for i in indices:
            if i in coordinate_valid:
                cells.setdefault((float(rows[i][coordinate_x]), float(rows[i][coordinate_y])), []).append(i)
        panel_summary = []
        for y_value in ys:
            for x_value in xs:
                selected_indices = cells.get((x_value, y_value), [])
                selected_rows = [rows[i] for i in selected_indices]
                if len(_condition_identity(selected_rows)) > 1:
                    raise ValueError(f"Conflicting recorded condition/attempt/axis-index identities at X={x_value:g}, Y={y_value:g}. "
                                     "Choose a fixed-condition filter or use scatter; different visits cannot be averaged.")
                cell_rows = [rows[i] for i in selected_indices if i in valid_indices]
                if mode == "raw" and len(cell_rows) > 1:
                    raise ValueError("Raw heatmaps require one qualified sample per cell; choose Mean ± SD/SEM for recorded formal repeats or use scatter.")
                points = repeat_statistics(cell_rows, x=coordinate_x, y=z, mode=mode) if cell_rows else ()
                if len(points) > 1:
                    raise ValueError("Heatmap cell contains incompatible recorded repeat identities; choose a fixed-condition filter or use scatter.")
                point = points[0] if points else {"n": 0, "sd": None, "sem": None, "error": None, "sample_ids": []}
                value = point.get("y")
                reasons = []
                exclusions = []
                if not selected_indices:
                    reasons.append("missing")
                for i in selected_indices:
                    reason = ("manual_excluded" if i in manual else "quality_excluded" if i not in qualified_indices
                              else "missing_or_nonfinite" if i not in valid_indices else None)
                    if reason:
                        reasons.append(reason)
                        exclusions.append({"sample": observation_id(rows[i]), "reason": reason})
                if cell_rows and not finite(value):
                    reasons.append("undefined_phase_mean" if z.endswith("phase_deg") else "undefined_mean")
                summary = {**{key: value for key, value in point.items() if key not in ("x", "y")},
                           "x": x_value, "y": y_value, "z": value if finite(value) else None,
                           "mean": value if finite(value) else None, "channel": z, "group": dict(identity),
                           "masked": not finite(value), "mask_reasons": list(dict.fromkeys(reasons)),
                           "selected_sample_ids": [observation_id(row) for row in selected_rows], "excluded_samples": exclusions}
                panel_summary.append(summary)
                summaries.append(summary)
        panels.append({"identity": identity, "x_coordinates": xs, "y_coordinates": ys,
                       "x_edges": x_edges, "y_edges": y_edges, "summary": panel_summary})

    for name in ("x", "y"):
        scale = spec.get(name + "_scale", "linear")
        if scale not in ("linear", "log"):
            raise ValueError("Axis scale must be linear or log.")
        if scale == "log" and any(v <= 0 for panel in panels for v in panel[name + "_edges"]):
            raise ValueError(f"Log {name.upper()} requires positive midpoint cell edges; use a linear axis or scatter for this grid.")

    with publication_style():
        import matplotlib as mpl
        import matplotlib.pyplot as plt
        from matplotlib.patches import Patch
        import numpy as np
        figure = None
        try:
            values = [point["z"] for point in summaries if not point["masked"]]
            cmap, norm, normalization = _color_scale(z, values, mpl)
            columns = min(len(panels), 2)
            figure, axes = plt.subplots(math.ceil(len(panels) / columns), columns,
                                       figsize=(6.2 * columns, 4.6 * math.ceil(len(panels) / columns)),
                                       layout="constrained", squeeze=False)
            used_axes, panel_reports = [], []
            for axis, panel in zip(axes.flat, panels):
                width, height = len(panel["x_coordinates"]), len(panel["y_coordinates"])
                grid = np.ma.masked_all((height, width))
                mask = []
                for index, point in enumerate(panel["summary"]):
                    if not point["masked"]:
                        grid[index // width, index % width] = point["z"]
                    mask.append(point["masked"])
                artist = axis.pcolormesh(panel["x_edges"], panel["y_edges"], grid,
                                         cmap=cmap, norm=norm, shading="flat", antialiased=False)
                axis.set(xlabel=_coordinate_label(coordinate_x, column_label),
                         ylabel=_coordinate_label(coordinate_y, column_label),
                         title=_facet_label(panel["identity"], column_label))
                for name in ("x", "y"):
                    getattr(axis, "set_" + name + "scale")(spec.get(name + "_scale", "linear"))
                axis.set_xlim(panel["x_edges"][0], panel["x_edges"][-1])
                axis.set_ylim(panel["y_edges"][0], panel["y_edges"][-1])
                style_axis(axis)
                axis.grid(False)
                if not any(not point["masked"] for point in panel["summary"]):
                    axis.text(.5, .5, "No qualified cell values", ha="center", va="center", transform=axis.transAxes)
                used_axes.append(axis)
                panel_reports.append({"facets": dict(panel["identity"]), "x_coordinates": list(panel["x_coordinates"]),
                                      "y_coordinates": list(panel["y_coordinates"]), "x_edges": list(panel["x_edges"]),
                                      "y_edges": list(panel["y_edges"]), "shape": [height, width],
                                      "masked_cells": [mask[i:i + width] for i in range(0, len(mask), width)],
                                      "displayed_cell_count": sum(not value for value in mask)})
            for axis in list(axes.flat)[len(panels):]:
                axis.set_visible(False)
            if values:
                figure.colorbar(artist, ax=used_axes, label=column_label(z))
            if any(point["masked"] for point in summaries):
                figure.legend(handles=[Patch(facecolor=MISSING_COLOR, edgecolor="#777777", label="Missing/excluded")],
                              loc="outside lower center")
            note = "Raw cells" if mode == "raw" else "Cell mean · " + ("SD" if mode == "mean_sd" else "SEM") + " exported"
            if quality["quality_policy"] == "include" and quality["flagged_row_count"]:
                note += " · AUDIT: flagged samples included"
            figure.suptitle(_audit_title(rows) + (spec.get("title") or "Recorded coordinate grid") + "\n" + note)
            report = {**quality, "mode": "heatmap", "statistics": mode, "selected_row_count": len(rows),
                      "plotted_row_count": len(valid), "omitted_missing_or_nonfinite_count": len(qualified_indices - valid_indices),
                      "manual_excluded_sample_ids": list(spec.get("excluded_sample_ids", ())),
                      "manual_excluded_count": len(manual),
                      "manual_excluded_samples": [observation_id(rows[i]) for i in sorted(manual)],
                      "missing_coordinate_count": len(rows) - len(coordinate_valid),
                      "displayed_point_count": len(values), "displayed_cell_count": len(values),
                      "masked_cell_count": len(summaries) - len(values), "panel_count": len(panels),
                      "facet_keys": list(facet_keys), "panels": panel_reports,
                      "coordinate_mappings": {name: {"selected_column": selected, "grid_column": coordinate,
                                                       "missing_grid_coordinate_count": sum(not finite(row.get(coordinate)) for row in rows)}
                                              for name, selected, coordinate in (("x", x, coordinate_x), ("y", y, coordinate_y))},
                      "coordinate_policy": "Use archived requested coordinates for matching actual axes; otherwise exact selected numeric coordinates. No rounding or inferred grid.",
                      "grid_edges": "arithmetic midpoints; outer edges extend by half the adjacent spacing",
                      "interpolation": "none", "missing_color": MISSING_COLOR, "colormap": cmap.name,
                      "color_normalization": normalization, "color_value": "raw value" if mode == "raw" else "within-condition mean",
                      "replication_unit": "formal repeats within the same source/run/condition/attempt and recorded axes",
                      "uncertainty": "SD/SEM exported only; SD uses ddof=1, SEM=SD/sqrt(n) assumes independence; undefined for n<2",
                      "undefined_error_point_count": sum(p["n"] == 1 for p in summaries) if mode != "raw" else 0,
                      "source_paths": sorted({str(row["source_path"]) for row in rows if row.get("source_path")})}
            return figure, {"rows": valid, "summary_rows": summaries, "report": report}
        except Exception:
            if figure is not None:
                plt.close(figure)
            raise
