from __future__ import annotations

import csv
from dataclasses import dataclass, fields
import json
import math
from pathlib import Path
from statistics import fmean, stdev
from typing import Callable, Iterable, Mapping, Sequence


RECORD_STATUSES = frozenset(
    {"completed", "rejected", "diagnostic", "other", "invalid"}
)
SAMPLE_STATUSES = frozenset(
    {"clean", "problem", "unlocked", "overload", "instrument_error"}
)
SWEEP_METRICS = frozenset({"x_v", "y_v", "amplitude_v", "phase_deg"})
SWEEP_X_AXES = frozenset(
    {
        "target_frequency_hz",
        "source_v_rms",
        "nominal_current_a_rms",
        "sine_output_current_a_rms",
    }
)
PLOT_ROLES = ("xx", "xy")
PLOT_HARMONICS = (1, 2, 3)


@dataclass(frozen=True, slots=True)
class CommissioningRecordSummary:
    path: Path
    scan_type: str
    record_status: str
    completed: bool | None
    sample_count: int
    problem_count: int
    modified_ns: int
    error: str | None = None


@dataclass(frozen=True, slots=True)
class CommissioningSample:
    source_path: str
    record_status: str
    scan_type: str
    point_index: int
    sample_index: int
    target_frequency_hz: float
    source_v_rms: float
    sine_output_v_rms: float
    nominal_current_a_rms: float | None
    role: str
    harmonic: int
    x_v: float
    y_v: float
    amplitude_v: float
    phase_deg: float
    reference_frequency_hz: float
    locked: bool
    overload: bool
    lia_status_raw: int
    error_status: int
    statuses: tuple[str, ...]
    problems: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExcitationPathResistance:
    """Known RMS excitation path used only for read-only current estimation."""

    external_series_resistance_ohm: float
    sr830_output_resistance_ohm: float
    approximate_device_resistance_ohm: float

    def __post_init__(self) -> None:
        values = (
            self.external_series_resistance_ohm,
            self.sr830_output_resistance_ohm,
            self.approximate_device_resistance_ohm,
        )
        if any(not math.isfinite(value) or value < 0.0 for value in values):
            raise ValueError("Every excitation-path resistance must be finite and non-negative.")
        if self.total_resistance_ohm <= 0.0:
            raise ValueError("Total excitation-path resistance must be positive.")

    @property
    def total_resistance_ohm(self) -> float:
        return (
            self.external_series_resistance_ohm
            + self.sr830_output_resistance_ohm
            + self.approximate_device_resistance_ohm
        )

    def current_from_sine_output(self, sine_output_v_rms: float) -> float:
        if not math.isfinite(sine_output_v_rms) or sine_output_v_rms < 0.0:
            raise ValueError("SINE OUT voltage must be finite and non-negative.")
        return sine_output_v_rms / self.total_resistance_ohm


@dataclass(frozen=True, slots=True)
class SweepStatistic:
    x_value: float
    role: str
    metric: str
    mean: float
    standard_deviation: float
    count: int


def load_commissioning_file(
    path: str | Path,
) -> dict[str, object] | tuple[dict[str, object], ...]:
    """Open one commissioning JSON or JSONL file without modifying it.

    Commissioning records created by PowerShell may be UTF-16, while records
    created by Python are UTF-8.  Decode the BOM before parsing so both remain
    directly browsable.
    """

    source = Path(path)
    raw = source.read_bytes()
    if raw.startswith((b"\xff\xfe\x00\x00", b"\x00\x00\xfe\xff")):
        text = raw.decode("utf-32")
    elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
    else:
        text = raw.decode("utf-8-sig")
    if source.suffix.lower() == ".json":
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("Commissioning JSON root must be an object.")
        return payload
    if source.suffix.lower() == ".jsonl":
        records: list[dict[str, object]] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError(
                    f"Commissioning JSONL line {line_number} is not an object."
                )
            records.append(record)
        return tuple(records)
    raise ValueError("Commissioning data file must end in .json or .jsonl.")


