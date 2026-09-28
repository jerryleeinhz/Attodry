"""Read-only, relative complex SR830 frequency-response calibration.

The fitted response is an empirical transfer proxy, not a measured impedance.
No instrument modules are imported here.
"""

from __future__ import annotations

from dataclasses import dataclass
import cmath
import math
from statistics import fmean
from typing import Sequence

from .commissioning_analysis import CommissioningSample
from .scientific_plotting import PUBLICATION_STACKED_FIGSIZE, publication_plot, style_axis


@dataclass(frozen=True, slots=True)
class ResponsePoint:
    target_frequency_hz: float
    actual_frequency_hz: float
    slope_v_per_v: complex
    intercept_v: complex | None
    residual_rms_v: float
    excitation_count: int
    formal_sample_count: int


@dataclass(frozen=True, slots=True)
class FrequencyResponse:
    role: str
    method: str
    source_path: str
    reference_target_frequency_hz: float
    reference_actual_frequency_hz: float
    points: tuple[ResponsePoint, ...]
    phase_shift_deg: float | None

    @property
    def reference_slope(self) -> complex:
        return next(
            point.slope_v_per_v for point in self.points
            if point.target_frequency_hz == self.reference_target_frequency_hz
        )

    def relative_at(self, frequency_hz: float) -> tuple[complex, bool]:
        """Return Q(f) and whether log-frequency complex interpolation was used.

        The phase is unwrapped between adjacent measured points; no extrapolation.
        """

        if not math.isfinite(frequency_hz) or frequency_hz <= 0:
            raise ValueError("Calibration frequency must be finite and positive.")
        points = sorted(self.points, key=lambda point: point.actual_frequency_hz)
        for point in points:
            if math.isclose(frequency_hz, point.actual_frequency_hz, rel_tol=1e-8):
                return point.slope_v_per_v / self.reference_slope, False
        if frequency_hz < points[0].actual_frequency_hz or frequency_hz > points[-1].actual_frequency_hz:
            raise ValueError("Frequency lies outside the measured calibration range; no extrapolation.")
        for low, high in zip(points, points[1:]):
            if low.actual_frequency_hz < frequency_hz < high.actual_frequency_hz:
                low_q = low.slope_v_per_v / self.reference_slope
                high_q = high.slope_v_per_v / self.reference_slope
                fraction = math.log(frequency_hz / low.actual_frequency_hz) / math.log(
                    high.actual_frequency_hz / low.actual_frequency_hz
                )
                phase_step = cmath.phase(high_q / low_q)
                magnitude = math.exp(
                    (1 - fraction) * math.log(abs(low_q))
                    + fraction * math.log(abs(high_q))
                )
                phase = cmath.phase(low_q) + fraction * phase_step
                return cmath.rect(magnitude, phase), True
        raise ValueError("Calibration frequency is not bracketed by measured points.")


@dataclass(frozen=True, slots=True)
class CalibratedH1:
    source_path: str
    point_index: int
    sample_index: int
    role: str
    frequency_hz: float
    source_v_rms: float
    raw_v: complex
    fitted_v: complex
    intercept_v: complex | None
    relative_q: complex
    corrected_v: complex


@dataclass(frozen=True, slots=True)
class CorrectedH2:
    source_path: str
    point_index: int
    sample_index: int
    role: str
    frequency_hz: float
    source_v_rms: float
    raw_v: complex
    corrected_v: complex | None
    factor: complex | None
    interpolated: bool
    reason: str | None
    h1_proxy_v: complex | None = None
    coefficient_per_v: complex | None = None
    coefficient_reason: str | None = None


def _finite_complex(value: complex) -> bool:
    return math.isfinite(value.real) and math.isfinite(value.imag)


