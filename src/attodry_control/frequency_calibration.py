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
            residual = math.sqrt(fmean(abs(z - slope * u) ** 2 for u, z in observations))
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
            except ValueError as exc:
                reason = str(exc)
        output.append(CorrectedH2(
            source_path=row.source_path, point_index=row.point_index,
            sample_index=row.sample_index, role=row.role,
            frequency_hz=row.actual_frequency_hz, source_v_rms=row.sine_output_v_rms,
            raw_v=raw, corrected_v=corrected, factor=factor,
            interpolated=interpolated, reason=reason,
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
def plot_frequency_response(response: FrequencyResponse):
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
    axes[2].set_ylabel("Fit residual RMS (V)")
    axes[2].set_xlabel("Actual excitation frequency (Hz)")
    axes[2].set_xscale("log")
    for axis in axes:
        style_axis(axis)
    method = "complex slope + intercept" if response.method == "frequency_excitation" else "mean(V1/U); intercept not identifiable"
    figure.suptitle(f"{response.role.upper()} h1 relative response · {method}\nReference {response.reference_actual_frequency_hz:g} Hz")
    return figure


@publication_plot
def plot_h2_correction(rows: Sequence[CorrectedH2], *, model: str):
    import matplotlib.pyplot as plt

    retained = sorted((row for row in rows if row.corrected_v is not None), key=lambda row: row.frequency_hz)
    if not retained:
        raise ValueError("No h2 samples lie within the selected calibration range.")
    figure, axes = plt.subplots(2, 1, sharex=True, figsize=PUBLICATION_STACKED_FIGSIZE, layout="constrained")
    frequencies = [row.frequency_hz for row in retained]
    for values, name, color, marker in (
        ([row.raw_v for row in retained], "Raw", "#0072B2", "o"),
        ([row.corrected_v for row in retained], "Corrected", "#D55E00", "s"),
    ):
        axes[0].scatter(frequencies, [abs(value) for value in values], label=name, color=color, marker=marker)
        axes[1].scatter(frequencies, [math.degrees(cmath.phase(value)) for value in values], label=name, color=color, marker=marker)
    axes[0].set_ylabel("h2 magnitude (V RMS)")
    axes[1].set_ylabel("h2 phase (deg, wrapped)")
    axes[1].set_xlabel("Actual excitation frequency (Hz)")
    axes[1].set_xscale("log")
    axes[0].legend()
    for axis in axes:
        style_axis(axis)
    figure.suptitle(f"H2 relative correction · {model} · measured points only")
    return figure
