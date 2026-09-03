from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, fields, replace
import json
import math
from pathlib import Path
from statistics import NormalDist, fmean, stdev
from typing import Callable, Iterable, Mapping, Sequence

from .scientific_plotting import (
    OKABE_ITO_ON_WHITE,
    PUBLICATION_SINGLE_FIGSIZE,
    PUBLICATION_STACKED_FIGSIZE,
    ordered_series_style,
    outside_legend,
    publication_plot,
    save_publication_figure,
    style_axis,
)


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
        "actual_frequency_hz",
        "source_v_rms",
        "nominal_current_a_rms",
        "sine_output_current_a_rms",
    }
)
PLOT_ROLES = ("xx", "xy")
PLOT_HARMONICS = (1, 2, 3)
HARMONIC_SCALING_PLOT_METHODS = ("log", "scalar", "complex")


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
    actual_frequency_hz: float
    source_v_rms: float
    sine_output_v_rms: float
    nominal_current_a_rms: float | None
    recorded_external_series_resistance_ohm: float | None
    recorded_sr830_output_resistance_ohm: float | None
    recorded_approximate_device_resistance_ohm: float | None
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


@dataclass(frozen=True, slots=True)
class MultiFrequencyIVStatistic:
    """One aggregated point for a frequency-by-excitation I--V curve."""

    frequency_hz: float
    current_a_rms: float
    role: str
    harmonic: int
    metric: str
    mean: float
    standard_deviation: float
    count: int


@dataclass(frozen=True, slots=True)
class HarmonicScalingRules:
    """Notebook-editable rules for current-power-law analysis.

    These are analysis choices and thresholds only.  They never affect
    acquisition or hardware safety decisions.  ``None`` disables an optional
    threshold.
    """

    confidence_level: float = 0.95
    minimum_points: int = 6
    minimum_current_decades: float = 1.0
    minimum_snr: float | None = 3.0
    max_exponent_ci_width: float | None = 0.5
    max_delta_aicc_consistent: float | None = 2.0
    min_delta_aicc_inconsistent: float | None = 6.0
    max_relative_rmse: float | None = 0.10
    max_phase_slope_deg_per_decade: float | None = 5.0
    max_phase_span_deg: float | None = 10.0
    scalar_background_mode: str = "auto"
    complex_background_mode: str = "auto"
    complex_free_exponent_min: float = 0.05
    complex_free_exponent_max: float = 6.0

    def __post_init__(self) -> None:
        if not 0.0 < self.confidence_level < 1.0:
            raise ValueError("confidence_level must be between 0 and 1.")
        if self.minimum_points < 3:
            raise ValueError("minimum_points must be at least 3.")
        if not math.isfinite(self.minimum_current_decades) or self.minimum_current_decades < 0.0:
            raise ValueError("minimum_current_decades must be finite and non-negative.")
        if self.scalar_background_mode not in {"auto", "none", "with_offset"}:
            raise ValueError(
                "scalar_background_mode must be 'auto', 'none', or 'with_offset'."
            )
        if self.complex_background_mode not in {"auto", "none", "with_offset"}:
            raise ValueError(
                "complex_background_mode must be 'auto', 'none', or 'with_offset'."
            )
        if (
            not math.isfinite(self.complex_free_exponent_min)
            or not math.isfinite(self.complex_free_exponent_max)
            or self.complex_free_exponent_min <= 0.0
            or self.complex_free_exponent_max <= self.complex_free_exponent_min
        ):
            raise ValueError(
                "complex free-exponent bounds must be finite, positive, and ordered."
            )
        for name in (
            "minimum_snr",
            "max_exponent_ci_width",
            "max_delta_aicc_consistent",
            "min_delta_aicc_inconsistent",
            "max_relative_rmse",
            "max_phase_slope_deg_per_decade",
            "max_phase_span_deg",
        ):
            value = getattr(self, name)
            if value is not None and (not math.isfinite(value) or value <= 0.0):
                raise ValueError(f"{name} must be None or finite and positive.")


@dataclass(frozen=True, slots=True)
class HarmonicScalingPoint:
    """One current point after replicate aggregation."""

    current_a_rms: float
    x_v: float
    x_standard_error_v: float
    y_v: float
    y_standard_error_v: float
    amplitude_v: float
    amplitude_standard_deviation_v: float
    amplitude_standard_error_v: float
    phase_deg: float
    phase_standard_deviation_deg: float
    count: int
    snr: float | None
    included: bool
    exclusion_reason: str | None = None
    complex_included: bool = False
    complex_exclusion_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ComplexHarmonicScalingModel:
    """One complex ``Z=B+C(I/I_ref)^p`` current-scaling model."""

    name: str
    includes_background: bool
    free_exponent: bool
    current_reference_a_rms: float
    exponent: float
    exponent_standard_error: float | None
    exponent_ci_low: float | None
    exponent_ci_high: float | None
    background_x_v: float
    background_y_v: float
    background_amplitude_v: float
    background_phase_deg: float | None
    response_x_v_at_reference_current: float
    response_y_v_at_reference_current: float
    response_amplitude_v_at_reference_current: float
    response_phase_deg: float | None
    r_squared: float | None
    relative_rmse: float | None
    weighted_residual_sum_squares: float | None
    aicc: float | None


@dataclass(frozen=True, slots=True)
class ScalarHarmonicScalingModel:
    """One phase-blind ``R=b+A(I/I_ref)^p`` magnitude model.

    ``R`` is the measured lock-in amplitude.  X/Y and phase are deliberately
    not used by this model; ``phase_ignored`` is archived so that this choice
    remains explicit in exported analysis results.
    """

    name: str
    includes_background: bool
    free_exponent: bool
    phase_ignored: bool
    current_reference_a_rms: float
    exponent: float
    exponent_standard_error: float | None
    exponent_ci_low: float | None
    exponent_ci_high: float | None
    background_v: float
    response_v_at_reference_current: float
    r_squared: float | None
    relative_rmse: float | None
    weighted_residual_sum_squares: float | None
    aicc: float | None


@dataclass(frozen=True, slots=True)
class ScalarHarmonicScalingAssessment:
    """Selection and fixed-vs-free decision for phase-blind magnitude fits."""

    models: tuple[ScalarHarmonicScalingModel, ...]
    selected_fixed_model: ScalarHarmonicScalingModel | None
    selected_free_model: ScalarHarmonicScalingModel | None
    background_verdict: str
    background_delta_aicc: float | None
    exponent_ci_width: float | None
    delta_aicc_fixed_minus_free: float | None
    power_law_verdict: str
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ComplexHarmonicScalingAssessment:
    """Selection and fixed-vs-free decision across complex scaling models."""

    models: tuple[ComplexHarmonicScalingModel, ...]
    selected_fixed_model: ComplexHarmonicScalingModel | None
    selected_free_model: ComplexHarmonicScalingModel | None
    background_verdict: str
    background_delta_aicc: float | None
    exponent_ci_width: float | None
    delta_aicc_fixed_minus_free: float | None
    power_law_verdict: str
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HarmonicScalingFit:
    """Power-law fit and auditable decision for one role/harmonic channel."""

    role: str
    harmonic: int
    expected_order: int
    point_count: int
    fit_point_count: int
    excluded_point_count: int
    current_min_a_rms: float | None
    current_max_a_rms: float | None
    current_decades: float | None
    exponent: float | None
    exponent_standard_error: float | None
    exponent_ci_low: float | None
    exponent_ci_high: float | None
    exponent_ci_width: float | None
    fixed_intercept_log: float | None
    free_intercept_log: float | None
    fixed_r_squared: float | None
    free_r_squared: float | None
    fixed_relative_rmse: float | None
    free_relative_rmse: float | None
    log_leave_one_out_relative_rmse: float | None
    delta_aicc_fixed_minus_free: float | None
    phase_slope_deg_per_decade: float | None
    phase_span_deg: float | None
    amplitude_verdict: str
    complex_response_verdict: str
    complex_fit_point_count: int
    complex_excluded_point_count: int
    complex_background_mode: str
    complex_selected_model: str | None
    complex_selected_free_model: str | None
    complex_background_verdict: str
    complex_background_delta_aicc: float | None
    complex_exponent: float | None
    complex_exponent_standard_error: float | None
    complex_exponent_ci_low: float | None
    complex_exponent_ci_high: float | None
    complex_exponent_ci_width: float | None
    complex_delta_aicc_fixed_minus_free: float | None
    complex_fixed_relative_rmse: float | None
    complex_leave_one_out_relative_rmse: float | None
    complex_power_law_verdict: str
    complex_reasons: tuple[str, ...]
    complex_models: tuple[ComplexHarmonicScalingModel, ...]
    scalar_fit_point_count: int
    scalar_excluded_point_count: int
    scalar_background_mode: str
    scalar_selected_model: str | None
    scalar_selected_free_model: str | None
    scalar_background_verdict: str
    scalar_background_delta_aicc: float | None
    scalar_exponent: float | None
    scalar_exponent_standard_error: float | None
    scalar_exponent_ci_low: float | None
    scalar_exponent_ci_high: float | None
    scalar_exponent_ci_width: float | None
    scalar_delta_aicc_fixed_minus_free: float | None
    scalar_fixed_relative_rmse: float | None
    scalar_leave_one_out_relative_rmse: float | None
    scalar_power_law_verdict: str
    scalar_phase_ignored: bool
    scalar_reasons: tuple[str, ...]
    scalar_models: tuple[ScalarHarmonicScalingModel, ...]
    reasons: tuple[str, ...]
    points: tuple[HarmonicScalingPoint, ...]

    def as_dict(self) -> dict[str, object]:
        values = asdict(self)
        values.pop("points", None)
        values["reasons"] = list(self.reasons)
        values["complex_reasons"] = list(self.complex_reasons)
        values["scalar_reasons"] = list(self.scalar_reasons)
        return values


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


def excitation_path_from_sweep_files(
    paths: Iterable[str | Path],
    *,
    excitation_path_override: ExcitationPathResistance | None = None,
) -> ExcitationPathResistance:
    """Resolve one analysis path from recorded sweep metadata or an override.

    New sweep JSON files archive the complete path in
    ``measurement_config.excitation_path``. All selected files must agree so a
    plotted condition cannot silently combine incompatible current calibrations.
    An explicit override is reserved for legacy records that lack this metadata.
    """

    selected_paths = tuple(Path(path) for path in paths)
    if not selected_paths:
        raise ValueError("At least one sweep file is required for current calibration.")
    if excitation_path_override is not None:
        return excitation_path_override
    recorded_paths: set[ExcitationPathResistance] = set()
    for path in selected_paths:
        payload = load_commissioning_file(path)
        if not isinstance(payload, dict) or payload.get("scan") not in {
            "frequency",
            "excitation",
            "frequency_excitation",
        }:
            raise ValueError(f"{path} is not a supported lock-in sweep.")
        recorded = _recorded_excitation_path(payload)
        if recorded is None:
            raise ValueError(
                f"{path.name} has no recorded measurement_config.excitation_path; "
                "provide an explicit ExcitationPathResistance override only for "
                "legacy data."
            )
        recorded_paths.add(recorded)
    if len(recorded_paths) != 1:
        raise ValueError(
            "Selected sweep files record different excitation paths; analyze them "
            "separately or provide one explicit override."
        )
    return next(iter(recorded_paths))


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
        "frequency_excitation",
    }:
        raise ValueError("Selected file is not a supported lock-in sweep.")
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
    recorded_excitation_path = _recorded_excitation_path(payload)
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
            for role in _formal_selected_roles(sample):
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
                    recorded_excitation_path=recorded_excitation_path,
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
    """Load selected sweep files without combining incompatible scan types."""

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
        raise ValueError("Load frequency, excitation, and combined files separately.")
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
            "actual_frequency_hz"
            if next(iter(scan_types)) == "frequency"
            else "source_v_rms"
        )
    if x_axis not in SWEEP_X_AXES:
        raise ValueError(f"Unsupported x axis: {x_axis}")
    resolved_excitation_path = (
        _single_excitation_path_for_rows(rows, excitation_path)
        if x_axis == "sine_output_current_a_rms"
        else excitation_path
    )
    grouped: dict[tuple[float, str], list[float]] = {}
    for row in rows:
        raw_x = _sweep_x_value(row, x_axis, resolved_excitation_path)
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


