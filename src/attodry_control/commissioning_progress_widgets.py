"""Manual, file-only notebook controls for commissioning progress snapshots."""

from __future__ import annotations

from datetime import datetime, timezone
import html
import math
from pathlib import Path

from .commissioning_progress_analysis import (
    CommissioningProgressReader,
    discover_progress_records,
    plot_progress_sweep,
)


def _last_record_time(timestamp: float | None) -> str:
    if timestamp is None:
        return "not recorded"
    try:
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return "not recorded"


def _snapshot_status(snapshot) -> str:
    def escape(value) -> str:
        return html.escape(str(value))

    state = (
        "Finished historical snapshot."
        if snapshot.finished
        else "Unfinished snapshot — partial data."
    )
    points_total = "unknown" if snapshot.points_total is None else snapshot.points_total
    current_point = snapshot.current_point or {}

    def recorded_point_value(key: str) -> str:
        value = current_point.get(key)
        return "not recorded" if value is None else escape(value)

    point_status = (
        "<p>Latest recorded point (historical snapshot): "
        f"index {recorded_point_value('point_index')}<br>"
        f"Requested SINE OUT (V RMS): {recorded_point_value('source_v_rms')}; "
        f"SINE OUT readback (V RMS): {recorded_point_value('source_readback_v_rms')}<br>"
        f"Actual frequency (Hz): {recorded_point_value('actual_frequency_hz')}</p>"
    )
    quality_rows = []
    for role, label in (("xx", "lockin_xx (Vxx)"), ("xy", "lockin_xy (Vxy)")):
        counts = snapshot.quality_counts.get(role, {})
        quality_rows.append(
            f"<tr><td>{label}</td><td>{escape(counts.get('total', 0))}</td>"
            f"<td>{escape(counts.get('clean', 0))}</td>"
            f"<td>{escape(counts.get('excluded', 0))}</td></tr>"
        )
    if snapshot.finished:
        verified = (
            "not recorded"
            if snapshot.cleanup_verified is None
            else str(snapshot.cleanup_verified).lower()
        )
        finish = (
            f"Finish record outcome: <b>{escape(snapshot.outcome or 'not recorded')}</b>; "
            f"recorded cleanup verified: <b>{verified}</b>. "
            "Outcome and cleanup are historical values from this file; "
            "they do not confirm current process or hardware state."
        )
    else:
        finish = "No finish record observed. Process and hardware state are unknown."
    frequency_axis = (
        "<p>Frequency scans use actual recorded frequency on the horizontal axis.</p>"
        if snapshot.scan_type == "frequency"
        else ""
    )
    return (
        f"<p><b>{state}</b><br>File: <code>{escape(snapshot.path)}</code><br>"
        f"Run name: {escape(snapshot.run_name or 'not recorded')}; "
        f"scan: {escape(snapshot.scan_type)}<br>"
        f"Completed points: {escape(snapshot.completed_point_count)} / {escape(points_total)}; "
        f"formal pairs: {escape(snapshot.formal_pair_count)}<br>"
        f"Last record: {escape(snapshot.last_event or 'not recorded')}; "
        f"UTC: {escape(_last_record_time(snapshot.last_event_unix_s))}</p>"
        "<table><thead><tr><th>Selected formal role</th><th>Total</th>"
        "<th>Clean</th><th>Excluded</th></tr></thead><tbody>"
        + "".join(quality_rows)
        + f"</tbody></table><p>{finish}</p>"
        + point_status
        + "<p>Curves use only clean selected formal samples. "
        "Transition and cleanup readings are excluded.</p>"
        + frequency_axis
    )


