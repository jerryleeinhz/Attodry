"""Typed, audited reference transients; recovery remains an explicit caller policy."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from enum import Enum


def _json_evidence(value):
    """Keep raw evidence serializable at every event boundary, including driver I/O."""
    if is_dataclass(value):
        return _json_evidence(asdict(value))
    if isinstance(value, Enum):
        return _json_evidence(value.value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json_evidence(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_evidence(item) for item in value]
    # Preserve non-finite numbers rather than hiding them. Strict JSON encoding
    # will reject malformed evidence instead of replacing it with a valid value.
    return value


class ReferenceTransientError(ValueError):
    """A known reference observation may be requalified without changing settings.

    This class does not perform retries or authorize acquisition. Communication,
    malformed/unknown status and unsupported instrument capabilities must retain
    their original hard-error types instead of being wrapped as transients.
    """
    def __init__(self, message, *, kind, evidence):
        super().__init__(message)
        self.kind = kind
        self.evidence = _json_evidence(evidence)