def aggregate_frequency_excitation_iv(
    rows: Sequence[CommissioningSample],
    *,
    role: str,
    harmonic: int,
    metric: str = "amplitude_v",
    excitation_path: ExcitationPathResistance | None = None,
) -> tuple[MultiFrequencyIVStatistic, ...]:
    """Aggregate a combined scan into one I--V curve per actual frequency.

    The current is always calculated from each recorded SINE OUT readback and
    the archived excitation-path resistance.  Frequency is taken from the
    per-point SR830 readback, not the requested grid value.  Small readback
    jitter is clustered using a relative tolerance so one physical frequency
    produces one curve.
    """

    if role not in PLOT_ROLES:
        raise ValueError(f"Unknown role: {role}")
    if harmonic not in PLOT_HARMONICS:
        raise ValueError(f"Unsupported harmonic: {harmonic}")
    if metric not in SWEEP_METRICS:
        raise ValueError(f"Unsupported metric: {metric}")
    if not rows:
        raise ValueError("No sweep samples match the selected filters.")
    if {row.scan_type for row in rows} != {"frequency_excitation"}:
        raise ValueError("Multi-frequency I--V analysis requires a combined sweep.")
    selected = tuple(
        row for row in rows if row.role == role and row.harmonic == harmonic
    )
    if not selected:
        return ()
    resolved_path = _single_excitation_path_for_rows(selected, excitation_path)
    # First assign rows to frequency bins.  The readback is retained as the
    # representative value; only sub-readback-resolution jitter is clustered.
    frequency_bins: list[tuple[float, list[CommissioningSample]]] = []
    for row in sorted(selected, key=lambda item: (item.actual_frequency_hz, item.point_index)):
        for representative, bin_rows in frequency_bins:
            if math.isclose(
                row.actual_frequency_hz,
                representative,
                rel_tol=2e-6,
                abs_tol=1e-6,
            ):
                bin_rows.append(row)
                break
        else:
            frequency_bins.append((row.actual_frequency_hz, [row]))
    grouped: dict[tuple[float, float], list[float]] = {}
    for frequency_hz, bin_rows in frequency_bins:
        for row in bin_rows:
            current = resolved_path.current_from_sine_output(row.sine_output_v_rms)
            grouped.setdefault((frequency_hz, current), []).append(
                float(getattr(row, metric))
            )
    statistics: list[MultiFrequencyIVStatistic] = []
    for (frequency_hz, current), values in sorted(grouped.items()):
        mean, spread = _mean_and_standard_deviation(values, metric=metric)
        statistics.append(
            MultiFrequencyIVStatistic(
                frequency_hz=frequency_hz,
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


def fit_harmonic_scaling(
    rows: Sequence[CommissioningSample],
    *,
    role: str,
    harmonic: int,
    excitation_path: ExcitationPathResistance | None = None,
    rules: HarmonicScalingRules | None = None,
) -> HarmonicScalingFit:
    """Fit one excitation-sweep channel against SINE OUT-derived RMS current.

    The primary fit is performed in log-log space.  It compares a free
    exponent ``p`` with the physically expected fixed exponent ``harmonic``.
    The returned amplitude and complex-response verdicts are intentionally
    separate: a magnitude can follow a power law while its phase rotates.
    """

    resolved_rules = rules or HarmonicScalingRules()
    if role not in PLOT_ROLES:
        raise ValueError(f"Unknown role: {role}")
    if harmonic not in PLOT_HARMONICS:
        raise ValueError(f"Unsupported harmonic: {harmonic}")
    if not rows:
        raise ValueError("No sweep samples match the selected filters.")
    scan_types = {row.scan_type for row in rows}
    if scan_types != {"excitation"}:
        raise ValueError("Harmonic scaling requires excitation sweep samples.")
    selected = tuple(
        row for row in rows if row.role == role and row.harmonic == harmonic
    )
    resolved_path = _single_excitation_path_for_rows(selected or rows, excitation_path)
    points = _harmonic_scaling_points(selected, resolved_path, resolved_rules)
    included = tuple(point for point in points if point.included)
    complex_included = tuple(point for point in points if point.complex_included)
    scalar_included = included
    current_values = tuple(point.current_a_rms for point in included)
    current_min = min(current_values) if current_values else None
    current_max = max(current_values) if current_values else None
    current_decades = (
        math.log10(current_max / current_min)
        if current_min is not None and current_min > 0.0 and current_max is not None
        else None
    )
    reasons: list[str] = []
    if not selected:
        reasons.append(f"no selected {role} h{harmonic} samples")
    if not included:
        reasons.append("no positive, sufficiently stable amplitude points remain")
    if len(included) < resolved_rules.minimum_points:
        reasons.append(
            f"requires at least {resolved_rules.minimum_points} fit points"
        )
    if (
        current_decades is not None
        and current_decades < resolved_rules.minimum_current_decades
    ):
        reasons.append(
            f"current span is {current_decades:.3g} decades; "
            f"requires {resolved_rules.minimum_current_decades:.3g}"
        )

    fixed_intercept = free_intercept = None
    exponent = exponent_se = exponent_low = exponent_high = None
    exponent_width = None
    fixed_r_squared = free_r_squared = None
    fixed_rmse = free_rmse = None
    delta_aicc = None
    phase_slope = phase_span = None
    if len(included) >= 3 and current_decades is not None and current_decades > 0.0:
        log_current = tuple(math.log(point.current_a_rms) for point in included)
        log_amplitude = tuple(math.log(point.amplitude_v) for point in included)
        log_sigma = tuple(
            (
                point.amplitude_standard_error_v / point.amplitude_v
                if point.amplitude_standard_error_v > 0.0
                else None
            )
            for point in included
        )
        fixed = _log_power_fit(
            log_current,
            log_amplitude,
            tuple(point.amplitude_v for point in included),
            fixed_slope=float(harmonic),
            sigma_y=log_sigma,
        )
        free = _log_power_fit(
            log_current,
            log_amplitude,
            tuple(point.amplitude_v for point in included),
            fixed_slope=None,
            sigma_y=log_sigma,
        )
        fixed_intercept = fixed["intercept"]
        free_intercept = free["intercept"]
        fixed_r_squared = fixed["r_squared"]
        free_r_squared = free["r_squared"]
        fixed_rmse = fixed["relative_rmse"]
        free_rmse = free["relative_rmse"]
        delta_aicc = fixed["aicc"] - free["aicc"]
        exponent = free["slope"]
        exponent_se = free["slope_standard_error"]
        if exponent is not None and exponent_se is not None:
            z = NormalDist().inv_cdf((1.0 + resolved_rules.confidence_level) / 2.0)
            exponent_low = exponent - z * exponent_se
            exponent_high = exponent + z * exponent_se
            exponent_width = exponent_high - exponent_low
        phase_slope, phase_span = _phase_scaling_metrics(included)

    complex_assessment = _assess_complex_harmonic_scaling(
        complex_included,
        expected_order=harmonic,
        rules=resolved_rules,
    )
    scalar_assessment = _assess_scalar_harmonic_scaling(
        scalar_included,
        expected_order=harmonic,
        rules=resolved_rules,
    )
    log_leave_one_out_rmse = _leave_one_out_log_relative_rmse(
        included,
        expected_order=harmonic,
    )
    scalar_leave_one_out_rmse = _leave_one_out_scalar_relative_rmse(
        scalar_included,
        expected_order=harmonic,
        selected_model=scalar_assessment.selected_fixed_model,
    )
    complex_leave_one_out_rmse = _leave_one_out_complex_relative_rmse(
        complex_included,
        expected_order=harmonic,
        selected_model=complex_assessment.selected_fixed_model,
    )

    if len(included) >= resolved_rules.minimum_points and (
        current_decades is not None
        and current_decades >= resolved_rules.minimum_current_decades
    ):
        amplitude_verdict, verdict_reasons = _amplitude_scaling_verdict(
            expected_order=harmonic,
            exponent=exponent,
            exponent_low=exponent_low,
            exponent_high=exponent_high,
            exponent_width=exponent_width,
            fixed_relative_rmse=fixed_rmse,
            delta_aicc=delta_aicc,
            rules=resolved_rules,
        )
        reasons.extend(verdict_reasons)
    else:
        amplitude_verdict = "insufficient_data"

    if amplitude_verdict != "consistent":
        complex_verdict = amplitude_verdict
    else:
        phase_ok = True
        if (
            resolved_rules.max_phase_slope_deg_per_decade is not None
            and phase_slope is not None
            and abs(phase_slope) > resolved_rules.max_phase_slope_deg_per_decade
        ):
            phase_ok = False
            reasons.append(
                f"phase slope {phase_slope:.3g} deg/decade exceeds "
                f"{resolved_rules.max_phase_slope_deg_per_decade:.3g}"
            )
        if (
            resolved_rules.max_phase_span_deg is not None
            and phase_span is not None
            and phase_span > resolved_rules.max_phase_span_deg
        ):
            phase_ok = False
            reasons.append(
                f"phase span {phase_span:.3g} deg exceeds "
                f"{resolved_rules.max_phase_span_deg:.3g}"
            )
        if phase_slope is None or phase_span is None:
            complex_verdict = "ambiguous"
            reasons.append("phase stability could not be estimated")
        else:
            complex_verdict = "consistent" if phase_ok else "inconsistent"

    return HarmonicScalingFit(
        role=role,
        harmonic=harmonic,
        expected_order=harmonic,
        point_count=len(points),
        fit_point_count=len(included),
        excluded_point_count=len(points) - len(included),
        current_min_a_rms=current_min,
        current_max_a_rms=current_max,
        current_decades=current_decades,
        exponent=exponent,
        exponent_standard_error=exponent_se,
        exponent_ci_low=exponent_low,
        exponent_ci_high=exponent_high,
        exponent_ci_width=exponent_width,
        fixed_intercept_log=fixed_intercept,
        free_intercept_log=free_intercept,
        fixed_r_squared=fixed_r_squared,
        free_r_squared=free_r_squared,
        fixed_relative_rmse=fixed_rmse,
        free_relative_rmse=free_rmse,
        log_leave_one_out_relative_rmse=log_leave_one_out_rmse,
        delta_aicc_fixed_minus_free=delta_aicc,
        phase_slope_deg_per_decade=phase_slope,
        phase_span_deg=phase_span,
        amplitude_verdict=amplitude_verdict,
        complex_response_verdict=complex_verdict,
        complex_fit_point_count=len(complex_included),
        complex_excluded_point_count=len(points) - len(complex_included),
        complex_background_mode=resolved_rules.complex_background_mode,
        complex_selected_model=(
            complex_assessment.selected_fixed_model.name
            if complex_assessment.selected_fixed_model is not None
            else None
        ),
        complex_selected_free_model=(
            complex_assessment.selected_free_model.name
            if complex_assessment.selected_free_model is not None
            else None
        ),
        complex_background_verdict=complex_assessment.background_verdict,
        complex_background_delta_aicc=complex_assessment.background_delta_aicc,
        complex_exponent=(
            complex_assessment.selected_free_model.exponent
            if complex_assessment.selected_free_model is not None
            else None
        ),
        complex_exponent_standard_error=(
            complex_assessment.selected_free_model.exponent_standard_error
            if complex_assessment.selected_free_model is not None
            else None
        ),
        complex_exponent_ci_low=(
            complex_assessment.selected_free_model.exponent_ci_low
            if complex_assessment.selected_free_model is not None
            else None
        ),
        complex_exponent_ci_high=(
            complex_assessment.selected_free_model.exponent_ci_high
            if complex_assessment.selected_free_model is not None
            else None
        ),
        complex_exponent_ci_width=complex_assessment.exponent_ci_width,
        complex_delta_aicc_fixed_minus_free=(
            complex_assessment.delta_aicc_fixed_minus_free
        ),
        complex_fixed_relative_rmse=(
            complex_assessment.selected_fixed_model.relative_rmse
            if complex_assessment.selected_fixed_model is not None
            else None
        ),
        complex_leave_one_out_relative_rmse=complex_leave_one_out_rmse,
        complex_power_law_verdict=complex_assessment.power_law_verdict,
        complex_reasons=complex_assessment.reasons,
        complex_models=complex_assessment.models,
        scalar_fit_point_count=len(scalar_included),
        scalar_excluded_point_count=len(points) - len(scalar_included),
        scalar_background_mode=resolved_rules.scalar_background_mode,
        scalar_selected_model=(
            scalar_assessment.selected_fixed_model.name
            if scalar_assessment.selected_fixed_model is not None
            else None
        ),
        scalar_selected_free_model=(
            scalar_assessment.selected_free_model.name
            if scalar_assessment.selected_free_model is not None
            else None
        ),
        scalar_background_verdict=scalar_assessment.background_verdict,
        scalar_background_delta_aicc=scalar_assessment.background_delta_aicc,
        scalar_exponent=(
            scalar_assessment.selected_free_model.exponent
            if scalar_assessment.selected_free_model is not None
            else None
        ),
        scalar_exponent_standard_error=(
            scalar_assessment.selected_free_model.exponent_standard_error
            if scalar_assessment.selected_free_model is not None
            else None
        ),
        scalar_exponent_ci_low=(
            scalar_assessment.selected_free_model.exponent_ci_low
            if scalar_assessment.selected_free_model is not None
            else None
        ),
        scalar_exponent_ci_high=(
            scalar_assessment.selected_free_model.exponent_ci_high
            if scalar_assessment.selected_free_model is not None
            else None
        ),
        scalar_exponent_ci_width=scalar_assessment.exponent_ci_width,
        scalar_delta_aicc_fixed_minus_free=(
            scalar_assessment.delta_aicc_fixed_minus_free
        ),
        scalar_fixed_relative_rmse=(
            scalar_assessment.selected_fixed_model.relative_rmse
            if scalar_assessment.selected_fixed_model is not None
            else None
        ),
        scalar_leave_one_out_relative_rmse=scalar_leave_one_out_rmse,
        scalar_power_law_verdict=scalar_assessment.power_law_verdict,
        scalar_phase_ignored=True,
        scalar_reasons=scalar_assessment.reasons,
        scalar_models=scalar_assessment.models,
        reasons=tuple(dict.fromkeys(reasons)),
        points=points,
    )


def fit_harmonic_scalings(
    rows: Sequence[CommissioningSample],
    *,
    excitation_path: ExcitationPathResistance | None = None,
    rules: HarmonicScalingRules | None = None,
    roles: Iterable[str] = PLOT_ROLES,
    harmonics: Iterable[int] = PLOT_HARMONICS,
) -> dict[tuple[str, int], HarmonicScalingFit]:
    """Fit every available role/harmonic combination independently."""

    selected_roles = tuple(roles)
    selected_harmonics = tuple(harmonics)
    return {
        (role, harmonic): fit_harmonic_scaling(
            rows,
            role=role,
            harmonic=harmonic,
            excitation_path=excitation_path,
            rules=rules,
        )
        for role in selected_roles
        for harmonic in selected_harmonics
        if any(row.role == role and row.harmonic == harmonic for row in rows)
    }


@publication_plot
def plot_harmonic_scaling_fit(
    fit: HarmonicScalingFit,
    *,
    methods: Iterable[str] = HARMONIC_SCALING_PLOT_METHODS,
    destination: str | Path | None = None,
):
    """Plot selected scaling views without changing the underlying fit results."""

    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError(
            "Plotting requires: python -m pip install -e '.[analysis]'"
        ) from exc
    selected_methods = _validate_harmonic_scaling_plot_methods(methods)
    figure, (axis, residual_axis) = plt.subplots(
        2, 1, figsize=PUBLICATION_STACKED_FIGSIZE, sharex=True,
        gridspec_kw={"height_ratios": (3, 1)},
        constrained_layout=True,
    )
    log_color = OKABE_ITO_ON_WHITE[0]
    scalar_color = OKABE_ITO_ON_WHITE[1]
    complex_color = OKABE_ITO_ON_WHITE[2]
    included = tuple(point for point in fit.points if point.included)
    excluded = tuple(point for point in fit.points if not point.included)
    selected_complex_model = next(
        (
            model
            for model in fit.complex_models
            if model.name == fit.complex_selected_model
        ),
        None,
    )
    selected_scalar_model = next(
        (
            model
            for model in fit.scalar_models
            if model.name == fit.scalar_selected_model
        ),
        None,
    )
    selected_complex_free_model = next(
        (
            model
            for model in fit.complex_models
            if model.name == fit.complex_selected_free_model
        ),
        None,
    )
    selected_scalar_free_model = next(
        (
            model
            for model in fit.scalar_models
            if model.name == fit.scalar_selected_free_model
        ),
        None,
    )
    residual_values: list[tuple[float, float, str, str]] = []
    if included:
        x_values = [point.current_a_rms for point in included]
        y_values = [point.amplitude_v for point in included]
        axis.errorbar(
            x_values,
            y_values,
            yerr=[point.amplitude_standard_deviation_v for point in included],
            color=OKABE_ITO_ON_WHITE[4],
            marker="o",
            linestyle="none",
            capsize=2.5,
            elinewidth=0.8,
            label="observed mean ± sample SD",
        )
        if "log" in selected_methods and fit.fixed_intercept_log is not None:
            axis.plot(
                x_values,
                [
                    math.exp(fit.fixed_intercept_log) * value ** fit.expected_order
                    for value in x_values
                ],
                color=log_color,
                linestyle="--",
                label=_format_log_fit_legend_label(
                    fit,
                    intercept_log=fit.fixed_intercept_log,
                    exponent=float(fit.expected_order),
                    free_exponent=False,
                ),
            )
        if (
            "log" in selected_methods
            and fit.free_intercept_log is not None
            and fit.exponent is not None
        ):
            axis.plot(
                x_values,
                [
                    math.exp(fit.free_intercept_log) * value ** fit.exponent
                    for value in x_values
                ],
                color=log_color,
                linestyle="-",
                label=_format_log_fit_legend_label(
                    fit,
                    intercept_log=fit.free_intercept_log,
                    exponent=fit.exponent,
                    free_exponent=True,
                ),
            )
        if "scalar" in selected_methods and selected_scalar_model is not None:
            scalar_predictions = [
                _scalar_model_prediction(selected_scalar_model, point.current_a_rms)
                for point in included
            ]
            axis.plot(
                x_values,
                scalar_predictions,
                color=scalar_color,
                linewidth=1.6,
                label=_format_scalar_fit_legend_label(selected_scalar_model),
            )
            residual_values.extend(
                (
                    point.current_a_rms,
                    (point.amplitude_v - predicted)
                    / max(abs(predicted), 1e-30),
                    scalar_color,
                    "scalar R residual",
                )
                for point, predicted in zip(included, scalar_predictions)
            )
        if "scalar" in selected_methods and selected_scalar_free_model is not None:
            scalar_free_predictions = [
                _scalar_model_prediction(
                    selected_scalar_free_model, point.current_a_rms
                )
                for point in included
            ]
            axis.plot(
                x_values,
                scalar_free_predictions,
                color=scalar_color,
                linewidth=1.3,
                linestyle="--",
                label=_format_scalar_fit_legend_label(selected_scalar_free_model),
            )
        if "complex" in selected_methods and selected_complex_model is not None:
            complex_predictions = [
                _complex_model_prediction(selected_complex_model, point.current_a_rms)
                for point in included
            ]
            axis.plot(
                x_values,
                [math.hypot(predicted_x, predicted_y) for predicted_x, predicted_y in complex_predictions],
                color=complex_color,
                linewidth=1.6,
                label=_format_complex_fit_legend_label(selected_complex_model),
            )
            residual_values.extend(
                (
                    point.current_a_rms,
                    math.hypot(point.x_v - predicted_x, point.y_v - predicted_y)
                    / max(math.hypot(predicted_x, predicted_y), 1e-30),
                    complex_color,
                    "complex residual",
                )
                for point, (predicted_x, predicted_y) in zip(
                    included, complex_predictions
                )
            )
        if "complex" in selected_methods and selected_complex_free_model is not None:
            complex_free_predictions = [
                _complex_model_prediction(
                    selected_complex_free_model, point.current_a_rms
                )
                for point in included
            ]
            axis.plot(
                x_values,
                [
                    math.hypot(predicted_x, predicted_y)
                    for predicted_x, predicted_y in complex_free_predictions
                ],
                color=complex_color,
                linewidth=1.3,
                linestyle="--",
                label=_format_complex_fit_legend_label(selected_complex_free_model),
            )
        if "log" in selected_methods and fit.fixed_intercept_log is not None:
            for point in included:
                predicted = (
                    math.exp(fit.fixed_intercept_log)
                    * point.current_a_rms ** fit.expected_order
                )
                residual_values.append(
                    (
                        point.current_a_rms,
                        (point.amplitude_v - predicted) / max(abs(predicted), 1e-30),
                        log_color,
                        "log fixed residual",
                    )
                )
        if residual_values:
            residual_axis.axhline(0.0, color="0.25", linewidth=0.8)
            seen_labels: set[str] = set()
            residual_markers = {
                log_color: "s",
                scalar_color: "^",
                complex_color: "D",
            }
            for color in (log_color, scalar_color, complex_color):
                items = [item for item in residual_values if item[2] == color]
                if not items:
                    continue
                label = items[0][3]
                residual_axis.scatter(
                    [item[0] for item in items],
                    [item[1] for item in items],
                    color=color,
                    marker=residual_markers[color],
                    label=label if label not in seen_labels else None,
                )
                seen_labels.add(label)
    if excluded:
        axis.scatter(
            [point.current_a_rms for point in excluded],
            [point.amplitude_v for point in excluded],
            marker="x",
            color="#767676",
            label="excluded",
        )
    axis.set_xscale("log")
    axis.set_yscale("log")
    residual_axis.set_xscale("log")
    residual_axis.set_xlabel("SINE OUT current (A RMS)")
    axis.set_ylabel(f"V{fit.role} h{fit.harmonic} R (V RMS)")
    residual_axis.set_ylabel("Relative residual")
    axis.set_title(f"V{fit.role} h{fit.harmonic} harmonic-scaling fits")
    style_axis(axis)
    style_axis(residual_axis)
    outside_legend(
        axis,
        title=_format_harmonic_scaling_legend_title(fit, selected_methods),
        fontsize=7.0,
        title_fontsize=7.5,
    )
    if residual_values:
        outside_legend(residual_axis, title="Fit residuals")
    if destination is not None:
        save_publication_figure(figure, destination)
    return figure


def _scalar_model_prediction(
    model: ScalarHarmonicScalingModel,
    current_a_rms: float,
) -> float:
    scale = (current_a_rms / model.current_reference_a_rms) ** model.exponent
    return model.background_v + model.response_v_at_reference_current * scale


def _complex_model_prediction(
    model: ComplexHarmonicScalingModel,
    current_a_rms: float,
) -> tuple[float, float]:
    scale = (current_a_rms / model.current_reference_a_rms) ** model.exponent
    return (
        model.background_x_v + model.response_x_v_at_reference_current * scale,
        model.background_y_v + model.response_y_v_at_reference_current * scale,
    )


def _validate_harmonic_scaling_plot_methods(
    methods: Iterable[str],
) -> tuple[str, ...]:
    """Return a non-empty, unique sequence of supported display methods."""

    selected = tuple(methods)
    if not selected:
        raise ValueError("At least one harmonic-scaling plot method is required.")
    unsupported = tuple(
        method for method in selected if method not in HARMONIC_SCALING_PLOT_METHODS
    )
    if unsupported:
        allowed = ", ".join(HARMONIC_SCALING_PLOT_METHODS)
        raise ValueError(
            f"Unsupported harmonic-scaling plot method(s): {unsupported!r}; "
            f"choose from {allowed}."
        )
    if len(set(selected)) != len(selected):
        raise ValueError("Harmonic-scaling plot methods must not contain duplicates.")
    return selected


def _format_harmonic_scaling_legend_title(
    fit: HarmonicScalingFit,
    methods: Sequence[str],
) -> str:
    """Summarize the auditable fit decisions above the equation legend."""

    verdicts = []
    if "log" in methods:
        verdicts.append(f"log: {fit.amplitude_verdict}")
    if "scalar" in methods:
        verdicts.append(f"scalar: {fit.scalar_power_law_verdict}")
    if "complex" in methods:
        verdicts.append(
            "phase/complex: "
            f"{fit.complex_response_verdict}/{fit.complex_power_law_verdict}"
        )
    return "\n".join(("Data and fitted equations", *verdicts))


def _format_log_fit_legend_label(
    fit: HarmonicScalingFit,
    *,
    intercept_log: float,
    exponent: float,
    free_exponent: bool,
) -> str:
    """Return an equivalent, current-normalized log-fit equation for a legend."""

    current_reference = _log_fit_current_reference(fit)
    heading = "Log free" if free_exponent else "Log fixed"
    exponent_text = _format_free_exponent(
        exponent,
        fit.exponent_ci_low if free_exponent else None,
        fit.exponent_ci_high if free_exponent else None,
        symbol="p" if free_exponent else "n",
    )
    if current_reference is None:
        equation = "R(I) unavailable"
    else:
        response_at_reference = math.exp(intercept_log) * current_reference**exponent
        equation = _format_scalar_power_law_equation(
            background_v=0.0,
            response_v=response_at_reference,
            current_reference_a_rms=current_reference,
            exponent=exponent,
            includes_background=False,
            response_symbol="R",
        )
    metrics = _format_fit_metrics(
        r_squared=fit.free_r_squared if free_exponent else fit.fixed_r_squared,
        relative_rmse=(
            fit.free_relative_rmse if free_exponent else fit.fixed_relative_rmse
        ),
        aicc_delta=(fit.delta_aicc_fixed_minus_free if free_exponent else None),
    )
    return "\n".join((f"{heading}, {exponent_text}", equation, metrics))


def _format_scalar_fit_legend_label(model: ScalarHarmonicScalingModel) -> str:
    """Format an amplitude-only model and its fitted numerical equation."""

    heading = "Scalar free" if model.free_exponent else "Scalar fixed"
    exponent_text = _format_free_exponent(
        model.exponent,
        model.exponent_ci_low if model.free_exponent else None,
        model.exponent_ci_high if model.free_exponent else None,
        symbol="p" if model.free_exponent else "n",
    )
    equation = _format_scalar_power_law_equation(
        background_v=model.background_v,
        response_v=model.response_v_at_reference_current,
        current_reference_a_rms=model.current_reference_a_rms,
        exponent=model.exponent,
        includes_background=model.includes_background,
        response_symbol="R",
    )
    metrics = _format_fit_metrics(
        r_squared=model.r_squared,
        relative_rmse=model.relative_rmse,
        aicc=model.aicc,
    )
    return "\n".join((f"{heading}, {exponent_text}", equation, metrics))


def _format_complex_fit_legend_label(model: ComplexHarmonicScalingModel) -> str:
    """Format a complex X+iY model without discarding phase-bearing terms."""

    heading = "Complex free" if model.free_exponent else "Complex fixed"
    exponent_text = _format_free_exponent(
        model.exponent,
        model.exponent_ci_low if model.free_exponent else None,
        model.exponent_ci_high if model.free_exponent else None,
        symbol="p" if model.free_exponent else "n",
    )
    response = _format_complex_voltage(
        model.response_x_v_at_reference_current,
        model.response_y_v_at_reference_current,
    )
    current = _format_engineering(model.current_reference_a_rms, "A")
    scale = f"(I / {current})^{model.exponent:.3g}"
    if model.includes_background:
        background = _format_complex_voltage(
            model.background_x_v,
            model.background_y_v,
        )
        equation = f"Z(I) = {background} + {response} {scale}"
    else:
        equation = f"Z(I) = {response} {scale}"
    metrics = _format_fit_metrics(
        r_squared=model.r_squared,
        relative_rmse=model.relative_rmse,
        aicc=model.aicc,
    )
    return "\n".join((f"{heading}, {exponent_text}", equation, metrics))


def _log_fit_current_reference(fit: HarmonicScalingFit) -> float | None:
    if (
        fit.current_min_a_rms is None
        or fit.current_max_a_rms is None
        or fit.current_min_a_rms <= 0.0
        or fit.current_max_a_rms <= 0.0
    ):
        return None
    return math.sqrt(fit.current_min_a_rms * fit.current_max_a_rms)


def _format_scalar_power_law_equation(
    *,
    background_v: float,
    response_v: float,
    current_reference_a_rms: float,
    exponent: float,
    includes_background: bool,
    response_symbol: str,
) -> str:
    response = _format_engineering(response_v, "V")
    current = _format_engineering(current_reference_a_rms, "A")
    scaled_current = f"(I / {current})^{exponent:.3g}"
    if includes_background:
        return (
            f"{response_symbol}(I) = {_format_engineering(background_v, 'V')} "
            f"+ {response} {scaled_current}"
        )
    return f"{response_symbol}(I) = {response} {scaled_current}"


def _format_complex_voltage(x_v: float, y_v: float) -> str:
    sign = "+" if y_v >= 0.0 else "−"
    return (
        f"({_format_engineering(x_v, 'V')} {sign} i "
        f"{_format_engineering(abs(y_v), 'V')})"
    )


def _format_free_exponent(
    exponent: float,
    ci_low: float | None,
    ci_high: float | None,
    *,
    symbol: str,
) -> str:
    if ci_low is None or ci_high is None:
        return f"{symbol}={exponent:.3g}"
    return f"{symbol}={exponent:.3g} [{ci_low:.3g}, {ci_high:.3g}]"


def _format_fit_metrics(
    *,
    r_squared: float | None,
    relative_rmse: float | None,
    aicc: float | None = None,
    aicc_delta: float | None = None,
) -> str:
    values: list[str] = []
    if r_squared is not None:
        values.append(f"R²={r_squared:.3g}")
    if relative_rmse is not None:
        values.append(f"rRMSE={relative_rmse * 100.0:.3g}%")
    if aicc is not None:
        values.append(f"AICc={aicc:.3g}")
    if aicc_delta is not None:
        values.append(f"ΔAICc(fixed−free)={aicc_delta:.3g}")
    return "; ".join(values) or "fit metrics unavailable"


def _harmonic_scaling_points(
    rows: Sequence[CommissioningSample],
    excitation_path: ExcitationPathResistance,
    rules: HarmonicScalingRules,
) -> tuple[HarmonicScalingPoint, ...]:
    grouped: dict[float, list[CommissioningSample]] = {}
    for row in rows:
        current = _sweep_x_value(row, "sine_output_current_a_rms", excitation_path)
        grouped.setdefault(float(current), []).append(row)
    points: list[HarmonicScalingPoint] = []
    for current, grouped_rows in sorted(grouped.items()):
        x_values = [float(row.x_v) for row in grouped_rows]
        y_values = [float(row.y_v) for row in grouped_rows]
        amplitudes = [float(row.amplitude_v) for row in grouped_rows]
        phases = [float(row.phase_deg) for row in grouped_rows]
        x_mean = fmean(x_values)
        y_mean = fmean(y_values)
        x_sem = (
            stdev(x_values) / math.sqrt(len(x_values))
            if len(x_values) > 1
            else 0.0
        )
        y_sem = (
            stdev(y_values) / math.sqrt(len(y_values))
            if len(y_values) > 1
            else 0.0
        )
        amplitude = fmean(amplitudes)
        amplitude_sd = stdev(amplitudes) if len(amplitudes) > 1 else 0.0
        amplitude_sem = amplitude_sd / math.sqrt(len(amplitudes)) if len(amplitudes) > 1 else 0.0
        snr = amplitude / amplitude_sem if amplitude_sem > 0.0 else None
        phase, phase_sd = _mean_and_standard_deviation(phases, metric="phase_deg")
        included = True
        exclusion_reason = None
        if not math.isfinite(current) or current <= 0.0:
            included = False
            exclusion_reason = "current is not positive and finite"
        elif not math.isfinite(amplitude) or amplitude <= 0.0:
            included = False
            exclusion_reason = "amplitude is not positive and finite"
        elif (
            rules.minimum_snr is not None
            and snr is not None
            and snr < rules.minimum_snr
        ):
            included = False
            exclusion_reason = f"SNR {snr:.3g} below {rules.minimum_snr:.3g}"
        complex_included = True
        complex_exclusion_reason = None
        if not math.isfinite(current) or current <= 0.0:
            complex_included = False
            complex_exclusion_reason = "current is not positive and finite"
        elif not math.isfinite(x_mean) or not math.isfinite(y_mean):
            complex_included = False
            complex_exclusion_reason = "X/Y response is not finite"
        points.append(
            HarmonicScalingPoint(
                current_a_rms=current,
                x_v=x_mean,
                x_standard_error_v=x_sem,
                y_v=y_mean,
                y_standard_error_v=y_sem,
                amplitude_v=amplitude,
                amplitude_standard_deviation_v=amplitude_sd,
                amplitude_standard_error_v=amplitude_sem,
                phase_deg=phase,
                phase_standard_deviation_deg=phase_sd,
                count=len(grouped_rows),
                snr=snr,
                included=included,
                exclusion_reason=exclusion_reason,
                complex_included=complex_included,
                complex_exclusion_reason=complex_exclusion_reason,
            )
        )
    return tuple(points)


def _log_power_fit(
    x: Sequence[float],
    y: Sequence[float],
    original_y: Sequence[float],
    *,
    fixed_slope: float | None,
    sigma_y: Sequence[float | None],
) -> dict[str, float | None]:
    weights = tuple(
        1.0 / sigma**2
        if sigma is not None and math.isfinite(sigma) and sigma > 0.0
        else 1.0
        for sigma in sigma_y
    )
    sw = sum(weights)
    if fixed_slope is None:
        sx = sum(weight * value for weight, value in zip(weights, x))
        sy = sum(weight * value for weight, value in zip(weights, y))
        sxx = sum(weight * value * value for weight, value in zip(weights, x))
        sxy = sum(weight * a * b for weight, a, b in zip(weights, x, y))
        denominator = sw * sxx - sx * sx
        if denominator <= 0.0:
            raise ValueError("Log-current values must vary for a scaling fit.")
        slope = (sw * sxy - sx * sy) / denominator
        intercept = (sy - slope * sx) / sw
        parameter_count = 2
        slope_variance_factor = sw / denominator
    else:
        slope = fixed_slope
        intercept = sum(weight * (value - slope * current) for weight, value, current in zip(weights, y, x)) / sw
        parameter_count = 1
        slope_variance_factor = 0.0
    residuals = tuple(value - (intercept + slope * current) for value, current in zip(y, x))
    sse = sum(weight * residual * residual for weight, residual in zip(weights, residuals))
    y_mean = sum(weight * value for weight, value in zip(weights, y)) / sw
    total = sum(weight * (value - y_mean) ** 2 for weight, value in zip(weights, y))
    r_squared = 1.0 if total == 0.0 and sse == 0.0 else (0.0 if total == 0.0 else 1.0 - sse / total)
    predictions = tuple(math.exp(intercept + slope * current) for current in x)
    relative_rmse = math.sqrt(
        fmean(((prediction - observed) / observed) ** 2 for prediction, observed in zip(predictions, original_y))
    )
    sample_count = len(x)
    degrees_of_freedom = sample_count - parameter_count
    scale = sse / degrees_of_freedom if degrees_of_freedom > 0 else None
    slope_se = (
        math.sqrt(scale * slope_variance_factor)
        if scale is not None and fixed_slope is None
        else (0.0 if fixed_slope is not None else None)
    )
    safe_sse = max(sse, 1e-30)
    aic = sample_count * math.log(safe_sse / sample_count) + 2.0 * parameter_count
    aicc = (
        aic + 2.0 * parameter_count * (parameter_count + 1) / (sample_count - parameter_count - 1)
        if sample_count > parameter_count + 1
        else math.inf
    )
    return {
        "intercept": intercept,
        "slope": slope,
        "slope_standard_error": slope_se,
        "r_squared": r_squared,
        "relative_rmse": relative_rmse,
        "aicc": aicc,
    }


def _leave_one_out_log_relative_rmse(
    points: Sequence[HarmonicScalingPoint],
    *,
    expected_order: int,
) -> float | None:
    if len(points) < 4:
        return None
    errors: list[float] = []
    for held_out_index, held_out in enumerate(points):
        training = tuple(
            point for index, point in enumerate(points) if index != held_out_index
        )
        fit = _log_power_fit(
            tuple(math.log(point.current_a_rms) for point in training),
            tuple(math.log(point.amplitude_v) for point in training),
            tuple(point.amplitude_v for point in training),
            fixed_slope=float(expected_order),
            sigma_y=tuple(
                (
                    point.amplitude_standard_error_v / point.amplitude_v
                    if point.amplitude_standard_error_v > 0.0
                    else None
                )
                for point in training
            ),
        )
        predicted = math.exp(
            (fit["intercept"] or 0.0)
            + expected_order * math.log(held_out.current_a_rms)
        )
        errors.append(
            (predicted - held_out.amplitude_v)
            / max(abs(held_out.amplitude_v), 1e-30)
        )
    return math.sqrt(fmean(error**2 for error in errors))


def _leave_one_out_scalar_relative_rmse(
    points: Sequence[HarmonicScalingPoint],
    *,
    expected_order: int,
    selected_model: ScalarHarmonicScalingModel | None,
) -> float | None:
    if selected_model is None or len(points) < 4:
        return None
    current_reference = math.sqrt(
        min(point.current_a_rms for point in points)
        * max(point.current_a_rms for point in points)
    )
    errors: list[float] = []
    for held_out_index, held_out in enumerate(points):
        training = tuple(
            point for index, point in enumerate(points) if index != held_out_index
        )
        model = _fit_scalar_power_law(
            training,
            name="scalar_cross_validation",
            exponent=float(expected_order),
            includes_background=selected_model.includes_background,
            current_reference=current_reference,
        )
        predicted = _scalar_model_prediction(model, held_out.current_a_rms)
        errors.append(
            (predicted - held_out.amplitude_v)
            / max(abs(held_out.amplitude_v), 1e-30)
        )
    return math.sqrt(fmean(error**2 for error in errors))


def _leave_one_out_complex_relative_rmse(
    points: Sequence[HarmonicScalingPoint],
    *,
    expected_order: int,
    selected_model: ComplexHarmonicScalingModel | None,
) -> float | None:
    if selected_model is None or len(points) < 4:
        return None
    current_reference = math.sqrt(
        min(point.current_a_rms for point in points)
        * max(point.current_a_rms for point in points)
    )
    errors: list[float] = []
    for held_out_index, held_out in enumerate(points):
        training = tuple(
            point for index, point in enumerate(points) if index != held_out_index
        )
        model = _fit_complex_power_law(
            training,
            name="complex_cross_validation",
            exponent=float(expected_order),
            includes_background=selected_model.includes_background,
            current_reference=current_reference,
        )
        predicted_x, predicted_y = _complex_model_prediction(
            model, held_out.current_a_rms
        )
        predicted_amplitude = math.hypot(predicted_x, predicted_y)
        errors.append(
            (predicted_amplitude - held_out.amplitude_v)
            / max(abs(held_out.amplitude_v), 1e-30)
        )
    return math.sqrt(fmean(error**2 for error in errors))


def _assess_scalar_harmonic_scaling(
    points: Sequence[HarmonicScalingPoint],
    *,
    expected_order: int,
    rules: HarmonicScalingRules,
) -> ScalarHarmonicScalingAssessment:
    """Compare phase-blind amplitude models with non-negative ``b`` and ``A``."""

    reasons: list[str] = [
        "phase is ignored; this fit uses measured amplitude R only"
    ]
    if len(points) < rules.minimum_points:
        reasons.append(f"requires at least {rules.minimum_points} scalar fit points")
        return _empty_scalar_scaling_assessment(reasons)
    current_min = min(point.current_a_rms for point in points)
    current_max = max(point.current_a_rms for point in points)
    if current_min <= 0.0 or current_max <= current_min:
        reasons.append("scalar-fit current values must span positive values")
        return _empty_scalar_scaling_assessment(reasons)
    current_decades = math.log10(current_max / current_min)
    if current_decades < rules.minimum_current_decades:
        reasons.append(
            f"scalar-fit current span is {current_decades:.3g} decades; "
            f"requires {rules.minimum_current_decades:.3g}"
        )
        return _empty_scalar_scaling_assessment(reasons)
    current_reference = math.sqrt(current_min * current_max)
    no_offset_fixed = _fit_scalar_power_law(
        points,
        name="scalar_no_offset_fixed_order",
        exponent=float(expected_order),
        includes_background=False,
        current_reference=current_reference,
    )
    no_offset_free = _fit_free_scalar_power_law(
        points,
        name="scalar_no_offset_free_order",
        includes_background=False,
        current_reference=current_reference,
        confidence_level=rules.confidence_level,
        exponent_min=rules.complex_free_exponent_min,
        exponent_max=rules.complex_free_exponent_max,
    )
    offset_fixed = _fit_scalar_power_law(
        points,
        name="scalar_offset_fixed_order",
        exponent=float(expected_order),
        includes_background=True,
        current_reference=current_reference,
    )
    offset_free = _fit_free_scalar_power_law(
        points,
        name="scalar_offset_free_order",
        includes_background=True,
        current_reference=current_reference,
        confidence_level=rules.confidence_level,
        exponent_min=rules.complex_free_exponent_min,
        exponent_max=rules.complex_free_exponent_max,
    )
    models = (no_offset_fixed, no_offset_free, offset_fixed, offset_free)
    background_delta = _aicc_difference(no_offset_fixed, offset_fixed)
    background_verdict = _background_verdict(background_delta, rules)
    if rules.scalar_background_mode == "none":
        selected_fixed = no_offset_fixed
        selected_free = no_offset_free
    elif rules.scalar_background_mode == "with_offset":
        selected_fixed = offset_fixed
        selected_free = offset_free
    else:
        selected_fixed = min(
            (no_offset_fixed, offset_fixed), key=_finite_aicc_sort_key
        )
        selected_free = (
            offset_free if selected_fixed.includes_background else no_offset_free
        )
    delta_aicc = _aicc_difference(selected_fixed, selected_free)
    exponent_width = (
        selected_free.exponent_ci_high - selected_free.exponent_ci_low
        if (
            selected_free.exponent_ci_low is not None
            and selected_free.exponent_ci_high is not None
        )
        else None
    )
    verdict, verdict_reasons = _scalar_power_law_verdict(
        expected_order=expected_order,
        fixed_model=selected_fixed,
        free_model=selected_free,
        exponent_ci_width=exponent_width,
        delta_aicc=delta_aicc,
        rules=rules,
    )
    reasons.extend(verdict_reasons)
    if rules.scalar_background_mode == "auto":
        reasons.append(
            "automatic scalar background selection chose "
            f"{selected_fixed.name} by corrected AIC"
        )
    return ScalarHarmonicScalingAssessment(
        models=models,
        selected_fixed_model=selected_fixed,
        selected_free_model=selected_free,
        background_verdict=background_verdict,
        background_delta_aicc=background_delta,
        exponent_ci_width=exponent_width,
        delta_aicc_fixed_minus_free=delta_aicc,
        power_law_verdict=verdict,
        reasons=tuple(dict.fromkeys(reasons)),
    )


def _empty_scalar_scaling_assessment(
    reasons: Sequence[str],
) -> ScalarHarmonicScalingAssessment:
    return ScalarHarmonicScalingAssessment(
        models=(),
        selected_fixed_model=None,
        selected_free_model=None,
        background_verdict="insufficient_data",
        background_delta_aicc=None,
        exponent_ci_width=None,
        delta_aicc_fixed_minus_free=None,
        power_law_verdict="insufficient_data",
        reasons=tuple(reasons),
    )


def _fit_free_scalar_power_law(
    points: Sequence[HarmonicScalingPoint],
    *,
    name: str,
    includes_background: bool,
    current_reference: float,
    confidence_level: float,
    exponent_min: float,
    exponent_max: float,
) -> ScalarHarmonicScalingModel:
    """Profile a phase-blind amplitude exponent with a bounded search."""

    lower = exponent_min
    upper = exponent_max
    grid_count = 121
    grid = tuple(
        lower + (upper - lower) * index / (grid_count - 1)
        for index in range(grid_count)
    )
    grid_models = tuple(
        _fit_scalar_power_law(
            points,
            name=name,
            exponent=exponent,
            includes_background=includes_background,
            current_reference=current_reference,
            free_exponent=True,
        )
        for exponent in grid
    )
    best_index = min(
        range(len(grid_models)),
        key=lambda index: _finite_aicc_sort_key(grid_models[index]),
    )
    best = grid_models[best_index]
    if 0 < best_index < len(grid_models) - 1:
        left = grid[best_index - 1]
        right = grid[best_index + 1]
        golden_ratio = (math.sqrt(5.0) - 1.0) / 2.0
        left_inner = right - golden_ratio * (right - left)
        right_inner = left + golden_ratio * (right - left)
        left_model = _fit_scalar_power_law(
            points,
            name=name,
            exponent=left_inner,
            includes_background=includes_background,
            current_reference=current_reference,
            free_exponent=True,
        )
        right_model = _fit_scalar_power_law(
            points,
            name=name,
            exponent=right_inner,
            includes_background=includes_background,
            current_reference=current_reference,
            free_exponent=True,
        )
        for _ in range(60):
            if _finite_aicc_sort_key(left_model) <= _finite_aicc_sort_key(right_model):
                right = right_inner
                right_inner = left_inner
                right_model = left_model
                left_inner = right - golden_ratio * (right - left)
                left_model = _fit_scalar_power_law(
                    points,
                    name=name,
                    exponent=left_inner,
                    includes_background=includes_background,
                    current_reference=current_reference,
                    free_exponent=True,
                )
            else:
                left = left_inner
                left_inner = right_inner
                left_model = right_model
                right_inner = left + golden_ratio * (right - left)
                right_model = _fit_scalar_power_law(
                    points,
                    name=name,
                    exponent=right_inner,
                    includes_background=includes_background,
                    current_reference=current_reference,
                    free_exponent=True,
                )
        best = min((left_model, right_model), key=_finite_aicc_sort_key)
    exponent_step = min(0.01, (upper - lower) / 100.0)
    if lower < best.exponent - exponent_step and best.exponent + exponent_step < upper:
        low = _fit_scalar_power_law(
            points,
            name=name,
            exponent=best.exponent - exponent_step,
            includes_background=includes_background,
            current_reference=current_reference,
            free_exponent=True,
        )
        high = _fit_scalar_power_law(
            points,
            name=name,
            exponent=best.exponent + exponent_step,
            includes_background=includes_background,
            current_reference=current_reference,
            free_exponent=True,
        )
        second_derivative = (
            (low.weighted_residual_sum_squares or 0.0)
            - 2.0 * (best.weighted_residual_sum_squares or 0.0)
            + (high.weighted_residual_sum_squares or 0.0)
        ) / exponent_step**2
        parameter_count = (2 if includes_background else 1) + 1
        degrees_of_freedom = len(points) - parameter_count
        residual_scale = (
            (best.weighted_residual_sum_squares or 0.0) / degrees_of_freedom
            if degrees_of_freedom > 0
            else None
        )
        exponent_standard_error = (
            math.sqrt(2.0 * residual_scale / second_derivative)
            if (
                residual_scale is not None
                and residual_scale >= 0.0
                and second_derivative > 0.0
            )
            else None
        )
        if exponent_standard_error is not None:
            z_value = NormalDist().inv_cdf((1.0 + confidence_level) / 2.0)
            return replace(
                best,
                exponent_standard_error=exponent_standard_error,
                exponent_ci_low=best.exponent - z_value * exponent_standard_error,
                exponent_ci_high=best.exponent + z_value * exponent_standard_error,
            )
    return best


def _fit_scalar_power_law(
    points: Sequence[HarmonicScalingPoint],
    *,
    name: str,
    exponent: float,
    includes_background: bool,
    current_reference: float,
    free_exponent: bool = False,
) -> ScalarHarmonicScalingModel:
    if not math.isfinite(exponent) or exponent <= 0.0:
        raise ValueError("A scalar power-law exponent must be finite and positive.")
    scaled_current = tuple(
        (point.current_a_rms / current_reference) ** exponent for point in points
    )
    observations = tuple(point.amplitude_v for point in points)
    weights = tuple(
        1.0 / point.amplitude_standard_error_v**2
        if (
            math.isfinite(point.amplitude_standard_error_v)
            and point.amplitude_standard_error_v > 0.0
        )
        else 1.0
        for point in points
    )
    background, response = _weighted_nonnegative_affine_fit(
        scaled_current,
        observations,
        weights,
        includes_background=includes_background,
    )
    predictions = tuple(
        background + response * value for value in scaled_current
    )
    residuals = tuple(
        observation - prediction
        for observation, prediction in zip(observations, predictions)
    )
    weighted_sse = sum(
        weight * residual**2 for weight, residual in zip(weights, residuals)
    )
    total_weight = sum(weights)
    weighted_mean = sum(
        weight * observation for weight, observation in zip(weights, observations)
    ) / total_weight
    total = sum(
        weight * (observation - weighted_mean) ** 2
        for weight, observation in zip(weights, observations)
    )
    r_squared = (
        1.0
        if total == 0.0 and weighted_sse == 0.0
        else (0.0 if total == 0.0 else 1.0 - weighted_sse / total)
    )
    relative_rmse = math.sqrt(
        fmean(
            ((prediction - observation) / max(abs(observation), 1e-30)) ** 2
            for prediction, observation in zip(predictions, observations)
        )
    )
    parameter_count = (2 if includes_background else 1) + (1 if free_exponent else 0)
    return ScalarHarmonicScalingModel(
        name=name,
        includes_background=includes_background,
        free_exponent=free_exponent,
        phase_ignored=True,
        current_reference_a_rms=current_reference,
        exponent=exponent,
        exponent_standard_error=None,
        exponent_ci_low=None,
        exponent_ci_high=None,
        background_v=background,
        response_v_at_reference_current=response,
        r_squared=r_squared,
        relative_rmse=relative_rmse,
        weighted_residual_sum_squares=weighted_sse,
        aicc=_corrected_aicc(
            weighted_sse,
            observation_count=len(points),
            parameter_count=parameter_count,
        ),
    )


def _weighted_nonnegative_affine_fit(
    x: Sequence[float],
    y: Sequence[float],
    weights: Sequence[float],
    *,
    includes_background: bool,
) -> tuple[float, float]:
    """Fit ``b + A*x`` while preventing negative amplitude/background terms."""

    def score(background: float, response: float) -> float:
        return sum(
            weight * (observation - background - response * value) ** 2
            for value, observation, weight in zip(x, y, weights)
        )

    denominator = sum(weight * value**2 for value, weight in zip(x, weights))
    if denominator <= 0.0:
        raise ValueError("Scalar scaling current values must vary from zero.")
    response = max(
        0.0,
        sum(weight * value * observation for value, observation, weight in zip(x, y, weights))
        / denominator,
    )
    candidates = [(0.0, response)]
    if includes_background:
        total_weight = sum(weights)
        weighted_x = sum(weight * value for value, weight in zip(x, weights))
        weighted_y = sum(weight * observation for observation, weight in zip(y, weights))
        weighted_xy = sum(
            weight * value * observation
            for value, observation, weight in zip(x, y, weights)
        )
        line_denominator = total_weight * denominator - weighted_x**2
        if line_denominator > 0.0:
            background = (denominator * weighted_y - weighted_x * weighted_xy) / line_denominator
            response = (total_weight * weighted_xy - weighted_x * weighted_y) / line_denominator
            if background >= 0.0 and response >= 0.0:
                candidates.append((background, response))
        background = max(0.0, weighted_y / total_weight)
        candidates.append((background, 0.0))
    return min(candidates, key=lambda pair: score(*pair))


def _scalar_power_law_verdict(
    *,
    expected_order: int,
    fixed_model: ScalarHarmonicScalingModel,
    free_model: ScalarHarmonicScalingModel,
    exponent_ci_width: float | None,
    delta_aicc: float | None,
    rules: HarmonicScalingRules,
) -> tuple[str, tuple[str, ...]]:
    reasons: list[str] = []
    if free_model.exponent_ci_low is None or free_model.exponent_ci_high is None:
        return "ambiguous", ("scalar free exponent could not be estimated",)
    ci_epsilon = max(1e-9, abs(expected_order) * 1e-9)
    ci_contains_order = (
        free_model.exponent_ci_low - ci_epsilon
        <= expected_order
        <= free_model.exponent_ci_high + ci_epsilon
    )
    if not ci_contains_order:
        reasons.append(
            f"expected order {expected_order} is outside the scalar exponent CI"
        )
    narrow_enough = (
        rules.max_exponent_ci_width is None
        or (
            exponent_ci_width is not None
            and exponent_ci_width <= rules.max_exponent_ci_width
        )
    )
    if not narrow_enough and exponent_ci_width is not None:
        reasons.append(
            f"scalar exponent CI width {exponent_ci_width:.3g} exceeds "
            f"{rules.max_exponent_ci_width:.3g}"
        )
    rmse_ok = (
        rules.max_relative_rmse is None
        or (
            fixed_model.relative_rmse is not None
            and fixed_model.relative_rmse <= rules.max_relative_rmse
        )
    )
    if not rmse_ok and fixed_model.relative_rmse is not None:
        reasons.append(
            f"scalar fixed-order relative RMSE {fixed_model.relative_rmse:.3g} "
            f"exceeds {rules.max_relative_rmse:.3g}"
        )
    fixed_competitive = (
        rules.max_delta_aicc_consistent is None
        or (
            delta_aicc is not None
            and delta_aicc <= rules.max_delta_aicc_consistent
        )
    )
    free_preferred = (
        rules.min_delta_aicc_inconsistent is not None
        and delta_aicc is not None
        and delta_aicc > rules.min_delta_aicc_inconsistent
    )
    if ci_contains_order and narrow_enough and fixed_competitive and rmse_ok:
        return "consistent", tuple(reasons)
    if not ci_contains_order and free_preferred:
        return "inconsistent", tuple(reasons)
    return "ambiguous", tuple(reasons)


def _assess_complex_harmonic_scaling(
    points: Sequence[HarmonicScalingPoint],
    *,
    expected_order: int,
    rules: HarmonicScalingRules,
) -> ComplexHarmonicScalingAssessment:
    """Compare background-aware complex power-law models without SciPy."""

    reasons: list[str] = []
    if len(points) < rules.minimum_points:
        reasons.append(f"requires at least {rules.minimum_points} complex fit points")
        return _empty_complex_scaling_assessment(reasons)
    current_min = min(point.current_a_rms for point in points)
    current_max = max(point.current_a_rms for point in points)
    if current_min <= 0.0 or current_max <= current_min:
        reasons.append("complex-fit current values must span positive values")
        return _empty_complex_scaling_assessment(reasons)
    current_decades = math.log10(current_max / current_min)
    if current_decades < rules.minimum_current_decades:
        reasons.append(
            f"complex-fit current span is {current_decades:.3g} decades; "
            f"requires {rules.minimum_current_decades:.3g}"
        )
        return _empty_complex_scaling_assessment(reasons)
    current_reference = math.sqrt(current_min * current_max)
    no_offset_fixed = _fit_complex_power_law(
        points,
        name="no_offset_fixed_order",
        exponent=float(expected_order),
        includes_background=False,
        current_reference=current_reference,
    )
    no_offset_free = _fit_free_complex_power_law(
        points,
        name="no_offset_free_order",
        includes_background=False,
        current_reference=current_reference,
        confidence_level=rules.confidence_level,
        exponent_min=rules.complex_free_exponent_min,
        exponent_max=rules.complex_free_exponent_max,
    )
    offset_fixed = _fit_complex_power_law(
        points,
        name="offset_fixed_order",
        exponent=float(expected_order),
        includes_background=True,
        current_reference=current_reference,
    )
    offset_free = _fit_free_complex_power_law(
        points,
        name="offset_free_order",
        includes_background=True,
        current_reference=current_reference,
        confidence_level=rules.confidence_level,
        exponent_min=rules.complex_free_exponent_min,
        exponent_max=rules.complex_free_exponent_max,
    )
    models = (no_offset_fixed, no_offset_free, offset_fixed, offset_free)
    background_delta = _aicc_difference(no_offset_fixed, offset_fixed)
    background_verdict = _background_verdict(background_delta, rules)
    if rules.complex_background_mode == "none":
        selected_fixed = no_offset_fixed
        selected_free = no_offset_free
    elif rules.complex_background_mode == "with_offset":
        selected_fixed = offset_fixed
        selected_free = offset_free
    else:
        selected_fixed = min(
            (no_offset_fixed, offset_fixed), key=_finite_aicc_sort_key
        )
        selected_free = (
            offset_free
            if selected_fixed.includes_background
            else no_offset_free
        )
    delta_aicc = _aicc_difference(selected_fixed, selected_free)
    exponent_width = (
        selected_free.exponent_ci_high - selected_free.exponent_ci_low
        if (
            selected_free.exponent_ci_low is not None
            and selected_free.exponent_ci_high is not None
        )
        else None
    )
    verdict, verdict_reasons = _complex_power_law_verdict(
        expected_order=expected_order,
        fixed_model=selected_fixed,
        free_model=selected_free,
        exponent_ci_width=exponent_width,
        delta_aicc=delta_aicc,
        rules=rules,
    )
    reasons.extend(verdict_reasons)
    if rules.complex_background_mode == "auto":
        reasons.append(
            "automatic background selection chose "
            f"{selected_fixed.name} by corrected AIC"
        )
    return ComplexHarmonicScalingAssessment(
        models=models,
        selected_fixed_model=selected_fixed,
        selected_free_model=selected_free,
        background_verdict=background_verdict,
        background_delta_aicc=background_delta,
        exponent_ci_width=exponent_width,
        delta_aicc_fixed_minus_free=delta_aicc,
        power_law_verdict=verdict,
        reasons=tuple(dict.fromkeys(reasons)),
    )


def _empty_complex_scaling_assessment(
    reasons: Sequence[str],
) -> ComplexHarmonicScalingAssessment:
    return ComplexHarmonicScalingAssessment(
        models=(),
        selected_fixed_model=None,
        selected_free_model=None,
        background_verdict="insufficient_data",
        background_delta_aicc=None,
        exponent_ci_width=None,
        delta_aicc_fixed_minus_free=None,
        power_law_verdict="insufficient_data",
        reasons=tuple(reasons),
    )


def _fit_free_complex_power_law(
    points: Sequence[HarmonicScalingPoint],
    *,
    name: str,
    includes_background: bool,
    current_reference: float,
    confidence_level: float,
    exponent_min: float,
    exponent_max: float,
) -> ComplexHarmonicScalingModel:
    """Profile the exponent, solving X and Y coefficients linearly per value."""

    lower = exponent_min
    upper = exponent_max
    grid_count = 121
    grid = tuple(
        lower + (upper - lower) * index / (grid_count - 1)
        for index in range(grid_count)
    )
    grid_models = tuple(
        _fit_complex_power_law(
            points,
            name=name,
            exponent=exponent,
            includes_background=includes_background,
            current_reference=current_reference,
            free_exponent=True,
        )
        for exponent in grid
    )
    best_index = min(range(len(grid_models)), key=lambda index: _finite_aicc_sort_key(grid_models[index]))
    best = grid_models[best_index]
    if 0 < best_index < len(grid_models) - 1:
        left = grid[best_index - 1]
        right = grid[best_index + 1]
        golden_ratio = (math.sqrt(5.0) - 1.0) / 2.0
        left_inner = right - golden_ratio * (right - left)
        right_inner = left + golden_ratio * (right - left)
        left_model = _fit_complex_power_law(
            points,
            name=name,
            exponent=left_inner,
            includes_background=includes_background,
            current_reference=current_reference,
            free_exponent=True,
        )
        right_model = _fit_complex_power_law(
            points,
            name=name,
            exponent=right_inner,
            includes_background=includes_background,
            current_reference=current_reference,
            free_exponent=True,
        )
        for _ in range(60):
            if _finite_aicc_sort_key(left_model) <= _finite_aicc_sort_key(right_model):
                right = right_inner
                right_inner = left_inner
                right_model = left_model
                left_inner = right - golden_ratio * (right - left)
                left_model = _fit_complex_power_law(
                    points,
                    name=name,
                    exponent=left_inner,
                    includes_background=includes_background,
                    current_reference=current_reference,
                    free_exponent=True,
                )
            else:
                left = left_inner
                left_inner = right_inner
                left_model = right_model
                right_inner = left + golden_ratio * (right - left)
                right_model = _fit_complex_power_law(
                    points,
                    name=name,
                    exponent=right_inner,
                    includes_background=includes_background,
                    current_reference=current_reference,
                    free_exponent=True,
                )
        best = min((left_model, right_model), key=_finite_aicc_sort_key)
    exponent_step = min(0.01, (upper - lower) / 100.0)
    if lower < best.exponent - exponent_step and best.exponent + exponent_step < upper:
        low = _fit_complex_power_law(
            points,
            name=name,
            exponent=best.exponent - exponent_step,
            includes_background=includes_background,
            current_reference=current_reference,
            free_exponent=True,
        )
        high = _fit_complex_power_law(
            points,
            name=name,
            exponent=best.exponent + exponent_step,
            includes_background=includes_background,
            current_reference=current_reference,
            free_exponent=True,
        )
        second_derivative = (
            (low.weighted_residual_sum_squares or 0.0)
            - 2.0 * (best.weighted_residual_sum_squares or 0.0)
            + (high.weighted_residual_sum_squares or 0.0)
        ) / exponent_step**2
        parameter_count = 5 if includes_background else 3
        degrees_of_freedom = 2 * len(points) - parameter_count
        residual_scale = (
            (best.weighted_residual_sum_squares or 0.0) / degrees_of_freedom
            if degrees_of_freedom > 0
            else None
        )
        exponent_standard_error = (
            math.sqrt(2.0 * residual_scale / second_derivative)
            if (
                residual_scale is not None
                and residual_scale >= 0.0
                and second_derivative > 0.0
            )
            else None
        )
        if exponent_standard_error is not None:
            z_value = NormalDist().inv_cdf((1.0 + confidence_level) / 2.0)
            return replace(
                best,
                exponent_standard_error=exponent_standard_error,
                exponent_ci_low=best.exponent - z_value * exponent_standard_error,
                exponent_ci_high=best.exponent + z_value * exponent_standard_error,
            )
    return best


def _fit_complex_power_law(
    points: Sequence[HarmonicScalingPoint],
    *,
    name: str,
    exponent: float,
    includes_background: bool,
    current_reference: float,
    free_exponent: bool = False,
) -> ComplexHarmonicScalingModel:
    if not math.isfinite(exponent) or exponent <= 0.0:
        raise ValueError("A complex power-law exponent must be finite and positive.")
    scaled_current = tuple(
        (point.current_a_rms / current_reference) ** exponent for point in points
    )
    x_weights = _complex_component_weights(points, "x_standard_error_v")
    y_weights = _complex_component_weights(points, "y_standard_error_v")
    background_x, response_x = _weighted_complex_component_fit(
        scaled_current,
        tuple(point.x_v for point in points),
        x_weights,
        includes_background=includes_background,
    )
    background_y, response_y = _weighted_complex_component_fit(
        scaled_current,
        tuple(point.y_v for point in points),
        y_weights,
        includes_background=includes_background,
    )
    predicted_x = tuple(background_x + response_x * value for value in scaled_current)
    predicted_y = tuple(background_y + response_y * value for value in scaled_current)
    residual_x = tuple(
        point.x_v - prediction for point, prediction in zip(points, predicted_x)
    )
    residual_y = tuple(
        point.y_v - prediction for point, prediction in zip(points, predicted_y)
    )
    weighted_sse = sum(
        weight * residual**2 for weight, residual in zip(x_weights, residual_x)
    ) + sum(weight * residual**2 for weight, residual in zip(y_weights, residual_y))
    r_squared = _complex_r_squared(
        points,
        x_weights=x_weights,
        y_weights=y_weights,
        weighted_sse=weighted_sse,
    )
    signal_rms = math.sqrt(
        fmean(point.x_v**2 + point.y_v**2 for point in points)
    )
    residual_rms = math.sqrt(
        fmean(
            x_residual**2 + y_residual**2
            for x_residual, y_residual in zip(residual_x, residual_y)
        )
    )
    parameter_count = (4 if includes_background else 2) + (1 if free_exponent else 0)
    return ComplexHarmonicScalingModel(
        name=name,
        includes_background=includes_background,
        free_exponent=free_exponent,
        current_reference_a_rms=current_reference,
        exponent=exponent,
        exponent_standard_error=None,
        exponent_ci_low=None,
        exponent_ci_high=None,
        background_x_v=background_x,
        background_y_v=background_y,
        background_amplitude_v=math.hypot(background_x, background_y),
        background_phase_deg=_complex_phase_deg(background_x, background_y),
        response_x_v_at_reference_current=response_x,
        response_y_v_at_reference_current=response_y,
        response_amplitude_v_at_reference_current=math.hypot(response_x, response_y),
        response_phase_deg=_complex_phase_deg(response_x, response_y),
        r_squared=r_squared,
        relative_rmse=(residual_rms / signal_rms if signal_rms > 0.0 else None),
        weighted_residual_sum_squares=weighted_sse,
        aicc=_corrected_aicc(
            weighted_sse,
            observation_count=2 * len(points),
            parameter_count=parameter_count,
        ),
    )


def _complex_component_weights(
    points: Sequence[HarmonicScalingPoint],
    standard_error_field: str,
) -> tuple[float, ...]:
    return tuple(
        1.0 / standard_error**2
        if math.isfinite(standard_error) and standard_error > 0.0
        else 1.0
        for standard_error in (
            float(getattr(point, standard_error_field)) for point in points
        )
    )


def _weighted_complex_component_fit(
    x: Sequence[float],
    y: Sequence[float],
    weights: Sequence[float],
    *,
    includes_background: bool,
) -> tuple[float, float]:
    denominator = sum(weight * value**2 for weight, value in zip(weights, x))
    if denominator <= 0.0:
        raise ValueError("Complex scaling current values must vary from zero.")
    if not includes_background:
        response = sum(
            weight * value * observation
            for weight, value, observation in zip(weights, x, y)
        ) / denominator
        return 0.0, response
    total_weight = sum(weights)
    weighted_x = sum(weight * value for weight, value in zip(weights, x))
    weighted_y = sum(weight * observation for weight, observation in zip(weights, y))
    weighted_xx = denominator
    weighted_xy = sum(
        weight * value * observation
        for weight, value, observation in zip(weights, x, y)
    )
    line_denominator = total_weight * weighted_xx - weighted_x**2
    if line_denominator <= 0.0:
        raise ValueError("Complex scaling current values must vary.")
    background = (weighted_xx * weighted_y - weighted_x * weighted_xy) / line_denominator
    response = (total_weight * weighted_xy - weighted_x * weighted_y) / line_denominator
    return background, response


def _complex_r_squared(
    points: Sequence[HarmonicScalingPoint],
    *,
    x_weights: Sequence[float],
    y_weights: Sequence[float],
    weighted_sse: float,
) -> float | None:
    def total_for_component(values: Sequence[float], weights: Sequence[float]) -> float:
        total_weight = sum(weights)
        mean = sum(weight * value for weight, value in zip(weights, values)) / total_weight
        return sum(
            weight * (value - mean) ** 2 for weight, value in zip(weights, values)
        )

    total = total_for_component(tuple(point.x_v for point in points), x_weights)
    total += total_for_component(tuple(point.y_v for point in points), y_weights)
    if total <= 0.0:
        return 1.0 if weighted_sse == 0.0 else 0.0
    return 1.0 - weighted_sse / total


def _corrected_aicc(
    weighted_sse: float,
    *,
    observation_count: int,
    parameter_count: int,
) -> float:
    safe_sse = max(weighted_sse, 1e-30)
    aic = observation_count * math.log(safe_sse / observation_count)
    aic += 2.0 * parameter_count
    if observation_count <= parameter_count + 1:
        return math.inf
    return aic + (
        2.0 * parameter_count * (parameter_count + 1)
        / (observation_count - parameter_count - 1)
    )


def _complex_phase_deg(x_v: float, y_v: float) -> float | None:
    return math.degrees(math.atan2(y_v, x_v)) if math.hypot(x_v, y_v) > 0.0 else None


def _finite_aicc_sort_key(model: ComplexHarmonicScalingModel) -> float:
    return model.aicc if model.aicc is not None else math.inf


def _aicc_difference(
    fixed_model: ComplexHarmonicScalingModel,
    free_model: ComplexHarmonicScalingModel,
) -> float | None:
    if fixed_model.aicc is None or free_model.aicc is None:
        return None
    return fixed_model.aicc - free_model.aicc


def _background_verdict(
    delta_aicc: float | None,
    rules: HarmonicScalingRules,
) -> str:
    if delta_aicc is None:
        return "ambiguous"
    if (
        rules.max_delta_aicc_consistent is None
        or delta_aicc <= rules.max_delta_aicc_consistent
    ):
        return "not_needed"
    if (
        rules.min_delta_aicc_inconsistent is not None
        and delta_aicc > rules.min_delta_aicc_inconsistent
    ):
        return "preferred"
    return "ambiguous"


def _complex_power_law_verdict(
    *,
    expected_order: int,
    fixed_model: ComplexHarmonicScalingModel,
    free_model: ComplexHarmonicScalingModel,
    exponent_ci_width: float | None,
    delta_aicc: float | None,
    rules: HarmonicScalingRules,
) -> tuple[str, tuple[str, ...]]:
    reasons: list[str] = []
    if free_model.exponent_ci_low is None or free_model.exponent_ci_high is None:
        return "ambiguous", ("complex free exponent could not be estimated",)
    ci_epsilon = max(1e-9, abs(expected_order) * 1e-9)
    ci_contains_order = (
        free_model.exponent_ci_low - ci_epsilon
        <= expected_order
        <= free_model.exponent_ci_high + ci_epsilon
    )
    if not ci_contains_order:
        reasons.append(
            f"expected order {expected_order} is outside the complex exponent CI"
        )
    narrow_enough = (
        rules.max_exponent_ci_width is None
        or (
            exponent_ci_width is not None
            and exponent_ci_width <= rules.max_exponent_ci_width
        )
    )
    if not narrow_enough and exponent_ci_width is not None:
        reasons.append(
            f"complex exponent CI width {exponent_ci_width:.3g} exceeds "
            f"{rules.max_exponent_ci_width:.3g}"
        )
    rmse_ok = (
        rules.max_relative_rmse is None
        or (
            fixed_model.relative_rmse is not None
            and fixed_model.relative_rmse <= rules.max_relative_rmse
        )
    )
    if not rmse_ok and fixed_model.relative_rmse is not None:
        reasons.append(
            f"complex fixed-order relative RMSE "
            f"{fixed_model.relative_rmse:.3g} exceeds "
            f"{rules.max_relative_rmse:.3g}"
        )
    fixed_competitive = (
        rules.max_delta_aicc_consistent is None
        or (
            delta_aicc is not None
            and delta_aicc <= rules.max_delta_aicc_consistent
        )
    )
    free_preferred = (
        rules.min_delta_aicc_inconsistent is not None
        and delta_aicc is not None
        and delta_aicc > rules.min_delta_aicc_inconsistent
    )
    if ci_contains_order and narrow_enough and fixed_competitive and rmse_ok:
        return "consistent", tuple(reasons)
    if not ci_contains_order and free_preferred:
        return "inconsistent", tuple(reasons)
    return "ambiguous", tuple(reasons)


def _phase_scaling_metrics(
    points: Sequence[HarmonicScalingPoint],
) -> tuple[float | None, float | None]:
    if len(points) < 2:
        return None, None
    ordered = sorted(points, key=lambda point: point.current_a_rms)
    unwrapped: list[float] = []
    previous_wrapped = None
    previous_unwrapped = None
    for point in ordered:
        if previous_wrapped is None or previous_unwrapped is None:
            value = point.phase_deg
        else:
            delta = (point.phase_deg - previous_wrapped + 180.0) % 360.0 - 180.0
            value = previous_unwrapped + delta
        unwrapped.append(value)
        previous_wrapped = point.phase_deg
        previous_unwrapped = value
    log_current = tuple(math.log10(point.current_a_rms) for point in ordered)
    mean_x = fmean(log_current)
    mean_y = fmean(unwrapped)
    denominator = sum((value - mean_x) ** 2 for value in log_current)
    if denominator <= 0.0:
        return None, None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(log_current, unwrapped)) / denominator
    return slope, max(unwrapped) - min(unwrapped)


def _amplitude_scaling_verdict(
    *,
    expected_order: int,
    exponent: float | None,
    exponent_low: float | None,
    exponent_high: float | None,
    exponent_width: float | None,
    fixed_relative_rmse: float | None,
    delta_aicc: float | None,
    rules: HarmonicScalingRules,
) -> tuple[str, tuple[str, ...]]:
    reasons: list[str] = []
    if exponent is None or exponent_low is None or exponent_high is None:
        return "ambiguous", ("free exponent could not be estimated",)
    ci_epsilon = max(1e-9, abs(expected_order) * 1e-9)
    ci_contains_order = (
        exponent_low - ci_epsilon <= expected_order <= exponent_high + ci_epsilon
    )
    if not ci_contains_order:
        reasons.append(f"expected order {expected_order} is outside the exponent CI")
    if (
        rules.max_exponent_ci_width is not None
        and exponent_width is not None
        and exponent_width > rules.max_exponent_ci_width
    ):
        reasons.append(
            f"exponent CI width {exponent_width:.3g} exceeds "
            f"{rules.max_exponent_ci_width:.3g}"
        )
    if (
        rules.max_relative_rmse is not None
        and fixed_relative_rmse is not None
        and fixed_relative_rmse > rules.max_relative_rmse
    ):
        reasons.append(
            f"fixed-order relative RMSE {fixed_relative_rmse:.3g} exceeds "
            f"{rules.max_relative_rmse:.3g}"
        )
    fixed_competitive = (
        rules.max_delta_aicc_consistent is None
        or delta_aicc is not None
        and delta_aicc <= rules.max_delta_aicc_consistent
    )
    free_preferred = (
        rules.min_delta_aicc_inconsistent is not None
        and delta_aicc is not None
        and delta_aicc > rules.min_delta_aicc_inconsistent
    )
    narrow_enough = (
        rules.max_exponent_ci_width is None
        or exponent_width is not None
        and exponent_width <= rules.max_exponent_ci_width
    )
    rmse_ok = (
        rules.max_relative_rmse is None
        or fixed_relative_rmse is not None
        and fixed_relative_rmse <= rules.max_relative_rmse
    )
    if ci_contains_order and narrow_enough and fixed_competitive and rmse_ok:
        return "consistent", tuple(reasons)
    if not ci_contains_order and free_preferred:
        return "inconsistent", tuple(reasons)
    return "ambiguous", tuple(reasons)


@publication_plot
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
        "actual_frequency_hz" if scan_type == "frequency" else "source_v_rms"
    )
    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError(
            "Plotting requires: python -m pip install -e '.[analysis]'"
        ) from exc
    figure, axis = plt.subplots(
        figsize=PUBLICATION_SINGLE_FIGSIZE, constrained_layout=True
    )
    for role_index, role in enumerate(("xx", "xy")):
        selected = [item for item in statistics if item.role == role]
        if not selected:
            continue
        axis.errorbar(
            [item.x_value for item in selected],
            [item.mean for item in selected],
            yerr=[item.standard_deviation for item in selected],
            color=OKABE_ITO_ON_WHITE[role_index],
            marker=("o", "s")[role_index],
            linestyle=("-", "--")[role_index],
            linewidth=1.35,
            capsize=2.5,
            elinewidth=0.8,
            label=role.upper(),
        )
    use_log_x = log_x if log_x is not None else scan_type == "frequency"
    if use_log_x:
        axis.set_xscale("log")
    axis.set_xlabel(
        {
            "target_frequency_hz": "Frequency (Hz)",
            "actual_frequency_hz": "Frequency (Hz)",
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
    style_axis(axis)
    uncertainty = "circular sample SD" if metric == "phase_deg" else "sample SD"
    outside_legend(
        axis,
        title=f"Signal\nError bars: {uncertainty}",
    )
    if destination is not None:
        save_publication_figure(figure, destination)
    return figure


@publication_plot
def plot_role_harmonic_sweep(
    rows: Sequence[CommissioningSample],
    *,
    role: str,
    harmonic: int,
    excitation_path: ExcitationPathResistance | None = None,
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
        "actual_frequency_hz"
        if scan_type == "frequency"
        else "sine_output_current_a_rms"
    )
    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError(
            "Plotting requires: python -m pip install -e '.[analysis]'"
        ) from exc
    figure, (voltage_axis, phase_axis) = plt.subplots(
        2,
        1,
        figsize=PUBLICATION_STACKED_FIGSIZE,
        sharex=True,
        gridspec_kw={"height_ratios": (3, 2)},
        constrained_layout=True,
    )
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
            linestyle="-",
            linewidth=1.35,
            capsize=2.5,
            elinewidth=0.8,
            color=OKABE_ITO_ON_WHITE[0],
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
            linestyle="--",
            linewidth=1.35,
            color=OKABE_ITO_ON_WHITE[1],
            label=phase_label,
        )
        if qualified_phase_x_values:
            phase_axis.errorbar(
                qualified_phase_x_values,
                qualified_phase_values,
                yerr=qualified_phase_spreads,
                fmt="none",
                capsize=2.5,
                elinewidth=0.8,
                color=OKABE_ITO_ON_WHITE[1],
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
        phase_axis.set_xlabel("Frequency (Hz)")
        title_prefix = _current_summary(selected, excitation_path)
        title = (
            f"Frequency sweep · {signal_name} · h{harmonic}\n"
            f"{title_prefix}"
        )
    else:
        phase_axis.set_xlabel("SINE OUT current (A RMS)")
        title = f"Current–voltage sweep · {signal_name} · h{harmonic}"
    voltage_axis.set_ylabel(f"{signal_name} R (V RMS)")
    phase_axis.set_ylabel("Unwrapped phase (°)")
    voltage_axis.set_title(title)
    style_axis(voltage_axis)
    style_axis(phase_axis)
    if voltage_axis.get_legend_handles_labels()[0]:
        outside_legend(voltage_axis, title="Mean ± sample SD")
    if phase_axis.get_legend_handles_labels()[0]:
        outside_legend(phase_axis, title="Mean ± circular sample SD")
    if destination is not None:
        save_publication_figure(figure, destination)
    return figure


@publication_plot
def plot_multi_frequency_iv_curves(
    rows: Sequence[CommissioningSample],
    *,
    role: str,
    harmonic: int,
    metric: str = "amplitude_v",
    excitation_path: ExcitationPathResistance | None = None,
    destination: str | Path | None = None,
):
    """Plot combined-sweep I--V curves, with one colored curve per frequency."""

    statistics = aggregate_frequency_excitation_iv(
        rows,
        role=role,
        harmonic=harmonic,
        metric=metric,
        excitation_path=excitation_path,
    )
    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError(
            "Plotting requires: python -m pip install -e '.[analysis]'"
        ) from exc
    figure, axis = plt.subplots(
        figsize=PUBLICATION_SINGLE_FIGSIZE, constrained_layout=True
    )
    frequencies = sorted({item.frequency_hz for item in statistics})
    for index, frequency_hz in enumerate(frequencies):
        selected = [item for item in statistics if item.frequency_hz == frequency_hz]
        axis.errorbar(
            [item.current_a_rms for item in selected],
            [item.mean for item in selected],
            yerr=[item.standard_deviation for item in selected],
            **ordered_series_style(index, len(frequencies)),
            linewidth=1.35,
            markersize=4.5,
            markeredgewidth=0.7,
            capsize=2.5,
            elinewidth=0.8,
            label=f"{frequency_hz:.7g} Hz",
        )
    if statistics and all(item.current_a_rms > 0.0 for item in statistics):
        axis.set_xscale("log")
    axis.set_xlabel("SINE OUT current (A RMS)")
    axis.set_ylabel(
        {
            "x_v": "X (V RMS)",
            "y_v": "Y (V RMS)",
            "amplitude_v": "R (V RMS)",
            "phase_deg": "Phase (degree)",
        }[metric]
    )
    axis.set_title(f"Combined sweep · I–V{role} · h{harmonic}")
    style_axis(axis)
    if frequencies:
        uncertainty = (
            "circular sample SD" if metric == "phase_deg" else "sample SD"
        )
        outside_legend(
            axis,
            title=f"Actual frequency\nError bars: {uncertainty}",
        )
    if destination is not None:
        save_publication_figure(figure, destination)
    return figure


def plot_six_role_harmonic_sweeps(
    rows: Sequence[CommissioningSample],
    *,
    excitation_path: ExcitationPathResistance | None = None,
    phase_minimum_amplitude_v: float = 0.0,
    phase_maximum_standard_deviation_deg: float | None = None,
) -> dict[tuple[str, int], object]:
    """Return one figure for each selected XX/XY and harmonic combination."""

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
        if any(row.role == role and row.harmonic == harmonic for row in rows)
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


def _formal_selected_roles(sample: Mapping[str, object]) -> tuple[str, ...]:
    """Return formal roles, treating records before role selection as paired."""

    selected = sample.get("selected_roles")
    if selected is None:
        return PLOT_ROLES
    if not isinstance(selected, list) or not selected:
        raise ValueError("Sweep sample selected_roles must be a non-empty role list.")
    roles = tuple(str(role) for role in selected)
    if len(set(roles)) != len(roles) or set(roles) - set(PLOT_ROLES):
        raise ValueError("Sweep sample selected_roles contains an unknown or duplicate role.")
    return roles


def _recorded_excitation_path(
    payload: Mapping[str, object],
) -> ExcitationPathResistance | None:
    """Read a complete path snapshot from a current sweep JSON record.

    Older records predate the snapshot and return ``None``. A present but malformed
    snapshot is an audit error rather than a reason to silently choose a path.
    """

    measurement_config = payload.get("measurement_config")
    if not isinstance(measurement_config, Mapping):
        return None
    excitation_path = measurement_config.get("excitation_path")
    if excitation_path is None:
        return None
    if not isinstance(excitation_path, Mapping):
        raise ValueError("measurement_config.excitation_path must be an object.")
    values = (
        excitation_path.get("series_resistance_ohm"),
        excitation_path.get("sr830_output_resistance_ohm"),
        excitation_path.get("approximate_device_resistance_ohm"),
    )
    if any(value is None for value in values):
        raise ValueError("measurement_config.excitation_path is incomplete.")
    try:
        return ExcitationPathResistance(
            external_series_resistance_ohm=float(values[0]),
            sr830_output_resistance_ohm=float(values[1]),
            approximate_device_resistance_ohm=float(values[2]),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("measurement_config.excitation_path is invalid.") from exc


def _commissioning_sample(
    *,
    path: Path,
    record_status: str,
    scan_type: str,
    point: Mapping[str, object],
    sample: Mapping[str, object],
    role: str,
    problems: tuple[str, ...],
    recorded_excitation_path: ExcitationPathResistance | None,
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
    # Daily sweeps do not use the SR830 CH1/CH2 output path.  LIAS bit 2 is
    # still preserved in each JSON sample for audit, but it must not make a
    # formally accepted measurement disappear from default analysis.  For
    # legacy records without decoded status, retain the recorded overload bit.
    if lia_status:
        overload = any(
            bool(lia_status.get(name))
            for name in ("input_or_reserve_overload", "filter_overload")
        )
    else:
        overload = bool(reading.get("overload"))
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
    actual_frequency = point.get("actual_frequency_hz")
    if actual_frequency is None:
        actual_frequency = point["target_frequency_hz"]
    return CommissioningSample(
        source_path=str(path),
        record_status=record_status,
        scan_type=scan_type,
        point_index=int(point.get("point_index", 0)),
        sample_index=int(sample.get("sample_index", 0)),
        target_frequency_hz=float(point["target_frequency_hz"]),
        actual_frequency_hz=float(actual_frequency),
        source_v_rms=float(point["source_v_rms"]),
        sine_output_v_rms=float(sine_output),
        nominal_current_a_rms=(
            None if nominal_current is None else float(nominal_current)
        ),
        recorded_external_series_resistance_ohm=(
            None
            if recorded_excitation_path is None
            else recorded_excitation_path.external_series_resistance_ohm
        ),
        recorded_sr830_output_resistance_ohm=(
            None
            if recorded_excitation_path is None
            else recorded_excitation_path.sr830_output_resistance_ohm
        ),
        recorded_approximate_device_resistance_ohm=(
            None
            if recorded_excitation_path is None
            else recorded_excitation_path.approximate_device_resistance_ohm
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


def _excitation_path_for_row(
    row: CommissioningSample,
    override: ExcitationPathResistance | None,
) -> ExcitationPathResistance:
    """Use an explicit override, otherwise the path archived with this record."""

    if override is not None:
        return override
    values = (
        row.recorded_external_series_resistance_ohm,
        row.recorded_sr830_output_resistance_ohm,
        row.recorded_approximate_device_resistance_ohm,
    )
    if all(value is None for value in values):
        raise ValueError(
            "SINE OUT current requires the recorded measurement_config.excitation_path; "
            "provide an explicit ExcitationPathResistance override only for legacy data."
        )
    if any(value is None for value in values):
        raise ValueError("Recorded excitation-path resistance is incomplete.")
    return ExcitationPathResistance(
        external_series_resistance_ohm=float(values[0]),
        sr830_output_resistance_ohm=float(values[1]),
        approximate_device_resistance_ohm=float(values[2]),
    )


def _single_excitation_path_for_rows(
    rows: Sequence[CommissioningSample],
    override: ExcitationPathResistance | None,
) -> ExcitationPathResistance:
    """Resolve one path and reject a mixed-calibration curve by default."""

    if override is not None:
        return override
    paths = {_excitation_path_for_row(row, None) for row in rows}
    if len(paths) != 1:
        raise ValueError(
            "Selected sweep rows record different excitation paths; plot them "
            "separately or provide one explicit ExcitationPathResistance override."
        )
    return next(iter(paths))


def _sweep_x_value(
    row: CommissioningSample,
    x_axis: str,
    excitation_path: ExcitationPathResistance | None,
) -> float:
    if x_axis == "sine_output_current_a_rms":
        return _excitation_path_for_row(
            row, excitation_path
        ).current_from_sine_output(row.sine_output_v_rms)
    raw_x = getattr(row, x_axis)
    if raw_x is None:
        raise ValueError(f"Selected x axis {x_axis} contains missing values.")
    return float(raw_x)


def _current_summary(
    rows: Sequence[CommissioningSample],
    excitation_path: ExcitationPathResistance | None,
) -> str:
    if not rows:
        return "I_RMS unavailable"
    resolved_excitation_path = _single_excitation_path_for_rows(
        rows, excitation_path
    )
    currents = [
        resolved_excitation_path.current_from_sine_output(row.sine_output_v_rms)
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
    if value == 0.0:
        return f"0 {unit}"
    for scale, prefix in ((1.0, ""), (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"), (1e-12, "p")):
        if abs(value) >= scale or scale == 1e-12:
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
