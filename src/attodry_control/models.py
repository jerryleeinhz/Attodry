from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math


class LockinRole(StrEnum):
    XX = "xx"
    XY = "xy"


@dataclass(frozen=True, slots=True)
class VectorField:
    """Magnetic-field components in the attoDRY controller X/Z coordinates."""

    bx_t: float
    bz_t: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.bx_t) or not math.isfinite(self.bz_t):
            raise ValueError("Magnetic-field components must be finite.")

    @property
    def magnitude_t(self) -> float:
        return math.hypot(self.bx_t, self.bz_t)

    @property
    def angle_deg_from_z(self) -> float:
        """Signed angle from +Z toward +X, in degrees."""

        return math.degrees(math.atan2(self.bx_t, self.bz_t))

    @classmethod
    def from_polar(cls, magnitude_t: float, angle_deg_from_z: float) -> VectorField:
        if not math.isfinite(magnitude_t) or magnitude_t < 0:
            raise ValueError("Magnetic-field magnitude must be finite and non-negative.")
        if not math.isfinite(angle_deg_from_z):
            raise ValueError("Magnetic-field angle must be finite.")
        angle_rad = math.radians(angle_deg_from_z)
        return cls(
            bx_t=magnitude_t * math.sin(angle_rad),
            bz_t=magnitude_t * math.cos(angle_rad),
        )


@dataclass(frozen=True, slots=True)
class CryostatState:
    sample_temperature_k: float
    user_temperature_k: float
    vti_temperature_k: float
    field: VectorField
    field_setpoint: VectorField
    temperature_control_enabled: bool
    field_control_enabled: bool
    error_code: int
    error_message: str = ""


@dataclass(frozen=True, slots=True)
class LockinReading:
    role: LockinRole
    harmonic: int
    x_v: float
    y_v: float
    amplitude_v: float
    phase_deg: float
    frequency_hz: float
    locked: bool
    overload: bool

    def __post_init__(self) -> None:
        if self.harmonic not in (1, 2, 3):
            raise ValueError("Only harmonics 1, 2, and 3 are supported.")


@dataclass(frozen=True, slots=True)
class GateState:
    role: str
    voltage_set_v: float
    voltage_read_v: float
    leakage_current_a: float | None
    compliance_a: float
    output_enabled: bool

