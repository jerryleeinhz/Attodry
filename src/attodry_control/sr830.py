from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import math
import time
from typing import Callable, Protocol

from .lockin_autorange import AutorangeAction, AutorangeDecision
from .models import LockinReading, LockinRole
from .sr830_settings import Sr830SettingCodes, sensitivity_code


MINIMUM_SINE_OUTPUT_V = 0.004
MAXIMUM_SINE_OUTPUT_V = 5.0
MAXIMUM_REFERENCE_FREQUENCY_HZ = 102_000.0
# Two sequential SR830 readbacks can differ by one 1 mHz display step.
PAIR_FREQUENCY_ABS_TOLERANCE_HZ = 0.001_01
MINIMUM_FIXED_SETTINGS_SETTLE_S = 1.5

# Semantic names are parsed in the configuration layer; these are the SR830
# integer codes used by RMOD/RMOD?.
RESERVE_MODE_CODES = {
    "high_reserve": 0,
    "normal": 1,
    "low_noise": 2,
}


class Sr830Error(RuntimeError):
    """Raised when an SR830 response or verified state is invalid."""


class AuthorizationRequired(Sr830Error):
    """Raised before a hardware-setting write without explicit authorization."""


class Sr830AcquisitionError(Sr830Error):
    def __init__(
        self, message: str, partial_samples: tuple[Sr830HarmonicSample, ...] = ()
    ) -> None:
        super().__init__(message)
        self.partial_samples = partial_samples

    @property
    def partial_readings(self) -> tuple[LockinReading, ...]:
        return tuple(sample.reading for sample in self.partial_samples)


class VisaResource(Protocol):
    def query(self, command: str) -> str: ...

    def write(self, command: str) -> object: ...

    def clear(self) -> object: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class LiaStatus:
    raw: int
    input_or_reserve_overload: bool
    filter_overload: bool
    output_overload: bool
    reference_unlocked: bool
    frequency_range_changed: bool
    time_constant_changed: bool
    triggered: bool

    @property
    def any_overload(self) -> bool:
        return (
            self.input_or_reserve_overload
            or self.filter_overload
            or self.output_overload
        )


@dataclass(frozen=True, slots=True)
class Sr830Diagnostic:
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
    line_filter: int
    sensitivity: int
    reserve_mode: int
    time_constant: int
    filter_slope: int
    phase_shift_deg: float
    x_v: float
    y_v: float
    amplitude_v: float
    phase_deg: float
    snapshot_frequency_hz: float
    lia_status: LiaStatus | None
    error_status: int | None

    @property
    def locked(self) -> bool | None:
        if self.lia_status is None:
            return None
        return not self.lia_status.reference_unlocked

    @property
    def overload(self) -> bool | None:
        if self.lia_status is None:
            return None
        return self.lia_status.any_overload


@dataclass(frozen=True, slots=True)
class PairConfigurationResult:
    before_xx: Sr830Diagnostic
    before_xy: Sr830Diagnostic
    after_xx: Sr830Diagnostic
    after_xy: Sr830Diagnostic


@dataclass(frozen=True, slots=True)
class FixedSettingsConfigurationResult:
    before_xx: Sr830Diagnostic
    before_xy: Sr830Diagnostic
    after_xx: Sr830Diagnostic
    after_xy: Sr830Diagnostic


@dataclass(frozen=True, slots=True)
class Sr830HarmonicSample:
    reading: LockinReading
    lia_status: LiaStatus
    error_status: int
    captured_at_utc: datetime

    def __post_init__(self) -> None:
        if self.captured_at_utc.tzinfo is None or self.captured_at_utc.utcoffset() is None:
            raise ValueError("captured_at_utc must be timezone-aware UTC.")
        if self.captured_at_utc.utcoffset().total_seconds() != 0:
            raise ValueError("captured_at_utc must use UTC.")


@dataclass(frozen=True, slots=True)
class AutorangeTransitionResult:
    decision: AutorangeDecision
    previous_sensitivity_code: int
    final_sensitivity_code: int
    transition_sample: Sr830HarmonicSample | None
    verification_sample: Sr830HarmonicSample | None
    formal_range_frozen: bool = True


@dataclass(frozen=True, slots=True)
class DualSr830Measurement:
    samples: tuple[Sr830HarmonicSample, ...]
    pair_reads_are_sequential: bool = True

    @property
    def readings(self) -> tuple[LockinReading, ...]:
        return tuple(sample.reading for sample in self.samples)


