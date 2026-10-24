"""XYZ motion/qualification/cleanup tested without SDK or real hardware."""
from copy import deepcopy
from dataclasses import replace
import unittest

from attodry_control.positioner_config import load_positioner_config, PositionerError
from attodry_control.positioner_points import PositionerPointSession
from tests.test_positioner_config import positioner_document, xyz


class FakeClock:
    def __init__(self):
        self.now, self.waits = 0., []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


class FakePositioner:
    def __init__(self, config):
        self.config, self.calls, self.read_hook = config, [], None
        self.move_error = self.stop_error = self.close_error = self.read_error = False
        self.never_complete = False
        self.state = {"serial_number": config.controller_serial_number,
             "controller_model": "ANC350 RES", "firmware_raw": config.expected_firmware_raw,
             "axes": {axis.role: {"axis_index": axis.index,
                 "actuator_name": axis.expected_actuator_name, "lut_name": axis.expected_lut_name,
                 "position_um": 0., "amplitude_v": axis.amplitude_v, "frequency_hz": axis.frequency_hz,
                 "sensor_connected": True, "output_enabled": False, "moving": False,
                 "target_reached": False, "eot_forward": False, "eot_backward": False,
                 "sensor_error": False} for axis in config.axes}}

    def open(self):
        self.calls.append("open")

    def read_state(self):
        self.calls.append("read")
        if self.read_error:
            raise IOError("USB lost")
        if self.read_hook:
            self.read_hook(self)
        return deepcopy(self.state)

    def _read_initial_state(self, *, allow_enabled=False):
        return self.read_state()

    def initialize_outputs_off(self, *, guard, checkpoint=None):
        self.calls.append("initialize_outputs_off")
        guard()
        if checkpoint is not None:
            checkpoint()
        for row in self.state["axes"].values():
            row["output_enabled"] = False
        return deepcopy(self.state)

    def initialize_drive(self, *, guard=None, checkpoint=None):
        self.calls.append("initialize_drive")
        if guard is not None:
            guard()
        if checkpoint is not None:
            checkpoint()
        for axis in self.config.axes:
            self.state["axes"][axis.role].update(amplitude_v=axis.amplitude_v, frequency_hz=axis.frequency_hz)
        return deepcopy(self.state)

    def prepare_axis(self, role):
        self.calls.append(("prepare", role))
        self.state["axes"][role]["output_enabled"] = True

    def move_to(self, role, position):
        self.calls.append(("move", role, position))
        self.state["axes"][role]["position_um"] = position
        if self.move_error:
            raise IOError("target write result unknown")
        self.state["axes"][role]["moving"] = self.never_complete

    def stop(self, role):
        self.calls.append(("stop", role))
        if self.stop_error:
            raise IOError("stop failed")
        self.state["axes"][role]["moving"] = False

    def close(self):
        self.calls.append("close")
        if self.close_error:
            raise IOError("close failed")


