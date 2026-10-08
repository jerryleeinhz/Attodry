"""Read saved standalone lock-in progress without opening a hardware path.

File identity is the run identity here. The recorded name is a display label;
neither it nor a terminal event associates this file with a final JSON record or
proves that an acquisition process is alive.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
import math
from pathlib import Path
from typing import TYPE_CHECKING, Mapping

from .commissioning_analysis import (
    CommissioningSample, PLOT_HARMONICS, SWEEP_METRICS,
    _commissioning_sample, _phase_plot_values, aggregate_sweep_samples,
)
from .progress_monitor import JsonlProgressTail, ProgressFormatError
from .scientific_plotting import (
    PUBLICATION_SINGLE_FIGSIZE, ordered_series_style, outside_legend,
    publication_plot, style_axis,
)

if TYPE_CHECKING:
    from matplotlib.figure import Figure


_SCAN_TYPES = ("frequency", "excitation", "frequency_excitation")
_POINT_FIELDS = (
    "target_frequency_hz", "actual_frequency_hz", "source_v_rms",
    "source_readback_v_rms",
)


@dataclass(frozen=True, slots=True)
class ProgressSweepSnapshot:
    path: Path
    scan_type: str | None
    run_name: str
    points_total: int | None
    completed_point_count: int
    formal_pair_count: int
    rows: tuple[CommissioningSample, ...]
    last_event: str
    last_event_unix_s: float | None
    outcome: str | None
    cleanup_verified: bool | None
    finished: bool
    current_point: Mapping[str, object] | None = None

    @property
    def clean_rows(self) -> tuple[CommissioningSample, ...]:
        return tuple(row for row in self.rows if row.statuses == ("clean",))

    @property
    def quality_counts(self) -> dict[str, dict[str, int]]:
        counts = {role: {"total": 0, "clean": 0, "excluded": 0}
                  for role in ("xx", "xy")}
        for row in self.rows:
            counts[row.role]["total"] += 1
            counts[row.role]["clean" if row.statuses == ("clean",) else "excluded"] += 1
        return counts


def discover_progress_records(directory: str | Path) -> tuple[Path, ...]:
    """List explicit standalone sweep files; leave selection to the caller."""

    root = Path(directory).resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"Progress directory does not exist: {root}")
    return tuple(sorted({path.resolve() for scan in _SCAN_TYPES
                         for path in root.glob(f"*_lockin_{scan}_progress.jsonl")
                         if path.is_file()}, key=lambda path: path.name))


class CommissioningProgressReader:
    """Append-only reader, rejecting inconsistent evidence until file replacement."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).resolve()
        self._tail = JsonlProgressTail(self.path)
        self._generation = self._tail.generation
        self._reset()

    def _reset(self) -> None:
        self._header: str | None = None
        self._scan: str | None = None
        self._run_name = ""
        self._points_total: int | None = None
        self._rows: list[CommissioningSample] = []
        self._pairs: dict[tuple[int, int, int], str] = {}
        self._point_coordinates: dict[int, str] = {}
        self._current_point: dict[str, object] | None = None
        self._completed_points: set[int] = set()
        self._last_event = "none"
        self._last_timestamp: float | None = None
        self._terminal: str | None = None
        self._outcome: str | None = None
        self._cleanup: bool | None = None
        self._error: Exception | None = None

    def refresh(self) -> ProgressSweepSnapshot:
        """Return all validated saved samples, or raise without a fresh snapshot."""

        events = self._tail.read_new_events()
        if self._generation != self._tail.generation:
            self._reset()
            self._generation = self._tail.generation
        if self._error is not None:
            raise ProgressFormatError(
                f"Selected progress evidence remains invalid: {self._error}"
            ) from self._error
        try:
            for event in events:
                self._consume(event)
        except (TypeError, ValueError, KeyError) as exc:
            self._error = exc
            raise ProgressFormatError(f"Invalid sweep progress in {self.path}: {exc}") from exc
        record_status = self._outcome or "progress"
        return ProgressSweepSnapshot(
            path=self.path, scan_type=self._scan, run_name=self._run_name,
            points_total=self._points_total,
            completed_point_count=len(self._completed_points),
            formal_pair_count=len(self._pairs),
            rows=tuple(replace(row, record_status=record_status) for row in self._rows),
            last_event=self._last_event, last_event_unix_s=self._last_timestamp,
            outcome=self._outcome, cleanup_verified=self._cleanup,
            finished=self._terminal is not None,
            current_point=None if self._current_point is None else dict(self._current_point),
        )

    def _consume(self, event: Mapping[str, object]) -> None:
        name = event.get("event")
        if not isinstance(name, str) or not name:
            raise ValueError("Progress event must have an event name.")
        timestamp = event.get("captured_unix_s")
        if timestamp is not None:
            timestamp = _number(timestamp, "captured_unix_s")
            if not math.isfinite(timestamp):
                raise ValueError("captured_unix_s must be finite.")
        if name == "scan_started":
            canonical = _canonical(event)
            if self._header is not None:
                if canonical != self._header:
                    raise ValueError("Conflicting scan_started events in one file.")
                return
            if self._terminal is not None:
                raise ValueError("scan_started follows a terminal event.")
            scan = event.get("scan")
            if scan not in _SCAN_TYPES:
                raise ValueError("Progress preview supports standalone lock-in sweeps only.")
            if (type(event.get("schema_version")) is not int or
                    event.get("schema_version") != 1 or event.get("command") != "lockin-sweep"):
                raise ValueError("Unsupported standalone sweep progress header.")
            metadata = event.get("run_metadata")
            if not isinstance(metadata, Mapping) or not isinstance(metadata.get("name"), str):
                raise ValueError("Progress header must record run_metadata.name.")
            self._scan = str(scan)
            self._points_total = _index(event.get("points_total"), "points_total", minimum=1)
            self._run_name = metadata["name"]
            self._header = canonical
        else:
            if self._header is None:
                raise ValueError("A standalone scan_started header is required before data.")
            if event.get("scan") not in (None, self._scan):
                raise ValueError("Event scan conflicts with the selected header.")
            if self._terminal is not None:
                if name == "scan_finished" and _canonical(event) == self._terminal:
                    return
                raise ValueError("Progress event follows the terminal scan_finished event.")
            if name == "lockin_formal_sample":
                self._consume_sample(event)
            elif name in {"lockin_point_ready", "lockin_point_completed"}:
                point = self._point(event.get("sweep_point"))
                if name == "lockin_point_completed":
                    self._completed_points.add(point["point_index"])
            elif name == "scan_finished":
                outcome = event.get("outcome")
                completed = event.get("completed")
                cleanup = event.get("cleanup_verified")
                if outcome not in {"completed", "rejected", "interrupted"}:
                    raise ValueError("Unknown terminal sweep outcome.")
                if type(completed) is not bool or completed != (outcome == "completed"):
                    raise ValueError("Terminal completed/outcome evidence conflicts.")
                if cleanup is not None and type(cleanup) is not bool:
                    raise ValueError("cleanup_verified must be boolean or null.")
                if completed and (cleanup is not True or
                                  len(self._completed_points) != self._points_total):
                    raise ValueError("Completed outcome lacks recorded points/verified cleanup.")
                self._outcome = str(outcome)
                self._cleanup = cleanup
                self._terminal = _canonical(event)
        self._last_event = name
        self._last_timestamp = timestamp

    def _point(self, value: object) -> Mapping[str, object]:
        if not isinstance(value, Mapping):
            raise ValueError("A complete sweep_point object is required.")
        index = _index(value.get("point_index"), "point_index")
        if self._points_total is not None and index >= self._points_total:
            raise ValueError("point_index exceeds header points_total.")
        if "points_total" in value and value["points_total"] != self._points_total:
            raise ValueError("Point/header points_total evidence conflicts.")
        for field in _POINT_FIELDS:
            _number(value.get(field), f"sweep_point.{field}")
        current = value.get("nominal_current_a_rms")
        if current is not None:
            _number(current, "sweep_point.nominal_current_a_rms")
        coordinates = _canonical({field: value.get(field) for field in
                                  (*_POINT_FIELDS, "nominal_current_a_rms",
                                   "frequency_index", "excitation_index")})
        previous = self._point_coordinates.get(index)
        if previous is not None and previous != coordinates:
            raise ValueError("Conflicting readback coordinates for one point_index.")
        self._point_coordinates[index] = coordinates
        self._current_point = dict(value)
        return value

    def _consume_sample(self, event: Mapping[str, object]) -> None:
        point = self._point(event.get("sweep_point"))
        sample = event.get("sample")
        if not isinstance(sample, Mapping):
            raise ValueError("Formal event must contain a sample object.")
        embedded = sample.get("sweep_point")
        if embedded is not None and _canonical(embedded) != _canonical(point):
            raise ValueError("Event/sample sweep_point evidence conflicts.")
        index = _index(sample.get("sample_index"), "sample_index")
        harmonic = _index(sample.get("harmonic"), "harmonic", minimum=1)
        if harmonic not in PLOT_HARMONICS:
            raise ValueError("Unsupported formal harmonic.")
        selected = sample.get("selected_roles")
        if (not isinstance(selected, list) or not selected or
                any(role not in ("xx", "xy") for role in selected) or
                len(set(selected)) != len(selected)):
            raise ValueError("Formal selected_roles must explicitly name unique xx/xy roles.")
        problems = _problems(sample.get("problems", []), "sample.problems")
        if "settings_verified" in sample and type(sample["settings_verified"]) is not bool:
            raise ValueError("settings_verified must be boolean when recorded.")
        for field in ("problems_by_role", "valid_for_analysis_by_role"):
            per_role = sample.get(field)
            if per_role is not None and not isinstance(per_role, Mapping):
                raise ValueError(f"{field} must be a role mapping.")
            if isinstance(per_role, Mapping):
                for role in selected:
                    role_name = f"lockin_{role}"
                    if role_name not in per_role:
                        raise ValueError(f"{field} is missing selected {role_name}.")
                    if field == "problems_by_role":
                        _problems(per_role[role_name], field)
                    elif type(per_role[role_name]) is not bool:
                        raise ValueError("Per-role validity must be boolean.")
        key = (point["point_index"], index, harmonic)
        canonical = _canonical({"sweep_point": point, "sample": sample})
        previous = self._pairs.get(key)
        if previous is not None:
            if previous != canonical:
                raise ValueError("Conflicting samples share a formal pair identity.")
            return
        if point["point_index"] in self._completed_points:
            raise ValueError("Formal sample follows completion of its point.")
        rows = []
        for role in ("xx", "xy"):
            instrument = sample.get(f"lockin_{role}")
            if not isinstance(instrument, Mapping):
                raise ValueError(f"Formal pair is missing lockin_{role}.")
            reading = instrument.get("reading")
            status = instrument.get("lia_status")
            if not isinstance(reading, Mapping) or not isinstance(status, Mapping):
                raise ValueError("Formal pair is missing recorded reading/status.")
            actual_harmonic = _index(reading.get("harmonic"), "reading.harmonic", minimum=1)
            if actual_harmonic not in PLOT_HARMONICS:
                raise ValueError("Unsupported actual formal harmonic.")
            _index(status.get("raw"), "lia_status.raw")
            _index(instrument.get("error_status"), "error_status")
            if role not in selected:
                continue
            if actual_harmonic != harmonic:
                raise ValueError("Selected role actual harmonic conflicts with formal harmonic.")
            if reading.get("role") not in (None, role, f"lockin_{role}"):
                raise ValueError("Recorded reading.role conflicts with its formal role.")
            for field in ("x_v", "y_v", "amplitude_v", "frequency_hz"):
                _number(reading.get(field), f"lockin_{role}.{field}")
            if type(reading.get("locked")) is not bool:
                raise ValueError("Formal locked state must be boolean.")
            if reading.get("phase_deg") is not None:
                _number(reading["phase_deg"], f"lockin_{role}.phase_deg")
            row = _commissioning_sample(
                path=self.path, record_status="progress", scan_type=str(self._scan),
                point=point, sample=sample, role=role, problems=problems,
                recorded_excitation_path=None, allow_nonfinite_phase=True,
            )
            extra_problems = []
            if row.model not in (None, "SR830", "SR865A"):
                extra_problems.append("Unknown recorded lock-in model")
            numeric = (row.target_frequency_hz, row.actual_frequency_hz,
                       row.source_v_rms, row.sine_output_v_rms, row.x_v, row.y_v,
                       row.amplitude_v, row.reference_frequency_hz,
                       row.nominal_current_a_rms, row.phase_deg, row.phase_shift_deg)
            if any(value is not None and not math.isfinite(value) for value in numeric):
                extra_problems.append("Nonfinite formal value")
            if (row.target_frequency_hz <= 0 or row.actual_frequency_hz <= 0 or
                    row.reference_frequency_hz <= 0 or row.source_v_rms < 0 or
                    row.sine_output_v_rms < 0 or row.amplitude_v < 0 or
                    (row.nominal_current_a_rms is not None and row.nominal_current_a_rms < 0)):
                extra_problems.append("Invalid formal frequency/amplitude/current coordinate")
            if sample.get("settings_verified") is False:
                extra_problems.append("Recorded formal settings are unverified")
            if row.model in (None, "SR830") and any(type(status.get(field)) is not bool
                    for field in ("input_or_reserve_overload", "filter_overload", "reference_unlocked")):
                extra_problems.append("Unknown SR830 formal safety status")
            if extra_problems:
                statuses = tuple(s for s in row.statuses if s != "clean")
                row = replace(row, statuses=tuple(dict.fromkeys((*statuses, "problem"))),
                              problems=(*row.problems, *extra_problems))
            rows.append(row)
        self._pairs[key] = canonical
        self._rows.extend(rows)