class Sr830:
    """Small SR830 command adapter with no implicit configuration writes."""

    def __init__(self, resource: VisaResource, role: LockinRole):
        self._resource = resource
        self.role = role

    def query_identity(self) -> str:
        identity = self._query_text("*IDN?")
        if "Stanford_Research_Systems,SR830," not in identity:
            raise Sr830Error(
                f"{self.role.value} expected an SR830 but received IDN {identity!r}."
            )
        return identity

    def read_diagnostic(
        self, *, consume_status_latches: bool
    ) -> Sr830Diagnostic:
        identity = self.query_identity()
        reference_mode = self._query_int("FMOD?")
        reference_slope = self._query_int("RSLP?")
        frequency_hz = self._query_float("FREQ?")
        harmonic = self._query_int("HARM?")
        sine_output_v = self._query_float("SLVL?")
        input_mode = self._query_int("ISRC?")
        shield_grounding = self._query_int("IGND?")
        input_coupling = self._query_int("ICPL?")
        line_filter = self._query_int("ILIN?")
        sensitivity = self.read_sensitivity()
        reserve_mode = self.read_reserve_mode()
        time_constant = self.read_time_constant()
        filter_slope = self.read_filter_slope()
        phase_shift_deg = self.read_phase_shift()
        snapshot = self._query_csv_floats("SNAP? 1,2,3,4,9", expected=5)
        lia_status = None
        error_status = None
        if consume_status_latches:
            lia_status = decode_lia_status(self._query_int("LIAS?"))
            error_status = self._query_int("ERRS?")
        return Sr830Diagnostic(
            role=self.role,
            identity=identity,
            reference_mode=reference_mode,
            reference_slope=reference_slope,
            frequency_hz=frequency_hz,
            harmonic=harmonic,
            sine_output_v=sine_output_v,
            input_mode=input_mode,
            shield_grounding=shield_grounding,
            input_coupling=input_coupling,
            line_filter=line_filter,
            sensitivity=sensitivity,
            reserve_mode=reserve_mode,
            time_constant=time_constant,
            filter_slope=filter_slope,
            phase_shift_deg=phase_shift_deg,
            x_v=snapshot[0],
            y_v=snapshot[1],
            amplitude_v=snapshot[2],
            phase_deg=snapshot[3],
            snapshot_frequency_hz=snapshot[4],
            lia_status=lia_status,
            error_status=error_status,
        )

    def set_minimum_sine_output(self) -> None:
        self._resource.write(f"SLVL {MINIMUM_SINE_OUTPUT_V:.3f}")

    def set_sine_output(self, amplitude_v: float) -> None:
        if (
            not math.isfinite(amplitude_v)
            or amplitude_v < MINIMUM_SINE_OUTPUT_V
            or amplitude_v > MAXIMUM_SINE_OUTPUT_V
        ):
            raise ValueError("SINE OUT must be finite and within 0.004-5 V RMS.")
        self._resource.write(f"SLVL {amplitude_v:g}")

    def read_sine_output(self) -> float:
        return self._query_float("SLVL?")

    def clear_interface(self) -> None:
        """Discard pending VISA I/O without changing SR830 settings.

        PyVISA exposes this as ``Resource.clear``.  A missing method is treated
        as an error instead of silently claiming that a possibly stale response
        queue was recovered.
        """

        clear = getattr(self._resource, "clear", None)
        if not callable(clear):
            raise Sr830Error(
                f"lockin_{self.role.value} VISA resource does not support interface clear."
            )
        clear()

    def set_internal_reference_frequency(self, frequency_hz: float) -> None:
        if self.role is not LockinRole.XX:
            raise Sr830Error("Internal reference frequency may only be set on lockin_xx.")
        if (
            not math.isfinite(frequency_hz)
            or frequency_hz < 0.001
            or frequency_hz > MAXIMUM_REFERENCE_FREQUENCY_HZ
        ):
            raise ValueError("Reference frequency must be within 0.001-102000 Hz.")
        self._resource.write(f"FREQ {frequency_hz:.12g}")

    def read_reference_frequency(self) -> float:
        return self._query_float("FREQ?")

    def set_sensitivity(self, code: int) -> None:
        if not 0 <= code <= 26:
            raise ValueError("SR830 sensitivity code must be between 0 and 26.")
        self._resource.write(f"SENS {code}")

    def read_sensitivity(self) -> int:
        code = self._query_int("SENS?")
        if not 0 <= code <= 26:
            raise Sr830Error(
                f"lockin_{self.role.value} returned invalid sensitivity code {code}."
            )
        return code

    def set_reserve_mode(self, code: int) -> None:
        if code not in (0, 1, 2):
            raise ValueError(
                "SR830 reserve mode code must be 0 (high reserve), 1 (normal), "
                "or 2 (low noise)."
            )
        self._resource.write(f"RMOD {code}")

    def read_reserve_mode(self) -> int:
        code = self._query_int("RMOD?")
        if code not in (0, 1, 2):
            raise Sr830Error(
                f"lockin_{self.role.value} returned invalid reserve mode code {code}."
            )
        return code

    def read_time_constant(self) -> int:
        code = self._query_int("OFLT?")
        if not 0 <= code <= 19:
            raise Sr830Error(
                f"lockin_{self.role.value} returned invalid time-constant code {code}."
            )
        return code

    def read_filter_slope(self) -> int:
        code = self._query_int("OFSL?")
        if not 0 <= code <= 3:
            raise Sr830Error(
                f"lockin_{self.role.value} returned invalid filter-slope code {code}."
            )
        return code

    def read_phase_shift(self) -> float:
        return self._query_float("PHAS?")

    def write_fixed_settings(self, settings: Sr830SettingCodes) -> None:
        _validate_fixed_settings_role(self.role, settings)
        self._resource.write(f"ISRC {settings.input_mode}")
        self._resource.write(f"IGND {settings.shield_grounding}")
        self._resource.write(f"ICPL {settings.input_coupling}")
        self._resource.write(f"OFLT {settings.time_constant}")
        self._resource.write(f"OFSL {settings.filter_slope}")
        self._resource.write(f"SENS {settings.sensitivity}")

    def set_harmonic(self, harmonic: int) -> None:
        if harmonic not in (1, 2, 3):
            raise ValueError("Only harmonics 1, 2, and 3 are supported.")
        self._resource.write(f"HARM {harmonic}")

    def read_harmonic(self) -> int:
        harmonic = self._query_int("HARM?")
        if harmonic not in (1, 2, 3):
            raise Sr830Error(
                f"lockin_{self.role.value} returned unsupported harmonic {harmonic}."
            )
        return harmonic

    def read_harmonic_sample(self, expected_harmonic: int) -> Sr830HarmonicSample:
        harmonic = self.read_harmonic()
        if harmonic != expected_harmonic:
            raise Sr830Error(
                f"lockin_{self.role.value} harmonic readback is {harmonic}; "
                f"expected {expected_harmonic}."
            )
        phase_shift_deg = self.read_phase_shift()
        snapshot = self._query_csv_floats("SNAP? 1,2,3,4,9", expected=5)
        captured_at_utc = datetime.now(UTC)
        lia_status = decode_lia_status(self._query_int("LIAS?"))
        error_status = self._query_int("ERRS?")
        reading = LockinReading(
            role=self.role,
            harmonic=harmonic,
            x_v=snapshot[0],
            y_v=snapshot[1],
            amplitude_v=snapshot[2],
            phase_deg=snapshot[3],
            phase_shift_deg=phase_shift_deg,
            frequency_hz=snapshot[4],
            locked=not lia_status.reference_unlocked,
            overload=lia_status.any_overload,
        )
        return Sr830HarmonicSample(
            reading, lia_status, error_status, captured_at_utc
        )

    def configure_xx_minimum_excitation(self, frequency_hz: float) -> None:
        if self.role is not LockinRole.XX:
            raise Sr830Error("Internal excitation may only be configured for lockin_xx.")
        self._resource.write("FMOD 1")
        self._resource.write("HARM 1")
        self.set_internal_reference_frequency(frequency_hz)

    def configure_xy_external_ttl(self) -> None:
        if self.role is not LockinRole.XY:
            raise Sr830Error("External TTL reference may only be configured for lockin_xy.")
        self._resource.write("FMOD 0")
        self._resource.write("RSLP 1")
        self._resource.write("HARM 1")

    def close(self) -> None:
        self._resource.close()

    def _query_text(self, command: str) -> str:
        response = self._resource.query(command).strip()
        if not response:
            raise Sr830Error(f"{self.role.value} returned an empty response to {command}.")
        return response

    def _query_int(self, command: str) -> int:
        response = self._query_text(command)
        try:
            return int(response)
        except ValueError as exc:
            raise Sr830Error(
                f"{self.role.value} returned non-integer {response!r} to {command}."
            ) from exc

    def _query_float(self, command: str) -> float:
        response = self._query_text(command)
        try:
            value = float(response)
        except ValueError as exc:
            raise Sr830Error(
                f"{self.role.value} returned non-numeric {response!r} to {command}."
            ) from exc
        if not math.isfinite(value):
            raise Sr830Error(
                f"{self.role.value} returned non-finite {response!r} to {command}."
            )
        return value

    def _query_csv_floats(self, command: str, expected: int) -> tuple[float, ...]:
        response = self._query_text(command)
        parts = response.split(",")
        if len(parts) != expected:
            raise Sr830Error(
                f"{self.role.value} returned {len(parts)} values to {command}; "
                f"expected {expected}."
            )
        try:
            values = tuple(float(part) for part in parts)
        except ValueError as exc:
            raise Sr830Error(
                f"{self.role.value} returned invalid numeric data to {command}: "
                f"{response!r}."
            ) from exc
        if any(not math.isfinite(value) for value in values):
            raise Sr830Error(
                f"{self.role.value} returned non-finite data to {command}: {response!r}."
            )
        return values