def summarize_commissioning_file(path: str | Path) -> CommissioningRecordSummary:
    source = Path(path)
    try:
        payload = load_commissioning_file(source)
        if isinstance(payload, tuple):
            return CommissioningRecordSummary(
                path=source,
                scan_type="diagnostic",
                record_status="diagnostic",
                completed=None,
                sample_count=len(payload),
                problem_count=sum(bool(record.get("problems")) for record in payload),
                modified_ns=source.stat().st_mtime_ns,
            )
        completed_value = payload.get("completed")
        completed = completed_value if isinstance(completed_value, bool) else None
        status = (
            "completed"
            if completed is True
            else "rejected"
            if completed is False
            else "other"
        )
        samples = tuple(_formal_sample_payloads(payload))
        return CommissioningRecordSummary(
            path=source,
            scan_type=str(payload.get("scan", "unknown")),
            record_status=status,
            completed=completed,
            sample_count=len(samples),
            problem_count=sum(bool(sample.get("problems")) for sample in samples),
            modified_ns=source.stat().st_mtime_ns,
            error=(None if payload.get("error") is None else str(payload["error"])),
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        modified_ns = source.stat().st_mtime_ns if source.exists() else 0
        return CommissioningRecordSummary(
            path=source,
            scan_type="unknown",
            record_status="invalid",
            completed=None,
            sample_count=0,
            problem_count=0,
            modified_ns=modified_ns,
            error=str(exc),
        )


def discover_commissioning_records(
    directory: str | Path,
    *,
    record_statuses: Iterable[str] | None = None,
    scan_types: Iterable[str] | None = None,
) -> tuple[CommissioningRecordSummary, ...]:
    """Recursively catalog JSON/JSONL files and apply explicit record filters."""

    root = Path(directory)
    selected_statuses = _validated_filter(record_statuses, RECORD_STATUSES, "record")
    selected_scans = None if scan_types is None else frozenset(scan_types)
    paths = tuple(root.rglob("*.json")) + tuple(root.rglob("*.jsonl"))
    summaries = [summarize_commissioning_file(path) for path in paths]
    summaries = [
        summary
        for summary in summaries
        if (selected_statuses is None or summary.record_status in selected_statuses)
        and (selected_scans is None or summary.scan_type in selected_scans)
    ]
    return tuple(sorted(summaries, key=lambda item: item.modified_ns, reverse=True))


def browse_commissioning_file(
    initial_directory: str | Path,
    *,
    chooser: Callable[[Path], str | Path | None] | None = None,
) -> Path | None:
    """Open a native file chooser; an injectable chooser keeps this testable."""

    initial = Path(initial_directory)
    if chooser is not None:
        selected = chooser(initial)
    else:
        try:
            from tkinter import Tk, filedialog

            root = Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            try:
                selected = filedialog.askopenfilename(
                    title="Open SR830 commissioning data",
                    initialdir=str(initial),
                    filetypes=(
                        ("Commissioning data", "*.json *.jsonl"),
                        ("JSON", "*.json"),
                        ("JSON Lines", "*.jsonl"),
                    ),
                )
            finally:
                root.destroy()
        except Exception as exc:
            raise RuntimeError(
                "The native file chooser is unavailable. Set the data Path "
                "directly in the notebook instead."
            ) from exc
    if not selected:
        return None
    path = Path(selected)
    if path.suffix.lower() not in {".json", ".jsonl"}:
        raise ValueError("Selected commissioning file must be JSON or JSONL.")
    return path


def browse_and_load_commissioning_file(
    initial_directory: str | Path,
    *,
    chooser: Callable[[Path], str | Path | None] | None = None,
) -> tuple[Path, dict[str, object] | tuple[dict[str, object], ...]] | None:
    path = browse_commissioning_file(initial_directory, chooser=chooser)
    return None if path is None else (path, load_commissioning_file(path))


def load_sweep_samples(
    path: str | Path,
    *,
    include_rejected: bool = False,
    sample_statuses: Iterable[str] | None = None,
    roles: Iterable[str] | None = None,
) -> tuple[CommissioningSample, ...]:
    payload = load_commissioning_file(path)
    if not isinstance(payload, dict) or payload.get("scan") not in {
        "frequency",
        "excitation",
    }:
        raise ValueError("Selected file is not a frequency or excitation sweep.")
    completed = payload.get("completed") is True
    if not completed and not include_rejected:
        raise ValueError(
            "Rejected or incomplete sweep data requires include_rejected=True."
        )
    selected_statuses = _validated_filter(
        sample_statuses, SAMPLE_STATUSES, "sample"
    )
    selected_roles = None if roles is None else frozenset(roles)
    unknown_roles = set() if selected_roles is None else selected_roles - {"xx", "xy"}
    if unknown_roles:
        raise ValueError(f"Unknown roles: {sorted(unknown_roles)}")
    record_status = "completed" if completed else "rejected"
    rows: list[CommissioningSample] = []
    points = payload.get("points", [])
    if not isinstance(points, list):
        raise ValueError("Sweep points must be a list.")
    for point in points:
        if not isinstance(point, dict):
            raise ValueError("Each sweep point must be an object.")
        samples = point.get("samples", [])
        if not isinstance(samples, list):
            raise ValueError("Sweep point samples must be a list.")
        for sample in samples:
            if not isinstance(sample, dict):
                raise ValueError("Each sweep sample must be an object.")
            problems = tuple(str(problem) for problem in sample.get("problems", []))
            for role in ("xx", "xy"):
                if selected_roles is not None and role not in selected_roles:
                    continue
                row = _commissioning_sample(
                    path=Path(path),
                    record_status=record_status,
                    scan_type=str(payload["scan"]),
                    point=point,
                    sample=sample,
                    role=role,
                    problems=problems,
                )
                if selected_statuses is None or not selected_statuses.isdisjoint(
                    row.statuses
                ):
                    rows.append(row)
    return tuple(rows)


def load_sweep_sample_files(
    paths: Iterable[str | Path],
    *,
    include_rejected: bool = False,
    sample_statuses: Iterable[str] | None = None,
    roles: Iterable[str] | None = None,
) -> tuple[CommissioningSample, ...]:
    """Load selected sweep files without combining frequency and excitation scans."""

    rows = tuple(
        row
        for path in paths
        for row in load_sweep_samples(
            path,
            include_rejected=include_rejected,
            sample_statuses=sample_statuses,
            roles=roles,
        )
    )
    if not rows:
        raise ValueError("At least one selected sweep file with formal samples is required.")
    scan_types = {row.scan_type for row in rows}
    if len(scan_types) != 1:
        raise ValueError("Load frequency and excitation files separately.")
    return rows


def aggregate_sweep_samples(
    rows: Sequence[CommissioningSample],
    *,
    metric: str = "amplitude_v",
    x_axis: str | None = None,
    excitation_path: ExcitationPathResistance | None = None,
) -> tuple[SweepStatistic, ...]:
    if metric not in SWEEP_METRICS:
        raise ValueError(f"Unsupported metric: {metric}")
    if not rows:
        raise ValueError("No sweep samples match the selected filters.")
    scan_types = {row.scan_type for row in rows}
    if len(scan_types) != 1:
        raise ValueError("Aggregate one sweep type at a time.")
    if x_axis is None:
        x_axis = (
            "target_frequency_hz"
            if next(iter(scan_types)) == "frequency"
            else "source_v_rms"
        )
    if x_axis not in SWEEP_X_AXES:
        raise ValueError(f"Unsupported x axis: {x_axis}")
    grouped: dict[tuple[float, str], list[float]] = {}
    for row in rows:
        raw_x = _sweep_x_value(row, x_axis, excitation_path)
        grouped.setdefault((float(raw_x), row.role), []).append(
            float(getattr(row, metric))
        )
    statistics: list[SweepStatistic] = []
    for (x_value, role), values in sorted(grouped.items()):
        mean, spread = _mean_and_standard_deviation(values, metric=metric)
        statistics.append(
            SweepStatistic(
                x_value=x_value,
                role=role,
                metric=metric,
                mean=mean,
                standard_deviation=spread,
                count=len(values),
            )
        )
    return tuple(statistics)


def plot_commissioning_sweep(
    rows: Sequence[CommissioningSample],
    *,
    metric: str = "amplitude_v",
    x_axis: str | None = None,
    log_x: bool | None = None,
    excitation_path: ExcitationPathResistance | None = None,
    destination: str | Path | None = None,
):
    """Plot per-point mean and sample standard deviation for XX and XY."""

    statistics = aggregate_sweep_samples(
        rows, metric=metric, x_axis=x_axis, excitation_path=excitation_path
    )
    scan_type = rows[0].scan_type
    resolved_x_axis = x_axis or (
        "target_frequency_hz" if scan_type == "frequency" else "source_v_rms"
    )
    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError(
            "Plotting requires: python -m pip install -e '.[analysis]'"
        ) from exc
    figure, axis = plt.subplots(figsize=(6.6, 4.4), constrained_layout=True)
    for role in ("xx", "xy"):
        selected = [item for item in statistics if item.role == role]
        if not selected:
            continue
        axis.errorbar(
            [item.x_value for item in selected],
            [item.mean for item in selected],
            yerr=[item.standard_deviation for item in selected],
            marker="o",
            linewidth=1.2,
            capsize=3,
            label=role.upper(),
        )
    use_log_x = log_x if log_x is not None else scan_type == "frequency"
    if use_log_x:
        axis.set_xscale("log")
    axis.set_xlabel(
        {
            "target_frequency_hz": "Frequency (Hz)",
            "source_v_rms": "Source voltage (V RMS)",
            "nominal_current_a_rms": "Nominal current (A RMS)",
            "sine_output_current_a_rms": "SINE OUT current (A RMS)",
        }[resolved_x_axis]
    )
    axis.set_ylabel(
        {
            "x_v": "X (V)",
            "y_v": "Y (V)",
            "amplitude_v": "R (V)",
            "phase_deg": "Phase (degree)",
        }[metric]
    )
    axis.set_title(f"SR830 {scan_type} sweep")
    axis.grid(True, alpha=0.25)
    axis.legend()
    if destination is not None:
        figure.savefig(destination, dpi=200)
    return figure


def plot_role_harmonic_sweep(
    rows: Sequence[CommissioningSample],
    *,
    role: str,
    harmonic: int,
    excitation_path: ExcitationPathResistance,
    phase_minimum_amplitude_v: float = 0.0,
    phase_maximum_standard_deviation_deg: float | None = None,
    destination: str | Path | None = None,
):
    """Plot one role and harmonic with voltage magnitude and phase on twin axes.

    Frequency records retain the calibrated SINE OUT RMS current in the title.
    Excitation records use that same current calculation as the x axis.
    """

    if role not in PLOT_ROLES:
        raise ValueError(f"Unknown role: {role}")
    if harmonic not in PLOT_HARMONICS:
        raise ValueError(f"Unsupported harmonic: {harmonic}")
    if not rows:
        raise ValueError("No sweep samples match the selected filters.")
    if (
        not math.isfinite(phase_minimum_amplitude_v)
        or phase_minimum_amplitude_v < 0.0
    ):
        raise ValueError("Phase minimum amplitude must be finite and non-negative.")
    if phase_maximum_standard_deviation_deg is not None and (
        not math.isfinite(phase_maximum_standard_deviation_deg)
        or phase_maximum_standard_deviation_deg <= 0.0
    ):
        raise ValueError(
            "Phase maximum standard deviation must be finite and positive."
        )
    scan_types = {row.scan_type for row in rows}
    if len(scan_types) != 1:
        raise ValueError("Plot one sweep type at a time.")
    scan_type = next(iter(scan_types))
    selected = tuple(
        row for row in rows if row.role == role and row.harmonic == harmonic
    )
    x_axis = (
        "target_frequency_hz"
        if scan_type == "frequency"
        else "sine_output_current_a_rms"
    )
    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError(
            "Plotting requires: python -m pip install -e '.[analysis]'"
        ) from exc
    figure, voltage_axis = plt.subplots(figsize=(6.6, 4.4), constrained_layout=True)
    phase_axis = voltage_axis.twinx()
    signal_name = f"V{role}"
    if selected:
        voltage_statistics = aggregate_sweep_samples(
            selected,
            metric="amplitude_v",
            x_axis=x_axis,
            excitation_path=excitation_path,
        )
        phase_statistics = aggregate_sweep_samples(
            selected,
            metric="phase_deg",
            x_axis=x_axis,
            excitation_path=excitation_path,
        )
        voltage_axis.errorbar(
            [item.x_value for item in voltage_statistics],
            [item.mean for item in voltage_statistics],
            yerr=[item.standard_deviation for item in voltage_statistics],
            marker="o",
            linewidth=1.2,
            capsize=3,
            color="tab:blue",
            label=f"{signal_name} R",
        )
        (
            phase_x_values,
            phase_values,
            qualified_phase_x_values,
            qualified_phase_values,
            qualified_phase_spreads,
        ) = _phase_plot_values(
            phase_statistics,
            voltage_statistics,
            minimum_amplitude_v=phase_minimum_amplitude_v,
            maximum_standard_deviation_deg=phase_maximum_standard_deviation_deg,
        )
        qualified_count = len(qualified_phase_x_values)
        phase_label = (
            "Phase"
            if qualified_count == len(phase_statistics)
            else f"Phase qualified ({qualified_count}/{len(phase_statistics)})"
        )
        phase_axis.plot(
            phase_x_values,
            phase_values,
            marker="s",
            linewidth=1.2,
            color="tab:orange",
            label=phase_label,
        )
        if qualified_phase_x_values:
            phase_axis.errorbar(
                qualified_phase_x_values,
                qualified_phase_values,
                yerr=qualified_phase_spreads,
                fmt="none",
                capsize=3,
                color="tab:orange",
            )
    else:
        voltage_axis.text(
            0.5,
            0.5,
            f"No selected {signal_name} h{harmonic} samples",
            ha="center",
            va="center",
            transform=voltage_axis.transAxes,
        )
    if scan_type == "frequency":
        voltage_axis.set_xscale("log")
        voltage_axis.set_xlabel("Frequency (Hz)")
        title_prefix = _current_summary(selected, excitation_path)
        title = f"Frequency sweep · {signal_name} / phase · h{harmonic} · {title_prefix}"
    else:
        voltage_axis.set_xlabel("SINE OUT current (A RMS)")
        title = f"Current–voltage sweep · {signal_name} / phase · h{harmonic}"
    voltage_axis.set_ylabel(f"{signal_name} R (V RMS)")
    phase_axis.set_ylabel("Unwrapped phase (degree)")
    voltage_axis.set_title(title)
    voltage_axis.grid(True, alpha=0.25)
    left_handles, left_labels = voltage_axis.get_legend_handles_labels()
    right_handles, right_labels = phase_axis.get_legend_handles_labels()
    if left_handles or right_handles:
        voltage_axis.legend(left_handles + right_handles, left_labels + right_labels)
    if destination is not None:
        figure.savefig(destination, dpi=200)
    return figure


def plot_six_role_harmonic_sweeps(
    rows: Sequence[CommissioningSample],
    *,
    excitation_path: ExcitationPathResistance,
    phase_minimum_amplitude_v: float = 0.0,
    phase_maximum_standard_deviation_deg: float | None = None,
) -> dict[tuple[str, int], object]:
    """Return the requested separate XX/XY × h1/h2/h3 sweep figures."""

    return {
        (role, harmonic): plot_role_harmonic_sweep(
            rows,
            role=role,
            harmonic=harmonic,
            excitation_path=excitation_path,
            phase_minimum_amplitude_v=phase_minimum_amplitude_v,
            phase_maximum_standard_deviation_deg=(
                phase_maximum_standard_deviation_deg
            ),
        )
        for role in PLOT_ROLES
        for harmonic in PLOT_HARMONICS
    }


def _phase_plot_values(
    phase_statistics: Sequence[SweepStatistic],
    voltage_statistics: Sequence[SweepStatistic],
    *,
    minimum_amplitude_v: float,
    maximum_standard_deviation_deg: float | None,
) -> tuple[list[float], list[float], list[float], list[float], list[float]]:
    """Unwrap contiguous qualified phase segments without altering raw statistics."""

    amplitude_by_x = {item.x_value: item.mean for item in voltage_statistics}
    phase_values = [math.nan] * len(phase_statistics)
    qualified_x_values: list[float] = []
    qualified_values: list[float] = []
    qualified_spreads: list[float] = []
    segment: list[int] = []

    def flush_segment() -> None:
        if not segment:
            return
        previous_wrapped: float | None = None
        previous_unwrapped: float | None = None
        for index in segment:
            wrapped = phase_statistics[index].mean
            if previous_wrapped is None or previous_unwrapped is None:
                unwrapped = wrapped
            else:
                delta = (wrapped - previous_wrapped + 180.0) % 360.0 - 180.0
                unwrapped = previous_unwrapped + delta
            phase_values[index] = unwrapped
            qualified_x_values.append(phase_statistics[index].x_value)
            qualified_values.append(unwrapped)
            qualified_spreads.append(phase_statistics[index].standard_deviation)
            previous_wrapped = wrapped
            previous_unwrapped = unwrapped
        segment.clear()

    for index, statistic in enumerate(phase_statistics):
        amplitude = amplitude_by_x[statistic.x_value]
        stable = (
            maximum_standard_deviation_deg is None
            or statistic.standard_deviation <= maximum_standard_deviation_deg
        )
        if amplitude >= minimum_amplitude_v and stable:
            segment.append(index)
        else:
            flush_segment()
    flush_segment()
    return (
        [item.x_value for item in phase_statistics],
        phase_values,
        qualified_x_values,
        qualified_values,
        qualified_spreads,
    )


def export_commissioning_csv(
    rows: Sequence[CommissioningSample], destination: str | Path
) -> None:
    names = [field.name for field in fields(CommissioningSample)]
    with Path(destination).open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=names)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    name: (
                        "|".join(getattr(row, name))
                        if isinstance(getattr(row, name), tuple)
                        else getattr(row, name)
                    )
                    for name in names
                }
            )


