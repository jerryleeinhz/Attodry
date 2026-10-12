"""Read-only raw temperature/heater traces from progress JSONL or one SQLite run.

No hardware modules are imported. Query timestamps describe host observations,
not simultaneous sensor sampling. Rejected and incomplete observations are kept.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import csv
from dataclasses import dataclass
from datetime import datetime
import json
import math
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping, Sequence

from .progress_monitor import JsonlProgressTail


_SAMPLE_EVENTS = {
    "temperature_sample", "measurement_temperature_sample", "cryostat_state",
    "temperature_telemetry",
}
_CSV_FIELDS = (
    "sample_id", "run_id", "event", "point_index", "temperature_index",
    "condition_id", "attempt_index", "phase", "stage", "captured_at_utc",
    "captured_unix_s", "captured_monotonic_s", "requested_temperature_k",
    "tolerance_k", "acceptance_mode", "sample_temperature_k", "vti_temperature_k",
    "user_temperature_k", "sample_heater_power_w", "vti_heater_power_w",
    "temperature_control_enabled", "error_code", "telemetry_complete",
    "heater_power_read_error", "state_read_started_unix_s",
    "state_read_finished_unix_s", "state_read_started_monotonic_s",
    "state_read_finished_monotonic_s", "heater_power_read_started_unix_s",
    "heater_power_read_finished_unix_s", "heater_power_read_started_monotonic_s",
    "heater_power_read_finished_monotonic_s", "raw_events_json",
    "sample_heater_read_finished_unix_s", "vti_heater_read_finished_unix_s",
)


@dataclass(frozen=True)
class TemperatureTrace:
    """A file snapshot; samples merge raw/completion events only by sample ID."""

    samples: tuple[dict[str, Any], ...]
    events: tuple[dict[str, Any], ...]
    source: str
    run_id: str | None = None


def load_temperature_trace(
    *, progress: str | Path | None = None, database: str | Path | None = None,
    run_id: str | None = None,
) -> TemperatureTrace:
    """Load complete JSONL lines or an explicitly selected SQLite run, read-only.

    An unfinished final JSONL line is deferred, never treated as a zero sample.
    Complete malformed lines raise. SQLite never chooses a latest run implicitly.
    """
    if (progress is None) == (database is None):
        raise ValueError("Provide exactly one progress file or database.")
    if progress is not None:
        if run_id is not None:
            raise ValueError("run_id is only used with a database.")
        path = Path(progress).resolve()
        events = JsonlProgressTail(path).read_new_events()
        configuration = None
    else:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("An explicit nonempty run_id is required for SQLite.")
        path = Path(database).resolve()  # type: ignore[arg-type]
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            selected = connection.execute(
                "SELECT plan_json FROM combination_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if selected is None:
                raise ValueError(f"Run ID does not exist in database: {run_id}")
            configuration = json.loads(selected[0])
            if not isinstance(configuration, Mapping):
                raise ValueError("Archived combination plan must be a JSON object.")
            records = connection.execute(
                "SELECT event_id, created_at_utc, event_type, payload_json "
                "FROM combination_events WHERE run_id=? ORDER BY event_id", (run_id,)
            ).fetchall()
        parsed = []
        for event_id, captured_at, event_name, payload in records:
            event = json.loads(payload)
            if not isinstance(event, dict):
                raise ValueError(f"SQLite event {event_id} payload must be an object.")
            event = dict(event)
            event["event"] = event_name
            event["run_id"] = run_id
            event["database_event_id"] = event_id
            event.setdefault("captured_at_utc", captured_at)
            parsed.append(event)
        events = tuple(parsed)
    return _build_trace(events, source=str(path), run_id=run_id, configuration=configuration)


def _recorded_stability(record: Mapping[str, Any]) -> Mapping[str, Any]:
    """Resolve only archived temperature policies, never magnetic criteria."""
    config = record.get("measurement_config", record)
    if not isinstance(config, Mapping):
        return {}
    candidates = [config, config.get("temperature")]
    hardware = config.get("hardware")
    if isinstance(hardware, Mapping):
        candidates.append(hardware.get("temperature"))
    for candidate in candidates:
        if isinstance(candidate, Mapping) and isinstance(candidate.get("temperature_stability"), Mapping):
            return candidate["temperature_stability"]
    return {}


def _policy_values(stability: Mapping[str, Any]) -> tuple[Any, Any]:
    criteria = stability.get("criteria")
    tolerance = stability.get("tolerance_k")
    if tolerance is None and isinstance(criteria, Mapping):
        tolerance = criteria.get("tolerance")
    return tolerance, stability.get("acceptance_mode")


def _build_trace(
    events: Sequence[Mapping[str, Any]], *, source: str, run_id: str | None,
    configuration: Mapping[str, Any] | None = None,
) -> TemperatureTrace:
    samples: list[dict[str, Any]] = []
    indices: dict[str, int] = {}
    target = None
    tolerance, acceptance_mode = _policy_values(_recorded_stability(configuration or {}))
    for event in events:
        request = event.get("temperature_run")
        if isinstance(request, Mapping) and "target_k" in request:
            target = request["target_k"]
        for key in ("requested_temperature_k", "target_k"):
            if key in event:
                target = event[key]
                break
        stability = _recorded_stability(event)
        if stability:
            tolerance, acceptance_mode = _policy_values(stability)
        if event.get("event") not in _SAMPLE_EVENTS and not isinstance(event.get("sample_id"), str):
            continue
        state = event.get("state")
        if not isinstance(state, Mapping):
            continue
        sample = dict(event)
        sample.setdefault("requested_temperature_k", target)
        sample.setdefault("tolerance_k", tolerance)
        sample.setdefault("acceptance_mode", acceptance_mode)
        sample_id = event.get("sample_id")
        if isinstance(sample_id, str) and sample_id in indices:
            index = indices[sample_id]
            previous = samples[index]
            # A partial raw event must not overwrite a later completed record.
            if event.get("event") == "temperature_telemetry":
                samples[index] = {**previous, **sample}
            samples[index]["raw_events"] = [*previous["raw_events"], dict(event)]
        else:
            sample["raw_events"] = [dict(event)]
            if isinstance(sample_id, str):
                indices[sample_id] = len(samples)
            samples.append(sample)
    return TemperatureTrace(tuple(samples), tuple(dict(e) for e in events), source, run_id)


def export_temperature_trace_csv(trace: TemperatureTrace, destination: str | Path) -> Path:
    """Export every sample, including rejected/missing readings; nulls stay blank."""
    path = Path(destination)
    if path.resolve() == Path(trace.source).resolve():
        raise ValueError("CSV destination must not overwrite the raw source file.")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=_CSV_FIELDS)
        writer.writeheader()
        for sample in trace.samples:
            row = {key: sample.get(key) for key in _CSV_FIELDS}
            state = sample["state"]
            for key in (
                "sample_temperature_k", "vti_temperature_k", "user_temperature_k",
                "temperature_control_enabled", "error_code",
            ):
                row[key] = state.get(key)
            power = sample.get("heater_power")
            if isinstance(power, Mapping):
                row["sample_heater_power_w"] = power.get("sample_w")
                row["vti_heater_power_w"] = power.get("vti_w")
            for prefix in ("state_read", "heater_power_read"):
                timing = sample.get(prefix + "_timing")
                if isinstance(timing, Mapping):
                    for suffix in (
                        "started_unix_s", "finished_unix_s", "started_monotonic_s",
                        "finished_monotonic_s",
                    ):
                        row[prefix + "_" + suffix] = timing.get(suffix)
            row["raw_events_json"] = json.dumps(sample["raw_events"], ensure_ascii=False)
            for channel, prefix in (("sample_w", "sample"), ("vti_w", "vti")):
                reads = sample.get("heater_power_reads")
                if isinstance(reads, Mapping) and isinstance(reads.get(channel), Mapping):
                    row[f"{prefix}_heater_read_finished_unix_s"] = reads[channel].get("finished_unix_s")
            writer.writerow(row)
    return path


def _finite(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return math.nan
    return float(value) if math.isfinite(value) else math.nan


def _timestamp(event: Mapping[str, Any], timing: str | None = None) -> float:
    if timing is not None and isinstance(event.get(timing), Mapping):
        value = _finite(event[timing].get("finished_unix_s"))
        if math.isfinite(value):
            return value
    value = _finite(event.get("captured_unix_s"))
    if math.isfinite(value):
        return value
    captured = event.get("captured_at_utc")
    if isinstance(captured, str):
        try:
            parsed = datetime.fromisoformat(captured.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.timestamp()
        except ValueError:
            pass
    return math.nan


def _power_timestamp(event: Mapping[str, Any], channel: str) -> float:
    reads = event.get("heater_power_reads")
    if isinstance(reads, Mapping) and isinstance(reads.get(channel), Mapping):
        value = _finite(reads[channel].get("finished_unix_s"))
        if math.isfinite(value):
            return value
    return _timestamp(event, "heater_power_read_timing")


def plot_temperature_trace(trace: TemperatureTrace) -> Any:
    """Plot raw host-timed observations; missing power is a gap, never zero.

    Separate state/power query finish times are used when recorded. All values,
    including unsafe/rejected temperature jumps, remain visible without smoothing.
    """
    from .scientific_plotting import OKABE_ITO_ON_WHITE, publication_style, style_axis
    import matplotlib.pyplot as plt

    timestamps = [_timestamp(sample, "state_read_timing") for sample in trace.samples]
    finite_times = [value for value in timestamps if math.isfinite(value)]
    if not finite_times:
        raise ValueError("No timestamped temperature samples are available to plot.")
    origin = finite_times[0]
    elapsed = [(value - origin) / 60 for value in timestamps]
    with publication_style():
        figure, (temperature_axis, power_axis) = plt.subplots(
            2, 1, figsize=(10.2, 7.0), sharex=True, constrained_layout=True,
            gridspec_kw={"height_ratios": (3, 2)},
        )
        for key, label, index, marker, linestyle in (
            ("sample_temperature_k", "Sample temperature", 0, "o", "-"),
            ("vti_temperature_k", "VTI temperature", 1, "s", "--"),
        ):
            temperature_axis.plot(
                elapsed, [_finite(s["state"].get(key)) for s in trace.samples],
                label=label, color=OKABE_ITO_ON_WHITE[index], marker=marker,
                markersize=2.5, linestyle=linestyle,
            )
        target = [_finite(s.get("requested_temperature_k")) for s in trace.samples]
        # Stable-readback qualifies a stable measured baseline, not proximity to
        # target. Do not draw its tolerance as a target acceptance band.
        tolerance = [_finite(s.get("tolerance_k")) if s.get("acceptance_mode") == "target"
                     else math.nan for s in trace.samples]
        if any(math.isfinite(value) for value in target):
            temperature_axis.step(elapsed, target, where="post", color="black",
                                  linestyle=":", label="Requested target")
        setpoint = [_finite(s["state"].get("user_temperature_k")) for s in trace.samples]
        if any(math.isfinite(value) for value in setpoint):
            temperature_axis.step(elapsed, setpoint, where="post", color=OKABE_ITO_ON_WHITE[2],
                                  linestyle="--", label="Setpoint readback")
        if any(math.isfinite(t) and math.isfinite(w) for t, w in zip(target, tolerance)):
            temperature_axis.fill_between(
                elapsed, [t - w for t, w in zip(target, tolerance)],
                [t + w for t, w in zip(target, tolerance)], step="post",
                color="black", alpha=0.08, label="Recorded target tolerance",
            )
        for key, label, index, marker, linestyle in (
            ("sample_w", "Sample heater", 0, "o", "-"),
            ("vti_w", "VTI heater", 1, "s", "--"),
        ):
            power_elapsed = [(_power_timestamp(s, key) - origin) / 60 for s in trace.samples]
            values = [_finite(s["heater_power"].get(key))
                      if isinstance(s.get("heater_power"), Mapping) else math.nan
                      for s in trace.samples]
            power_axis.plot(power_elapsed, values, label=label,
                            color=OKABE_ITO_ON_WHITE[index], marker=marker,
                            markersize=2.5, linestyle=linestyle)
        if not any(
            isinstance(s.get("heater_power"), Mapping)
            and any(math.isfinite(_finite(value)) for value in s["heater_power"].values())
            for s in trace.samples
        ):
            power_axis.text(0.5, 0.5, "Heater power is missing / was not recorded in this snapshot",
                            transform=power_axis.transAxes, ha="center", va="center")
        _mark_events(trace, (temperature_axis, power_axis), origin)
        temperature_axis.set(ylabel="Temperature (K)", title="Raw temperature and heater observations")
        power_axis.set(ylabel="Heater power (W)", xlabel="Elapsed host time (min)")
        for axis in (temperature_axis, power_axis):
            style_axis(axis)
            axis.legend(loc="upper left", bbox_to_anchor=(1.01, 1.0), borderaxespad=0)
        figure.supxlabel(
            "Sequential host queries; gaps = unrecorded/failed reads; all observations retained",
            fontsize=7,
        )
        return figure


def _mark_events(trace: TemperatureTrace, axes: Sequence[Any], origin: float) -> None:
    markers = {
        "temperature_command_attempt": ("Target change", "#666666", ":"),
        "setpoint_confirmed": ("Target change", "#666666", ":"),
        "temperature_stable": ("Qualified", "#009E73", "--"),
        "point_completed": ("Qualified", "#009E73", "--"),
        "temperature_qualification_completed": ("Qualified", "#009E73", "--"),
        "scan_failed": ("Failure", "#D55E00", "-."),
    }
    labelled: set[str] = set()
    previous_control: object = None
    for event in trace.events:
        event_markers = []
        marker = markers.get(str(event.get("event")))
        if marker is not None:
            event_markers.append(marker)
        state = event.get("state")
        if isinstance(state, Mapping):
            control = state.get("temperature_control_enabled")
            if isinstance(control, bool) and control != previous_control:
                if previous_control is not None:
                    event_markers.append(("Control on" if control else "Control off", "#CC79A7", ":"))
                previous_control = control
            if state.get("error_code") not in (None, 0):
                event_markers.append(("Device error", "#D55E00", "-."))
        if event.get("event") == "scan_finished" and event.get("outcome") in {"rejected", "interrupted"}:
            event_markers.append(("Failure / interruption", "#D55E00", "-."))
        if event.get("event") == "temperature_run_finished" and event.get("completed") is False:
            event_markers.append(("Failure / interruption", "#D55E00", "-."))
        timestamp = _timestamp(event, "state_read_timing")
        if not math.isfinite(timestamp):
            continue
        for label, color, linestyle in event_markers:
            for index, axis in enumerate(axes):
                axis.axvline((timestamp - origin) / 60, color=color, linestyle=linestyle,
                             linewidth=0.7, alpha=0.6,
                             label=label if index == 0 and label not in labelled else "_nolegend_")
            labelled.add(label)


def run(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--progress", type=Path)
    source.add_argument("--database", type=Path)
    parser.add_argument("--run-id", help="Required explicit run identity for SQLite.")
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--plot", action="store_true", help="Also export raw trace as PNG and PDF.")
    args = parser.parse_args(argv)
    trace = load_temperature_trace(progress=args.progress, database=args.database, run_id=args.run_id)
    output = args.output_directory.resolve()
    source_path = Path(trace.source).resolve()
    if output == source_path.parent:
        raise ValueError("Choose a separate output directory to preserve raw source files.")
    output.mkdir(parents=True, exist_ok=True)
    stem = output / "temperature_trace"
    destinations = [stem.with_suffix(".csv"), output / "temperature_trace_manifest.json"]
    if args.plot:
        destinations.extend(stem.with_suffix(suffix) for suffix in (".png", ".pdf"))
    if any(path.exists() for path in destinations):
        raise FileExistsError("Trace exports already exist; choose a new output directory.")
    export_temperature_trace_csv(trace, destinations[0])
    if args.plot:
        from .scientific_plotting import export_publication_figure_set
        import matplotlib.pyplot as plt
        figure = plot_temperature_trace(trace)
        try:
            export_publication_figure_set(figure, stem, formats=("png", "pdf"))
        finally:
            plt.close(figure)
    destinations[1].write_text(json.dumps({
        "source": trace.source, "run_id": trace.run_id, "samples": len(trace.samples),
        "events": len(trace.events), "transformations": "none; sample-ID raw/completion merging only",
        "missing_values": "CSV blank, plot gap; no zero fill or interpolation",
        "timestamps": "sequential host query finish times when present; not sensor refresh times",
        "outputs": [str(path) for path in destinations if path != destinations[1]],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Read-only trace: {len(trace.samples)} samples from {trace.source}")
    print(f"Exports: {output}")
    return 0


def main(argv: Sequence[str] | None = None) -> None:
    try:
        raise SystemExit(run(argv))
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