def decode_lia_status(raw: int) -> LiaStatus:
    if not 0 <= raw <= 255:
        raise Sr830Error(f"LIA status byte must be between 0 and 255, got {raw}.")
    return LiaStatus(
        raw=raw,
        input_or_reserve_overload=bool(raw & (1 << 0)),
        filter_overload=bool(raw & (1 << 1)),
        output_overload=bool(raw & (1 << 2)),
        reference_unlocked=bool(raw & (1 << 3)),
        frequency_range_changed=bool(raw & (1 << 4)),
        time_constant_changed=bool(raw & (1 << 5)),
        triggered=bool(raw & (1 << 6)),
    )


def configure_minimum_excitation_pair(
    lockin_xx: Sr830,
    lockin_xy: Sr830,
    *,
    frequency_hz: float,
    authorize_writes: bool,
    confirm_xy_sine_disconnected: bool,
) -> PairConfigurationResult:
    """Configure only the confirmed reference wiring at minimum sine output."""

    if not authorize_writes:
        raise AuthorizationRequired("SR830 setting writes were not explicitly authorized.")
    if not confirm_xy_sine_disconnected:
        raise AuthorizationRequired(
            "Physical disconnection of lockin_xy SINE OUT was not confirmed."
        )
    if lockin_xx.role is not LockinRole.XX or lockin_xy.role is not LockinRole.XY:
        raise Sr830Error("The SR830 pair must be supplied as lockin_xx then lockin_xy.")
    if (
        not math.isfinite(frequency_hz)
        or frequency_hz < 0.001
        or frequency_hz > MAXIMUM_REFERENCE_FREQUENCY_HZ
    ):
        raise Sr830Error("Reference frequency must be finite and within 0.001-102000 Hz.")

    before_xx = lockin_xx.read_diagnostic(consume_status_latches=False)
    before_xy = lockin_xy.read_diagnostic(consume_status_latches=False)
    if before_xx.identity == before_xy.identity:
        raise Sr830Error(
            "Both semantic roles returned the same SR830 identity; verify that the "
            "VISA addresses refer to two distinct physical instruments."
        )
    writes_started = False
    try:
        writes_started = True
        lockin_xx.set_minimum_sine_output()
        lockin_xy.set_minimum_sine_output()
        lockin_xx.configure_xx_minimum_excitation(frequency_hz)
        lockin_xy.configure_xy_external_ttl()
        xx_diagnostic = lockin_xx.read_diagnostic(consume_status_latches=False)
        xy_diagnostic = lockin_xy.read_diagnostic(consume_status_latches=False)
        verify_pair_readback(xx_diagnostic, xy_diagnostic, frequency_hz)
        return PairConfigurationResult(
            before_xx=before_xx,
            before_xy=before_xy,
            after_xx=xx_diagnostic,
            after_xy=xy_diagnostic,
        )
    except BaseException as exc:
        if writes_started:
            cleanup_errors = _attempt_minimum_output(lockin_xx, lockin_xy)
            for error in cleanup_errors:
                exc.add_note(f"Minimum-output cleanup also failed: {error}")
        raise


