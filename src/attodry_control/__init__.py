"""Public core API, loaded lazily so file-only tools never import drivers."""
from importlib import import_module

_EXPORTS = {
    'AcceptedTransportResult': ('records', 'AcceptedTransportResult'),
    'AttemptRecord': ('records', 'AttemptRecord'),
    'AttemptStatus': ('records', 'AttemptStatus'),
    'AttoDryDriver': ('attodry', 'AttoDryDriver'),
    'CleanupReport': ('cleanup', 'CleanupReport'),
    'ConfigError': ('config', 'ConfigError'),
    'ControlConfig': ('config', 'ControlConfig'),
    'CryostatState': ('models', 'CryostatState'),
    'DualSr830Controller': ('sr830', 'DualSr830Controller'),
    'ExperimentCondition': ('records', 'ExperimentCondition'),
    'ExecutionSummary': ('acquisition', 'ExecutionSummary'),
    'GateState': ('models', 'GateState'),
    'GateSafetyLimits': ('gates', 'GateSafetyLimits'),
    'LockinReading': ('models', 'LockinReading'),
    'LockinRole': ('models', 'LockinRole'),
    'LinearGateRelation': ('transport', 'LinearGateRelation'),
    'MagnetLimits': ('safety', 'MagnetLimits'),
    'RawTransportReading': ('records', 'RawTransportReading'),
    'RawStationSample': ('records', 'RawStationSample'),
    'RunMode': ('config', 'RunMode'),
    'RunMonitor': ('storage', 'RunMonitor'),
    'RunStore': ('storage', 'RunStore'),
    'SafetyViolation': ('safety', 'SafetyViolation'),
    'SafeGateController': ('gates', 'SafeGateController'),
    'SimulationStation': ('simulation', 'SimulationStation'),
    'SimulationRunEngine': ('acquisition', 'SimulationRunEngine'),
    'StabilityCriteria': ('stability', 'StabilityCriteria'),
    'TemperatureInterruptPolicy': ('config', 'TemperatureInterruptPolicy'),
    'TimedValue': ('stability', 'TimedValue'),
    'VectorField': ('models', 'VectorField'),
    'cleanup_after_failure': ('cleanup', 'cleanup_after_failure'),
    'cleanup_after_normal_completion': ('cleanup', 'cleanup_after_normal_completion'),
    'evaluate_stability': ('stability', 'evaluate_stability'),
    'load_config': ('config', 'load_config'),
    'signed_resistance_ohm': ('transport', 'signed_resistance_ohm'),
    'validate_vector_field': ('safety', 'validate_vector_field'),
}

__all__ = list(_EXPORTS)
__version__ = "0.1.0"


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module, attribute = _EXPORTS[name]
    value = getattr(import_module("." + module, __name__), attribute)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))