def estimate_frequency_response(
    rows: Sequence[CommissioningSample],
    *,
    role: str = "xx",
    reference_target_frequency_hz: float | None = None,
) -> FrequencyResponse:
    """Estimate H1 response from one frequency or f×e run's clean formal data.

    Frequency sweeps use mean(V1/U); f×e uses a per-frequency complex line
    V1=b(f)U+a(f). A minimum of three distinct readback U values leaves a
    residual degree of freedom for the intercept fit.
    """

    if role not in {"xx", "xy"}:
        raise ValueError("Calibration role must be xx or xy.")
    selected = tuple(row for row in rows if row.role == role and row.harmonic == 1)
    if not selected:
        raise ValueError(f"No {role} h1 formal samples were selected.")
    scan_types = {row.scan_type for row in selected}
    paths = {row.source_path for row in selected}
    if len(scan_types) != 1 or next(iter(scan_types)) not in {"frequency", "frequency_excitation"}:
        raise ValueError("Use one frequency or f×e scan type for calibration.")
    if len(paths) != 1:
        raise ValueError("Select one calibration run; do not pool runs with possibly different phase settings.")
    if any(row.record_status != "completed" or row.statuses != ("clean",) for row in selected):
        raise ValueError("Calibration requires completed, clean formal h1 samples only.")
    phase_settings = {row.phase_shift_deg for row in selected if row.phase_shift_deg is not None}
    if len(phase_settings) > 1:
        raise ValueError("SR830 phase setting changed within the calibration run.")
    if phase_settings and any(row.phase_shift_deg is None for row in selected):
        raise ValueError("SR830 phase setting is missing for part of the calibration run.")
    method = next(iter(scan_types))
    by_frequency: dict[float, list[CommissioningSample]] = {}
    for row in selected:
        if not math.isfinite(row.actual_frequency_hz) or row.actual_frequency_hz <= 0:
            raise ValueError("Frequency readback must be positive and finite.")
        tolerance_hz = max(0.1, 0.01 * row.target_frequency_hz)
        if (
            not math.isfinite(row.reference_frequency_hz)
            or abs(row.actual_frequency_hz - row.target_frequency_hz) > tolerance_hz
            or abs(row.reference_frequency_hz - row.actual_frequency_hz) > tolerance_hz
        ):
            raise ValueError(
                f"H1 frequency readbacks disagree with requested {row.target_frequency_hz:g} Hz; "
                "do not calibrate a mislabeled sweep."
            )
        if not math.isfinite(row.sine_output_v_rms) or row.sine_output_v_rms <= 0:
            raise ValueError("SINE OUT readback must be positive and finite.")
        if not row.source_readback_confirmed:
            raise ValueError("Calibration requires recorded SINE OUT readback, not a requested-voltage fallback.")
        if not _finite_complex(complex(row.x_v, row.y_v)):
            raise ValueError("Complex h1 reading must be finite.")
        by_frequency.setdefault(row.target_frequency_hz, []).append(row)
    points = []
    for target, frequency_rows in sorted(by_frequency.items()):
        # Average repeated formal reads within a point before fitting, so a
        # longer formal window does not silently give one excitation more weight.
        by_point: dict[int, list[CommissioningSample]] = {}
        for row in frequency_rows:
            by_point.setdefault(row.point_index, []).append(row)
        observations = []
        for group in by_point.values():
            u = fmean(row.sine_output_v_rms for row in group)
            z = complex(fmean(row.x_v for row in group), fmean(row.y_v for row in group))
            observations.append((u, z))
        if method == "frequency":
            slope = sum((z / u for u, z in observations), 0j) / len(observations)
            intercept = None  # Not identifiable with one excitation per frequency.
            # With one excitation, the point mean is reproduced by definition.
            # Report formal-read scatter, not that tautological zero residual.
            residual = math.sqrt(fmean(
                abs(complex(row.x_v, row.y_v) - slope * row.sine_output_v_rms) ** 2
                for row in frequency_rows
            ))
        else:
            distinct_u = {u for u, _ in observations}
            if len(distinct_u) < 3:
                raise ValueError(f"f×e frequency {target:g} Hz needs at least three distinct SINE OUT readbacks for complex intercept fit.")
            mean_u = fmean(u for u, _ in observations)
            mean_z = sum((z for _, z in observations), 0j) / len(observations)
            denominator = sum((u - mean_u) ** 2 for u, _ in observations)
            slope = sum(((u - mean_u) * (z - mean_z) for u, z in observations), 0j) / denominator
            intercept = mean_z - slope * mean_u
            residual = math.sqrt(fmean(abs(z - slope * u - intercept) ** 2 for u, z in observations))
        if not _finite_complex(slope) or abs(slope) == 0:
            raise ValueError(f"Zero or non-finite h1 response at {target:g} Hz cannot be calibrated.")
        points.append(ResponsePoint(
            target_frequency_hz=target,
            actual_frequency_hz=fmean(row.actual_frequency_hz for row in frequency_rows),
            slope_v_per_v=slope,
            intercept_v=intercept,
            residual_rms_v=residual,
            excitation_count=len(observations),
            formal_sample_count=len(frequency_rows),
        ))
    if len(points) < 2:
        raise ValueError("At least two measured frequencies are needed for a response curve.")
    actual = [point.actual_frequency_hz for point in points]
    if any(right <= left for left, right in zip(actual, actual[1:])):
        raise ValueError("Actual frequencies must be strictly increasing with requested frequencies.")
    reference_target = points[0].target_frequency_hz if reference_target_frequency_hz is None else reference_target_frequency_hz
    matches = [point for point in points if point.target_frequency_hz == reference_target]
    if len(matches) != 1:
        raise ValueError("Reference must be one measured requested frequency, not an interpolated value.")
    return FrequencyResponse(
        role=role, method=method, source_path=next(iter(paths)),
        reference_target_frequency_hz=reference_target,
        reference_actual_frequency_hz=matches[0].actual_frequency_hz,
        points=tuple(points),
        phase_shift_deg=next(iter(phase_settings)) if phase_settings else None,
    )


