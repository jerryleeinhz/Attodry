from copy import deepcopy
import unittest
from unittest.mock import patch

from attodry_control.rotator_config import RotatorError, load_rotator_config
from attodry_control.rotator_points import RotatorPointSession


class FakeClock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def __call__(self):
        return self.now

    def sleep(self, value):
        self.waits.append(value)
        self.now += value


class FakeMotor:
    def __init__(self, config, *, homed=True):
        self.config = config
        self.calls = []
        self.calibrated = False
        self.close_error = False
        self.never_complete = False
        self.pending = None
        self.read_hook = None
        self.state = dict(serial_number=config.serial_number, controller_model="KDC101",
                          stage_model="PRM1/MZ8", angle_deg=0.0, homed=homed,
                          moving=False, enabled=True, fault=False)

    def open(self):
        self.calls.append("open")

    def prepare(self):
        self.calls.append("prepare")
        self.calibrated = True

    def home(self):
        self.calls.append("home")
        self.pending = (0.0, 2)
        self.state["moving"] = True

    def move_to(self, angle):
        self.calls.append(("move", angle))
        self.pending = (angle, 2)
        self.state["moving"] = True

    def read_state(self):
        self.calls.append("read")
        if self.pending is not None and not self.never_complete:
            angle, count = self.pending
            self.pending = (angle, count - 1)
            if count <= 0:
                self.state.update(angle_deg=angle, homed=True, moving=False)
                self.pending = None
        if self.read_hook:
            self.read_hook(self)
        result = deepcopy(self.state)
        result["unit_calibration_verified"] = self.calibrated
        if not self.calibrated:
            result["angle_deg"] = None
        return result

    def stop(self):
        self.calls.append("stop")
        self.pending = None
        self.state["moving"] = False

    def close(self):
        self.calls.append("close")
        if self.close_error:
            raise RuntimeError("cannot close")


def config(two=False, **options):
    raw = {"rotator_1": dict(enabled=True, serial_number="27000001", settle_s=0.2,
                               poll_interval_s=0.1, move_timeout_s=2, home_timeout_s=2,
                               **options),
           "rotator_2": {"enabled": False},
           "rotator_scan": {"rotator_1_angles_deg": [0, 90, 360]}}
    if two:
        raw["rotator_2"] = dict(raw["rotator_1"], serial_number="27000002")
        raw["rotator_scan"]["rotator_2_angles_deg"] = [0]
    return load_rotator_config(raw)


