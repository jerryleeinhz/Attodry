from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
import math

from .models import CryostatState, GateState, LockinReading, LockinRole, VectorField
from .safety import validate_vector_field


class AttemptStatus(StrEnum):
    STARTED = "started"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class ExperimentCondition:
    condition_id: str
    sequence_index: int
    temperature_k: float
    field: VectorField
    excitation_v: float
    frequency_hz: float
    gate_top_v: float
    gate_bottom_v: float
    scan_id: str = "default"

    def __post_init__(self) -> None:
        _nonempty_id(self.condition_id, "condition_id")
        _nonempty_id(self.scan_id, "scan_id")
        if (
            isinstance(self.sequence_index, bool)
            or not isinstance(self.sequence_index, int)
            or self.sequence_index < 0
        ):
            raise ValueError("sequence_index must be a non-negative integer.")
        _positive_finite(self.temperature_k, "temperature_k")
        validate_vector_field(self.field)
        _nonnegative_finite(self.excitation_v, "excitation_v")
        _positive_finite(self.frequency_hz, "frequency_hz")
        _finite(self.gate_top_v, "gate_top_v")
        _finite(self.gate_bottom_v, "gate_bottom_v")


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    condition_id: str
    attempt_index: int
    started_at_utc: datetime
    completed_at_utc: datetime | None
    status: AttemptStatus
    rejection_reason: str | None = None

    def __post_init__(self) -> None:
        _attempt_key(self.condition_id, self.attempt_index)
        _utc_timestamp(self.started_at_utc, "started_at_utc")
        if self.status is AttemptStatus.STARTED:
            if self.completed_at_utc is not None:
                raise ValueError("A started attempt cannot have completed_at_utc.")
        else:
            if self.completed_at_utc is None:
                raise ValueError("A completed attempt requires completed_at_utc.")
            _utc_timestamp(self.completed_at_utc, "completed_at_utc")
            if self.completed_at_utc < self.started_at_utc:
                raise ValueError("completed_at_utc cannot precede started_at_utc.")
        if self.status is AttemptStatus.REJECTED:
            if not self.rejection_reason or not self.rejection_reason.strip():
                raise ValueError("A rejected attempt requires rejection_reason.")
        elif self.rejection_reason is not None:
            raise ValueError("rejection_reason is only valid for rejected attempts.")

    @property
    def accepted(self) -> bool:
        return self.status is AttemptStatus.ACCEPTED


@dataclass(frozen=True, slots=True)
class RawTransportReading:
    condition_id: str
    attempt_index: int
    captured_at_utc: datetime
    reading: LockinReading

    def __post_init__(self) -> None:
        _attempt_key(self.condition_id, self.attempt_index)
        _utc_timestamp(self.captured_at_utc, "captured_at_utc")


@dataclass(frozen=True, slots=True)
class RawStationSample:
    condition_id: str
    attempt_index: int
    captured_at_utc: datetime
    cryostat: CryostatState
    gate_top: GateState
    gate_bottom: GateState
    gate_top_leakage_limit_a: float
    gate_bottom_leakage_limit_a: float

    def __post_init__(self) -> None:
        _attempt_key(self.condition_id, self.attempt_index)
        _utc_timestamp(self.captured_at_utc, "captured_at_utc")
        if self.gate_top.role != "top" or self.gate_bottom.role != "bottom":
            raise ValueError("Station sample requires semantic top/bottom gate roles.")
        _positive_finite(
            self.gate_top_leakage_limit_a, "gate_top_leakage_limit_a"
        )
        _positive_finite(
            self.gate_bottom_leakage_limit_a, "gate_bottom_leakage_limit_a"
        )
        if self.gate_top_leakage_limit_a > self.gate_top.compliance_a:
            raise ValueError("Top-gate leakage limit cannot exceed compliance.")
        if self.gate_bottom_leakage_limit_a > self.gate_bottom.compliance_a:
            raise ValueError("Bottom-gate leakage limit cannot exceed compliance.")

    @property
    def safe_for_acceptance(self) -> bool:
        return (
            self.cryostat.error_code == 0
            and self.cryostat.temperature_control_enabled
            and self.cryostat.field_control_enabled
            and self.cryostat.field.magnitude_t <= 3.0
            and self.gate_top.output_enabled
            and self.gate_bottom.output_enabled
            and self.gate_top.leakage_current_a is not None
            and self.gate_bottom.leakage_current_a is not None
            and abs(self.gate_top.leakage_current_a)
            <= self.gate_top_leakage_limit_a
            and abs(self.gate_bottom.leakage_current_a)
            <= self.gate_bottom_leakage_limit_a
        )


@dataclass(frozen=True, slots=True)
class AcceptedTransportResult:
    condition_id: str
    attempt_index: int
    readings: tuple[RawTransportReading, ...]

    def __post_init__(self) -> None:
        _attempt_key(self.condition_id, self.attempt_index)
        keys: set[tuple[LockinRole, int]] = set()
        for raw in self.readings:
            if raw.condition_id != self.condition_id or raw.attempt_index != self.attempt_index:
                raise ValueError("Accepted readings must match the result attempt key.")
            if not raw.reading.locked:
                raise ValueError("An unlocked raw reading cannot be accepted.")
            if raw.reading.overload:
                raise ValueError("An overloaded raw reading cannot be accepted.")
            key = (raw.reading.role, raw.reading.harmonic)
            if key in keys:
                raise ValueError(f"Duplicate accepted reading for {key}.")
            keys.add(key)
        expected = {
            (role, harmonic)
            for role in (LockinRole.XX, LockinRole.XY)
            for harmonic in (1, 2, 3)
        }
        if keys != expected:
            missing = sorted(
                (f"{role.value}/h{harmonic}" for role, harmonic in expected - keys)
            )
            raise ValueError(
                "Accepted result requires xx/xy harmonics 1, 2, and 3; missing: "
                + ", ".join(missing)
                + "."
            )

    @property
    def accepted(self) -> bool:
        return True


def _attempt_key(condition_id: str, attempt_index: int) -> None:
    _nonempty_id(condition_id, "condition_id")
    if isinstance(attempt_index, bool) or not isinstance(attempt_index, int) or attempt_index < 0:
        raise ValueError("attempt_index must be a non-negative integer.")


def _nonempty_id(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string.")


def _utc_timestamp(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware UTC.")
    if value.utcoffset() != timedelta(0):
        raise ValueError(f"{name} must use UTC.")


def _finite(value: float, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite.")


def _positive_finite(value: float, name: str) -> None:
    _finite(value, name)
    if value <= 0:
        raise ValueError(f"{name} must be positive.")


def _nonnegative_finite(value: float, name: str) -> None:
    _finite(value, name)
    if value < 0:
        raise ValueError(f"{name} must be non-negative.")
