"""SR865A physical-unit tables, from the SRS manual revision 2.11, pp. 110–114.

These are hardware capabilities, not approved sample/excitation limits.
"""
from __future__ import annotations

import math


MINIMUM_REFERENCE_FREQUENCY_HZ = 0.001
MAXIMUM_REFERENCE_FREQUENCY_HZ = 4_000_000.0
MAXIMUM_HARMONIC = 99
MINIMUM_SINE_OUTPUT_V = 1e-9
MAXIMUM_SINE_OUTPUT_V = 2.0

TIME_CONSTANTS_S = (
    1e-6, 3e-6, 10e-6, 30e-6, 100e-6, 300e-6,
    1e-3, 3e-3, 10e-3, 30e-3, 100e-3, 300e-3,
    1.0, 3.0, 10.0, 30.0, 100.0, 300.0, 1000.0, 3000.0,
    10000.0, 30000.0,
)
SENSITIVITIES_V = (
    1.0, 0.5, 0.2, 0.1, 0.05, 0.02, 0.01, 0.005, 0.002,
    0.001, 0.0005, 0.0002, 0.0001, 50e-6, 20e-6, 10e-6,
    5e-6, 2e-6, 1e-6, 500e-9, 200e-9, 100e-9, 50e-9, 20e-9,
    10e-9, 5e-9, 2e-9, 1e-9,
)
INPUT_RANGES_V_PEAK = (1.0, 0.3, 0.1, 0.03, 0.01)
FILTER_SLOPES_DB_OCT = (6, 12, 18, 24)


def finite_number(value: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number, not a boolean.")
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite.")
    return float(value)


def _encode(value: float, table: tuple[float, ...], name: str) -> int:
    value = finite_number(value, name)
    try:
        return table.index(value)
    except ValueError as exc:
        raise ValueError(f"Unsupported SR865A {name}: {value!r}.") from exc


def _decode(code: int, table: tuple[float, ...], name: str) -> float:
    if type(code) is not int or not 0 <= code < len(table):
        raise ValueError(f"Invalid SR865A {name} code: {code!r}.")
    return table[code]


def time_constant_code(seconds: float) -> int:
    return _encode(seconds, TIME_CONSTANTS_S, "time constant")


def time_constant_seconds(code: int) -> float:
    return _decode(code, TIME_CONSTANTS_S, "time constant")


def sensitivity_code(full_scale_v: float) -> int:
    return _encode(full_scale_v, SENSITIVITIES_V, "voltage sensitivity")


def sensitivity_full_scale_v(code: int) -> float:
    return _decode(code, SENSITIVITIES_V, "voltage sensitivity")


def input_range_code(v_peak: float) -> int:
    return _encode(v_peak, INPUT_RANGES_V_PEAK, "input range (V peak)")


def input_range_v_peak(code: int) -> float:
    return _decode(code, INPUT_RANGES_V_PEAK, "input range")


def filter_slope_code(db_oct: int) -> int:
    return _encode(db_oct, FILTER_SLOPES_DB_OCT, "filter slope")


def filter_slope_db_oct(code: int) -> int:
    return int(_decode(code, FILTER_SLOPES_DB_OCT, "filter slope"))


def validate_harmonic_frequency(harmonic: int, reference_hz: float) -> float:
    if type(harmonic) is not int or not 1 <= harmonic <= MAXIMUM_HARMONIC:
        raise ValueError("SR865A harmonic must be an integer from 1 to 99.")
    reference_hz = finite_number(reference_hz, "reference frequency")
    if not MINIMUM_REFERENCE_FREQUENCY_HZ <= reference_hz <= MAXIMUM_REFERENCE_FREQUENCY_HZ:
        raise ValueError("SR865A reference frequency must be within 1 mHz–4 MHz.")
    detection_hz = harmonic * reference_hz
    if detection_hz >= MAXIMUM_REFERENCE_FREQUENCY_HZ:
        raise ValueError("SR865A harmonic detection requires n * f_ref < 4 MHz.")
    return detection_hz
