"""Read-only XY-only harmonic analysis for SR830 commissioning records."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from statistics import fmean, stdev
from typing import Iterable, Sequence

from .commissioning_analysis import load_commissioning_file


HARMONIC_METRICS = frozenset({"x_v", "y_v", "amplitude_v", "phase_deg"})
SAMPLE_STATUSES = frozenset(
    {"clean", "problem", "unlocked", "overload", "instrument_error"}
)


@dataclass(frozen=True, slots=True)
class XYHarmonicSample:
    """One formal XY reading from a paired harmonic measurement."""

    source_path: str
    record_status: str
    harmonic: int
    x_v: float
    y_v: float
    amplitude_v: float
    phase_deg: float
    frequency_hz: float
    locked: bool
    overload: bool
    error_status: int
    statuses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class XYHarmonicStatistic:
    harmonic: int
    metric: str
    mean: float
    standard_deviation: float
    count: int


def load_xy_harmonic_samples(
    path: str | Path,
    *,
    include_rejected: bool = False,
    sample_statuses: Iterable[str] | None = None,
) -> tuple[XYHarmonicSample, ...]:
    """Load only the XY readings from one paired harmonic JSON record.

    XX readings are intentionally discarded at load time. Rejected records
    require an explicit audit opt-in, matching the commissioning sweep loader.
    """

    source = Path(path)
    if source.suffix.lower() != ".json":
        raise ValueError("Harmonic data file must end in .json.")
    payload = load_commissioning_file(source)
    if not isinstance(payload, dict) or not isinstance(payload.get("readings"), list):
        raise ValueError("Selected file is not a paired harmonic record.")
    completed = payload.get("completed") is True
    if not completed and not include_rejected:
        raise ValueError(
            "Rejected or incomplete harmonic data requires include_rejected=True."
        )
    selected_statuses = _validated_status_filter(sample_statuses)
    record_status = "completed" if completed else "rejected"
    rows: list[XYHarmonicSample] = []
    for reading in payload["readings"]:
        if not isinstance(reading, dict):
            raise ValueError("Each harmonic reading must be an object.")
        if str(reading.get("role", "")).lower() != "xy":
            continue
        row = _xy_harmonic_sample(source, record_status, reading)
        if selected_statuses is None or not selected_statuses.isdisjoint(
            row.statuses
        ):
            rows.append(row)
    if not rows:
        raise ValueError("The harmonic record contains no XY readings.")
    return tuple(rows)


def discover_xy_harmonic_records(
    directory: str | Path,
    *,
    record_statuses: Iterable[str] = ("completed",),
) -> tuple[Path, ...]:
    """Find harmonic JSON records newest first, without opening instruments."""

    selected = frozenset(record_statuses)
    unknown = selected - {"completed", "rejected"}
    if unknown:
        raise ValueError(f"Unknown record statuses: {sorted(unknown)}")
    root = Path(directory)
    records: list[tuple[int, Path, str]] = []
    for path in root.glob("*.json"):
        try:
            payload = load_commissioning_file(path)
        except (OSError, ValueError, UnicodeError):
            continue
        if not isinstance(payload, dict) or not isinstance(
            payload.get("readings"), list
        ):
            continue
        status = "completed" if payload.get("completed") is True else "rejected"
        if status in selected:
            records.append((path.stat().st_mtime_ns, path, status))
    return tuple(path for _, path, _ in sorted(records, reverse=True))


def aggregate_xy_harmonics(
    rows: Sequence[XYHarmonicSample],
    *,
    metric: str = "amplitude_v",
) -> tuple[XYHarmonicStatistic, ...]:
    """Aggregate XY readings by harmonic order for plotting."""

    _validate_metric(metric)
    if not rows:
        raise ValueError("No XY harmonic samples match the selected filters.")
    values: dict[int, list[float]] = {}
    for row in rows:
        values.setdefault(row.harmonic, []).append(float(getattr(row, metric)))
    return tuple(
        XYHarmonicStatistic(
            harmonic=harmonic,
            metric=metric,
            mean=fmean(measurements),
            standard_deviation=(
                stdev(measurements) if len(measurements) > 1 else 0.0
            ),
            count=len(measurements),
        )
        for harmonic, measurements in sorted(values.items())
    )


def plot_xy_harmonics(
    rows: Sequence[XYHarmonicSample],
    *,
    metric: str = "amplitude_v",
    destination: str | Path | None = None,
):
    """Plot XY only, with an ``hN`` annotation at every harmonic point."""

    statistics = aggregate_xy_harmonics(rows, metric=metric)
    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError(
            "Plotting requires: python -m pip install -e '.[analysis]'"
        ) from exc
    figure, axis = plt.subplots(figsize=(6.6, 4.4), constrained_layout=True)
    x_values = [item.harmonic for item in statistics]
    axis.errorbar(
        x_values,
        [item.mean for item in statistics],
        yerr=[item.standard_deviation for item in statistics],
        marker="o",
        linewidth=1.2,
        capsize=3,
        label="XY",
    )
    for item in statistics:
        axis.annotate(
            f"h{item.harmonic}",
            (item.harmonic, item.mean),
            xytext=(0, 8),
            textcoords="offset points",
            ha="center",
        )
    axis.set_xticks(x_values, [f"h{value}" for value in x_values])
    axis.set_xlabel("Harmonic order")
    axis.set_ylabel(
        {
            "x_v": "X (V)",
            "y_v": "Y (V)",
            "amplitude_v": "R (V)",
            "phase_deg": "Phase (degree)",
        }[metric]
    )
    axis.set_title("SR830 XY-only harmonic response")
    axis.grid(True, alpha=0.25)
    axis.legend()
    if destination is not None:
        figure.savefig(destination, dpi=200)
    return figure


def _xy_harmonic_sample(
    source: Path,
    record_status: str,
    reading: dict[str, object],
) -> XYHarmonicSample:
    harmonic = int(reading["harmonic"])
    if harmonic < 1:
        raise ValueError("Harmonic order must be positive.")
    locked = bool(reading.get("locked"))
    overload = bool(reading.get("overload"))
    error_status = int(reading.get("error_status", 0))
    problems = reading.get("problems", ())
    statuses: list[str] = []
    if isinstance(problems, (list, tuple)) and problems:
        statuses.append("problem")
    if not locked:
        statuses.append("unlocked")
    if overload:
        statuses.append("overload")
    if error_status:
        statuses.append("instrument_error")
    if not statuses:
        statuses.append("clean")
    return XYHarmonicSample(
        source_path=str(source),
        record_status=record_status,
        harmonic=harmonic,
        x_v=float(reading["x_v"]),
        y_v=float(reading["y_v"]),
        amplitude_v=float(reading["amplitude_v"]),
        phase_deg=float(reading["phase_deg"]),
        frequency_hz=float(reading["frequency_hz"]),
        locked=locked,
        overload=overload,
        error_status=error_status,
        statuses=tuple(statuses),
    )


def _validated_status_filter(
    selected: Iterable[str] | None,
) -> frozenset[str] | None:
    if selected is None:
        return None
    values = frozenset(selected)
    unknown = values - SAMPLE_STATUSES
    if unknown:
        raise ValueError(f"Unknown sample statuses: {sorted(unknown)}")
    return values


def _validate_metric(metric: str) -> None:
    if metric not in HARMONIC_METRICS:
        raise ValueError(f"Unsupported metric: {metric}")