def configure_fixed_settings_pair(
    lockin_xx: Sr830,
    lockin_xy: Sr830,
    *,
    xx_settings: Sr830SettingCodes,
    xy_settings: Sr830SettingCodes,
    expected_frequency_hz: float,
    settle_s: float,
    sleeper: Callable[[float], None],
    authorize_writes: bool,
    confirm_xy_sine_disconnected: bool,
) -> FixedSettingsConfigurationResult:
    """Apply only the confirmed fixed input/filter/range settings to both roles."""

    if not authorize_writes:
        raise AuthorizationRequired(
            "SR830 fixed-setting writes were not explicitly authorized."
        )
    if not confirm_xy_sine_disconnected:
        raise AuthorizationRequired(
            "Physical disconnection of lockin_xy SINE OUT was not confirmed."
        )
    if lockin_xx.role is not LockinRole.XX or lockin_xy.role is not LockinRole.XY:
        raise Sr830Error("The SR830 pair must be supplied as lockin_xx then lockin_xy.")
    _validate_fixed_settings_role(LockinRole.XX, xx_settings)
    _validate_fixed_settings_role(LockinRole.XY, xy_settings)
    if not math.isfinite(settle_s) or settle_s < MINIMUM_FIXED_SETTINGS_SETTLE_S:
        raise Sr830Error(
            "Fixed-setting verification requires at least 1.5 s (five 300 ms "
            "time constants) of settling."
        )

    before_xx = lockin_xx.read_diagnostic(consume_status_latches=False)
    before_xy = lockin_xy.read_diagnostic(consume_status_latches=False)
    if before_xx.identity == before_xy.identity:
        raise Sr830Error(
            "Both semantic roles returned the same SR830 identity; verify that the "
            "VISA addresses refer to two distinct physical instruments."
        )
    verify_pair_readback(before_xx, before_xy, expected_frequency_hz)

    writes_started = False
    try:
        writes_started = True
        lockin_xx.write_fixed_settings(xx_settings)
        lockin_xy.write_fixed_settings(xy_settings)
        sleeper(settle_s)
        after_xx = lockin_xx.read_diagnostic(consume_status_latches=False)
        after_xy = lockin_xy.read_diagnostic(consume_status_latches=False)
        verify_fixed_settings_readback(after_xx, xx_settings, before_xx.phase_shift_deg)
        verify_fixed_settings_readback(after_xy, xy_settings, before_xy.phase_shift_deg)
        return FixedSettingsConfigurationResult(
            before_xx=before_xx,
            before_xy=before_xy,
            after_xx=after_xx,
            after_xy=after_xy,
        )
    except BaseException as exc:
        if writes_started:
            for error in _attempt_minimum_output(lockin_xx, lockin_xy):
                exc.add_note(f"Minimum-output cleanup also failed: {error}")
            originals = (
                (lockin_xx, _settings_from_diagnostic(before_xx)),
                (lockin_xy, _settings_from_diagnostic(before_xy)),
            )
            for instrument, settings in originals:
                try:
                    instrument.write_fixed_settings(settings)
                    sleeper(settle_s)
                    restored = instrument.read_diagnostic(
                        consume_status_latches=False
                    )
                    original = before_xx if instrument.role is LockinRole.XX else before_xy
                    verify_fixed_settings_readback(
                        restored, settings, original.phase_shift_deg
                    )
                except BaseException as restore_error:
                    exc.add_note(
                        f"lockin_{instrument.role.value} fixed-setting restoration "
                        f"also failed: {restore_error}"
                    )
        raise


