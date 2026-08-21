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

    def __post_init__(self) -> None:
        temperatures = (
            self.sample_temperature_k,
            self.user_temperature_k,
            self.vti_temperature_k,
        )
        if any(not math.isfinite(value) or value <= 0 for value in temperatures):
            raise ValueError("Cryostat temperatures must be finite and positive.")
        if isinstance(self.error_code, bool) or not isinstance(self.error_code, int):
            raise ValueError("Cryostat error_code must be an integer.")


@dataclass(frozen=True, slots=True)
class LockinReading:
    role: LockinRole
    harmonic: int
    x_v: float
    y_v: float
    amplitude_v: float
    phase_deg: float
    phase_shift_deg: float
    frequency_hz: float
    locked: bool
    overload: bool

    def __post_init__(self) -> None:
        if self.harmonic not in (1, 2, 3):
            raise ValueError("Only harmonics 1, 2, and 3 are supported.")
        values = (
            self.x_v,
            self.y_v,
            self.amplitude_v,
            self.phase_deg,
            self.phase_shift_deg,
            self.frequency_hz,
        )
        if any(not math.isfinite(value) for value in values):
            raise ValueError("Lock-in readings must be finite.")
        if self.amplitude_v < 0:
            raise ValueError("Lock-in amplitude must be non-negative.")
        if self.frequency_hz <= 0:
            raise ValueError("Lock-in frequency must be positive.")


@dataclass(frozen=True, slots=True)
class GateState:
    role: str
    voltage_set_v: float
    voltage_read_v: float
    leakage_current_a: float | None
    compliance_a: float
    output_enabled: bool

    def __post_init__(self) -> None:
        if not self.role.strip():
            raise ValueError("Gate role must be non-empty.")
        values = (self.voltage_set_v, self.voltage_read_v, self.compliance_a)
        if any(not math.isfinite(value) for value in values):
            raise ValueError("Gate state values must be finite.")
        if self.compliance_a <= 0:
            raise ValueError("Gate compliance must be positive.")
        if self.leakage_current_a is not None and not math.isfinite(
            self.leakage_current_a
        ):
            raise ValueError("Gate leakage current must be finite when present.")
