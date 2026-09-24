from __future__ import annotations

from collections import deque
import ctypes
from dataclasses import asdict, dataclass
import math
from pathlib import Path
import time
from typing import Callable, Mapping

from .config import (
    ControlConfig,
    RunMode,
    StabilityConfig,
    TemperatureStabilityMode,
)
from .models import CryostatState, VectorField
from .safety import (
    AxisWriteOrder,
    CONFIRMED_FIELD_TOLERANCE_MAX_T,
    FIELD_SETPOINT_READBACK_TOLERANCE_T,
    FieldTransitionPlan,
    FieldTransitionPolicy,
    MagnetLimits,
    float32_bits_hex,
    float32_field,
    plan_field_transition,
    plan_field_waypoint,
    serialize_float32_field,
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
FieldCommandCallback = Callable[[dict[str, object]], None]


class AttoDryError(RuntimeError):
    pass


class AttoDryDllError(AttoDryError):
    def __init__(self, message: str, *, return_code: int | None = None) -> None:
        super().__init__(message)
        self.return_code = return_code


class AttoDryTimeout(AttoDryError):
    pass


class AttoDryAuthorizationError(AttoDryError):
    pass


@dataclass(frozen=True)
class HeaterPowerState:
    sample_w: float
    vti_w: float


@dataclass(frozen=True, slots=True)
class _FieldCommandRecord:
    """Private correlation data for one durable field-command transcript pair."""

    command_index: int
    command_kind: str
    dll_symbol: str
    callback: FieldCommandCallback
    payload: dict[str, object]


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
        self._next_field_command_index = 0

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

    def set_temperature_and_enable(
        self,
        temperature_k: float,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> CryostatState:
        """Confirm the requested target BEFORE enabling a disabled controller.

        A disabled cryostat can retain an unrelated target (e.g. 300 K while
        the sample is at base temperature). An unacknowledged preload must not
        enable that stale target. Preserve the established post-enable reapply
        for firmware that requires it. The caller owns failure cleanup.
        """
        self._require_write_authorized()
        before = self.read_state()
        self._require_clear_error(before)
        self.set_temperature(temperature_k, monotonic=monotonic, sleeper=sleeper)
        self.ensure_temperature_control(True, monotonic=monotonic, sleeper=sleeper)
        if not before.temperature_control_enabled:
            self.set_temperature(
                temperature_k, force_write=True, monotonic=monotonic, sleeper=sleeper
            )
        confirmed = self.read_state()
        self._require_clear_error(confirmed)
        if not confirmed.temperature_control_enabled or not math.isclose(
            confirmed.user_temperature_k, temperature_k, rel_tol=0.0, abs_tol=1e-4
        ):
            raise AttoDryError("Temperature target/control changed during startup.")
        return confirmed

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
        on_command: FieldCommandCallback | None = None,
        command_context: Mapping[str, object] | None = None,
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
            field_command_callback=on_command,
            field_command_context=command_context,
            field_command_kind=(
                "toggle_field_control" if on_command is not None else None
            ),
        )

    def set_vector_field(
        self,
        target: VectorField,
        *,
        max_step_t: float = 0.05,
        planning_start: VectorField | None = None,
        transition_plan: FieldTransitionPlan | None = None,
        transition_policy: FieldTransitionPolicy = FieldTransitionPolicy.VIA_ZERO,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        on_sample: FieldSampleCallback | None = None,
        on_command: FieldCommandCallback | None = None,
        command_context: Mapping[str, object] | None = None,
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
        try:
            policy = FieldTransitionPolicy(transition_policy)
        except ValueError as exc:
            raise ValueError("transition_policy must be 'direct' or 'via_zero'.") from exc
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
        if transition_plan is None:
            plan = plan_field_transition(
                plan_start,
                checked,
                max_step_t,
                policy,
                self.limits,
            )
        else:
            plan = transition_plan
            if plan.transition_policy is not policy:
                raise AttoDryError(
                    "Supplied transition plan policy does not match the requested "
                    "transition policy."
                )
            if plan.requested_target != checked:
                raise AttoDryError(
                    "Supplied transition plan target does not match the requested field."
                )
            if not self._field_matches(plan.start_command, plan_start):
                raise AttoDryError(
                    "Supplied transition plan start does not match the planning start."
                )
            if not self._field_matches(
                plan.target_command, float32_field(checked)
            ):
                raise AttoDryError(
                    "Supplied transition plan target is not the exact float32 command."
                )

        if self._field_matches(state.field_setpoint, plan.target_command):
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

        # Do not begin a new setpoint path while the actual field is still
        # converging toward the confirmed starting setpoint.
        current_state = wait_at_setpoint(
            plan.start_command,
            phase="planning_start_stability",
            waypoint_index=None,
        )
        for waypoint_index, planned_waypoint in enumerate(plan.waypoints):
            if not self._field_matches(
                current_state.field_setpoint,
                plan.start_command
                if waypoint_index == 0
                else plan.waypoints[waypoint_index - 1].command_field,
            ):
                raise AttoDryError(
                    "Confirmed setpoint no longer matches the prevalidated field "
                    "transition; refusing to infer a new path."
                )
            # Re-evaluate both mixed corners against the just-confirmed setpoint.
            # Readback quantization or an external change must never cause the
            # preplanned axis order to be trusted blindly.
            execution_waypoint = plan_field_waypoint(
                current_state.field_setpoint,
                planned_waypoint.command_field,
                max_step_t,
                self.limits,
            )
            if on_command is not None:
                on_command(
                    {
                        **dict(command_context or {}),
                        "event": "field_waypoint_execution",
                        "waypoint_index": waypoint_index,
                        "planned_requested_field": asdict(
                            planned_waypoint.requested_field
                        ),
                        "confirmed_predecessor": serialize_float32_field(
                            current_state.field_setpoint
                        ),
                        "command_field": serialize_float32_field(
                            execution_waypoint.command_field
                        ),
                        "x_then_z_mixed_corner": serialize_float32_field(
                            execution_waypoint.x_then_z_mixed_corner
                        ),
                        "z_then_x_mixed_corner": serialize_float32_field(
                            execution_waypoint.z_then_x_mixed_corner
                        ),
                        "axis_order": execution_waypoint.axis_order.value,
                    }
                )

            axis_sequence = (
                ("x", "setUserMagneticFieldX", "AttoDRY_Interface_setUserMagneticFieldX")
                if execution_waypoint.axis_order is AxisWriteOrder.X_THEN_Z
                else ("z", "setUserMagneticFieldZ", "AttoDRY_Interface_setUserMagneticFieldZ"),
                ("z", "setUserMagneticFieldZ", "AttoDRY_Interface_setUserMagneticFieldZ")
                if execution_waypoint.axis_order is AxisWriteOrder.X_THEN_Z
                else ("x", "setUserMagneticFieldX", "AttoDRY_Interface_setUserMagneticFieldX"),
            )
            for axis_order_index, (axis, label, symbol) in enumerate(axis_sequence):
                previous_setpoint = current_state.field_setpoint
                command_value = (
                    execution_waypoint.command_field.bx_t
                    if axis == "x"
                    else execution_waypoint.command_field.bz_t
                )
                requested_value = (
                    planned_waypoint.requested_field.bx_t
                    if axis == "x"
                    else planned_waypoint.requested_field.bz_t
                )
                expected_setpoint = (
                    VectorField(command_value, previous_setpoint.bz_t)
                    if axis == "x"
                    else VectorField(previous_setpoint.bx_t, command_value)
                )
                validate_vector_field(expected_setpoint, self.limits)
                if (
                    math.hypot(
                        expected_setpoint.bx_t - previous_setpoint.bx_t,
                        expected_setpoint.bz_t - previous_setpoint.bz_t,
                    )
                    > max_step_t + FIELD_SETPOINT_READBACK_TOLERANCE_T
                ):
                    raise AttoDryError(
                        "Confirmed component setpoint fell behind the prevalidated "
                        "path; refusing a step larger than max_step_t."
                    )
                command_payload = {
                    **dict(command_context or {}),
                    "waypoint_index": waypoint_index,
                    "axis_order": execution_waypoint.axis_order.value,
                    "axis_order_index": axis_order_index,
                    "axis": axis,
                    "requested_value_t": requested_value,
                    "float32_value_t": command_value,
                    "float32_ieee754_bits_hex": float32_bits_hex(command_value),
                    "previous_setpoint": serialize_float32_field(previous_setpoint),
                    "expected_setpoint": serialize_float32_field(expected_setpoint),
                }
                current_value = (
                    previous_setpoint.bx_t if axis == "x" else previous_setpoint.bz_t
                )
                if math.isclose(
                    current_value,
                    command_value,
                    rel_tol=0.0,
                    abs_tol=FIELD_SETPOINT_READBACK_TOLERANCE_T,
                ):
                    if on_command is not None:
                        on_command(
                            {
                                **command_payload,
                                "event": "field_component_skipped",
                                "reason": "already_within_setpoint_ack_tolerance",
                            }
                        )
                    if on_sample is not None:
                        on_sample(
                            current_state,
                            0.0,
                            f"setpoint_{axis}_unchanged",
                            waypoint_index,
                        )
                    continue
                # A setpoint acknowledgement is not proof that the other coil
                # has ramped down. Check the new component against its latest
                # actual readback as well, before allowing a high-field axis switch.
                validate_vector_field(
                    VectorField(command_value, current_state.field.bz_t)
                    if axis == "x"
                    else VectorField(current_state.field.bx_t, command_value),
                    self.limits,
                )
                command = self._begin_field_command(
                    command_kind="set_field_component",
                    dll_symbol=symbol,
                    callback=on_command,
                    payload=command_payload,
                )
                try:
                    return_code = self._call(
                        label,
                        symbol,
                        ctypes.c_float(command_value),
                    )
                except BaseException as exc:
                    self._record_field_command_failure(
                        command,
                        exc,
                        acknowledgement="not_attempted",
                    )
                    raise
                try:
                    current_state = self._wait_for_field_setpoint(
                        expected_setpoint,
                        waypoint_index=waypoint_index,
                        phase=f"setpoint_{axis}_ack",
                        monotonic=monotonic,
                        sleeper=sleeper,
                        on_sample=on_sample,
                    )
                except BaseException as exc:
                    self._record_field_command_failure(
                        command,
                        exc,
                        acknowledgement="failed",
                        dll_return_code=return_code,
                    )
                    raise
                self._record_field_command_result(
                    command,
                    success=True,
                    dll_return_code=return_code,
                    acknowledgement="confirmed",
                    state=current_state,
                )

            # Intermediate waypoints must be reached and stable before another
            # component write.  The executor owns the final explicit-target
            # dwell so point completion has exactly one stability window.
            if waypoint_index < len(plan.waypoints) - 1:
                current_state = wait_at_setpoint(
                    execution_waypoint.command_field,
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

        if not self._field_matches(current_state.field_setpoint, plan.target_command):
            raise AttoDryError("Vector-field setpoint readback does not match target.")
        return current_state

    def wait_for_temperature(
        self,
        target_k: float,
        *,
        max_overshoot_k: float | None = None,
        minimum_response_k: float = 0.0,
        response_reference_k: float | None = None,
        maximum_accepted_k: float | None = None,
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
        if maximum_accepted_k is not None and (
            not math.isfinite(maximum_accepted_k)
            or not self.temperature_min_k < maximum_accepted_k <= self.temperature_max_k
        ):
            raise ValueError("maximum_accepted_k must be within configured temperature limits.")
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
            # A descending combined inner-axis reset may start above the new
            # target's overshoot band. Monitor that cooldown under a bounded
            # transient ceiling, but never accept its still-hot stable plateau.
            qualify=lambda state: response_seen and (maximum_accepted_k is None or
                state.sample_temperature_k < maximum_accepted_k),
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
        on_command: FieldCommandCallback | None = None,
        command_context: Mapping[str, object] | None = None,
    ) -> CryostatState:
        self._require_write_authorized()
        state = self.read_state()
        if on_sample is not None:
            on_sample(state, 0.0, "zero_preflight", None)
        self._validate_field_state(state, require_control=True)
        command = self._begin_field_command(
            command_kind="sweep_field_to_zero",
            dll_symbol="AttoDRY_Interface_sweepFieldToZero",
            callback=on_command,
            payload=dict(command_context or {}),
        )
        try:
            return_code = self._call(
                "sweepFieldToZero", "AttoDRY_Interface_sweepFieldToZero"
            )
        except BaseException as exc:
            self._record_field_command_failure(
                command,
                exc,
                acknowledgement="not_attempted",
            )
            raise
        zero = VectorField(0.0, 0.0)
        try:
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
        except BaseException as exc:
            self._record_field_command_failure(
                command,
                exc,
                acknowledgement="failed",
                dll_return_code=return_code,
            )
            raise
        self._record_field_command_result(
            command,
            success=True,
            dll_return_code=return_code,
            acknowledgement="confirmed",
            state=confirmed,
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
        field_command_callback: FieldCommandCallback | None = None,
        field_command_context: Mapping[str, object] | None = None,
        field_command_kind: str | None = None,
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
        if (field_command_callback is None) != (field_command_kind is None):
            raise ValueError(
                "field-command callback and kind must be provided together."
            )
        command = self._begin_field_command(
            command_kind=field_command_kind,
            dll_symbol=toggle_symbol,
            callback=field_command_callback,
            payload={
                **dict(field_command_context or {}),
                "requested_enabled": enabled,
                "pre_state": asdict(state),
            },
        )
        try:
            return_code = self._call(toggle_label, toggle_symbol)
        except BaseException as exc:
            self._record_field_command_failure(
                command,
                exc,
                acknowledgement="not_attempted",
            )
            raise
        try:
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
                        break
                    raise AttoDryError(
                        f"{state_attribute} readback did not reach {enabled}."
                    )
                if elapsed >= acknowledgment_timeout_s:
                    raise AttoDryTimeout(
                        f"{state_attribute} readback did not reach {enabled} within "
                        f"{acknowledgment_timeout_s:g} s."
                    )
                if reached:
                    break
                sleeper(
                    min(
                        acknowledgment_poll_interval_s,
                        acknowledgment_timeout_s - elapsed,
                    )
                )
        except BaseException as exc:
            self._record_field_command_failure(
                command,
                exc,
                acknowledgement="failed",
                dll_return_code=return_code,
            )
            raise
        self._record_field_command_result(
            command,
            success=True,
            dll_return_code=return_code,
            acknowledgement="confirmed",
            state=confirmed,
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
        limits = self._field_readback_limits()
        validate_vector_field(state.field, limits)
        validate_vector_field(state.field_setpoint, limits)
        self._require_clear_error(state)
        if require_control and not state.field_control_enabled:
            raise AttoDryError("Field control is not confirmed enabled.")

    def _field_readback_limits(self) -> MagnetLimits:
        """Readback policy; requested targets always use the configured limits."""
        return self.limits

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

    def _begin_field_command(
        self,
        *,
        command_kind: str | None,
        dll_symbol: str,
        callback: FieldCommandCallback | None,
        payload: Mapping[str, object],
    ) -> _FieldCommandRecord | None:
        if callback is None:
            return None
        if command_kind is None:
            raise ValueError("A field-command audit callback requires a command kind.")
        index = self._next_field_command_index
        record = _FieldCommandRecord(
            command_index=index,
            command_kind=command_kind,
            dll_symbol=dll_symbol,
            callback=callback,
            payload=dict(payload),
        )
        callback(
            {
                **record.payload,
                "event": "field_command_attempt",
                "command_index": record.command_index,
                "command_kind": record.command_kind,
                "dll_symbol": record.dll_symbol,
            }
        )
        self._next_field_command_index += 1
        return record

    @staticmethod
    def _field_command_error_payload(exc: BaseException) -> dict[str, object]:
        payload: dict[str, object] = {
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        if isinstance(exc, AttoDryDllError) and exc.return_code is not None:
            payload["dll_return_code"] = exc.return_code
        return payload

    def _record_field_command_result(
        self,
        command: _FieldCommandRecord | None,
        *,
        success: bool,
        dll_return_code: int | None,
        acknowledgement: str,
        state: CryostatState | None = None,
        error: BaseException | None = None,
    ) -> None:
        if command is None:
            return
        event: dict[str, object] = {
            **command.payload,
            "event": "field_command_result",
            "command_index": command.command_index,
            "command_kind": command.command_kind,
            "dll_symbol": command.dll_symbol,
            "success": success,
            "dll_return_code": dll_return_code,
            "acknowledgement": acknowledgement,
        }
        if state is not None:
            event["state"] = asdict(state)
        if error is not None:
            event.update(self._field_command_error_payload(error))
        command.callback(event)

    def _record_field_command_failure(
        self,
        command: _FieldCommandRecord | None,
        exc: BaseException,
        *,
        acknowledgement: str,
        dll_return_code: int | None = None,
    ) -> None:
        if command is None:
            return
        if dll_return_code is None and isinstance(exc, AttoDryDllError):
            dll_return_code = exc.return_code
        try:
            self._record_field_command_result(
                command,
                success=False,
                dll_return_code=dll_return_code,
                acknowledgement=acknowledgement,
                error=exc,
            )
        except BaseException as audit_error:
            if audit_error is not exc:
                exc.add_note(
                    "Could not append field-command failure evidence: "
                    f"{audit_error}"
                )

    def _call(self, label: str, symbol: str, *args: object) -> int:
        code = int(getattr(self.dll, symbol)(*args))
        if code != 0:
            raise AttoDryDllError(
                f"{label} returned DLL error code {code}.", return_code=code
            )
        return code

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
