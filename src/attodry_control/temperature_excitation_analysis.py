"""Read-only temperature-stacked I--Vxx/Vxy analysis."""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, fields
import math
from pathlib import Path
from statistics import fmean, stdev
from typing import Iterable, Mapping, Sequence

from .commissioning_analysis import (
    PLOT_HARMONICS,
    PLOT_ROLES,
    SAMPLE_STATUSES,
    load_commissioning_file,
)
from .scientific_plotting import (
    PUBLICATION_WIDE_FIGSIZE,
    ordered_series_style,
    outside_legend,
    publication_plot,
    save_publication_figure,
    style_axis,
)


TEMPERATURE_IV_METRICS = ("amplitude_v", "phase_deg")
_ROLE_NAMES = {
    "xx": "xx",
    "xy": "xy",
    "lockin_xx": "xx",
    "lockin_xy": "xy",
}


@dataclass(frozen=True, slots=True)
class TemperatureExcitationSample:
    source_path: str
    temperature_index: int
    requested_temperature_k: float
    stability_measurement_temperature_k: float
    condition_measurement_temperature_k: float
    sample_measurement_temperature_k: float
    point_index: int
    sample_index: int
    source_v_rms: float
    source_readback_v_rms: float
    current_a_rms: float
    role: str
    harmonic: int
    x_v: float
    y_v: float
    amplitude_v: float
    phase_deg: float
    frequency_hz: float
    lia_status_raw: int
    error_status: int
    statuses: tuple[str, ...]
    problems: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TemperatureIVStatistic:
    source_path: str
    temperature_index: int
    requested_temperature_k: float
    condition_measurement_temperature_k: float
    current_a_rms: float
    role: str
    harmonic: int
    metric: str
    mean: float
    standard_deviation: float
    count: int


def discover_temperature_excitation_records(
    directory: str | Path,
) -> tuple[Path, ...]:
    """Discover final summary JSON, falling back to its matching formal CSV."""

    root = Path(directory).expanduser().resolve()
    if root.is_file():
        return (root,) if _is_temperature_excitation_result(root) else ()
    if not root.exists():
        return ()
    summaries = {
        _temperature_result_key(path): path
        for path in root.rglob("*_temperature_excitation_summary.json")
        if path.is_file()
    }
    csv_records = {
        _temperature_result_key(path): path
        for path in root.rglob("*_temperature_excitation_formal_samples.csv")
        if path.is_file()
    }
    selected = list(summaries.values()) + [
        path for key, path in csv_records.items() if key not in summaries
    ]
    return tuple(
        sorted(
            selected,
            key=lambda path: (path.stat().st_mtime_ns, str(path)),
            reverse=True,
        )
    )


def load_temperature_excitation_samples(
    path: str | Path,
    *,
    sample_statuses: Iterable[str] | None = ("clean",),
) -> tuple[TemperatureExcitationSample, ...]:
    """Load completed temperature conditions from a summary JSON or formal CSV."""

    source = Path(path).expanduser().resolve()
    selected_statuses = _validated_status_filter(sample_statuses)
    if source.suffix.lower() == ".csv":
        rows = _load_formal_csv(source)
    else:
        rows = _load_summary_json(source)
    if selected_statuses is None:
        return rows
    return tuple(
        row for row in rows if not selected_statuses.isdisjoint(row.statuses)
    )


def load_temperature_excitation_sample_files(
    paths: Iterable[str | Path],
    *,
    sample_statuses: Iterable[str] | None = ("clean",),
) -> tuple[TemperatureExcitationSample, ...]:
    rows = tuple(
        row
        for path in paths
        for row in load_temperature_excitation_samples(
            path, sample_statuses=sample_statuses
        )
    )
    if not rows:
        raise ValueError(
            "At least one selected temperature-excitation file with formal samples "
            "is required."
        )
    return rows


