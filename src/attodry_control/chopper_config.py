"""Hardware-free MC2000B configuration and manufacturer blade mappings."""
from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path

from .nkt_config import NktError, integer, keys, number, text


class ChopperError(NktError):
    pass


# Thorlabs TTN102010-D02, Rev B (2023), printed pp. 18-19, 24-25.
# index, inner slots, outer slots (0 for a single track), minimum/maximum rev/s.
BLADE_SPECS = {
    "MC1F2": (0, 2, 100), "MC1F10": (1, 10, 0),
    "MC1F15": (2, 15, 0), "MC1F30": (3, 30, 0),
    "MC1F60": (4, 60, 0), "MC1F100": (5, 100, 0),
    "MC1F10HP": (6, 10, 100), "MC1F2P10": (7, 2, 100),
    "MC1F6P10": (8, 6, 0), "MC1F10A": (9, 10, 0),
    "MC2F330": (10, 3, 30), "MC2F47": (11, 4, 7),
    "MC2F57B": (12, 5, 7), "MC2F860": (13, 8, 60),
    "MC2F5360": (14, 53, 60),
}
HIGH_PRECISION = {"MC1F2", "MC1F10HP", "MC1F2P10"}


@dataclass(frozen=True)
class ChopperConfig:
    backend: str = "manual"
    model: str = "MC2000B"
    port: str = ""
    expected_idn: str = ""
    blade: str = ""
    reference_source: str = "external"
    reference_track: str = "single"
    single_opening_per_rotation: bool = False
    harmonic_n: int = 1
    harmonic_d: int = 1
    phase_deg: float | None = None
    run_enabled: bool = True
    finish_rotation: str = "stop"
    frequency_tolerance_hz: float = 1.0
    io_timeout_s: float = 2.0
    serial_inter_character_delay_s: float = 0.0
    ready_timeout_s: float = 30.0
    poll_interval_s: float = 1.0
    consecutive_good: int = 3
    manual_phase_lock_confirmed: bool = False

    def validate(self):
        if self.backend not in ("simulation", "serial", "manual"):
            raise ChopperError("chopper.backend must be simulation, serial or manual")
        if self.model not in ("MC2000B", "MC2000B-EC"):
            raise ChopperError("Only confirmed MC2000B / MC2000B-EC protocol is supported")
        if self.blade not in BLADE_SPECS:
            raise ChopperError("chopper.blade must name the confirmed installed blade")
        if self.reference_source not in ("external", "internal"):
            raise ChopperError("chopper.reference_source must be external or internal")
        tracks = ("inner", "outer") if BLADE_SPECS[self.blade][2] else ("single",)
        if self.reference_track not in tracks:
            raise ChopperError(f"chopper.reference_track must be one of {tracks} for {self.blade}")
        integer(self.harmonic_n, "chopper.harmonic_n", 1, 15)
        integer(self.harmonic_d, "chopper.harmonic_d", 1, 15)
        if self.reference_source == "internal" and (self.harmonic_n, self.harmonic_d) != (1, 1):
            raise ChopperError("Harmonic multiplication/division applies only to external reference")
        if self.phase_deg is not None:
            number(self.phase_deg, "chopper.phase_deg", 0, 360)
            if self.phase_deg != int(self.phase_deg):
                raise ChopperError("chopper.phase_deg must be an integer degree supported by the controller")
        for name in ("run_enabled", "manual_phase_lock_confirmed", "single_opening_per_rotation"):
            if type(getattr(self, name)) is not bool:
                raise ChopperError(f"chopper.{name} must be boolean")
        if self.single_opening_per_rotation and (
                not self.blade.startswith("MC2F") or self.reference_track != "inner"
                or BLADE_SPECS[self.blade][2] <= 0):
            raise ChopperError("single_opening_per_rotation requires a confirmed MC2F inner beam "
                               "and an unmodified outer sensor track")
        if self.finish_rotation not in ("stop", "preserve"):
            raise ChopperError("chopper.finish_rotation must be stop or preserve")
        number(self.frequency_tolerance_hz, "chopper.frequency_tolerance_hz", 0, 1000)
        number(self.io_timeout_s, "chopper.io_timeout_s", 0.01, 60)
        number(self.serial_inter_character_delay_s, "chopper.serial_inter_character_delay_s", 0, 0.1)
        number(self.ready_timeout_s, "chopper.ready_timeout_s", 0.01, 3600)
        number(self.poll_interval_s, "chopper.poll_interval_s", 0.001, self.ready_timeout_s)
        integer(self.consecutive_good, "chopper.consecutive_good", 1, 100)
        for name in ("port", "expected_idn"):
            text(getattr(self, name), f"chopper.{name}", empty=True)

    @property
    def reference_code(self):
        if self.blade in HIGH_PRECISION:
            return {("internal", "outer"): 0, ("internal", "inner"): 1,
                    ("external", "outer"): 2, ("external", "inner"): 3}[
                        self.reference_source, self.reference_track]
        return 1 if self.reference_source == "external" else 0

    @property
    def actual_output_code(self):
        if self.single_opening_per_rotation:
            return 1  # The unmodified outer sensor measures complete rotations.
        return 2 if self.reference_track == "inner" else 1

    def _frequencies(self, reference_frequency_hz):
        self.validate()
        reference = number(reference_frequency_hz, "chopper reference frequency", 0.001, 1e6)
        control = reference * self.harmonic_n / self.harmonic_d
        nominal = control
        # Dual-frequency blades synchronize their outer track; HP blades permit
        # selecting either track. The motor limits apply before any beam mask.
        _, inner, outer = BLADE_SPECS[self.blade]
        if self.blade.startswith("MC2F"):
            number(control, "chopper native outer frequency", outer * 2, outer * 100)
            if self.reference_track == "inner":
                nominal *= inner / outer
        slots = outer if self.reference_track == "outer" else inner
        number(nominal, "chopper nominal selected-track frequency", slots * 2, slots * 100)
        if self.reference_source == "internal" and reference != int(reference):
            raise ChopperError("Internal chopper reference must use integer Hz")
        beam = control / outer if self.single_opening_per_rotation else nominal
        refout = control if self.single_opening_per_rotation else nominal
        return control, nominal, refout, beam

    def expected_input_frequency(self, reference_frequency_hz):
        """MC2000B 4.16 input? reports the reference after external N/D."""
        return self._frequencies(reference_frequency_hz)[0]

    def nominal_track_frequency(self, reference_frequency_hz):
        return self._frequencies(reference_frequency_hz)[1]

    def expected_refout_frequency(self, reference_frequency_hz):
        return self._frequencies(reference_frequency_hz)[2]

    def target_frequency(self, reference_frequency_hz):
        """Frequency at the operator-declared beam, separate from sensor slots."""
        return self._frequencies(reference_frequency_hz)[3]

    def beam_frequency_from_refout(self, refout_frequency_hz):
        self.validate()
        return (refout_frequency_hz / BLADE_SPECS[self.blade][2]
                if self.single_opening_per_rotation else refout_frequency_hz)

    def validate_reference_frequency(self, reference_frequency_hz):
        """Pure preflight hook used by the Combination grid validator."""
        return self.target_frequency(reference_frequency_hz)

    def require_hardware(self):
        self.validate()
        if not self.port.strip() or "CHANGE_ME" in self.port:
            raise ChopperError("Configure the explicit chopper serial port; discovery is not automatic")
        if not self.expected_idn.strip() or "CHANGE_ME" in self.expected_idn:
            raise ChopperError("Confirm exact chopper id? response before real connection")
        if self.model not in self.expected_idn:
            raise ChopperError("Chopper expected_idn must include the configured model")


def load_chopper_config(raw: dict, base: Path) -> ChopperConfig | None:
    """Load optional root [chopper]; disabled tables retain choices without I/O."""
    del base  # The serial port is an address, never a path relative to the TOML.
    if not isinstance(raw, dict):
        raise ChopperError("chopper configuration document must be a table")
    table = raw.get("chopper")
    if table is None:
        return None
    allowed = {field.name for field in fields(ChopperConfig)}
    keys(table, "chopper", set(), allowed | {"enabled"})
    enabled = table.get("enabled", False)
    if type(enabled) is not bool:
        raise ChopperError("chopper.enabled must be boolean")
    if not enabled:
        return None
    result = ChopperConfig(**{key: value for key, value in table.items() if key != "enabled"})
    result.validate()
    return result
