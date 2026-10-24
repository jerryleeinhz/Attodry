"""SDK-arrival / output-OFF policy through the production device and session."""
from contextlib import nullcontext
from copy import deepcopy
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from attodry_control.anc350 import Anc350Device, Anc350Error
from attodry_control.positioner_config import load_positioner_config, PositionerError
from attodry_control.positioner_points import PositionerPointSession
from tests.test_anc350 import FakeAnc350, pe_bytes
from tests.test_positioner_config import positioner_document
from tests.test_positioner_points import FakeClock


class ArrivalSDK(FakeAnc350):
    def __init__(self, config):
        super().__init__(config)
        self.reach = True
        self.target = dict.fromkeys(range(3), False)
        self.sticky_moving = False
        self.after_off_offset_um = 0.
        self.ignore_off = False
        self.stopped = set()

    def __getattr__(self, name):
        base = super().__getattr__(name)
        def function(*args):
            result = base(*args)
            if name == "ANC_startAutoMove":
                index = args[1]
                if args[2]:
                    self.target[index] = self.reach
                    self.axes[index]["moving"] = 1
                else:
                    self.stopped.add(index)
            elif name == "ANC_setAxisOutput" and args[2] == 0:
                index = args[1]
                if self.ignore_off:
                    self.axes[index]["enabled"] = 1
                self.axes[index]["position"] += self.after_off_offset_um * 1e-6
                self.target[index] = False
            elif name == "ANC_getAxisStatus":
                index = args[1]
                args[5]._obj.value = int(self.target[index])
                if self.sticky_moving and index in self.stopped:
                    args[4]._obj.value = 1
            return result
        return function


