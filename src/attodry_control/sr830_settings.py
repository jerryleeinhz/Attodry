from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ReferenceSource(StrEnum):
    INTERNAL = "internal"
    EXTERNAL_TTL = "external_ttl"


class ExternalReferenceEdge(StrEnum):
    RISING = "rising"


class InputMode(StrEnum):
    A_MINUS_B = "a_minus_b"


class ShieldGrounding(StrEnum):
    FLOAT = "float"


class InputCoupling(StrEnum):
    AC = "ac"


class SensitivityMode(StrEnum):
    FIXED = "fixed"
    BOUNDED_AUTO = "bounded_auto"


class ReserveMode(StrEnum):
    """SR830 dynamic-reserve operating modes exposed by ``RMOD``."""

    HIGH_RESERVE = "high_reserve"
    NORMAL = "normal"
    LOW_NOISE = "low_noise"


@dataclass(frozen=True, slots=True)
class Sr830SettingCodes:
    reference_source: int
    external_reference_edge: int | None
    input_mode: int
    shield_grounding: int
    input_coupling: int
    time_constant: int
    filter_slope: int
    sensitivity: int


_REFERENCE_SOURCE_CODES = {
    ReferenceSource.INTERNAL: 1,
    ReferenceSource.EXTERNAL_TTL: 0,
}
_EXTERNAL_REFERENCE_EDGE_CODES = {ExternalReferenceEdge.RISING: 1}
_INPUT_MODE_CODES = {InputMode.A_MINUS_B: 1}
_SHIELD_GROUNDING_CODES = {ShieldGrounding.FLOAT: 0}
_INPUT_COUPLING_CODES = {InputCoupling.AC: 0}

# Complete SR830 OFLT mapping.  These are discrete hardware settings; callers
# must never substitute an arbitrary time constant between adjacent values.
_TIME_CONSTANT_CODES = {
    10e-6: 0,
    30e-6: 1,
    100e-6: 2,
    300e-6: 3,
    1e-3: 4,
    3e-3: 5,
    10e-3: 6,
    30e-3: 7,
    100e-3: 8,
    300e-3: 9,
    1.0: 10,
    3.0: 11,
    10.0: 12,
    30.0: 13,
    100.0: 14,
    300.0: 15,
    1_000.0: 16,
    3_000.0: 17,
    10_000.0: 18,
    30_000.0: 19,
}
_FILTER_SLOPE_CODES = {24: 3}

# Complete SR830 voltage-input full-scale mapping. Project policy decides which
# hardware-supported values a particular sweep may use.
_SENSITIVITY_CODES = {
    2e-9: 0,
    5e-9: 1,
    1e-8: 2,
    2e-8: 3,
    5e-8: 4,
    1e-7: 5,
    2e-7: 6,
    5e-7: 7,
    1e-6: 8,
    2e-6: 9,
    5e-6: 10,
    1e-5: 11,
    2e-5: 12,
    5e-5: 13,
    1e-4: 14,
    2e-4: 15,
    5e-4: 16,
    1e-3: 17,
    2e-3: 18,
    5e-3: 19,
    1e-2: 20,
    2e-2: 21,
    5e-2: 22,
    1e-1: 23,
    2e-1: 24,
    5e-1: 25,
    1.0: 26,
}


def sensitivity_code(full_scale_v: float) -> int:
    try:
        return _SENSITIVITY_CODES[full_scale_v]
    except KeyError as exc:
        raise ValueError(
            "sensitivity full scale is not an SR830 voltage-input full scale"
        ) from exc


def sensitivity_full_scale_v(code: int) -> float:
    for full_scale_v, mapped_code in _SENSITIVITY_CODES.items():
        if mapped_code == code:
            return full_scale_v
    raise ValueError("sensitivity code is outside the SR830 voltage-input range")


def map_sr830_settings(
    *,
    reference_source: ReferenceSource,
    external_reference_edge: ExternalReferenceEdge | None,
    input_mode: InputMode,
    shield_grounding: ShieldGrounding,
    input_coupling: InputCoupling,
    time_constant_s: float,
    filter_slope_db_oct: int,
    sensitivity_full_scale_v: float,
) -> Sr830SettingCodes:
    if reference_source is ReferenceSource.EXTERNAL_TTL:
        if external_reference_edge is None:
            raise ValueError("external TTL reference requires an edge")
        edge_code = _EXTERNAL_REFERENCE_EDGE_CODES[external_reference_edge]
    elif external_reference_edge is not None:
        raise ValueError("internal reference must not define an external edge")
    else:
        edge_code = None

    try:
        time_constant_code = _TIME_CONSTANT_CODES[time_constant_s]
    except KeyError as exc:
        raise ValueError(
            "time_constant_s must be a discrete SR830 OFLT value from "
            "0.00001 to 30000 s"
        ) from exc
    try:
        filter_slope_code = _FILTER_SLOPE_CODES[filter_slope_db_oct]
    except KeyError as exc:
        raise ValueError("filter_slope_db_oct must be the confirmed 24 dB/oct") from exc
    try:
        sensitivity_code_value = sensitivity_code(sensitivity_full_scale_v)
    except ValueError as exc:
        raise ValueError("unsupported sensitivity_full_scale_v") from exc

    return Sr830SettingCodes(
        reference_source=_REFERENCE_SOURCE_CODES[reference_source],
        external_reference_edge=edge_code,
        input_mode=_INPUT_MODE_CODES[input_mode],
        shield_grounding=_SHIELD_GROUNDING_CODES[shield_grounding],
        input_coupling=_INPUT_COUPLING_CODES[input_coupling],
        time_constant=time_constant_code,
        filter_slope=filter_slope_code,
        sensitivity=sensitivity_code_value,
    )
