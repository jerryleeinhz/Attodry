"""Pure model-specific setting conversions for electrical lock-in sessions.

Native codes are never ordered across models. Compare physical full scales,
then translate the requested value with the selected instrument's table.
Imports are local to keep the SR830 driver/autorange dependency acyclic.
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class ElectricalSettingCodes:
    model: str
    reference_source: int
    external_reference_edge: int | None
    input_mode: int
    shield_grounding: int
    input_coupling: int
    time_constant: int
    filter_slope: int
    sensitivity: int


def receiver_invariant_targets(config):
    """Physical settings that remain fixed throughout an electrical XY run."""
    if model_name(config) != "SR865A" or config.sr865a is None:
        raise ValueError("Native receiver invariants require explicit SR865A config")
    return {
        "reference_source": "external",
        "external_reference_edge": config.external_reference_edge.value,
        "reference_input_impedance_ohm": config.sr865a.reference_input_impedance_ohm,
        "input_range_v_peak": config.sr865a.input_range_v_peak,
        "input_mode": config.input_mode.value,
        "shield_grounding": config.shield_grounding.value,
        "input_coupling": config.input_coupling.value,
        "filter_slope_db_oct": config.filter_slope_db_oct,
        "time_constant_s": config.time_constant_s,
        "advanced_filter": False, "synchronous_filter": False,
    }


def model_name(value) -> str:
    model = value if isinstance(value, str) else getattr(value, "model", "SR830")
    if model not in ("SR830", "SR865A"):
        raise ValueError(f"Unsupported lock-in model: {model!r}")
    return model


def sensitivity_code_for(value, full_scale_v: float) -> int:
    if model_name(value) == "SR865A":
        from .sr865a_settings import sensitivity_code
    else:
        from .sr830_settings import sensitivity_code
    return sensitivity_code(full_scale_v)


def sensitivity_full_scale_for(value, code: int) -> float:
    if model_name(value) == "SR865A":
        from .sr865a_settings import sensitivity_full_scale_v
    else:
        from .sr830_settings import sensitivity_full_scale_v
    return sensitivity_full_scale_v(code)


def time_constant_code_for(value, seconds: float) -> int:
    if model_name(value) == "SR865A":
        from .sr865a_settings import time_constant_code
        return time_constant_code(seconds)
    from .sr830_settings import time_constant_seconds
    for code in range(20):
        if time_constant_seconds(code) == seconds:
            return code
    raise ValueError(f"Unsupported SR830 time constant: {seconds}")


def time_constant_seconds_for(value, code: int) -> float:
    if model_name(value) == "SR865A":
        from .sr865a_settings import time_constant_seconds
    else:
        from .sr830_settings import time_constant_seconds
    return time_constant_seconds(code)


def reserve_code_for(config) -> int | None:
    if model_name(config) == "SR865A":
        if getattr(config, "reserve_mode", None) is not None:
            raise ValueError("SR865A has no SR830 Reserve setting")
        return None
    from .sr830 import RESERVE_MODE_CODES
    reserve = config.reserve_mode
    return RESERVE_MODE_CODES[getattr(reserve, "value", reserve)]


def harmonic_supported(value, frequency_hz: float, harmonic: int) -> bool:
    from .lockin_backend import capabilities_for
    try:
        capabilities_for(model_name(value)).validate_detection(frequency_hz, harmonic)
    except ValueError:
        return False
    return True