def aggregate_temperature_iv(
    rows: Sequence[TemperatureExcitationSample],
    *,
    role: str,
    harmonic: int,
    metric: str,
) -> tuple[TemperatureIVStatistic, ...]:
    """Aggregate repeats without combining different runs or temperatures."""

    if role not in PLOT_ROLES:
        raise ValueError(f"Unknown role: {role}")
    if harmonic not in PLOT_HARMONICS:
        raise ValueError(f"Unsupported harmonic: {harmonic}")
    if metric not in TEMPERATURE_IV_METRICS:
        raise ValueError(f"Unsupported temperature I-V metric: {metric}")
    selected = tuple(
        row for row in rows if row.role == role and row.harmonic == harmonic
    )
    if not selected:
        raise ValueError(f"No selected V{role} h{harmonic} samples.")
    grouped: dict[
        tuple[str, int, float, float, float], list[TemperatureExcitationSample]
    ] = {}
    for row in selected:
        key = (
            row.source_path,
            row.temperature_index,
            row.requested_temperature_k,
            row.condition_measurement_temperature_k,
            row.current_a_rms,
        )
        grouped.setdefault(key, []).append(row)
    statistics: list[TemperatureIVStatistic] = []
    for key, grouped_rows in sorted(
        grouped.items(),
        key=lambda item: (item[0][3], item[0][0], item[0][1], item[0][4]),
    ):
        values = [float(getattr(row, metric)) for row in grouped_rows]
        mean, spread = _mean_and_standard_deviation(values, metric=metric)
        source_path, temperature_index, requested_k, measured_k, current = key
        statistics.append(
            TemperatureIVStatistic(
                source_path=source_path,
                temperature_index=temperature_index,
                requested_temperature_k=requested_k,
                condition_measurement_temperature_k=measured_k,
                current_a_rms=current,
                role=role,
                harmonic=harmonic,
                metric=metric,
                mean=mean,
                standard_deviation=spread,
                count=len(values),
            )
        )
    return tuple(statistics)


@publication_plot
def plot_temperature_iv_curves(
    rows: Sequence[TemperatureExcitationSample],
    *,
    role: str,
    harmonic: int,
    metric: str,
    destination: str | Path | None = None,
):
    """Plot one Vxx/Vxy harmonic and metric with one curve per actual temperature."""

    statistics = aggregate_temperature_iv(
        rows, role=role, harmonic=harmonic, metric=metric
    )
    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError(
            "Plotting requires: python -m pip install -e '.[analysis]'"
        ) from exc
    figure, axis = plt.subplots(
        figsize=PUBLICATION_WIDE_FIGSIZE, constrained_layout=True
    )
    curve_keys = sorted(
        {
            (
                item.source_path,
                item.temperature_index,
                item.requested_temperature_k,
                item.condition_measurement_temperature_k,
            )
            for item in statistics
        },
        key=lambda key: (key[3], key[0], key[1]),
    )
    temperatures = [key[3] for key in curve_keys]
    duplicate_temperatures = {
        value for value in temperatures if temperatures.count(value) > 1
    }
    for index, curve_key in enumerate(curve_keys):
        source_path, temperature_index, _, measured_k = curve_key
        selected = [
            item
            for item in statistics
            if (
                item.source_path,
                item.temperature_index,
                item.requested_temperature_k,
                item.condition_measurement_temperature_k,
            )
            == curve_key
        ]
        selected.sort(key=lambda item: item.current_a_rms)
        y_values = [item.mean for item in selected]
        if metric == "phase_deg":
            y_values = _unwrap_degrees(y_values)
        label = f"{measured_k:.6g} K"
        if measured_k in duplicate_temperatures:
            label += f" · {Path(source_path).stem} · T#{temperature_index}"
        axis.errorbar(
            [item.current_a_rms for item in selected],
            y_values,
            yerr=[item.standard_deviation for item in selected],
            **ordered_series_style(
                index,
                len(curve_keys),
                colormap_name="plasma",
            ),
            linewidth=1.35,
            markersize=4.5,
            markeredgewidth=0.7,
            capsize=2.5,
            elinewidth=0.8,
            label=label,
        )
    if statistics and all(item.current_a_rms > 0.0 for item in statistics):
        axis.set_xscale("log")
    signal_name = f"V{role}"
    axis.set_xlabel("SINE OUT current (A RMS)")
    axis.set_ylabel(
        f"{signal_name} R (V RMS)"
        if metric == "amplitude_v"
        else f"{signal_name} phase (degree)"
    )
    metric_name = "amplitude" if metric == "amplitude_v" else "phase"
    axis.set_title(
        f"Temperature–excitation · {signal_name} {metric_name} · h{harmonic}"
    )
    style_axis(axis)
    uncertainty = (
        "circular sample SD" if metric == "phase_deg" else "sample SD"
    )
    outside_legend(
        axis,
        title=f"Actual mean temperature\nError bars: {uncertainty}",
    )
    if destination is not None:
        save_publication_figure(figure, destination)
    return figure


