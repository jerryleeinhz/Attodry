"""Injected-resource SR865A adapter; no discovery, connection or implicit writes.

Command definitions: https://www.thinksrs.com/downloads/pdfs/manuals/SR865Am.pdf
(revision 2.11). Source amplitude is the instrument's differential/50-ohm
setting, not an inferred sample voltage. The manual describes BlazeX sync as
2.5 V on p. 62 and +/-2 V or 0–2 V on p. 98; neither is an assumed 5 V TTL output.
A connected receiver and the actual waveform need commissioning.
This adapter deliberately has no source-amplitude/offset write or cleanup default.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import math
import re
from typing import Protocol

from .models import LockinRole
from . import sr865a_settings as settings


class Sr865aError(RuntimeError):
    """Invalid instrument response or unverified setting; audit remains available."""


class AuthorizationRequired(Sr865aError):
    pass


class VisaResource(Protocol):
    def query(self, command: str) -> str: ...
    def write(self, command: str) -> object: ...
    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class QueryRecord:
    command: str
    response: str | None
    started_at_utc: datetime
    completed_at_utc: datetime
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Sr865aSettings:
    identity: str
    reference_source: str
    external_reference_edge: str
    reference_input_impedance_ohm: float
    harmonic: int
    time_constant_s: float
    sensitivity_full_scale_v: float
    input_range_v_peak: float
    filter_slope_db_oct: int
    advanced_filter: bool
    synchronous_filter: bool
    input_mode: str
    input_coupling: str
    shield_grounding: str
    phase_shift_deg: float
    sine_output_v: float
    source_offset_v: float
    source_dc_mode: str
    sync_output_mode: str
    raw: tuple[QueryRecord, ...]
    source_amplitude_definition: str = "differential_rms_into_50_ohm_loads"


@dataclass(frozen=True, slots=True)
class Sr865aStatus:
    locked: bool | None
    input_overload: bool | None
    output_scale_overload: bool | None
    instrument_error: bool | None
    lia_status_raw: int | None
    error_status_raw: int | None
    standard_event_status_raw: int | None
    current_status_raw: int | None
    reference_unlock_latched: bool | None
    input_overload_latched: bool | None
    output_scale_overload_latched: bool | None
    filter_fault_latched: bool | None
    configuration_changed_latched: bool | None
    power_on_latched: bool | None
    unknown_status_bits: int
    consumed_status_latches: bool
    observed_at_utc: datetime
    raw: tuple[QueryRecord, ...]

    @property
    def valid(self) -> bool | None:
        """True needs current-state evidence AND a complete clean latch window."""
        bad = (
            self.locked is False or self.input_overload is True
            or self.output_scale_overload is True or self.instrument_error is True
            or self.reference_unlock_latched is True
            or self.input_overload_latched is True
            or self.output_scale_overload_latched is True
            or self.filter_fault_latched is True
            or self.configuration_changed_latched is True
            or self.power_on_latched is True or bool(self.unknown_status_bits)
        )
        if bad:
            return False
        if self.locked is None or self.instrument_error is None or not self.consumed_status_latches:
            return None
        return True


@dataclass(frozen=True, slots=True)
class Sr865aSample:
    role: LockinRole
    identity: str
    harmonic: int
    x_v: float
    y_v: float
    amplitude_v: float
    phase_deg: float | None
    reference_frequency_hz: float
    detection_frequency_hz: float
    captured_at_utc: datetime
    status: Sr865aStatus
    raw: tuple[QueryRecord, ...]
    amplitude_phase_source: str = "derived_from_simultaneous_xy"
    frequencies_are_sequential: bool = True


_REFERENCE_SOURCES = ("internal", "external", "dual", "chop")
_TRIGGERS = ("sine", "rising", "falling")
_IMPEDANCES = (50.0, 1_000_000.0)
_DC_MODES = ("common", "difference")
_SYNC_MODES = ("blazex", "bipolar_sync", "unipolar_sync")
_OUTPUT_OVERLOAD_MASK = 0xF03
_KNOWN_CURRENT_MASK = 0xF1B
# LIAS query description says 12 bits, but its status table also defines bits
# 12–14 (capture/scan). Preserve the full word; bit 2 and bit 15 are unknown.
_KNOWN_LIA_MASK = 0x7FFB
_CODE_COUNTS = {"RSRC": 4, "RTRG": 3, "REFZ": 2, "OFLT": 22,
                "SCAL": 28, "IRNG": 5}


class Sr865a:
    def __init__(self, resource: VisaResource, role: LockinRole):
        if not isinstance(role, LockinRole):
            raise ValueError("role must be a LockinRole.")
        self._resource = resource
        self.role = role
        self._identity: str | None = None
        self._audit: list[QueryRecord] = []

    @property
    def audit(self) -> tuple[QueryRecord, ...]:
        """All I/O so far, including a failed/partial operation."""
        return tuple(self._audit)

    def query_identity(self) -> str:
        self._identity = None
        identity = self._query_text("*IDN?")
        parts = identity.split(",")
        if (
            len(parts) != 4 or parts[0] != "Stanford_Research_Systems"
            or parts[1] != "SR865A" or not parts[2].strip()
            or not re.fullmatch(r"[vV]\d+\.\d+(?:\.\d+)?", parts[3])
        ):
            raise Sr865aError(f"{self.role.value} expected SR865A identity, received {identity!r}.")
        self._identity = identity
        return identity

    def read_identity(self) -> str:
        return self.query_identity()

    def read_settings(self) -> Sr865aSettings:
        start = len(self._audit)
        identity = self.query_identity()
        self._verify_voltage_input()
        values = dict(
            identity=identity,
            reference_source=self._enum("RSRC?", _REFERENCE_SOURCES),
            external_reference_edge=self._enum("RTRG?", _TRIGGERS),
            reference_input_impedance_ohm=self._enum("REFZ?", _IMPEDANCES),
            harmonic=self.read_harmonic(),
            time_constant_s=self.read_time_constant(),
            sensitivity_full_scale_v=self.read_sensitivity(),
            input_range_v_peak=self.read_input_range(),
            filter_slope_db_oct=self._mapped("OFSL?", settings.filter_slope_db_oct),
            advanced_filter=bool(self._enum("ADVFILT?", (0, 1))),
            synchronous_filter=bool(self._enum("SYNC?", (0, 1))),
            input_mode=self._enum("ISRC?", ("a", "a_minus_b")),
            input_coupling=self._enum("ICPL?", ("ac", "dc")),
            shield_grounding=self._enum("IGND?", ("float", "ground")),
            phase_shift_deg=self._query_float("PHAS?"),
            sine_output_v=self.read_sine_output(),
            source_offset_v=self.read_source_offset(),
            source_dc_mode=self.read_source_dc_mode(),
            sync_output_mode=self._enum("BLAZEX?", _SYNC_MODES),
        )
        return Sr865aSettings(**values, raw=tuple(self._audit[start:]))

    def read_time_constant(self) -> float:
        return self._mapped("OFLT?", settings.time_constant_seconds)

    def read_sensitivity(self) -> float:
        self._verify_voltage_input()
        return self._mapped("SCAL?", settings.sensitivity_full_scale_v)

    def read_input_range(self) -> float:
        self._verify_voltage_input()
        return self._mapped("IRNG?", settings.input_range_v_peak)

    def read_harmonic(self) -> int:
        value = self._query_int("HARM?")
        if not 1 <= value <= settings.MAXIMUM_HARMONIC:
            self._identity = None
            raise Sr865aError(f"Invalid SR865A harmonic {value}.")
        return value

    def read_external_frequency(self) -> float:
        return self._frequency("FREQEXT?")

    def read_detection_frequency(self) -> float:
        return self._frequency("FREQDET?")

    def read_reference_frequency(self) -> float:
        source = self._enum("RSRC?", _REFERENCE_SOURCES)
        if source not in ("internal", "external"):
            self._identity = None
            raise Sr865aError("Dual/chop references are outside the initial adapter scope.")
        return self._frequency("FREQEXT?" if source == "external" else "FREQINT?")

    def read_sine_output(self) -> float:
        value = self._query_float("SLVL?")
        if not settings.MINIMUM_SINE_OUTPUT_V <= value <= settings.MAXIMUM_SINE_OUTPUT_V:
            self._identity = None
            raise Sr865aError(f"Invalid SR865A sine amplitude {value} V.")
        return value

    def read_source_offset(self) -> float:
        value = self._query_float("SOFF?")
        if not -5.0 <= value <= 5.0:
            self._identity = None
            raise Sr865aError(f"Invalid SR865A source DC offset {value} V.")
        return value

    def read_source_dc_mode(self) -> str:
        return self._enum("REFM?", _DC_MODES)

    def configure_reference(
        self, source: str, *, edge: str | None = None,
        input_impedance_ohm: float | None = None, authorized: bool = False,
    ) -> None:
        # Validate every parameter before any query or setting write.
        if source not in ("internal", "external_ttl"):
            raise ValueError("Supported sources are internal and external_ttl.")
        if source == "internal":
            if edge is not None or input_impedance_ohm is not None:
                raise ValueError("Internal reference must not supply external input parameters.")
            commands = (("RSRC", 0),)
        else:
            if edge not in ("rising", "falling"):
                raise ValueError("External TTL requires an explicit rising/falling edge.")
            impedance = settings.finite_number(input_impedance_ohm, "reference input impedance")
            if impedance not in _IMPEDANCES:
                raise ValueError("Reference impedance must be explicitly 50 or 1000000 ohms.")
            commands = (("REFZ", _IMPEDANCES.index(impedance)),
                        ("RTRG", _TRIGGERS.index(edge)), ("RSRC", 1))
        self._authorize(authorized)
        for command, value in commands:
            self._write_verified_code(command, value)

    def set_time_constant(self, seconds: float, *, authorized: bool = False) -> float:
        code = settings.time_constant_code(seconds)
        self._authorize(authorized)
        self._write_verified_code("OFLT", code)
        return settings.time_constant_seconds(code)

    def set_sensitivity(self, full_scale_v: float, *, authorized: bool = False) -> float:
        code = settings.sensitivity_code(full_scale_v)
        self._authorize(authorized)
        self._verify_voltage_input()
        self._write_verified_code("SCAL", code)
        return settings.sensitivity_full_scale_v(code)

    def set_input_range(self, v_peak: float, *, authorized: bool = False) -> float:
        code = settings.input_range_code(v_peak)
        self._authorize(authorized)
        self._verify_voltage_input()
        self._write_verified_code("IRNG", code)
        return settings.input_range_v_peak(code)

    def set_harmonic(self, harmonic: int, *, authorized: bool = False) -> int:
        if type(harmonic) is not int or not 1 <= harmonic <= settings.MAXIMUM_HARMONIC:
            raise ValueError("SR865A harmonic must be an integer from 1 to 99.")
        self._authorize(authorized)
        settings.validate_harmonic_frequency(harmonic, self.read_reference_frequency())
        self._write_verified_code("HARM", harmonic)
        # A drifting reference can cross a boundary after the command as well.
        settings.validate_harmonic_frequency(harmonic, self.read_reference_frequency())
        return harmonic

    def read_status(
        self, *, consume_status_latches: bool, current_status_supported: bool = False,
    ) -> Sr865aStatus:
        if type(consume_status_latches) is not bool or type(current_status_supported) is not bool:
            raise ValueError("Status capability/consumption flags must be booleans.")
        start = len(self._audit)
        current = self._status_word("CUROVLDSTAT?", 0xFFFF) if current_status_supported else None
        lia = self._status_word("LIAS?", 0xFFFF) if consume_status_latches else None
        errors = self._status_word("ERRS?", 0xFF) if consume_status_latches else None
        standard = self._status_word("*ESR?", 0xFF) if consume_status_latches else None
        unknown = (0 if lia is None else lia & ~_KNOWN_LIA_MASK)
        unknown |= 0 if current is None else current & ~_KNOWN_CURRENT_MASK
        unknown |= 0 if errors is None else errors & 0x0C
        unknown |= 0 if standard is None else standard & 0x04
        return Sr865aStatus(
            locked=None if current is None else not bool(current & 8),
            input_overload=None if current is None else bool(current & 16),
            output_scale_overload=None if current is None else bool(current & _OUTPUT_OVERLOAD_MASK),
            instrument_error=None if errors is None else bool(errors or (standard & 0x3A)),
            lia_status_raw=lia, error_status_raw=errors,
            standard_event_status_raw=standard, current_status_raw=current,
            reference_unlock_latched=None if lia is None else bool(lia & 8),
            input_overload_latched=None if lia is None else bool(lia & 16),
            output_scale_overload_latched=None if lia is None else bool(lia & _OUTPUT_OVERLOAD_MASK),
            filter_fault_latched=None if lia is None else bool(lia & 0x60),
            configuration_changed_latched=None if standard is None else bool(standard & 0x40),
            power_on_latched=None if standard is None else bool(standard & 0x80),
            unknown_status_bits=unknown, consumed_status_latches=consume_status_latches,
            observed_at_utc=datetime.now(UTC), raw=tuple(self._audit[start:]),
        )

    def read_sample(
        self, *, consume_status_latches: bool, current_status_supported: bool = False,
    ) -> Sr865aSample:
        if type(consume_status_latches) is not bool or type(current_status_supported) is not bool:
            raise ValueError("Status capability/consumption flags must be booleans.")
        start = len(self._audit)
        identity = self.query_identity()
        self._verify_voltage_input()
        harmonic = self.read_harmonic()
        reference_hz = self.read_reference_frequency()
        try:
            expected_detection = settings.validate_harmonic_frequency(harmonic, reference_hz)
        except ValueError as exc:
            self._identity = None
            raise Sr865aError(str(exc)) from exc
        detection_hz = self.read_detection_frequency()
        if not (
            settings.MINIMUM_REFERENCE_FREQUENCY_HZ <= detection_hz
            < settings.MAXIMUM_REFERENCE_FREQUENCY_HZ
        ) or not math.isclose(detection_hz, expected_detection, rel_tol=1e-6, abs_tol=0.001):
            self._identity = None
            raise Sr865aError("Detection frequency does not match harmonic and reference readbacks.")
        xy = self._query_text("SNAP? X,Y").split(",")
        if len(xy) != 2:
            self._identity = None
            raise Sr865aError("SR865A SNAP? X,Y must return exactly two fields.")
        x, y = (self._parse_float(value, "SNAP? X,Y") for value in xy)
        captured = self._audit[-1].completed_at_utc
        amplitude = math.hypot(x, y)
        if not math.isfinite(amplitude):
            self._identity = None
            raise Sr865aError("Derived SR865A amplitude is non-finite.")
        phase = None if x == y == 0 else math.degrees(math.atan2(y, x))
        status = self.read_status(consume_status_latches=consume_status_latches,
                                  current_status_supported=current_status_supported)
        return Sr865aSample(
            role=self.role, identity=identity, harmonic=harmonic, x_v=x, y_v=y,
            amplitude_v=amplitude, phase_deg=phase,
            reference_frequency_hz=reference_hz, detection_frequency_hz=detection_hz,
            captured_at_utc=captured, status=status, raw=tuple(self._audit[start:]),
        )

    def close(self) -> None:
        self._identity = None
        self._resource.close()

    def _authorize(self, authorized: bool) -> None:
        if authorized is not True:
            raise AuthorizationRequired("Explicit authorization is required before SR865A settings writes.")
        if self._identity is None:
            raise Sr865aError("Verify SR865A identity before settings writes.")

    def _write_verified_code(self, command: str, value: int) -> None:
        previous = self._query_int(command + "?")
        valid_previous = (1 <= previous <= 99 if command == "HARM"
                          else 0 <= previous < _CODE_COUNTS[command])
        if not valid_previous:
            self._identity = None
            raise Sr865aError(f"Invalid pre-write {command} setting: {previous}.")
        wire = f"{command} {value}"
        started = datetime.now(UTC)
        try:
            self._resource.write(wire)
        except Exception as exc:
            self._identity = None
            self._audit.append(QueryRecord(wire, None, started, datetime.now(UTC), str(exc)))
            raise Sr865aError(f"SR865A write failed: {wire}.") from exc
        self._audit.append(QueryRecord(wire, None, started, datetime.now(UTC)))
        if self._query_int(command + "?") != value:
            self._identity = None
            raise Sr865aError(f"SR865A {command} readback did not match requested code {value}.")

    def _query_text(self, command: str) -> str:
        started = datetime.now(UTC)
        try:
            response = self._resource.query(command)
        except Exception as exc:
            self._identity = None
            self._audit.append(QueryRecord(command, None, started, datetime.now(UTC), str(exc)))
            raise Sr865aError(f"SR865A query failed: {command}.") from exc
        self._audit.append(QueryRecord(command, response, started, datetime.now(UTC)))
        if not isinstance(response, str) or not response.strip():
            self._identity = None
            raise Sr865aError(f"Empty/nontext SR865A response to {command}.")
        return response.strip()

    def _query_int(self, command: str) -> int:
        response = self._query_text(command)
        if not re.fullmatch(r"[+-]?\d+", response):
            self._identity = None
            raise Sr865aError(f"Invalid integer reply to {command}: {response!r}.")
        return int(response)

    def _parse_float(self, response: str, command: str) -> float:
        try:
            value = float(response)
        except (TypeError, ValueError) as exc:
            self._identity = None
            raise Sr865aError(f"Invalid numeric reply to {command}: {response!r}.") from exc
        if not math.isfinite(value):
            self._identity = None
            raise Sr865aError(f"Non-finite reply to {command}: {response!r}.")
        return value

    def _query_float(self, command: str) -> float:
        return self._parse_float(self._query_text(command), command)

    def _enum(self, command: str, values: tuple):
        index = self._query_int(command)
        if not 0 <= index < len(values):
            self._identity = None
            raise Sr865aError(f"Unsupported {command} code {index}.")
        return values[index]

    def _mapped(self, command: str, decoder):
        try:
            return decoder(self._query_int(command))
        except ValueError as exc:
            self._identity = None
            raise Sr865aError(f"Invalid {command} setting.") from exc

    def _verify_voltage_input(self) -> None:
        if self._enum("IVMD?", ("voltage", "current")) != "voltage":
            self._identity = None
            raise Sr865aError("Current-input mode cannot be labelled as voltage data.")

    def _frequency(self, command: str) -> float:
        value = self._query_float(command)
        if not 0 <= value <= settings.MAXIMUM_REFERENCE_FREQUENCY_HZ:
            self._identity = None
            raise Sr865aError(f"Invalid SR865A frequency response {value} Hz.")
        return value

    def _status_word(self, command: str, maximum: int) -> int:
        value = self._query_int(command)
        if not 0 <= value <= maximum:
            self._identity = None
            raise Sr865aError(f"Invalid SR865A status word from {command}: {value}.")
        return value
