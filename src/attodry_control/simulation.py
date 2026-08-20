from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
import math
from typing import Callable

from .cleanup import (
    CleanupReport,
    cleanup_after_failure,
    cleanup_after_normal_completion,
)
from .config import ControlConfig, RunMode
from .models import CryostatState, GateState, LockinReading, LockinRole, VectorField
from .records import (
    AcceptedTransportResult,
    AttemptRecord,
    AttemptStatus,
    ExperimentCondition,
    RawStationSample,
    RawTransportReading,
)
from .safety import MagnetLimits, validate_vector_field


MINIMUM_SR830_SINE_OUTPUT_V = 0.004


class SimulationError(RuntimeError):
    pass


class SimulationTimeout(SimulationError):
    pass


class SimulatedCommunicationError(SimulationError):
    pass


class GateLeakageViolation(SimulationError):
    pass


class AcquisitionRejected(SimulationError):
    pass


@dataclass(frozen=True, slots=True)
class SimulatedAttemptOutcome:
    attempt: AttemptRecord
    raw_readings: tuple[RawTransportReading, ...]
    station_samples: tuple[RawStationSample, ...]
    accepted_result: AcceptedTransportResult | None
    cleanup: CleanupReport | None
    last_confirmed_cryostat_state: CryostatState


@dataclass(frozen=True, slots=True)
class InterruptedAttemptState:
    raw_readings: tuple[RawTransportReading, ...]
    station_samples: tuple[RawStationSample, ...]
    cleanup: CleanupReport


class _FailureQueue:
    def __init__(self) -> None:
        self._remaining: dict[str, int] = {}

    def fail_next(self, operation: str, count: int = 1) -> None:
        if count <= 0:
            raise ValueError("Failure count must be positive.")
        self._remaining[operation] = self._remaining.get(operation, 0) + count

    def check(self, operation: str) -> None:
        remaining = self._remaining.get(operation, 0)
        if remaining:
            if remaining == 1:
                del self._remaining[operation]
            else:
                self._remaining[operation] = remaining - 1
            raise SimulatedCommunicationError(
                f"Injected communication failure during {operation}."
            )


class SimulationCryostat:
    def __init__(
        self,
        *,
        temperature_min_k: float,
        temperature_max_k: float,
        limits: MagnetLimits,
        event_log: list[str],
    ) -> None:
        self.temperature_min_k = temperature_min_k
        self.temperature_max_k = temperature_max_k
        self.limits = limits
        self.event_log = event_log
        self.temperature_stuck = False
        self.field_stuck = False
        self.closed = False
        self._failures = _FailureQueue()
        zero = VectorField(0.0, 0.0)
        self._state = CryostatState(
            sample_temperature_k=temperature_min_k,
            user_temperature_k=temperature_min_k,
            vti_temperature_k=temperature_min_k,
            field=zero,
            field_setpoint=zero,
            temperature_control_enabled=False,
            field_control_enabled=False,
            error_code=0,
        )

    def fail_next(self, operation: str, count: int = 1) -> None:
        self._failures.fail_next(operation, count)

    def read_state(self) -> CryostatState:
        self._failures.check("read_state")
        self.event_log.append("cryostat.read_state")
        return self._state

    def set_temperature(self, temperature_k: float) -> None:
        self._failures.check("set_temperature")
        if not math.isfinite(temperature_k) or not (
            self.temperature_min_k <= temperature_k <= self.temperature_max_k
        ):
            raise ValueError("Temperature target is outside simulation limits.")
        self.event_log.append("cryostat.set_temperature")
        self._state = replace(self._state, user_temperature_k=temperature_k)

    def ensure_temperature_control(self, enabled: bool) -> None:
        self._failures.check("ensure_temperature_control")
        self.event_log.append("cryostat.ensure_temperature_control")
        self._state = replace(self._state, temperature_control_enabled=enabled)

    def wait_for_temperature(self, max_polls: int) -> CryostatState:
        self.event_log.append("cryostat.wait_for_temperature")
        if max_polls <= 0:
            raise ValueError("max_polls must be positive.")
        if self.temperature_stuck:
            raise SimulationTimeout(
                f"Temperature stability timeout after {max_polls} simulated polls."
            )
        target = self._state.user_temperature_k
        self._state = replace(
            self._state,
            sample_temperature_k=target,
            vti_temperature_k=target,
        )
        return self._state

    def set_vector_field(self, target: VectorField) -> None:
        self._failures.check("set_vector_field")
        checked = validate_vector_field(target, self.limits)
        self.event_log.append("cryostat.set_vector_field")
        self._state = replace(self._state, field_setpoint=checked)

    def ensure_field_control(self, enabled: bool) -> None:
        self._failures.check("ensure_field_control")
        self.event_log.append("cryostat.ensure_field_control")
        self._state = replace(self._state, field_control_enabled=enabled)

    def wait_for_field(self, max_polls: int) -> CryostatState:
        self.event_log.append("cryostat.wait_for_field")
        if max_polls <= 0:
            raise ValueError("max_polls must be positive.")
        if self.field_stuck:
            raise SimulationTimeout(
                f"Field stability timeout after {max_polls} simulated polls."
            )
        self._state = replace(self._state, field=self._state.field_setpoint)
        return self._state

    def request_zero_field(self) -> None:
        self._failures.check("request_zero_field")
        self.event_log.append("cryostat.request_zero_field")
        zero = VectorField(0.0, 0.0)
        self._state = replace(self._state, field_setpoint=zero)
        if not self.field_stuck:
            self._state = replace(self._state, field=zero)

    def close(self) -> None:
        self.event_log.append("cryostat.close")
        self.closed = True