def build_progress_panel(
    directory: str | Path,
    *,
    minimum_phase_amplitude_v: float = 1e-6,
    maximum_phase_standard_deviation_deg: float = 5.0,
):
    """Return a VBox that reads data only on explicit load or refresh clicks.

    Catalog discovery lists paths without parsing every progress record. Selecting
    another path clears the displayed snapshot; no file is selected implicitly.
    There is no timer, hardware access, or automatic final-JSON selection.
    """
    import ipywidgets as widgets
    import matplotlib.pyplot as plt
    from IPython.display import display

    data_directory = Path(directory).expanduser().resolve()
    record_widget = widgets.Dropdown(
        options=(("Select a progress file…", None),),
        value=None,
        description="Progress file",
        layout=widgets.Layout(width="100%"),
    )
    refresh_records_button = widgets.Button(description="Refresh records")
    load_button = widgets.Button(description="Load selected progress", disabled=True)
    refresh_data_button = widgets.Button(description="Refresh data and plots", disabled=True)
    metric_widget = widgets.Dropdown(
        options=(("R (amplitude)", "amplitude_v"), ("X", "x_v"),
                 ("Y", "y_v"), ("Phase", "phase_deg")),
        value="amplitude_v",
        description="Metric",
    )
    axis_widget = widgets.Dropdown(
        options=(("SINE OUT readback (V RMS)", "sine_output_v_rms"),
                 ("Archived estimated current (A RMS)", "nominal_current_a_rms")),
        value="sine_output_v_rms",
        description="Excitation X",
        layout=widgets.Layout(width="440px"),
    )
    status_widget = widgets.HTML(value="Select a progress file, then click Load selected progress.")
    notice_widget = widgets.HTML()
    plot_output = widgets.Output()
    reader = None
    snapshot = None
    updating_catalog = False

    def clear_snapshot(message: str) -> None:
        nonlocal reader, snapshot
        reader = None
        snapshot = None
        plot_output.clear_output(wait=False)
        status_widget.value = message
        notice_widget.value = ""
        refresh_data_button.disabled = True

    def selected_changed(_change) -> None:
        if updating_catalog:
            return
        load_button.disabled = record_widget.value is None
        clear_snapshot("Selection changed. Click Load selected progress to read the selected file.")

    def settings_changed(_change) -> None:
        if snapshot is not None:
            notice_widget.value = (
                "Plot settings changed. Click Refresh data and plots to apply them. "
                "Displayed curves still belong to the previous manual refresh."
            )

    def refresh_records(_button=None) -> None:
        nonlocal updating_catalog
        previous = record_widget.value
        try:
            paths = discover_progress_records(data_directory)
            values = tuple(str(Path(path).resolve()) for path in paths)
            updating_catalog = True
            record_widget.options = (("Select a progress file…", None),) + tuple(
                (Path(path).name, path) for path in values
            )
            record_widget.value = previous if previous in values else None
            updating_catalog = False
            if record_widget.value != previous:
                selected_changed(None)
            load_button.disabled = record_widget.value is None
            if not values:
                notice_widget.value = (
                    f"No commissioning progress files found in "
                    f"<code>{html.escape(str(data_directory))}</code>."
                )
        except Exception as error:
            updating_catalog = False
            record_widget.options = (("Select a progress file…", None),)
            record_widget.value = None
            load_button.disabled = True
            clear_snapshot(f"Catalog refresh failed: {html.escape(str(error))}")

    def render_snapshot() -> None:
        plot_output.clear_output(wait=False)
        status_widget.value = _snapshot_status(snapshot)
        notice_widget.value = ""
        if snapshot.finished and snapshot.outcome in ("rejected", "interrupted"):
            notice_widget.value = (
                f"This {html.escape(snapshot.outcome)} record is excluded from default progress plots. "
                "Raw counts remain visible above. Use the final JSON browser's explicit "
                "audit selection when a final record is available."
            )
            return
        if not snapshot.clean_rows:
            notice_widget.value = (
                "No clean selected formal samples are available in this snapshot. "
                "No curves are drawn; this can occur before the first formal sample "
                "or when all recorded samples are excluded."
            )
            return
        if snapshot.scan_type != "frequency" and axis_widget.value == "nominal_current_a_rms":
            if any(
                row.nominal_current_a_rms is None
                or not math.isfinite(row.nominal_current_a_rms)
                for row in snapshot.clean_rows
            ):
                notice_widget.value = (
                    "Archived estimated current is missing for one or more clean samples. "
                    "Select SINE OUT readback (V RMS) and click Refresh data and plots. "
                    "Current is never recalculated from today's TOML."
                )
                return
        figures = ()
        try:
            figures = plot_progress_sweep(
                snapshot,
                metric=metric_widget.value,
                x_axis=axis_widget.value,
                minimum_phase_amplitude_v=minimum_phase_amplitude_v,
                maximum_phase_standard_deviation_deg=maximum_phase_standard_deviation_deg,
            )
            with plot_output:
                for figure in figures:
                    display(figure)
            if not figures:
                notice_widget.value = (
                    "No curves are available for the selected metric in this snapshot. "
                    "Phase curves also require the configured amplitude and circular-spread thresholds."
                )
        except Exception as error:
            plot_output.clear_output(wait=False)
            notice_widget.value = f"Plot failed: {html.escape(str(error))}. No previous curves are retained."
        finally:
            for figure in figures:
                plt.close(figure)

    def read_selected(*, new_reader: bool) -> None:
        nonlocal reader, snapshot
        selected = record_widget.value
        if selected is None:
            clear_snapshot("Select a progress file, then click Load selected progress.")
            return
        if not new_reader and reader is None:
            clear_snapshot("Click Load selected progress before refreshing data and plots.")
            return
        try:
            if new_reader:
                clear_snapshot("Loading selected progress…")
                reader = CommissioningProgressReader(Path(selected))
            snapshot = reader.refresh()
            refresh_data_button.disabled = False
        except Exception as error:
            clear_snapshot(
                f"Progress load failed for <code>{html.escape(selected)}</code>: "
                f"{html.escape(str(error))}. No previous snapshot or curves are retained."
            )
            return
        render_snapshot()

    record_widget.observe(selected_changed, names="value")
    metric_widget.observe(settings_changed, names="value")
    axis_widget.observe(settings_changed, names="value")
    refresh_records_button.on_click(refresh_records)
    load_button.on_click(lambda _button: read_selected(new_reader=True))
    refresh_data_button.on_click(lambda _button: read_selected(new_reader=False))
    panel = widgets.VBox([
        widgets.HTML(value="<h3>File progress snapshots (manual refresh)</h3>"),
        widgets.HTML(value=(
            "Refresh records lists filenames only. Select a file and load it; "
            "Refresh data and plots reads newly appended records. "
            "No automatic polling is performed."
        )),
        widgets.HBox([refresh_records_button, load_button, refresh_data_button]),
        record_widget,
        widgets.HBox([metric_widget, axis_widget]),
        status_widget,
        notice_widget,
        plot_output,
    ])
    refresh_records()
    return panel
