"""Offline-safe physical-unit contracts for explicitly selected lock-in models.

This foundation is not connected to the legacy acquisition/session entry points.
It does not open VISA resources or configure excitation/reference topology.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
import math
from typing import Protocol

from .models import LockinRole
from .sr830 import Sr830, VisaResource
from .sr830_settings import sensitivity_full_scale_v, time_constant_seconds


class LockinModel(StrEnum):
    SR830 = "SR830"
    SR865A = "SR865A"


class LockinBackendError(RuntimeError):
    """A selected instrument or physical-unit readback cannot be verified."""


@dataclass(frozen=True, slots=True)
class LockinCapabilities:
    model: LockinModel
    minimum_reference_hz: float
    maximum_reference_hz: float
    maximum_detection_hz: float
    detection_maximum_inclusive: bool
    maximum_hardware_harmonic: int
    time_constants_s: tuple[float, ...]
    sensitivity_full_scales_v: tuple[float, ...]
    supports_input_range: bool
    software_harmonics: tuple[int, ...] = (1, 2, 3)

    def validate_detection(self, reference_frequency_hz: float, harmonic: int) -> float:
        if isinstance(reference_frequency_hz, bool) or not isinstance(
            reference_frequency_hz, (int, float)
        ):
            raise ValueError("Reference frequency must be numeric Hz.")
        if not math.isfinite(reference_frequency_hz) or not (
            self.minimum_reference_hz <= reference_frequency_hz <= self.maximum_reference_hz
        ):
            raise ValueError(f"Reference frequency is outside {self.model} capabilities.")
        if type(harmonic) is not int or harmonic not in self.software_harmonics:
            raise ValueError("This software supports integer harmonics 1, 2, and 3 only.")
        if harmonic > self.maximum_hardware_harmonic:
            raise ValueError(f"Harmonic exceeds {self.model} capabilities.")
        detection_hz = reference_frequency_hz * harmonic
        exceeds = (
            detection_hz > self.maximum_detection_hz
            if self.detection_maximum_inclusive
            else detection_hz >= self.maximum_detection_hz
        )
        if exceeds:
            raise ValueError(f"Detection frequency exceeds {self.model} capabilities.")
        return detection_hz


def capabilities_for(model: LockinModel | str) -> LockinCapabilities:
    selected = LockinModel(model)
    if selected is LockinModel.SR830:
        return LockinCapabilities(
            selected, 0.001, 102_000.0, 102_000.0, True, 19_999,
            tuple(time_constant_seconds(code) for code in range(20)),
            tuple(sensitivity_full_scale_v(code) for code in range(27)), False,
        )
    from .sr865a_settings import SENSITIVITIES_V, TIME_CONSTANTS_S

    return LockinCapabilities(
        selected, 0.001, 4_000_000.0, 4_000_000.0, False, 99,
        tuple(sorted(TIME_CONSTANTS_S)), tuple(sorted(SENSITIVITIES_V)), True,
    )


def _role(value: LockinRole | str) -> LockinRole:
    if isinstance(value, LockinRole):
        return value
    if value == "lockin_xx":
        return LockinRole.XX
    if value == "lockin_xy":
        return LockinRole.XY
    raise ValueError("Lock-in role must be lockin_xx, lockin_xy, or LockinRole.")


@dataclass(frozen=True, slots=True)
class RoleHarmonicPlan:
    role: LockinRole
    model: LockinModel
    reference_frequency_hz: float
    harmonic: int


def validate_role_harmonics(plans: tuple[RoleHarmonicPlan, ...]) -> tuple[float, ...]:
    """Prevalidate an entire role plan; never union role-specific harmonics."""

    roles = tuple(_role(plan.role) for plan in plans)
    if len(set(roles)) != len(roles):
        raise ValueError("A harmonic plan must contain each role at most once.")
    return tuple(
        capabilities_for(plan.model).validate_detection(plan.reference_frequency_hz, plan.harmonic)
        for plan in plans
    )


@dataclass(frozen=True, slots=True)
class RawIo:
    command: str
    response: str | None
    started_at_utc: datetime
    completed_at_utc: datetime
    error: str | None = None
    operation: str = "query"


@dataclass(frozen=True, slots=True)
class BackendSettings:
    role: LockinRole
    model: LockinModel
    identity: str
    time_constant_s: float
    sensitivity_full_scale_v: float
    harmonic: int
    reference_source: str
    input_range_v_peak: float | None
    native_settings: object
    raw: tuple[RawIo, ...]


@dataclass(frozen=True, slots=True)
class BackendStatus:
    locked: bool | None
    input_overload: bool | None
    output_scale_overload: bool | None
    instrument_error: bool | None
    observation: str
    native_status: object | None
    validity: bool | None

    @property
    def clean(self) -> bool | None:
        """Status evidence only; True is not formal acquisition acceptance."""

        return self.validity


@dataclass(frozen=True, slots=True)
class BackendSample:
    role: LockinRole
    model: LockinModel
    harmonic: int
    x_v: float
    y_v: float
    amplitude_v: float
    phase_deg: float | None
    reference_frequency_hz: float
    detection_frequency_hz: float
    amplitude_phase_source: str
    detection_frequency_source: str
    status: BackendStatus
    native_sample: object
    raw: tuple[RawIo, ...]


class LockinBackend(Protocol):
    role: LockinRole
    capabilities: LockinCapabilities

    def read_identity(self) -> str: ...
    def read_settings(self) -> BackendSettings: ...
    def set_time_constant(self, seconds: float, *, authorize_writes: bool = False) -> None: ...
    def set_sensitivity(self, full_scale_v: float, *, authorize_writes: bool = False) -> None: ...
    def set_harmonic(self, harmonic: int, *, authorize_writes: bool = False) -> None: ...
    def read_sample(
        self, *, consume_status_latches: bool, current_status_supported: bool = False
    ) -> BackendSample: ...


class _AuditedResource:
    def __init__(self, resource: VisaResource):
        self.resource = resource
        self.queries: list[RawIo] = []

    def query(self, command: str) -> str:
        started = datetime.now(UTC)
        try:
            response = self.resource.query(command)
        except BaseException as exc:
            self.queries.append(RawIo(command, None, started, datetime.now(UTC), repr(exc)))
            raise
        self.queries.append(RawIo(command, response, started, datetime.now(UTC)))
        return response

    def write(self, command: str) -> object:
        started = datetime.now(UTC)
        try:
            result = self.resource.write(command)
        except BaseException as exc:
            self.queries.append(RawIo(command, None, started, datetime.now(UTC), repr(exc), "write"))
            raise
        self.queries.append(RawIo(command, None, started, datetime.now(UTC), operation="write"))
        return result

    def clear(self) -> object:
        return self.resource.clear()

    def close(self) -> None:
        self.resource.close()


class PhysicalLockinBackend:
    """Thin facade; the caller owns the injected resource and hardware stage."""

    def __init__(self, model: LockinModel, role: LockinRole, resource: VisaResource):
        self.capabilities = capabilities_for(model)
        self.role = _role(role)
        self._resource = _AuditedResource(resource)
        if self.capabilities.model is LockinModel.SR830:
            self._driver = Sr830(self._resource, self.role)
        else:
            from .sr865a import Sr865a

            self._driver = Sr865a(self._resource, self.role)

    @property
    def audit(self) -> tuple[RawIo, ...]:
        """Retain reads/writes even when an operation fails before returning."""

        return tuple(self._resource.queries)

    def read_identity(self) -> str:
        identity = self._driver.query_identity()
        fields = tuple(part.strip() for part in identity.split(","))
        if len(fields) != 4 or fields[0] != "Stanford_Research_Systems" or (
            fields[1] != self.capabilities.model.value or not fields[2] or not fields[3]
        ):
            raise LockinBackendError(f"Unexpected identity for lockin_{self.role.value}: {identity!r}.")
        return identity

    def _authorize(self, authorized: bool) -> None:
        if authorized is not True:
            raise LockinBackendError("Setting writes require explicit authorization.")
        self.read_identity()

    def set_time_constant(self, seconds: float, *, authorize_writes: bool = False) -> None:
        if isinstance(seconds, bool) or seconds not in self.capabilities.time_constants_s:
            raise ValueError("Unsupported physical time constant for selected model.")
        self._authorize(authorize_writes)
        if self.capabilities.model is LockinModel.SR830:
            code = self.capabilities.time_constants_s.index(seconds)
            if self._driver.read_time_constant() == code:
                return
            self._driver.set_fixed_setting("time_constant", code)
            if self._driver.read_time_constant() != code:
                raise LockinBackendError("SR830 time-constant readback mismatch.")
        else:
            self._driver.set_time_constant(seconds, authorized=True)

    def set_sensitivity(self, full_scale_v: float, *, authorize_writes: bool = False) -> None:
        if isinstance(full_scale_v, bool) or full_scale_v not in self.capabilities.sensitivity_full_scales_v:
            raise ValueError("Unsupported voltage full scale for selected model.")
        self._authorize(authorize_writes)
        if self.capabilities.model is LockinModel.SR830:
            from .sr830_settings import sensitivity_code

            self._verify_sr830_voltage_input()
            code = sensitivity_code(full_scale_v)
            if self._driver.read_sensitivity() == code:
                return
            self._driver.set_sensitivity(code)
            if self._driver.read_sensitivity() != code:
                raise LockinBackendError("SR830 sensitivity readback mismatch.")
        else:
            self._driver.set_sensitivity(full_scale_v, authorized=True)

    def set_harmonic(self, harmonic: int, *, authorize_writes: bool = False) -> None:
        # Static type/range validation precedes any instrument interaction.
        self.capabilities.validate_detection(self.capabilities.minimum_reference_hz, harmonic)
        self._authorize(authorize_writes)
        frequency = self._driver.read_reference_frequency()
        self.capabilities.validate_detection(frequency, harmonic)
        if self.capabilities.model is LockinModel.SR830:
            if self._driver.read_harmonic() == harmonic:
                return
            self._driver.set_harmonic(harmonic)
            if self._driver.read_harmonic() != harmonic:
                raise LockinBackendError("SR830 harmonic readback mismatch.")
            self.capabilities.validate_detection(self._driver.read_reference_frequency(), harmonic)
        else:
            self._driver.set_harmonic(harmonic, authorized=True)

    def read_settings(self) -> BackendSettings:
        start = len(self._resource.queries)
        identity = self.read_identity()
        if self.capabilities.model is LockinModel.SR830:
            native = self._driver.read_diagnostic(consume_status_latches=False)
            if native.reference_mode not in (0, 1):
                raise LockinBackendError("Unknown SR830 reference source readback.")
            if native.input_mode not in (0, 1):
                raise LockinBackendError("Physical voltage interface requires SR830 voltage input.")
            seconds = time_constant_seconds(native.time_constant)
            full_scale = sensitivity_full_scale_v(native.sensitivity)
            source = "external" if native.reference_mode == 0 else "internal"
            input_range = None
        else:
            native = self._driver.read_settings()
            seconds = native.time_constant_s
            full_scale = native.sensitivity_full_scale_v
            source = native.reference_source
            input_range = native.input_range_v_peak
        if type(native.harmonic) is not int or native.harmonic not in self.capabilities.software_harmonics:
            raise LockinBackendError("Readback harmonic is outside the current software scope.")
        return BackendSettings(
            self.role, self.capabilities.model, identity, seconds, full_scale,
            native.harmonic, source, input_range, native, tuple(self._resource.queries[start:]),
        )

    def read_sample(
        self, *, consume_status_latches: bool, current_status_supported: bool = False
    ) -> BackendSample:
        if type(consume_status_latches) is not bool or type(current_status_supported) is not bool:
            raise ValueError("Status consumption and capability flags must be booleans.")
        if self.capabilities.model is LockinModel.SR830 and current_status_supported:
            raise ValueError("SR830 facade does not support instantaneous status queries.")
        start = len(self._resource.queries)
        self.read_identity()
        if self.capabilities.model is LockinModel.SR830:
            native = self._driver.read_diagnostic(consume_status_latches=consume_status_latches)
            if native.reference_mode not in (0, 1):
                raise LockinBackendError("Unknown SR830 reference source readback.")
            if native.input_mode not in (0, 1):
                raise LockinBackendError("Physical voltage interface requires SR830 voltage input.")
            lias = native.lia_status
            known = lias is not None and not (lias.raw & 0x80)
            validity = None
            if lias is not None:
                if not known or lias.reference_unlocked or lias.any_overload or native.error_status != 0:
                    validity = False
                elif not (lias.frequency_range_changed or lias.time_constant_changed):
                    validity = True
            status = BackendStatus(
                None if not known else not lias.reference_unlocked,
                None if not known else lias.input_or_reserve_overload,
                None if not known else lias.output_overload or lias.filter_overload,
                None if native.error_status is None else native.error_status != 0,
                "latched_interval" if consume_status_latches else "not_observed",
                (lias, native.error_status),
                validity,
            )
            reference = native.snapshot_frequency_hz
            detection = self.capabilities.validate_detection(reference, native.harmonic)
            return BackendSample(
                self.role, self.capabilities.model, native.harmonic,
                native.x_v, native.y_v, native.amplitude_v, native.phase_deg,
                reference, detection, "instrument_snapshot", "derived_harmonic_times_reference",
                status, native, tuple(self._resource.queries[start:]),
            )
        return self._read_sr865a_sample(start, consume_status_latches, current_status_supported)

    def _read_sr865a_sample(
        self, start: int, consume_status_latches: bool, current_status_supported: bool
    ) -> BackendSample:
        native = self._driver.read_sample(
            consume_status_latches=consume_status_latches,
            current_status_supported=current_status_supported,
        )
        self.capabilities.validate_detection(native.reference_frequency_hz, native.harmonic)
        status = BackendStatus(
            native.status.locked, native.status.input_overload,
            native.status.output_scale_overload, native.status.instrument_error,
            "instantaneous_and_latched" if current_status_supported and consume_status_latches
            else "instantaneous" if current_status_supported
            else "latched_interval" if consume_status_latches else "not_observed",
            native.status,
            native.status.valid,
        )
        return BackendSample(
            self.role, self.capabilities.model, native.harmonic,
            native.x_v, native.y_v, native.amplitude_v, native.phase_deg,
            native.reference_frequency_hz, native.detection_frequency_hz,
            "derived_from_snapshot_xy", "instrument_query",
            status, native, tuple(self._resource.queries[start:]),
        )

    def _verify_sr830_voltage_input(self) -> None:
        if self._driver.read_fixed_setting("input_mode") not in (0, 1):
            raise LockinBackendError("Physical voltage interface requires SR830 voltage input.")

    def read_reference_frequency(self) -> float:
        self.read_identity()
        return self._driver.read_reference_frequency()

    def configure_external_reference(
        self, *, edge: str, input_impedance_ohm: float | None,
        sync_output_mode: str | None, authorize_writes: bool = False,
        reference_source: str = "external_ttl",
    ) -> None:
        sine_reference = reference_source == "external_sine"
        if reference_source not in ("external_ttl", "external_sine") or edge not in (
                ("sine_zero_crossing",) if sine_reference else ("rising", "falling")):
            raise ValueError("External reference requires its explicit matching trigger.")
        if self.capabilities.model is LockinModel.SR830:
            if input_impedance_ohm is not None or sync_output_mode is not None:
                raise ValueError("SR830 has no selectable reference impedance/BlazeX mode.")
        elif sine_reference or input_impedance_ohm not in (50.0, 1_000_000.0) or sync_output_mode not in ("bipolar_sync", "unipolar_sync", "preserve"):
            raise ValueError("SR865A requires explicit reference impedance and sync mode.")
        self._authorize(authorize_writes)
        if self.capabilities.model is LockinModel.SR830:
            self._set_code("RSLP", 0 if sine_reference else (1 if edge == "rising" else 2), (0, 1, 2))
            self._set_code("FMOD", 0, (0, 1))
        else:
            self._driver.configure_reference("external_ttl", edge=edge,
                input_impedance_ohm=input_impedance_ohm, authorized=True)
            if sync_output_mode == "preserve":
                # XY feeds XX from SINE OUT; BlazeX is unrelated and stays untouched.
                if self._resource.query("BLAZEX?").strip() not in ("0", "1", "2"):
                    raise LockinBackendError("Unknown preserved BlazeX mode.")
            else:
                self._set_code("BLAZEX", 1 if sync_output_mode == "bipolar_sync" else 2, (0, 1, 2))

    def configure_fixed_measurement(self, settings, *, authorize_writes: bool = False) -> None:
        """Configure the explicit fixed-range RC subset, without touching excitation."""
        if settings.model != self.capabilities.model.value or settings.role is not self.role:
            raise ValueError("Measurement settings do not belong to this role/model.")
        if (settings.input_mode, settings.input_coupling,
            settings.sensitivity_mode, settings.filter_slope_db_oct) != (
            "a_minus_b", "ac", "fixed", 24
        ) or settings.shield_grounding not in ("float", "ground"):
            raise ValueError("Only A-B/explicit-grounding/AC/fixed/24 dB RC is supported in this profile.")
        if (isinstance(settings.time_constant_s, bool)
                or settings.time_constant_s not in self.capabilities.time_constants_s
                or isinstance(settings.sensitivity_full_scale_v, bool)
                or settings.sensitivity_full_scale_v not in self.capabilities.sensitivity_full_scales_v):
            raise ValueError("Unsupported physical filter/range settings.")
        phase = settings.phase_shift_deg
        if isinstance(phase, bool) or not isinstance(phase, (int, float)) or not math.isfinite(phase) or not -180 <= phase <= 180:
            raise ValueError("Phase must be within -180 to 180 degrees.")
        if self.capabilities.model is LockinModel.SR830:
            if settings.reserve_mode not in ("high_reserve", "normal", "low_noise"):
                raise ValueError("Unsupported SR830 reserve mode.")
        else:
            from .sr865a_settings import INPUT_RANGES_V_PEAK
            if isinstance(settings.input_range_v_peak, bool) or settings.input_range_v_peak not in INPUT_RANGES_V_PEAK:
                raise ValueError("Unsupported SR865A input range.")
        self._authorize(authorize_writes)
        self._set_code("ISRC", 1, (0, 1) if self.capabilities.model is LockinModel.SR865A else (0, 1, 2, 3))
        self._set_code("IGND", 0 if settings.shield_grounding == "float" else 1, (0, 1))
        self._set_code("ICPL", 0, (0, 1))
        self._set_code("OFSL", 3, (0, 1, 2, 3))
        if self.capabilities.model is LockinModel.SR830:
            self._set_code("ILIN", 0, (0, 1, 2, 3))
            reserve = {"high_reserve": 0, "normal": 1, "low_noise": 2}[settings.reserve_mode]
            self._set_code("RMOD", reserve, (0, 1, 2))
        else:
            self._set_code("ADVFILT", 0, (0, 1))
            self._set_code("SYNC", 0, (0, 1))
            self._driver.set_input_range(settings.input_range_v_peak, authorized=True)
        self.set_time_constant(settings.time_constant_s, authorize_writes=True)
        self.set_sensitivity(settings.sensitivity_full_scale_v, authorize_writes=True)
        if not math.isclose(self._query_float("PHAS?"), phase, abs_tol=0.001, rel_tol=0):
            self._resource.write(f"PHAS {phase:.12g}")
            if not math.isclose(self._query_float("PHAS?"), phase, abs_tol=0.001, rel_tol=0):
                raise LockinBackendError("Phase setting readback mismatch.")

    def read_source_state(self) -> dict:
        self.read_identity()
        amplitude = self._driver.read_sine_output()
        if self.capabilities.model is LockinModel.SR830:
            if not 0.004 <= amplitude <= 5.0:
                raise LockinBackendError("Invalid SR830 source amplitude readback.")
            return {"source_voltage_v": amplitude, "dc_offset_v": 0.0, "dc_mode": "not_supported"}
        return {"source_voltage_v": amplitude, "dc_offset_v": self._driver.read_source_offset(),
                "dc_mode": self._driver.read_source_dc_mode()}

    def set_source_amplitude(
        self, target_v: float, *, minimum_v: float, maximum_v: float,
        expected_dc_mode: str, authorize_writes: bool = False, protect: bool = False,
    ) -> dict:
        """Write a device SLVL setting, never an inferred sample voltage/current.

        Protection may reduce AC despite nonzero DC, but cannot certify DC safety;
        the caller must retain and assess the returned DC readback separately.
        """
        if self.role is not LockinRole.XX:
            raise ValueError("Only lockin_xx owns the connected excitation source.")
        lower, upper = (0.004, 5.0) if self.capabilities.model is LockinModel.SR830 else (1e-9, 2.0)
        values = (target_v, minimum_v, maximum_v)
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in values):
            raise ValueError("Source target and explicit policy limits must be finite numbers.")
        if not lower <= minimum_v <= target_v <= maximum_v <= upper or type(protect) is not bool:
            raise ValueError("Source target violates model or explicit policy limits.")
        allowed_modes = ("not_supported",) if self.capabilities.model is LockinModel.SR830 else ("common", "difference")
        if expected_dc_mode not in allowed_modes:
            raise ValueError("Source DC mode is not valid for the selected model.")
        self._authorize(authorize_writes)
        before = self.read_source_state()
        if not protect and not minimum_v <= before["source_voltage_v"] <= maximum_v:
            raise LockinBackendError("Existing source amplitude is outside the explicit policy.")
        if not protect and (before["dc_offset_v"] != 0 or before["dc_mode"] != expected_dc_mode):
            raise LockinBackendError("Nonzero/unknown source DC state prevents excitation settings.")
        target = min(target_v, before["source_voltage_v"]) if protect else target_v
        if target != before["source_voltage_v"]:
            self._resource.write(f"SLVL {target:.12g}")
        after = self.read_source_state()
        if not math.isclose(after["source_voltage_v"], target, rel_tol=1e-6, abs_tol=1e-12):
            raise LockinBackendError("Source amplitude readback differs from requested setting.")
        if not protect and (after["dc_offset_v"] != 0 or after["dc_mode"] != expected_dc_mode):
            raise LockinBackendError("Source DC state changed during excitation settings.")
        return after

    def _set_code(self, command: str, code: int, allowed) -> None:
        raw = self._resource.query(command + "?").strip()
        try:
            previous = int(raw)
        except ValueError as exc:
            raise LockinBackendError(f"Invalid {command} readback {raw!r}.") from exc
        if previous not in allowed or code not in allowed:
            raise LockinBackendError(f"Invalid {command} code.")
        if previous != code:
            self._resource.write(f"{command} {code}")
            if self._resource.query(command + "?").strip() != str(code):
                raise LockinBackendError(f"{command} readback mismatch.")

    def _query_float(self, command: str) -> float:
        try:
            value = float(self._resource.query(command).strip())
        except ValueError as exc:
            raise LockinBackendError(f"Invalid numeric reply to {command}.") from exc
        if not math.isfinite(value):
            raise LockinBackendError(f"Nonfinite reply to {command}.")
        return value


def create_lockin_backend(
    *, model: LockinModel | str, role: LockinRole | str, resource: VisaResource
) -> LockinBackend:
    """Select only the configured model/role; construction performs no I/O."""

    return PhysicalLockinBackend(LockinModel(model), _role(role), resource)
