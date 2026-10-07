"""Offline launch resolution and operator-facing summary; no hardware access."""
from __future__ import annotations

from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone
import re
import itertools
from pathlib import Path

from .combination_store import open_readonly


def resolve_launch(config, database=None, run_id=None):
    # Legacy command-line paths remain relative to the calling directory.
    # TOML paths are resolved by the loader relative to that TOML's directory.
    database = Path(database).resolve() if database is not None else config.database_path
    if database is None or "CHANGE_ME" in str(database):
        raise ValueError("Set project.database_path or provide --database")
    selected_id = config.run_id if run_id is None else run_id
    if not isinstance(selected_id, str) or not selected_id.strip() or selected_id != selected_id.strip():
        raise ValueError("run_id must be a nonempty string without outer whitespace")
    if selected_id == "auto":
        name = re.sub(r"[^A-Za-z0-9_.-]+", "_", config.plan.run_name).strip("_.-")[:80]
        selected_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + "_" + (name or "combination")
    return database, selected_id


def check_run_destination(database, run_id):
    if not database.exists():
        return
    with closing(open_readonly(database)) as connection:
        present = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                     "AND name='combination_runs'").fetchone()
        if present and connection.execute("SELECT 1 FROM combination_runs WHERE run_id=?",
                                          (run_id,)).fetchone():
            raise ValueError("Run already exists; never overwrite an audit or replay hardware history")


def _points(values):
    return values if len(values) <= 16 else {
        "count": len(values), "first": values[:8], "last": values[-8:], "min": min(values), "max": max(values)}


def _route(values):
    turns = [values[0]]
    previous_sign = 0
    for before, after in zip(values, values[1:]):
        sign = (after > before) - (after < before)
        if sign and previous_sign and sign != previous_sign:
            turns.append(before)
        if sign:
            previous_sign = sign
    if len(values) > 1:
        turns.append(values[-1])
    return turns


def launch_summary(config, database, run_id):
    axes = []
    count = config.plan.repeats
    for axis in config.plan.axes:
        count *= len(axis.points)
        coordinates = {key: _points(list(dict.fromkeys(p.values[key] for p in axis.points)))
                       for key in axis.points[0].values}
        axes.append({"module": axis.module, "points": len(axis.points),
            "requested_coordinates": coordinates,
            "first_target": axis.points[0].values, "last_target": axis.points[-1].values,
            "segments_and_directions": list(dict.fromkeys(
                (p.segment, p.direction) for p in axis.points)),
            "segments": [{"segment": segment, "direction": direction,
                "points": len(points),
                "coordinates": {key: _points(list(dict.fromkeys(p.values[key] for p in points)))
                                for key in points[0].values},
                "first_target": points[0].values, "last_target": points[-1].values,
                "routes": {key: _route([p.values[key] for p in points])
                           for key in points[0].values} if axis.module != "lockin" else {}}
                for (segment, direction), group in itertools.groupby(
                    axis.points, key=lambda p: (p.segment, p.direction))
                for points in [list(group)]]})
    summary = {"config": str(config.path), "config_sha256": config.snapshot["config_sha256"],
        "database": str(database), "run_name": config.plan.run_name, "run_id": run_id,
        "note": config.plan.note, "outer_to_inner": [a.module for a in config.plan.axes],
        "axes": axes, "repeats": config.plan.repeats, "total_conditions": count,
        "samples_per_condition": config.plan.samples_per_condition,
        "total_samples": count * config.plan.samples_per_condition,
        "cleanup": config.snapshot["cleanup_policy"], "hardware_resume": False}
    if config.magnetic is not None:
        summary["field_transition_policy"] = config.magnetic.run.transition_policy.value
        policy = config.snapshot["field_readback_policy"]
        if policy["mode"] == "vector":
            summary["field_resultant_limit_t"] = policy["limits"]["experiment_vector_max_t"]
        summary["field_readback_policy"] = config.snapshot["field_readback_policy"]
        summary["field_setpoint_ack_tolerance_t"] = config.magnetic.magnet.setpoint_ack_tolerance_t
    if config.smu is not None:
        summary["smu"] = {role: {"source_mode": device.source_mode.value,
            "max_abs_voltage_v": device.max_abs_voltage_v,
            "max_abs_current_a": device.max_abs_current_a}
            for role, device in config.smu.hardware.by_role().items()}
    if config.lockin is not None:
        hardware = config.snapshot["hardware"]["lockin"]
        summary["lockin"] = {"mode": config.lockin_mode,
            "internal_order": (["frequency", "excitation"]
                               if config.lockin_mode == "frequency_excitation" else [config.lockin_mode]),
            "harmonics_by_role": hardware["harmonics_by_role"],
            "skipped_harmonics_by_frequency": {key: value for key, value in
                hardware["skipped_harmonics_by_frequency"].items() if value},
            "overload_policy": config.lockin.lockin_sweep.overload_policy,
            "settle_time_constants": config.lockin.lockin_sweep.settle_time_constants,
            "baseline_frequency_hz": config.lockin.lockin_xx.frequency_hz,
            "segment_sensitivity_overrides": {
                name: [{"segment_index": segment, "xx_full_scale_v": xx, "xy_full_scale_v": xy}
                       for segment, xx, xy in dict.fromkeys(
                           (p.segment_index, p.xx_full_scale_v, p.xy_full_scale_v)
                           for p in getattr(config.lockin.lockin_sweep, name)
                           if p.xx_full_scale_v is not None or p.xy_full_scale_v is not None)]
                for name in ("frequency_point_specs", "excitation_point_specs")
                if config.lockin_mode == "frequency_excitation"
                    or name.startswith(config.lockin_mode)},
            "roles": {role: {"model": device.model,
                "sensitivity_mode": device.sensitivity_mode.value,
                "sensitivity_full_scale_v": device.sensitivity_full_scale_v,
                "reserve_mode": device.reserve_mode.value if device.reserve_mode is not None else None,
                "time_constant_s": device.time_constant_s,
                "sr865a": asdict(device.sr865a) if device.sr865a is not None else None,
                "autorange_min_full_scale_v": device.autorange_min_full_scale_v,
                "autorange_max_full_scale_v": device.autorange_max_full_scale_v,
                "harmonic_settings": [asdict(s) for s in device.harmonic_settings]}
                for role, device in (("lockin_xx", config.lockin.lockin_xx),
                                     ("lockin_xy", config.lockin.lockin_xy))}}
    return summary