def _number(value: object, field: str) -> float:
    if type(value) not in (int, float):
        raise ValueError(f"{field} must be a recorded numeric value.")
    return float(value)


def _index(value: object, field: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{field} must be an integer >= {minimum}.")
    return value


def _problems(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a string list.")
    return tuple(value)


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@publication_plot
def plot_progress_sweep(
    snapshot: ProgressSweepSnapshot, *, metric: str = "amplitude_v",
    x_axis: str = "sine_output_v_rms", minimum_phase_amplitude_v: float = 1e-6,
    maximum_phase_standard_deviation_deg: float | None = 5.0,
) -> tuple[Figure, ...]:
    """Plot each recorded clean role/harmonic, using archived coordinates only."""

    if metric not in SWEEP_METRICS:
        raise ValueError(f"Unsupported progress metric: {metric}")
    if snapshot.finished and snapshot.outcome in {"rejected", "interrupted"}:
        raise ValueError(
            "Rejected/interrupted progress is audit evidence; use the final JSON "
            "with explicit audit selection to plot it."
        )
    if x_axis not in {"sine_output_v_rms", "nominal_current_a_rms"}:
        raise ValueError(f"Unsupported progress excitation axis: {x_axis}")
    if (not math.isfinite(minimum_phase_amplitude_v) or minimum_phase_amplitude_v < 0 or
            (maximum_phase_standard_deviation_deg is not None and (
                not math.isfinite(maximum_phase_standard_deviation_deg) or
                maximum_phase_standard_deviation_deg <= 0))):
        raise ValueError("Phase thresholds must be finite, with nonnegative amplitude and positive SD.")
    rows = snapshot.clean_rows
    actual_axis = "actual_frequency_hz" if snapshot.scan_type == "frequency" else x_axis
    if actual_axis == "nominal_current_a_rms" and any(
            row.nominal_current_a_rms is None for row in rows):
        raise ValueError("Archived nominal current is missing; select the SINE OUT voltage axis.")
    import matplotlib.pyplot as plt

    figures = []
    for role, harmonic in sorted({(row.role, row.harmonic) for row in rows}):
        selected = tuple(row for row in rows if (row.role, row.harmonic) == (role, harmonic))
        groups = ({frequency: tuple(row for row in selected if row.actual_frequency_hz == frequency)
                   for frequency in sorted({row.actual_frequency_hz for row in selected})}
                  if snapshot.scan_type == "frequency_excitation" else {None: selected})
        figure, axis = plt.subplots(figsize=PUBLICATION_SINGLE_FIGSIZE, constrained_layout=True)
        has_usable_phase_points = False
        for index, (frequency, group) in enumerate(groups.items()):
            statistics = aggregate_sweep_samples(group, metric=metric, x_axis=actual_axis)
            if metric == "phase_deg":
                amplitude = aggregate_sweep_samples(group, metric="amplitude_v", x_axis=actual_axis)
                _, values, _, _, _ = _phase_plot_values(
                    statistics, amplitude, minimum_amplitude_v=minimum_phase_amplitude_v,
                    maximum_standard_deviation_deg=maximum_phase_standard_deviation_deg,
                )
                spreads = [item.standard_deviation if math.isfinite(value) else math.nan
                           for item, value in zip(statistics, values)]
                has_usable_phase_points |= any(math.isfinite(value) for value in values)
            else:
                values = [item.mean for item in statistics]
                spreads = [item.standard_deviation for item in statistics]
            if statistics:
                axis.errorbar(
                    [item.x_value for item in statistics], values, yerr=spreads,
                    **ordered_series_style(index, len(groups), colormap_name="viridis"),
                    capsize=2.5, elinewidth=0.8,
                    label=f"{frequency:.7g} Hz" if frequency is not None else f"V{role} h{harmonic}",
                )
        axis.set_xlabel({"actual_frequency_hz": "Actual frequency (Hz)",
                         "sine_output_v_rms": "SINE OUT readback (V RMS)",
                         "nominal_current_a_rms": "Archived nominal current (A RMS)"}[actual_axis])
        axis.set_ylabel({"amplitude_v": "R (V RMS)", "x_v": "X (V RMS)",
                         "y_v": "Y (V RMS)", "phase_deg": "Unwrapped phase (°)"}[metric])
        axis.set_title(f"Saved progress · {snapshot.run_name}\n{snapshot.scan_type} · V{role} · h{harmonic}")
        if snapshot.scan_type == "frequency":
            axis.set_xscale("log")
        style_axis(axis)
        if axis.get_legend_handles_labels()[0]:
            outside_legend(axis, title="Mean ± circular sample SD" if metric == "phase_deg"
                           else "Mean ± sample SD")
        if metric == "phase_deg" and not has_usable_phase_points:
            axis.text(0.5, 0.5, "No usable phase points after display thresholds",
                      ha="center", va="center",
                      transform=axis.transAxes)
        figures.append(figure)
    return tuple(figures)
