"""Condensed, read-only report figures from completed harmonic-scaling fits."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .commissioning_analysis import (
    HarmonicScalingFit,
    HarmonicScalingPoint,
    ScalarHarmonicScalingModel,
)
from .scientific_plotting import (
    SERIES_MARKERS,
    outside_legend,
    publication_plot,
    save_publication_figure,
    style_axis,
)


ReportChannel = tuple[str, int]
REPORT_PHASE_MODES = frozenset({"none", "right"})
REPORT_COLORS = (
    "#0072B2",
    "#D55E00",
    "#009E73",
    "#CC79A7",
    "#332288",
    "#997700",
)


@publication_plot
def plot_condensed_iv_report(
    fits: Mapping[ReportChannel, HarmonicScalingFit],
    *,
    amplitude_channels: Iterable[ReportChannel],
    phase_channels: Iterable[ReportChannel] = (),
    phase_mode: str = "none",
    title: str = "Selected lock-in I–V summary",
    destination: str | Path | None = None,
):
    """Plot selected amplitudes, final scalar-R fits, and optional phase.

    Only fit-qualified points are displayed.  Every amplitude channel uses its
    selected free-exponent scalar model, so one final fitted curve is drawn per
    channel.  Phase is shown only on an explicitly requested right-hand axis.
    """

    selected_amplitudes = _validate_channels(amplitude_channels, "amplitude")
    selected_phases = _validate_channels(phase_channels, "phase")
    if not selected_amplitudes:
        raise ValueError("Select at least one amplitude channel for the report.")
    if phase_mode not in REPORT_PHASE_MODES:
        raise ValueError("phase_mode must be 'none' or 'right'.")
    if phase_mode == "none" and selected_phases:
        raise ValueError("phase_channels must be empty when phase_mode is 'none'.")
    required = set(selected_amplitudes)
    if phase_mode == "right":
        required.update(selected_phases)
    missing = sorted(required.difference(fits))
    if missing:
        raise ValueError(f"No harmonic-scaling fit is available for: {missing!r}.")

    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError(
            "Plotting requires: python -m pip install -e '.[analysis]'"
        ) from exc

    figure, amplitude_axis = plt.subplots(
        figsize=(10.8, 6.0), constrained_layout=True
    )
    phase_axis = amplitude_axis.twinx() if phase_mode == "right" else None
    channel_order = tuple(dict.fromkeys((*selected_amplitudes, *selected_phases)))
    channel_styles = {
        channel: (
            REPORT_COLORS[index % len(REPORT_COLORS)],
            SERIES_MARKERS[index % len(SERIES_MARKERS)],
        )
        for index, channel in enumerate(channel_order)
    }
    amplitude_handles = []
    phase_handles = []

    for channel in selected_amplitudes:
        fit = fits[channel]
        points = _fit_qualified_points(fit)
        model = _selected_scalar_free_model(fit)
        color, marker = channel_styles[channel]
        amplitude_axis.errorbar(
            [point.current_a_rms for point in points],
            [point.amplitude_v for point in points],
            yerr=[point.amplitude_standard_deviation_v for point in points],
            color=color,
            marker=marker,
            linestyle="none",
            markeredgewidth=0.7,
            capsize=2.5,
            elinewidth=0.8,
            label="_nolegend_",
        )
        curve_current = _logarithmic_curve_grid(points)
        (fit_line,) = amplitude_axis.plot(
            curve_current,
            [_scalar_prediction(model, current) for current in curve_current],
            color=color,
            linewidth=1.8,
            label=_format_scalar_report_label(fit, model),
        )
        amplitude_handles.append(fit_line)

    if phase_axis is not None:
        for channel in selected_phases:
            fit = fits[channel]
            points = _fit_qualified_phase_points(fit)
            color, marker = channel_styles[channel]
            phase_values = _unwrap_degrees([point.phase_deg for point in points])
            phase_container = phase_axis.errorbar(
                [point.current_a_rms for point in points],
                phase_values,
                yerr=[point.phase_standard_deviation_deg for point in points],
                color=color,
                marker=marker,
                markerfacecolor="white",
                linestyle="none",
                markeredgewidth=1.0,
                capsize=2.5,
                elinewidth=0.8,
                label=f"V{fit.role} h{fit.harmonic} phase",
            )
            phase_handles.append(phase_container)

    amplitude_axis.set_xscale("log")
    amplitude_axis.set_yscale("log")
    amplitude_axis.set_xlabel("SINE OUT current (A RMS)")
    amplitude_axis.set_ylabel("Lock-in amplitude R (V RMS)")
    amplitude_axis.set_title(title)
    style_axis(amplitude_axis)
    if phase_axis is not None:
        phase_axis.set_ylabel("Unwrapped phase (degree)")
        phase_axis.tick_params(which="both", direction="in", right=True)
        phase_axis.spines["right"].set_linewidth(0.8)

    handles = [*amplitude_handles, *phase_handles]
    legend_title_lines = ["Amplitude: mean ± sample SD"]
    if phase_handles:
        legend_title_lines.append("Phase: circular mean ± circular sample SD")
    legend_title_lines.append("Scalar R: selected free-exponent model")
    outside_legend(
        amplitude_axis,
        handles=handles,
        labels=[handle.get_label() for handle in handles],
        title="\n".join(legend_title_lines),
        fontsize=7.0,
        title_fontsize=7.5,
    )
    if destination is not None:
        save_publication_figure(figure, destination)
    return figure


def condensed_iv_report_manifest(
    fits: Mapping[ReportChannel, HarmonicScalingFit],
    *,
    amplitude_channels: Iterable[ReportChannel],
    phase_channels: Iterable[ReportChannel] = (),
    phase_mode: str = "none",
    source_paths: Iterable[str | Path] = (),
) -> dict[str, object]:
    """Return the selected report channels and final scalar-model provenance."""

    selected_amplitudes = _validate_channels(amplitude_channels, "amplitude")
    selected_phases = _validate_channels(phase_channels, "phase")
    if not selected_amplitudes:
        raise ValueError("Select at least one amplitude channel for the report.")
    if phase_mode not in REPORT_PHASE_MODES:
        raise ValueError("phase_mode must be 'none' or 'right'.")
    if phase_mode == "none" and selected_phases:
        raise ValueError("phase_channels must be empty when phase_mode is 'none'.")
    required = set(selected_amplitudes)
    if phase_mode == "right":
        required.update(selected_phases)
    missing = sorted(required.difference(fits))
    if missing:
        raise ValueError(f"No harmonic-scaling fit is available for: {missing!r}.")
    models: dict[str, object] = {}
    for channel in selected_amplitudes:
        fit = fits[channel]
        model = _selected_scalar_free_model(fit)
        models[f"{channel[0]}_h{channel[1]}"] = {
            "model_name": model.name,
            "equation": _format_scalar_equation(model),
            "background_v": model.background_v,
            "response_v_at_reference_current": model.response_v_at_reference_current,
            "current_reference_a_rms": model.current_reference_a_rms,
            "exponent": model.exponent,
            "exponent_ci_low": model.exponent_ci_low,
            "exponent_ci_high": model.exponent_ci_high,
            "r_squared": model.r_squared,
            "relative_rmse": model.relative_rmse,
            "fit_point_count": len(_fit_qualified_points(fit)),
            "excluded_point_count": fit.scalar_excluded_point_count,
        }
    phase_points = {}
    for channel in selected_phases:
        fit = fits[channel]
        qualified = _fit_qualified_phase_points(fit)
        phase_points[f"{channel[0]}_h{channel[1]}"] = {
            "qualified_point_count": len(qualified),
            "excluded_point_count": len(fit.points) - len(qualified),
        }
    return {
        "source_files": [str(Path(path)) for path in source_paths],
        "amplitude_channels": [list(channel) for channel in selected_amplitudes],
        "phase_channels": [list(channel) for channel in selected_phases],
        "phase_mode": phase_mode,
        "phase_display": (
            "circular mean and circular sample SD; "
            "unwrapped along current for display"
        ),
        "scalar_curve": "scalar_selected_free_model",
        "models": models,
        "phase_points": phase_points,
    }


def _validate_channels(
    channels: Iterable[ReportChannel],
    label: str,
) -> tuple[ReportChannel, ...]:
    selected = tuple((str(role).lower(), int(harmonic)) for role, harmonic in channels)
    invalid = [
        channel
        for channel in selected
        if channel[0] not in {"xx", "xy"} or channel[1] not in {1, 2, 3}
    ]
    if invalid:
        raise ValueError(f"Unsupported {label} report channel(s): {invalid!r}.")
    if len(set(selected)) != len(selected):
        raise ValueError(f"Duplicate {label} report channels are not allowed.")
    return selected


def _fit_qualified_points(
    fit: HarmonicScalingFit,
) -> tuple[HarmonicScalingPoint, ...]:
    points = tuple(
        sorted(
            (
                point
                for point in fit.points
                if point.included
                and point.current_a_rms > 0.0
                and point.amplitude_v > 0.0
            ),
            key=lambda point: point.current_a_rms,
        )
    )
    if not points:
        raise ValueError(f"V{fit.role} h{fit.harmonic} has no fit-qualified points.")
    return points


def _fit_qualified_phase_points(
    fit: HarmonicScalingFit,
) -> tuple[HarmonicScalingPoint, ...]:
    points = tuple(
        point
        for point in _fit_qualified_points(fit)
        if point.complex_included and math.isfinite(point.phase_deg)
    )
    if not points:
        raise ValueError(f"V{fit.role} h{fit.harmonic} has no qualified phase points.")
    return points


def _selected_scalar_free_model(
    fit: HarmonicScalingFit,
) -> ScalarHarmonicScalingModel:
    model = next(
        (
            candidate
            for candidate in fit.scalar_models
            if candidate.name == fit.scalar_selected_free_model
        ),
        None,
    )
    if model is None:
        raise ValueError(
            f"V{fit.role} h{fit.harmonic} has no selected free-exponent scalar model."
        )
    return model


def _logarithmic_curve_grid(
    points: Sequence[HarmonicScalingPoint],
    count: int = 240,
) -> tuple[float, ...]:
    lower = math.log10(points[0].current_a_rms)
    upper = math.log10(points[-1].current_a_rms)
    if lower == upper:
        return (points[0].current_a_rms,)
    return tuple(
        10.0 ** (lower + (upper - lower) * index / (count - 1))
        for index in range(count)
    )


def _scalar_prediction(
    model: ScalarHarmonicScalingModel,
    current_a_rms: float,
) -> float:
    scaled_current = (current_a_rms / model.current_reference_a_rms) ** model.exponent
    return model.background_v + model.response_v_at_reference_current * scaled_current


def _format_scalar_report_label(
    fit: HarmonicScalingFit,
    model: ScalarHarmonicScalingModel,
) -> str:
    exponent = f"p={model.exponent:.4g}"
    if model.exponent_ci_low is not None and model.exponent_ci_high is not None:
        exponent += (
            f" [{model.exponent_ci_low:.4g}, {model.exponent_ci_high:.4g}]"
        )
    metrics = []
    if model.r_squared is not None:
        metrics.append(f"R²={model.r_squared:.4g}")
    if model.relative_rmse is not None:
        metrics.append(f"relative RMSE={model.relative_rmse:.3g}")
    return "\n".join(
        (
            f"V{fit.role} h{fit.harmonic} · {exponent}",
            _format_scalar_equation(model),
            "; ".join(metrics),
        )
    ).rstrip()


def _format_scalar_equation(model: ScalarHarmonicScalingModel) -> str:
    response = f"{model.response_v_at_reference_current:.4g} V"
    current_reference = f"{model.current_reference_a_rms:.4g} A"
    power = f"(I / {current_reference})^{model.exponent:.4g}"
    if model.includes_background:
        return f"R(I) = {model.background_v:.4g} V + {response} {power}"
    return f"R(I) = {response} {power}"


def _unwrap_degrees(values: Sequence[float]) -> tuple[float, ...]:
    unwrapped: list[float] = []
    previous_wrapped = None
    previous_unwrapped = None
    for value in values:
        if previous_wrapped is None or previous_unwrapped is None:
            current = value
        else:
            delta = (value - previous_wrapped + 180.0) % 360.0 - 180.0
            current = previous_unwrapped + delta
        unwrapped.append(current)
        previous_wrapped = value
        previous_unwrapped = current
    return tuple(unwrapped)