def _formal_sample_payloads(payload: Mapping[str, object]):
    points = payload.get("points", [])
    if not isinstance(points, list):
        return
    for point in points:
        if not isinstance(point, dict):
            continue
        samples = point.get("samples", [])
        if isinstance(samples, list):
            yield from (sample for sample in samples if isinstance(sample, dict))


def _commissioning_sample(
    *,
    path: Path,
    record_status: str,
    scan_type: str,
    point: Mapping[str, object],
    sample: Mapping[str, object],
    role: str,
    problems: tuple[str, ...],
) -> CommissioningSample:
    instrument = sample.get(f"lockin_{role}")
    if not isinstance(instrument, dict):
        raise ValueError(f"Sweep sample is missing lockin_{role} data.")
    reading = instrument.get("reading")
    lia_status = instrument.get("lia_status")
    if not isinstance(reading, dict) or not isinstance(lia_status, dict):
        raise ValueError(f"lockin_{role} sample is missing reading/status data.")
    unlocked = bool(lia_status.get("reference_unlocked")) or not bool(
        reading.get("locked")
    )
    overload = bool(reading.get("overload")) or any(
        bool(lia_status.get(name))
        for name in (
            "input_or_reserve_overload",
            "filter_overload",
            "output_overload",
        )
    )
    error_status = int(instrument.get("error_status", 0))
    statuses: list[str] = []
    if problems:
        statuses.append("problem")
    if unlocked:
        statuses.append("unlocked")
    if overload:
        statuses.append("overload")
    if error_status:
        statuses.append("instrument_error")
    if not statuses:
        statuses.append("clean")
    nominal_current = point.get("nominal_current_a_rms")
    sine_output = point.get("source_readback_v_rms")
    if sine_output is None:
        sine_output = point["source_v_rms"]
    return CommissioningSample(
        source_path=str(path),
        record_status=record_status,
        scan_type=scan_type,
        point_index=int(point.get("point_index", 0)),
        sample_index=int(sample.get("sample_index", 0)),
        target_frequency_hz=float(point["target_frequency_hz"]),
        source_v_rms=float(point["source_v_rms"]),
        sine_output_v_rms=float(sine_output),
        nominal_current_a_rms=(
            None if nominal_current is None else float(nominal_current)
        ),
        role=role,
        harmonic=int(reading["harmonic"]),
        x_v=float(reading["x_v"]),
        y_v=float(reading["y_v"]),
        amplitude_v=float(reading["amplitude_v"]),
        phase_deg=float(reading["phase_deg"]),
        reference_frequency_hz=float(reading["frequency_hz"]),
        locked=bool(reading["locked"]),
        overload=overload,
        lia_status_raw=int(lia_status.get("raw", 0)),
        error_status=error_status,
        statuses=tuple(statuses),
        problems=problems,
    )


