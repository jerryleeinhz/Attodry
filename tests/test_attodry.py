from collections import deque
import ctypes
from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
import math
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

from attodry_control.attodry import (
    AttoDryAuthorizationError,
    AttoDryDriver,
    AttoDryDllError,
    AttoDryError,
    AttoDryTimeout,
)
from attodry_control.attodry_test import run as run_attodry_test
from attodry_control.config import load_config
from attodry_control.models import VectorField
from attodry_control.safety import (
    FieldTransitionPolicy,
    SafetyViolation,
    float32_bits_hex,
    float32_field,
    validate_vector_field,
)
from attodry_control.temperature_test import run as run_temperature_test
from attodry_control.temperature_run import run as run_temperature_operation
from attodry_control.temperature_scan import run as run_temperature_scan


class FakeAttoDryDll:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.return_codes: dict[str, int] = {}
        self.initialized = deque([1])
        self.sample_temperature_k = 2.0
        self.sample_temperatures_k: deque[float] = deque()
        self.user_temperature_k = 2.0
        self.vti_temperature_k = 2.1
        self.sample_heater_power_w = 0.25
        self.vti_heater_power_w = 0.5
        self.bx_t = 0.0
        self.bz_t = 0.0
        self.setpoint_x_t = 0.0
        self.setpoint_z_t = 0.0
        self.temperature_control = 0
        self.temperature_follows_setpoint = False
        self.field_control = 0
        self.error_code = 0
        self.connected = False

    def _code(self, name: str) -> int:
        self.events.append(name)
        return self.return_codes.get(name, 0)

    @staticmethod
    def _set(pointer, value) -> None:
        pointer._obj.value = value

    def AttoDRY_Interface_begin(self, device_type) -> int:
        self.device_type = device_type.value
        return self._code("begin")

    def AttoDRY_Interface_Connect(self, com_port) -> int:
        self.com_port = com_port.value.decode()
        code = self._code("connect")
        self.connected = code == 0
        return code

    def AttoDRY_Interface_Disconnect(self) -> int:
        self.connected = False
        return self._code("disconnect")

    def AttoDRY_Interface_end(self) -> int:
        return self._code("end")

    def AttoDRY_Interface_isDeviceInitialised(self, pointer) -> int:
        value = self.initialized[0]
        if len(self.initialized) > 1:
            self.initialized.popleft()
        self._set(pointer, value)
        return self._code("is_initialized")

    def _float_getter(self, name: str, pointer, value: float) -> int:
        self._set(pointer, value)
        return self._code(name)

    def _int_getter(self, name: str, pointer, value: int) -> int:
        self._set(pointer, value)
        return self._code(name)

    def AttoDRY_Interface_getSampleTemperature(self, pointer):
        if self.sample_temperatures_k:
            self.sample_temperature_k = self.sample_temperatures_k.popleft()
        return self._float_getter("get_sample_temperature", pointer, self.sample_temperature_k)

    def AttoDRY_Interface_getUserTemperature(self, pointer):
        return self._float_getter("get_user_temperature", pointer, self.user_temperature_k)

    def AttoDRY_Interface_getVtiTemperature(self, pointer):
        return self._float_getter("get_vti_temperature", pointer, self.vti_temperature_k)

    def AttoDRY_Interface_getSampleHeaterPower(self, pointer):
        return self._float_getter(
            "get_sample_heater_power", pointer, self.sample_heater_power_w
        )

    def AttoDRY_Interface_getVtiHeaterPower(self, pointer):
        return self._float_getter(
            "get_vti_heater_power", pointer, self.vti_heater_power_w
        )

    def AttoDRY_Interface_getMagneticFieldX(self, pointer):
        return self._float_getter("get_field_x", pointer, self.bx_t)

    def AttoDRY_Interface_getMagneticFieldZ(self, pointer):
        return self._float_getter("get_field_z", pointer, self.bz_t)

    def AttoDRY_Interface_getMagneticFieldSetPointX(self, pointer):
        return self._float_getter("get_setpoint_x", pointer, self.setpoint_x_t)

    def AttoDRY_Interface_getMagneticFieldSetPointZ(self, pointer):
        return self._float_getter("get_setpoint_z", pointer, self.setpoint_z_t)

    def AttoDRY_Interface_isControllingTemperature(self, pointer):
        return self._int_getter("is_temperature_control", pointer, self.temperature_control)

    def AttoDRY_Interface_isControllingField(self, pointer):
        return self._int_getter("is_field_control", pointer, self.field_control)

    def AttoDRY_Interface_getAttodryErrorStatus(self, pointer):
        return self._int_getter("get_error", pointer, self.error_code)

    def AttoDRY_Interface_setUserTemperature(self, value):
        self.user_temperature_k = value.value
        if self.temperature_follows_setpoint:
            self.sample_temperature_k = value.value
            self.vti_temperature_k = value.value
        return self._code("set_temperature")

    def AttoDRY_Interface_setUserMagneticFieldX(self, value):
        self.setpoint_x_t = value.value
        self.bx_t = value.value
        self._assert_safe_intermediate()
        return self._code("set_field_x")

    def AttoDRY_Interface_setUserMagneticFieldZ(self, value):
        self.setpoint_z_t = value.value
        self.bz_t = value.value
        self._assert_safe_intermediate()
        return self._code("set_field_z")

    def _assert_safe_intermediate(self) -> None:
        if math.hypot(self.setpoint_x_t, self.setpoint_z_t) > 3.000001:
            raise AssertionError("unsafe intermediate vector sent to fake DLL")

    def AttoDRY_Interface_toggleFullTemperatureControl(self):
        self.temperature_control = 1 - self.temperature_control
        return self._code("toggle_temperature_control")

    def AttoDRY_Interface_toggleMagneticFieldControl(self):
        self.field_control = 1 - self.field_control
        return self._code("toggle_field_control")

    def AttoDRY_Interface_sweepFieldToZero(self):
        self.setpoint_x_t = 0.0
        self.setpoint_z_t = 0.0
        self.bx_t = 0.0
        self.bz_t = 0.0
        return self._code("sweep_zero")


