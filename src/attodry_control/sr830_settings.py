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

# L1 intentionally exposes only the fixed settings confirmed for this project.
# Additional hardware-supported values belong here only after their project use is
# specified and tested; accepting a physical value is not write authorization.
_TIME_CONSTANT_CODES = {0.3: 9}
_FILTER_SLOPE_CODES = {24: 3}
_SENSITIVITY_CODES = {0.001: 17}


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
        raise ValueError("time_constant_s must be the confirmed 0.3 s") from exc
    try:
        filter_slope_code = _FILTER_SLOPE_CODES[filter_slope_db_oct]
    except KeyError as exc:
        raise ValueError("filter_slope_db_oct must be the confirmed 24 dB/oct") from exc
    try:
        sensitivity_code = _SENSITIVITY_CODES[sensitivity_full_scale_v]
    except KeyError as exc:
        raise ValueError(
            "sensitivity_full_scale_v must be the confirmed 0.001 V"
        ) from exc

    return Sr830SettingCodes(
        reference_source=_REFERENCE_SOURCE_CODES[reference_source],
        external_reference_edge=edge_code,
        input_mode=_INPUT_MODE_CODES[input_mode],
        shield_grounding=_SHIELD_GROUNDING_CODES[shield_grounding],
        input_coupling=_INPUT_COUPLING_CODES[input_coupling],
        time_constant=time_constant_code,
        filter_slope=filter_slope_code,
        sensitivity=sensitivity_code,
    )
