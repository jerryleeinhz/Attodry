from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
import math
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from attodry_control.attodry import (
    AttoDryAuthorizationError,
    AttoDryDllError,
    AttoDryError,
)
from attodry_control.config import ConfigError, load_magnetic_field_operation_config
from attodry_control.field_audit import read_jsonl_events
from attodry_control.magnetic_field import (
    execute_field_target,
    execute_ordered_field_points,
)
from attodry_control.magnetic_field_cli import (
    _validate_hold_state,
    run as run_magnetic_field,
)
from attodry_control.magnetic_field_monitor import read_progress_snapshot
from attodry_control.models import CryostatState, VectorField
from attodry_control.safety import (
    FIELD_LIMIT_POLICY,
    FieldTransitionPolicy,
    MagnetLimits,
    SafetyViolation,
    float32_field,
    plan_ordered_zero_detours,
    plan_zero_detour,
    validate_vector_field,
)
from tests.test_attodry import FakeAttoDryDll, StepClock


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _state(field: VectorField, *, control: bool = True) -> CryostatState:
    return CryostatState(
        sample_temperature_k=2.0,
        user_temperature_k=2.0,
        vti_temperature_k=2.1,
        field=field,
        field_setpoint=field,
        temperature_control_enabled=False,
        field_control_enabled=control,
        error_code=0,
    )


class InjectedFieldDriver:
    """Small executor spy; connection ownership deliberately remains observable."""

    def __init__(self) -> None:
        self.connected = True
        self.writes_authorized = True
        self.limits = MagnetLimits()
        self.current = VectorField(0.0, 0.0)
        self.control_enabled = False
        self.last_confirmed_state = _state(self.current, control=False)
        self.control_requests: list[bool] = []
        self.targets: list[VectorField] = []
        self.wait_targets: list[VectorField] = []
        self.connect_calls = 0
        self.close_calls = 0

    def connect(self, **_: object) -> None:
        self.connect_calls += 1

    def close(self) -> None:
        self.close_calls += 1

    def read_state(self) -> CryostatState:
        state = _state(self.current, control=self.control_enabled)
        self.last_confirmed_state = state
        return state

    def ensure_field_control(self, enabled: bool, **_: object) -> None:
        self.control_requests.append(enabled)
        self.control_enabled = enabled
        self.last_confirmed_state = _state(self.current, control=enabled)

    def set_vector_field(self, target: VectorField, **_: object) -> CryostatState:
        self.targets.append(target)
        self.current = target
        self.last_confirmed_state = _state(target, control=self.control_enabled)
        return self.last_confirmed_state

    def wait_for_field(self, target: VectorField, **kwargs: object) -> CryostatState:
        self.wait_targets.append(target)
        state = _state(self.current, control=self.control_enabled)
        callback = kwargs.get("on_sample")
        if callable(callback):
            callback(state, 0.0, "field_stability", None)
        self.last_confirmed_state = state
        return state


class _MagneticConfigFixture:
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def write_config(
        self,
        points: tuple[tuple[float, float], ...],
        *,
        normal_end_policy: str = "hold",
        transition_policy: str = "via_zero",
        max_step_t: float = 0.5,
        field_tolerance_t: float = 0.001,
        extra_run: str = "",
        extra_tables: str = "",
    ) -> Path:
        rendered_points = ", ".join(
            f"{{ bx_t = {bx!r}, bz_t = {bz!r} }}" for bx, bz in points
        )
        path = self.directory / "hardware.local.toml"
        path.write_text(
            f"""
[project]
mode = "hardware"
database_path = "unused.sqlite"

[cryostat]
backend = "legacy_dll"
com_port = "COM_TEST"
dll_path = "C:/does/not/exist/attoDRYxyz64bit.dll"
device_type = 1
connection_timeout_s = 10.0
temperature_min_k = 1.6
temperature_max_k = 300.0

[magnet]
hardware_x_max_t = 3.0
hardware_z_max_t = 9.0
experiment_vector_max_t = 3.0
field_tolerance_t = {field_tolerance_t!r}
stable_range_t = 0.0005
stable_dwell_s = 2.0
poll_interval_s = 1.0
wait_timeout_s = 10.0

[cleanup]
normal_end_field_policy = "{normal_end_policy}"
exception_field_policy = "zero"

[magnetic_field_run]
points = [{rendered_points}]
transition_policy = "{transition_policy}"
max_step_t = {max_step_t!r}
run_name = "ordered_fields"
note = "Offline fake-DLL magnetic regression."
output_directory = "field-output"
{extra_run}

{extra_tables}
""".strip()
            + "\n",
            encoding="utf-8",
        )
        return path