class AttoDryDriverTests(unittest.TestCase):
    def setUp(self) -> None:
        config = load_config("config/hardware.example.toml")
        self.dll = FakeAttoDryDll()
        self.driver = AttoDryDriver.from_config(
            config,
            dll=self.dll,
            connection_authorized=True,
            writes_authorized=True,
        )

    def connect(self) -> None:
        self.driver.connect(
            monotonic=iter([0.0, 0.1, 0.2, 0.3]).__next__,
            sleeper=lambda _: None,
        )

    def test_connect_begins_connects_and_waits_for_initialization(self) -> None:
        self.dll.initialized = deque([0, 0, 1])

        self.connect()

        self.assertEqual(self.dll.events[:2], ["begin", "connect"])
        self.assertEqual(self.dll.events.count("is_initialized"), 3)
        self.assertEqual(self.dll.com_port, "CHANGE_ME_COM_PORT")

    def test_connection_requires_explicit_authorization(self) -> None:
        config = load_config("config/hardware.example.toml")
        driver = AttoDryDriver.from_config(
            config,
            dll=self.dll,
            connection_authorized=False,
            writes_authorized=False,
        )

        with self.assertRaises(AttoDryAuthorizationError):
            driver.connect(monotonic=lambda: 0.0, sleeper=lambda _: None)

        self.assertEqual(self.dll.events, [])

    def test_initialization_timeout_disconnects_and_fails(self) -> None:
        self.dll.initialized = deque([0])
        times = iter([0.0, 10.0, 70.0])

        with self.assertRaises(AttoDryTimeout):
            self.driver.connect(monotonic=times.__next__, sleeper=lambda _: None)

        self.assertIn("disconnect", self.dll.events)
        self.assertEqual(self.dll.events[-1], "end")

    def test_invalid_initialization_status_disconnects_and_fails(self) -> None:
        self.dll.initialized = deque([2])

        with self.assertRaisesRegex(AttoDryError, "invalid control state 2"):
            self.connect()

        self.assertIn("disconnect", self.dll.events)
        self.assertEqual(self.dll.events[-1], "end")

    def test_connect_failure_ends_begun_interface(self) -> None:
        self.dll.return_codes["connect"] = 5

        with self.assertRaisesRegex(AttoDryDllError, "Connect.*5"):
            self.connect()

        self.assertEqual(self.dll.events, ["begin", "connect", "end"])

    def test_every_nonzero_dll_return_is_an_error(self) -> None:
        self.connect()
        self.dll.return_codes["get_field_x"] = 7

        with self.assertRaisesRegex(AttoDryDllError, "getMagneticFieldX.*7"):
            self.driver.read_state()

    def test_control_enable_is_read_then_toggle_and_idempotent(self) -> None:
        self.connect()

        self.driver.ensure_field_control(True)
        self.driver.ensure_field_control(True)

        self.assertEqual(self.dll.events.count("toggle_field_control"), 1)

    def test_field_control_waits_for_delayed_acknowledgement(self) -> None:
        self.connect()
        control_readbacks = iter([0, 0, 1])

        def delayed_getter(pointer):
            return self.dll._int_getter(
                "is_field_control", pointer, next(control_readbacks)
            )

        def delayed_toggle():
            return self.dll._code("toggle_field_control")

        self.dll.AttoDRY_Interface_isControllingField = delayed_getter
        self.dll.AttoDRY_Interface_toggleMagneticFieldControl = delayed_toggle
        sleeps: list[float] = []
        samples: list[tuple[bool, float, str, int | None]] = []

        self.driver.ensure_field_control(
            True,
            monotonic=iter([0.0, 0.0, 1.0]).__next__,
            sleeper=sleeps.append,
            on_sample=lambda state, elapsed, phase, waypoint_index: samples.append(
                (state.field_control_enabled, elapsed, phase, waypoint_index)
            ),
        )

        self.assertEqual(sleeps, [1.0])
        self.assertEqual([sample[0] for sample in samples], [False, False, True])
        self.assertEqual([sample[1] for sample in samples], [0.0, 0.0, 1.0])
        self.assertTrue(all(sample[2] == "field_control_ack" for sample in samples))
        self.assertTrue(all(sample[3] is None for sample in samples))
        self.assertEqual(self.dll.events.count("toggle_field_control"), 1)

    def test_field_control_acknowledgement_timeout_fails_closed(self) -> None:
        self.connect()

        def ignored_toggle():
            return self.dll._code("toggle_field_control")

        self.dll.AttoDRY_Interface_toggleMagneticFieldControl = ignored_toggle

        with self.assertRaisesRegex(AttoDryTimeout, "readback did not reach True"):
            self.driver.ensure_field_control(
                True,
                monotonic=iter([0.0, 30.0]).__next__,
                sleeper=lambda _: None,
            )

        self.assertFalse(self.driver.last_confirmed_state.field_control_enabled)
        self.assertEqual(self.dll.events.count("toggle_field_control"), 1)

    def test_field_control_first_post_toggle_read_uses_real_elapsed_timeout(
        self,
    ) -> None:
        self.connect()
        samples = []

        with self.assertRaisesRegex(AttoDryTimeout, "readback did not reach True"):
            self.driver.ensure_field_control(
                True,
                monotonic=iter([0.0, 30.0]).__next__,
                sleeper=lambda _: None,
                on_sample=lambda state, elapsed, phase, waypoint_index: samples.append(
                    (state.field_control_enabled, elapsed, phase, waypoint_index)
                ),
            )

        self.assertEqual([sample[1] for sample in samples], [0.0, 30.0])
        self.assertTrue(self.driver.last_confirmed_state.field_control_enabled)
        self.assertEqual(self.dll.events.count("toggle_field_control"), 1)

    def test_field_control_acknowledgement_rejects_device_error(self) -> None:
        self.connect()
        error_readbacks = iter([0, 7])

        def sequenced_error(pointer):
            return self.dll._int_getter("get_error", pointer, next(error_readbacks))

        self.dll.AttoDRY_Interface_getAttodryErrorStatus = sequenced_error
        samples = []

        with self.assertRaisesRegex(AttoDryError, "error code 7"):
            self.driver.ensure_field_control(
                True,
                on_sample=lambda state, elapsed, phase, waypoint_index: samples.append(
                    (state, elapsed, phase, waypoint_index)
                ),
            )

        self.assertEqual(self.dll.events.count("toggle_field_control"), 1)
        self.assertEqual([sample[0].error_code for sample in samples], [0, 7])
        self.assertEqual(self.driver.last_confirmed_state.error_code, 7)

    def test_field_control_takeover_rejects_mismatched_latent_setpoint(self) -> None:
        self.connect()
        self.dll.bx_t = 3.0
        self.dll.bz_t = 0.0
        self.dll.setpoint_x_t = 0.0
        self.dll.setpoint_z_t = 3.0

        with self.assertRaisesRegex(AttoDryError, "latent setpoint"):
            self.driver.ensure_field_control(True)

        self.assertEqual(self.dll.events.count("toggle_field_control"), 0)
        self.assertEqual(self.dll.events.count("set_field_x"), 0)
        self.assertEqual(self.dll.events.count("set_field_z"), 0)

    def test_field_control_takeover_rejects_unsafe_mixed_corner(self) -> None:
        self.connect()
        self.dll.bx_t = 3.0
        self.dll.bz_t = 0.0
        self.dll.setpoint_x_t = 2.9999995
        self.dll.setpoint_z_t = 0.0009

        with self.assertRaises(SafetyViolation):
            self.driver.ensure_field_control(True)

        self.assertEqual(self.dll.events.count("toggle_field_control"), 0)

    def test_temperature_control_is_read_then_toggle_and_idempotent(self) -> None:
        self.connect()

        self.driver.ensure_temperature_control(True)
        self.driver.ensure_temperature_control(True)

        self.assertEqual(self.dll.events.count("toggle_temperature_control"), 1)

    def test_temperature_control_waits_for_delayed_toggle_readback(self) -> None:
        self.connect()
        control_readbacks = iter([0, 0, 1])

        def delayed_getter(pointer):
            return self.dll._int_getter(
                "is_temperature_control", pointer, next(control_readbacks)
            )

        def delayed_toggle():
            return self.dll._code("toggle_temperature_control")

        self.dll.AttoDRY_Interface_isControllingTemperature = delayed_getter
        self.dll.AttoDRY_Interface_toggleFullTemperatureControl = delayed_toggle
        sleeps = []

        self.driver.ensure_temperature_control(
            True,
            monotonic=iter([0.0, 0.0, 1.5]).__next__,
            sleeper=sleeps.append,
        )

        self.assertEqual(sleeps, [1.5])
        self.assertTrue(self.driver.last_confirmed_state.temperature_control_enabled)

    def test_temperature_control_toggle_readback_timeout_fails_closed(self) -> None:
        self.connect()

        def ignored_toggle():
            return self.dll._code("toggle_temperature_control")

        self.dll.AttoDRY_Interface_toggleFullTemperatureControl = ignored_toggle

        with self.assertRaisesRegex(AttoDryTimeout, "readback did not reach True"):
            self.driver.ensure_temperature_control(
                True,
                monotonic=iter([0.0, 30.0]).__next__,
                sleeper=lambda _: None,
            )

        self.assertFalse(self.driver.last_confirmed_state.temperature_control_enabled)
        self.assertEqual(self.dll.events.count("toggle_temperature_control"), 1)

    def test_invalid_temperature_control_state_blocks_toggle(self) -> None:
        self.connect()
        self.dll.temperature_control = 2

        with self.assertRaisesRegex(AttoDryError, "invalid control state 2"):
            self.driver.ensure_temperature_control(True)

        self.assertNotIn("toggle_temperature_control", self.dll.events)

    def test_vector_limit_is_rejected_before_any_field_write(self) -> None:
        self.connect()
        before = list(self.dll.events)

        with self.assertRaises(SafetyViolation):
            self.driver.set_vector_field(VectorField(2.0, 2.5))

        self.assertNotIn("set_field_x", self.dll.events[len(before) :])
        self.assertNotIn("set_field_z", self.dll.events[len(before) :])

    def test_vector_rotation_uses_safe_zero_detour(self) -> None:
        self.connect()
        self.dll.bx_t = self.dll.setpoint_x_t = 3.0
        self.dll.bz_t = self.dll.setpoint_z_t = 0.0
        self.dll.field_control = 1

        self.driver.set_vector_field(
            VectorField(0.0, 3.0),
            max_step_t=0.5,
            monotonic=StepClock(step_s=5.0),
            sleeper=lambda _: None,
        )

        self.assertAlmostEqual(self.dll.setpoint_x_t, 0.0)
        self.assertAlmostEqual(self.dll.setpoint_z_t, 3.0)
        self.assertGreater(self.dll.events.count("set_field_x"), 2)

    def test_direct_rotation_uses_the_only_safe_component_order_at_three_tesla(
        self,
    ) -> None:
        self.connect()
        self.dll.bx_t = self.dll.setpoint_x_t = 3.0
        self.dll.bz_t = self.dll.setpoint_z_t = 0.0
        self.dll.field_control = 1

        self.driver.set_vector_field(
            VectorField(0.0, 3.0),
            max_step_t=5.0,
            transition_policy=FieldTransitionPolicy.DIRECT,
            monotonic=StepClock(step_s=5.0),
            sleeper=lambda _: None,
        )
        self.driver.set_vector_field(
            VectorField(3.0, 0.0),
            max_step_t=5.0,
            transition_policy=FieldTransitionPolicy.DIRECT,
            monotonic=StepClock(step_s=5.0),
            sleeper=lambda _: None,
        )

        writes = [
            event
            for event in self.dll.events
            if event in {"set_field_x", "set_field_z"}
        ]
        self.assertEqual(writes, ["set_field_x", "set_field_z", "set_field_z", "set_field_x"])
        self.assertNotIn("sweep_zero", self.dll.events)

    def test_field_command_audit_records_attempt_result_pairs_and_float32_bits(
        self,
    ) -> None:
        self.connect()
        events: list[dict[str, object]] = []

        self.driver.ensure_field_control(True, on_command=events.append)
        self.driver.set_vector_field(
            VectorField(0.1, 0.0),
            max_step_t=0.1,
            transition_policy=FieldTransitionPolicy.DIRECT,
            on_command=events.append,
            monotonic=StepClock(step_s=5.0),
            sleeper=lambda _: None,
        )
        self.driver.request_zero_field(
            on_command=events.append,
            monotonic=StepClock(step_s=5.0),
            sleeper=lambda _: None,
        )

        attempts = [event for event in events if event["event"] == "field_command_attempt"]
        results = [event for event in events if event["event"] == "field_command_result"]
        self.assertEqual([event["command_index"] for event in attempts], list(range(len(attempts))))
        self.assertEqual(
            [event["command_index"] for event in results],
            [event["command_index"] for event in attempts],
        )
        self.assertTrue(all(event["success"] is True for event in results))
        self.assertTrue(all(event["acknowledgement"] == "confirmed" for event in results))
        component = next(
            event
            for event in attempts
            if event["command_kind"] == "set_field_component" and event["axis"] == "x"
        )
        self.assertEqual(component["float32_value_t"], float32_field(VectorField(0.1, 0.0)).bx_t)
        self.assertEqual(
            component["float32_ieee754_bits_hex"],
            float32_bits_hex(float(component["float32_value_t"])),
        )
        self.assertIn("toggle_field_control", [event["command_kind"] for event in attempts])
        self.assertIn("sweep_field_to_zero", [event["command_kind"] for event in attempts])

    def test_field_command_attempt_audit_failure_prevents_the_component_write(self) -> None:
        self.connect()
        self.dll.field_control = 1

        def fail_attempt(event: dict[str, object]) -> None:
            if event["event"] == "field_command_attempt":
                raise OSError("injected command-audit failure")

        with self.assertRaisesRegex(OSError, "command-audit"):
            self.driver.set_vector_field(
                VectorField(0.1, 0.0),
                max_step_t=0.1,
                transition_policy=FieldTransitionPolicy.DIRECT,
                on_command=fail_attempt,
                monotonic=StepClock(step_s=5.0),
                sleeper=lambda _: None,
            )

        self.assertNotIn("set_field_x", self.dll.events)
        self.assertNotIn("set_field_z", self.dll.events)

    def test_field_command_audit_records_a_dll_failure_result(self) -> None:
        self.connect()
        self.dll.field_control = 1
        self.dll.return_codes["set_field_x"] = 7
        events: list[dict[str, object]] = []

        with self.assertRaises(AttoDryDllError):
            self.driver.set_vector_field(
                VectorField(0.1, 0.0),
                max_step_t=0.1,
                transition_policy=FieldTransitionPolicy.DIRECT,
                on_command=events.append,
                monotonic=StepClock(step_s=5.0),
                sleeper=lambda _: None,
            )

        component_attempt = next(
            event
            for event in events
            if event["event"] == "field_command_attempt"
            and event["command_kind"] == "set_field_component"
        )
        component_result = next(
            event
            for event in events
            if event["event"] == "field_command_result"
            and event["command_index"] == component_attempt["command_index"]
        )
        self.assertFalse(component_result["success"])
        self.assertEqual(component_result["acknowledgement"], "not_attempted")
        self.assertEqual(component_result["dll_return_code"], 7)

    def test_vector_plan_uses_the_other_verified_order_when_one_corner_is_unsafe(
        self,
    ) -> None:
        self.connect()
        self.dll.field_control = 1
        mixed_transient = VectorField(0.3, 0.2)
        observed: list[tuple[VectorField, int]] = []

        def reject_mixed_transient(target, limits):
            writes = self.dll.events.count("set_field_x") + self.dll.events.count(
                "set_field_z"
            )
            observed.append((target, writes))
            if target == float32_field(mixed_transient):
                raise SafetyViolation("injected mixed X/Z transient rejection")
            return validate_vector_field(target, limits)

        with (
            patch(
                "attodry_control.safety.validate_vector_field",
                side_effect=reject_mixed_transient,
            ),
        ):
            self.driver.set_vector_field(
                VectorField(0.3, 0.4),
                max_step_t=0.25,
            )

        self.assertIn((float32_field(mixed_transient), 0), observed)
        self.assertEqual(
            [
                event
                for event in self.dll.events
                if event in {"set_field_x", "set_field_z"}
            ],
            ["set_field_x", "set_field_z", "set_field_z", "set_field_x"],
        )

    def test_vector_plan_rejects_float32_rounding_outside_three_tesla(self) -> None:
        self.connect()
        self.dll.field_control = 1
        target = VectorField(2.2012714153011306, 2.038235549728774)
        self.assertLessEqual(target.magnitude_t, 3.0)

        with self.assertRaises(SafetyViolation):
            self.driver.set_vector_field(target, max_step_t=3.0)

        self.assertEqual(self.dll.events.count("set_field_x"), 0)
        self.assertEqual(self.dll.events.count("set_field_z"), 0)

    def test_vector_field_acknowledges_each_component_and_intermediate_waypoint(
        self,
    ) -> None:
        self.connect()
        self.dll.field_control = 1
        pending: dict[str, float | None] = {"x": None, "z": None}
        reads_since_write = {"x": 0, "z": 0}
        written_axes: list[str] = []

        def write_axis(axis: str, event: str, value) -> int:
            self.assertIsNone(pending["x"])
            self.assertIsNone(pending["z"])
            pending[axis] = value.value
            reads_since_write[axis] = 0
            written_axes.append(axis)
            return self.dll._code(event)

        def read_setpoint(axis: str, event: str, pointer) -> int:
            if pending[axis] is not None:
                reads_since_write[axis] += 1
                if reads_since_write[axis] >= 2:
                    value = pending[axis]
                    setattr(self.dll, f"setpoint_{axis}_t", value)
                    setattr(self.dll, f"b{axis}_t", value)
                    pending[axis] = None
            return self.dll._float_getter(
                event,
                pointer,
                getattr(self.dll, f"setpoint_{axis}_t"),
            )

        self.dll.AttoDRY_Interface_setUserMagneticFieldX = lambda value: write_axis(
            "x", "set_field_x", value
        )
        self.dll.AttoDRY_Interface_setUserMagneticFieldZ = lambda value: write_axis(
            "z", "set_field_z", value
        )
        self.dll.AttoDRY_Interface_getMagneticFieldSetPointX = (
            lambda pointer: read_setpoint("x", "get_setpoint_x", pointer)
        )
        self.dll.AttoDRY_Interface_getMagneticFieldSetPointZ = (
            lambda pointer: read_setpoint("z", "get_setpoint_z", pointer)
        )
        sleeps: list[float] = []
        samples = []

        state = self.driver.set_vector_field(
            VectorField(0.2, 0.1),
            max_step_t=0.1,
            monotonic=StepClock(step_s=5.0),
            sleeper=sleeps.append,
            on_sample=lambda state, elapsed, phase, waypoint_index: samples.append(
                (state, elapsed, phase, waypoint_index)
            ),
        )

        self.assertEqual(written_axes, ["z", "x"] * 3)
        self.assertEqual(len(sleeps), 12)
        self.assertEqual(
            [sample[3] for sample in samples if sample[2] == "waypoint_confirmed"],
            [0, 1],
        )
        self.assertAlmostEqual(state.field_setpoint.bx_t, 0.2, delta=1e-5)
        self.assertAlmostEqual(state.field_setpoint.bz_t, 0.1, delta=1e-5)

    def test_vector_field_waits_for_actual_waypoint_before_next_write(self) -> None:
        self.connect()
        self.dll.field_control = 1
        pending_actual: list[float] = []
        reads_since_write = 0
        writes: list[float] = []

        def delayed_actual_write(value) -> int:
            nonlocal reads_since_write
            if writes:
                self.assertAlmostEqual(self.dll.bx_t, self.dll.setpoint_x_t)
            self.dll.setpoint_x_t = value.value
            pending_actual[:] = [value.value]
            reads_since_write = 0
            writes.append(value.value)
            return self.dll._code("set_field_x")

        def delayed_actual_read(pointer) -> int:
            nonlocal reads_since_write
            if pending_actual:
                reads_since_write += 1
                if reads_since_write >= 4:
                    self.dll.bx_t = pending_actual.pop()
            return self.dll._float_getter("get_field_x", pointer, self.dll.bx_t)

        self.dll.AttoDRY_Interface_setUserMagneticFieldX = delayed_actual_write
        self.dll.AttoDRY_Interface_getMagneticFieldX = delayed_actual_read

        state = self.driver.set_vector_field(
            VectorField(0.2, 0.0),
            max_step_t=0.1,
            monotonic=StepClock(step_s=5.0),
            sleeper=lambda _: None,
        )

        self.assertEqual(len(writes), 2)
        self.assertAlmostEqual(state.field.bx_t, 0.1, delta=1e-5)
        self.assertAlmostEqual(state.field_setpoint.bx_t, 0.2, delta=1e-5)

        stable = self.driver.wait_for_field(
            VectorField(0.2, 0.0),
            monotonic=StepClock(step_s=5.0),
            sleeper=lambda _: None,
        )
        self.assertAlmostEqual(stable.field.bx_t, 0.2, delta=1e-5)

    def test_vector_field_rejects_step_below_setpoint_ack_resolution(self) -> None:
        self.connect()
        self.dll.field_control = 1

        with self.assertRaisesRegex(ValueError, "acknowledgement tolerance"):
            self.driver.set_vector_field(
                VectorField(0.1, 0.0),
                max_step_t=5e-6,
            )

        self.assertEqual(self.dll.events.count("set_field_x"), 0)
        self.assertEqual(self.dll.events.count("set_field_z"), 0)

    def test_vector_field_waits_for_actual_start_before_first_write(self) -> None:
        self.connect()
        self.dll.field_control = 1
        self.dll.bx_t = 3.0
        reads = 0

        def converging_x(pointer) -> int:
            nonlocal reads
            reads += 1
            if reads >= 4:
                self.dll.bx_t = 0.0
            return self.dll._float_getter("get_field_x", pointer, self.dll.bx_t)

        def guarded_z_write(value) -> int:
            self.assertAlmostEqual(self.dll.bx_t, 0.0)
            self.dll.setpoint_z_t = value.value
            self.dll.bz_t = value.value
            return self.dll._code("set_field_z")

        self.dll.AttoDRY_Interface_getMagneticFieldX = converging_x
        self.dll.AttoDRY_Interface_setUserMagneticFieldZ = guarded_z_write

        state = self.driver.set_vector_field(
            VectorField(0.0, 0.1),
            max_step_t=0.1,
            monotonic=StepClock(step_s=5.0),
            sleeper=lambda _: None,
        )

        self.assertGreaterEqual(reads, 4)
        self.assertAlmostEqual(state.field.bz_t, 0.1, delta=1e-5)

    def test_read_failure_preserves_last_confirmed_state(self) -> None:
        self.connect()
        confirmed = self.driver.read_state()
        self.dll.return_codes["get_field_x"] = 3

        with self.assertRaises(AttoDryDllError):
            self.driver.read_state()

        self.assertIs(self.driver.last_confirmed_state, confirmed)

    def test_heater_power_read_failure_preserves_last_confirmed_state(self) -> None:
        self.connect()
        confirmed = self.driver.read_state()
        self.dll.return_codes["get_vti_heater_power"] = 6

        with self.assertRaisesRegex(AttoDryDllError, "getVtiHeaterPower.*6"):
            self.driver.read_heater_powers()

        self.assertIs(self.driver.last_confirmed_state, confirmed)

    def test_negative_heater_power_readback_is_rejected(self) -> None:
        self.connect()
        self.dll.sample_heater_power_w = -0.1

        with self.assertRaisesRegex(AttoDryError, "cannot be negative"):
            self.driver.read_heater_powers()

    def test_temperature_read_failure_preserves_last_confirmed_state(self) -> None:
        self.connect()
        confirmed = self.driver.read_state()
        self.dll.return_codes["get_sample_temperature"] = 3

        with self.assertRaises(AttoDryDllError):
            self.driver.read_state()

        self.assertIs(self.driver.last_confirmed_state, confirmed)

    def test_temperature_setpoint_write_records_full_confirmed_state(self) -> None:
        self.connect()

        self.driver.set_temperature(3.0)

        confirmed = self.driver.last_confirmed_state
        self.assertIsNotNone(confirmed)
        self.assertAlmostEqual(confirmed.user_temperature_k, 3.0)

    def test_temperature_setpoint_write_waits_for_delayed_readback(self) -> None:
        self.connect()
        user_readbacks = iter([2.0, 2.0, 3.0])

        def delayed_getter(pointer):
            return self.dll._float_getter(
                "get_user_temperature", pointer, next(user_readbacks)
            )

        def delayed_setter(value):
            return self.dll._code("set_temperature")

        self.dll.AttoDRY_Interface_getUserTemperature = delayed_getter
        self.dll.AttoDRY_Interface_setUserTemperature = delayed_setter
        sleeps = []

        self.driver.set_temperature(
            3.0,
            monotonic=iter([0.0, 0.0]).__next__,
            sleeper=sleeps.append,
        )

        self.assertEqual(sleeps, [1.5])
        self.assertAlmostEqual(
            self.driver.last_confirmed_state.user_temperature_k, 3.0
        )

    def test_temperature_setpoint_write_is_idempotent(self) -> None:
        self.connect()
        self.dll.user_temperature_k = 3.0

        self.driver.set_temperature(3.0)

        self.assertNotIn("set_temperature", self.dll.events)

    def test_temperature_setpoint_write_can_be_forced(self) -> None:
        self.connect()
        self.dll.user_temperature_k = 3.0

        self.driver.set_temperature(3.0, force_write=True)

        self.assertEqual(self.dll.events.count("set_temperature"), 1)

    def test_temperature_setpoint_mismatch_is_rejected(self) -> None:
        self.connect()

        def ignore_temperature_write(value):
            return self.dll._code("set_temperature")

        self.dll.AttoDRY_Interface_setUserTemperature = ignore_temperature_write

        with self.assertRaisesRegex(AttoDryTimeout, "setpoint readback"):
            self.driver.set_temperature(
                3.0,
                monotonic=iter([0.0, 30.0]).__next__,
                sleeper=lambda _: None,
            )

    def test_temperature_stability_requires_continuous_controlled_dwell(self) -> None:
        self.connect()
        self.dll.temperature_control = 1
        self.dll.sample_temperatures_k = deque([2.0] * 6)
        times = iter([0.0, 0.0, 10.0, 20.0, 30.0, 40.0, 50.0])
        original_getter = self.dll.AttoDRY_Interface_isControllingTemperature
        control_states = deque([1, 0, 1, 1, 1, 1])

        def sequenced_control(pointer):
            self.dll.temperature_control = control_states.popleft()
            return original_getter(pointer)

        self.dll.AttoDRY_Interface_isControllingTemperature = sequenced_control

        state = self.driver.wait_for_temperature(
            2.0,
            monotonic=times.__next__,
            sleeper=lambda _: None,
        )

        self.assertEqual(state.sample_temperature_k, 2.0)
        self.assertEqual(len(control_states), 0)

    def test_temperature_stable_readback_accepts_offset_from_setpoint(self) -> None:
        self.connect()
        self.dll.temperature_control = 1
        self.dll.sample_temperature_k = 2.05
        self.dll.user_temperature_k = 2.0
        times = iter([0.0, 0.0, 10.0, 20.0, 30.0, 40.0])

        state = self.driver.wait_for_temperature(
            2.0,
            monotonic=times.__next__,
            sleeper=lambda _: None,
        )

        self.assertAlmostEqual(state.sample_temperature_k, 2.05)

    def test_temperature_wait_rejects_error_and_timeout(self) -> None:
        self.connect()
        self.dll.temperature_control = 1
        self.dll.error_code = 4

        with self.assertRaisesRegex(AttoDryError, "error code 4"):
            self.driver.wait_for_temperature(
                2.0,
                monotonic=iter([0.0, 0.0]).__next__,
                sleeper=lambda _: None,
            )

        self.dll.error_code = 0
        self.dll.temperature_control = 0
        with self.assertRaises(AttoDryTimeout):
            self.driver.wait_for_temperature(
                2.0,
                monotonic=iter([0.0, 7200.0]).__next__,
                sleeper=lambda _: None,
            )

    def test_temperature_wait_rejects_invalid_target_before_read(self) -> None:
        self.connect()
        before = list(self.dll.events)

        with self.assertRaises(ValueError):
            self.driver.wait_for_temperature(float("nan"))

        self.assertEqual(self.dll.events, before)

    def test_temperature_wait_records_and_rejects_overshoot(self) -> None:
        self.connect()
        self.dll.temperature_control = 1
        self.dll.sample_temperature_k = 2.31
        recorded = []

        with self.assertRaisesRegex(AttoDryError, "overshoot limit"):
            self.driver.wait_for_temperature(
                2.1,
                max_overshoot_k=0.2,
                monotonic=iter([0.0, 0.0]).__next__,
                sleeper=lambda _: None,
                on_sample=lambda state, elapsed: recorded.append(
                    (state.sample_temperature_k, elapsed)
                ),
            )

        self.assertEqual(len(recorded), 1)
        self.assertAlmostEqual(recorded[0][0], 2.31, delta=1e-4)

    def test_field_stability_uses_control_error_and_full_dwell_window(self) -> None:
        self.connect()
        self.dll.field_control = 1
        self.dll.bx_t = 1.0
        self.dll.bz_t = 0.0
        self.dll.setpoint_x_t = 1.0
        self.dll.setpoint_z_t = 0.0
        times = iter([0.0, 0.0, 5.0, 10.0])

        state = self.driver.wait_for_field(
            VectorField(1.0, 0.0),
            monotonic=times.__next__,
            sleeper=lambda _: None,
        )

        self.assertEqual(state.field, VectorField(1.0, 0.0))
        self.assertGreaterEqual(self.dll.events.count("get_field_x"), 3)

    def test_field_stability_resets_after_field_control_loss(self) -> None:
        self.connect()
        target = VectorField(1.0, 0.0)
        self.dll.bx_t = self.dll.setpoint_x_t = target.bx_t
        self.dll.field_control = 1
        control_states = deque([1, 1, 1, 0, 1, 1, 1])
        original_getter = self.dll.AttoDRY_Interface_isControllingField

        def sequenced_control(pointer):
            self.dll.field_control = control_states.popleft()
            return original_getter(pointer)

        self.dll.AttoDRY_Interface_isControllingField = sequenced_control
        samples = []

        state = self.driver.wait_for_field(
            target,
            monotonic=iter([0.0, 0.0, 5.0, 9.0, 10.0, 15.0, 20.0, 25.0]).__next__,
            sleeper=lambda _: None,
            on_sample=lambda state, elapsed, phase, waypoint_index: samples.append(
                (state.field_control_enabled, elapsed, phase, waypoint_index)
            ),
        )

        self.assertEqual(state.field, target)
        self.assertEqual(len(control_states), 0)
        self.assertEqual(samples[-1][1], 25.0)
        self.assertEqual([sample[0] for sample in samples], [True, True, True, False, True, True, True])

    def test_field_stability_keeps_jitter_predecessor_at_tolerance_boundary(
        self,
    ) -> None:
        self.connect()
        tolerance = 1.0 / 1024.0
        self.driver.field_stability = replace(
            self.driver.field_stability,
            criteria=replace(
                self.driver.field_stability.criteria,
                tolerance=tolerance,
            ),
        )
        target = VectorField(0.25, 0.0)
        self.dll.field_control = 1
        self.dll.setpoint_x_t = target.bx_t
        self.dll.bx_t = target.bx_t + tolerance
        samples = []

        state = self.driver.wait_for_field(
            target,
            monotonic=iter([0.0, 0.0, 1.501, 5.501, 10.502]).__next__,
            sleeper=lambda _: None,
            on_sample=lambda state, elapsed, phase, waypoint_index: samples.append(
                (state, elapsed, phase, waypoint_index)
            ),
        )

        self.assertAlmostEqual(state.field.bx_t - target.bx_t, tolerance)
        self.assertEqual([sample[1] for sample in samples], [0.0, 1.501, 5.501, 10.502])
        self.assertTrue(all(sample[2] == "field_stability" for sample in samples))

    def test_zero_stability_rejects_out_of_band_jitter_predecessor(self) -> None:
        self.connect()
        self.dll.field_control = 1
        self.dll.setpoint_x_t = 0.0
        field_x = iter([0.002, 0.0, 0.0, 0.0, 0.0])
        self.dll.AttoDRY_Interface_getMagneticFieldX = (
            lambda pointer: self.dll._float_getter(
                "get_field_x", pointer, next(field_x)
            )
        )
        samples = []

        state = self.driver.wait_for_field(
            VectorField(0.0, 0.0),
            monotonic=iter(
                [0.0, 0.0, 1.501, 5.501, 10.502, 11.502]
            ).__next__,
            sleeper=lambda _: None,
            on_sample=lambda state, elapsed, phase, waypoint_index: samples.append(
                (state, elapsed, phase, waypoint_index)
            ),
        )

        self.assertEqual(state.field, VectorField(0.0, 0.0))
        self.assertEqual(
            [sample[1] for sample in samples],
            [0.0, 1.501, 5.501, 10.502, 11.502],
        )

    def test_field_wait_rejects_unsafe_actual_and_setpoint_during_monitoring(
        self,
    ) -> None:
        config = load_config("config/hardware.example.toml")
        for unsafe_readback in ("actual", "setpoint"):
            with self.subTest(unsafe_readback=unsafe_readback):
                dll = FakeAttoDryDll()
                driver = AttoDryDriver.from_config(
                    config,
                    dll=dll,
                    connection_authorized=True,
                    writes_authorized=True,
                )
                driver.connect(
                    monotonic=iter([0.0, 0.1]).__next__,
                    sleeper=lambda _: None,
                )
                dll.field_control = 1
                readbacks = deque([0.0, 3.1])
                samples = []

                if unsafe_readback == "actual":
                    dll.AttoDRY_Interface_getMagneticFieldX = lambda pointer: dll._float_getter(
                        "get_field_x", pointer, readbacks.popleft()
                    )
                else:
                    dll.AttoDRY_Interface_getMagneticFieldSetPointX = (
                        lambda pointer: dll._float_getter(
                            "get_setpoint_x", pointer, readbacks.popleft()
                        )
                    )

                with self.assertRaises(SafetyViolation):
                    driver.wait_for_field(
                        VectorField(0.0, 0.0),
                        monotonic=iter([0.0, 0.0, 1.0]).__next__,
                        sleeper=lambda _: None,
                        on_sample=lambda state, elapsed, phase, waypoint_index: samples.append(
                            (state, elapsed, phase, waypoint_index)
                        ),
                    )

                self.assertEqual(len(samples), 2)
                unsafe_state = samples[-1][0]
                unsafe_vector = (
                    unsafe_state.field
                    if unsafe_readback == "actual"
                    else unsafe_state.field_setpoint
                )
                self.assertGreater(unsafe_vector.magnitude_t, 3.0)

    def test_field_callbacks_cover_control_setpoint_stability_and_zero(self) -> None:
        self.connect()
        samples = []

        def record(state, elapsed, phase, waypoint_index):
            samples.append((state, elapsed, phase, waypoint_index))

        self.driver.ensure_field_control(True, on_sample=record)
        target = VectorField(0.1, 0.0)
        self.driver.set_vector_field(
            target,
            max_step_t=0.1,
            monotonic=StepClock(step_s=5.0),
            sleeper=lambda _: None,
            on_sample=record,
        )
        self.driver.wait_for_field(
            target,
            monotonic=iter([0.0, 0.0, 5.0, 10.0]).__next__,
            sleeper=lambda _: None,
            on_sample=record,
        )
        self.driver.request_zero_field(
            monotonic=iter([0.0, 0.0, 0.0, 0.0, 5.0, 10.0]).__next__,
            sleeper=lambda _: None,
            on_sample=record,
        )

        phases = {sample[2] for sample in samples}
        self.assertTrue(
            {
                "field_control_ack",
                "setpoint_preflight",
                "setpoint_x_ack",
                "setpoint_z_unchanged",
                "field_stability",
                "zero_preflight",
                "zero_setpoint_ack",
            }
            <= phases
        )

    def test_zero_request_monitors_to_configured_tolerance_and_zero_setpoint(
        self,
    ) -> None:
        self.connect()
        self.dll.field_control = 1
        tolerance = self.driver.field_stability.criteria.tolerance
        self.assertIsNotNone(tolerance)

        def settle_inside_configured_tolerance():
            self.dll.setpoint_x_t = 0.0
            self.dll.setpoint_z_t = 0.0
            self.dll.bx_t = 0.9 * tolerance
            self.dll.bz_t = 0.0
            return self.dll._code("sweep_zero")

        self.dll.AttoDRY_Interface_sweepFieldToZero = settle_inside_configured_tolerance

        state = self.driver.request_zero_field(
            monotonic=iter([0.0, 0.0, 0.0, 0.0, 5.0, 10.0]).__next__,
            sleeper=lambda _: None,
        )

        self.assertIn("sweep_zero", self.dll.events)
        self.assertEqual(state.field_setpoint, VectorField(0.0, 0.0))
        self.assertGreater(state.field.magnitude_t, 1e-5)
        self.assertLessEqual(state.field.magnitude_t, tolerance)

    def test_driver_rejects_uncommissioned_broad_field_tolerance(self) -> None:
        bad_stability = replace(
            self.driver.field_stability,
            criteria=replace(
                self.driver.field_stability.criteria,
                tolerance=0.01,
            ),
        )

        with self.assertRaisesRegex(ValueError, "cannot exceed 0.001 T"):
            AttoDryDriver(
                dll=FakeAttoDryDll(),
                com_port="COM_TEST",
                device_type=1,
                connection_timeout_s=10.0,
                temperature_min_k=1.6,
                temperature_max_k=300.0,
                limits=self.driver.limits,
                field_stability=bad_stability,
                temperature_stability=self.driver.temperature_stability,
                connection_authorized=True,
                writes_authorized=True,
            )

    def test_zero_request_rejects_actual_field_outside_configured_tolerance(
        self,
    ) -> None:
        self.connect()
        self.dll.field_control = 1
        tolerance = self.driver.field_stability.criteria.tolerance
        self.assertIsNotNone(tolerance)

        def settle_outside_configured_tolerance():
            self.dll.setpoint_x_t = 0.0
            self.dll.setpoint_z_t = 0.0
            self.dll.bx_t = 1.1 * tolerance
            self.dll.bz_t = 0.0
            return self.dll._code("sweep_zero")

        self.dll.AttoDRY_Interface_sweepFieldToZero = settle_outside_configured_tolerance

        with self.assertRaises(AttoDryTimeout):
            self.driver.request_zero_field(
                monotonic=iter([0.0, 0.0, 0.0, 0.0, 7200.0]).__next__,
                sleeper=lambda _: None,
            )

    def test_zero_request_requires_zero_setpoint_acknowledgement(self) -> None:
        self.connect()
        self.dll.field_control = 1

        def leave_nonzero_setpoint():
            self.dll.setpoint_x_t = 0.02
            self.dll.setpoint_z_t = 0.0
            self.dll.bx_t = 0.0
            self.dll.bz_t = 0.0
            return self.dll._code("sweep_zero")

        self.dll.AttoDRY_Interface_sweepFieldToZero = leave_nonzero_setpoint

        with self.assertRaisesRegex(AttoDryTimeout, "zero_setpoint_ack"):
            self.driver.request_zero_field(
                monotonic=iter([0.0, 30.0]).__next__,
                sleeper=lambda _: None,
            )

    def test_setting_write_requires_separate_write_authorization(self) -> None:
        config = load_config("config/hardware.example.toml")
        driver = AttoDryDriver.from_config(
            config,
            dll=self.dll,
            connection_authorized=True,
            writes_authorized=False,
        )
        driver.connect(
            monotonic=iter([0.0, 0.1]).__next__, sleeper=lambda _: None
        )

        with self.assertRaises(AttoDryAuthorizationError):
            driver.set_temperature(3.0)

        self.assertNotIn("set_temperature", self.dll.events)

    def test_read_only_cli_requires_authorization_before_loading_dll(self) -> None:
        loaded = []

        with self.assertRaises(AttoDryAuthorizationError):
            run_attodry_test(
                ["--config", "config/hardware.example.toml"],
                dll_loader=lambda path: loaded.append(path),
            )

        self.assertEqual(loaded, [])

    def test_read_only_cli_reads_state_without_setting_writes(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = run_attodry_test(
                [
                    "--config",
                    "config/hardware.example.toml",
                    "--samples",
                    "2",
                    "--interval-s",
                    "0",
                    "--authorize-connection",
                ],
                dll_loader=lambda _: self.dll,
            )

        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(result["completed"])
        self.assertFalse(result["writes_authorized"])
        self.assertEqual(len(result["samples"]), 2)
        self.assertEqual(
            result["samples"][0]["heater_power"],
            {"sample_w": 0.25, "vti_w": 0.5},
        )
        self.assertTrue(result["disconnected"])
        self.assertEqual(self.dll.events[-2:], ["disconnect", "end"])
        self.assertFalse(
            any(
                event.startswith(("set_", "toggle_", "sweep_"))
                for event in self.dll.events
            )
        )

    @staticmethod
    def temperature_args(
        *,
        success_policy: str = "hold-target",
        failure_policy: str = "disable-control",
        timeout_s: str = "5",
    ) -> list[str]:
        return [
            "--config",
            "config/hardware.example.toml",
            "--target-k",
            "2.1",
            "--max-delta-k",
            "0.2",
            "--max-overshoot-k",
            "0.2",
            "--tolerance-k",
            "0.01",
            "--stable-range-k",
            "0.005",
            "--dwell-s",
            "2",
            "--poll-interval-s",
            "1",
            "--timeout-s",
            timeout_s,
            "--success-policy",
            success_policy,
            "--failure-policy",
            failure_policy,
        ]

    def test_temperature_cli_requires_both_authorizations_before_dll_load(self) -> None:
        loaded = []
        args = self.temperature_args() + ["--authorize-connection"]

        with self.assertRaises(AttoDryAuthorizationError):
            run_temperature_test(args, dll_loader=lambda path: loaded.append(path))

        self.assertEqual(loaded, [])

    def test_temperature_cli_reads_parameters_from_commissioning_config(self) -> None:
        self.dll.temperature_follows_setpoint = True
        output = io.StringIO()
        request_path = Path(".test-tmp") / "temperature_commissioning.toml"
        request_path.parent.mkdir(exist_ok=True)
        self.addCleanup(request_path.unlink, missing_ok=True)
        request_path.write_text(
            """[temperature_commissioning]
target_k = 2.1
max_delta_k = 0.2
max_overshoot_k = 0.2
tolerance_k = 0.01
stable_range_k = 0.005
dwell_s = 2.0
poll_interval_s = 1.0
timeout_s = 5.0
success_policy = "hold-target"
failure_policy = "disable-control"
""",
            encoding="utf-8",
        )
        with redirect_stdout(output):
            exit_code = run_temperature_test(
                [
                    "--config",
                    "config/hardware.example.toml",
                    "--commissioning-config",
                    str(request_path),
                    "--authorize-connection",
                    "--authorize-temperature-write",
                ],
                dll_loader=lambda _: self.dll,
                monotonic=StepClock(),
                sleeper=lambda _: None,
                wall_time=lambda: 123.0,
            )

        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(result["completed"])
        self.assertAlmostEqual(result["request"]["target_k"], 2.1)
        self.assertGreaterEqual(len(result["target_samples"]), 3)
        self.assertEqual(self.dll.events.count("set_temperature"), 1)

    def test_temperature_cli_rejects_mixed_parameter_sources_before_dll_load(self) -> None:
        loaded = []
        args = self.temperature_args() + [
            "--commissioning-config",
            "config/temperature_commissioning.example.toml",
            "--authorize-connection",
            "--authorize-temperature-write",
        ]

        with self.assertRaisesRegex(ValueError, "cannot be combined"):
            run_temperature_test(args, dll_loader=lambda path: loaded.append(path))

        self.assertEqual(loaded, [])

    def test_temperature_cli_rejects_placeholder_commissioning_config_before_dll_load(
        self,
    ) -> None:
        loaded = []

        with self.assertRaisesRegex(ValueError, "target_k must be a number"):
            run_temperature_test(
                [
                    "--config",
                    "config/hardware.example.toml",
                    "--commissioning-config",
                    "config/temperature_commissioning.example.toml",
                    "--authorize-connection",
                    "--authorize-temperature-write",
                ],
                dll_loader=lambda path: loaded.append(path),
            )

        self.assertEqual(loaded, [])

    def test_temperature_cli_rejects_out_of_range_target_before_dll_load(self) -> None:
        loaded = []
        args = self.temperature_args()
        args[args.index("--target-k") + 1] = "301"

        with self.assertRaisesRegex(ValueError, "outside configured limits"):
            run_temperature_test(
                args + ["--authorize-connection", "--authorize-temperature-write"],
                dll_loader=lambda path: loaded.append(path),
            )

        self.assertEqual(loaded, [])

    def test_temperature_cli_records_stable_target_and_holds_it(self) -> None:
        self.dll.temperature_follows_setpoint = True
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = run_temperature_test(
                self.temperature_args()
                + ["--authorize-connection", "--authorize-temperature-write"],
                dll_loader=lambda _: self.dll,
                monotonic=StepClock(),
                sleeper=lambda _: None,
                wall_time=lambda: 123.0,
            )

        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(result["completed"])
        self.assertTrue(result["writes_authorized"])
        self.assertGreaterEqual(len(result["target_samples"]), 3)
        self.assertAlmostEqual(
            result["final_state"]["user_temperature_k"], 2.1, delta=1e-4
        )
        self.assertTrue(result["final_state"]["temperature_control_enabled"])
        self.assertEqual(self.dll.events.count("set_temperature"), 1)
        self.assertEqual(self.dll.events.count("toggle_temperature_control"), 1)
        self.assertLess(
            self.dll.events.index("toggle_temperature_control"),
            self.dll.events.index("set_temperature"),
        )
        self.assertTrue(result["setpoint_force_reapply_requested"])
        self.assertEqual(
            result["command_actions"],
            [
                "temperature_control_confirmed_enabled",
                "temperature_setpoint_confirmed",
            ],
        )
        self.assertTrue(result["disconnected"])

    def test_temperature_cli_reapplies_matching_setpoint_after_enabling_control(
        self,
    ) -> None:
        self.dll.user_temperature_k = 2.1
        self.dll.temperature_follows_setpoint = True
        output = io.StringIO()

        with redirect_stdout(output):
            run_temperature_test(
                self.temperature_args()
                + ["--authorize-connection", "--authorize-temperature-write"],
                dll_loader=lambda _: self.dll,
                monotonic=StepClock(),
                sleeper=lambda _: None,
                wall_time=lambda: 123.0,
            )

        result = json.loads(output.getvalue())
        self.assertTrue(result["completed"])
        self.assertTrue(result["setpoint_force_reapply_requested"])
        self.assertEqual(self.dll.events.count("set_temperature"), 1)
        self.assertLess(
            self.dll.events.index("toggle_temperature_control"),
            self.dll.events.index("set_temperature"),
        )

    def test_temperature_cli_max_delta_uses_initial_sample_sensor(self) -> None:
        self.dll.sample_temperature_k = 1.7244
        self.dll.user_temperature_k = 2.0
        self.dll.temperature_follows_setpoint = True
        args = self.temperature_args()
        args[args.index("--target-k") + 1] = "1.75"
        args[args.index("--max-delta-k") + 1] = "0.05"
        output = io.StringIO()

        with redirect_stdout(output):
            run_temperature_test(
                args + ["--authorize-connection", "--authorize-temperature-write"],
                dll_loader=lambda _: self.dll,
                monotonic=StepClock(),
                sleeper=lambda _: None,
                wall_time=lambda: 123.0,
            )

        result = json.loads(output.getvalue())
        check = result["prewrite_check"]
        self.assertTrue(check["passed"])
        self.assertAlmostEqual(check["sample_target_delta_k"], 0.0256, places=4)
        self.assertAlmostEqual(check["user_setpoint_target_delta_k"], 0.25)
        self.assertEqual(self.dll.events.count("set_temperature"), 1)

    def test_temperature_cli_rejects_excessive_step_before_write(self) -> None:
        args = self.temperature_args()
        args[args.index("--max-delta-k") + 1] = "0.05"
        output = io.StringIO()

        with redirect_stdout(output), self.assertRaisesRegex(
            ValueError, "sample-temperature movement exceeds.*max-delta-k"
        ):
            run_temperature_test(
                args + ["--authorize-connection", "--authorize-temperature-write"],
                dll_loader=lambda _: self.dll,
                monotonic=StepClock(),
                sleeper=lambda _: None,
                wall_time=lambda: 123.0,
            )

        result = json.loads(output.getvalue())
        self.assertFalse(result["completed"])
        self.assertNotIn("set_temperature", self.dll.events)
        self.assertNotIn("toggle_temperature_control", self.dll.events)
        self.assertTrue(result["disconnected"])

    def test_temperature_cli_can_restore_initial_disabled_state(self) -> None:
        self.dll.temperature_follows_setpoint = True
        output = io.StringIO()

        with redirect_stdout(output):
            run_temperature_test(
                self.temperature_args(success_policy="restore-initial")
                + ["--authorize-connection", "--authorize-temperature-write"],
                dll_loader=lambda _: self.dll,
                monotonic=StepClock(),
                sleeper=lambda _: None,
                wall_time=lambda: 123.0,
            )

        result = json.loads(output.getvalue())
        self.assertTrue(result["completed"])
        self.assertAlmostEqual(result["final_state"]["user_temperature_k"], 2.0)
        self.assertFalse(result["final_state"]["temperature_control_enabled"])
        self.assertEqual(self.dll.events.count("set_temperature"), 2)
        self.assertEqual(self.dll.events.count("toggle_temperature_control"), 2)
        self.assertEqual(
            result["recovery_actions"],
            [
                "temperature_control_restored_disabled",
                "temperature_setpoint_restored",
            ],
        )

    def test_temperature_cli_timeout_restores_initial_state_when_requested(self) -> None:
        output = io.StringIO()

        with redirect_stdout(output), self.assertRaises(AttoDryTimeout):
            run_temperature_test(
                self.temperature_args(
                    failure_policy="restore-initial", timeout_s="2"
                )
                + ["--authorize-connection", "--authorize-temperature-write"],
                dll_loader=lambda _: self.dll,
                monotonic=StepClock(),
                sleeper=lambda _: None,
                wall_time=lambda: 123.0,
            )

        result = json.loads(output.getvalue())
        self.assertFalse(result["completed"])
        self.assertIn("timed out", result["error"])
        self.assertAlmostEqual(result["final_state"]["user_temperature_k"], 2.0)
        self.assertFalse(result["final_state"]["temperature_control_enabled"])
        self.assertEqual(self.dll.events.count("set_temperature"), 2)
        self.assertTrue(result["disconnected"])

    def test_temperature_cli_failure_disables_control_and_records_diagnostic(
        self,
    ) -> None:
        output = io.StringIO()

        with redirect_stdout(output), self.assertRaises(AttoDryTimeout):
            run_temperature_test(
                self.temperature_args(timeout_s="2")
                + ["--authorize-connection", "--authorize-temperature-write"],
                dll_loader=lambda _: self.dll,
                monotonic=StepClock(),
                sleeper=lambda _: None,
                wall_time=lambda: 123.0,
            )

        result = json.loads(output.getvalue())
        self.assertFalse(result["completed"])
        self.assertEqual(self.dll.events.count("set_temperature"), 1)
        self.assertEqual(self.dll.events.count("toggle_temperature_control"), 2)
        self.assertAlmostEqual(
            result["final_state"]["user_temperature_k"], 2.1, delta=1e-4
        )
        self.assertFalse(result["final_state"]["temperature_control_enabled"])
        self.assertEqual(
            result["recovery_actions"],
            ["temperature_control_disabled_after_failure"],
        )
        self.assertEqual(
            result["failure_diagnostic"]["heater_power"],
            {"sample_w": 0.25, "vti_w": 0.5},
        )
        self.assertAlmostEqual(
            result["failure_diagnostic"]["trigger_state"][
                "sample_temperature_k"
            ],
            2.0,
        )
        self.assertAlmostEqual(
            result["failure_diagnostic"]["trigger_state"]["vti_temperature_k"],
            2.1,
            delta=1e-4,
        )

    def test_temperature_cli_overshoot_disables_control(self) -> None:
        self.dll.sample_temperatures_k = deque([2.0, 2.0, 2.0, 2.0, 2.31])
        output = io.StringIO()

        with redirect_stdout(output), self.assertRaisesRegex(
            AttoDryError, "overshoot limit"
        ):
            run_temperature_test(
                self.temperature_args()
                + ["--authorize-connection", "--authorize-temperature-write"],
                dll_loader=lambda _: self.dll,
                monotonic=StepClock(),
                sleeper=lambda _: None,
                wall_time=lambda: 123.0,
            )

        result = json.loads(output.getvalue())
        self.assertFalse(result["completed"])
        self.assertAlmostEqual(
            result["target_samples"][-1]["state"]["sample_temperature_k"],
            2.31,
            delta=1e-4,
        )
        self.assertAlmostEqual(
            result["failure_diagnostic"]["trigger_state"][
                "sample_temperature_k"
            ],
            2.31,
            delta=1e-4,
        )
        self.assertFalse(result["final_state"]["temperature_control_enabled"])
        self.assertEqual(
            result["recovery_actions"],
            ["temperature_control_disabled_after_failure"],
        )

    def test_temperature_config_rejects_obsolete_hold_current_policy(self) -> None:
        loaded = []
        request_path = Path(".test-tmp") / "temperature_hold_current.toml"
        request_path.parent.mkdir(exist_ok=True)
        self.addCleanup(request_path.unlink, missing_ok=True)
        request_path.write_text(
            """[temperature_commissioning]
target_k = 2.1
max_delta_k = 0.2
max_overshoot_k = 0.2
tolerance_k = 0.01
stable_range_k = 0.005
dwell_s = 2.0
poll_interval_s = 1.0
timeout_s = 5.0
success_policy = "hold-target"
failure_policy = "hold-current"
""",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "disable-control, restore-initial"):
            run_temperature_test(
                [
                    "--config",
                    "config/hardware.example.toml",
                    "--commissioning-config",
                    str(request_path),
                    "--authorize-connection",
                    "--authorize-temperature-write",
                ],
                dll_loader=lambda path: loaded.append(path),
            )

        self.assertEqual(loaded, [])

    def test_temperature_failure_keeps_primary_error_if_heater_read_fails(
        self,
    ) -> None:
        self.dll.return_codes["get_vti_heater_power"] = 6
        output = io.StringIO()

        with redirect_stdout(output), self.assertRaises(AttoDryTimeout):
            run_temperature_test(
                self.temperature_args(timeout_s="2")
                + ["--authorize-connection", "--authorize-temperature-write"],
                dll_loader=lambda _: self.dll,
                monotonic=StepClock(),
                sleeper=lambda _: None,
                wall_time=lambda: 123.0,
            )

        result = json.loads(output.getvalue())
        self.assertIn("timed out", result["error"])
        self.assertIn(
            "getVtiHeaterPower",
            result["failure_diagnostic"]["heater_power_read_error"],
        )
        self.assertEqual(
            result["recovery_actions"],
            ["temperature_control_disabled_after_failure"],
        )
        self.assertFalse(result["final_state"]["temperature_control_enabled"])

    def test_temperature_cli_does_not_claim_disconnect_after_close_failure(self) -> None:
        self.dll.temperature_follows_setpoint = True
        self.dll.return_codes["disconnect"] = 5
        output = io.StringIO()

        with redirect_stdout(output), self.assertRaisesRegex(
            AttoDryDllError, "Disconnect.*5"
        ):
            run_temperature_test(
                self.temperature_args()
                + ["--authorize-connection", "--authorize-temperature-write"],
                dll_loader=lambda _: self.dll,
                monotonic=StepClock(),
                sleeper=lambda _: None,
                wall_time=lambda: 123.0,
            )

        result = json.loads(output.getvalue())
        self.assertFalse(result["completed"])
        self.assertFalse(result["disconnected"])
        self.assertIn("Disconnect", result["close_error"])

    def temperature_run_config(
        self, *, target_k: float = 2.1, pre_measure_wait_s: float = 1800.0
    ) -> Path:
        path = Path(".test-tmp") / "temperature_run_hardware.toml"
        path.parent.mkdir(exist_ok=True)
        self.addCleanup(path.unlink, missing_ok=True)
        text = Path("config/hardware.example.toml").read_text(encoding="utf-8")
        text = text.replace("target_k = 1.8", f"target_k = {target_k}")
        text = text.replace(
            "pre_measure_wait_s = 1800.0",
            f"pre_measure_wait_s = {pre_measure_wait_s}",
        )
        path.write_text(text, encoding="utf-8")
        return path

    def test_temperature_run_uses_one_config_and_records_actual_temperature(
        self,
    ) -> None:
        output = io.StringIO()
        config_path = self.temperature_run_config()

        with redirect_stdout(output):
            exit_code = run_temperature_operation(
                ["--config", str(config_path)],
                dll_loader=lambda _: self.dll,
                # Keep acknowledgement reads inside their 30 s deadline;
                # the no-op sleeper still makes the 1800 s monitor deterministic.
                monotonic=StepClock(step_s=10.0),
                sleeper=lambda _: None,
                wall_time=lambda: 123.0,
            )

        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(result["completed"])
        self.assertTrue(result["measurement_ready"])
        self.assertAlmostEqual(
            result["measurement_state"]["sample_temperature_k"], 2.0
        )
        self.assertAlmostEqual(
            result["measurement_state"]["user_temperature_k"], 2.1, delta=1e-4
        )
        self.assertGreaterEqual(
            result["temperature_samples"][-1]["elapsed_s"], 1800.0
        )
        self.assertTrue(result["final_state"]["temperature_control_enabled"])
        self.assertLess(
            self.dll.events.index("toggle_temperature_control"),
            self.dll.events.index("set_temperature"),
        )
        self.assertEqual(self.dll.events[-2:], ["disconnect", "end"])

    def test_temperature_run_overshoot_disables_control(self) -> None:
        self.dll.sample_temperature_k = 2.31
        output = io.StringIO()
        config_path = self.temperature_run_config(pre_measure_wait_s=2.0)

        with redirect_stdout(output), self.assertRaisesRegex(
            AttoDryError, "overshoot limit"
        ):
            run_temperature_operation(
                ["--config", str(config_path)],
                dll_loader=lambda _: self.dll,
                monotonic=StepClock(),
                sleeper=lambda _: None,
                wall_time=lambda: 123.0,
            )

        result = json.loads(output.getvalue())
        self.assertFalse(result["completed"])
        self.assertFalse(result["measurement_ready"])
        self.assertAlmostEqual(
            result["temperature_samples"][-1]["state"]["sample_temperature_k"],
            2.31,
            delta=1e-4,
        )
        self.assertEqual(
            result["recovery_actions"],
            ["temperature_control_disabled_after_failure"],
        )
        self.assertFalse(result["final_state"]["temperature_control_enabled"])
        self.assertTrue(result["disconnected"])

    def test_temperature_run_continue_rechecks_and_finishes_after_interrupt(
        self,
    ) -> None:
        output = io.StringIO()
        config_path = self.temperature_run_config(pre_measure_wait_s=2.0)
        text = config_path.read_text(encoding="utf-8").replace(
            'interrupt_policy = "abort"',
            'interrupt_policy = "continue"',
        ).replace(
            "resume_recheck_s = 30.0",
            "resume_recheck_s = 1.0",
        )
        config_path.write_text(text, encoding="utf-8")

        class InterruptOnce:
            def __init__(self) -> None:
                self.interrupted = False

            def __call__(self, _: float) -> None:
                if not self.interrupted:
                    self.interrupted = True
                    raise KeyboardInterrupt

        with redirect_stdout(output):
            exit_code = run_temperature_operation(
                ["--config", str(config_path)],
                dll_loader=lambda _: self.dll,
                monotonic=StepClock(),
                sleeper=InterruptOnce(),
                wall_time=lambda: 123.0,
            )

        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(result["completed"])
        self.assertEqual(result["interruptions"][0]["action"], "continue")
        self.assertTrue(result["final_state"]["temperature_control_enabled"])

    def test_temperature_run_wait_confirmation_decline_uses_abort_cleanup(
        self,
    ) -> None:
        output = io.StringIO()
        config_path = self.temperature_run_config(pre_measure_wait_s=2.0)
        text = config_path.read_text(encoding="utf-8").replace(
            'interrupt_policy = "abort"',
            'interrupt_policy = "wait-confirmation"',
        )
        config_path.write_text(text, encoding="utf-8")

        class InterruptOnce:
            def __init__(self) -> None:
                self.interrupted = False

            def __call__(self, _: float) -> None:
                if not self.interrupted:
                    self.interrupted = True
                    raise KeyboardInterrupt

        with redirect_stdout(output), self.assertRaises(KeyboardInterrupt):
            run_temperature_operation(
                ["--config", str(config_path)],
                dll_loader=lambda _: self.dll,
                monotonic=StepClock(),
                sleeper=InterruptOnce(),
                wall_time=lambda: 123.0,
                confirmation=lambda _: "n",
            )

        result = json.loads(output.getvalue())
        self.assertEqual(result["interruptions"][0]["action"], "abort")
        self.assertFalse(result["final_state"]["temperature_control_enabled"])

    def test_temperature_run_continue_changes_to_confirmation_after_second_interrupt(
        self,
    ) -> None:
        output = io.StringIO()
        config_path = self.temperature_run_config(pre_measure_wait_s=2.0)
        text = config_path.read_text(encoding="utf-8").replace(
            'interrupt_policy = "abort"',
            'interrupt_policy = "continue"',
        ).replace(
            "resume_recheck_s = 30.0",
            "resume_recheck_s = 1.0",
        )
        config_path.write_text(text, encoding="utf-8")

        class InterruptTwice:
            def __init__(self) -> None:
                self.interruptions = 0

            def __call__(self, _: float) -> None:
                if self.interruptions < 2:
                    self.interruptions += 1
                    raise KeyboardInterrupt

        with redirect_stdout(output):
            exit_code = run_temperature_operation(
                ["--config", str(config_path)],
                dll_loader=lambda _: self.dll,
                monotonic=StepClock(step_s=0.1),
                sleeper=InterruptTwice(),
                wall_time=lambda: 123.0,
                confirmation=lambda _: "yes",
            )

        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            [item["action"] for item in result["interruptions"]],
            ["continue", "continue-after-confirmation"],
        )

    def temperature_scan_config(
        self, *, start_k: float = 1.7, stop_k: float = 1.9, step_k: float = 0.1
    ) -> tuple[Path, Path]:
        path = Path(".test-tmp") / "temperature_scan_hardware.toml"
        output_directory = path.parent / "temperature-scan-output"
        path.parent.mkdir(exist_ok=True)
        self.addCleanup(path.unlink, missing_ok=True)
        self.addCleanup(shutil.rmtree, output_directory, True)
        text = Path("config/hardware.example.toml").read_text(encoding="utf-8")
        text = text.replace("start_k = 1.7", f"start_k = {start_k}")
        text = text.replace("stop_k = 2.7", f"stop_k = {stop_k}")
        text = text.replace("step_k = 0.1", f"step_k = {step_k}")
        text = text.replace(
            "stable_dwell_s = 30.0\npoll_interval_s = 1.0\nwait_timeout_s = 7200.0",
            "stable_dwell_s = 2.0\npoll_interval_s = 1.0\nwait_timeout_s = 10.0",
        )
        text = text.replace(
            'output_directory = "../run_data/temperature_commissioning"',
            'output_directory = "temperature-scan-output"',
        )
        path.write_text(text, encoding="utf-8")
        return path, output_directory

    def test_temperature_scan_requires_authorization_before_dll_load(self) -> None:
        config_path, output_directory = self.temperature_scan_config()
        loaded: list[Path] = []

        with self.assertRaises(AttoDryAuthorizationError):
            run_temperature_scan(
                ["--config", str(config_path)],
                dll_loader=lambda path: loaded.append(Path(path)),
            )

        self.assertEqual(loaded, [])
        self.assertFalse(output_directory.exists())

    def test_temperature_scan_records_stability_time_and_incremental_audit(
        self,
    ) -> None:
        config_path, _ = self.temperature_scan_config()
        self.dll.sample_temperature_k = 1.7
        self.dll.user_temperature_k = 1.7
        self.dll.temperature_follows_setpoint = True
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = run_temperature_scan(
                [
                    "--config",
                    str(config_path),
                    "--authorize-temperature-scan",
                ],
                dll_loader=lambda _: self.dll,
                monotonic=StepClock(),
                sleeper=lambda _: None,
                wall_time=lambda: 123.0,
            )

        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(result["completed"])
        self.assertEqual(result["outcome"], "completed")
        self.assertEqual(
            [point["requested_temperature_k"] for point in result["points"]],
            [1.7, 1.8, 1.9],
        )
        self.assertTrue(
            all(point["time_to_stable_s"] >= 2.0 for point in result["points"])
        )
        self.assertEqual(self.dll.events.count("set_temperature"), 3)
        self.assertLess(
            self.dll.events.index("toggle_temperature_control"),
            self.dll.events.index("set_temperature"),
        )
        self.assertTrue(Path(result["stable_times_csv"]).is_file())
        progress = Path(result["progress_jsonl"]).read_text(encoding="utf-8")
        self.assertIn('"event": "temperature_sample"', progress)
        self.assertEqual(progress.count('"event": "point_completed"'), 3)

    def test_temperature_scan_records_actual_stable_readback_temperature(self) -> None:
        config_path, _ = self.temperature_scan_config(
            start_k=1.7, stop_k=1.8, step_k=0.1
        )
        self.dll.sample_temperature_k = 1.7
        self.dll.user_temperature_k = 1.7
        original_setter = self.dll.AttoDRY_Interface_setUserTemperature

        def offset_setter(value):
            result = original_setter(value)
            self.dll.sample_temperature_k = value.value + 0.05
            return result

        self.dll.AttoDRY_Interface_setUserTemperature = offset_setter
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = run_temperature_scan(
                [
                    "--config",
                    str(config_path),
                    "--authorize-temperature-scan",
                ],
                dll_loader=lambda _: self.dll,
                monotonic=StepClock(),
                sleeper=lambda _: None,
                wall_time=lambda: 123.0,
            )

        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        for point, expected in zip(result["points"], (1.75, 1.85)):
            self.assertAlmostEqual(point["measurement_temperature_k"], expected, places=5)
        self.assertTrue(
            all(point["time_to_first_tolerance_s"] is None for point in result["points"])
        )
        self.assertTrue(
            all("stable_mean_k" in line for line in Path(result["stable_times_csv"]).read_text().splitlines()[:1])
        )

    def test_temperature_scan_failure_disables_control(self) -> None:
        config_path, _ = self.temperature_scan_config(
            start_k=1.7, stop_k=1.7, step_k=0.1
        )
        self.dll.sample_temperature_k = 1.91
        self.dll.user_temperature_k = 1.7
        self.dll.temperature_control = 1
        output = io.StringIO()

        with redirect_stdout(output), self.assertRaisesRegex(
            AttoDryError, "overshoot limit"
        ):
            run_temperature_scan(
                [
                    "--config",
                    str(config_path),
                    "--authorize-temperature-scan",
                ],
                dll_loader=lambda _: self.dll,
                monotonic=StepClock(),
                sleeper=lambda _: None,
                wall_time=lambda: 123.0,
            )

        result = json.loads(output.getvalue())
        self.assertEqual(result["outcome"], "rejected")
        self.assertEqual(
            result["recovery_actions"],
            ["temperature_control_disabled_after_failure"],
        )
        self.assertFalse(result["final_state"]["temperature_control_enabled"])

    def test_temperature_scan_continue_restarts_stability_window(self) -> None:
        config_path, _ = self.temperature_scan_config(
            start_k=1.7, stop_k=1.7, step_k=0.1
        )
        text = config_path.read_text(encoding="utf-8").replace(
            'interrupt_policy = "abort"',
            'interrupt_policy = "continue"',
        ).replace(
            "resume_recheck_s = 30.0",
            "resume_recheck_s = 1.0",
        )
        config_path.write_text(text, encoding="utf-8")
        self.dll.sample_temperature_k = 1.7
        self.dll.user_temperature_k = 1.7
        self.dll.temperature_follows_setpoint = True

        class InterruptOnce:
            def __init__(self) -> None:
                self.interrupted = False

            def __call__(self, _: float) -> None:
                if not self.interrupted:
                    self.interrupted = True
                    raise KeyboardInterrupt

        output = io.StringIO()
        with redirect_stdout(output):
            run_temperature_scan(
                [
                    "--config",
                    str(config_path),
                    "--authorize-temperature-scan",
                ],
                dll_loader=lambda _: self.dll,
                monotonic=StepClock(),
                sleeper=InterruptOnce(),
                wall_time=lambda: 123.0,
            )

        result = json.loads(output.getvalue())
        self.assertEqual(result["interruptions"][0]["action"], "continue")
        self.assertEqual(result["points"][0]["successful_attempt_index"], 1)
        progress = Path(result["progress_jsonl"]).read_text(encoding="utf-8")
        self.assertIn('"phase": "resume_recheck"', progress)

    def test_temperature_scan_resume_skips_completed_points(self) -> None:
        config_path, _ = self.temperature_scan_config(
            start_k=1.7, stop_k=1.8, step_k=0.1
        )
        self.dll.sample_temperature_k = 1.7
        self.dll.user_temperature_k = 1.7
        self.dll.temperature_follows_setpoint = True
        first_output = io.StringIO()
        with redirect_stdout(first_output):
            run_temperature_scan(
                [
                    "--config",
                    str(config_path),
                    "--authorize-temperature-scan",
                ],
                dll_loader=lambda _: self.dll,
                monotonic=StepClock(),
                sleeper=lambda _: None,
                wall_time=lambda: 123.0,
            )
        first_result = json.loads(first_output.getvalue())
        progress_path = Path(first_result["progress_jsonl"])
        retained: list[str] = []
        for line in progress_path.read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event.get("event") == "scan_finished":
                continue
            if event.get("point_index") == 1:
                continue
            retained.append(line)
        progress_path.write_text("\n".join(retained) + "\n", encoding="utf-8")

        resumed_dll = FakeAttoDryDll()
        resumed_dll.sample_temperature_k = 1.7
        resumed_dll.user_temperature_k = 1.7
        resumed_dll.temperature_control = 1
        resumed_dll.temperature_follows_setpoint = True
        resumed_output = io.StringIO()
        with redirect_stdout(resumed_output):
            run_temperature_scan(
                [
                    "--config",
                    str(config_path),
                    "--authorize-temperature-scan",
                    "--resume-progress",
                    str(progress_path),
                ],
                dll_loader=lambda _: resumed_dll,
                monotonic=StepClock(),
                sleeper=lambda _: None,
                wall_time=lambda: 124.0,
            )

        result = json.loads(resumed_output.getvalue())
        self.assertEqual(len(result["points"]), 2)
        self.assertEqual(resumed_dll.events.count("set_temperature"), 1)
        self.assertAlmostEqual(result["points"][1]["requested_temperature_k"], 1.8)


class StepClock:
    def __init__(self, step_s: float = 1.0) -> None:
        self.value = -step_s
        self.step_s = step_s

    def __call__(self) -> float:
        self.value += self.step_s
        return self.value


if __name__ == "__main__":
    unittest.main()