def _validated_filter(
    selected: Iterable[str] | None,
    allowed: frozenset[str],
    label: str,
) -> frozenset[str] | None:
    if selected is None:
        return None
    values = frozenset(selected)
    unknown = values - allowed
    if unknown:
        raise ValueError(f"Unknown {label} statuses: {sorted(unknown)}")
    return values


def _sweep_x_value(
    row: CommissioningSample,
    x_axis: str,
    excitation_path: ExcitationPathResistance | None,
) -> float:
    if x_axis == "sine_output_current_a_rms":
        if excitation_path is None:
            raise ValueError(
                "SINE OUT current requires an explicit ExcitationPathResistance."
            )
        return excitation_path.current_from_sine_output(row.sine_output_v_rms)
    raw_x = getattr(row, x_axis)
    if raw_x is None:
        raise ValueError(f"Selected x axis {x_axis} contains missing values.")
    return float(raw_x)


def _current_summary(
    rows: Sequence[CommissioningSample],
    excitation_path: ExcitationPathResistance,
) -> str:
    if not rows:
        return "I_RMS unavailable"
    currents = [
        excitation_path.current_from_sine_output(row.sine_output_v_rms)
        for row in rows
    ]
    minimum = min(currents)
    maximum = max(currents)
    if math.isclose(minimum, maximum, rel_tol=1e-12, abs_tol=0.0):
        return f"I_RMS = {_format_engineering(minimum, 'A')}"
    return (
        "I_RMS = "
        f"{_format_engineering(minimum, 'A')}–{_format_engineering(maximum, 'A')}"
    )


def _format_engineering(value: float, unit: str) -> str:
    for scale, prefix in ((1.0, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"), (1e-12, "p")):
        if value >= scale or scale == 1e-12:
            return f"{value / scale:.4g} {prefix}{unit}"
    raise AssertionError("Unreachable engineering-format scale.")


def _mean_and_standard_deviation(
    values: Sequence[float], *, metric: str
) -> tuple[float, float]:
    if metric != "phase_deg":
        return fmean(values), stdev(values) if len(values) > 1 else 0.0
    sine_mean = fmean(math.sin(math.radians(value)) for value in values)
    cosine_mean = fmean(math.cos(math.radians(value)) for value in values)
    mean = math.degrees(math.atan2(sine_mean, cosine_mean))
    resultant = min(1.0, math.hypot(sine_mean, cosine_mean))
    spread = (
        180.0
        if resultant <= 0.0
        else math.degrees(math.sqrt(-2.0 * math.log(resultant)))
    )
    return mean, spread