class RotatorSessionTests(unittest.TestCase):
    def session(self, two=False, *, homed=True, **options):
        cfg = config(two, **options)
        self.clock, self.motors, self.events = FakeClock(), {}, []

        def factory(device_config):
            motor = FakeMotor(device_config, homed=homed)
            self.motors[device_config.role] = motor
            return motor

        session = RotatorPointSession(cfg, device_factory=factory,
                                     event_sink=lambda name, payload: self.events.append((name, payload)),
                                     clock=self.clock, sleep=self.clock.sleep)
        return session

    def started(self, **options):
        result = self.session(**options)
        result.preflight()
        result.begin()
        return result

    def test_construction_and_disabled_slot_do_not_open_anything(self):
        session = self.session()
        self.assertEqual(self.motors, {})
        session.preflight()
        self.assertEqual(tuple(self.motors), ("rotator_1",))
        self.assertEqual(self.motors["rotator_1"].calls, ["open", "read"])

    def test_default_driver_raw_transport_audit_streams_to_session_event_sink(self):
        events, callbacks = [], []

        def build(device_config, *, on_event):
            callbacks.append(on_event)
            return FakeMotor(device_config)

        session = RotatorPointSession(config(), event_sink=lambda kind, data: events.append((kind, data)))
        with patch("attodry_control.thorlabs_kdc101.Kdc101Device", side_effect=build):
            session.preflight()
        self.assertEqual(len(callbacks), 1)
        raw = {"role": "rotator_1", "utc": "2026-10-19T12:00:00Z",
               "command_hex": "050000005001", "response_hex": "060000005001",
               "error": "example retained transport error"}
        callbacks[0](raw)
        self.assertEqual(events[-1], ("rotator_raw", raw))
        raw["command_hex"] = "changed later"
        self.assertEqual(events[-1][1]["command_hex"], "050000005001")

    def test_all_identities_preflight_before_any_prepare(self):
        session = self.session(two=True)
        session.preflight()
        for motor in self.motors.values():
            self.assertNotIn("prepare", motor.calls)
        self.motors["rotator_2"].state["serial_number"] = "27000099"
        with self.assertRaisesRegex(RotatorError, "identity"):
            session.begin()
        for motor in self.motors.values():
            self.assertNotIn("prepare", motor.calls)

    def test_initial_unconverted_position_is_not_measurement_and_finite_after_prepare(self):
        session = self.session()
        states = session.preflight()
        self.assertIsNone(states["rotator_1"]["angle_deg"])
        motor = self.motors["rotator_1"]
        motor.state["angle_deg"] = float("nan")
        with self.assertRaisesRegex(RotatorError, "finite"):
            session.begin()

    def test_home_if_needed_once_then_moves_to_absolute_360(self):
        session = self.started(homed=False)
        motor = self.motors["rotator_1"]
        self.assertEqual(motor.calls.count("home"), 1)
        self.assertGreaterEqual(self.clock.now, 0.2)
        result = session.set_point({"rotator_1_angle_deg": 360})
        self.assertTrue(result["changed"])
        self.assertEqual(result["actual"], {"rotator_1_angle_deg": 360})
        self.assertEqual(motor.calls.count("home"), 1)

    def test_always_home_and_require_homed_no_motion(self):
        session = self.started(home_policy="always")
        self.assertEqual(self.motors["rotator_1"].calls.count("home"), 1)
        session = self.session(homed=False, home_policy="require_homed")
        with self.assertRaisesRegex(RotatorError, "Home"):
            session.preflight()
        self.assertNotIn("prepare", self.motors["rotator_1"].calls)

    def test_second_device_starts_moving_during_first_home_and_is_not_prepared(self):
        session = self.session(two=True, homed=False)
        session.preflight()
        first, second = self.motors["rotator_1"], self.motors["rotator_2"]
        original_home = first.home

        def home_and_upset_second():
            original_home()
            second.state["moving"] = True

        first.home = home_and_upset_second
        with self.assertRaisesRegex(RotatorError, "unexpected motion before prepare"):
            session.begin()
        self.assertEqual(first.calls.count("home"), 1)
        self.assertNotIn("prepare", second.calls)
        self.assertNotIn("home", second.calls)

    def test_same_coordinate_skips_move_and_settle_but_freshly_reads(self):
        session = self.started()
        first = session.set_point({"rotator_1_angle_deg": 90})
        before = self.clock.now
        motor = self.motors["rotator_1"]
        reads = motor.calls.count("read")
        second = session.set_point({"rotator_1_angle_deg": 90})
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        self.assertEqual(before, self.clock.now)
        self.assertEqual(motor.calls.count(("move", 90)), 1)
        self.assertGreater(motor.calls.count("read"), reads)

    def test_initial_same_position_waits_only_remaining_stability_time(self):
        session = self.started()
        self.clock.now += 0.15
        result = session.set_point({"rotator_1_angle_deg": 0})
        self.assertFalse(result["changed"])
        self.assertAlmostEqual(self.clock.now, 0.2)
        self.assertAlmostEqual(sum(self.clock.waits), 0.05)

    def test_second_device_is_freshly_verified_after_first_device_move(self):
        session = self.started(two=True)
        first, second = self.motors["rotator_1"], self.motors["rotator_2"]

        def upset_second(motor):
            if motor.pending is not None:
                second.state["fault"] = True

        first.read_hook = upset_second
        with self.assertRaisesRegex(RotatorError, "fault"):
            session.set_point({"rotator_1_angle_deg": 90, "rotator_2_angle_deg": 180})
        self.assertNotIn(("move", 180), second.calls)

    def test_guard_before_preparation_home_move_and_during_wait(self):
        session = self.session(homed=False)
        session.preflight()
        guards = []
        session.begin(guard=lambda: guards.append(self.clock.now))
        home_guards = len(guards)
        session.set_point({"rotator_1_angle_deg": 90}, guard=lambda: guards.append(self.clock.now))
        self.assertGreater(home_guards, 4)
        self.assertGreater(len(guards) - home_guards, 4)
        self.assertTrue(all(a <= b for a, b in zip(guards, guards[1:])))

    def test_guard_failure_never_moves_and_keeps_previous_angle(self):
        session = self.started()

        def guard():
            raise RuntimeError("OFF not confirmed")

        with self.assertRaisesRegex(RuntimeError, "OFF"):
            session.set_point({"rotator_1_angle_deg": 90}, guard=guard)
        self.assertNotIn(("move", 90), self.motors["rotator_1"].calls)

    def test_complete_grid_validation_before_first_motion(self):
        session = self.started(two=True)
        for values in ({"rotator_1_angle_deg": 90},
                       {"rotator_1_angle_deg": 90, "rotator_2_angle_deg": 361},
                       {"rotator_1_angle_deg": 90, "rotator_2_angle_deg": True}):
            with self.subTest(values=values), self.assertRaises(RotatorError):
                session.set_point(values)
        self.assertFalse(any(isinstance(call, tuple) for motor in self.motors.values() for call in motor.calls))

    def test_motion_timeout_preserves_last_confirmed_and_cleanup_stops(self):
        session = self.started()
        motor = self.motors["rotator_1"]
        motor.never_complete = True
        with self.assertRaisesRegex(RotatorError, "timeout"):
            session.set_point({"rotator_1_angle_deg": 90})
        self.assertEqual(motor.calls.count(("move", 90)), 1)
        self.assertFalse(session.last_confirmed["rotator_1"]["moving"])
        self.assertEqual(motor.calls.count("stop"), 1)
        result = session.cleanup(failed=True)
        self.assertFalse(result["verified"])
        self.assertEqual(motor.calls.count("stop"), 1)
        self.assertEqual(motor.calls.count("home"), 0)
        self.assertEqual(result["states"]["rotator_1"]["angle_deg"], 0)

    def test_new_fault_is_audited_and_never_retried(self):
        session = self.started()
        motor = self.motors["rotator_1"]
        motor.read_hook = lambda obj: obj.state.update(fault=True) if obj.pending else None
        with self.assertRaisesRegex(RotatorError, "fault"):
            session.set_point({"rotator_1_angle_deg": 90})
        self.assertEqual(motor.calls.count(("move", 90)), 1)
        self.assertTrue(any(event[1].get("state", {}).get("fault") for event in self.events))
        self.assertFalse(session.cleanup(failed=True)["verified"])

    def test_unexpected_motion_lost_home_and_unknown_status_block_formal_verification(self):
        for changes in ({"moving": True}, {"homed": False}, {"enabled": False},
                        {"fault": None}, {"angle_deg": 361}):
            with self.subTest(changes=changes):
                session = self.started()
                self.motors["rotator_1"].state.update(changes)
                with self.assertRaises(RotatorError):
                    session.verify()

    def test_actual_uses_readback_and_verify_rejects_drift(self):
        session = self.started()
        session.set_point({"rotator_1_angle_deg": 90})
        self.motors["rotator_1"].state["angle_deg"] = 91
        self.assertEqual(session.actual(), {"rotator_1_angle_deg": 91})
        with self.assertRaisesRegex(RotatorError, "drifted"):
            session.verify()

    def test_cleanup_preserves_and_close_all_independently(self):
        session = self.started(two=True)
        session.set_point({"rotator_1_angle_deg": 90, "rotator_2_angle_deg": 180})
        result = session.cleanup()
        self.assertTrue(result["verified"])
        self.assertEqual(result["states"]["rotator_1"]["angle_deg"], 90)
        self.assertEqual(result["states"]["rotator_2"]["angle_deg"], 180)
        self.assertTrue(all("stop" not in motor.calls for motor in self.motors.values()))
        self.motors["rotator_2"].close_error = True
        errors = session.close()
        self.assertEqual(len(errors), 1)
        self.assertTrue(all("close" in motor.calls for motor in self.motors.values()))

    def test_cleanup_lost_home_reference_is_unverified_and_never_automatically_homes(self):
        session = self.started()
        motor = self.motors["rotator_1"]
        motor.state["homed"] = False
        result = session.cleanup(failed=True)
        self.assertFalse(result["verified"])
        self.assertFalse(result["states"]["rotator_1"]["homed"])
        self.assertTrue(any("Home reference" in error for error in result["errors"]))
        self.assertNotIn("home", motor.calls)
        self.assertNotIn("stop", motor.calls)

    def test_cleanup_connection_failure_keeps_last_confirmed_and_still_other_device(self):
        session = self.started(two=True)
        prior = deepcopy(session.last_confirmed["rotator_1"])

        def fail():
            raise RuntimeError("communication lost")

        self.motors["rotator_1"].read_state = fail
        result = session.cleanup(failed=True)
        self.assertFalse(result["verified"])
        self.assertIn("rotator_2", result["states"])
        self.assertEqual(result["last_confirmed"]["rotator_1"], prior)

    def test_closed_or_failed_session_cannot_reopen_and_reuse_stability(self):
        session = self.started()
        session.close()
        with self.assertRaisesRegex(RotatorError, "once"):
            session.preflight()

    def test_io_finishing_after_deadline_cannot_promote_ready_state(self):
        session = self.started()
        motor = self.motors["rotator_1"]
        original = motor.read_state

        def slow():
            state = original()
            if motor.pending is not None:
                self.clock.now += 3
            return state

        motor.read_state = slow
        with self.assertRaisesRegex(RotatorError, "timeout"):
            session.set_point({"rotator_1_angle_deg": 90})

    def test_lost_communication_during_owned_motion_attempts_one_stop_and_retains_unknown(self):
        session = self.started()
        motor = self.motors["rotator_1"]
        original = motor.read_state

        def lose():
            if motor.pending is not None:
                raise RuntimeError("communication uncertain")
            return original()

        def stop_fail():
            motor.calls.append("stop")
            raise RuntimeError("stop acknowledgement missing")

        motor.read_state, motor.stop = lose, stop_fail
        with self.assertRaisesRegex(RuntimeError, "uncertain"):
            session.set_point({"rotator_1_angle_deg": 90})
        self.assertEqual(motor.calls.count("stop"), 1)
        result = session.cleanup(failed=True)
        self.assertFalse(result["verified"])
        self.assertEqual(motor.calls.count("stop"), 1)
        self.assertTrue(any("stop not verified" in error for error in result["errors"]))

    def test_final_guard_angle_change_must_not_promote_stale_ready_read(self):
        session = self.started()
        motor = self.motors["rotator_1"]

        def guard():
            if self.clock.now >= 0.4:
                motor.state["angle_deg"] = 91

        with self.assertRaisesRegex(RotatorError, "timeout"):
            session.set_point({"rotator_1_angle_deg": 90}, guard=guard)
        self.assertEqual(motor.calls.count("stop"), 1)
        self.assertFalse(session.cleanup(failed=True)["verified"])


if __name__ == "__main__":
    unittest.main()
