from __future__ import annotations

from collections import deque
import ctypes
from dataclasses import dataclass
import math
from pathlib import Path
import time
from typing import Callable

from .config import (
    ControlConfig,
    RunMode,
    StabilityConfig,
    TemperatureStabilityMode,
)
from .models import CryostatState, VectorField
from .safety import MagnetLimits, validate_vector_field
from .stability import (
    TimedValue,
    evaluate_readback_stability,
    evaluate_stability,
)


TEMPERATURE_COMMAND_ACK_TIMEOUT_S = 30.0


class AttoDryError(RuntimeError):
    pass


class AttoDryDllError(AttoDryError):
    pass


class AttoDryTimeout(AttoDryError):
    pass


class AttoDryAuthorizationError(AttoDryError):
    pass


@dataclass(frozen=True)
class HeaterPowerState:
    sample_w: float
    vti_w: float


def load_attodry_dll(path: str | Path) -> object:
    dll_path = Path(path)
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        raise AttoDryDllError("attoDRYxyz64bit.dll requires 64-bit Python.")
    if not dll_path.is_file():
        raise AttoDryDllError(f"Configured attoDRY DLL does not exist: {dll_path}")
    if dll_path.suffix.lower() != ".dll":
        raise AttoDryDllError("Configured attoDRY library must be a .dll file.")
    dll = ctypes.CDLL(str(dll_path))
    configure_attodry_signatures(dll)
    return dll


def configure_attodry_signatures(dll: object) -> None:
    signatures = {
        "AttoDRY_Interface_begin": ([ctypes.c_ushort], ctypes.c_int),
        "AttoDRY_Interface_Connect": ([ctypes.c_char_p], ctypes.c_int),
        "AttoDRY_Interface_Disconnect": ([], ctypes.c_int),
        "AttoDRY_Interface_end": ([], ctypes.c_int),
        "AttoDRY_Interface_isDeviceInitialised": (
            [ctypes.POINTER(ctypes.c_int)],
            ctypes.c_int,
        ),
        "AttoDRY_Interface_getSampleTemperature": (
            [ctypes.POINTER(ctypes.c_float)],
            ctypes.c_int,
        ),
        "AttoDRY_Interface_getUserTemperature": (
            [ctypes.POINTER(ctypes.c_float)],
            ctypes.c_int,
        ),
        "AttoDRY_Interface_getVtiTemperature": (
            [ctypes.POINTER(ctypes.c_float)],
            ctypes.c_int,
        ),
        "AttoDRY_Interface_getSampleHeaterPower": (
            [ctypes.POINTER(ctypes.c_float)],
            ctypes.c_int,
        ),
        "AttoDRY_Interface_getVtiHeaterPower": (
            [ctypes.POINTER(ctypes.c_float)],
            ctypes.c_int,
        ),
        "AttoDRY_Interface_getMagneticFieldX": (
            [ctypes.POINTER(ctypes.c_float)],
            ctypes.c_int,
        ),
        "AttoDRY_Interface_getMagneticFieldZ": (
            [ctypes.POINTER(ctypes.c_float)],
            ctypes.c_int,
        ),
        "AttoDRY_Interface_getMagneticFieldSetPointX": (
            [ctypes.POINTER(ctypes.c_float)],
            ctypes.c_int,
        ),
        "AttoDRY_Interface_getMagneticFieldSetPointZ": (
            [ctypes.POINTER(ctypes.c_float)],
            ctypes.c_int,
        ),
        "AttoDRY_Interface_isControllingTemperature": (
            [ctypes.POINTER(ctypes.c_int)],
            ctypes.c_int,
        ),
        "AttoDRY_Interface_isControllingField": (
            [ctypes.POINTER(ctypes.c_int)],
            ctypes.c_int,
        ),
        "AttoDRY_Interface_getAttodryErrorStatus": (
            [ctypes.POINTER(ctypes.c_int8)],
            ctypes.c_int,
        ),
        "AttoDRY_Interface_setUserTemperature": ([ctypes.c_float], ctypes.c_int),
        "AttoDRY_Interface_setUserMagneticFieldX": (
            [ctypes.c_float],
            ctypes.c_int,
        ),
        "AttoDRY_Interface_setUserMagneticFieldZ": (
            [ctypes.c_float],
            ctypes.c_int,
        ),
        "AttoDRY_Interface_toggleFullTemperatureControl": ([], ctypes.c_int),
        "AttoDRY_Interface_toggleMagneticFieldControl": ([], ctypes.c_int),
        "AttoDRY_Interface_sweepFieldToZero": ([], ctypes.c_int),
    }
    for name, (argtypes, restype) in signatures.items():
        try:
            function = getattr(dll, name)
        except AttributeError as exc:
            raise AttoDryDllError(f"attoDRY DLL is missing required symbol {name}.") from exc
        try:
            function.argtypes = argtypes
            function.restype = restype
        except AttributeError:
            # Plain Python fake-DLL bound methods intentionally do not expose
            # ctypes function attributes.
            pass


