from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Callable, Protocol

from .models import LockinReading, LockinRole


MINIMUM_SINE_OUTPUT_V = 0.004
MAXIMUM_REFERENCE_FREQUENCY_HZ = 102_000.0


class Sr830Error(RuntimeError):
    """Raised when an SR830 response or verified state is invalid."""


class AuthorizationRequired(Sr830Error):
    """Raised before a hardware-setting write without explicit authorization."""


class Sr830AcquisitionError(Sr830Error):
    def __init__(
        self, message: str, partial_readings: tuple[LockinReading, ...] = ()
    ) -> None:
        super().__init__(message)
        self.partial_readings = partial_readings


class VisaResource(Protocol):
    def query(self, command: str) -> str: ...

    def write(self, command: str) -> object: ...

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
class Sr830HarmonicSample:
    reading: LockinReading
    lia_status: LiaStatus
    error_status: int


@dataclass(frozen=True, slots=True)
class DualSr830Measurement:
    readings: tuple[LockinReading, ...]
    pair_reads_are_sequential: bool = True


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
        sensitivity = self._query_int("SENS?")
        reserve_mode = self._query_int("RMOD?")
        time_constant = self._query_int("OFLT?")
        filter_slope = self._query_int("OFSL?")
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

    def set_harmonic(self, harmonic: int) -> None:
        if harmonic not in (1, 2, 3):
            raise ValueError("Only harmonics 1, 2, and 3 are supported.")
        self._resource.write(f"HARM {harmonic}")

    def read_harmonic_sample(self, expected_harmonic: int) -> Sr830HarmonicSample:
        harmonic = self._query_int("HARM?")
        if harmonic != expected_harmonic:
            raise Sr830Error(
                f"lockin_{self.role.value} harmonic readback is {harmonic}; "
                f"expected {expected_harmonic}."
            )
        snapshot = self._query_csv_floats("SNAP? 1,2,3,4,9", expected=5)
        lia_status = decode_lia_status(self._query_int("LIAS?"))
        error_status = self._query_int("ERRS?")
        reading = LockinReading(
            role=self.role,
            harmonic=harmonic,
            x_v=snapshot[0],
            y_v=snapshot[1],
            amplitude_v=snapshot[2],
            phase_deg=snapshot[3],
            frequency_hz=snapshot[4],
            locked=not lia_status.reference_unlocked,
            overload=lia_status.any_overload,
        )
        return Sr830HarmonicSample(reading, lia_status, error_status)

    def configure_xx_minimum_excitation(self, frequency_hz: float) -> None:
        if self.role is not LockinRole.XX:
            raise Sr830Error("Internal excitation may only be configured for lockin_xx.")
        self._resource.write("FMOD 1")
        self._resource.write("HARM 1")
        self._resource.write(f"FREQ {frequency_hz:g}")

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
        _verify_pair_readback(xx_diagnostic, xy_diagnostic, frequency_hz)
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


def _verify_pair_readback(
    xx: Sr830Diagnostic, xy: Sr830Diagnostic, expected_frequency_hz: float
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
        if not math.isclose(
            diagnostic.frequency_hz,
            expected_frequency_hz,
            rel_tol=1e-5,
            abs_tol=0.0001,
        ):
            problems.append(f"{diagnostic.role.value} frequency readback does not match")
    if problems:
        raise Sr830Error("SR830 configuration verification failed: " + "; ".join(problems))


def _attempt_minimum_output(*instruments: Sr830) -> list[BaseException]:
    errors: list[BaseException] = []
    for instrument in instruments:
        try:
            instrument.set_minimum_sine_output()
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
        readings: list[LockinReading] = []
        try:
            for harmonic in (1, 2, 3):
                self.lockin_xx.set_harmonic(harmonic)
                self.lockin_xy.set_harmonic(harmonic)
                sleeper(settle_s)
                xx_sample = self.lockin_xx.read_harmonic_sample(harmonic)
                readings.append(xx_sample.reading)
                _validate_harmonic_sample(xx_sample, tuple(readings))
                xy_sample = self.lockin_xy.read_harmonic_sample(harmonic)
                readings.append(xy_sample.reading)
                _validate_harmonic_sample(xy_sample, tuple(readings))
                if not math.isclose(
                    xx_sample.reading.frequency_hz,
                    xy_sample.reading.frequency_hz,
                    rel_tol=1e-5,
                    abs_tol=0.0001,
                ):
                    raise Sr830AcquisitionError(
                        f"xx/xy frequency mismatch at harmonic {harmonic}.",
                        tuple(readings),
                    )
            self.lockin_xx.set_harmonic(1)
            self.lockin_xy.set_harmonic(1)
            return DualSr830Measurement(tuple(readings))
        except BaseException as exc:
            cleanup_errors = _attempt_minimum_output(
                self.lockin_xx, self.lockin_xy
            )
            for cleanup_error in cleanup_errors:
                exc.add_note(f"Minimum-output cleanup also failed: {cleanup_error}")
            if isinstance(exc, (KeyboardInterrupt, SystemExit, Sr830AcquisitionError)):
                raise
            raise Sr830AcquisitionError(
                f"Dual SR830 acquisition failed: {type(exc).__name__}: {exc}",
                tuple(readings),
            ) from exc


def _validate_harmonic_sample(
    sample: Sr830HarmonicSample,
    partial_readings: tuple[LockinReading, ...],
) -> None:
    role = sample.reading.role.value
    if sample.lia_status.reference_unlocked:
        raise Sr830AcquisitionError(
            f"lockin_{role} reference unlocked.", partial_readings
        )
    if sample.lia_status.any_overload:
        raise Sr830AcquisitionError(
            f"lockin_{role} reported overload.", partial_readings
        )
    if sample.error_status:
        raise Sr830AcquisitionError(
            f"lockin_{role} error status is {sample.error_status}.",
            partial_readings,
        )
