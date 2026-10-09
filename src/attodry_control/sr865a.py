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
import struct
import time
from typing import Callable, Protocol

from .models import LockinRole
from .reference_transients import ReferenceTransientError
from . import sr865a_settings as settings


class Sr865aError(RuntimeError):
    """Invalid instrument response or unverified setting; audit remains available."""


class Sr865aReferenceTransientError(ReferenceTransientError, Sr865aError):
    """Finite in-range sequential frequency disagreement, without implicit retry."""


class AuthorizationRequired(Sr865aError):
    pass


class Sr865aCaptureError(Sr865aError):
    """An owned capture failed; ``result`` retains partial data and stop evidence."""

    def __init__(self, message: str, result: Sr865aCapture):
        super().__init__(message)
        self.result = result


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


@dataclass(frozen=True, slots=True)
class Sr865aCapture:
    role: LockinRole
    identity: str
    requested_duration_s: float
    requested_rate_hz: float
    actual_rate_hz: float | None
    rate_divider: int | None
    buffer_length_kib: int | None
    valid_data_bytes: int | None
    downloaded_bytes: int
    padding_bytes: int
    final_capture_status: int | None
    x_v: tuple[float, ...]
    y_v: tuple[float, ...]
    r_v: tuple[float, ...]
    t_s: tuple[float, ...]
    started_at_utc: datetime | None
    stopped_at_utc: datetime | None
    completed: bool
    error: str | None
    cleanup_errors: tuple[str, ...]
    raw: tuple[QueryRecord, ...]
    time_axis_source: str = "sample_index_divided_by_actual_rate; UTC start/stop are host bounds"
    binary_audit_format: str = "decoded float32 little-endian payload hex; IEEE header parsed by transport"
    capture_owned: bool = False
    stop_verified: bool = False
    capture_configuration_restored: bool = False
    original_capture_configuration: dict | None = None


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

    def read_internal_frequency(self) -> float:
        value = self._frequency("FREQINT?")
        if value < settings.MINIMUM_REFERENCE_FREQUENCY_HZ:
            self._identity = None
            raise Sr865aError("Stored internal frequency is below the instrument minimum.")
        return value

    def restore_stored_internal_frequency(self, frequency_hz: float, *, authorized: bool = False) -> float:
        """Restore the saved unused oscillator setting while external remains selected.

        FREQINT is a stored setting in external mode. This deliberately does not
        switch RSRC to internal, since a saved unused oscillator frequency may
        lie outside the connected downstream receiver's approved interval.
        External frequency observations are sequential and can drift; retain
        them rather than claiming that equal numeric readbacks prove phase lock.
        """
        requested = settings.finite_number(frequency_hz, "stored internal frequency")
        if not settings.MINIMUM_REFERENCE_FREQUENCY_HZ <= requested <= settings.MAXIMUM_REFERENCE_FREQUENCY_HZ:
            raise ValueError("Stored internal frequency must be within 1 mHz to 4 MHz.")
        self._authorize(authorized)
        if self._enum("RSRC?", _REFERENCE_SOURCES) != "external":
            raise Sr865aError("Unused internal frequency can only be restored while external is selected.")
        harmonic = self.read_harmonic()
        self.read_external_frequency()
        self.read_detection_frequency()
        previous = self.read_internal_frequency()
        if previous != requested:
            self._write_command(f"FREQINT {requested:.12g}")
        actual = self.read_internal_frequency()
        quantum = max(0.0001, 10.0 ** (math.floor(math.log10(requested)) - 5))
        allowance = quantum / 2 + max(math.ulp(requested), math.ulp(actual)) * 4
        if abs(actual - requested) > allowance:
            self._identity = None
            raise Sr865aError("Stored internal frequency restore exceeds documented rounding resolution.")
        self.read_external_frequency()
        self.read_detection_frequency()
        if self._enum("RSRC?", _REFERENCE_SOURCES) != "external" or self.read_harmonic() != harmonic:
            self._identity = None
            raise Sr865aError("External reference source/harmonic changed during stored-frequency restore.")
        return actual

    def set_internal_frequency(self, frequency_hz: float, *, authorized: bool = False) -> float:
        requested = settings.finite_number(frequency_hz, "internal frequency")
        if not settings.MINIMUM_REFERENCE_FREQUENCY_HZ <= requested < settings.MAXIMUM_REFERENCE_FREQUENCY_HZ:
            raise ValueError("Internal detection frequency must be within 1 mHz to below 4 MHz.")
        self._authorize(authorized)
        if self._enum("RSRC?", _REFERENCE_SOURCES) != "internal":
            raise Sr865aError("Set internal reference mode before its diagnostic frequency.")
        harmonic = self.read_harmonic()
        settings.validate_harmonic_frequency(harmonic, requested)
        previous = self.read_internal_frequency()
        settings.validate_harmonic_frequency(harmonic, previous)
        if previous != requested:
            self._write_command(f"FREQINT {requested:.12g}")
        actual = self.read_internal_frequency()
        # FREQINT rounds to six significant digits or 0.1 mHz, whichever
        # resolution is coarser (manual p. 108); this is not oscillator accuracy.
        quantum = max(0.0001, 10.0 ** (math.floor(math.log10(requested)) - 5))
        allowance = quantum / 2 + max(math.ulp(requested), math.ulp(actual)) * 4
        if abs(actual - requested) > allowance:
            self._identity = None
            raise Sr865aError("Internal frequency readback exceeds the documented rounding resolution.")
        expected_detection = settings.validate_harmonic_frequency(harmonic, actual)
        detection = self.read_detection_frequency()
        if abs(detection - expected_detection) > allowance * harmonic:
            self._identity = None
            raise Sr865aError("Internal detection frequency does not match harmonic and reference.")
        return actual

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
        reference_frequency_tolerance_hz: float | None = None,
    ) -> Sr865aSample:
        if type(consume_status_latches) is not bool or type(current_status_supported) is not bool:
            raise ValueError("Status capability/consumption flags must be booleans.")
        if reference_frequency_tolerance_hz is not None:
            reference_frequency_tolerance_hz = settings.finite_number(
                reference_frequency_tolerance_hz, "reference_frequency_tolerance_hz")
            if reference_frequency_tolerance_hz <= 0:
                raise ValueError("reference_frequency_tolerance_hz must be positive.")
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
        # The two frequency queries are sequential. An explicitly supplied
        # tolerance is in fundamental-reference Hz, independent of harmonic.
        # Keep the legacy comparison unchanged for callers that omit it.
        frequency_matches = (
            math.isclose(detection_hz, expected_detection, rel_tol=1e-6, abs_tol=0.001)
            if reference_frequency_tolerance_hz is None else
            abs(detection_hz / harmonic - reference_hz) <= reference_frequency_tolerance_hz
        )
        if not (
            settings.MINIMUM_REFERENCE_FREQUENCY_HZ <= detection_hz
            < settings.MAXIMUM_REFERENCE_FREQUENCY_HZ
        ):
            self._identity = None
            raise Sr865aError("Detection frequency does not match harmonic and reference readbacks.")
        if not frequency_matches:
            # Preserve the already verified identity for this narrow read-only
            # disagreement. A caller may explicitly protect outputs and wait
            # for fresh consistent reference evidence; malformed/range/I/O
            # errors above retain their hard-error path and identity revocation.
            raise Sr865aReferenceTransientError(
                "Detection frequency does not match harmonic and reference readbacks.",
                kind="detection_frequency_mismatch", evidence={
                    "role": self.role.value, "model": "SR865A", "identity": identity,
                    "harmonic": harmonic, "reference_frequency_hz": reference_hz,
                    "detection_frequency_hz": detection_hz,
                    "expected_detection_frequency_hz": expected_detection,
                    "detection_fundamental_frequency_hz": detection_hz / harmonic,
                    "reference_difference_hz": abs(detection_hz / harmonic - reference_hz),
                    "reference_frequency_tolerance_hz": reference_frequency_tolerance_hz,
                    "raw": tuple(self._audit[start:]),
                })
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

    def capture_xy(
        self, duration_s: float, requested_rate_hz: float, *, timeout_s: float,
        authorized: bool = False, clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        on_poll: Callable[[], None] | None = None,
        poll_interval_s: float = 1.0,
    ) -> Sr865aCapture:
        """Own one immediate XY capture, with bounded stop/download on failure.

        The caller must declare that no user capture is armed and that replacing
        the old buffer is permitted: CAPTURESTAT cannot distinguish armed from
        idle. It does reject a running capture without touching or stopping it.
        Callbacks are serialized with capture I/O, approximately every second;
        no callback result is substituted for actual buffered XY samples.
        """
        duration = settings.finite_number(duration_s, "capture duration")
        requested_rate = settings.finite_number(requested_rate_hz, "capture sample rate")
        timeout = settings.finite_number(timeout_s, "capture timeout")
        poll_interval = settings.finite_number(poll_interval_s, "capture guard interval")
        if duration <= 0 or requested_rate <= 0 or timeout <= duration:
            raise ValueError("Capture duration/rate must be positive and timeout must exceed duration.")
        if duration * requested_rate * 8 > 4096 * 1024:
            raise ValueError("Requested XY capture exceeds the 4 MiB instrument buffer.")
        if not 0.1 <= poll_interval <= 5.0:
            raise ValueError("Capture guard interval must be between 0.1 and 5 s.")
        if not callable(clock) or not callable(sleep) or (on_poll is not None and not callable(on_poll)):
            raise ValueError("Capture clock, sleep and optional guard must be callable.")
        self._authorize(authorized)
        query_binary = getattr(self._resource, "query_binary_values", None)
        supports_binary = getattr(self._resource, "supports_binary_capture", callable(query_binary))
        resource_name = str(getattr(self._resource, "resource_name", ""))
        if not callable(query_binary) or not supports_binary or resource_name.upper().startswith("ASRL"):
            raise Sr865aError("CAPTUREGET requires binary-capable USB/GPIB/VXI-11; RS-232 is unsupported.")
        start_audit = len(self._audit)
        identity = self._identity
        deadline = clock() + timeout
        actual_rate = divider = length = valid_bytes = final_status = None
        started_at = stopped_at = None
        downloaded_bytes = padding_bytes = 0
        x: list[float] = []
        y: list[float] = []
        owned = False
        stop_verified = False
        primary: BaseException | None = None
        cleanup_errors: list[str] = []
        original_capture: tuple[int, int, int, float] | None = None
        capture_settings_touched = False
        capture_configuration_restored = True
        download_attempted = False
        has_transport_timeout = hasattr(self._resource, "timeout")
        original_transport_timeout = getattr(self._resource, "timeout", None)

        def check_deadline(limit: float) -> None:
            if clock() >= limit:
                raise TimeoutError("SR865A capture deadline expired.")

        def bound_timeout(limit: float) -> None:
            check_deadline(limit)
            if has_transport_timeout:
                old_limit = original_transport_timeout
                if not isinstance(old_limit, (int, float)) or not math.isfinite(old_limit) or old_limit <= 0:
                    old_limit = 5000
                self._resource.timeout = max(1, min(old_limit, 5000, int((limit - clock()) * 1000)))

        def query_int(command: str, limit: float) -> int:
            bound_timeout(limit)
            return self._query_int(command)

        def query_float(command: str, limit: float) -> float:
            bound_timeout(limit)
            return self._query_float(command)

        def write(command: str, limit: float) -> None:
            bound_timeout(limit)
            self._write_command(command)

        def capture_status(limit: float) -> int:
            bound_timeout(limit)
            return self._capture_status()

        def poll_guard(limit: float) -> None:
            check_deadline(limit)
            if on_poll is not None:
                bound_timeout(limit)
                on_poll()
            check_deadline(limit)

        def stop_capture(limit: float) -> None:
            nonlocal final_status, stopped_at, stop_verified
            write("CAPTURESTOP", limit)
            while True:
                check_deadline(limit)
                final_status = capture_status(limit)
                if not final_status & 1:
                    stopped_at = datetime.now(UTC)
                    stop_verified = True
                    return
                sleep(min(0.05, max(0.0, limit - clock())))

        def download(limit: float, *, guard: bool) -> None:
            nonlocal valid_bytes, downloaded_bytes, padding_bytes, download_attempted
            download_attempted = True
            check_deadline(limit)
            valid_bytes = query_int("CAPTUREBYTES?", limit)
            if valid_bytes < 0 or valid_bytes > length * 1024 or valid_bytes % 8:
                raise Sr865aError("Invalid capture byte count for complete XY float32 pairs.")
            total_bytes = math.ceil(valid_bytes / 2048) * 2048
            padding_bytes = total_bytes - valid_bytes
            offset = 0
            next_download_guard = clock()
            while offset * 1024 < total_bytes:
                if guard and clock() >= next_download_guard:
                    poll_guard(limit)
                    next_download_guard = clock() + poll_interval
                check_deadline(limit)
                count_kib = min(64, total_bytes // 1024 - offset)
                bound_timeout(limit)
                values = self._query_capture_values(f"CAPTUREGET? {offset},{count_kib}")
                downloaded_bytes += len(values) * 4
                if len(values) != count_kib * 256:
                    raise Sr865aError("Binary capture block length does not match requested KiB.")
                real_count = min(len(values), max(0, valid_bytes // 4 - offset * 256))
                real_values = values[:real_count]
                if any(not math.isfinite(value) for value in real_values):
                    raise Sr865aError("Non-finite value in captured XY data.")
                x.extend(real_values[0::2])
                y.extend(real_values[1::2])
                offset += count_kib
                check_deadline(limit)

        try:
            final_status = capture_status(deadline)
            if final_status & 1:
                raise Sr865aError("An existing active capture is not owned by this operation.")
            bound_timeout(deadline)
            self._verify_voltage_input()
            maximum_rate = query_float("CAPTURERATEMAX?", deadline)
            if not 0 < maximum_rate <= 1_250_000 or requested_rate > maximum_rate:
                raise Sr865aError("Requested capture rate exceeds current instrument capability.")
            divider = min(20, max(0, math.floor(math.log2(maximum_rate / requested_rate))))
            planned_rate = maximum_rate / 2 ** divider
            length = max(2, math.ceil(duration * planned_rate * 8 / 2048) * 2)
            if length > 4096:
                raise Sr865aError("Actual hardware rate makes the XY capture exceed 4 MiB.")
            poll_guard(deadline)
            previous_cfg = query_int("CAPTURECFG?", deadline)
            previous_length = query_int("CAPTURELEN?", deadline)
            previous_rate = query_float("CAPTURERATE?", deadline)
            if not (0 <= previous_cfg <= 3 and 2 <= previous_length <= 4096
                    and previous_length % 2 == 0 and 0 < previous_rate <= maximum_rate):
                raise Sr865aError("Invalid existing capture configuration.")
            previous_divider = round(math.log2(maximum_rate / previous_rate))
            if not 0 <= previous_divider <= 20 or not math.isclose(
                previous_rate, maximum_rate / 2 ** previous_divider, rel_tol=1e-9, abs_tol=1e-9
            ):
                raise Sr865aError("Existing capture rate is not a documented hardware divider.")
            original_capture = previous_cfg, previous_length, previous_divider, previous_rate
            for command, value, previous in (
                ("CAPTURECFG", 1, previous_cfg),
                ("CAPTURELEN", length, previous_length),
                ("CAPTURERATE", divider, previous_divider),
            ):
                check_deadline(deadline)
                if previous != value:
                    capture_settings_touched = True
                    capture_configuration_restored = False
                    write(f"{command} {value}", deadline)
                    # CAPTURERATE? returns Hz, unlike its divider write.
                    if command != "CAPTURERATE" and query_int(command + "?", deadline) != value:
                        raise Sr865aError(f"{command} readback mismatch.")
            actual_rate = query_float("CAPTURERATE?", deadline)
            if not math.isclose(actual_rate, planned_rate, rel_tol=1e-9, abs_tol=1e-9):
                raise Sr865aError("Actual capture rate does not match the selected hardware divider.")
            poll_guard(deadline)
            # A failed START write might still have reached the device. From
            # this point, bounded STOP is required even if identity is revoked.
            owned = True
            started_at = datetime.now(UTC)
            write("CAPTURESTART 0,0", deadline)
            capture_start = clock()
            next_guard = capture_start
            while True:
                check_deadline(deadline)
                final_status = capture_status(deadline)
                if not final_status & 2:
                    raise Sr865aError("Immediate capture did not report its triggered state.")
                if clock() >= next_guard:
                    poll_guard(deadline)
                    next_guard = clock() + poll_interval
                if not final_status & 1 or clock() - capture_start >= duration:
                    break
                sleep(min(0.05, max(0.0, capture_start + duration - clock())))
            stop_capture(min(deadline, clock() + 5.0))
            poll_guard(deadline)
            download(deadline, guard=True)
            # The first hardware sample is not aligned to the host START clock;
            # one sample of endpoint quantization is therefore expected.
            if len(x) < max(1, math.floor(duration * actual_rate)):
                raise Sr865aError("Capture stopped before the requested number of samples was acquired.")
            poll_guard(deadline)
        except BaseException as exc:
            primary = exc
            if owned and not stop_verified:
                try:
                    stop_capture(clock() + 5.0)
                except BaseException as cleanup_exc:
                    cleanup_errors.append(f"capture stop: {type(cleanup_exc).__name__}: {cleanup_exc}")
            if owned and stop_verified and not download_attempted:
                try:
                    download(clock() + 5.0, guard=False)
                except BaseException as cleanup_exc:
                    cleanup_errors.append(f"partial capture download: {type(cleanup_exc).__name__}: {cleanup_exc}")
        if capture_settings_touched and original_capture is not None:
            try:
                restore_deadline = clock() + 5.0
                if capture_status(restore_deadline) & 1:
                    raise Sr865aError("Capture configuration cannot be restored while capture remains active.")
                cfg, size, old_divider, old_rate = original_capture
                for command, value in (("CAPTURECFG", cfg), ("CAPTURELEN", size),
                                       ("CAPTURERATE", old_divider)):
                    write(f"{command} {value}", restore_deadline)
                    actual = query_float(command + "?", restore_deadline)
                    expected = old_rate if command == "CAPTURERATE" else value
                    if not math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-9):
                        raise Sr865aError(f"Capture cleanup {command} readback mismatch.")
                capture_configuration_restored = True
            except BaseException as cleanup_exc:
                cleanup_errors.append(f"capture configuration restore: {type(cleanup_exc).__name__}: {cleanup_exc}")
                if primary is None:
                    primary = cleanup_exc
        if has_transport_timeout:
            try:
                self._resource.timeout = original_transport_timeout
            except BaseException as cleanup_exc:
                cleanup_errors.append(f"transport timeout restore: {type(cleanup_exc).__name__}: {cleanup_exc}")
                if primary is None:
                    primary = cleanup_exc
        r = tuple(math.hypot(a, b) for a, b in zip(x, y))
        result = Sr865aCapture(
            self.role, identity, duration, requested_rate, actual_rate, divider,
            length, valid_bytes, downloaded_bytes, padding_bytes, final_status,
            tuple(x), tuple(y), r,
            tuple(index / actual_rate for index in range(len(x))) if actual_rate else (),
            started_at, stopped_at, primary is None and not cleanup_errors,
            None if primary is None else f"{type(primary).__name__}: {primary}",
            tuple(cleanup_errors), tuple(self._audit[start_audit:]),
            capture_owned=owned, stop_verified=stop_verified,
            capture_configuration_restored=capture_configuration_restored,
            original_capture_configuration=None if original_capture is None else {
                "configuration_code": original_capture[0], "length_kib": original_capture[1],
                "rate_divider": original_capture[2], "actual_rate_hz": original_capture[3]},
        )
        if primary is not None:
            raise Sr865aCaptureError(result.error, result) from primary
        return result

    def _capture_status(self) -> int:
        value = self._query_int("CAPTURESTAT?")
        if not 0 <= value <= 7:
            raise Sr865aError("Unknown capture status bits.")
        return value

    def read_capture_status(self) -> int:
        """Read-only preflight; cannot distinguish idle from trigger-armed state."""
        self.query_identity()
        return self._capture_status()

    def _query_capture_values(self, command: str) -> list[float]:
        started = datetime.now(UTC)
        try:
            values = list(self._resource.query_binary_values(
                command, datatype="f", is_big_endian=False, header_fmt="ieee",
                expect_termination=False, container=list))
            # Retain decoded payload bytes, including invalid NaN/inf values,
            # without claiming that the transport's IEEE header was retained.
            payload_hex = struct.pack("<" + "f" * len(values), *values).hex()
        except BaseException as exc:
            self._identity = None
            self._audit.append(QueryRecord(command, None, started, datetime.now(UTC), repr(exc)))
            raise Sr865aError(f"SR865A binary capture query failed: {command}.") from exc
        self._audit.append(QueryRecord(command, payload_hex, started, datetime.now(UTC)))
        return values

    def _write_command(self, command: str) -> None:
        started = datetime.now(UTC)
        try:
            self._resource.write(command)
        except BaseException as exc:
            self._identity = None
            self._audit.append(QueryRecord(command, None, started, datetime.now(UTC), repr(exc)))
            raise Sr865aError(f"SR865A write failed: {command}.") from exc
        self._audit.append(QueryRecord(command, None, started, datetime.now(UTC)))

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