def plot_temperature_iv_suite(
    rows: Sequence[TemperatureExcitationSample],
) -> dict[tuple[str, int, str], object]:
    """Return separate amplitude and phase figures for every available channel."""

    return {
        (role, harmonic, metric): plot_temperature_iv_curves(
            rows, role=role, harmonic=harmonic, metric=metric
        )
        for role in PLOT_ROLES
        for harmonic in PLOT_HARMONICS
        if any(row.role == role and row.harmonic == harmonic for row in rows)
        for metric in TEMPERATURE_IV_METRICS
    }


def export_temperature_excitation_csv(
    rows: Sequence[TemperatureExcitationSample], destination: str | Path
) -> Path:
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    names = [field.name for field in fields(TemperatureExcitationSample)]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=names)
        writer.writeheader()
        for row in rows:
            payload = asdict(row)
            payload["statuses"] = "|".join(row.statuses)
            payload["problems"] = "|".join(row.problems)
            writer.writerow(payload)
    return path


def _load_summary_json(path: Path) -> tuple[TemperatureExcitationSample, ...]:
    payload = load_commissioning_file(path)
    if not isinstance(payload, Mapping) or payload.get("command") != (
        "temperature-excitation-scan"
    ):
        raise ValueError("Selected JSON is not a temperature-excitation summary.")
    conditions = payload.get("temperature_conditions")
    if not isinstance(conditions, list):
        raise ValueError("Temperature-excitation summary has no condition list.")
    rows: list[TemperatureExcitationSample] = []
    for condition in conditions:
        if not isinstance(condition, Mapping):
            raise ValueError("Temperature condition must be an object.")
        excitation = condition.get("excitation")
        if not isinstance(excitation, Mapping):
            continue
        cleanup = excitation.get("cleanup")
        if not (
            excitation.get("completed") is True
            and excitation.get("outcome") == "completed"
            and isinstance(cleanup, Mapping)
            and cleanup.get("verified") is True
        ):
            continue
        rows.extend(_summary_condition_rows(path, condition, excitation))
    return tuple(rows)


def _summary_condition_rows(
    path: Path,
    condition: Mapping[str, object],
    excitation: Mapping[str, object],
) -> list[TemperatureExcitationSample]:
    temperature_index = _integer(condition.get("temperature_index"), "temperature_index")
    requested_k = _finite_float(
        condition.get("requested_temperature_k"), "requested_temperature_k"
    )
    stability_k = _finite_float(
        condition.get("stability_measurement_temperature_k"),
        "stability_measurement_temperature_k",
    )
    measured_k = _finite_float(
        condition.get("measurement_temperature_k"), "measurement_temperature_k"
    )
    points = excitation.get("points")
    if not isinstance(points, list):
        raise ValueError("Completed excitation record has no point list.")
    rows: list[TemperatureExcitationSample] = []
    for point_offset, point in enumerate(points):
        if not isinstance(point, Mapping):
            raise ValueError("Excitation point must be an object.")
        current = _finite_float(
            point.get("nominal_current_a_rms"), "nominal_current_a_rms"
        )
        if current <= 0.0:
            raise ValueError("nominal_current_a_rms must be positive.")
        point_index = _integer(point.get("point_index", point_offset), "point_index")
        source_v = _finite_float(point.get("source_v_rms"), "source_v_rms")
        source_readback = _finite_float(
            point.get("source_readback_v_rms"), "source_readback_v_rms"
        )
        samples = point.get("samples")
        if not isinstance(samples, list):
            raise ValueError("Excitation point has no formal sample list.")
        for sample in samples:
            if not isinstance(sample, Mapping):
                raise ValueError("Formal sample must be an object.")
            harmonic = _integer(sample.get("harmonic"), "harmonic")
            if harmonic not in PLOT_HARMONICS:
                raise ValueError(f"Unsupported harmonic: {harmonic}")
            sample_temperature = _finite_float(
                sample.get("measurement_temperature_k"),
                "sample measurement_temperature_k",
            )
            sample_index = _integer(sample.get("sample_index", 0), "sample_index")
            problems_value = sample.get("problems", ())
            if not isinstance(problems_value, (list, tuple)):
                raise ValueError("Formal sample problems must be a list.")
            problems = tuple(str(problem) for problem in problems_value)
            for role in _selected_roles(sample):
                rows.append(
                    _summary_sample_row(
                        path=path,
                        temperature_index=temperature_index,
                        requested_k=requested_k,
                        stability_k=stability_k,
                        measured_k=measured_k,
                        sample_temperature=sample_temperature,
                        point_index=point_index,
                        sample_index=sample_index,
                        source_v=source_v,
                        source_readback=source_readback,
                        current=current,
                        role=role,
                        harmonic=harmonic,
                        sample=sample,
                        problems=problems,
                    )
                )
    return rows