class MagneticFieldConfigTests(_MagneticConfigFixture, unittest.TestCase):
    def test_new_limits_accept_single_axes_and_reject_combined_overlimit(self):
        for point in ((0.0, 9.0), (0.0, -9.0), (3.0, 0.0), (-3.0, 0.0)):
            with self.subTest(point=point):
                config = load_magnetic_field_operation_config(self.write_config((point,)))
                self.assertEqual(config.run.points, (VectorField(*point),))
        for point in ((3.1, 0.0), (0.0, 9.1), (1e-12, 9.0), (1.0, 3.0)):
            with self.subTest(point=point), self.assertRaises((ConfigError, SafetyViolation)):
                load_magnetic_field_operation_config(self.write_config((point,)))


    def test_ordered_points_preserve_order_and_duplicates(self) -> None:
        path = self.write_config(((1.0, 0.0), (0.0, 2.0), (1.0, 0.0), (1.0, 0.0)))

        config = load_magnetic_field_operation_config(path)

        self.assertEqual(
            config.run.points,
            (
                VectorField(1.0, 0.0),
                VectorField(0.0, 2.0),
                VectorField(1.0, 0.0),
                VectorField(1.0, 0.0),
            ),
        )
        self.assertEqual(config.run.transition_policy, FieldTransitionPolicy.VIA_ZERO)
        self.assertEqual(config.run.max_step_t, 0.5)
        self.assertEqual(config.run.run_name, "ordered_fields")
        self.assertEqual(config.run.note, "Offline fake-DLL magnetic regression.")
        self.assertEqual(config.run.output_directory, Path("field-output"))

    def test_unsafe_point_and_unknown_run_field_are_rejected(self) -> None:
        unsafe = self.write_config(((2.0, 2.5),))
        with self.assertRaises((ConfigError, SafetyViolation)):
            load_magnetic_field_operation_config(unsafe)

        unknown = self.write_config(((0.1, 0.0),), extra_run="unknown = true")
        with self.assertRaisesRegex(ConfigError, "unknown"):
            load_magnetic_field_operation_config(unknown)

    def test_transition_policy_is_required_and_strict(self) -> None:
        direct = self.write_config(((0.1, 0.0),), transition_policy="direct")
        self.assertEqual(
            load_magnetic_field_operation_config(direct).run.transition_policy,
            FieldTransitionPolicy.DIRECT,
        )

        invalid = self.write_config(
            ((0.1, 0.0),), transition_policy="implicit_zero"
        )
        with self.assertRaisesRegex(ConfigError, "transition_policy"):
            load_magnetic_field_operation_config(invalid)

        missing = self.write_config(((0.1, 0.0),))
        missing.write_text(
            missing.read_text(encoding="utf-8").replace(
                'transition_policy = "via_zero"\n', ""
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ConfigError, "transition_policy"):
            load_magnetic_field_operation_config(missing)

    def test_unrelated_incomplete_instrument_tables_are_not_parsed(self) -> None:
        path = self.write_config(
            ((0.1, 0.0),),
            extra_tables='[lockin_xx]\naddress = "not-used"\n\n[smu_bias]\naddress = "not-used"',
        )

        config = load_magnetic_field_operation_config(path)

        self.assertEqual(config.run.points, (VectorField(0.1, 0.0),))

    def test_step_resolution_and_zero_tolerance_are_bounded(self) -> None:
        tiny_step = self.write_config(((0.1, 0.0),), max_step_t=5e-6)
        with self.assertRaisesRegex(ConfigError, "acknowledgement tolerance"):
            load_magnetic_field_operation_config(tiny_step)

        broad_zero = self.write_config(
            ((0.1, 0.0),), field_tolerance_t=0.01
        )
        with self.assertRaisesRegex(ConfigError, "zero-field criterion"):
            load_magnetic_field_operation_config(broad_zero)


class MagneticFieldPlanningTests(unittest.TestCase):
    def test_zero_detour_prevalidates_waypoints_and_x_then_z_transients(self) -> None:
        start = VectorField(3.0, 0.0)
        target = VectorField(0.0, 3.0)

        path = plan_zero_detour(start, target, 0.5)

        self.assertEqual(path[-1], target)
        self.assertIn(VectorField(0.0, 0.0), path)
        previous = start
        for waypoint in path:
            validate_vector_field(waypoint)
            validate_vector_field(VectorField(waypoint.bx_t, previous.bz_t))
            self.assertLessEqual(
                math.hypot(
                    waypoint.bx_t - previous.bx_t,
                    waypoint.bz_t - previous.bz_t,
                ),
                0.5 + 1e-12,
            )
            previous = waypoint

    def test_ordered_planner_retains_duplicate_entries_without_cartesian_expansion(self) -> None:
        targets = (
            VectorField(1.0, 0.0),
            VectorField(0.0, 2.0),
            VectorField(1.0, 0.0),
            VectorField(1.0, 0.0),
        )

        paths = plan_ordered_zero_detours(VectorField(0.0, 0.0), targets, 0.5)

        self.assertEqual(len(paths), len(targets))
        self.assertEqual(tuple(path[-1] for path in paths[:-1]), targets[:-1])
        self.assertEqual(paths[-1], ())
        with self.assertRaises(SafetyViolation):
            plan_ordered_zero_detours(
                VectorField(0.0, 0.0),
                targets + (VectorField(2.0, 2.5),),
                0.5,
            )


class MagneticFieldExecutorTests(unittest.TestCase):
    def test_single_target_executor_does_not_take_connection_ownership(self) -> None:
        driver = InjectedFieldDriver()
        target = VectorField(0.4, -0.3)

        execute_field_target(
            driver,
            target,
            0.1,
            point_index=7,
            monotonic=StepClock(),
            sleeper=lambda _: None,
        )

        self.assertEqual(driver.control_requests, [True])
        self.assertEqual(driver.targets, [target])
        self.assertEqual(driver.wait_targets, [float32_field(target)])
        self.assertEqual(driver.connect_calls, 0)
        self.assertEqual(driver.close_calls, 0)

    def test_ordered_executor_preserves_duplicate_point_calls(self) -> None:
        driver = InjectedFieldDriver()
        points = (
            VectorField(0.25, 0.0),
            VectorField(0.0, 0.25),
            VectorField(0.25, 0.0),
            VectorField(0.25, 0.0),
        )

        execute_ordered_field_points(
            driver,
            points,
            0.1,
            monotonic=StepClock(),
            sleeper=lambda _: None,
        )

        self.assertEqual(driver.targets, list(points))
        self.assertEqual(driver.wait_targets, [float32_field(point) for point in points])
        self.assertEqual(driver.connect_calls, 0)
        self.assertEqual(driver.close_calls, 0)


class MagneticFieldCliTests(_MagneticConfigFixture, unittest.TestCase):
    def test_high_z_single_target_returns_verified_zero_and_versions_limit_policy(self):
        path = self.write_config(((0.0, 9.0),), max_step_t=3.0)
        output = io.StringIO()
        dll = FakeAttoDryDll()
        with redirect_stdout(output):
            code = run_magnetic_field(
                ["single-target", "--config", str(path),
                 "--authorize-connection", "--authorize-field-writes"],
                dll_loader=lambda _: dll, monotonic=StepClock(), sleeper=lambda _: None,
            )
        summary = json.loads(output.getvalue())
        progress = Path(summary["progress_jsonl"])
        events, _ = read_jsonl_events(progress)
        self.assertEqual(code, 0)
        self.assertTrue(summary["zero_verified"])
        self.assertEqual(events[0]["field_limit_policy"], FIELD_LIMIT_POLICY)
        self.assertNotIn("set_field_x", dll.events)
        snapshot = read_progress_snapshot(progress)
        self.assertEqual(snapshot["integrity_errors"], [])
        self.assertEqual(snapshot["outcome"], "completed")

        # A record without the new declaration still uses the old 3 T cap.
        events[0].pop("field_limit_policy")
        progress.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
        legacy = read_progress_snapshot(progress)
        self.assertEqual(legacy["outcome"], "incomplete")
        self.assertTrue(any("3 T project vector limit" in e for e in legacy["integrity_errors"]))

    def test_high_z_to_x_scan_audits_selected_safe_corner_only(self):
        path = self.write_config(
            ((0.0, 9.0), (3.0, 0.0)), max_step_t=10.0, transition_policy="direct",
        )
        output = io.StringIO()
        with redirect_stdout(output):
            run_magnetic_field(
                ["scan", "--config", str(path), "--authorize-connection",
                 "--authorize-field-writes", "--authorize-ordered-field-scan"],
                dll_loader=lambda _: FakeAttoDryDll(),
                monotonic=StepClock(), sleeper=lambda _: None,
            )
        progress = Path(json.loads(output.getvalue())["progress_jsonl"])
        self.assertEqual(read_progress_snapshot(progress)["integrity_errors"], [])
        events, _ = read_jsonl_events(progress)
        for event in events:
            if event["event"] == "field_waypoint_execution" and event["point_index"] == 1:
                event["axis_order"] = "x_then_z"
        progress.write_text("".join(json.dumps(e) + "\n" for e in events), encoding="utf-8")
        invalid = read_progress_snapshot(progress)
        self.assertEqual(invalid["outcome"], "incomplete")
        self.assertTrue(any("violates recorded field limits" in e for e in invalid["integrity_errors"]))

    def test_zero_hold_uses_vector_magnitude_not_only_component_tolerance(
        self,
    ) -> None:
        path = self.write_config(((0.0, 0.0),), field_tolerance_t=0.001)
        config = load_magnetic_field_operation_config(path)
        driver = SimpleNamespace(
            limits=config.magnet.limits,
            field_stability=config.magnet.stability,
        )
        state = CryostatState(
            sample_temperature_k=2.0,
            user_temperature_k=2.0,
            vti_temperature_k=2.1,
            field=VectorField(0.0008, 0.0008),
            field_setpoint=VectorField(0.0, 0.0),
            temperature_control_enabled=False,
            field_control_enabled=True,
            error_code=0,
        )

        with self.assertRaisesRegex(AttoDryError, "target tolerance"):
            _validate_hold_state(driver, state, VectorField(0.0, 0.0))

    def test_dll_load_failure_still_writes_terminal_manual_verification(self) -> None:
        path = self.write_config(((0.25, 0.0),))

        def fail_load(_: Path) -> object:
            raise AttoDryDllError("injected DLL load failure")

        with redirect_stdout(io.StringIO()), self.assertRaises(AttoDryDllError):
            run_magnetic_field(
                [
                    "single-target",
                    "--config",
                    str(path),
                    "--authorize-connection",
                    "--authorize-field-writes",
                ],
                dll_loader=fail_load,
            )

        progress_path = next((path.parent / "field-output").glob("*.jsonl"))
        events, trailing_line_incomplete = read_jsonl_events(progress_path)
        self.assertFalse(trailing_line_incomplete)
        self.assertEqual(events[0]["event"], "run_started")
        terminal = events[-1]
        self.assertEqual(terminal["event"], "run_finished")
        self.assertEqual(terminal["outcome"], "rejected")
        self.assertFalse(terminal["zero_verified"])
        self.assertTrue(terminal["manual_verification_required"])
        self.assertIsNone(terminal["last_confirmed_state"])

    def test_keyboard_interrupt_attempts_one_zero_and_disconnects(self) -> None:
        path = self.write_config(((0.25, 0.0),))
        dll = FakeAttoDryDll()

        with (
            patch(
                "attodry_control.magnetic_field_cli.execute_ordered_field_points",
                side_effect=KeyboardInterrupt,
            ),
            redirect_stdout(io.StringIO()),
            self.assertRaises(KeyboardInterrupt),
        ):
            run_magnetic_field(
                [
                    "single-target",
                    "--config",
                    str(path),
                    "--authorize-connection",
                    "--authorize-field-writes",
                ],
                dll_loader=lambda _: dll,
                monotonic=StepClock(),
                sleeper=lambda _: None,
            )

        self.assertEqual(dll.events.count("sweep_zero"), 1)
        self.assertEqual(dll.events.count("disconnect"), 1)
        self.assertEqual(dll.events.count("end"), 1)
        progress_path = next((path.parent / "field-output").glob("*.jsonl"))
        events, trailing_line_incomplete = read_jsonl_events(progress_path)
        self.assertFalse(trailing_line_incomplete)
        terminal = events[-1]
        self.assertEqual(terminal["outcome"], "interrupted")
        self.assertTrue(terminal["zero_verified"])
        self.assertFalse(terminal["manual_verification_required"])
        self.assertTrue(terminal["disconnected"])

    def test_interrupt_during_normal_zero_retries_cleanup_and_disconnects(self) -> None:
        path = self.write_config(((0.25, 0.0),))
        dll = FakeAttoDryDll()
        original_zero = dll.AttoDRY_Interface_sweepFieldToZero
        zero_calls = 0

        def interrupt_first_zero() -> int:
            nonlocal zero_calls
            zero_calls += 1
            if zero_calls == 1:
                raise KeyboardInterrupt
            return original_zero()

        dll.AttoDRY_Interface_sweepFieldToZero = interrupt_first_zero
        with redirect_stdout(io.StringIO()), self.assertRaises(KeyboardInterrupt):
            run_magnetic_field(
                [
                    "single-target",
                    "--config",
                    str(path),
                    "--authorize-connection",
                    "--authorize-field-writes",
                ],
                dll_loader=lambda _: dll,
                monotonic=StepClock(),
                sleeper=lambda _: None,
            )

        self.assertEqual(zero_calls, 2)
        self.assertEqual(dll.events.count("sweep_zero"), 1)
        self.assertEqual(dll.events.count("disconnect"), 1)
        progress_path = next((path.parent / "field-output").glob("*.jsonl"))
        events, trailing_line_incomplete = read_jsonl_events(progress_path)
        self.assertFalse(trailing_line_incomplete)
        terminal = events[-1]
        self.assertEqual(terminal["outcome"], "interrupted")
        self.assertTrue(terminal["zero_verified"])
        self.assertFalse(terminal["manual_verification_required"])

    def test_communication_failure_never_credits_later_apparent_zero(self) -> None:
        path = self.write_config(((0.25, 0.0),))
        dll = FakeAttoDryDll()

        with (
            patch(
                "attodry_control.magnetic_field_cli.execute_ordered_field_points",
                side_effect=AttoDryDllError("injected communication failure"),
            ),
            redirect_stdout(io.StringIO()),
            self.assertRaises(AttoDryDllError),
        ):
            run_magnetic_field(
                [
                    "single-target",
                    "--config",
                    str(path),
                    "--authorize-connection",
                    "--authorize-field-writes",
                ],
                dll_loader=lambda _: dll,
                monotonic=StepClock(),
                sleeper=lambda _: None,
            )

        self.assertEqual(dll.events.count("sweep_zero"), 1)
        progress_path = next((path.parent / "field-output").glob("*.jsonl"))
        events, trailing_line_incomplete = read_jsonl_events(progress_path)
        self.assertFalse(trailing_line_incomplete)
        terminal = events[-1]
        self.assertEqual(terminal["outcome"], "rejected")
        self.assertFalse(terminal["zero_verified"])
        self.assertTrue(terminal["manual_verification_required"])
        self.assertIsInstance(terminal["last_confirmed_state"], dict)

    def test_raw_oserror_never_credits_later_apparent_zero(self) -> None:
        path = self.write_config(((0.25, 0.0),))
        dll = FakeAttoDryDll()

        with (
            patch(
                "attodry_control.magnetic_field_cli.execute_ordered_field_points",
                side_effect=OSError("injected low-level I/O failure"),
            ),
            redirect_stdout(io.StringIO()),
            self.assertRaises(OSError),
        ):
            run_magnetic_field(
                [
                    "single-target",
                    "--config",
                    str(path),
                    "--authorize-connection",
                    "--authorize-field-writes",
                ],
                dll_loader=lambda _: dll,
                monotonic=StepClock(),
                sleeper=lambda _: None,
            )

        progress_path = next((path.parent / "field-output").glob("*.jsonl"))
        events, trailing_line_incomplete = read_jsonl_events(progress_path)
        self.assertFalse(trailing_line_incomplete)
        terminal = events[-1]
        self.assertFalse(terminal["zero_verified"])
        self.assertTrue(terminal["manual_verification_required"])

    def test_finite_invalid_state_readback_never_credits_later_apparent_zero(
        self,
    ) -> None:
        path = self.write_config(((0.25, 0.0),))
        dll = FakeAttoDryDll()
        sample_reads = 0

        def invalid_once(pointer: object) -> int:
            nonlocal sample_reads
            sample_reads += 1
            value = 0.0 if sample_reads == 1 else 2.0
            return dll._float_getter("get_sample_temperature", pointer, value)

        dll.AttoDRY_Interface_getSampleTemperature = invalid_once
        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(
            AttoDryError, "invalid full-state readback"
        ):
            run_magnetic_field(
                [
                    "single-target",
                    "--config",
                    str(path),
                    "--authorize-connection",
                    "--authorize-field-writes",
                ],
                dll_loader=lambda _: dll,
                monotonic=StepClock(),
                sleeper=lambda _: None,
            )

        self.assertEqual(dll.events.count("sweep_zero"), 1)
        progress_path = next((path.parent / "field-output").glob("*.jsonl"))
        events, trailing_line_incomplete = read_jsonl_events(progress_path)
        self.assertFalse(trailing_line_incomplete)
        terminal = events[-1]
        self.assertFalse(terminal["zero_verified"])
        self.assertTrue(terminal["manual_verification_required"])

    def test_single_target_requires_authorization_before_dll_loading(self) -> None:
        path = self.write_config(((0.25, 0.0),))
        loaded: list[Path] = []

        with self.assertRaises(AttoDryAuthorizationError):
            run_magnetic_field(
                ["single-target", "--config", str(path)],
                dll_loader=lambda candidate: loaded.append(Path(candidate)),
            )

        self.assertEqual(loaded, [])
        self.assertFalse((path.parent / "field-output").exists())

    def test_scan_requires_its_separate_authorization_before_dll_loading(self) -> None:
        path = self.write_config(((0.25, 0.0), (0.0, 0.25)))
        loaded: list[Path] = []

        with self.assertRaises(AttoDryAuthorizationError):
            run_magnetic_field(
                [
                    "scan",
                    "--config",
                    str(path),
                    "--authorize-connection",
                    "--authorize-field-writes",
                ],
                dll_loader=lambda candidate: loaded.append(Path(candidate)),
            )

        self.assertEqual(loaded, [])
        self.assertFalse((path.parent / "field-output").exists())

    def test_single_target_success_is_monitored_to_zero_and_fsynced_to_jsonl(self) -> None:
        path = self.write_config(((0.25, 0.0),))
        dll = FakeAttoDryDll()
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = run_magnetic_field(
                [
                    "single-target",
                    "--config",
                    str(path),
                    "--authorize-connection",
                    "--authorize-field-writes",
                ],
                dll_loader=lambda _: dll,
                monotonic=StepClock(),
                sleeper=lambda _: None,
            )

        summary = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(summary["outcome"], "completed")
        self.assertTrue(summary["completed"])
        self.assertTrue(summary["zero_verified"])
        self.assertFalse(summary["manual_verification_required"])
        self.assertTrue(summary["disconnected"])
        self.assertAlmostEqual(dll.bx_t, 0.0)
        self.assertAlmostEqual(dll.bz_t, 0.0)

        progress_path = next((path.parent / "field-output").glob("*.jsonl"))
        events, trailing_line_incomplete = read_jsonl_events(progress_path)
        self.assertFalse(trailing_line_incomplete)
        self.assertEqual(events[0]["event"], "run_started")
        self.assertEqual(events[-1]["event"], "run_finished")
        self.assertEqual(events[-1]["outcome"], "completed")
        indices = [event["event_index"] for event in events]
        self.assertEqual(indices, list(range(indices[0], indices[0] + len(indices))))
        self.assertTrue(
            all(
                {"schema_version", "run_id", "captured_unix_s"} <= event.keys()
                for event in events
            )
        )
        started = events[0]
        self.assertEqual(
            started["config_sha256"], hashlib.sha256(path.read_bytes()).hexdigest()
        )
        self.assertEqual(started["field_stability"]["minimum_samples"], 3)
        self.assertEqual(started["driver_field_protocol"]["command_ack_timeout_s"], 30.0)
        self.assertEqual(started["transition_policy"], "via_zero")
        self.assertEqual(
            started["field_command_audit"],
            {
                "protocol": "attempt-result-v1",
                "required": True,
                "float32_encoding": "ieee754-binary32-bits-hex",
            },
        )
        self.assertTrue(started["authorization_scope"]["connection"])
        self.assertEqual(started["cryostat_interface"]["com_port"], "COM_TEST")
        attempts = [
            event for event in events if event["event"] == "field_command_attempt"
        ]
        results = [
            event for event in events if event["event"] == "field_command_result"
        ]
        self.assertEqual(summary["field_command_attempt_count"], len(attempts))
        self.assertEqual(summary["field_command_result_count"], len(results))
        self.assertTrue(summary["field_command_audit_complete"])
        self.assertEqual(
            [event["command_index"] for event in attempts], list(range(len(attempts)))
        )
        self.assertEqual(
            [event["command_index"] for event in results],
            [event["command_index"] for event in attempts],
        )
        components = [
            event
            for event in attempts
            if event["command_kind"] == "set_field_component"
        ]
        self.assertTrue(components)
        self.assertTrue(
            all(
                "float32_ieee754_bits_hex" in event
                and "float32_value_t" in event
                for event in components
            )
        )
        self.assertTrue(
            all(
                event["acknowledgement"] == "confirmed"
                and event["dll_return_code"] == 0
                for event in results
            )
        )
        self.assertTrue(
            any(event["event"] == "field_waypoint_execution" for event in events)
        )
        snapshot = read_progress_snapshot(progress_path)
        self.assertEqual(snapshot["outcome"], "completed")
        self.assertTrue(snapshot["audit_complete"])
        self.assertEqual(snapshot["integrity_errors"], [])

    def test_direct_scan_audits_a_direct_path_without_a_hidden_zero_waypoint(
        self,
    ) -> None:
        points = ((0.25, 0.0), (0.0, 0.25))
        path = self.write_config(
            points,
            normal_end_policy="hold",
            transition_policy="direct",
        )
        dll = FakeAttoDryDll()

        with redirect_stdout(io.StringIO()):
            exit_code = run_magnetic_field(
                [
                    "scan",
                    "--config",
                    str(path),
                    "--authorize-connection",
                    "--authorize-field-writes",
                    "--authorize-ordered-field-scan",
                ],
                dll_loader=lambda _: dll,
                monotonic=StepClock(),
                sleeper=lambda _: None,
            )

        self.assertEqual(exit_code, 0)
        self.assertNotIn("sweep_zero", dll.events)
        progress_path = next((path.parent / "field-output").glob("*.jsonl"))
        events, trailing_line_incomplete = read_jsonl_events(progress_path)
        self.assertFalse(trailing_line_incomplete)
        started = events[0]
        self.assertEqual(started["transition_policy"], "direct")
        point_starts = [event for event in events if event["event"] == "point_started"]
        self.assertEqual(len(point_starts), 2)
        second_plan = point_starts[1]["execution_plan"]
        self.assertEqual(second_plan["transition_policy"], "direct")
        self.assertEqual(len(second_plan["waypoints"]), 1)
        self.assertNotEqual(
            second_plan["waypoints"][0]["command_field"],
            {
                "bx_t": 0.0,
                "bz_t": 0.0,
                "bx_t_float32_bits_hex": "00000000",
                "bz_t_float32_bits_hex": "00000000",
            },
        )

    def test_ordered_scan_holds_normally_and_retains_duplicate_point_events(self) -> None:
        points = ((0.25, 0.0), (0.0, 0.25), (0.25, 0.0), (0.25, 0.0))
        path = self.write_config(points, normal_end_policy="hold")
        dll = FakeAttoDryDll()
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = run_magnetic_field(
                [
                    "scan",
                    "--config",
                    str(path),
                    "--authorize-connection",
                    "--authorize-field-writes",
                    "--authorize-ordered-field-scan",
                ],
                dll_loader=lambda _: dll,
                monotonic=StepClock(),
                sleeper=lambda _: None,
            )

        summary = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(summary["outcome"], "completed")
        self.assertFalse(summary["manual_verification_required"])
        self.assertAlmostEqual(dll.bx_t, points[-1][0])
        self.assertAlmostEqual(dll.bz_t, points[-1][1])

        progress_path = next((path.parent / "field-output").glob("*.jsonl"))
        events, trailing_line_incomplete = read_jsonl_events(progress_path)
        self.assertFalse(trailing_line_incomplete)
        completed = [
            event
            for event in events
            if event["event"] == "point_completed"
        ]
        self.assertEqual([event["point_index"] for event in completed], [0, 1, 2, 3])
        self.assertEqual(
            [event["requested_field"] for event in completed],
            [{"bx_t": bx, "bz_t": bz} for bx, bz in points],
        )

    def test_ordered_scan_zero_policy_is_monitored_before_disconnect(self) -> None:
        path = self.write_config(
            ((0.25, 0.0), (0.0, 0.25)), normal_end_policy="zero"
        )
        dll = FakeAttoDryDll()
        output = io.StringIO()

        with redirect_stdout(output):
            exit_code = run_magnetic_field(
                [
                    "scan",
                    "--config",
                    str(path),
                    "--authorize-connection",
                    "--authorize-field-writes",
                    "--authorize-ordered-field-scan",
                ],
                dll_loader=lambda _: dll,
                monotonic=StepClock(),
                sleeper=lambda _: None,
            )

        summary = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(dll.events.count("sweep_zero"), 1)
        self.assertLess(
            dll.events.index("sweep_zero"), dll.events.index("disconnect")
        )
        self.assertTrue(summary["zero_verified"])
        self.assertFalse(summary["manual_verification_required"])
        self.assertAlmostEqual(dll.bx_t, 0.0)
        self.assertAlmostEqual(dll.bz_t, 0.0)

    def test_float32_duplicate_is_remeasured_without_an_extra_zero_detour(self) -> None:
        path = self.write_config(
            ((0.1, 0.0), (0.1, 0.0)), normal_end_policy="hold"
        )
        dll = FakeAttoDryDll()

        with redirect_stdout(io.StringIO()):
            exit_code = run_magnetic_field(
                [
                    "scan",
                    "--config",
                    str(path),
                    "--authorize-connection",
                    "--authorize-field-writes",
                    "--authorize-ordered-field-scan",
                ],
                dll_loader=lambda _: dll,
                monotonic=StepClock(),
                sleeper=lambda _: None,
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(dll.events.count("set_field_x"), 1)
        progress_path = next((path.parent / "field-output").glob("*.jsonl"))
        events, trailing_line_incomplete = read_jsonl_events(progress_path)
        self.assertFalse(trailing_line_incomplete)
        completed = [
            event for event in events if event["event"] == "point_completed"
        ]
        self.assertEqual(len(completed), 2)
        self.assertEqual(completed[1]["waypoint_count"], 0)

    def test_float32_boundary_steps_match_audit_plan_without_false_rejection(
        self,
    ) -> None:
        path = self.write_config(
            ((0.1, 0.0), (0.0, 0.0)),
            normal_end_policy="hold",
            max_step_t=0.05,
        )
        dll = FakeAttoDryDll()

        with redirect_stdout(io.StringIO()):
            exit_code = run_magnetic_field(
                [
                    "scan",
                    "--config",
                    str(path),
                    "--authorize-connection",
                    "--authorize-field-writes",
                    "--authorize-ordered-field-scan",
                ],
                dll_loader=lambda _: dll,
                monotonic=StepClock(),
                sleeper=lambda _: None,
        )

        self.assertEqual(exit_code, 0)
        progress_path = next((path.parent / "field-output").glob("*.jsonl"))
        events, trailing_line_incomplete = read_jsonl_events(progress_path)
        self.assertFalse(trailing_line_incomplete)
        x_attempts = [
            event
            for event in events
            if event["event"] == "field_command_attempt"
            and event["command_kind"] == "set_field_component"
            and event["axis"] == "x"
        ]
        self.assertEqual(dll.events.count("set_field_x"), len(x_attempts))
        self.assertEqual(len(x_attempts), 5)
        completed = [
            event for event in events if event["event"] == "point_completed"
        ]
        self.assertEqual([event["waypoint_count"] for event in completed], [2, 3])

    def test_component_write_failure_attempts_zero_and_preserves_uncertainty(
        self,
    ) -> None:
        path = self.write_config(((0.25, 0.0),))
        dll = FakeAttoDryDll()
        dll.return_codes["set_field_x"] = 7

        with redirect_stdout(io.StringIO()), self.assertRaises(AttoDryDllError):
            run_magnetic_field(
                [
                    "single-target",
                    "--config",
                    str(path),
                    "--authorize-connection",
                    "--authorize-field-writes",
                ],
                dll_loader=lambda _: dll,
                monotonic=StepClock(),
                sleeper=lambda _: None,
            )

        self.assertEqual(dll.events.count("set_field_x"), 1)
        self.assertEqual(dll.events.count("sweep_zero"), 1)
        progress_path = next((path.parent / "field-output").glob("*.jsonl"))
        events, _ = read_jsonl_events(progress_path)
        terminal = events[-1]
        self.assertFalse(terminal["zero_verified"])
        self.assertTrue(terminal["manual_verification_required"])
        self.assertEqual(
            terminal["last_confirmed_state"]["field_setpoint"],
            {"bx_t": 0.0, "bz_t": 0.0},
        )
        failed_component = next(
            event
            for event in events
            if event["event"] == "field_command_result"
            and event["command_kind"] == "set_field_component"
        )
        self.assertFalse(failed_component["success"])
        self.assertEqual(failed_component["acknowledgement"], "not_attempted")
        self.assertEqual(failed_component["dll_return_code"], 7)
        cleanup_zero = next(
            event
            for event in events
            if event["event"] == "field_command_result"
            and event["command_kind"] == "sweep_field_to_zero"
        )
        self.assertEqual(cleanup_zero["phase"], "exception_zero_cleanup")
        self.assertTrue(cleanup_zero["success"])
        self.assertEqual(cleanup_zero["acknowledgement"], "confirmed")
        self.assertTrue(terminal["field_command_audit_complete"])
        snapshot = read_progress_snapshot(progress_path)
        self.assertEqual(snapshot["outcome"], "rejected")
        self.assertTrue(snapshot["audit_complete"])
        self.assertEqual(snapshot["integrity_errors"], [])

    def test_nonzero_field_stability_timeout_attempts_uncredited_zero(self) -> None:
        path = self.write_config(((0.25, 0.0),))
        dll = FakeAttoDryDll()
        dll.AttoDRY_Interface_getMagneticFieldX = lambda pointer: dll._float_getter(
            "get_field_x", pointer, 0.0
        )

        with redirect_stdout(io.StringIO()), self.assertRaisesRegex(
            AttoDryError, "stability wait timed out"
        ):
            run_magnetic_field(
                [
                    "single-target",
                    "--config",
                    str(path),
                    "--authorize-connection",
                    "--authorize-field-writes",
                ],
                dll_loader=lambda _: dll,
                monotonic=StepClock(),
                sleeper=lambda _: None,
            )

        self.assertEqual(dll.events.count("sweep_zero"), 1)
        progress_path = next((path.parent / "field-output").glob("*.jsonl"))
        events, _ = read_jsonl_events(progress_path)
        terminal = events[-1]
        self.assertEqual(terminal["error_type"], "AttoDryTimeout")
        self.assertFalse(terminal["zero_verified"])
        self.assertTrue(terminal["manual_verification_required"])

    def test_audit_fsync_failure_cannot_interrupt_zero_cleanup(self) -> None:
        path = self.write_config(((0.25, 0.0),))
        dll = FakeAttoDryDll()
        output = io.StringIO()

        with (
            patch(
                "attodry_control.field_audit.os.fsync",
                side_effect=[None, None, OSError("injected fsync failure")],
            ),
            redirect_stdout(output),
            self.assertRaises(OSError),
        ):
            run_magnetic_field(
                [
                    "single-target",
                    "--config",
                    str(path),
                    "--authorize-connection",
                    "--authorize-field-writes",
                ],
                dll_loader=lambda _: dll,
                monotonic=StepClock(),
                sleeper=lambda _: None,
            )

        summary = json.loads(output.getvalue())
        self.assertEqual(dll.events.count("sweep_zero"), 1)
        self.assertEqual(dll.events.count("disconnect"), 1)
        self.assertTrue(summary["zero_verified"])
        self.assertFalse(summary["audit_complete"])
        self.assertTrue(summary["manual_verification_required"])

    def test_component_attempt_audit_failure_prevents_that_write_but_not_cleanup(
        self,
    ) -> None:
        path = self.write_config(((0.25, 0.0),))
        dll = FakeAttoDryDll()
        output = io.StringIO()
        real_fsync = os.fsync
        component_attempt_failure_injected = False

        def fail_component_attempt_fsync(file_descriptor: int) -> None:
            nonlocal component_attempt_failure_injected
            progress_files = list((path.parent / "field-output").glob("*.jsonl"))
            if progress_files and not component_attempt_failure_injected:
                last_line = progress_files[0].read_bytes().splitlines()[-1]
                if (
                    b'"event":"field_command_attempt"' in last_line
                    and b'"command_kind":"set_field_component"' in last_line
                ):
                    component_attempt_failure_injected = True
                    raise OSError("injected component-attempt fsync failure")
            real_fsync(file_descriptor)

        with (
            patch(
                "attodry_control.field_audit.os.fsync",
                fail_component_attempt_fsync,
            ),
            redirect_stdout(output),
            self.assertRaisesRegex(OSError, "component-attempt"),
        ):
            run_magnetic_field(
                [
                    "single-target",
                    "--config",
                    str(path),
                    "--authorize-connection",
                    "--authorize-field-writes",
                ],
                dll_loader=lambda _: dll,
                monotonic=StepClock(),
                sleeper=lambda _: None,
            )

        self.assertTrue(component_attempt_failure_injected)
        self.assertNotIn("set_field_x", dll.events)
        self.assertNotIn("set_field_z", dll.events)
        self.assertEqual(dll.events.count("sweep_zero"), 1)
        self.assertEqual(dll.events.count("disconnect"), 1)
        summary = json.loads(output.getvalue())
        self.assertFalse(summary["audit_complete"])
        self.assertFalse(summary["field_command_audit_complete"])
        self.assertTrue(summary["manual_verification_required"])
        progress_path = next((path.parent / "field-output").glob("*.jsonl"))
        self.assertEqual(read_progress_snapshot(progress_path)["outcome"], "incomplete")

    def test_terminal_fsync_failure_cannot_leave_certified_completion(self) -> None:
        path = self.write_config(((0.25, 0.0),))
        dll = FakeAttoDryDll()
        output = io.StringIO()
        real_fsync = os.fsync
        terminal_failure_injected = False

        def fail_terminal_fsync(file_descriptor: int) -> None:
            nonlocal terminal_failure_injected
            progress_files = list((path.parent / "field-output").glob("*.jsonl"))
            if progress_files and not terminal_failure_injected:
                final_line = progress_files[0].read_bytes().splitlines()[-1]
                if b'"event":"run_finished"' in final_line:
                    terminal_failure_injected = True
                    raise OSError("injected terminal fsync failure")
            real_fsync(file_descriptor)

        with (
            patch("attodry_control.field_audit.os.fsync", fail_terminal_fsync),
            redirect_stdout(output),
            self.assertRaises(OSError),
        ):
            run_magnetic_field(
                [
                    "single-target",
                    "--config",
                    str(path),
                    "--authorize-connection",
                    "--authorize-field-writes",
                ],
                dll_loader=lambda _: dll,
                monotonic=StepClock(),
                sleeper=lambda _: None,
            )

        self.assertTrue(terminal_failure_injected)
        summary = json.loads(output.getvalue())
        self.assertFalse(summary["audit_complete"])
        self.assertFalse(summary["field_command_audit_complete"])
        self.assertTrue(summary["manual_verification_required"])
        progress_path = next((path.parent / "field-output").glob("*.jsonl"))
        snapshot = read_progress_snapshot(progress_path)
        self.assertEqual(snapshot["outcome"], "incomplete")
        self.assertFalse(snapshot["audit_complete"])
        self.assertTrue(snapshot["manual_verification_required"])

    def test_disconnect_failure_uncredits_verified_zero_and_requires_manual_check(
        self,
    ) -> None:
        path = self.write_config(((0.25, 0.0),))
        dll = FakeAttoDryDll()
        dll.return_codes["disconnect"] = 5
        output = io.StringIO()

        with redirect_stdout(output), self.assertRaises(AttoDryDllError):
            run_magnetic_field(
                [
                    "single-target",
                    "--config",
                    str(path),
                    "--authorize-connection",
                    "--authorize-field-writes",
                ],
                dll_loader=lambda _: dll,
                monotonic=StepClock(),
                sleeper=lambda _: None,
            )

        summary = json.loads(output.getvalue())
        self.assertEqual(dll.events.count("sweep_zero"), 1)
        self.assertEqual(dll.events.count("disconnect"), 1)
        self.assertEqual(dll.events.count("end"), 1)
        self.assertEqual(summary["outcome"], "rejected")
        self.assertFalse(summary["zero_verified"])
        self.assertFalse(summary["disconnected"])
        self.assertTrue(summary["manual_verification_required"])

    def test_failed_zero_is_rejected_and_requires_manual_verification(self) -> None:
        path = self.write_config(((0.25, 0.0),))
        dll = FakeAttoDryDll()
        dll.return_codes["sweep_zero"] = 9
        output = io.StringIO()

        with redirect_stdout(output), self.assertRaises(AttoDryDllError):
            run_magnetic_field(
                [
                    "single-target",
                    "--config",
                    str(path),
                    "--authorize-connection",
                    "--authorize-field-writes",
                ],
                dll_loader=lambda _: dll,
                monotonic=StepClock(),
                sleeper=lambda _: None,
            )

        progress_path = next((path.parent / "field-output").glob("*.jsonl"))
        events, trailing_line_incomplete = read_jsonl_events(progress_path)
        self.assertFalse(trailing_line_incomplete)
        terminal = events[-1]
        self.assertEqual(terminal["event"], "run_finished")
        self.assertEqual(terminal["outcome"], "rejected")
        self.assertFalse(terminal["completed"])
        self.assertFalse(terminal["zero_verified"])
        self.assertTrue(terminal["manual_verification_required"])
        self.assertIsInstance(terminal["last_confirmed_state"], dict)


if __name__ == "__main__":
    unittest.main()
