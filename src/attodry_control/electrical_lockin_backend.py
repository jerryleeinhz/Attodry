"""Electrical SR865A receiver bridge for the existing semantic pair engine.

Native SCAL/OFLT/RSRC codes and native status words remain model-specific.
Only lockin_xx owns excitation; an SR865A XY receiver never writes its source,
stored oscillator, DC offset or BlazeX output. Construction performs no I/O.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
import math
import time

from .models import LockinRole
from .sr830 import (AuthorizationRequired, DualSr830Controller, Sr830, Sr830Error,
                    MINIMUM_SINE_OUTPUT_V, PAIR_FREQUENCY_ABS_TOLERANCE_HZ)
from .sr865a import QueryRecord, Sr865a, Sr865aError, Sr865aStatus
from . import sr865a_settings as settings


class ElectricalLockinError(Sr830Error):
    """Unverified electrical settings, native evidence or receiver ownership."""

    def __init__(self, message, *, evidence=None, raw=()):
        super().__init__(message)
        self.evidence = evidence
        self.raw = raw


def _json(value):
    if is_dataclass(value):
        return _json(asdict(value))
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class ElectricalLiaStatus:
    """Named faults for orchestration; ``raw`` is an SR865A word, never SR830.

    SR830-only frequency/OFLT flags have no native SR865A equivalent. Generic
    configuration changes and unknown evidence must be checked independently.
    """
    raw: int | None
    input_or_reserve_overload: bool
    filter_overload: bool
    output_overload: bool
    reference_unlocked: bool
    frequency_range_changed: bool = False
    time_constant_changed: bool = False
    triggered: bool | None = None
    model: str = "SR865A"
    status_known: bool = False
    unknown_status_bits: int = 0
    configuration_changed_latched: bool | None = None
    power_on_latched: bool | None = None
    instrument_error: bool | None = None
    native_status: Sr865aStatus | None = None

    @property
    def any_overload(self) -> bool:
        return self.input_or_reserve_overload or self.filter_overload or self.output_overload


def electrical_status(native: Sr865aStatus) -> ElectricalLiaStatus:
    return ElectricalLiaStatus(
        raw=native.lia_status_raw,
        input_or_reserve_overload=(native.input_overload is True or native.input_overload_latched is True),
        filter_overload=native.filter_fault_latched is True,
        output_overload=(native.output_scale_overload is True or native.output_scale_overload_latched is True),
        reference_unlocked=(native.locked is False or native.reference_unlock_latched is True),
        status_known=(native.locked is not None and native.instrument_error is not None
                      and native.consumed_status_latches),
        unknown_status_bits=native.unknown_status_bits,
        configuration_changed_latched=native.configuration_changed_latched,
        power_on_latched=native.power_on_latched,
        instrument_error=native.instrument_error,
        native_status=native,
    )


def native_status_problems(status: ElectricalLiaStatus) -> list[str]:
    """Non-overload faults cannot be excused by an acquisition overload policy."""
    problems = []
    if not status.status_known:
        problems.append("safety status is incomplete")
    if status.unknown_status_bits:
        problems.append("unknown native status bits")
    if status.configuration_changed_latched:
        problems.append("configuration changed")
    if status.power_on_latched:
        problems.append("instrument power-on")
    if status.instrument_error:
        problems.append("instrument error")
    return problems


@dataclass(frozen=True, slots=True)
class ElectricalLockinReading:
    role: LockinRole
    harmonic: int
    x_v: float
    y_v: float
    amplitude_v: float
    phase_deg: float | None
    phase_shift_deg: float
    frequency_hz: float
    locked: bool | None
    overload: bool | None
    model: str = "SR865A"
    detection_frequency_hz: float | None = None
    amplitude_phase_source: str = "derived_from_simultaneous_xy"
    frequencies_are_sequential: bool = True


@dataclass(frozen=True, slots=True)
class ElectricalHarmonicSample:
    reading: ElectricalLockinReading
    lia_status: ElectricalLiaStatus
    error_status: int | None
    captured_at_utc: datetime
    model: str
    native_sample: object
    raw: tuple[QueryRecord, ...]


@dataclass(frozen=True, slots=True)
class ElectricalDiagnostic:
    role: LockinRole
    identity: str
    reference_mode: int
    reference_slope: int
    frequency_hz: float
    harmonic: int
    sine_output_v: float
    input_mode: int
    shield_grounding: int
    input_coupling: int
    line_filter: None
    sensitivity: int
    reserve_mode: None
    time_constant: int
    filter_slope: int
    phase_shift_deg: float
    x_v: float
    y_v: float
    amplitude_v: float
    phase_deg: float | None
    snapshot_frequency_hz: float
    lia_status: ElectricalLiaStatus | None
    error_status: int | None
    model: str
    reference_source: str
    input_range_v_peak: float
    sensitivity_full_scale_v: float
    time_constant_s: float
    detection_frequency_hz: float
    native_settings: object
    native_status: Sr865aStatus
    raw: tuple[QueryRecord, ...]

    @property
    def locked(self) -> bool | None:
        return self.native_status.locked

    @property
    def overload(self) -> bool | None:
        if self.lia_status is None or not self.lia_status.status_known:
            return None
        return self.lia_status.any_overload


_FIXED_CODES = {
    "reference_mode": ("RSRC", (0, 1, 2, 3)),
    "reference_slope": ("RTRG", (0, 1, 2)),
    "reference_input_impedance": ("REFZ", (0, 1)),
    "input_mode": ("ISRC", (0, 1)),
    "shield_grounding": ("IGND", (0, 1)),
    "input_coupling": ("ICPL", (0, 1)),
    "time_constant": ("OFLT", tuple(range(22))),
    "filter_slope": ("OFSL", (0, 1, 2, 3)),
    "advanced_filter": ("ADVFILT", (0, 1)),
    "synchronous_filter": ("SYNC", (0, 1)),
}


class Sr865aReceiver:
    model = "SR865A"

    def __init__(self, resource, role=LockinRole.XY, *, config=None, authorize_writes=False):
        if role is not LockinRole.XY:
            raise ValueError("Electrical SR865A is supported only as lockin_xy receiver.")
        if type(authorize_writes) is not bool:
            raise ValueError("Write authorization must be an explicit boolean.")
        self.role = role
        self._resource = resource
        self._driver = Sr865a(resource, role)
        self.config = config
        self._receiver_config = getattr(config, "sr865a", config)
        self.authorize_writes = authorize_writes
        self._disconnected_source_baseline = None
        self.current_status_supported = getattr(self._receiver_config, "current_status_supported", False)
        if type(self.current_status_supported) is not bool:
            raise ValueError("Current-status support must be an explicit boolean.")

    @property
    def audit(self):
        return self._driver.audit

    def _call(self, method, *args, **kwargs):
        try:
            return method(*args, **kwargs)
        except Sr865aError as exc:
            raise ElectricalLockinError(str(exc), raw=self.audit) from exc

    def _authorized(self):
        if not self.authorize_writes:
            raise AuthorizationRequired("Electrical receiver setting writes were not authorized.")
        self._call(self._driver._authorize, True)

    def query_identity(self):
        return self._call(self._driver.query_identity)

    def read_diagnostic(self, *, consume_status_latches=False):
        start = len(self.audit)
        native = self._call(self._driver.read_settings)
        sample = self._call(self._driver.read_sample,
                            consume_status_latches=consume_status_latches,
                            current_status_supported=self.current_status_supported)
        status = electrical_status(sample.status) if consume_status_latches else None
        return ElectricalDiagnostic(
            self.role, native.identity, ("internal", "external", "dual", "chop").index(native.reference_source),
            ("sine", "rising", "falling").index(native.external_reference_edge),
            sample.reference_frequency_hz, sample.harmonic, native.sine_output_v,
            ("a", "a_minus_b").index(native.input_mode),
            ("float", "ground").index(native.shield_grounding),
            ("ac", "dc").index(native.input_coupling), None,
            settings.sensitivity_code(native.sensitivity_full_scale_v), None,
            settings.time_constant_code(native.time_constant_s),
            settings.filter_slope_code(native.filter_slope_db_oct), native.phase_shift_deg,
            sample.x_v, sample.y_v, sample.amplitude_v, sample.phase_deg,
            sample.reference_frequency_hz, status, sample.status.error_status_raw, self.model,
            native.reference_source, native.input_range_v_peak, native.sensitivity_full_scale_v,
            native.time_constant_s, sample.detection_frequency_hz, native, sample.status,
            self.audit[start:],
        )

    def read_harmonic_sample(self, expected_harmonic):
        if type(expected_harmonic) is not int or expected_harmonic not in (1, 2, 3):
            raise ValueError("Electrical acquisition supports harmonics 1, 2 and 3 only.")
        start = len(self.audit)
        native = self._call(self._driver.read_sample, consume_status_latches=True,
                            current_status_supported=self.current_status_supported)
        if native.harmonic != expected_harmonic:
            raise ElectricalLockinError("SR865A harmonic readback differs from requested harmonic.")
        phase = self._call(self._driver._query_float, "PHAS?")
        status = electrical_status(native.status)
        reading = ElectricalLockinReading(self.role, native.harmonic, native.x_v, native.y_v,
            native.amplitude_v, native.phase_deg, phase, native.reference_frequency_hz,
            native.status.locked, None if not status.status_known else status.any_overload,
            detection_frequency_hz=native.detection_frequency_hz)
        return ElectricalHarmonicSample(reading, status, native.status.error_status_raw,
                                       native.captured_at_utc, self.model, native, self.audit[start:])

    def read_status_latches(self):
        native = self._call(self._driver.read_status, consume_status_latches=True,
                            current_status_supported=self.current_status_supported)
        return electrical_status(native), native.error_status_raw

    def read_sensitivity(self):
        return settings.sensitivity_code(self._call(self._driver.read_sensitivity))

    def set_sensitivity(self, code):
        physical = settings.sensitivity_full_scale_v(code)
        self._authorized()
        if self.read_sensitivity() == code:
            return physical
        return self._call(self._driver.set_sensitivity, physical, authorized=True)

    def read_time_constant(self):
        return settings.time_constant_code(self._call(self._driver.read_time_constant))

    def read_filter_slope(self):
        return self.read_fixed_setting("filter_slope")

    def read_phase_shift(self):
        return self._call(self._driver._query_float, "PHAS?")

    def read_harmonic(self):
        return self._call(self._driver.read_harmonic)

    def set_harmonic(self, harmonic):
        if type(harmonic) is not int or harmonic not in (1, 2, 3):
            raise ValueError("Electrical acquisition supports harmonics 1, 2 and 3 only.")
        self._authorized()
        settings.validate_harmonic_frequency(harmonic, self.read_reference_frequency())
        if self.read_harmonic() == harmonic:
            return harmonic
        return self._call(self._driver.set_harmonic, harmonic, authorized=True)

    def read_reference_frequency(self):
        return self._call(self._driver.read_reference_frequency)

    def read_sine_output(self):
        return self._call(self._driver.read_sine_output)

    def read_reserve_mode(self):
        return None

    def read_fixed_setting(self, field):
        if field not in _FIXED_CODES:
            raise ValueError(f"SR865A has no electrical fixed setting {field!r}.")
        command, allowed = _FIXED_CODES[field]
        value = self._call(self._driver._query_int, command + "?")
        if value not in allowed:
            self._driver._identity = None
            raise ElectricalLockinError(f"Invalid SR865A {command} code {value}.")
        return value

    def set_fixed_setting(self, field, code):
        if field not in _FIXED_CODES or type(code) is not int or code not in _FIXED_CODES[field][1]:
            raise ValueError("Unsupported native SR865A fixed-setting code.")
        if field == "reference_mode" and code != 1:
            raise ValueError("Electrical XY must remain on external reference.")
        if field == "reference_slope" and code not in (1, 2):
            raise ValueError("Electrical XY must use an explicit TTL edge.")
        self._authorized()
        self._call(self._driver._verify_voltage_input)
        previous = self.read_fixed_setting(field)
        if previous == code:
            return
        command = f"{_FIXED_CODES[field][0]} {code}"
        started = datetime.now(UTC)
        try:
            self._resource.write(command)
        except BaseException as exc:
            self._driver._identity = None
            self._driver._audit.append(QueryRecord(command, None, started, datetime.now(UTC), repr(exc)))
            raise
        self._driver._audit.append(QueryRecord(command, None, started, datetime.now(UTC)))
        if self.read_fixed_setting(field) != code:
            self._driver._identity = None
            raise ElectricalLockinError("SR865A fixed-setting readback mismatch.")

    def write_fixed_settings(self, requested):
        """Apply validated native input/filter/SCAL codes, preserving reference and source."""
        if requested.model != self.model:
            raise ValueError("Fixed-setting codes belong to a different lock-in model.")
        if type(requested.reference_source) is not int or requested.reference_source != 1:
            raise ValueError("Electrical receiver fixed settings require native external reference.")
        if type(requested.external_reference_edge) is not int or requested.external_reference_edge not in (1, 2):
            raise ValueError("Electrical receiver fixed settings require an explicit native TTL edge.")
        fields = ("input_mode", "shield_grounding", "input_coupling", "time_constant", "filter_slope")
        for field in fields:
            code = getattr(requested, field)
            if type(code) is not int or code not in _FIXED_CODES[field][1]:
                raise ValueError(f"Unsupported native SR865A {field} code.")
        settings.sensitivity_full_scale_v(requested.sensitivity)
        self._authorized()
        if self.read_fixed_setting("reference_mode") != requested.reference_source or (
            self.read_fixed_setting("reference_slope") != requested.external_reference_edge
        ):
            raise ElectricalLockinError("Configure and verify the external reference before fixed settings.")
        for field in fields:
            self.set_fixed_setting(field, getattr(requested, field))
        self.set_sensitivity(requested.sensitivity)

    def configure_receiver(self):
        config = self._receiver_config
        if config is None or getattr(config, "sync_output_mode", None) != "preserve":
            raise ValueError("Receiver requires explicit native settings and preserved BlazeX output.")
        settings.input_range_code(config.input_range_v_peak)
        if config.reference_input_impedance_ohm not in (50.0, 1_000_000.0):
            raise ValueError("Receiver requires an explicit 50 or 1000000 ohm reference input.")
        edge = getattr(self.config, "external_reference_edge", "rising")
        edge = getattr(edge, "value", edge)
        if edge not in ("rising", "falling"):
            raise ValueError("Receiver requires an explicit TTL edge.")
        self._authorized()
        start = len(self.audit)
        source_before = self._call(self._driver.read_settings)
        self._verify_disconnected_source(source_before)
        if self._disconnected_source_baseline is None:
            self._disconnected_source_baseline = {
                field: getattr(source_before, field) for field in
                ("sine_output_v", "source_offset_v", "source_dc_mode", "sync_output_mode", "phase_shift_deg")
            }
        self.set_fixed_setting("reference_input_impedance", 0 if config.reference_input_impedance_ohm == 50.0 else 1)
        self.set_fixed_setting("reference_slope", 1 if edge == "rising" else 2)
        self.set_fixed_setting("reference_mode", 1)
        if source_before.input_range_v_peak != config.input_range_v_peak:
            self._call(self._driver.set_input_range, config.input_range_v_peak, authorized=True)
        self.set_fixed_setting("advanced_filter", 0)
        self.set_fixed_setting("synchronous_filter", 0)
        source_after = self._call(self._driver.read_settings)
        self._verify_disconnected_source(source_after)
        return {"before": _json(source_before), "after": _json(source_after), "raw": _json(self.audit[start:]),
                "source_output_policy": "preserve_disconnected_receiver", "source_write_attempted": False}

    def _verify_disconnected_source(self, native):
        baseline = self._disconnected_source_baseline
        if baseline is not None and any(getattr(native, field) != value for field, value in baseline.items()):
            raise ElectricalLockinError("Disconnected receiver source/output state changed; manual verification required.",
                evidence={"source_output_baseline": dict(baseline), "actual": _json(native)}, raw=self.audit)

    def read_receiver_invariants(self):
        """Read native/physical measurement and preserved source evidence, without writes."""
        native = self._call(self._driver.read_settings)
        self._verify_disconnected_source(native)
        result = _json(native)
        result["source_output_baseline"] = (
            None if self._disconnected_source_baseline is None else dict(self._disconnected_source_baseline))
        result["source_output_policy"] = "preserve_disconnected_receiver"
        return result

    def _reject_source(self, *args, **kwargs):
        raise ElectricalLockinError("Only lockin_xx owns electrical excitation/source frequency writes.")

    set_sine_output = _reject_source
    set_minimum_sine_output = _reject_source
    set_internal_reference_frequency = _reject_source
    configure_xx_minimum_excitation = _reject_source
    set_reserve_mode = _reject_source

    def clear_interface(self):
        self._resource.clear()

    def close(self):
        self._driver.close()


def create_electrical_lockin(resource, config, *, authorize_writes=False):
    if config.model == "SR830":
        driver = Sr830(resource, config.role)
        driver.model = "SR830"
        return driver
    if config.model == "SR865A" and config.role is LockinRole.XY:
        return Sr865aReceiver(resource, config.role, config=config, authorize_writes=authorize_writes)
    raise ValueError("Electrical pair requires XX SR830 and XY SR830 or SR865A.")


class ElectricalPairController:
    def __init__(self, xx, xy):
        if xx.role is not LockinRole.XX or xy.role is not LockinRole.XY:
            raise ElectricalLockinError("Electrical pair requires semantic XX then XY roles.")
        if getattr(xx, "model", "SR830") != "SR830" or getattr(xy, "model", "SR830") != "SR865A":
            raise ElectricalLockinError("Mixed electrical pair requires XX SR830 and XY SR865A.")
        self.lockin_xx, self.lockin_xy = xx, xy

    def verify_existing_configuration(self, *, frequency_hz, check_frequency=True,
                                      ignore_output_overload=False, transient_overload_recheck_s=0.0):
        if isinstance(frequency_hz, bool) or not isinstance(frequency_hz, (int, float)) or not (
            math.isfinite(frequency_hz) and 0.001 <= frequency_hz <= 102_000.0
        ):
            raise ValueError("Electrical source frequency must be within the SR830 capability.")
        if isinstance(transient_overload_recheck_s, bool) or not math.isfinite(transient_overload_recheck_s) or transient_overload_recheck_s < 0:
            raise ValueError("Transient recheck interval must be finite and nonnegative.")

        def read_and_check():
            xx = self.lockin_xx.read_diagnostic(consume_status_latches=True)
            xy = self.lockin_xy.read_diagnostic(consume_status_latches=True)
            problems = []
            fields = tuple(part.strip() for part in xx.identity.split(","))
            if len(fields) != 4 or fields[:2] != ("Stanford_Research_Systems", "SR830") or (
                not fields[2] or not fields[3]
            ):
                problems.append("lockin_xx unexpected instrument identity")
            if xx.identity == xy.identity:
                problems.append("duplicate physical instrument identity")
            if xx.reference_mode != 1 or xy.reference_source != "external":
                problems.append("electrical reference topology differs from XX internal / XY external")
            expected_edge = getattr(self.lockin_xy.config, "external_reference_edge", "rising")
            expected_edge = getattr(expected_edge, "value", expected_edge)
            if xy.reference_slope != (1 if expected_edge == "rising" else 2):
                problems.append("lockin_xy external TTL edge differs from configuration")
            if xx.harmonic != 1 or xy.harmonic != 1:
                problems.append("both lock-ins must enter at first harmonic")
            if not math.isclose(xx.sine_output_v, MINIMUM_SINE_OUTPUT_V, rel_tol=0, abs_tol=1e-9):
                problems.append("lockin_xx is not at the 4 mVrms minimum")
            for role, diagnostic in (("lockin_xx", xx), ("lockin_xy", xy)):
                for actual in (diagnostic.frequency_hz, diagnostic.snapshot_frequency_hz):
                    if not math.isfinite(actual) or not 0.001 <= actual <= 102_000.0:
                        problems.append(f"{role} invalid electrical reference frequency")
                    elif check_frequency and not math.isclose(actual, frequency_hz, rel_tol=1e-5,
                                                                              abs_tol=PAIR_FREQUENCY_ABS_TOLERANCE_HZ):
                        problems.append(f"{role} reference frequency mismatch")
                status = diagnostic.lia_status
                if status is None or diagnostic.error_status is None:
                    problems.append(f"{role} safety status is incomplete")
                    continue
                if role == "lockin_xy":
                    problems.extend(f"{role} {problem}" for problem in native_status_problems(status))
                elif status.raw & 0x80:
                    problems.append(f"{role} unknown native status bits")
                if status.reference_unlocked:
                    problems.append(f"{role} reference unlocked")
                if status.input_or_reserve_overload:
                    problems.append(f"{role} input/reserve overload")
                if status.filter_overload:
                    problems.append(f"{role} filter overload")
                if status.output_overload and (role == "lockin_xy" or not ignore_output_overload):
                    problems.append(f"{role} output-scale overload")
                if diagnostic.error_status:
                    problems.append(f"{role} instrument error {diagnostic.error_status}")
            return (xx, xy), problems

        diagnostics, problems = read_and_check()
        if problems and transient_overload_recheck_s > 0 and all(
            "input/reserve overload" in problem or "filter overload" in problem for problem in problems
        ):
            time.sleep(transient_overload_recheck_s)
            diagnostics, problems = read_and_check()
        if problems:
            raise ElectricalLockinError("Electrical preflight rejected: " + "; ".join(problems))
        return diagnostics

    def authorize_existing_configuration(self, *, frequency_hz, authorize_writes,
                                         confirm_xy_sine_disconnected):
        if authorize_writes is not True or confirm_xy_sine_disconnected is not True:
            raise AuthorizationRequired("Electrical harmonic writes and XY disconnection require authorization.")
        return self.verify_existing_configuration(frequency_hz=frequency_hz)


def pair_controller(xx, xy):
    if getattr(xy, "model", "SR830") == "SR830":
        return DualSr830Controller(xx, xy)
    return ElectricalPairController(xx, xy)
