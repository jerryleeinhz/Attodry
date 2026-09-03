"""Read-only XY-only plots for SR830 frequency and excitation sweeps."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

from .commissioning_analysis import (
    CommissioningSample,
    ExcitationPathResistance,
    aggregate_sweep_samples,
    load_sweep_samples,
)


def load_xy_sweep_samples(
    path: str | Path,
    *,
    include_rejected: bool = False,
    sample_statuses: Iterable[str] | None = None,
) -> tuple[CommissioningSample, ...]:
    """Load formal XY samples while discarding XX at the loader boundary."""

    return load_sweep_samples(
        path,
        include_rejected=include_rejected,
        sample_statuses=sample_statuses,
        roles={"xy"},
    )


def xy_sweep_harmonic(rows: Sequence[CommissioningSample]) -> int:
    """Return the single harmonic order represented by the XY sweep rows."""

    xy_rows = tuple(row for row in rows if row.role == "xy")
    if not xy_rows:
        raise ValueError("No XY sweep samples match the selected filters.")
    harmonics = {row.harmonic for row in xy_rows}
    if len(harmonics) != 1:
        raise ValueError("Plot one XY harmonic order at a time.")
    return next(iter(harmonics))


def plot_xy_sweep(
    rows: Sequence[CommissioningSample],
    *,
    metric: str = "amplitude_v",
    x_axis: str | None = None,
    log_x: bool | None = None,
    excitation_path: ExcitationPathResistance | None = None,
    destination: str | Path | None = None,
):
    """Plot an XY-only frequency or excitation sweep and label its harmonic."""

    xy_rows = tuple(row for row in rows if row.role == "xy")
    harmonic = xy_sweep_harmonic(xy_rows)
    statistics = aggregate_sweep_samples(
        xy_rows,
        metric=metric,
        x_axis=x_axis,
        excitation_path=excitation_path,
    )
    scan_type = xy_rows[0].scan_type
    resolved_x_axis = x_axis or (
        "actual_frequency_hz" if scan_type == "frequency" else "source_v_rms"
    )
    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise RuntimeError(
            "Plotting requires: python -m pip install -e '.[analysis]'"
        ) from exc
    figure, axis = plt.subplots(figsize=(6.6, 4.4), constrained_layout=True)
    axis.errorbar(
        [item.x_value for item in statistics],
        [item.mean for item in statistics],
        yerr=[item.standard_deviation for item in statistics],
        marker="o",
        linewidth=1.2,
        capsize=3,
        label=f"XY · h{harmonic}",
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
    axis.set_title(f"SR830 XY h{harmonic} {scan_type} sweep")
    axis.grid(True, alpha=0.25)
    axis.legend(
        loc="upper left",
        bbox_to_anchor=(1.02, 1.0),
        borderaxespad=0.0,
    )
    if destination is not None:
        figure.savefig(destination, dpi=200)
    return figure