def calibrate_h1_samples(
    rows: Sequence[CommissioningSample], response: FrequencyResponse,
) -> tuple[CalibratedH1, ...]:
    """Show fitted V1 and (V1-a)/Q on the same data that estimated Q.

    This is an in-sample normalization diagnostic, not independent validation.
    A frequency-only sweep assumes no intercept; it cannot estimate one.
    """
    selected = tuple(row for row in rows if row.role == response.role and row.harmonic == 1)
    # Re-estimation also checks provenance, filters, finite values and phase.
    rebuilt = estimate_frequency_response(
        selected, role=response.role,
        reference_target_frequency_hz=response.reference_target_frequency_hz,
    )
    if rebuilt != response:
        raise ValueError("H1 data or filters changed; rebuild the response first.")
    points = {point.target_frequency_hz: point for point in response.points}
    output = []
    for row in selected:
        point = points[row.target_frequency_hz]
        raw = complex(row.x_v, row.y_v)
        offset = point.intercept_v if point.intercept_v is not None else 0j
        q = point.slope_v_per_v / response.reference_slope
        output.append(CalibratedH1(
            source_path=row.source_path, point_index=row.point_index,
            sample_index=row.sample_index, role=row.role,
            frequency_hz=row.actual_frequency_hz, source_v_rms=row.sine_output_v_rms,
            raw_v=raw, fitted_v=point.slope_v_per_v * row.sine_output_v_rms + offset,
            intercept_v=point.intercept_v, relative_q=q, corrected_v=(raw - offset) / q,
        ))
    return tuple(output)


