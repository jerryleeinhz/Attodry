"""Offline, explicit sine-current excitation contract for the Keithley 6221.

The source accepts peak amperes over SCPI. User coordinates are RMS amperes;
neither these coordinates nor their setting readbacks are measured current.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
import math
from collections.abc import Mapping


def finite_number(value, name, low, high):
    if (type(value) not in (int, float) or not math.isfinite(value)
            or not low <= value <= high):
        raise ValueError(f"{name} must be finite and within [{low}, {high}]")
    return float(value)


@dataclass(frozen=True)
class Excitation6221Config:
    resource: str
    expected_idn: str
    max_current_peak_a: float
    compliance_voltage_v: float
    offset_a: float
    phase_marker_line: int
    phase_marker_phase_deg: float
    current_rms_points_a: tuple[float, ...]
    frequency_points_hz: tuple[float, ...]

    def __post_init__(self):
        for name in ("resource", "expected_idn"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or any(c in value for c in "\r\n"):
                raise ValueError(f"excitation_6221.{name} must be a nonempty single line")
            if "CHANGE_ME" in value.upper():
                raise ValueError(f"excitation_6221.{name} still contains a placeholder")
        if "6221" not in self.expected_idn:
            raise ValueError("excitation_6221.expected_idn must identify model 6221")
        finite_number(self.max_current_peak_a, "max_current_peak_a", 2e-12, .105)
        finite_number(self.compliance_voltage_v, "compliance_voltage_v", .1, 105)
        # Instrument compliance programming resolution is 10 mV.
        if not math.isclose(self.compliance_voltage_v * 100,
                            round(self.compliance_voltage_v * 100), abs_tol=1e-8):
            raise ValueError("6221 compliance_voltage_v must use 0.01 V steps")
        finite_number(self.offset_a, "offset_a", -.105, .105)
        if type(self.phase_marker_line) is not int or not 1 <= self.phase_marker_line <= 6:
            raise ValueError("phase_marker_line must be an integer from 1 to 6")
        finite_number(self.phase_marker_phase_deg, "phase_marker_phase_deg", 0, 360)
        for name in ("current_rms_points_a", "frequency_points_hz"):
            values = getattr(self, name)
            if not isinstance(values, (tuple, list)) or not values:
                raise ValueError(f"{name} must be a nonempty ordered list")
            object.__setattr__(self, name, tuple(values))
        if len(self.current_rms_points_a) * len(self.frequency_points_hz) > 100000:
            raise ValueError("6221 grid exceeds 100000 conditions")
        for current in self.current_rms_points_a:
            self.validate_point(current, self.frequency_points_hz[0])
        for frequency in self.frequency_points_hz:
            finite_number(frequency, "excitation_frequency_hz", .001, 100000)

    def validate_point(self, current_rms_a, frequency_hz):
        current = finite_number(current_rms_a, "excitation_current_rms_a",
                                2e-12 / math.sqrt(2), .105 / math.sqrt(2))
        frequency = finite_number(frequency_hz, "excitation_frequency_hz", .001, 100000)
        peak = current * math.sqrt(2)
        if abs(self.offset_a) + peak > self.max_current_peak_a:
            raise ValueError("6221 abs(offset) + peak current exceeds the configured DUT limit")
        return current, frequency, peak

    def grid(self):
        """Frequency outer/current inner, preserving literal repeated points."""
        return tuple({"excitation_current_rms_a": float(current),
                      "excitation_frequency_hz": float(frequency)}
                     for frequency in self.frequency_points_hz
                     for current in self.current_rms_points_a)


def parse_excitation_6221(table):
    names = {field.name for field in fields(Excitation6221Config)}
    if not isinstance(table, Mapping) or set(table) != names:
        present = set(table) if isinstance(table, Mapping) else set()
        raise ValueError(f"excitation_6221 fields: missing={sorted(names - present)}, "
                         f"unknown={sorted(present - names)}")
    return Excitation6221Config(**table)
