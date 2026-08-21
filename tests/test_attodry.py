from collections import deque
import ctypes
from contextlib import redirect_stdout
import io
import json
import math
from pathlib import Path
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
from attodry_control.safety import SafetyViolation
from attodry_control.temperature_test import run as run_temperature_test


class FakeAttoDryDll:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.return_codes: dict[str, int] = {}
        self.initialized = deque([1])
        self.sample_temperature_k = 2.0
        self.sample_temperatures_k: deque[float] = deque()
        self.user_temperature_k = 2.0
        self.vti_temperature_k = 2.1
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

    def test_temperature_control_is_read_then_toggle_and_idempotent(self) -> None:
        self.connect()

        self.driver.ensure_temperature_control(True)
        self.driver.ensure_temperature_control(True)

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

    def test_temperature_setpoint_mismatch_is_rejected(self) -> None:
        self.connect()

        def ignore_temperature_write(value):
            return self.dll._code("set_temperature")

        self.dll.AttoDRY_Interface_setUserTemperature = ignore_temperature_write

        with self.assertRaisesRegex(AttoDryError, "setpoint readback"):
            self.driver.set_temperature(3.0)

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
        failure_policy: str = "hold-current",
        timeout_s: str = "5",
    ) -> list[str]:
        return [
            "--config",
            "config/hardware.example.toml",
            "--target-k",
            "2.1",
            "--max-delta-k",
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
        self.assertTrue(result["disconnected"])

    def test_temperature_cli_rejects_excessive_step_before_write(self) -> None:
        args = self.temperature_args()
        args[args.index("--max-delta-k") + 1] = "0.05"
        output = io.StringIO()

        with redirect_stdout(output), self.assertRaisesRegex(
            ValueError, "exceeds.*max-delta-k"
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

    def test_temperature_cli_hold_failure_policy_sends_no_recovery_write(self) -> None:
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
        self.assertEqual(self.dll.events.count("toggle_temperature_control"), 1)
        self.assertAlmostEqual(
            result["final_state"]["user_temperature_k"], 2.1, delta=1e-4
        )
        self.assertTrue(result["final_state"]["temperature_control_enabled"])

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


class StepClock:
    def __init__(self, step_s: float = 1.0) -> None:
        self.value = -step_s
        self.step_s = step_s

    def __call__(self) -> float:
        self.value += self.step_s
        return self.value


if __name__ == "__main__":
    unittest.main()
