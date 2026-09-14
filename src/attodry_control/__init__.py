"""Safety-first core models for the attoDRY transport-control project.

Public conveniences are resolved lazily so file-only utilities such as the
magnetic-field JSONL monitor do not import hardware-driver modules as a package
initialization side effect.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


__version__ = "0.1.0"

_EXPORTS = {
    "AcceptedTransportResult": ("records", "AcceptedTransportResult"),
    "AttemptRecord": ("records", "AttemptRecord"),
    "AttemptStatus": ("records", "AttemptStatus"),
    "AttoDryDriver": ("attodry", "AttoDryDriver"),
    "CleanupReport": ("cleanup", "CleanupReport"),
    "ConfigError": ("config", "ConfigError"),
    "ControlConfig": ("config", "ControlConfig"),
    "CryostatState": ("models", "CryostatState"),
    "DualSr830Controller": ("sr830", "DualSr830Controller"),
    "ExperimentCondition": ("records", "ExperimentCondition"),
    "ExecutionSummary": ("acquisition", "ExecutionSummary"),
    "GateState": ("models", "GateState"),
    "GateSafetyLimits": ("gates", "GateSafetyLimits"),
    "LockinReading": ("models", "LockinReading"),
    "LockinRole": ("models", "LockinRole"),
    "LinearGateRelation": ("transport", "LinearGateRelation"),
    "MagnetLimits": ("safety", "MagnetLimits"),
    "RawTransportReading": ("records", "RawTransportReading"),
    "RawStationSample": ("records", "RawStationSample"),
    "RunMode": ("config", "RunMode"),
    "RunMonitor": ("storage", "RunMonitor"),
    "RunStore": ("storage", "RunStore"),
    "SafetyViolation": ("safety", "SafetyViolation"),
    "SafeGateController": ("gates", "SafeGateController"),
    "SimulationStation": ("simulation", "SimulationStation"),
    "SimulationRunEngine": ("acquisition", "SimulationRunEngine"),
    "StabilityCriteria": ("stability", "StabilityCriteria"),
    "TemperatureInterruptPolicy": ("config", "TemperatureInterruptPolicy"),
    "TimedValue": ("stability", "TimedValue"),
    "VectorField": ("models", "VectorField"),
    "cleanup_after_failure": ("cleanup", "cleanup_after_failure"),
    "cleanup_after_normal_completion": (
        "cleanup",
        "cleanup_after_normal_completion",
    ),
    "evaluate_stability": ("stability", "evaluate_stability"),
    "load_config": ("config", "load_config"),
    "signed_resistance_ohm": ("transport", "signed_resistance_ohm"),
    "validate_vector_field": ("safety", "validate_vector_field"),
}

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(f".{module_name}", __name__), attribute_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