def execute_autorange_transition(
    instrument: Sr830,
    *,
    decision: AutorangeDecision,
    previous_full_scale_v: float,
    settle_s: float,
    sleeper: Callable[[float], None],
    authorize_writes: bool,
    authorize_status_latch_consumption: bool,
) -> AutorangeTransitionResult:
    """Execute one audited XX range transition before formal sampling."""

    if decision.action is AutorangeAction.FAIL:
        raise Sr830Error(f"XX autorange failed closed: {decision.reason}")
    previous_code = sensitivity_code(previous_full_scale_v)
    final_code = sensitivity_code(decision.state.current_full_scale_v)
    if decision.action is AutorangeAction.KEEP:
        if previous_code != final_code:
            raise Sr830Error("KEEP decision cannot change sensitivity.")
        return AutorangeTransitionResult(decision, previous_code, final_code, None, None)
    if not authorize_writes:
        raise AuthorizationRequired("SR830 sensitivity write was not explicitly authorized.")
    if not authorize_status_latch_consumption:
        raise AuthorizationRequired(
            "SR830 LIAS?/ERRS? latch consumption was not explicitly authorized."
        )
    if instrument.role is not LockinRole.XX:
        raise Sr830Error("Bounded autorange is allowed only for lockin_xx.")
    if not math.isfinite(settle_s) or settle_s < MINIMUM_FIXED_SETTINGS_SETTLE_S:
        raise Sr830Error("Autorange requires at least 1.5 s of settling.")
    actual_before = instrument.read_sensitivity()
    if actual_before != previous_code:
        raise Sr830Error(
            f"lockin_xx sensitivity readback {actual_before} != {previous_code}."
        )

    write_started = False
    try:
        write_started = True
        instrument.set_sensitivity(final_code)
        sleeper(settle_s)
        if instrument.read_sensitivity() != final_code:
            raise Sr830Error("lockin_xx sensitivity did not reach the target range.")
        transition_sample = instrument.read_harmonic_sample(1)
        status = transition_sample.lia_status
        if (
            status.reference_unlocked
            or status.input_or_reserve_overload
            or status.filter_overload
            or status.frequency_range_changed
            or status.time_constant_changed
            or transition_sample.error_status
            or (status.output_overload and decision.action is not AutorangeAction.NARROW)
        ):
            raise Sr830Error("Unexpected status in autorange transition sample.")
        sleeper(settle_s)
        verification_sample = instrument.read_harmonic_sample(1)
        _validate_harmonic_sample(verification_sample, (verification_sample,))
        if instrument.read_sensitivity() != final_code:
            raise Sr830Error("lockin_xx sensitivity changed before range freeze.")
        return AutorangeTransitionResult(
            decision,
            previous_code,
            final_code,
            transition_sample,
            verification_sample,
        )
    except BaseException as exc:
        if write_started:
            try:
                instrument.set_minimum_sine_output()
            except BaseException as cleanup_error:
                exc.add_note(f"Minimum-output cleanup also failed: {cleanup_error}")
            try:
                instrument.set_sensitivity(previous_code)
                sleeper(settle_s)
                restored = instrument.read_sensitivity()
                if restored != previous_code:
                    raise Sr830Error(
                        f"restored sensitivity readback {restored} != {previous_code}"
                    )
            except BaseException as restore_error:
                exc.add_note(f"Sensitivity restoration also failed: {restore_error}")
        raise


