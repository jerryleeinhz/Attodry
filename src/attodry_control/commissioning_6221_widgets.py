"""Manual SQLite snapshot panel; no polling, instrument imports or live settings."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import html
import json
from pathlib import Path

from .commissioning_6221_analysis import list_6221_runs, load_6221_sweep
from .commissioning_analysis import (HarmonicScalingRules, export_commissioning_csv,
    fit_harmonic_scalings, plot_commissioning_sweep, plot_harmonic_scaling_fit)
from .report_plotting import plot_experiment_fit_report, plot_condensed_iv_report


def current_sweep_report(snapshot, group, *, excluded_conditions=(), metrics=("amplitude_v",),
                         rules=None, methods=("log", "scalar", "complex")):
    """Reuse legacy plot/fit engines on one immutable current-source selection."""
    rows = tuple(r for r in snapshot.groups[group] if r.condition_id not in excluded_conditions)
    if not rows:
        raise ValueError("No rows remain after point exclusions.")
    # Audit data may be displayed/exported, but never silently fit as clean data.
    fit_rows = tuple(r for r in rows if r.record_status in ("completed", "other") and r.statuses == ("clean",))
    resolved_rules = rules or HarmonicScalingRules()
    fits = fit_harmonic_scalings(fit_rows, rules=resolved_rules) if fit_rows else {}
    figures = {}
    for role, harmonic in sorted({(r.role, r.harmonic) for r in rows}):
        channel = tuple(r for r in rows if r.role == role and r.harmonic == harmonic)
        label = channel[0].channel_label or "V" + role
        title = f"{label} · h{harmonic} · {channel[0].target_frequency_hz:g} Hz requested"
        title += f"\n{channel[0].run_id} · group {group}"
        if any(r.record_status != "completed" for r in rows):
            title = "PRELIMINARY / AUDIT · " + title
        for metric in metrics:
            figure = plot_commissioning_sweep(channel, metric=metric, x_axis="excitation_current_rms_a")
            figure.axes[0].set_title(title)
            figures[f"{role}_h{harmonic}_{metric}"] = figure
        fit = fits.get((role, harmonic))
        if fit is not None:
            figure = plot_harmonic_scaling_fit(fit, methods=methods)
            figure.axes[0].set_title(title + "\n" + figure.axes[0].get_title())
            figures[f"{role}_h{harmonic}_fits"] = figure
        figures[f"{role}_h{harmonic}_report"] = plot_experiment_fit_report(
            rows, role=role, harmonic=harmonic, fit=fit, methods=methods,
            excitation_x_axis="excitation_current_rms_a", title=title)
    scalar_channels = tuple(k for k, fit in fits.items() if fit.scalar_selected_free_model is not None)
    if scalar_channels:
        figures["merged_scalar_report"] = plot_condensed_iv_report(
            fits, amplitude_channels=scalar_channels, title=rows[0].run_id + " · " + group)
    manifest = {"snapshot": snapshot.metadata, "group": group,
        "excluded_conditions": sorted(excluded_conditions), "metrics": list(metrics),
        "rules": asdict(resolved_rules), "methods": list(methods),
        "fit_row_count": len(fit_rows), "display_row_count": len(rows),
        "fits": {f"{role}_h{harmonic}": fit.as_dict() for (role, harmonic), fit in fits.items()},
        "rows": [asdict(row) for row in rows]}
    return {"rows": rows, "fits": fits, "figures": figures, "manifest": manifest}


def build_6221_panel(database, *, output_directory, rules=None, methods=("log", "scalar", "complex")):
    """Construct controls; only an explicit Refresh/Load reads the database."""
    import ipywidgets as widgets
    import matplotlib.pyplot as plt
    from IPython.display import display

    path = widgets.Text(value=str(database), description="SQLite:", layout=widgets.Layout(width="100%"))
    runs = widgets.Dropdown(options=(), description="Run ID:", layout=widgets.Layout(width="100%"))
    groups = widgets.Dropdown(options=(), description="Curve group:", layout=widgets.Layout(width="100%"))
    refresh = widgets.Button(description="Refresh SQLite runs")
    load = widgets.Button(description="Load selected run", button_style="success")
    preview = widgets.Checkbox(value=False, description="Accepted active-run preview")
    rejected = widgets.Checkbox(value=False, description="Include rejected (audit)")
    problem = widgets.Checkbox(value=False, description="Include flagged channels (audit)")
    exclude = widgets.SelectMultiple(options=(), description="Exclude points:", layout=widgets.Layout(width="100%"))
    metrics = widgets.SelectMultiple(options=(("R", "amplitude_v"), ("X", "x_v"), ("Y", "y_v"), ("Phase", "phase_deg")),
                                    value=("amplitude_v",), description="Plots:")
    apply = widgets.Button(description="Apply selection and plot", button_style="info")
    export = widgets.Button(description="Export displayed result")
    message = widgets.HTML()
    output = widgets.Output()
    state = {"snapshot": None, "report": None}

    def clear_report():
        if state["report"] is not None:
            for figure in state["report"]["figures"].values():
                plt.close(figure)
        state["report"] = None
        output.clear_output(wait=False)

    def clear_snapshot(_=None):
        clear_report()
        state["snapshot"] = None
        groups.options = ()
        exclude.options = ()
        message.value = "Load the selected run to obtain a new read-only snapshot."

    def refresh_runs(_=None):
        clear_snapshot()
        runs.options = ()
        try:
            records = list_6221_runs(path.value)
            runs.options = [(f"{r['run_id']} · {r['status']}", r["run_id"]) for r in records]
            runs.value = None
            message.value = f"{len(records)} 6221 runs. Select a Run ID, then Load selected run."
        except Exception as exc:
            message.value = html.escape(str(exc))

    def select_group(_=None):
        clear_report()
        snapshot = state["snapshot"]
        if snapshot is None or groups.value not in snapshot.groups:
            exclude.options = ()
            return
        conditions = {r.condition_id: r for r in snapshot.groups[groups.value]}
        exclude.options = [(f"#{r.point_index} · {r.requested_current_rms_a * 1e9:g} nA RMS requested", key)
                           for key, r in conditions.items()]
        exclude.value = ()

    def load_run(_=None):
        clear_snapshot()
        try:
            if not runs.value:
                raise ValueError("Select an explicit Run ID first.")
            snapshot = load_6221_sweep(path.value, runs.value, preview=preview.value,
                include_rejected=rejected.value, include_problem=problem.value)
            state["snapshot"] = snapshot
            options = []
            for key, rows in snapshot.groups.items():
                binding = snapshot.metadata["group_bindings"][key]
                outer = json.dumps(binding["outer_visit"], ensure_ascii=False)
                options.append((f"{binding['requested_frequency_hz']:g} Hz · {len(rows)} channel rows · "
                                f"{outer} · {key}", key))
            groups.options = options
            # ipywidgets versions differ on auto-selection after empty options.
            groups.value = options[0][1] if options else None
            select_group()
            message.value = (f"Loaded {snapshot.metadata['selected_channel_rows']} channel rows; "
                f"excluded {len(snapshot.metadata['quality_exclusions'])} flagged channels. "
                "Current = archived 6221 RMS setting readback (not measured current). "
                "Apply selection and plot uses this snapshot; Reload reads new data.")
        except Exception as exc:
            clear_snapshot()
            message.value = html.escape(str(exc))

    def render(_=None):
        clear_report()
        try:
            if state["snapshot"] is None or groups.value is None:
                raise ValueError("Load a run with eligible formal rows first.")
            report = current_sweep_report(state["snapshot"], groups.value,
                excluded_conditions=exclude.value, metrics=metrics.value, rules=rules, methods=methods)
            state["report"] = report
            with output:
                for (role, harmonic), fit in report["fits"].items():
                    print(f"{role} h{harmonic}: {fit.fit_point_count} points; "
                          f"log={fit.amplitude_verdict}; scalar={fit.scalar_power_law_verdict}; "
                          f"complex={fit.complex_power_law_verdict}")
                    for reason in dict.fromkeys((*fit.reasons, *fit.scalar_reasons, *fit.complex_reasons)):
                        print("  " + reason)
                for figure in report["figures"].values():
                    display(figure)
                    plt.close(figure)
        except Exception as exc:
            clear_report()
            message.value = html.escape(str(exc))

    def export_report(_=None):
        try:
            report = state["report"]
            if report is None:
                raise ValueError("Apply selection and plot before exporting.")
            directory = Path(output_directory) / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            directory.mkdir(parents=True, exist_ok=False)
            export_commissioning_csv(report["rows"], directory / "samples.csv")
            (directory / "manifest.json").write_text(json.dumps(report["manifest"], indent=2, ensure_ascii=False,
                                                               allow_nan=False), encoding="utf-8")
            for name, figure in report["figures"].items():
                for extension in ("png", "pdf", "svg"):
                    figure.savefig(directory / f"{name}.{extension}", dpi=200, bbox_inches="tight")
            message.value = "Exported displayed snapshot to " + html.escape(str(directory))
        except Exception as exc:
            message.value = html.escape(str(exc))

    refresh.on_click(refresh_runs)
    load.on_click(load_run)
    apply.on_click(render)
    export.on_click(export_report)
    groups.observe(select_group, names="value")
    for control in (path, runs, preview, rejected, problem):
        control.observe(clear_snapshot, names="value")
    for control in (exclude, metrics):
        control.observe(lambda _: clear_report(), names="value")
    panel = widgets.VBox([widgets.HTML("<h3>6221 SQLite · RMS current sweeps</h3>"), path,
        widgets.HBox([refresh, load]), runs, widgets.HBox([preview, rejected, problem]), groups,
        exclude, metrics, widgets.HBox([apply, export]), message, output])
    # Named controls support offline callback tests without a browser/kernel.
    panel.analysis_controls = {"path": path, "runs": runs, "groups": groups, "refresh": refresh,
        "load": load, "apply": apply, "exclude": exclude, "state": state, "message": message,
        "export": export, "metrics": metrics, "preview": preview, "rejected": rejected, "problem": problem}
    return panel