def correct_h2(
    rows: Sequence[CommissioningSample],
    response: FrequencyResponse,
    *,
    model: str,
    role: str,
) -> tuple[CorrectedH2, ...]:
    """Apply a declared limiting model; never call it a full chain correction.

    ``excitation_squared`` assumes Q tracks the input path and the h2 detector
    is frequency-flat. ``readout_2f`` assumes Q tracks the same channel's output
    detector and the input path is flat; h1 must be calibrated through 2f.
    """

    if model not in {"excitation_squared", "readout_2f"}:
        raise ValueError("Choose an explicit h2 correction model.")
    if role not in {"xx", "xy"}:
        raise ValueError("Target role must be xx or xy.")
    if model == "readout_2f" and role != response.role:
        raise ValueError("Readout-only correction requires h1 and h2 from the same SR830 channel.")
    if any(row.source_path != response.source_path for row in rows if row.role == role and row.harmonic == 2):
        raise ValueError("H2 correction currently requires the same run as the h1 calibration.")
    selected_phase_settings = {
        row.phase_shift_deg for row in rows
        if row.role == role and row.harmonic == 2 and row.phase_shift_deg is not None
    }
    if len(selected_phase_settings) > 1:
        raise ValueError("SR830 h2 phase setting changed within the selected correction data.")
    if selected_phase_settings and any(
        row.phase_shift_deg is None for row in rows
        if row.role == role and row.harmonic == 2
    ):
        raise ValueError("SR830 h2 phase setting is missing for part of the correction data.")
    output = []
    for row in rows:
        if row.role != role or row.harmonic != 2:
            continue
        raw = complex(row.x_v, row.y_v)
        factor = None
        corrected = None
        interpolated = False
        reason = None
        h1_proxy = None
        coefficient = None
        coefficient_reason = None
        if row.record_status != "completed" or row.statuses != ("clean",):
            reason = "non-clean or incomplete formal sample"
        elif not _finite_complex(raw):
            reason = "non-finite raw h2"
        elif (
            not math.isfinite(row.actual_frequency_hz)
            or row.actual_frequency_hz <= 0
            or abs(row.actual_frequency_hz - row.target_frequency_hz)
            > max(0.1, 0.01 * row.target_frequency_hz)
        ):
            reason = "h2 frequency readback disagrees with requested frequency"
        else:
            try:
                if model == "excitation_squared":
                    q, interpolated = response.relative_at(row.actual_frequency_hz)
                    factor = q * q
                else:
                    q, interpolated = response.relative_at(2 * row.actual_frequency_hz)
                    q0, interpolated0 = response.relative_at(2 * response.reference_actual_frequency_hz)
                    factor = q / q0
                    interpolated = interpolated or interpolated0
                corrected = raw / factor
                if model == "excitation_squared":
                    if not row.source_readback_confirmed or not math.isfinite(row.sine_output_v_rms) or row.sine_output_v_rms <= 0:
                        coefficient_reason = "Voltage proxy requires positive recorded SINE OUT readback."
                    else:
                        # b(f)U is the driven H1 voltage; the f×e intercept is
                        # background, so it is not part of the current proxy.
                        h1_proxy = q * response.reference_slope * row.sine_output_v_rms
                        coefficient = raw / h1_proxy**2
                else:
                    coefficient_reason = "Voltage proxy requires the excitation-squared model."
            except ValueError as exc:
                reason = str(exc)
        output.append(CorrectedH2(
            source_path=row.source_path, point_index=row.point_index,
            sample_index=row.sample_index, role=row.role,
            frequency_hz=row.actual_frequency_hz, source_v_rms=row.sine_output_v_rms,
            raw_v=raw, corrected_v=corrected, factor=factor,
            interpolated=interpolated, reason=reason,
            h1_proxy_v=h1_proxy, coefficient_per_v=coefficient,
            coefficient_reason=reason or coefficient_reason,
        ))
    return tuple(output)


def lcr_anchored_impedance(
    response: FrequencyResponse,
    *,
    anchor_impedance_ohm: complex,
    model: str,
) -> tuple[tuple[float, complex], ...]:
    """Conditional impedance *estimate* at measured frequencies only.

    Anchor must be an LCR measurement at the response's reference frequency and
    the same reference plane. The model cannot separate device and wiring.
    """

    if model not in {"inverse_current_proxy", "direct_impedance_proxy"}:
        raise ValueError("Choose an explicit LCR transfer model.")
    if not _finite_complex(anchor_impedance_ohm) or abs(anchor_impedance_ohm) == 0:
        raise ValueError("LCR anchor impedance must be finite and nonzero.")
    return tuple(
        (
            point.actual_frequency_hz,
            anchor_impedance_ohm / (point.slope_v_per_v / response.reference_slope)
            if model == "inverse_current_proxy"
            else anchor_impedance_ohm * (point.slope_v_per_v / response.reference_slope),
        )
        for point in response.points
    )