class AttoDryDriver:
    def __init__(
        self,
        *,
        dll: object,
        com_port: str,
        device_type: int,
        connection_timeout_s: float,
        temperature_min_k: float,
        temperature_max_k: float,
        limits: MagnetLimits,
        field_stability: StabilityConfig,
        temperature_stability: StabilityConfig,
        connection_authorized: bool,
        writes_authorized: bool,
    ) -> None:
        configure_attodry_signatures(dll)
        self.dll = dll
        self.com_port = com_port
        self.device_type = device_type
        self.connection_timeout_s = connection_timeout_s
        self.temperature_min_k = temperature_min_k
        self.temperature_max_k = temperature_max_k
        self.limits = limits
        self.field_stability = field_stability
        self.temperature_stability = temperature_stability
        self.connection_authorized = connection_authorized
        self.writes_authorized = writes_authorized
        self.connected = False
        self.last_confirmed_state: CryostatState | None = None

    @classmethod
    def from_config(
        cls,
        config: ControlConfig,
        *,
        dll: object | None = None,
        connection_authorized: bool = False,
        writes_authorized: bool = False,
    ) -> AttoDryDriver:
        if config.project.mode is not RunMode.HARDWARE:
            raise ValueError("AttoDryDriver requires hardware configuration.")
        cryostat = config.cryostat
        if (
            cryostat.com_port is None
            or cryostat.dll_path is None
            or cryostat.device_type is None
            or cryostat.connection_timeout_s is None
        ):
            raise ValueError("Hardware cryostat configuration is incomplete.")
        loaded_dll = dll if dll is not None else load_attodry_dll(cryostat.dll_path)
        return cls(
            dll=loaded_dll,
            com_port=cryostat.com_port,
            device_type=cryostat.device_type,
            connection_timeout_s=cryostat.connection_timeout_s,
            temperature_min_k=cryostat.temperature_min_k,
            temperature_max_k=cryostat.temperature_max_k,
            limits=config.magnet.limits,
            field_stability=config.magnet.stability,
            temperature_stability=config.temperature_stability,
            connection_authorized=connection_authorized,
            writes_authorized=writes_authorized,
        )

    def connect(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if not self.connection_authorized:
            raise AttoDryAuthorizationError(
                "attoDRY hardware connection was not explicitly authorized."
            )
        if self.connected:
            raise AttoDryError("attoDRY driver is already connected.")
        started = monotonic()
        begun = False
        try:
            self._call("begin", "AttoDRY_Interface_begin", ctypes.c_ushort(self.device_type))
            begun = True
            self._call(
                "Connect",
                "AttoDRY_Interface_Connect",
                ctypes.c_char_p(self.com_port.encode("ascii")),
            )
            self.connected = True
            while True:
                if self._get_int(
                    "isDeviceInitialised",
                    "AttoDRY_Interface_isDeviceInitialised",
                ):
                    return
                if monotonic() - started >= self.connection_timeout_s:
                    raise AttoDryTimeout(
                        f"attoDRY initialization exceeded {self.connection_timeout_s:g} s."
                    )
                sleeper(0.1)
        except BaseException as exc:
            if self.connected:
                try:
                    self._call("Disconnect", "AttoDRY_Interface_Disconnect")
                except BaseException as cleanup_error:
                    exc.add_note(f"attoDRY disconnect cleanup also failed: {cleanup_error}")
                finally:
                    self.connected = False
            if begun:
                try:
                    self._call("end", "AttoDRY_Interface_end")
                except BaseException as cleanup_error:
                    exc.add_note(f"attoDRY end cleanup also failed: {cleanup_error}")
            raise

    def read_state(self) -> CryostatState:
        self._require_connected()
        sample_temperature = self._get_float(
            "getSampleTemperature", "AttoDRY_Interface_getSampleTemperature"
        )
        user_temperature = self._get_float(
            "getUserTemperature", "AttoDRY_Interface_getUserTemperature"
        )
        vti_temperature = self._get_float(
            "getVtiTemperature", "AttoDRY_Interface_getVtiTemperature"
        )
        field = VectorField(
            self._get_float("getMagneticFieldX", "AttoDRY_Interface_getMagneticFieldX"),
            self._get_float("getMagneticFieldZ", "AttoDRY_Interface_getMagneticFieldZ"),
        )
        field_setpoint = VectorField(
            self._get_float(
                "getMagneticFieldSetPointX",
                "AttoDRY_Interface_getMagneticFieldSetPointX",
            ),
            self._get_float(
                "getMagneticFieldSetPointZ",
                "AttoDRY_Interface_getMagneticFieldSetPointZ",
            ),
        )
        state = CryostatState(
            sample_temperature_k=sample_temperature,
            user_temperature_k=user_temperature,
            vti_temperature_k=vti_temperature,
            field=field,
            field_setpoint=field_setpoint,
            temperature_control_enabled=self._get_control_flag(
                "isControllingTemperature",
                "AttoDRY_Interface_isControllingTemperature",
            ),
            field_control_enabled=self._get_control_flag(
                "isControllingField", "AttoDRY_Interface_isControllingField"
            ),
            error_code=self._get_int8(
                "getAttodryErrorStatus",
                "AttoDRY_Interface_getAttodryErrorStatus",
            ),
        )
        self.last_confirmed_state = state
        return state

    def read_heater_powers(self) -> HeaterPowerState:
        self._require_connected()
        sample_w = self._get_float(
            "getSampleHeaterPower", "AttoDRY_Interface_getSampleHeaterPower"
        )
        vti_w = self._get_float(
            "getVtiHeaterPower", "AttoDRY_Interface_getVtiHeaterPower"
        )
        if sample_w < 0 or vti_w < 0:
            raise AttoDryError("attoDRY heater power readback cannot be negative.")
        return HeaterPowerState(sample_w=sample_w, vti_w=vti_w)

    def set_temperature(
        self,
        temperature_k: float,
        *,
        force_write: bool = False,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._require_write_authorized()
        state = self.read_state()
        self._require_clear_error(state)
        if not math.isfinite(temperature_k) or not (
            self.temperature_min_k <= temperature_k <= self.temperature_max_k
        ):
            raise ValueError("Temperature target is outside configured limits.")
        if (
            not force_write
            and math.isclose(state.user_temperature_k, temperature_k, abs_tol=1e-4)
        ):
            return
        self._call(
            "setUserTemperature",
            "AttoDRY_Interface_setUserTemperature",
            ctypes.c_float(temperature_k),
        )
        confirmed = self.read_state()
        self._require_clear_error(confirmed)
        if math.isclose(confirmed.user_temperature_k, temperature_k, abs_tol=1e-4):
            return

        started = monotonic()
        while True:
            elapsed = monotonic() - started
            if elapsed >= TEMPERATURE_COMMAND_ACK_TIMEOUT_S:
                raise AttoDryTimeout(
                    "Temperature setpoint readback did not reach target within "
                    f"{TEMPERATURE_COMMAND_ACK_TIMEOUT_S:g} s."
                )
            sleeper(
                min(
                    self.temperature_stability.poll_interval_s,
                    TEMPERATURE_COMMAND_ACK_TIMEOUT_S - elapsed,
                )
            )
            confirmed = self.read_state()
            self._require_clear_error(confirmed)
            if math.isclose(
                confirmed.user_temperature_k, temperature_k, abs_tol=1e-4
            ):
                return

    def ensure_temperature_control(
        self,
        enabled: bool,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._ensure_control(
            enabled=enabled,
            state_attribute="temperature_control_enabled",
            toggle_label="toggleFullTemperatureControl",
            toggle_symbol="AttoDRY_Interface_toggleFullTemperatureControl",
            verify_label="isControllingTemperature",
            verify_symbol="AttoDRY_Interface_isControllingTemperature",
            acknowledgment_timeout_s=TEMPERATURE_COMMAND_ACK_TIMEOUT_S,
            acknowledgment_poll_interval_s=self.temperature_stability.poll_interval_s,
            monotonic=monotonic,
            sleeper=sleeper,
        )

    def ensure_field_control(self, enabled: bool) -> None:
        self._ensure_control(
            enabled=enabled,
            state_attribute="field_control_enabled",
            toggle_label="toggleMagneticFieldControl",
            toggle_symbol="AttoDRY_Interface_toggleMagneticFieldControl",
            verify_label="isControllingField",
            verify_symbol="AttoDRY_Interface_isControllingField",
        )

    def set_vector_field(self, target: VectorField, *, max_step_t: float = 0.05) -> None:
        self._require_write_authorized()
        checked = validate_vector_field(target, self.limits)
        if not math.isfinite(max_step_t) or max_step_t <= 0:
            raise ValueError("max_step_t must be finite and positive.")
        state = self.read_state()
        self._require_clear_error(state)
        validate_vector_field(state.field_setpoint, self.limits)
        for point in _safe_zero_detour(state.field_setpoint, checked, max_step_t):
            validate_vector_field(point, self.limits)
            self._call(
                "setUserMagneticFieldX",
                "AttoDRY_Interface_setUserMagneticFieldX",
                ctypes.c_float(point.bx_t),
            )
            self._call(
                "setUserMagneticFieldZ",
                "AttoDRY_Interface_setUserMagneticFieldZ",
                ctypes.c_float(point.bz_t),
            )
        final_state = self.read_state()
        if (
            not math.isclose(final_state.field_setpoint.bx_t, checked.bx_t, abs_tol=1e-5)
            or not math.isclose(
                final_state.field_setpoint.bz_t, checked.bz_t, abs_tol=1e-5
            )
        ):
            raise AttoDryError("Vector-field setpoint readback does not match target.")

    def wait_for_temperature(
        self,
        target_k: float,
        *,
        max_overshoot_k: float | None = None,
        minimum_response_k: float = 0.0,
        response_reference_k: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        on_sample: Callable[[CryostatState, float], None] | None = None,
    ) -> CryostatState:
        if not math.isfinite(target_k) or not (
            self.temperature_min_k <= target_k <= self.temperature_max_k
        ):
            raise ValueError("Temperature target is outside configured limits.")
        if max_overshoot_k is not None and (
            not math.isfinite(max_overshoot_k) or max_overshoot_k <= 0
        ):
            raise ValueError("max_overshoot_k must be finite and positive.")
        if not math.isfinite(minimum_response_k) or minimum_response_k < 0:
            raise ValueError("minimum_response_k must be finite and non-negative.")
        if response_reference_k is not None and not math.isfinite(response_reference_k):
            raise ValueError("response_reference_k must be finite when provided.")
        if minimum_response_k > 0 and response_reference_k is None:
            raise ValueError(
                "response_reference_k is required when minimum_response_k is positive."
            )
        if (
            max_overshoot_k is not None
            and target_k + max_overshoot_k > self.temperature_max_k
        ):
            raise ValueError("Temperature overshoot limit is outside configured limits.")

        response_seen = minimum_response_k <= 0

        def record_and_check(state: CryostatState, elapsed_s: float) -> None:
            nonlocal response_seen
            if on_sample is not None:
                on_sample(state, elapsed_s)
            if (
                not response_seen
                and response_reference_k is not None
                and abs(state.sample_temperature_k - response_reference_k)
                >= minimum_response_k
            ):
                response_seen = True
            if state.error_code:
                return
            if (
                max_overshoot_k is not None
                and state.sample_temperature_k >= target_k + max_overshoot_k
            ):
                raise AttoDryError(
                    "Sample temperature reached the configured overshoot limit: "
                    f"{state.sample_temperature_k:g} K >= "
                    f"{target_k + max_overshoot_k:g} K."
                )

        return self._wait_stable(
            target=target_k,
            config=self.temperature_stability,
            value=lambda state: state.sample_temperature_k,
            control=lambda state: state.temperature_control_enabled,
            qualify=lambda _: response_seen,
            require_target=(
                self.temperature_stability.acceptance_mode
                is TemperatureStabilityMode.TARGET
            ),
            label="temperature",
            monotonic=monotonic,
            sleeper=sleeper,
            on_sample=record_and_check,
        )

    def wait_for_field(
        self,
        target: VectorField,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> CryostatState:
        checked = validate_vector_field(target, self.limits)
        started = monotonic()
        x_samples: deque[TimedValue] = deque()
        z_samples: deque[TimedValue] = deque()
        while True:
            state = self.read_state()
            elapsed = monotonic() - started
            if state.error_code:
                raise AttoDryError(f"attoDRY error code {state.error_code} while waiting for field.")
            if state.field_control_enabled:
                x_samples.append(TimedValue(elapsed, state.field.bx_t))
                z_samples.append(TimedValue(elapsed, state.field.bz_t))
                cutoff = elapsed - self.field_stability.criteria.dwell_s
                while x_samples and x_samples[0].elapsed_s < cutoff:
                    x_samples.popleft()
                    z_samples.popleft()
                if evaluate_stability(
                    tuple(x_samples), checked.bx_t, self.field_stability.criteria
                ) and evaluate_stability(
                    tuple(z_samples), checked.bz_t, self.field_stability.criteria
                ):
                    return state
            if elapsed >= self.field_stability.wait_timeout_s:
                raise AttoDryTimeout("Field stability wait timed out.")
            sleeper(self.field_stability.poll_interval_s)

    def request_zero_field(self) -> None:
        self._require_write_authorized()
        state = self.read_state()
        self._require_clear_error(state)
        self._call("sweepFieldToZero", "AttoDRY_Interface_sweepFieldToZero")
        self.wait_for_field(VectorField(0.0, 0.0))

    def close(self) -> None:
        first_error: BaseException | None = None
        if self.connected:
            try:
                self._call("Disconnect", "AttoDRY_Interface_Disconnect")
            except BaseException as exc:
                first_error = exc
            finally:
                self.connected = False
        try:
            self._call("end", "AttoDRY_Interface_end")
        except BaseException as exc:
            first_error = first_error or exc
        if first_error is not None:
            raise first_error

    def _ensure_control(
        self,
        *,
        enabled: bool,
        state_attribute: str,
        toggle_label: str,
        toggle_symbol: str,
        verify_label: str,
        verify_symbol: str,
        acknowledgment_timeout_s: float = 0.0,
        acknowledgment_poll_interval_s: float = 0.0,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        state = self.read_state()
        self._require_clear_error(state)
        if bool(getattr(state, state_attribute)) == enabled:
            return
        self._require_write_authorized()
        self._call(toggle_label, toggle_symbol)
        if self._get_control_flag(verify_label, verify_symbol) == enabled:
            return
        if acknowledgment_timeout_s <= 0:
            raise AttoDryError(f"{state_attribute} readback did not reach {enabled}.")

        started = monotonic()
        while True:
            elapsed = monotonic() - started
            if elapsed >= acknowledgment_timeout_s:
                raise AttoDryTimeout(
                    f"{state_attribute} readback did not reach {enabled} within "
                    f"{acknowledgment_timeout_s:g} s."
                )
            sleeper(
                min(
                    acknowledgment_poll_interval_s,
                    acknowledgment_timeout_s - elapsed,
                )
            )
            confirmed = self.read_state()
            self._require_clear_error(confirmed)
            if bool(getattr(confirmed, state_attribute)) == enabled:
                return

    def _wait_stable(
        self,
        *,
        target: float,
        config: StabilityConfig,
        value: Callable[[CryostatState], float],
        control: Callable[[CryostatState], bool],
        qualify: Callable[[CryostatState], bool] | None = None,
        require_target: bool = True,
        label: str,
        monotonic: Callable[[], float],
        sleeper: Callable[[float], None],
        on_sample: Callable[[CryostatState, float], None] | None = None,
    ) -> CryostatState:
        started = monotonic()
        samples: deque[TimedValue] = deque()
        while True:
            state = self.read_state()
            elapsed = monotonic() - started
            if on_sample is not None:
                on_sample(state, elapsed)
            if state.error_code:
                raise AttoDryError(
                    f"attoDRY error code {state.error_code} while waiting for {label}."
                )
            if control(state):
                samples.append(TimedValue(elapsed, value(state)))
                cutoff = elapsed - config.criteria.dwell_s
                # Keep one sample before the cutoff so a jittered poll still
                # proves the full dwell coverage without requiring an exact
                # timestamp match at the boundary.
                while len(samples) > 1 and samples[1].elapsed_s < cutoff:
                    samples.popleft()
                stable = (
                    evaluate_stability(tuple(samples), target, config.criteria)
                    if require_target
                    else evaluate_readback_stability(tuple(samples), config.criteria)
                )
                if (qualify is None or qualify(state)) and stable:
                    return state
            else:
                samples.clear()
            if elapsed >= config.wait_timeout_s:
                raise AttoDryTimeout(f"{label.capitalize()} stability wait timed out.")
            sleeper(config.poll_interval_s)

    def _get_float(self, label: str, symbol: str) -> float:
        value = ctypes.c_float()
        self._call(label, symbol, ctypes.byref(value))
        if not math.isfinite(value.value):
            raise AttoDryError(f"{label} returned a non-finite value.")
        return float(value.value)

    def _get_int(self, label: str, symbol: str) -> int:
        value = ctypes.c_int()
        self._call(label, symbol, ctypes.byref(value))
        return int(value.value)

    def _get_int8(self, label: str, symbol: str) -> int:
        value = ctypes.c_int8()
        self._call(label, symbol, ctypes.byref(value))
        return int(value.value)

    def _get_control_flag(self, label: str, symbol: str) -> bool:
        value = self._get_int(label, symbol)
        if value not in (0, 1):
            raise AttoDryError(f"{label} returned invalid control state {value}.")
        return bool(value)

    def _call(self, label: str, symbol: str, *args: object) -> None:
        code = int(getattr(self.dll, symbol)(*args))
        if code != 0:
            raise AttoDryDllError(f"{label} returned DLL error code {code}.")

    def _require_connected(self) -> None:
        if not self.connected:
            raise AttoDryError("attoDRY driver is not connected.")

    def _require_write_authorized(self) -> None:
        self._require_connected()
        if not self.writes_authorized:
            raise AttoDryAuthorizationError(
                "attoDRY setting writes were not explicitly authorized."
            )

    @staticmethod
    def _require_clear_error(state: CryostatState) -> None:
        if state.error_code:
            raise AttoDryError(
                f"attoDRY error code {state.error_code}; refusing setting write."
            )


def _safe_zero_detour(
    start: VectorField, target: VectorField, max_step_t: float
) -> tuple[VectorField, ...]:
    if start == target:
        return ()
    points: list[VectorField] = []
    if start.magnitude_t:
        steps = max(1, math.ceil(start.magnitude_t / max_step_t))
        for index in range(steps - 1, -1, -1):
            scale = index / steps
            points.append(VectorField(start.bx_t * scale, start.bz_t * scale))
    if target.magnitude_t:
        steps = max(1, math.ceil(target.magnitude_t / max_step_t))
        for index in range(1, steps + 1):
            scale = index / steps
            points.append(VectorField(target.bx_t * scale, target.bz_t * scale))
    return tuple(points)
