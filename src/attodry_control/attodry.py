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
from .safety import (
    CONFIRMED_FIELD_TOLERANCE_MAX_T,
    FIELD_SETPOINT_READBACK_TOLERANCE_T,
    MagnetLimits,
    plan_zero_detour,
    validate_vector_field,
)
from .stability import (
    TimedValue,
    evaluate_readback_stability,
    evaluate_stability,
)


TEMPERATURE_COMMAND_ACK_TIMEOUT_S = 30.0
FIELD_COMMAND_ACK_TIMEOUT_S = 30.0
FieldSampleCallback = Callable[
    [CryostatState, float, str, int | None],
    None,
]


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
        field_tolerance = field_stability.criteria.tolerance
        if (
            field_tolerance is None
            or field_tolerance > CONFIRMED_FIELD_TOLERANCE_MAX_T
        ):
            raise ValueError(
                "AttoDryDriver field tolerance must be configured and cannot "
                f"exceed {CONFIRMED_FIELD_TOLERANCE_MAX_T:g} T."
            )
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
                if self._get_control_flag(
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
        try:
            field = VectorField(
                self._get_float(
                    "getMagneticFieldX", "AttoDRY_Interface_getMagneticFieldX"
                ),
                self._get_float(
                    "getMagneticFieldZ", "AttoDRY_Interface_getMagneticFieldZ"
                ),
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
        except ValueError as exc:
            raise AttoDryError(
                f"attoDRY returned an invalid full-state readback: {exc}"
            ) from exc
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
            acknowledgment_timeout_s=TEMPERATURE_COMMAND_ACK_TIMEOUT_S,
            acknowledgment_poll_interval_s=self.temperature_stability.poll_interval_s,
            monotonic=monotonic,
            sleeper=sleeper,
        )

    def ensure_field_control(
        self,
        enabled: bool,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        on_sample: FieldSampleCallback | None = None,
    ) -> None:
        def record(state: CryostatState, elapsed_s: float) -> None:
            if on_sample is not None:
                on_sample(state, elapsed_s, "field_control_ack", None)
            self._validate_field_state(state)

        def validate_safe_takeover(state: CryostatState) -> None:
            if not enabled:
                return
            tolerance = self.field_stability.criteria.tolerance
            if (
                tolerance is None
                or tolerance > CONFIRMED_FIELD_TOLERANCE_MAX_T
            ):
                raise AttoDryError(
                    "Field-control takeover requires a configured field tolerance "
                    f"no greater than {CONFIRMED_FIELD_TOLERANCE_MAX_T:g} T."
                )
            mismatch = math.hypot(
                state.field.bx_t - state.field_setpoint.bx_t,
                state.field.bz_t - state.field_setpoint.bz_t,
            )
            if mismatch > tolerance:
                raise AttoDryError(
                    "Refusing to enable field control because actual field and "
                    "the latent setpoint do not agree within the configured "
                    "field tolerance. Establish a manually verified safe state first."
                )
            # The controller's internal axis order is not established.  Both
            # possible mixed corners must therefore satisfy the vector limit
            # before enabling a latent setpoint can be considered safe.
            validate_vector_field(
                VectorField(state.field.bx_t, state.field_setpoint.bz_t),
                self.limits,
            )
            validate_vector_field(
                VectorField(state.field_setpoint.bx_t, state.field.bz_t),
                self.limits,
            )

        self._ensure_control(
            enabled=enabled,
            state_attribute="field_control_enabled",
            toggle_label="toggleMagneticFieldControl",
            toggle_symbol="AttoDRY_Interface_toggleMagneticFieldControl",
            acknowledgment_timeout_s=FIELD_COMMAND_ACK_TIMEOUT_S,
            acknowledgment_poll_interval_s=self.field_stability.poll_interval_s,
            monotonic=monotonic,
            sleeper=sleeper,
            on_sample=record,
            before_toggle=validate_safe_takeover,
        )

    def set_vector_field(
        self,
        target: VectorField,
        *,
        max_step_t: float = 0.05,
        planning_start: VectorField | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        on_sample: FieldSampleCallback | None = None,
    ) -> CryostatState:
        self._require_write_authorized()
        if (
            not math.isfinite(max_step_t)
            or max_step_t <= FIELD_SETPOINT_READBACK_TOLERANCE_T
        ):
            raise ValueError(
                "max_step_t must exceed the "
                f"{FIELD_SETPOINT_READBACK_TOLERANCE_T:g} T setpoint-readback "
                "acknowledgement tolerance."
            )
        checked = validate_vector_field(target, self.limits)
        state = self.read_state()
        if on_sample is not None:
            on_sample(state, 0.0, "setpoint_preflight", None)
        self._validate_field_state(state, require_control=True)
        plan_start = state.field_setpoint
        if planning_start is not None:
            plan_start = validate_vector_field(planning_start, self.limits)
            if not self._field_matches(state.field_setpoint, plan_start):
                raise AttoDryError(
                    "Confirmed setpoint does not match the supplied planning start."
                )
        if self._field_matches(state.field_setpoint, checked):
            if on_sample is not None:
                on_sample(state, 0.0, "setpoint_already_confirmed", None)
            return state

        def wait_at_setpoint(
            expected: VectorField,
            *,
            phase: str,
            waypoint_index: int | None,
        ) -> CryostatState:
            def stability_sample(
                sample_state: CryostatState,
                elapsed_s: float,
                _phase: str,
                _waypoint_index: int | None,
            ) -> None:
                if on_sample is not None:
                    on_sample(
                        sample_state,
                        elapsed_s,
                        phase,
                        waypoint_index,
                    )

            return self.wait_for_field(
                expected,
                monotonic=monotonic,
                sleeper=sleeper,
                on_sample=stability_sample,
            )

        # Planning validates the complete waypoint sequence and every X-first
        # mixed transient before the first setting write is issued.
        waypoints = plan_zero_detour(
            plan_start,
            checked,
            max_step_t,
            self.limits,
        )
        # Do not begin a new setpoint path while the actual field is still
        # converging toward the confirmed starting setpoint.
        current_state = wait_at_setpoint(
            plan_start,
            phase="planning_start_stability",
            waypoint_index=None,
        )
        for waypoint_index, point in enumerate(waypoints):
            previous_setpoint = current_state.field_setpoint
            command_x = float(ctypes.c_float(point.bx_t).value)
            after_x = VectorField(point.bx_t, previous_setpoint.bz_t)
            validate_vector_field(
                VectorField(command_x, previous_setpoint.bz_t), self.limits
            )
            if (
                abs(command_x - previous_setpoint.bx_t)
                > max_step_t + FIELD_SETPOINT_READBACK_TOLERANCE_T
            ):
                raise AttoDryError(
                    "Confirmed X setpoint fell behind the prevalidated path; "
                    "refusing a component step larger than max_step_t."
                )
            if not math.isclose(
                previous_setpoint.bx_t,
                point.bx_t,
                rel_tol=0.0,
                abs_tol=FIELD_SETPOINT_READBACK_TOLERANCE_T,
            ):
                self._call(
                    "setUserMagneticFieldX",
                    "AttoDRY_Interface_setUserMagneticFieldX",
                    ctypes.c_float(command_x),
                )
                current_state = self._wait_for_field_setpoint(
                    after_x,
                    waypoint_index=waypoint_index,
                    phase="setpoint_x_ack",
                    monotonic=monotonic,
                    sleeper=sleeper,
                    on_sample=on_sample,
                )
            elif on_sample is not None:
                on_sample(
                    current_state,
                    0.0,
                    "setpoint_x_unchanged",
                    waypoint_index,
                )

            command_z = float(ctypes.c_float(point.bz_t).value)
            after_z = VectorField(current_state.field_setpoint.bx_t, point.bz_t)
            validate_vector_field(
                VectorField(current_state.field_setpoint.bx_t, command_z),
                self.limits,
            )
            if (
                abs(command_z - current_state.field_setpoint.bz_t)
                > max_step_t + FIELD_SETPOINT_READBACK_TOLERANCE_T
            ):
                raise AttoDryError(
                    "Confirmed Z setpoint fell behind the prevalidated path; "
                    "refusing a component step larger than max_step_t."
                )
            if not math.isclose(
                current_state.field_setpoint.bz_t,
                point.bz_t,
                rel_tol=0.0,
                abs_tol=FIELD_SETPOINT_READBACK_TOLERANCE_T,
            ):
                self._call(
                    "setUserMagneticFieldZ",
                    "AttoDRY_Interface_setUserMagneticFieldZ",
                    ctypes.c_float(command_z),
                )
                current_state = self._wait_for_field_setpoint(
                    after_z,
                    waypoint_index=waypoint_index,
                    phase="setpoint_z_ack",
                    monotonic=monotonic,
                    sleeper=sleeper,
                    on_sample=on_sample,
                )
            elif on_sample is not None:
                on_sample(
                    current_state,
                    0.0,
                    "setpoint_z_unchanged",
                    waypoint_index,
                )

            # Intermediate waypoints must be reached and stable before another
            # component write.  The executor owns the final explicit-target
            # dwell so point completion has exactly one stability window.
            if waypoint_index < len(waypoints) - 1:
                current_state = wait_at_setpoint(
                    point,
                    phase="waypoint_stability",
                    waypoint_index=waypoint_index,
                )
                self._validate_field_state(current_state, require_control=True)
                if on_sample is not None:
                    on_sample(
                        current_state,
                        0.0,
                        "waypoint_confirmed",
                        waypoint_index,
                    )

        if not self._field_matches(current_state.field_setpoint, checked):
            raise AttoDryError("Vector-field setpoint readback does not match target.")
        return current_state

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
        on_sample: FieldSampleCallback | None = None,
    ) -> CryostatState:
        checked = validate_vector_field(target, self.limits)
        tolerance = self.field_stability.criteria.tolerance
        if tolerance is None:
            raise ValueError("Field stability requires a configured tolerance.")
        started = monotonic()
        x_samples: deque[TimedValue] = deque()
        z_samples: deque[TimedValue] = deque()
        magnitude_samples: deque[TimedValue] = deque()
        while True:
            state = self.read_state()
            elapsed = monotonic() - started
            if on_sample is not None:
                on_sample(state, elapsed, "field_stability", None)
            self._validate_field_state(state)
            if not self._field_matches(state.field_setpoint, checked):
                raise AttoDryError(
                    "Vector-field setpoint changed while waiting for field stability."
                )
            if state.field_control_enabled:
                x_samples.append(TimedValue(elapsed, state.field.bx_t))
                z_samples.append(TimedValue(elapsed, state.field.bz_t))
                magnitude_samples.append(TimedValue(elapsed, state.field.magnitude_t))
                cutoff = elapsed - self.field_stability.criteria.dwell_s
                # Keep one sample before the cutoff so jittered polling can
                # still prove full dwell coverage.
                while len(x_samples) > 1 and x_samples[1].elapsed_s < cutoff:
                    x_samples.popleft()
                    z_samples.popleft()
                    magnitude_samples.popleft()
                stable = evaluate_stability(
                    tuple(x_samples), checked.bx_t, self.field_stability.criteria
                ) and evaluate_stability(
                    tuple(z_samples), checked.bz_t, self.field_stability.criteria
                )
                if checked.magnitude_t == 0.0:
                    stable = stable and evaluate_stability(
                        tuple(magnitude_samples),
                        0.0,
                        self.field_stability.criteria,
                    )
                if stable:
                    return state
            else:
                x_samples.clear()
                z_samples.clear()
                magnitude_samples.clear()
            if elapsed >= self.field_stability.wait_timeout_s:
                raise AttoDryTimeout("Field stability wait timed out.")
            sleeper(
                min(
                    self.field_stability.poll_interval_s,
                    self.field_stability.wait_timeout_s - elapsed,
                )
            )

    def request_zero_field(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        on_sample: FieldSampleCallback | None = None,
    ) -> CryostatState:
        self._require_write_authorized()
        state = self.read_state()
        if on_sample is not None:
            on_sample(state, 0.0, "zero_preflight", None)
        self._validate_field_state(state, require_control=True)
        self._call("sweepFieldToZero", "AttoDRY_Interface_sweepFieldToZero")
        zero = VectorField(0.0, 0.0)
        self._wait_for_field_setpoint(
            zero,
            waypoint_index=None,
            phase="zero_setpoint_ack",
            monotonic=monotonic,
            sleeper=sleeper,
            on_sample=on_sample,
        )
        confirmed = self.wait_for_field(
            zero,
            monotonic=monotonic,
            sleeper=sleeper,
            on_sample=on_sample,
        )
        tolerance = self.field_stability.criteria.tolerance
        if (
            tolerance is None
            or confirmed.field.magnitude_t > tolerance
            or confirmed.field_setpoint.magnitude_t > tolerance
        ):
            raise AttoDryError(
                "Zero-field actual or setpoint readback is outside the configured "
                "field tolerance."
            )
        return confirmed

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
        acknowledgment_timeout_s: float = 0.0,
        acknowledgment_poll_interval_s: float = 0.0,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        on_sample: Callable[[CryostatState, float], None] | None = None,
        before_toggle: Callable[[CryostatState], None] | None = None,
    ) -> None:
        state = self.read_state()
        if on_sample is not None:
            on_sample(state, 0.0)
        self._require_clear_error(state)
        if bool(getattr(state, state_attribute)) == enabled:
            return
        self._require_write_authorized()
        if before_toggle is not None:
            before_toggle(state)
        self._call(toggle_label, toggle_symbol)
        started = monotonic()
        while True:
            confirmed = self.read_state()
            elapsed = monotonic() - started
            if on_sample is not None:
                on_sample(confirmed, elapsed)
            self._require_clear_error(confirmed)
            reached = bool(getattr(confirmed, state_attribute)) == enabled
            if acknowledgment_timeout_s <= 0:
                if reached:
                    return
                raise AttoDryError(
                    f"{state_attribute} readback did not reach {enabled}."
                )
            if elapsed >= acknowledgment_timeout_s:
                raise AttoDryTimeout(
                    f"{state_attribute} readback did not reach {enabled} within "
                    f"{acknowledgment_timeout_s:g} s."
                )
            if reached:
                return
            sleeper(
                min(
                    acknowledgment_poll_interval_s,
                    acknowledgment_timeout_s - elapsed,
                )
            )

    def _wait_for_field_setpoint(
        self,
        expected: VectorField,
        *,
        waypoint_index: int | None,
        phase: str,
        monotonic: Callable[[], float],
        sleeper: Callable[[float], None],
        on_sample: FieldSampleCallback | None,
    ) -> CryostatState:
        validate_vector_field(expected, self.limits)
        started = monotonic()
        while True:
            state = self.read_state()
            elapsed = monotonic() - started
            if on_sample is not None:
                on_sample(state, elapsed, phase, waypoint_index)
            self._validate_field_state(state, require_control=True)
            if self._field_matches(state.field_setpoint, expected):
                return state
            if elapsed >= FIELD_COMMAND_ACK_TIMEOUT_S:
                raise AttoDryTimeout(
                    "Vector-field setpoint readback did not reach the requested "
                    f"{phase} state within {FIELD_COMMAND_ACK_TIMEOUT_S:g} s."
                )
            sleeper(
                min(
                    self.field_stability.poll_interval_s,
                    FIELD_COMMAND_ACK_TIMEOUT_S - elapsed,
                )
            )

    def _validate_field_state(
        self, state: CryostatState, *, require_control: bool = False
    ) -> None:
        validate_vector_field(state.field, self.limits)
        validate_vector_field(state.field_setpoint, self.limits)
        self._require_clear_error(state)
        if require_control and not state.field_control_enabled:
            raise AttoDryError("Field control is not confirmed enabled.")

    @staticmethod
    def _field_matches(actual: VectorField, expected: VectorField) -> bool:
        return math.isclose(
            actual.bx_t,
            expected.bx_t,
            rel_tol=0.0,
            abs_tol=FIELD_SETPOINT_READBACK_TOLERANCE_T,
        ) and math.isclose(
            actual.bz_t,
            expected.bz_t,
            rel_tol=0.0,
            abs_tol=FIELD_SETPOINT_READBACK_TOLERANCE_T,
        )

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