def _summary_sample_row(
    *,
    path: Path,
    temperature_index: int,
    requested_k: float,
    stability_k: float,
    measured_k: float,
    sample_temperature: float,
    point_index: int,
    sample_index: int,
    source_v: float,
    source_readback: float,
    current: float,
    role: str,
    harmonic: int,
    sample: Mapping[str, object],
    problems: tuple[str, ...],
) -> TemperatureExcitationSample:
    instrument = sample.get(f"lockin_{role}")
    if not isinstance(instrument, Mapping):
        raise ValueError(f"Formal sample is missing lockin_{role} data.")
    reading = instrument.get("reading")
    lia_status = instrument.get("lia_status")
    if not isinstance(reading, Mapping) or not isinstance(lia_status, Mapping):
        raise ValueError(f"lockin_{role} sample is missing reading/status data.")
    raw = _integer(lia_status.get("raw", 0), "lia_status.raw")
    error_status = _integer(instrument.get("error_status", 0), "error_status")
    statuses = _sample_statuses(
        reading=reading,
        lia_status=lia_status,
        raw=raw,
        error_status=error_status,
        problems=problems,
    )
    return TemperatureExcitationSample(
        source_path=str(path),
        temperature_index=temperature_index,
        requested_temperature_k=requested_k,
        stability_measurement_temperature_k=stability_k,
        condition_measurement_temperature_k=measured_k,
        sample_measurement_temperature_k=sample_temperature,
        point_index=point_index,
        sample_index=sample_index,
        source_v_rms=source_v,
        source_readback_v_rms=source_readback,
        current_a_rms=current,
        role=role,
        harmonic=harmonic,
        x_v=_finite_float(reading.get("x_v"), "x_v"),
        y_v=_finite_float(reading.get("y_v"), "y_v"),
        amplitude_v=_finite_float(reading.get("amplitude_v"), "amplitude_v"),
        phase_deg=_finite_float(reading.get("phase_deg"), "phase_deg"),
        frequency_hz=_finite_float(reading.get("frequency_hz"), "frequency_hz"),
        lia_status_raw=raw,
        error_status=error_status,
        statuses=statuses,
        problems=problems,
    )