def verify_pair_readback(
    xx: Sr830Diagnostic,
    xy: Sr830Diagnostic,
    expected_frequency_hz: float,
    *,
    check_frequency: bool = True,
) -> None:
    problems: list[str] = []
    if xx.reference_mode != 1:
        problems.append("lockin_xx is not using internal reference")
    if xy.reference_mode != 0:
        problems.append("lockin_xy is not using external reference")
    if xy.reference_slope != 1:
        problems.append("lockin_xy is not using TTL rising edge")
    if xx.harmonic != 1 or xy.harmonic != 1:
        problems.append("both lock-ins must use first harmonic")
    if not math.isclose(xx.sine_output_v, MINIMUM_SINE_OUTPUT_V, abs_tol=0.001):
        problems.append("lockin_xx sine output is not at the 4 mVrms minimum")
    if not math.isclose(xy.sine_output_v, MINIMUM_SINE_OUTPUT_V, abs_tol=0.001):
        problems.append("lockin_xy sine output is not at the 4 mVrms minimum")
    for diagnostic in (xx, xy):
        for label, frequency_hz in (
            ("frequency", diagnostic.frequency_hz),
            ("snapshot frequency", diagnostic.snapshot_frequency_hz),
        ):
            if not math.isfinite(frequency_hz) or not (
                0.001 <= frequency_hz <= MAXIMUM_REFERENCE_FREQUENCY_HZ
            ):
                problems.append(
                    f"{diagnostic.role.value} {label} readback is invalid"
                )
        if check_frequency and not math.isclose(
            diagnostic.frequency_hz,
            expected_frequency_hz,
            rel_tol=1e-5,
            abs_tol=PAIR_FREQUENCY_ABS_TOLERANCE_HZ,
        ):
            problems.append(f"{diagnostic.role.value} frequency readback does not match")
    if problems:
        raise Sr830Error("SR830 configuration verification failed: " + "; ".join(problems))


def _validate_fixed_settings_role(
    role: LockinRole, settings: Sr830SettingCodes
) -> None:
    if role is LockinRole.XX:
        if settings.reference_source != 1 or settings.external_reference_edge is not None:
            raise Sr830Error("lockin_xx fixed settings must use internal reference.")
    elif (
        settings.reference_source != 0
        or settings.external_reference_edge != 1
    ):
        raise Sr830Error(
            "lockin_xy fixed settings must use external TTL rising-edge reference."
        )


def verify_fixed_settings_readback(
    diagnostic: Sr830Diagnostic,
    expected: Sr830SettingCodes,
    expected_phase_shift_deg: float,
) -> None:
    problems: list[str] = []
    checks = (
        ("reference source", diagnostic.reference_mode, expected.reference_source),
        ("input mode", diagnostic.input_mode, expected.input_mode),
        ("shield grounding", diagnostic.shield_grounding, expected.shield_grounding),
        ("input coupling", diagnostic.input_coupling, expected.input_coupling),
        ("time constant", diagnostic.time_constant, expected.time_constant),
        ("filter slope", diagnostic.filter_slope, expected.filter_slope),
        ("sensitivity", diagnostic.sensitivity, expected.sensitivity),
    )
    for label, actual, target in checks:
        if actual != target:
            problems.append(f"{label} readback {actual} != {target}")
    if diagnostic.role is LockinRole.XY and (
        diagnostic.reference_slope != expected.external_reference_edge
    ):
        problems.append("external reference edge readback mismatch")
    if not math.isclose(
        diagnostic.phase_shift_deg,
        expected_phase_shift_deg,
        rel_tol=0.0,
        abs_tol=1e-9,
    ):
        problems.append("PHAS shift changed during fixed-setting configuration")
    if problems:
        raise Sr830Error(
            f"lockin_{diagnostic.role.value} fixed-setting verification failed: "
            + "; ".join(problems)
        )