@publication_plot
def plot_frequency_response(response: FrequencyResponse, *, x_scale: str = "log"):
    import matplotlib.pyplot as plt

    frequencies = [point.actual_frequency_hz for point in response.points]
    relative = [point.slope_v_per_v / response.reference_slope for point in response.points]
    phases = [math.degrees(cmath.phase(relative[0]))]
    for previous, current in zip(relative, relative[1:]):
        phases.append(phases[-1] + math.degrees(cmath.phase(current / previous)))
    reference_index = next(
        index for index, point in enumerate(response.points)
        if point.target_frequency_hz == response.reference_target_frequency_hz
    )
    phases = [phase - phases[reference_index] for phase in phases]
    figure, axes = plt.subplots(3, 1, sharex=True, figsize=PUBLICATION_STACKED_FIGSIZE, layout="constrained")
    axes[0].plot(frequencies, [abs(value) for value in relative], "o-", color="#0072B2")
    axes[0].axhline(1, color="0.5", linewidth=0.8, linestyle="--")
    axes[0].set_ylabel("|Q(f)| (relative)")
    axes[1].plot(frequencies, phases, "o-", color="#D55E00")
    axes[1].axhline(0, color="0.5", linewidth=0.8, linestyle="--")
    axes[1].set_ylabel("arg Q(f) (deg)")
    axes[2].plot(frequencies, [point.residual_rms_v for point in response.points], "o-", color="#009E73")
    axes[2].set_ylabel(
        "Formal-read scatter RMS (V)" if response.method == "frequency"
        else "Fit residual RMS (V)"
    )
    axes[2].set_xlabel("Actual excitation frequency (Hz)")
    axes[2].set_xscale(x_scale)
    for axis in axes:
        style_axis(axis)
    method = "complex slope + intercept" if response.method == "frequency_excitation" else "mean(V1/U); intercept not identifiable"
    figure.suptitle(f"{response.role.upper()} h1 relative response · {method}\nReference {response.reference_actual_frequency_hz:g} Hz")
    return figure


@publication_plot
def plot_h1_calibration(rows: Sequence[CalibratedH1], *, x_scale: str = "log"):
    """Raw/fitted transfer and self-normalized H1, preserving formal repeats."""
    import matplotlib.pyplot as plt

    if not rows:
        raise ValueError("No h1 samples to plot.")
    figure, axes = plt.subplots(2, 2, sharex=True, figsize=(12, 7), layout="constrained")
    frequencies = [row.frequency_hz for row in rows]
    series = (
        (0, [row.raw_v / row.source_v_rms for row in rows], "Raw V1/U", "#0072B2", "o"),
        (0, [row.fitted_v / row.source_v_rms for row in rows], "Fitted (bU+a)/U", "#D55E00", "x"),
        (1, [row.corrected_v / row.source_v_rms for row in rows], "(V1-a)/(U Q)", "#009E73", "s"),
    )
    first = rows[0]
    reference = (first.fitted_v - (first.intercept_v or 0j)) / (first.source_v_rms * first.relative_q)
    for column, values, label, color, marker in series:
        axes[0, column].scatter(frequencies, [abs(z) for z in values], label=label, color=color, marker=marker, s=16)
        # Display corrected phases near the reference branch, preserving its
        # absolute phase. This avoids numerical +180/-180 splits at negative X.
        phases = [
            math.degrees(cmath.phase(reference) + cmath.phase(z / reference))
            if column == 1 and abs(z) else math.degrees(cmath.phase(z)) if abs(z) else math.nan
            for z in values
        ]
        axes[1, column].scatter(frequencies, phases, color=color, marker=marker, s=16)
    axes[0, 0].set_title("Raw and fitted H1 at measured frequencies")
    axes[0, 1].set_title("H1 self-normalization (same calibration data)")
    for column in (0, 1):
        axes[0, column].set_ylabel("Magnitude / SINE OUT (V/V)")
        axes[1, column].set_ylabel("Phase (deg, near reference)" if column else "Phase (deg, wrapped)")
        axes[1, column].set_xlabel("Actual excitation frequency (Hz)")
        axes[0, column].legend(fontsize=9)
    for axis in axes.flat:
        axis.set_xscale(x_scale)
        style_axis(axis)
    figure.suptitle(
        f"{rows[0].role.upper()} h1 fit and normalized samples\n"
        "A flat normalized mean follows from Q=b/b(ref); it does not validate the H2 model."
    )
    return figure