class PositionerSessionTests(unittest.TestCase):
    def session(self, *, focus=False, relative=False, **options):
        document = positioner_document(focus=focus, mode="relative" if relative else "absolute")
        document["positioner"].update(options)
        self.config = load_positioner_config(document)
        self.motor, self.clock, self.events = FakePositioner(self.config), FakeClock(), []
        return PositionerPointSession(self.config, device_factory=lambda config: self.motor,
            event_sink=lambda name, payload: self.events.append((name, payload)),
            clock=self.clock, sleep=self.clock.sleep)

    def started(self, **options):
        session = self.session(**options)
        session.open()
        session.begin()
        return session

    def test_preflight_is_read_only_and_prepares_all_axes_after_stable_origin(self):
        session = self.session()
        self.assertEqual(self.motor.calls, [])
        session.open()
        self.assertFalse(any(isinstance(call, tuple) for call in self.motor.calls))
        self.assertNotIn("initialize_drive", self.motor.calls)
        session.begin()
        self.assertEqual([call for call in self.motor.calls if isinstance(call, tuple)],
                         [("prepare", "x"), ("prepare", "y"), ("prepare", "z")])
        self.assertGreaterEqual(self.clock.now, self.config.settle_s)
        self.assertEqual(session.origin, xyz(0, 0, 0))

    def test_absolute_xyz_move_then_full_window_and_fresh_verification(self):
        session = self.started()
        before = self.clock.now
        result = session.set_point(self.config.points[1])
        self.assertEqual(result["actual"], xyz(10, 10, 0))
        self.assertEqual(result["target"], xyz(10, 10, 0))
        self.assertEqual([call for call in self.motor.calls if isinstance(call, tuple) and call[0] == "move"],
                         [("move", "x", 10), ("move", "y", 10)])
        self.assertGreaterEqual(self.clock.now - before + 1e-12, 3 * self.config.settle_s)
        count = len(self.motor.calls)
        self.assertEqual(session.verify(), xyz(10, 10, 0))
        self.assertEqual(self.motor.calls[count:], ["read"])

    def test_drive_difference_is_initialized_only_after_full_stable_origin(self):
        session = self.session()
        self.motor.state["axes"]["x"].update(amplitude_v=1., frequency_hz=2.)
        initial = session.open()
        self.assertEqual(initial["axes"]["x"]["amplitude_v"], 1.)
        original = self.motor.initialize_drive
        def initialize(**kwargs):
            self.assertGreaterEqual(self.clock.now, self.config.settle_s)
            self.assertEqual(session.origin, xyz(0, 0, 0))
            self.assertFalse(any(isinstance(call, tuple) for call in self.motor.calls))
            return original(**kwargs)
        self.motor.initialize_drive = initialize
        session.begin()
        names = [name for name, payload in self.events]
        self.assertLess(names.index("positioner_origin_frozen"), names.index("positioner_drive_initialization_requested"))
        self.assertLess(names.index("positioner_drive_initialization_requested"), names.index("positioner_drive_initialized"))
        request = next(payload for name, payload in self.events if name == "positioner_drive_initialization_requested")
        self.assertEqual(request["actual"]["axes"]["x"]["amplitude_v"], 1.)
        self.assertEqual(request["requested"]["x"]["amplitude_v"], self.config.axes[0].amplitude_v)
        session.set_point(self.config.points[1])
        self.motor.state["axes"]["x"]["frequency_hz"] += 1
        with self.assertRaisesRegex(PositionerError, "amplitude/frequency"):
            session.verify()
        self.assertEqual(self.motor.calls.count("initialize_drive"), 1)

    def test_return_path_failure_prevents_drive_initialization(self):
        session = self.session(finish="return_start")
        session.config = replace(self.config, points=(xyz(5, 0, 0), xyz(10, 0, 0)),
            axes=tuple(replace(axis, max_move_um=6) for axis in self.config.axes))
        session.open()
        with self.assertRaisesRegex(PositionerError, "max_move"):
            session.begin()
        self.assertNotIn("initialize_drive", self.motor.calls)
        self.assertFalse(any(isinstance(call, tuple) for call in self.motor.calls))

    def test_drive_initialization_failure_is_not_retried_or_followed_by_output(self):
        session = self.session()
        session.open()
        def failed(**kwargs):
            self.motor.calls.append("initialize_drive")
            raise IOError("drive setting outcome unknown")
        self.motor.initialize_drive = failed
        with self.assertRaisesRegex(IOError, "outcome unknown"):
            session.begin()
        with self.assertRaises(PositionerError):
            session.begin()
        self.assertEqual(self.motor.calls.count("initialize_drive"), 1)
        self.assertFalse(any(isinstance(call, tuple) for call in self.motor.calls))
        cleanup = session.cleanup(failed=True)
        self.assertFalse(cleanup["verified"])
        self.assertTrue(cleanup["manual_review"])
        self.assertTrue(any("drive setting outcome unknown" in error for error in cleanup["errors"]))
        self.assertEqual(session.close(), [])

    def test_late_drive_initialization_cannot_enable_outputs(self):
        session = self.session()
        session.open()
        original = self.motor.initialize_drive
        def late(**kwargs):
            state = original(**kwargs)
            self.clock.now += self.config.move_timeout_s + 1
            return state
        self.motor.initialize_drive = late
        with self.assertRaisesRegex(PositionerError, "deadline"):
            session.begin()
        self.assertFalse(any(isinstance(call, tuple) for call in self.motor.calls))

    def test_initialization_return_must_be_strict_healthy_off_and_stationary(self):
        for key, value in (("output_enabled", True), ("moving", True),
                           ("sensor_error", True), ("frequency_hz", 123.)):
            with self.subTest(key=key):
                session = self.session()
                session.open()
                original = self.motor.initialize_drive
                def invalid(**kwargs):
                    state = original(**kwargs)
                    state["axes"]["z"][key] = value
                    return state
                self.motor.initialize_drive = invalid
                with self.assertRaises(PositionerError):
                    session.begin()
                self.assertFalse(any(isinstance(call, tuple) for call in self.motor.calls))

    def test_initialization_boundary_audit_failure_prevents_output(self):
        for event_type, expected_calls in (("positioner_drive_initialization_requested", 0),
                                           ("positioner_drive_initialized", 1)):
            with self.subTest(event_type=event_type):
                session = self.session()
                session.open()
                def unavailable(name, payload):
                    if name == event_type:
                        raise IOError("drive audit unavailable")
                session.event_sink = unavailable
                with self.assertRaisesRegex(IOError, "audit unavailable"):
                    session.begin()
                self.assertEqual(self.motor.calls.count("initialize_drive"), expected_calls)
                self.assertFalse(any(isinstance(call, tuple) for call in self.motor.calls))
                with self.assertRaises(PositionerError):
                    session.begin()

    def test_negative_initial_drive_cannot_be_treated_as_configurable_difference(self):
        for key in ("amplitude_v", "frequency_hz"):
            with self.subTest(key=key):
                session = self.session()
                self.motor.state["axes"]["x"][key] = -1.
                with self.assertRaisesRegex(PositionerError, "negative drive"):
                    session.open()
                self.assertNotIn("initialize_drive", self.motor.calls)
                self.assertEqual(self.motor.calls[-1], "close")

    def test_relative_points_use_frozen_origin_and_never_accumulate(self):
        session = self.session(relative=True)
        for role, value in zip(("x", "y", "z"), (20, 30, 5)):
            self.motor.state["axes"][role]["position_um"] = value
        session.open()
        session.begin()
        session.set_point(self.config.points[1])
        self.assertEqual(session.verify(), xyz(30, 40, 5))
        session.set_point(self.config.points[1])
        self.assertEqual(session.verify(), xyz(30, 40, 5))
        session.set_point(self.config.points[0])
        self.assertEqual(session.verify(), xyz(20, 30, 5))

    def test_begin_rejects_entire_path_before_first_prepare(self):
        session = self.session(relative=True)
        session.config = replace(self.config, axes=tuple(replace(axis, max_move_um=2) for axis in self.config.axes))
        session.open()
        with self.assertRaisesRegex(PositionerError, "max_move"):
            session.begin()
        self.assertNotIn("initialize_drive", self.motor.calls)
        self.assertFalse(any(isinstance(call, tuple) for call in self.motor.calls))

    def test_wrap_path_checked_before_output(self):
        session = self.session()
        session.config = replace(self.config, points=(xyz(0, 0, 0), xyz(5, 0, 0), xyz(10, 0, 0)),
               axes=tuple(replace(axis, max_move_um=6) for axis in self.config.axes))
        session.config.validate()
        session.open()
        with self.assertRaisesRegex(PositionerError, "max_move"):
            session.begin(wrap_required=True)
        self.assertNotIn("initialize_drive", self.motor.calls)
        self.assertFalse(any(isinstance(call, tuple) for call in self.motor.calls))

    def test_drifting_origin_never_prepares(self):
        session = self.session()
        self.motor.read_hook = lambda motor: motor.state["axes"]["x"].__setitem__("position_um", .2 if len(motor.calls) % 2 else 0)
        session.open()
        with self.assertRaisesRegex(PositionerError, "timeout"):
            session.begin()
        self.assertNotIn("initialize_drive", self.motor.calls)
        self.assertFalse(any(isinstance(call, tuple) for call in self.motor.calls))

    def test_missing_sensor_or_unknown_bool_rejected_and_closed(self):
        session = self.session()
        self.motor.state["axes"]["y"]["sensor_connected"] = None
        with self.assertRaisesRegex(PositionerError, "boolean"):
            session.open()
        self.assertEqual(self.motor.calls[-1], "close")

    def test_guard_failure_before_prepare_has_no_motion_and_cannot_restart(self):
        session = self.session(external_light_off_confirmed=False)
        session.open()
        with self.assertRaisesRegex(PositionerError, "OFF"):
            session.begin()
        self.assertFalse(any(isinstance(call, tuple) for call in self.motor.calls))
        with self.assertRaises(PositionerError):
            session.begin(lambda: None)

    def test_unknown_target_write_stops_once_and_prevents_reissue(self):
        session = self.started()
        self.motor.move_error = True
        with self.assertRaisesRegex(IOError, "unknown"):
            session.set_point(self.config.points[1])
        with self.assertRaises(PositionerError):
            session.set_point(self.config.points[1])
        cleanup = session.cleanup(failed=True)
        self.assertFalse(cleanup["verified"])
        stops = [call for call in self.motor.calls if isinstance(call, tuple) and call[0] == "stop"]
        self.assertEqual(set(stops), {("stop", "x"), ("stop", "y"), ("stop", "z")})
        self.assertEqual(len(stops), 3)
        self.assertEqual(sum(isinstance(call, tuple) and call[0] == "move" for call in self.motor.calls), 1)

    def test_motion_timeout_stops_and_reports_actual_last_confirmed(self):
        session = self.started()
        self.motor.never_complete = True
        with self.assertRaisesRegex(PositionerError, "timeout"):
            session.set_point(self.config.points[1])
        result = session.cleanup(failed=True)
        self.assertFalse(result["verified"])
        self.assertEqual(result["last_confirmed"]["axes"]["x"]["position_um"], 10)
        self.assertTrue(any("timeout" in error for error in result["errors"]))

    def test_guard_io_time_counts_toward_shared_deadline(self):
        session = self.started()
        def late_guard():
            self.clock.now += 3.1
        with self.assertRaisesRegex(PositionerError, "deadline"):
            session.set_point(self.config.points[1], late_guard)
        self.assertFalse(any(isinstance(call, tuple) and call[0] == "move" for call in self.motor.calls))

    def test_audit_failure_during_motion_does_not_block_stop_close(self):
        session = self.started()
        def sink(name, payload):
            if name == "positioner_state":
                raise IOError("disk full")
        self.motor.move_error = True
        # Move-start remains durable; failed post-stop state audits are independent.
        session.event_sink = lambda name, payload: sink(name, payload) if any(
            isinstance(call, tuple) and call[0] == "move" for call in self.motor.calls) else None
        with self.assertRaises(IOError):
            session.set_point(self.config.points[1])
        result = session.cleanup(failed=True)
        self.assertFalse(result["verified"])
        self.assertEqual(session.close(), [])
        self.assertEqual(sum(call == ("stop", "x") for call in self.motor.calls), 1)
        self.assertEqual(self.motor.calls[-1], "close")

    def test_primary_stop_and_close_errors_remain_separate(self):
        session = self.started()
        self.motor.move_error = self.motor.stop_error = self.motor.close_error = True
        with self.assertRaises(IOError):
            session.set_point(self.config.points[1])
        result = session.cleanup(failed=True)
        close_errors = session.close()
        self.assertTrue(any("primary" in error for error in result["errors"]))
        self.assertTrue(any("stop unverified" in error for error in result["errors"]))
        self.assertTrue(any("disconnect" in error for error in close_errors))

    def test_communication_failure_records_unknown_not_zero(self):
        session = self.started()
        session.set_point(self.config.points[1])
        self.motor.read_error = True
        with self.assertRaises(IOError):
            session.verify()
        result = session.cleanup(failed=True)
        self.assertTrue(result["position_unknown"])
        self.assertFalse(result["state_current"])
        self.assertEqual(result["last_confirmed"]["axes"]["x"]["position_um"], 10)

    def test_verify_drift_is_rejected_without_new_movement(self):
        session = self.started()
        session.set_point(self.config.points[1])
        before = len(self.motor.calls)
        self.motor.state["axes"]["y"]["position_um"] += .2
        with self.assertRaisesRegex(PositionerError, "drifted"):
            session.verify()
        self.assertEqual(self.motor.calls[before:], ["read"])

    def test_focus_plane_computes_z_and_preserves_calibration_id(self):
        session = self.started(focus=True)
        result = session.set_point(self.config.points[1])
        self.assertAlmostEqual(result["actual"]["positioner_z_um"], 10.1)
        self.assertAlmostEqual(result["focus_error_um"], 0.)
        self.assertEqual(result["focus_plane_id"], self.config.focus_plane.calibration_sha256)

    def test_focus_error_uses_actual_xy_and_rejects_within_axis_tolerance(self):
        session = self.started(focus=True)
        session.config = replace(session.config, focus_tolerance_um=.0001)
        session.set_point(self.config.points[1])
        self.motor.state["axes"]["x"]["position_um"] += .05
        with self.assertRaisesRegex(PositionerError, "drifted"):
            session.verify()

    def test_hold_cleanup_never_returns_or_disables_output(self):
        session = self.started()
        session.set_point(self.config.points[1])
        before = len(self.motor.calls)
        result = session.cleanup()
        self.assertTrue(result["verified"])
        self.assertEqual(self.motor.calls[before:], ["read"])
        self.assertEqual(session.close(), [])

    def test_return_start_only_on_success_with_guard_and_verified_position(self):
        session = self.started(finish="return_start")
        session.set_point(self.config.points[1])
        result = session.cleanup(guard=lambda: None)
        self.assertTrue(result["verified"])
        self.assertEqual(session._actual(result["states"]), xyz(0, 0, 0))

    def test_failed_cleanup_never_returns_start(self):
        session = self.started(finish="return_start")
        session.set_point(self.config.points[1])
        before = len([call for call in self.motor.calls if isinstance(call, tuple) and call[0] == "move"])
        session.cleanup(failed=True)
        self.assertEqual(before, len([call for call in self.motor.calls if isinstance(call, tuple) and call[0] == "move"]))

    def test_disconnect_returned_errors_are_preserved(self):
        session = self.started()
        self.motor.close = lambda: ["SDK disconnect failed"]
        self.assertEqual(session.close(), ["Positioner disconnect: SDK disconnect failed"])
        self.assertEqual(session.close(), ["Positioner disconnect: SDK disconnect failed"])

    def test_preflight_primary_and_close_failure_remain_in_cleanup_evidence(self):
        session = self.session()
        self.motor.state["axes"]["y"]["sensor_error"] = True
        self.motor.close_error = True
        with self.assertRaisesRegex(PositionerError, "sensor/EOT"):
            session.open()
        result = session.cleanup(failed=True)
        self.assertTrue(any("disconnect failed" in error for error in result["errors"]))
        self.assertTrue(any("disconnect failed" in error for error in session.close()))
        self.assertEqual(self.motor.calls.count("close"), 1)

    def test_enabled_outputs_are_read_only_until_guarded_begin_then_origin_is_frozen(self):
        session = self.session(relative=True)
        self.motor.state["axes"]["x"].update(output_enabled=True, position_um=5.)
        session.open()
        self.assertNotIn("initialize_outputs_off", self.motor.calls)
        self.assertTrue(self.motor.state["axes"]["x"]["output_enabled"])
        original = self.motor.initialize_outputs_off
        def release(**kwargs):
            self.assertIsNone(session.origin)
            result = original(**kwargs)
            self.motor.state["axes"]["x"]["position_um"] = 2.
            return result
        self.motor.initialize_outputs_off = release
        session.begin()
        self.assertEqual(session.origin, xyz(2, 0, 0))
        self.assertLess(self.motor.calls.index("initialize_outputs_off"), self.motor.calls.index("initialize_drive"))
        events = [name for name, payload in self.events]
        self.assertLess(events.index("positioner_outputs_off"), events.index("positioner_origin_frozen"))

    def test_missing_dark_guard_prevents_startup_output_release(self):
        session = self.session(external_light_off_confirmed=False)
        self.motor.state["axes"]["x"]["output_enabled"] = True
        session.open()
        with self.assertRaisesRegex(PositionerError, "optical OFF guard"):
            session.begin()
        self.assertTrue(self.motor.state["axes"]["x"]["output_enabled"])
        self.assertNotIn("initialize_drive", self.motor.calls)
        self.assertFalse(any(isinstance(call, tuple) for call in self.motor.calls))

    def test_output_release_failure_is_retained_and_never_prepares_or_retries(self):
        session = self.session()
        session.open()
        def fail(**kwargs):
            self.motor.calls.append("initialize_outputs_off")
            raise IOError("output OFF result unknown")
        self.motor.initialize_outputs_off = fail
        with self.assertRaisesRegex(IOError, "OFF result unknown"):
            session.begin()
        with self.assertRaises(PositionerError):
            session.begin()
        self.assertEqual(self.motor.calls.count("initialize_outputs_off"), 1)
        self.assertNotIn("initialize_drive", self.motor.calls)
        result = session.cleanup(failed=True)
        self.assertTrue(result["manual_review"])
        self.assertTrue(any("OFF result unknown" in error for error in result["errors"]))

    def test_output_release_return_must_be_off_healthy_stationary_and_complete(self):
        for field, value in (("output_enabled", True), ("moving", True),
                             ("sensor_error", True), ("position_um", float("nan"))):
            with self.subTest(field=field):
                session = self.session()
                session.open()
                original = self.motor.initialize_outputs_off
                def invalid(**kwargs):
                    state = original(**kwargs)
                    state["axes"]["z"][field] = value
                    return state
                self.motor.initialize_outputs_off = invalid
                with self.assertRaises(PositionerError):
                    session.begin()
                self.assertIsNone(session.origin)
                self.assertNotIn("initialize_drive", self.motor.calls)
                self.assertFalse(any(isinstance(call, tuple) for call in self.motor.calls))

    def test_output_off_boundary_audit_failure_cannot_continue_to_drive_or_motion(self):
        for event, expected in (("positioner_outputs_off_requested", 0), ("positioner_outputs_off", 1)):
            with self.subTest(event=event):
                session = self.session()
                session.open()
                def failed(name, payload):
                    if name == event:
                        raise IOError("output audit unavailable")
                session.event_sink = failed
                with self.assertRaisesRegex(IOError, "output audit unavailable"):
                    session.begin()
                self.assertEqual(self.motor.calls.count("initialize_outputs_off"), expected)
                self.assertIsNone(session.origin)
                self.assertNotIn("initialize_drive", self.motor.calls)

    def test_invalid_path_after_release_never_initializes_drive_or_enables_outputs(self):
        session = self.session(relative=True)
        self.motor.state["axes"]["x"]["output_enabled"] = True
        session.config = replace(self.config, axes=tuple(replace(axis, max_move_um=2) for axis in self.config.axes))
        session.open()
        with self.assertRaisesRegex(PositionerError, "max_move"):
            session.begin()
        self.assertEqual(self.motor.calls.count("initialize_outputs_off"), 1)
        self.assertNotIn("initialize_drive", self.motor.calls)
        self.assertFalse(any(isinstance(call, tuple) for call in self.motor.calls))
        self.assertTrue(all(not row["output_enabled"] for row in self.motor.state["axes"].values()))

    def test_prepare_target_failure_triggers_owned_stop_without_retry(self):
        session = self.session()
        session.open()
        def fail_prepare(role):
            self.motor.calls.append(("prepare", role))
            raise IOError("preload target result unknown")
        self.motor.prepare_axis = fail_prepare
        with self.assertRaisesRegex(IOError, "preload"):
            session.begin()
        self.assertEqual([call for call in self.motor.calls if isinstance(call, tuple)],
                         [("prepare", "x"), ("stop", "x")])
        session.cleanup(failed=True)
        self.assertEqual(sum(call == ("stop", "x") for call in self.motor.calls), 1)

    def test_late_read_cannot_be_promoted_as_qualified_position(self):
        session = self.started()
        def slow_read(motor):
            if any(isinstance(call, tuple) and call[0] == "move" for call in motor.calls):
                self.clock.now += 3.1
        self.motor.read_hook = slow_read
        with self.assertRaisesRegex(PositionerError, "deadline"):
            session.set_point(self.config.points[1])
        self.assertIsNone(session.targets)

    def test_guard_failure_after_motion_stops_without_closing_output(self):
        session = self.started()
        def guard():
            if any(isinstance(call, tuple) and call[0] == "move" for call in self.motor.calls):
                raise RuntimeError("laser OFF evidence lost")
        with self.assertRaisesRegex(RuntimeError, "OFF evidence"):
            session.set_point(self.config.points[1], guard)
        self.assertTrue(all(row["output_enabled"] for row in self.motor.state["axes"].values()))
        self.assertEqual(sum(call == ("stop", "x") for call in self.motor.calls), 1)

    def test_normal_cleanup_does_not_certify_lost_hold_or_last_target(self):
        session = self.started()
        session.set_point(self.config.points[1])
        self.motor.state["axes"]["x"]["output_enabled"] = False
        result = session.cleanup()
        self.assertFalse(result["verified"])
        self.assertTrue(any("holding output" in error for error in result["errors"]))
        self.motor.state["axes"]["x"]["output_enabled"] = True
        self.motor.state["axes"]["x"]["position_um"] += .2
        result = session.cleanup()
        self.assertFalse(result["verified"])
        self.assertTrue(any("qualification was lost" in error for error in result["errors"]))