def _settings_from_diagnostic(diagnostic: Sr830Diagnostic) -> Sr830SettingCodes:
    return Sr830SettingCodes(
        reference_source=diagnostic.reference_mode,
        external_reference_edge=(
            diagnostic.reference_slope
            if diagnostic.role is LockinRole.XY
            else None
        ),
        input_mode=diagnostic.input_mode,
        shield_grounding=diagnostic.shield_grounding,
        input_coupling=diagnostic.input_coupling,
        time_constant=diagnostic.time_constant,
        filter_slope=diagnostic.filter_slope,
        sensitivity=diagnostic.sensitivity,
    )


def _attempt_minimum_output(*instruments: Sr830) -> list[BaseException]:
    errors: list[BaseException] = []
    for instrument in instruments:
        try:
            instrument.set_minimum_sine_output()
        except BaseException as exc:
            errors.append(exc)
    return errors


def _attempt_first_harmonic(*instruments: Sr830) -> list[BaseException]:
    errors: list[BaseException] = []
    for instrument in instruments:
        try:
            instrument.set_harmonic(1)
        except BaseException as exc:
            errors.append(exc)
    return errors


class DualSr830Controller:
    """Semantic pair controller for sequential xx/xy harmonic snapshots."""

    def __init__(self, lockin_xx: Sr830, lockin_xy: Sr830) -> None:
        if lockin_xx.role is not LockinRole.XX or lockin_xy.role is not LockinRole.XY:
            raise Sr830Error("Dual controller requires lockin_xx then lockin_xy.")
        self.lockin_xx = lockin_xx
        self.lockin_xy = lockin_xy
        self._configured = False

    def configure_minimum(
        self,
        *,
        frequency_hz: float,
        authorize_writes: bool,
        confirm_xy_sine_disconnected: bool,
    ) -> PairConfigurationResult:
        result = configure_minimum_excitation_pair(
            self.lockin_xx,
            self.lockin_xy,
            frequency_hz=frequency_hz,
            authorize_writes=authorize_writes,
            confirm_xy_sine_disconnected=confirm_xy_sine_disconnected,
        )
        self._configured = True
        return result

    def authorize_existing_configuration(
        self,
        *,
        frequency_hz: float,
        authorize_writes: bool,
        confirm_xy_sine_disconnected: bool,
    ) -> tuple[Sr830Diagnostic, Sr830Diagnostic]:
        """Verify an already configured pair before separately authorized HARM writes."""

        if not authorize_writes:
            raise AuthorizationRequired("SR830 harmonic writes were not authorized.")
        if not confirm_xy_sine_disconnected:
            raise AuthorizationRequired(
                "Physical disconnection of lockin_xy SINE OUT was not confirmed."
            )
        return self.verify_existing_configuration(frequency_hz=frequency_hz)

    def verify_existing_configuration(
        self,
        *,
        frequency_hz: float,
        check_frequency: bool = True,
        ignore_output_overload: bool = False,
        transient_overload_recheck_s: float = 0.0,
    ) -> tuple[Sr830Diagnostic, Sr830Diagnostic]:
        """Read and validate an existing dual-SR830 configuration without writes."""

        xx = self.lockin_xx.read_diagnostic(consume_status_latches=True)
        xy = self.lockin_xy.read_diagnostic(consume_status_latches=True)
        if xx.identity == xy.identity:
            raise Sr830Error("Both semantic roles returned the same SR830 identity.")
        verify_pair_readback(xx, xy, frequency_hz, check_frequency=check_frequency)
        def status_problems(
            diagnostics: tuple[Sr830Diagnostic, Sr830Diagnostic]
        ) -> list[str]:
            found: list[str] = []
            for diagnostic in diagnostics:
                if diagnostic.lia_status is None or diagnostic.error_status is None:
                    found.append(
                        f"lockin_{diagnostic.role.value} safety status is incomplete"
                    )
                    continue
                if diagnostic.lia_status.reference_unlocked:
                    found.append(f"lockin_{diagnostic.role.value} reference is unlocked")
                if diagnostic.lia_status.input_or_reserve_overload:
                    found.append(f"lockin_{diagnostic.role.value} input/reserve overload")
                if diagnostic.lia_status.filter_overload:
                    found.append(f"lockin_{diagnostic.role.value} filter overload")
                if diagnostic.lia_status.output_overload and not ignore_output_overload:
                    found.append(f"lockin_{diagnostic.role.value} reports output overload")
                if diagnostic.error_status:
                    found.append(
                        f"lockin_{diagnostic.role.value} error status is "
                        f"{diagnostic.error_status}"
                    )
            return found

        diagnostics = (xx, xy)
        problems = status_problems(diagnostics)
        transient_only = bool(problems) and all(
            "input/reserve overload" in problem or "filter overload" in problem
            for problem in problems
        )
        if transient_only and transient_overload_recheck_s > 0:
            time.sleep(transient_overload_recheck_s)
            xx = self.lockin_xx.read_diagnostic(consume_status_latches=True)
            xy = self.lockin_xy.read_diagnostic(consume_status_latches=True)
            verify_pair_readback(xx, xy, frequency_hz, check_frequency=check_frequency)
            problems = status_problems((xx, xy))
        if problems:
            raise Sr830Error(
                "SR830 preflight status verification failed: " + "; ".join(problems)
            )
        self._configured = True
        return xx, xy

    def measure_harmonics(
        self,
        *,
        settle_s: float,
        sleeper: Callable[[float], None],
    ) -> DualSr830Measurement:
        if not self._configured:
            raise AuthorizationRequired(
                "The dual SR830 pair must be explicitly configured before acquisition."
            )
        if not math.isfinite(settle_s) or settle_s < 0:
            raise ValueError("settle_s must be finite and non-negative.")
        samples: list[Sr830HarmonicSample] = []
        try:
            for harmonic in (1, 2, 3):
                self.lockin_xx.set_harmonic(harmonic)
                self.lockin_xy.set_harmonic(harmonic)
                sleeper(settle_s)
                xx_sample = self.lockin_xx.read_harmonic_sample(harmonic)
                samples.append(xx_sample)
                _validate_harmonic_sample(xx_sample, tuple(samples))
                xy_sample = self.lockin_xy.read_harmonic_sample(harmonic)
                samples.append(xy_sample)
                _validate_harmonic_sample(xy_sample, tuple(samples))
                if not math.isclose(
                    xx_sample.reading.frequency_hz,
                    xy_sample.reading.frequency_hz,
                    rel_tol=1e-5,
                    abs_tol=PAIR_FREQUENCY_ABS_TOLERANCE_HZ,
                ):
                    raise Sr830AcquisitionError(
                        f"xx/xy frequency mismatch at harmonic {harmonic}.",
                        tuple(samples),
                    )
            self.lockin_xx.set_harmonic(1)
            self.lockin_xy.set_harmonic(1)
            for instrument in (self.lockin_xx, self.lockin_xy):
                restored = instrument.read_harmonic()
                if restored != 1:
                    raise Sr830AcquisitionError(
                        f"lockin_{instrument.role.value} did not restore harmonic 1.",
                        tuple(samples),
                    )
            return DualSr830Measurement(tuple(samples))
        except BaseException as exc:
            harmonic_errors = _attempt_first_harmonic(
                self.lockin_xx, self.lockin_xy
            )
            cleanup_errors = _attempt_minimum_output(
                self.lockin_xx, self.lockin_xy
            )
            for harmonic_error in harmonic_errors:
                exc.add_note(
                    f"First-harmonic restoration also failed: {harmonic_error}"
                )
            for cleanup_error in cleanup_errors:
                exc.add_note(f"Minimum-output cleanup also failed: {cleanup_error}")
            if isinstance(exc, (KeyboardInterrupt, SystemExit, Sr830AcquisitionError)):
                raise
            raise Sr830AcquisitionError(
                f"Dual SR830 acquisition failed: {type(exc).__name__}: {exc}",
                tuple(samples),
            ) from exc


def _validate_harmonic_sample(
    sample: Sr830HarmonicSample,
    partial_samples: tuple[Sr830HarmonicSample, ...],
) -> None:
    role = sample.reading.role.value
    if sample.lia_status.reference_unlocked:
        raise Sr830AcquisitionError(
            f"lockin_{role} reference unlocked.", partial_samples
        )
    if sample.lia_status.any_overload:
        raise Sr830AcquisitionError(
            f"lockin_{role} reported overload.", partial_samples
        )
    if sample.error_status:
        raise Sr830AcquisitionError(
            f"lockin_{role} error status is {sample.error_status}.", partial_samples
        )
