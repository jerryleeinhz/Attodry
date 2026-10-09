"""Offline amplitude and beat-frequency summaries for noncoherent PEM captures.

R is computed per sample before averaging. Independent internal references do
not supply PEM phase or a signed Hall coefficient. Noise gives R a positive
bias; no automatic background subtraction or inverse-filter correction occurs.
"""
from __future__ import annotations

import math
from typing import Mapping


def _finite(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number.")
    if positive and value <= 0:
        raise ValueError(f"{name} must be positive.")
    return float(value)


def rc_amplitude_retention(beat_frequency_hz, time_constant_s):
    """Four ordinary RC poles (24 dB/oct); not the SR865A Advanced filter."""
    beat = _finite(beat_frequency_hz, "beat_frequency_hz")
    tau = _finite(time_constant_s, "time_constant_s", positive=True)
    argument = 2 * math.pi * abs(beat) * tau
    if not math.isfinite(argument):
        return 0.0
    inverse = 1 / math.hypot(1, argument)
    return inverse ** 4


def summarize_capture(capture: dict, *, internal_frequency_hz, harmonic,
                      time_constant_s, pem_frequency_hz) -> dict:
    """Retain raw-quality exclusions while summarizing finite numeric samples.

    Input arrays are ``x_v``/``y_v`` with the actual hardware ``sample_rate_hz``.
    Missing hardware-quality metadata cannot certify a usable diagnostic.
    ``validity=True`` means the acquisition's complete guard window passed.
    """
    internal = _finite(internal_frequency_hz, "internal_frequency_hz", positive=True)
    tau = _finite(time_constant_s, "time_constant_s", positive=True)
    if type(harmonic) is not int or not 1 <= harmonic <= 99:
        raise ValueError("harmonic must be an integer from 1 to 99.")
    pem = None if pem_frequency_hz is None else _finite(pem_frequency_hz, "pem_frequency_hz", positive=True)
    if not isinstance(capture, Mapping):
        raise ValueError("capture must be a mapping.")
    reasons = []
    if capture.get("validity") is not True:
        reasons.append("recorded_invalid" if capture.get("validity") is False else "hardware_quality_not_recorded")
    for key in ("exclusion_reasons", "errors"):
        evidence = capture.get(key, ())
        if isinstance(evidence, (list, tuple)):
            reasons.extend(str(item) for item in evidence if item)
        elif evidence:
            reasons.append(str(evidence))
    signed_beat = None if pem is None else harmonic * (pem - internal)
    result = {
        "schema_version": "pem-internal-diagnostic-analysis-v1",
        "internal_frequency_hz": internal,
        "harmonic": harmonic,
        "time_constant_s": tau,
        "pem_frequency_hz": pem,
        "expected_signed_beat_hz": signed_beat,
        "expected_absolute_beat_hz": None if signed_beat is None else abs(signed_beat),
        "expected_beat_candidates_hz": None if signed_beat is None else sorted({-signed_beat, signed_beat}),
        "fft_sign_convention": "X + iY; physical beat sign depends on instrument quadrature convention",
        "ordinary_rc_amplitude_retention": None if signed_beat is None else rc_amplitude_retention(signed_beat, tau),
        "filter_correction_applied": False,
        "formal_hall_eligible": False,
        "pem_phase_available": False,
        "r_has_positive_noise_bias": True,
        "sample_rate_hz": None,
        "time_axis_source": capture.get("time_axis_source", "sample_index / hardware_sample_rate_hz"),
        "sample_count": 0,
        "duration_s": None,
        "x_mean_v": None, "y_mean_v": None,
        "x_sample_sd_v": None, "y_sample_sd_v": None,
        "r_mean_v": None, "r_sample_sd_v": None,
        "complex_mean_magnitude_v": None,
        "fft_peak_signed_hz": None, "fft_peak_absolute_hz": None,
        "fft_bin_width_hz": None,
        "valid_for_diagnostic_analysis": False,
        "exclusion_reasons": reasons,
    }
    try:
        rate = _finite(capture.get("sample_rate_hz"), "sample_rate_hz", positive=True)
    except ValueError:
        reasons.append("missing_or_invalid_actual_sample_rate")
        return result
    result["sample_rate_hz"] = rate
    if capture.get("time_axis_source") is not None and not isinstance(capture["time_axis_source"], str):
        reasons.append("invalid_time_axis_source")
    # Numpy is an optional analysis dependency, imported only for this operation.
    import numpy as np

    try:
        for key in ("x_v", "y_v"):
            if key not in capture or isinstance(capture[key], (str, bytes)):
                raise ValueError(key)
            if any(isinstance(item, bool) for item in capture[key]):
                raise ValueError(key)
        x = np.asarray(capture["x_v"])
        y = np.asarray(capture["y_v"])
        if x.dtype.kind not in "iuf" or y.dtype.kind not in "iuf":
            raise ValueError("non-real numeric arrays")
        if x.ndim != 1 or y.ndim != 1 or len(x) != len(y) or len(x) < 2:
            raise ValueError("shape")
        x = x.astype(float, copy=False)
        y = y.astype(float, copy=False)
        if not np.all(np.isfinite(x)) or not np.all(np.isfinite(y)):
            raise ValueError("nonfinite")
        r = np.hypot(x, y)
        if not np.all(np.isfinite(r)):
            raise ValueError("overflow")
    except (ValueError, TypeError, OverflowError):
        reasons.append("missing_or_invalid_capture_arrays")
        return result
    size = len(x)
    with np.errstate(over="ignore", invalid="ignore"):
        statistics = {
            "x_mean_v": float(np.mean(x)), "y_mean_v": float(np.mean(y)),
            "x_sample_sd_v": float(np.std(x, ddof=1)), "y_sample_sd_v": float(np.std(y, ddof=1)),
            "r_mean_v": float(np.mean(r)), "r_sample_sd_v": float(np.std(r, ddof=1)),
            "complex_mean_magnitude_v": float(np.hypot(np.mean(x), np.mean(y))),
        }
    if not all(math.isfinite(value) for value in statistics.values()):
        reasons.append("capture_statistics_overflow")
        return result
    result.update(sample_count=size, duration_s=(size - 1) / rate,
                  fft_bin_width_hz=rate / size, **statistics)
    if signed_beat is not None and abs(signed_beat) >= rate / 2:
        reasons.append("expected_beat_at_or_above_nyquist")
    # Include the DC bin: a near-matched reference or constant signal may peak
    # at zero. Do not invent a nonzero beat by subtracting the complex mean.
    values = x + 1j * y
    spectrum = np.abs(np.fft.fft(values))
    if np.any(spectrum):
        peak = float(np.fft.fftfreq(size, d=1 / rate)[int(np.argmax(spectrum))])
        result.update(fft_peak_signed_hz=peak, fft_peak_absolute_hz=abs(peak))
    result["exclusion_reasons"] = list(dict.fromkeys(reasons))
    result["valid_for_diagnostic_analysis"] = not result["exclusion_reasons"]
    return result