class SimulationLockin:
    def __init__(
        self,
        *,
        role: LockinRole,
        frequency_hz: float,
        source_voltage_v: float,
        sine_output_connected: bool,
        event_log: list[str],
    ) -> None:
        self.role = role
        self.frequency_hz = frequency_hz
        self.source_voltage_v = source_voltage_v
        self.sine_output_connected = sine_output_connected
        self.event_log = event_log
        self.locked = True
        self.overload = False
        self.closed = False
        self.reference_configured = False
        self._failures = _FailureQueue()

    def fail_next(self, operation: str, count: int = 1) -> None:
        self._failures.fail_next(operation, count)

    def configure_reference(self) -> None:
        self._failures.check("configure_reference")
        self.event_log.append(f"lockin_{self.role.value}.configure_reference")
        self.reference_configured = True

    def configure_excitation(self, voltage_v: float, frequency_hz: float) -> None:
        self._failures.check("configure_excitation")
        if self.role is not LockinRole.XX:
            raise SimulationError("Only lockin_xx may configure device excitation.")
        if not math.isfinite(voltage_v) or not (
            MINIMUM_SR830_SINE_OUTPUT_V <= voltage_v <= 5.0
        ):
            raise ValueError("SR830 excitation must be within 4 mVrms to 5 Vrms.")
        if not math.isfinite(frequency_hz) or not (0.001 <= frequency_hz <= 102_000):
            raise ValueError("Lock-in frequency must be within 0.001-102000 Hz.")
        self.event_log.append("lockin_xx.configure_excitation")
        self.source_voltage_v = voltage_v
        self.frequency_hz = frequency_hz

    def read_harmonic(self, harmonic: int) -> LockinReading:
        self._failures.check("read_harmonic")
        if harmonic not in (1, 2, 3):
            raise ValueError("Only harmonics 1, 2, and 3 are supported.")
        self.event_log.append(f"lockin_{self.role.value}.read_harmonic_{harmonic}")
        scale = 1e-6 if self.role is LockinRole.XX else 1e-7
        x_v = scale / harmonic
        y_v = scale * 0.1 / harmonic
        return LockinReading(
            role=self.role,
            harmonic=harmonic,
            x_v=x_v,
            y_v=y_v,
            amplitude_v=math.hypot(x_v, y_v),
            phase_deg=math.degrees(math.atan2(y_v, x_v)),
            frequency_hz=self.frequency_hz,
            locked=self.locked,
            overload=self.overload,
        )

    def set_minimum_excitation(self) -> None:
        self._failures.check("set_minimum_excitation")
        self.event_log.append(f"lockin_{self.role.value}.set_minimum_excitation")
        self.source_voltage_v = MINIMUM_SR830_SINE_OUTPUT_V

    def close(self) -> None:
        self.event_log.append(f"lockin_{self.role.value}.close")
        self.closed = True