class TargetOutputOffTests(unittest.TestCase):
    def setUp(self):
        unknown = patch("attodry_control.anc350._SDK_CONNECTION_UNKNOWN", False)
        unknown.start()
        self.addCleanup(unknown.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / "fixture.dll"
        path.write_bytes(pe_bytes())
        self.document = positioner_document()
        self.document["positioner"].update(
            dll_path=str(path), dll_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
            arrival_policy="target_reached_output_off")
        self.config = load_positioner_config(self.document)
        self.sdk = ArrivalSDK(self.config)
        self.clock, self.events = FakeClock(), []
        self.device = Anc350Device(self.config, dll_factory=lambda _: self.sdk,
            lease_factory=lambda _: nullcontext(),
            on_event=lambda payload: self.events.append(("native", payload)))
        self.session = PositionerPointSession(self.config, device_factory=lambda _: self.device,
            clock=self.clock, sleep=self.clock.sleep,
            event_sink=lambda name, payload: self.events.append((name, payload)))
        self.addCleanup(self.session.close)

    def begin(self):
        self.session.preflight()
        self.session.begin()

    def count(self, name, axis=None, flag=None):
        return sum(call[0] == name and (axis is None or call[2] == axis)
                   and (flag is None or call[3] == flag) for call in self.sdk.calls)

    def point(self, index=0):
        return self.session.set_point(self.config.points[index])

    def test_default_and_snapshot_and_invalid_policy(self):
        default = load_positioner_config(positioner_document())
        self.assertEqual(default.arrival_policy, "stable_hold")
        self.assertEqual(default.snapshot()["arrival_policy"], "stable_hold")
        self.assertEqual(self.config.snapshot()["arrival_policy"], "target_reached_output_off")
        for value in ("", "unknown", False, 0, [], None):
            doc = deepcopy(self.document)
            doc["positioner"]["arrival_policy"] = value
            with self.subTest(value=value), self.assertRaises(PositionerError):
                load_positioner_config(doc)

    def test_focus_plane_cannot_claim_output_off_qualification(self):
        doc = positioner_document(focus=True)
        doc["positioner"]["arrival_policy"] = "target_reached_output_off"
        with self.assertRaisesRegex(PositionerError, "manual XYZ"):
            load_positioner_config(doc)

    def test_archive_reference_context_separates_arrival_policies(self):
        from attodry_control.combination_analysis import positioner_reference_context
        from tests.test_positioner_analysis import attach_position
        from tests.test_optical_power_reference import make_rows
        record = attach_position(make_rows())[0]
        legacy = positioner_reference_context(record)
        record["status.positioner"].update(arrival_policy="stable_hold")
        self.assertEqual(positioner_reference_context(record), legacy)
        record["status.positioner"].update(arrival_policy="target_reached_output_off", z_mode="manual")
        self.assertNotEqual(positioner_reference_context(record), legacy)
        self.assertEqual(positioner_reference_context(record)["arrival_policy"], "target_reached_output_off")
        record["status.positioner"]["arrival_policy"] = "unknown"
        with self.assertRaisesRegex(ValueError, "arrival policy"):
            positioner_reference_context(record)

    def test_moving_target_true_releases_each_axis_without_post_arrival_dwell(self):
        self.begin()
        start = self.clock()
        result = self.point()
        self.assertEqual(self.clock(), start)
        self.assertTrue(result["changed"])
        self.assertEqual(set(result["arrival_evidence"]), set("xyz"))
        for index, role in enumerate("xyz"):
            self.assertEqual(self.count("ANC_startAutoMove", index, 1), 1)
            self.assertEqual(self.count("ANC_startAutoMove", index, 0), 1)
            self.assertEqual(self.count("ANC_setAxisOutput", index, 0), 1)
            proof = result["arrival_evidence"][role]
            self.assertTrue(proof["sdk_target_reached"] and proof["output_off_confirmed"])
            self.assertTrue(proof["arrival_captured_at_utc"] and proof["output_off_captured_at_utc"])
        arrivals = [p for n, p in self.events if n == "positioner_target_reached"]
        self.assertEqual(len(arrivals), 3)
        self.assertTrue(all(p["state"]["axes"][p["role"]]["moving"] for p in arrivals))
        self.assertTrue(self.session.cleanup()["verified"])

    def test_post_arrival_noise_and_lost_target_bit_remain_actual_not_rejected(self):
        self.begin()
        self.sdk.sticky_moving = True
        self.sdk.after_off_offset_um = 2.  # 20 times the configured tolerance.
        result = self.point()
        self.assertEqual(result["actual"], dict(positioner_x_um=2., positioner_y_um=2., positioner_z_um=2.))
        state = self.session.observe()
        self.assertTrue(all(r["moving"] and not r["output_enabled"] and not r["target_reached"]
                            for r in state["axes"].values()))
        self.assertEqual(self.session.verify(), result["actual"])
        self.assertEqual(self.session.actual(), result["actual"])
        self.assertTrue(self.session.cleanup()["verified"])

    def test_duplicate_target_does_not_chase_sensor_noise_and_changed_axis_rearms(self):
        self.begin()
        self.sdk.after_off_offset_um = 2.
        self.point()
        before = len(self.sdk.calls)
        self.assertFalse(self.point()["changed"])
        self.assertFalse(any(c[0].startswith(("ANC_set", "ANC_start")) for c in self.sdk.calls[before:]))
        result = self.point(1)
        self.assertTrue(result["changed"])
        self.assertEqual(self.count("ANC_startAutoMove", 0, 1), 2)
        self.assertEqual(self.count("ANC_startAutoMove", 1, 1), 2)
        self.assertEqual(self.count("ANC_startAutoMove", 2, 1), 1)
        self.assertTrue(self.session.cleanup()["verified"])

    def test_target_false_even_at_exact_position_times_out_then_stops_off_once(self):
        self.begin()
        self.sdk.reach = False
        with self.assertRaisesRegex(PositionerError, "timeout|deadline"):
            self.point()
        self.assertEqual(self.count("ANC_startAutoMove", 0, 1), 1)
        self.assertEqual(self.count("ANC_startAutoMove", 0, 0), 1)
        self.assertEqual(self.count("ANC_setAxisOutput", 0, 0), 1)
        self.assertEqual(self.count("ANC_startAutoMove", 1, 1), 0)
        self.session.cleanup(failed=True)
        self.assertEqual(self.count("ANC_setAxisOutput", 0, 0), 1)

    def test_off_ack_without_off_readback_fails_without_reissuing(self):
        self.begin()
        self.sdk.ignore_off = True
        with self.assertRaisesRegex(PositionerError, "OFF"):
            self.point()
        cleanup = self.session.cleanup(failed=True)
        self.assertFalse(cleanup["verified"])
        self.assertTrue(cleanup["manual_review"])
        self.assertEqual(self.count("ANC_setAxisOutput", 0, 0), 1)
        self.assertEqual(self.count("ANC_startAutoMove", 0, 0), 1)

    def test_stop_communication_failure_still_attempts_off_once(self):
        self.begin()
        original = self.sdk.hook
        def fail_stop(name, args):
            if name == "ANC_startAutoMove" and args[2] == 0:
                raise OSError("stop reply lost")
            if original:
                original(name, args)
        self.sdk.hook = fail_stop
        with self.assertRaisesRegex(Anc350Error, "stop reply lost"):
            self.point()
        self.assertEqual(self.count("ANC_startAutoMove", 0, 0), 1)
        self.assertEqual(self.count("ANC_setAxisOutput", 0, 0), 1)
        self.assertFalse(self.sdk.axes[0]["enabled"])
        self.session.cleanup(failed=True)
        self.assertEqual(self.count("ANC_startAutoMove", 0, 0), 1)
        self.assertFalse(self.session._arrival_evidence)

    def test_off_reply_lost_is_not_retried_even_when_readback_says_off(self):
        self.begin()
        def lost(name, args):
            if name == "ANC_setAxisOutput" and args[2] == 0:
                self.sdk.axes[args[1]]["enabled"] = 0
                raise OSError("off reply lost")
        self.sdk.hook = lost
        with self.assertRaisesRegex(Anc350Error, "off reply lost"):
            self.point()
        self.session.cleanup(failed=True)
        self.assertEqual(self.count("ANC_setAxisOutput", 0, 0), 1)
        self.assertFalse(self.session._arrival_evidence)

    def test_output_on_after_arrival_blocks_acquisition_without_auto_repair(self):
        self.begin()
        self.point()
        self.sdk.axes[1]["enabled"] = 1
        before = len(self.sdk.calls)
        with self.assertRaisesRegex(PositionerError, "OFF"):
            self.session.verify()
        self.assertFalse(any(c[0].startswith(("ANC_set", "ANC_start")) for c in self.sdk.calls[before:]))
        self.assertFalse(self.session.cleanup(failed=True)["verified"])

    def test_faults_drive_and_hard_limits_still_block_after_arrival(self):
        self.begin()
        self.point()
        clean = deepcopy(self.sdk.axes)
        for field, value in (("sensor_error", 1), ("eot_forward", 1), ("eot_backward", 1),
                             ("sensor_connected", 0), ("position", .001), ("amplitude", 99.)):
            with self.subTest(field=field):
                self.sdk.axes = deepcopy(clean)
                self.sdk.axes[0][field] = value
                self.session._failed = False
                with self.assertRaises(PositionerError):
                    self.session.verify()
        self.sdk.axes = clean
        self.sdk.nonfinite_position = True
        self.session._failed = False
        with self.assertRaises(PositionerError):
            self.session.verify()
        self.sdk.nonfinite_position = False

    def test_release_cost_and_native_call_order(self):
        self.begin()
        self.device.prepare_axis("x")
        self.device.move_to("x", 0.)
        start = len(self.sdk.calls)
        result = self.device.complete_target_if_reached("x", guard=lambda: None)
        calls = self.sdk.calls[start:]
        self.assertTrue(result["reached"])
        self.assertEqual(len(calls), 61)
        writes = [c for c in calls if c[0].startswith(("ANC_set", "ANC_start"))]
        self.assertEqual([c[0] for c in writes], ["ANC_startAutoMove", "ANC_setAxisOutput"])
        self.assertEqual([c[3] for c in writes], [0, 0])

    def test_pending_target_is_polled_without_reissuing_move(self):
        self.begin()
        self.sdk.reach = False
        polls = []
        def reach_later(name, args):
            if name == "ANC_getAxisStatus" and args[1] == 0 and self.sdk.axes[0]["moving"]:
                polls.append(1)
                if len(polls) >= 3:
                    self.sdk.target[0] = True
                    self.sdk.reach = True
        self.sdk.hook = reach_later
        self.point()
        self.assertGreaterEqual(len(polls), 3)
        self.assertEqual(self.count("ANC_startAutoMove", 0, 1), 1)
        self.assertEqual(self.count("ANC_setAxisOutput", 0, 0), 1)

    def test_other_axis_output_on_prevents_next_preparation(self):
        self.begin()
        self.sdk.axes[1]["enabled"] = 1
        with self.assertRaisesRegex(PositionerError, "OFF"):
            self.point()
        self.assertEqual(self.count("ANC_startAutoMove", 0, 1), 0)

    def test_optical_guard_failure_prevents_any_axis_enable(self):
        self.begin()
        before = len(self.sdk.calls)
        def light_on():
            raise PositionerError("optical is ON")
        with self.assertRaisesRegex(PositionerError, "optical is ON"):
            self.session.set_point(self.config.points[0], light_on)
        self.assertFalse(any(c[0].startswith(("ANC_set", "ANC_start")) for c in self.sdk.calls[before:]))

    def test_full_path_rejected_before_first_output_enable(self):
        doc = deepcopy(self.document)
        doc["positioner"]["scan"]["mode"] = "relative"
        doc["positioner"]["scan"]["points"][1]["x_um"] = 500.
        config = load_positioner_config(doc)
        self.session.config = config
        self.session.preflight()
        with self.assertRaisesRegex(PositionerError, "limits"):
            self.session.begin()
        self.assertEqual(self.count("ANC_setAxisOutput"), 0)

    def test_audit_failure_before_release_prevents_accepted_arrival(self):
        self.begin()
        def unavailable(event):
            if event.get("name") == "target_reached_latched":
                raise OSError("arrival audit unavailable")
        self.device.on_event = unavailable
        with self.assertRaisesRegex(OSError, "audit unavailable"):
            self.point()
        self.assertEqual(self.count("ANC_startAutoMove", 0, 0), 1)
        self.assertEqual(self.count("ANC_setAxisOutput", 0, 0), 1)
        self.assertFalse(self.session._arrival_evidence)

    def test_late_release_call_fails_and_cleanup_never_repeats_stop(self):
        self.begin()
        def slow(name, args):
            if name == "ANC_startAutoMove" and args[2] == 0:
                self.clock.sleep(4.)
        self.sdk.hook = slow
        with self.assertRaisesRegex(PositionerError, "deadline"):
            self.point()
        self.assertEqual(self.count("ANC_startAutoMove", 0, 0), 1)
        self.assertEqual(self.count("ANC_setAxisOutput", 0, 0), 1)
        self.assertFalse(self.session._arrival_evidence)

    def test_return_start_also_latches_and_finishes_off(self):
        self.document["positioner"]["finish"] = "return_start"
        self.config = load_positioner_config(self.document)
        self.device.config = self.session.config = self.config
        self.begin()
        self.point(1)
        result = self.session.cleanup()
        self.assertTrue(result["verified"], result)
        self.assertEqual(self.session.actual(), self.session.origin)
        self.assertTrue(all(not r["output_enabled"] for r in result["states"]["axes"].values()))
