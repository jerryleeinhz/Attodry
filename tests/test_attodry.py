from collections import deque
import ctypes
import math
import unittest
from unittest.mock import patch

from attodry_control.attodry import (
    AttoDryAuthorizationError,
    AttoDryDriver,
    AttoDryDllError,
    AttoDryTimeout,
)
from attodry_control.config import load_config
from attodry_control.models import VectorField
from attodry_control.safety import SafetyViolation


class FakeAttoDryDll:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.return_codes: dict[str, int] = {}
        self.initialized = deque([1])
        self.sample_temperature_k = 2.0
        self.user_temperature_k = 2.0
        self.vti_temperature_k = 2.1
        self.bx_t = 0.0
        self.bz_t = 0.0
        self.setpoint_x_t = 0.0
        self.setpoint_z_t = 0.0
        self.temperature_control = 0
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
        return self._float_getter("get_sample_temperature", pointer, self.sample_temperature_k)

    def AttoDRY_Interface_getUserTemperature(self, pointer):
        return self._float_getter("get_user_temperature", pointer, self.user_temperature_k)

    def AttoDRY_Interface_getVtiTemperature(self, pointer):
        return self._float_getter("get_vti_temperature", pointer, self.vti_temperature_k)

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

        self.driver.set_vector_field(VectorField(0.0, 3.0), max_step_t=0.5)

        self.assertAlmostEqual(self.dll.setpoint_x_t, 0.0)
        self.assertAlmostEqual(self.dll.setpoint_z_t, 3.0)
        self.assertGreater(self.dll.events.count("set_field_x"), 2)

    def test_read_failure_preserves_last_confirmed_state(self) -> None:
        self.connect()
        confirmed = self.driver.read_state()
        self.dll.return_codes["get_field_x"] = 3

        with self.assertRaises(AttoDryDllError):
            self.driver.read_state()

        self.assertIs(self.driver.last_confirmed_state, confirmed)

    def test_field_stability_uses_control_error_and_full_dwell_window(self) -> None:
        self.connect()
        self.dll.field_control = 1
        self.dll.bx_t = 1.0
        self.dll.bz_t = 0.0
        times = iter([0.0, 0.0, 5.0, 10.0])

        state = self.driver.wait_for_field(
            VectorField(1.0, 0.0),
            monotonic=times.__next__,
            sleeper=lambda _: None,
        )

        self.assertEqual(state.field, VectorField(1.0, 0.0))
        self.assertGreaterEqual(self.dll.events.count("get_field_x"), 3)

    def test_zero_request_calls_vendor_sweep_then_monitors_readback(self) -> None:
        self.connect()
        self.dll.field_control = 1
        with patch.object(self.driver, "wait_for_field") as wait:
            self.driver.request_zero_field()

        self.assertIn("sweep_zero", self.dll.events)
        wait.assert_called_once_with(VectorField(0.0, 0.0))

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


if __name__ == "__main__":
    unittest.main()