class SimulationGate:
    def __init__(
        self,
        *,
        role: str,
        compliance_a: float,
        leakage_limit_a: float,
        event_log: list[str],
    ) -> None:
        self.role = role
        self.compliance_a = compliance_a
        self.leakage_limit_a = leakage_limit_a
        self.event_log = event_log
        self.injected_leakage_current_a = 0.0
        self.voltage_set_v = 0.0
        self.voltage_read_v = 0.0
        self.output_enabled = False
        self.closed = False
        self._failures = _FailureQueue()

    def fail_next(self, operation: str, count: int = 1) -> None:
        self._failures.fail_next(operation, count)

    def read_state(self) -> GateState:
        self._failures.check("read_state")
        self.event_log.append(f"gate_{self.role}.read_state")
        leakage = self.injected_leakage_current_a if self.output_enabled else None
        return GateState(
            role=self.role,
            voltage_set_v=self.voltage_set_v,
            voltage_read_v=self.voltage_read_v,
            leakage_current_a=leakage,
            compliance_a=self.compliance_a,
            output_enabled=self.output_enabled,
        )

    def set_voltage(self, voltage_v: float) -> None:
        self._failures.check("set_voltage")
        if not math.isfinite(voltage_v):
            raise ValueError("Gate voltage must be finite.")
        self.event_log.append(f"gate_{self.role}.set_voltage")
        self.voltage_set_v = voltage_v
        self.voltage_read_v = voltage_v
        self._enforce_leakage_limit()

    def enable_output(self) -> None:
        self._failures.check("enable_output")
        self.event_log.append(f"gate_{self.role}.enable_output")
        self.output_enabled = True
        self._enforce_leakage_limit()

    def disable_output(self) -> None:
        self._failures.check("disable_output")
        self.event_log.append(f"gate_{self.role}.disable_output")
        self.output_enabled = False

    def close(self) -> None:
        self.event_log.append(f"gate_{self.role}.close")
        self.closed = True

    def _enforce_leakage_limit(self) -> None:
        if (
            self.output_enabled
            and abs(self.injected_leakage_current_a) > self.leakage_limit_a
        ):
            measured = self.injected_leakage_current_a
            self.voltage_set_v = 0.0
            self.voltage_read_v = 0.0
            self.output_enabled = False
            raise GateLeakageViolation(
                f"gate_{self.role} leakage {measured:g} A exceeds "
                f"{self.leakage_limit_a:g} A; output failed closed."
            )


