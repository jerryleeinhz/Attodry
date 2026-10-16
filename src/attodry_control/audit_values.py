"""JSON-safe raw values, independent of command lines and instrument adapters."""
import math


def audit_safe(value):
    if isinstance(value, float) and not math.isfinite(value):
        return {"invalid_float": repr(value)}
    if isinstance(value, dict):
        return {key: audit_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [audit_safe(item) for item in value]
    return value
