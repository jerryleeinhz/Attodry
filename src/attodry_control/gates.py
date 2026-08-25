from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Callable, Protocol

from .models import GateState


class GateSafetyError(RuntimeError):
    """Base class for a gate operation that could not be confirmed safe."""


class GateWriteNotAuthorized(GateSafetyError):
    pass


class GateLeakageTrip(GateSafetyError):
    pass


class GateReadbackMismatch(GateSafetyError):
    pass


class GatePreflightRejected(GateSafetyError):
    pass


@dataclass(frozen=True, slots=True)
class GatePreflightState:
    """Query-only physical state needed before a gate backend may write."""

    identity: str
    output_enabled: bool
    source_setpoint_v: float
    voltage_read_v: float
    current_read_a: float
    status: str | None = None


class GateBackend(Protocol):
    """Model-specific primitive operations; implementations must check I/O errors."""

    def set_current_compliance(self, current_a: float) -> None: ...

    def preflight(self) -> GatePreflightState: ...

    def set_output(self, enabled: bool) -> None: ...

    def set_voltage(self, voltage_v: float) -> None: ...

    def measure_voltage(self) -> float: ...

    def measure_current(self) -> float: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class GateSafetyLimits:
    max_abs_voltage_v: float
    compliance_a: float
    leakage_limit_a: float
    ramp_step_v: float
    readback_tolerance_v: float
    settle_s: float = 0.0

    def __post_init__(self) -> None:
        for name, value in (
            ("max_abs_voltage_v", self.max_abs_voltage_v),
            ("compliance_a", self.compliance_a),
            ("leakage_limit_a", self.leakage_limit_a),
            ("ramp_step_v", self.ramp_step_v),
            ("readback_tolerance_v", self.readback_tolerance_v),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive.")
        if self.leakage_limit_a > self.compliance_a:
            raise ValueError("leakage_limit_a cannot exceed compliance_a.")
        if not math.isfinite(self.settle_s) or self.settle_s < 0:
            raise ValueError("settle_s must be finite and non-negative.")


def validate_gate_preflight(
    role: str,
    state: GatePreflightState,
    limits: GateSafetyLimits,
) -> None:
    """Reject any unconfirmed, enabled, or non-zero state without writing."""

    if not state.identity.strip():
        raise GatePreflightRejected(f"gate_{role} returned an empty identity")
    values = {
        "source setpoint": state.source_setpoint_v,
        "voltage readback": state.voltage_read_v,
        "current readback": state.current_read_a,
    }
    for name, value in values.items():
        if not math.isfinite(value):
            raise GatePreflightRejected(f"gate_{role} {name} is not finite")
    if state.output_enabled:
        raise GatePreflightRejected(
            f"gate_{role} output is already enabled; check the front panel manually"
        )
    if abs(state.source_setpoint_v) > limits.readback_tolerance_v:
        raise GatePreflightRejected(
            f"gate_{role} source setpoint is not confirmed at zero"
        )
    if abs(state.voltage_read_v) > limits.readback_tolerance_v:
        raise GatePreflightRejected(
            f"gate_{role} voltage readback is not confirmed at zero"
        )
    if state.status is not None and not _status_is_clean(state.status):
        raise GatePreflightRejected(f"gate_{role} instrument status is not clean")


class SafeGateController:
    """Fail-closed gate controller independent of a particular SMU command set."""

    def __init__(
        self,
        *,
        role: str,
        backend: GateBackend,
        limits: GateSafetyLimits,
        writes_authorized: bool = False,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not role.strip():
            raise ValueError("role must be non-empty.")
        self.role = role
        self.backend = backend
        self.limits = limits
        self.writes_authorized = writes_authorized
        self.sleep = sleep
        self.last_confirmed_state: GateState | None = None
        self.events: list[str] = []
        self._last_commanded_voltage_v = 0.0
        self._output_enabled = False
        self._preflight_confirmed = False

    def preflight(self) -> GatePreflightState:
        """Query and validate the real instrument state before any write."""

        state = self.backend.preflight()
        validate_gate_preflight(self.role, state, self.limits)
        self._last_commanded_voltage_v = 0.0
        self._output_enabled = False
        self._preflight_confirmed = True
        self.events.append(f"gate_{self.role}.preflight_confirmed")
        return state

    def read_state(self) -> GateState:
        voltage = self._finite_readback(
            self.backend.measure_voltage(), "gate voltage readback"
        )
        current = self._finite_readback(
            self.backend.measure_current(), "gate current readback"
        )
        state = GateState(
            role=self.role,
            voltage_set_v=self._last_commanded_voltage_v,
            voltage_read_v=voltage,
            leakage_current_a=current if self._output_enabled else None,
            compliance_a=self.limits.compliance_a,
            output_enabled=self._output_enabled,
        )
        self.last_confirmed_state = state
        return state

    def enable_output(self) -> None:
        self._require_write_authorization()
        if not self._preflight_confirmed:
            self.preflight()
        try:
            self.backend.set_current_compliance(self.limits.compliance_a)
            self.backend.set_voltage(0.0)
            self._last_commanded_voltage_v = 0.0
            self.backend.set_output(True)
            self._output_enabled = True
            self.events.append(f"gate_{self.role}.output_enabled_at_zero")
            self._confirm_step(0.0)
        except Exception:
            self._fail_closed()
            raise

    def set_voltage(self, voltage_v: float) -> None:
        self._require_write_authorization()
        target = self._validate_target(voltage_v)
        try:
            for step in ramp_values(
                self._last_commanded_voltage_v,
                target,
                self.limits.ramp_step_v,
            ):
                self.backend.set_voltage(step)
                self._last_commanded_voltage_v = step
                self.events.append(f"gate_{self.role}.set_voltage:{step:.12g}")
                if self.limits.settle_s:
                    self.sleep(self.limits.settle_s)
                self._confirm_step(step)
        except Exception:
            self._fail_closed()
            raise

    def disable_output(self) -> None:
        self._require_write_authorization()
        pending_error: Exception | None = None
        try:
            self.set_voltage(0.0)
        except Exception as exc:
            pending_error = exc
        try:
            self.backend.set_output(False)
            self._output_enabled = False
            self.events.append(f"gate_{self.role}.output_disabled")
        except Exception as exc:
            if pending_error is None:
                pending_error = exc
        if pending_error is not None:
            raise GateSafetyError(
                f"gate_{self.role} could not confirm zero and disabled output"
            ) from pending_error

    def close(self) -> None:
        self.backend.close()

    def _confirm_step(self, expected_voltage_v: float) -> GateState:
        state = self.read_state()
        if abs(state.voltage_read_v - expected_voltage_v) > (
            self.limits.readback_tolerance_v
        ):
            raise GateReadbackMismatch(
                f"gate_{self.role} readback {state.voltage_read_v:g} V does not "
                f"match commanded {expected_voltage_v:g} V within "
                f"{self.limits.readback_tolerance_v:g} V."
            )
        leakage = state.leakage_current_a
        if leakage is not None and abs(leakage) > self.limits.leakage_limit_a:
            raise GateLeakageTrip(
                f"gate_{self.role} leakage {leakage:g} A exceeds "
                f"{self.limits.leakage_limit_a:g} A."
            )
        return state

    def _fail_closed(self) -> None:
        self.events.append(f"gate_{self.role}.fail_closed_started")
        try:
            for step in ramp_values(
                self._last_commanded_voltage_v,
                0.0,
                self.limits.ramp_step_v,
            ):
                try:
                    self.backend.set_voltage(step)
                    self._last_commanded_voltage_v = step
                except Exception:
                    self.events.append(f"gate_{self.role}.emergency_zero_step_failed")
        finally:
            try:
                self.backend.set_output(False)
                self._output_enabled = False
                self.events.append(f"gate_{self.role}.fail_closed_output_disabled")
            except Exception:
                self.events.append(f"gate_{self.role}.disable_unconfirmed")

    def _require_write_authorization(self) -> None:
        if not self.writes_authorized:
            raise GateWriteNotAuthorized(
                f"Writes to gate_{self.role} require explicit authorization."
            )

    def _validate_target(self, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("Gate target must be finite.")
        if abs(value) > self.limits.max_abs_voltage_v:
            raise GateSafetyError(
                f"gate_{self.role} target {value:g} V exceeds the configured "
                f"absolute limit {self.limits.max_abs_voltage_v:g} V."
            )
        return float(value)

    @staticmethod
    def _finite_readback(value: float, name: str) -> float:
        if not math.isfinite(value):
            raise GateSafetyError(f"{name} must be finite.")
        return float(value)


def _status_is_clean(status: str) -> bool:
    prefix = status.strip().split(",", 1)[0].strip()
    try:
        return int(float(prefix)) == 0
    except ValueError:
        return False


def ramp_values(start_v: float, stop_v: float, max_step_v: float) -> tuple[float, ...]:
    """Return inclusive ramp endpoints without ever exceeding max_step_v."""

    if any(not math.isfinite(value) for value in (start_v, stop_v, max_step_v)):
        raise ValueError("Ramp values must be finite.")
    if max_step_v <= 0:
        raise ValueError("max_step_v must be positive.")
    delta = stop_v - start_v
    if delta == 0:
        return ()
    count = math.ceil(abs(delta) / max_step_v)
    return tuple(start_v + delta * index / count for index in range(1, count + 1))