def _plot_h2_comparison(
    rows: Sequence[CorrectedH2], *, coefficient: bool, title: str,
    magnitude_scale: str, x_scale: str,
):
    import matplotlib.pyplot as plt

    if magnitude_scale not in {"linear", "log"} or x_scale not in {"linear", "log"}:
        raise ValueError("Plot scales must be linear or log.")
    attribute = "coefficient_per_v" if coefficient else "corrected_v"
    retained = [row for row in rows if getattr(row, attribute) is not None]
    if not retained:
        raise ValueError("No h2 samples have the selected derived quantity.")
    figure, (amplitude, phase) = plt.subplots(2, 1, sharex=True, figsize=(9, 7), layout="constrained")
    right = amplitude.twinx()
    raw_color, derived_color = "#0072B2", "#D55E00"
    handles = []
    zeros = 0
    for selected, attr, axis, color, marker, label in (
        (rows, "raw_v", amplitude, raw_color, "o", "Raw H2 (left)"),
        (retained, attribute, right, derived_color, "s", "Voltage coefficient (right)" if coefficient else "Corrected H2 (right)"),
    ):
        frequencies = [row.frequency_hz for row in selected]
        values = [getattr(row, attr) for row in selected]
        zeros += sum(abs(z) == 0 for z in values)
        magnitudes = [abs(z) if magnitude_scale == "linear" or abs(z) > 0 else math.nan for z in values]
        handles.append(axis.scatter(frequencies, magnitudes, label=label, color=color, marker=marker, s=16))
        phase.scatter(frequencies, [math.degrees(cmath.phase(z)) if abs(z) else math.nan for z in values], color=color, marker=marker, s=16)
        axis.set_yscale(magnitude_scale)
        if magnitude_scale == "linear":
            axis.set_ylim(bottom=0)
    amplitude.set_ylabel("Raw H2 magnitude (V RMS)", color=raw_color)
    right.set_ylabel("|H2 / H1_proxy²| (1/V)" if coefficient else "Corrected H2 magnitude (V RMS)", color=derived_color)
    phase.set_ylabel("Phase (deg, wrapped)")
    phase.set_xlabel("Actual excitation frequency (Hz)")
    phase.set_xscale(x_scale)
    for axis in (amplitude, phase, right):
        style_axis(axis)
    # One grid belongs to the left axis. The right amplitude axis has its own
    # natural autoscale and units; no scaling is chosen to align the curves.
    right.grid(False)
    amplitude.tick_params(axis="y", right=False, colors=raw_color)
    right.tick_params(axis="y", colors=derived_color)
    right.spines['right'].set_color(derived_color)
    amplitude.legend(handles=handles, loc="upper right")
    omitted = len(rows)-len(retained)
    note = f"Separate amplitude scales; {omitted} unavailable derived samples kept as raw"
    if magnitude_scale == "log":
        note += f"; {zeros} zero magnitudes absent on log axes"
    formula = "H1_proxy=b(f)U; coefficient=H2/H1_proxy² (no current conversion)" if coefficient else "Corrected H2 = raw H2 / factor"
    figure.suptitle(f"{title}\n{formula}\n{note}", fontsize=10)
    return figure


@publication_plot
def plot_h2_correction(
    rows: Sequence[CorrectedH2], *, model: str,
    magnitude_scale: str = "linear", x_scale: str = "log",
):
    return _plot_h2_comparison(
        rows, coefficient=False, title=f"H2 relative correction · {model}",
        magnitude_scale=magnitude_scale, x_scale=x_scale,
    )


@publication_plot
def plot_h2_voltage_coefficient(
    rows: Sequence[CorrectedH2], *, magnitude_scale: str = "linear", x_scale: str = "log",
):
    """Voltage-only H2/[b(f)U]^2, with raw volts and inverse volts on named axes."""
    return _plot_h2_comparison(
        rows, coefficient=True, title="H2 normalized by the driven H1 voltage",
        magnitude_scale=magnitude_scale, x_scale=x_scale,
    )