def _load_formal_csv(path: Path) -> tuple[TemperatureExcitationSample, ...]:
    rows: list[TemperatureExcitationSample] = []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        for offset, raw_row in enumerate(reader):
            role = _normalize_role(raw_row.get("role"))
            raw_status = _integer(raw_row.get("lia_status_raw", 0), "lia_status_raw")
            error_status = _integer(raw_row.get("error_status", 0), "error_status")
            statuses = _sample_statuses(
                reading={},
                lia_status={},
                raw=raw_status,
                error_status=error_status,
                problems=(),
            )
            current = _finite_float(
                raw_row.get("nominal_current_a_rms"), "nominal_current_a_rms"
            )
            if current <= 0.0:
                raise ValueError("nominal_current_a_rms must be positive.")
            rows.append(
                TemperatureExcitationSample(
                    source_path=str(path),
                    temperature_index=_integer(
                        raw_row.get("temperature_index"), "temperature_index"
                    ),
                    requested_temperature_k=_finite_float(
                        raw_row.get("requested_temperature_k"),
                        "requested_temperature_k",
                    ),
                    stability_measurement_temperature_k=_finite_float(
                        raw_row.get("stability_measurement_temperature_k"),
                        "stability_measurement_temperature_k",
                    ),
                    condition_measurement_temperature_k=_finite_float(
                        raw_row.get("condition_measurement_temperature_k"),
                        "condition_measurement_temperature_k",
                    ),
                    sample_measurement_temperature_k=_finite_float(
                        raw_row.get("measurement_temperature_k"),
                        "measurement_temperature_k",
                    ),
                    point_index=offset,
                    sample_index=_integer(
                        raw_row.get("sample_index", 0), "sample_index"
                    ),
                    source_v_rms=_finite_float(
                        raw_row.get("source_v_rms"), "source_v_rms"
                    ),
                    source_readback_v_rms=_finite_float(
                        raw_row.get("source_readback_v_rms"),
                        "source_readback_v_rms",
                    ),
                    current_a_rms=current,
                    role=role,
                    harmonic=_integer(raw_row.get("harmonic"), "harmonic"),
                    x_v=_finite_float(raw_row.get("x_v"), "x_v"),
                    y_v=_finite_float(raw_row.get("y_v"), "y_v"),
                    amplitude_v=_finite_float(
                        raw_row.get("amplitude_v"), "amplitude_v"
                    ),
                    phase_deg=_finite_float(raw_row.get("phase_deg"), "phase_deg"),
                    frequency_hz=_finite_float(
                        raw_row.get("frequency_hz"), "frequency_hz"
                    ),
                    lia_status_raw=raw_status,
                    error_status=error_status,
                    statuses=statuses,
                    problems=(),
                )
            )
    return tuple(rows)


def _selected_roles(sample: Mapping[str, object]) -> tuple[str, ...]:
    selected = sample.get("selected_roles")
    if selected is None:
        return PLOT_ROLES
    if not isinstance(selected, list) or not selected:
        raise ValueError("Formal sample selected_roles must be a non-empty list.")
    roles = tuple(_normalize_role(role) for role in selected)
    if len(set(roles)) != len(roles):
        raise ValueError("Formal sample selected_roles contains a duplicate role.")
    return roles


def _normalize_role(value: object) -> str:
    role = _ROLE_NAMES.get(str(value))
    if role is None:
        raise ValueError(f"Unknown lock-in role: {value}")
    return role


def _sample_statuses(
    *,
    reading: Mapping[str, object],
    lia_status: Mapping[str, object],
    raw: int,
    error_status: int,
    problems: tuple[str, ...],
) -> tuple[str, ...]:
    statuses: list[str] = []
    if problems or raw & ~0b1111:
        statuses.append("problem")
    unlocked = bool(lia_status.get("reference_unlocked")) or (
        "locked" in reading and not bool(reading["locked"])
    ) or bool(raw & 0b1000)
    overload = any(
        bool(lia_status.get(name))
        for name in ("input_or_reserve_overload", "filter_overload")
    ) or bool(raw & 0b0011)
    if unlocked:
        statuses.append("unlocked")
    if overload:
        statuses.append("overload")
    if error_status:
        statuses.append("instrument_error")
    if not statuses:
        statuses.append("clean")
    return tuple(statuses)


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


def _unwrap_degrees(values: Sequence[float]) -> list[float]:
    if not values:
        return []
    unwrapped = [float(values[0])]
    previous_wrapped = float(values[0])
    for value in values[1:]:
        wrapped = float(value)
        delta = (wrapped - previous_wrapped + 180.0) % 360.0 - 180.0
        unwrapped.append(unwrapped[-1] + delta)
        previous_wrapped = wrapped
    return unwrapped


def _finite_float(value: object, label: str) -> float:
    try:
        converted = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be numeric.") from exc
    if not math.isfinite(converted):
        raise ValueError(f"{label} must be finite.")
    return converted


def _integer(value: object, label: str) -> int:
    try:
        converted = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer.") from exc
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{label} must be an integer.")
    return converted


def _temperature_result_key(path: Path) -> tuple[Path, str]:
    name = path.name
    for suffix in (
        "_temperature_excitation_summary.json",
        "_temperature_excitation_formal_samples.csv",
    ):
        if name.endswith(suffix):
            return path.parent.resolve(), name[: -len(suffix)]
    return path.parent.resolve(), name


def _is_temperature_excitation_result(path: Path) -> bool:
    return path.name.endswith(
        (
            "_temperature_excitation_summary.json",
            "_temperature_excitation_formal_samples.csv",
        )
    )
