"""File-only plots for noncoherent PEM diagnostics; no hardware imports or I/O."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path


def _number(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


def _libraries():
    try:
        import numpy as np
        import matplotlib
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_agg import FigureCanvasAgg
    except ImportError as exc:
        raise ValueError("Diagnostic plotting requires numpy and matplotlib; install the analysis dependencies.") from exc
    return np, matplotlib, Figure, FigureCanvasAgg


def _quality(entry, run_reasons):
    reasons = list(run_reasons) + list(entry.get("exclusion_reasons") or ())
    analysis = entry.get("analysis") or {}
    reasons.extend(analysis.get("exclusion_reasons") or ())
    if analysis.get("valid_for_diagnostic_analysis") is not True:
        reasons.append("analysis_not_qualified")
    if (entry.get("capture") or {}).get("completed") is not True:
        reasons.append("capture_not_completed")
    return list(dict.fromkeys(str(item) for item in reasons))


def _arrays(entry, np):
    capture = entry.get("capture") or {}
    rate = capture.get("actual_rate_hz")
    if not _number(rate) or rate <= 0:
        raise ValueError("missing_or_invalid_actual_rate_hz")
    values = []
    for key in ("x_v", "y_v"):
        raw = np.asarray(capture.get(key))
        if raw.ndim != 1 or raw.dtype.kind not in "iuf" or len(raw) < 2:
            raise ValueError("missing_or_invalid_xy_arrays")
        values.append(raw.astype(float, copy=False))
    x, y = values
    if len(x) != len(y):
        raise ValueError("xy_length_mismatch")
    if "t_s" in capture:
        raw_time = np.asarray(capture["t_s"])
        if raw_time.ndim != 1 or raw_time.dtype.kind not in "iuf" or len(raw_time) != len(x):
            raise ValueError("missing_or_invalid_time_axis")
        time = raw_time.astype(float, copy=False)
        if not np.all(np.isfinite(time)) or not np.all(np.diff(time) > 0):
            raise ValueError("nonfinite_or_nonincreasing_time_axis")
        if not np.allclose(np.diff(time), 1 / rate, rtol=1e-6, atol=1e-10):
            raise ValueError("time_axis_disagrees_with_actual_rate")
        time_source = "archived capture.t_s"
    else:
        time = np.arange(len(x)) / rate
        time_source = "sample_index / capture.actual_rate_hz"
    finite = bool(np.all(np.isfinite(x)) and np.all(np.isfinite(y)))
    # Gaps remain in the time plot. An FFT of an incomplete/nonfinite record is
    # omitted rather than zero-filled or computed on a silently shortened trace.
    with np.errstate(invalid="ignore", over="ignore"):
        r = np.hypot(x, y)
    finite = finite and bool(np.all(np.isfinite(r)))
    return time, x, y, r, float(rate), time_source, finite


def _save(figure, output, stem, files, record):
    for extension in ("png", "pdf"):
        path = output / f"{stem}.{extension}"
        if path.exists():
            raise ValueError(f"Refusing to overwrite plot: {path}")
        figure.savefig(path, dpi=300, facecolor="white", transparent=False)
        files.append(path)
        record.setdefault("files", []).append(str(path))
    figure.clear()


def _capture_figure(entries, key, output, sequence, libraries, files, context):
    np, _, Figure, Canvas = libraries
    point, frequency, tau, harmonic = key
    figure = Figure(figsize=(11, 7.5), layout="constrained")
    Canvas(figure)
    axes = figure.subplots(2, 2)
    origin = "SYNTHETIC | " if context["simulated"] else ""
    figure.suptitle(f"{origin}Noncoherent PEM diagnostic | optical point {point}\n"
                   f"Internal {frequency:.12g} Hz | h{harmonic} | RC 24 dB/oct | tau {tau:g} s", fontsize=13)
    dark_axes, light_axes = axes[0]
    amplitude_axes, spectrum_axes = axes[1]
    dark_axes.set(title="Dark: raw quadratures", xlabel="Capture time (s)", ylabel="X, Y (uV)")
    light_axes.set(title="Light: raw quadratures", xlabel="Capture time (s)", ylabel="X, Y (uV)")
    amplitude_axes.set(title="Per-sample R = hypot(X, Y)", xlabel="Capture time (s)", ylabel="R (uV)")
    spectrum_axes.set(title="Complex FFT; DC retained", xlabel="Signed baseband frequency (Hz)",
                      ylabel="|FFT(X + iY)| / N (uV)")
    record = {"kind": "capture_comparison", "optical_point_index": point,
              "internal_frequency_hz": frequency, "time_constant_s": tau,
              "harmonic": harmonic, "captures": [], **context,
              "transformations": ["voltage displayed in uV", "R = hypot(X,Y) at each sample",
                                  "complex FFT magnitude / sample count; no window, smoothing, mean removal or inverse filter"],
              "alt_text": "Dark and illuminated raw X/Y, per-sample R, and signed complex FFT for one diagnostic setting; invalid records are labelled."}
    footer = []
    present = set()
    for index, entry in enumerate(entries):
        illumination = entry.get("illumination")
        present.add(illumination)
        axis = dark_axes if illumination == "dark" else light_axes
        reasons = _quality(entry, context["run_exclusion_reasons"])
        item = {"capture_id": entry.get("capture_id"), "illumination": illumination,
                "exclusion_reasons": reasons}
        record["captures"].append(item)
        try:
            time, x, y, r, rate, time_source, finite = _arrays(entry, np)
        except (TypeError, ValueError) as exc:
            reasons.append(str(exc))
            footer.append(f"{illumination}: INVALID ({'; '.join(reasons)})")
            axis.text(0.5, 0.5, "INVALID: capture arrays unavailable\nSee manifest for recorded reasons",
                      transform=axis.transAxes, ha="center", va="center")
            continue
        if not finite:
            reasons.append("nonfinite_raw_samples; FFT omitted")
        prefix = f"{illumination} {entry.get('capture_id', index)}"
        suffix = " [INVALID]" if reasons else " [SYNTHETIC]" if context["simulated"] else " [diagnostic]"
        color = "#0072B2" if illumination == "dark" else "#D55E00"
        line_style = "--" if illumination == "dark" else "-"
        axis.plot(time, x * 1e6, color=color, linestyle="-", linewidth=0.7, label=f"X{suffix}")
        axis.plot(time, y * 1e6, color="#666666", linestyle="--", linewidth=0.7, label=f"Y{suffix}")
        amplitude_axes.plot(time, r * 1e6, color=color, linestyle=line_style, linewidth=0.8, label=prefix + suffix)
        item.update(sample_count=len(x), actual_rate_hz=rate, time_axis_source=time_source)
        if finite:
            frequencies = np.fft.fftshift(np.fft.fftfreq(len(x), d=1 / rate))
            spectrum = np.abs(np.fft.fftshift(np.fft.fft(x + 1j * y))) / len(x)
            spectrum_axes.plot(frequencies, spectrum * 1e6, color=color,
                               linestyle=line_style, linewidth=0.8, label=prefix + suffix)
        analysis = entry.get("analysis") or {}
        expected = analysis.get("expected_signed_beat_hz")
        pem = entry.get("pem_frequency_hz")
        if not _number(expected) and _number(pem):
            expected = harmonic * (pem - frequency)
        if _number(expected):
            candidates = sorted({-float(expected), float(expected)})
            item["expected_beat_candidates_hz"] = candidates
            for candidate in candidates:
                spectrum_axes.axvline(candidate, color=color, linestyle=":", linewidth=0.8,
                                      label=f"Expected {candidate:+.4g} Hz ({illumination})")
        else:
            item["expected_beat_candidates_hz"] = None
        footer.append(f"{illumination}: fs={rate:g} Hz, n={len(x)}" +
                      (f"; INVALID ({'; '.join(reasons)})" if reasons else "; synthetic" if context["simulated"] else "; diagnostic only"))
    for illumination, axis in (("dark", dark_axes), ("light", light_axes)):
        if illumination not in present:
            axis.text(0.5, 0.5, f"No {illumination} capture recorded", transform=axis.transAxes, ha="center")
    for axis in axes.flat:
        axis.grid(alpha=0.2, linewidth=0.5)
        if axis.get_legend_handles_labels()[0]:
            axis.legend(fontsize=7, loc="best")
    spectrum_axes.set_ylim(bottom=0)
    amplitude_axes.set_ylim(bottom=0)
    overall = ("SYNTHETIC: no hardware acquisition" if context["simulated"] else
               f"Run {context['overall_status']}; final cleanup verified={context['cleanup_verified']}")
    figure.supxlabel(overall + "\n" + "\n".join(footer) + "\nPEM phase unavailable; no signed Hall inference. R has positive noise bias.", fontsize=8)
    _save(figure, output, f"capture-comparison-{sequence:03d}", files, record)
    return record


def _frequency_figure(observation, output, sequence, libraries, files, context):
    np, _, Figure, Canvas = libraries
    samples = observation.get("samples")
    if not isinstance(samples, list):
        raise ValueError("Frequency observation samples must be a list.")
    figure = Figure(figsize=(10, 4.8), layout="constrained")
    Canvas(figure)
    time_axis, counts_axis = figure.subplots(1, 2)
    point = observation.get("optical_point_index")
    origin = "SYNTHETIC | " if context["simulated"] else ""
    figure.suptitle(f"{origin}PEM frequency readbacks | optical point {point} | {len(samples)} observations")
    frequency = []
    times = []
    invalid = []
    stable_flags = []
    for index, sample in enumerate(samples):
        if not isinstance(sample, dict):
            sample = {}
        value, elapsed = sample.get("frequency_hz"), sample.get("elapsed_s")
        valid_frequency = _number(value)
        valid_time = _number(elapsed)
        frequency.append(float(value) if valid_frequency else np.nan)
        times.append(float(elapsed) if valid_time else np.nan)
        if not valid_frequency or not valid_time:
            invalid.append(index)
        stable_flags.append(sample.get("stable"))
    values = np.asarray(frequency)
    time = np.asarray(times)
    valid_values = values[np.isfinite(values)]
    record = {"kind": "pem_frequency", "optical_point_index": point, **context,
              "raw_observation_count": len(samples), "invalid_observation_indices": invalid,
              "transformations": ["frequency minus first finite readback for time plot", "exact unique finite readback values and integer counts; no histogram bins"],
              "alt_text": "PEM frequency deviation versus elapsed time and counts at exact reported frequency values; stability and missing observations remain explicit."}
    if len(valid_values):
        baseline = float(valid_values[0])
        time_axis.plot(time, values - baseline, color="#0072B2", marker="o", markersize=3, linewidth=0.7)
        time_axis.set(xlabel="Elapsed time (s)", ylabel=f"Readback frequency - {baseline} Hz (Hz)")
        time_axis.ticklabel_format(axis="y", style="plain", useOffset=False)
        unique, counts = np.unique(valid_values, return_counts=True)
        counts_axis.bar(np.arange(len(unique)), counts, color="#0072B2", edgecolor="black", linewidth=0.5)
        ticks = np.unique(np.linspace(0, len(unique) - 1, min(len(unique), 8), dtype=int))
        counts_axis.set_xticks(ticks, [str(float(unique[index])) for index in ticks], rotation=45 if len(unique) > 3 else 0)
        counts_axis.set(xlabel="Exact reported frequency value (Hz; categorical)", ylabel="Readback count")
        counts_axis.yaxis.get_major_locator().set_params(integer=True)
        record.update(baseline_frequency_hz=baseline, unique_frequencies_hz=unique.tolist(), counts=counts.tolist())
        time_axis.set_title("All finite readbacks identical" if len(unique) == 1 else "Frequency observations")
    else:
        time_axis.text(0.5, 0.5, "No finite frequency readbacks", transform=time_axis.transAxes, ha="center")
        counts_axis.text(0.5, 0.5, "No counts available", transform=counts_axis.transAxes, ha="center")
    if any(flag is False for flag in stable_flags):
        stable_note = "Controller STABLE=false recorded; stability is not established."
    elif stable_flags and all(flag is True for flag in stable_flags):
        stable_note = "Controller STABLE=true in every recorded observation."
    else:
        stable_note = "PEM stability not recorded for every observation."
    record["stability_note"] = stable_note
    figure.supxlabel(stable_note + "\nFrequency readback/display resolution is not inferred from counts or axis width."
                     + (f" Missing/invalid observations: {len(invalid)}." if invalid else ""), fontsize=8)
    time_axis.grid(alpha=0.2, linewidth=0.5)
    counts_axis.grid(axis="y", alpha=0.2, linewidth=0.5)
    _save(figure, output, f"pem-frequency-{sequence:03d}", files, record)
    return record


def plot_diagnostic(run_directory: str | Path) -> list[Path]:
    """Read a saved result and export PNG/PDF plus provenance without overwrite."""
    run_directory = Path(run_directory).resolve()
    source = run_directory / "result.json"
    payload = source.read_bytes()
    try:
        result = json.loads(payload)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("Diagnostic result.json must contain valid JSON.") from exc
    if (not isinstance(result, dict) or type(result.get("schema_version")) is not int
            or result.get("schema_version") != 1 or result.get("mode") != "pem_internal_diagnostic"):
        raise ValueError("Unsupported PEM diagnostic result schema or mode.")
    captures = result.get("captures", [])
    observations = result.get("frequency_observations", [])
    if not isinstance(captures, list) or not isinstance(observations, list):
        raise ValueError("Diagnostic captures and frequency_observations must be lists.")
    grouped = defaultdict(list)
    for entry in captures:
        if not isinstance(entry, dict) or entry.get("illumination") not in ("dark", "light"):
            raise ValueError("A capture must identify dark or light illumination.")
        key = (entry.get("optical_point_index"), entry.get("internal_frequency_hz"),
               entry.get("time_constant_s"), entry.get("harmonic"))
        if type(key[0]) is not int or key[0] < 0 or not _number(key[1]) or key[1] <= 0 or not _number(key[2]) or key[2] <= 0 or type(key[3]) is not int or key[3] < 1:
            raise ValueError("Capture grouping settings are missing or invalid.")
        grouped[key].append(entry)
    simulated = result.get("simulated") is True
    cleanup = result.get("cleanup") or {}
    run_reasons = []
    if result.get("status") != "completed":
        run_reasons.append("run_not_completed" if result.get("status") == "active" else "run_terminal_failed")
    if not simulated and cleanup.get("verified") is not True:
        run_reasons.append("final_cleanup_not_verified")
    context = {"overall_status": result.get("status"), "cleanup_verified": cleanup.get("verified"),
               "simulated": simulated, "run_exclusion_reasons": run_reasons}
    libraries = _libraries()
    np, matplotlib, _, _ = libraries
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = run_directory / f"plots-{stamp}"
    output.mkdir(exist_ok=False)
    files = []
    records = []
    with matplotlib.rc_context({"font.family": "DejaVu Sans", "font.size": 9,
                                "pdf.fonttype": 42, "ps.fonttype": 42,
                                "axes.titleweight": "normal", "savefig.facecolor": "white"}):
        for sequence, (key, entries) in enumerate(sorted(grouped.items())):
            records.append(_capture_figure(entries, key, output, sequence, libraries, files, context))
        for sequence, observation in enumerate(observations):
            if not isinstance(observation, dict):
                raise ValueError("Frequency observations must be mappings.")
            records.append(_frequency_figure(observation, output, sequence, libraries, files, context))
    manifest = {
        "schema_version": 1, "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_path": str(source), "source_sha256": hashlib.sha256(payload).hexdigest(),
        "output_directory": str(output), "numpy_version": np.__version__,
        "matplotlib_version": matplotlib.__version__, "formats": ["png", "pdf"], "png_dpi": 300,
        "smoothing": "none", "filter_correction": "none", "formal_hall_eligible": False,
        "figures": records, **context,
    }
    manifest_path = output / "manifest.json"
    with manifest_path.open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, indent=2, allow_nan=False)
    files.append(manifest_path)
    return files
