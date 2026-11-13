"""Offline, explicit sine-current excitation contract for the Keithley 6221.

The source accepts peak amperes over SCPI. User coordinates are RMS amperes;
neither these coordinates nor their setting readbacks are measured current.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from decimal import Decimal
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
    # None is only for reading pre-v2 archives, never for new hardware sessions.
    inner_shield: str | None = None
    output_low_grounding: str | None = None
    phase_marker_enabled: bool | None = None

    def __post_init__(self):
        if any(v is not None for v in (self.inner_shield, self.output_low_grounding, self.phase_marker_enabled)):
            self.require_output_contract()
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

    def require_output_contract(self):
        if self.inner_shield not in ("output_low", "guard"):
            raise ValueError("inner_shield must be output_low or guard")
        if self.output_low_grounding not in ("floating", "earth"):
            raise ValueError("output_low_grounding must be floating or earth")
        if self.phase_marker_enabled is not True:
            raise ValueError("phase_marker_enabled must be true for external TTL detectors")

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

    def amplitude_setting(self, current_rms_a):
        from .excitation_quantization import quantize_amplitude
        return quantize_amplitude(current_rms_a, self.offset_a, self.max_current_peak_a)


def parse_excitation_6221(table):
    names = {field.name for field in fields(Excitation6221Config)}
    coordinates = (("current_rms_points_a", "current_rms_ranges_a", 2e-12 / math.sqrt(2), .105 / math.sqrt(2)),
                   ("frequency_points_hz", "frequency_ranges_hz", .001, 100000))
    required = names - {point_name for point_name, *_ in coordinates}
    allowed = names | {range_name for _, range_name, *_ in coordinates}
    if not isinstance(table, Mapping) or not required <= set(table) or set(table) - allowed:
        present = set(table) if isinstance(table, Mapping) else set()
        raise ValueError(f"excitation_6221 fields: missing={sorted(required - present)}, "
                         f"unknown={sorted(present - allowed)}")
    normalized = dict(table)
    segments_by_coordinate = {}
    counts = []
    for point_name, range_name, low, high in coordinates:
        if (point_name in table) == (range_name in table):
            raise ValueError(f"excitation_6221 requires exactly one of {point_name} or {range_name}")
        if point_name in table:
            points = table[point_name]
            if not isinstance(points, (tuple, list)) or not points:
                raise ValueError(f"{point_name} must be a nonempty ordered list")
            counts.append(len(points))
        else:
            segments = _parse_ranges(table[range_name], range_name, low, high)
            segments_by_coordinate[point_name] = segments
            counts.append(sum(segment[3] for segment in segments))
            del normalized[range_name]
    # Count both axes before allocating any generated coordinate points.
    if counts[0] * counts[1] > 100000:
        raise ValueError("6221 grid exceeds 100000 conditions")
    for point_name, segments in segments_by_coordinate.items():
        normalized[point_name] = _expand_ranges(segments)
    config = Excitation6221Config(**normalized)
    config.require_output_contract()
    return config


def _parse_ranges(value, name, low, high):
    if not isinstance(value, list) or not value:
        raise ValueError(f"{name} must be a nonempty array of range tables")
    segments = []
    total = 0
    for index, raw in enumerate(value):
        segment_name = f"{name}[{index}]"
        if not isinstance(raw, Mapping) or set(raw) != {"min", "max", "scale", "points"}:
            raise ValueError(f"{segment_name} requires exactly min, max, scale and points")
        minimum = finite_number(raw["min"], segment_name + ".min", low, high)
        maximum = finite_number(raw["max"], segment_name + ".max", low, high)
        if maximum <= minimum:
            raise ValueError(f"{segment_name}.max must be greater than min")
        scale, count = raw["scale"], raw["points"]
        if scale not in ("linear", "log"):
            raise ValueError(f"{segment_name}.scale must be linear or log")
        if type(count) is not int or count < 2:
            raise ValueError(f"{segment_name}.points must be an integer at least 2")
        total += count
        if total > 100000:
            raise ValueError("6221 grid exceeds 100000 conditions")
        segments.append((minimum, maximum, scale, count))
    return tuple(segments)


def _expand_ranges(segments):
    """Keep declared segment order and repeated endpoints; never sort/deduplicate."""
    values = []
    for minimum, maximum, scale, count in segments:
        if scale == "linear":
            start = Decimal(str(minimum))
            span = Decimal(str(maximum)) - start
            interior = (float(start + span * index / (count - 1)) for index in range(1, count - 1))
        else:
            start = math.log(minimum)
            span = math.log(maximum) - start
            interior = (math.exp(start + span * index / (count - 1)) for index in range(1, count - 1))
        values.extend((minimum, *interior, maximum))
    return tuple(values)