class SimulationStation:
    def __init__(
        self,
        *,
        cryostat: SimulationCryostat,
        lockin_xx: SimulationLockin,
        lockin_xy: SimulationLockin,
        gate_top: SimulationGate,
        gate_bottom: SimulationGate,
        temperature_max_polls: int,
        field_max_polls: int,
        event_log: list[str],
    ) -> None:
        self.cryostat = cryostat
        self.lockin_xx = lockin_xx
        self.lockin_xy = lockin_xy
        self.gate_top = gate_top
        self.gate_bottom = gate_bottom
        self.temperature_max_polls = temperature_max_polls
        self.field_max_polls = field_max_polls
        self.event_log = event_log

    @classmethod
    def from_config(cls, config: ControlConfig) -> SimulationStation:
        if config.project.mode is not RunMode.SIMULATION:
            raise ValueError("SimulationStation requires simulation configuration.")
        event_log: list[str] = []
        cryostat = SimulationCryostat(
            temperature_min_k=config.cryostat.temperature_min_k,
            temperature_max_k=config.cryostat.temperature_max_k,
            limits=config.magnet.limits,
            event_log=event_log,
        )
        lockin_xx = SimulationLockin(
            role=LockinRole.XX,
            frequency_hz=config.lockin_xx.frequency_hz,
            source_voltage_v=config.lockin_xx.source_voltage_v,
            sine_output_connected=config.lockin_xx.sine_output_connected,
            event_log=event_log,
        )
        lockin_xy = SimulationLockin(
            role=LockinRole.XY,
            frequency_hz=config.lockin_xy.frequency_hz,
            source_voltage_v=config.lockin_xy.source_voltage_v,
            sine_output_connected=config.lockin_xy.sine_output_connected,
            event_log=event_log,
        )
        gate_top = SimulationGate(
            role="top",
            compliance_a=config.gate_top.compliance_a,
            leakage_limit_a=config.gate_top.leakage_limit_a,
            event_log=event_log,
        )
        gate_bottom = SimulationGate(
            role="bottom",
            compliance_a=config.gate_bottom.compliance_a,
            leakage_limit_a=config.gate_bottom.leakage_limit_a,
            event_log=event_log,
        )
        temperature_max_polls = math.ceil(
            config.temperature_stability.wait_timeout_s
            / config.temperature_stability.poll_interval_s
        )
        field_max_polls = math.ceil(
            config.magnet.stability.wait_timeout_s
            / config.magnet.stability.poll_interval_s
        )
        return cls(
            cryostat=cryostat,
            lockin_xx=lockin_xx,
            lockin_xy=lockin_xy,
            gate_top=gate_top,
            gate_bottom=gate_bottom,
            temperature_max_polls=temperature_max_polls,
            field_max_polls=field_max_polls,
            event_log=event_log,
        )

    def run_attempt(
        self,
        condition: ExperimentCondition,
        *,
        attempt_index: int,
        started_at_utc: datetime | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> SimulatedAttemptOutcome:
        started = started_at_utc or now()
        raw_readings: list[RawTransportReading] = []
        station_samples: list[RawStationSample] = []
        last_confirmed = self.cryostat.read_state()
        try:
            self.cryostat.ensure_temperature_control(True)
            self.cryostat.set_temperature(condition.temperature_k)
            last_confirmed = self.cryostat.wait_for_temperature(
                self.temperature_max_polls
            )
            self.cryostat.ensure_field_control(True)
            self.cryostat.set_vector_field(condition.field)
            last_confirmed = self.cryostat.wait_for_field(self.field_max_polls)

            self.lockin_xx.configure_reference()
            self.lockin_xy.configure_reference()
            self.lockin_xx.configure_excitation(
                condition.excitation_v, condition.frequency_hz
            )
            self.gate_top.set_voltage(0.0)
            self.gate_top.enable_output()
            self.gate_top.set_voltage(condition.gate_top_v)
            self.gate_bottom.set_voltage(0.0)
            self.gate_bottom.enable_output()
            self.gate_bottom.set_voltage(condition.gate_bottom_v)

            station_samples.append(
                RawStationSample(
                    condition_id=condition.condition_id,
                    attempt_index=attempt_index,
                    captured_at_utc=now(),
                    cryostat=last_confirmed,
                    gate_top=self.gate_top.read_state(),
                    gate_bottom=self.gate_bottom.read_state(),
                    gate_top_leakage_limit_a=self.gate_top.leakage_limit_a,
                    gate_bottom_leakage_limit_a=self.gate_bottom.leakage_limit_a,
                )
            )

            for harmonic in (1, 2, 3):
                for lockin in (self.lockin_xx, self.lockin_xy):
                    reading = lockin.read_harmonic(harmonic)
                    raw = RawTransportReading(
                        condition_id=condition.condition_id,
                        attempt_index=attempt_index,
                        captured_at_utc=now(),
                        reading=reading,
                    )
                    raw_readings.append(raw)
                    if not reading.locked:
                        raise AcquisitionRejected(
                            f"lockin_{reading.role.value} reference unlocked."
                        )
                    if reading.overload:
                        raise AcquisitionRejected(
                            f"lockin_{reading.role.value} reported overload."
                        )

            accepted_result = AcceptedTransportResult(
                condition_id=condition.condition_id,
                attempt_index=attempt_index,
                readings=tuple(raw_readings),
            )
            attempt = AttemptRecord(
                condition_id=condition.condition_id,
                attempt_index=attempt_index,
                started_at_utc=started,
                completed_at_utc=now(),
                status=AttemptStatus.ACCEPTED,
            )
            return SimulatedAttemptOutcome(
                attempt=attempt,
                raw_readings=tuple(raw_readings),
                station_samples=tuple(station_samples),
                accepted_result=accepted_result,
                cleanup=None,
                last_confirmed_cryostat_state=last_confirmed,
            )
        except KeyboardInterrupt as exc:
            cleanup = cleanup_after_failure(
                lockin_xx=self.lockin_xx,
                lockin_xy=self.lockin_xy,
                gate_top=self.gate_top,
                gate_bottom=self.gate_bottom,
                cryostat=self.cryostat,
                last_confirmed_cryostat_state=last_confirmed,
            )
            exc.attempt_state = InterruptedAttemptState(
                raw_readings=tuple(raw_readings),
                station_samples=tuple(station_samples),
                cleanup=cleanup,
            )
            # Retain the existing convenience attribute for direct callers.
            exc.cleanup_report = cleanup
            raise
        except Exception as exc:
            cleanup = cleanup_after_failure(
                lockin_xx=self.lockin_xx,
                lockin_xy=self.lockin_xy,
                gate_top=self.gate_top,
                gate_bottom=self.gate_bottom,
                cryostat=self.cryostat,
                last_confirmed_cryostat_state=last_confirmed,
            )
            attempt = AttemptRecord(
                condition_id=condition.condition_id,
                attempt_index=attempt_index,
                started_at_utc=started,
                completed_at_utc=now(),
                status=AttemptStatus.REJECTED,
                rejection_reason=f"{type(exc).__name__}: {exc}",
            )
            return SimulatedAttemptOutcome(
                attempt=attempt,
                raw_readings=tuple(raw_readings),
                station_samples=tuple(station_samples),
                accepted_result=None,
                cleanup=cleanup,
                last_confirmed_cryostat_state=cleanup.last_confirmed_cryostat_state,
            )

    def shutdown_normal(
        self,
        *,
        zero_field: bool,
        last_confirmed_cryostat_state: CryostatState,
    ) -> CleanupReport:
        return cleanup_after_normal_completion(
            lockin_xx=self.lockin_xx,
            lockin_xy=self.lockin_xy,
            gate_top=self.gate_top,
            gate_bottom=self.gate_bottom,
            cryostat=self.cryostat,
            last_confirmed_cryostat_state=last_confirmed_cryostat_state,
            zero_field=zero_field,
        )
