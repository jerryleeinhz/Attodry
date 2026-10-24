"""XYZ coordination through the real coordinator with owned fake transports."""
from contextlib import closing, redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from attodry_control.combination_analysis import load_combination_rows
from attodry_control.combination_cli import run as cli
from attodry_control.combination_hardware import HardwareCombinationStation, load_hardware_combination, run_hardware_combination
from attodry_control.combination_launch import launch_summary
from attodry_control.combination_scan import AxisPoint, CombinationPlan, ScanAxis, run_simulated_combination
from attodry_control.combination_store import CombinationStore, open_readonly
from attodry_control.combination_terminal import launch_text
from attodry_control.hardware_lease import controller_hardware_lease
from attodry_control.lockin_range_rules import compile_lockin_range_rules
from tests import test_internal_optical_combination as fixtures
from tests.test_positioner_config import positioner_document


class FakePositioner:
    def __init__(self, config, owner):
        self.config, self.owner = config, owner
        self.position = dict(x=0., y=0., z=10. if config.focus_plane else 0.)
        self.enabled = dict.fromkeys("xyz", False)
        self.closed, self.moving = False, False

    def open(self):
        self.owner.position_log.append(("open",))

    def read_state(self):
        return {"serial_number": self.config.controller_serial_number,
            "controller_model": "ANC350 RES", "firmware_raw": self.config.expected_firmware_raw,
            "axes": {axis.role: {"axis_index": axis.index,
                "actuator_name": axis.expected_actuator_name, "lut_name": axis.expected_lut_name,
                "position_um": self.position[axis.role], "sensor_connected": True,
                "output_enabled": self.enabled[axis.role], "moving": self.moving,
                "target_reached": False, "eot_forward": False, "eot_backward": False,
                "sensor_error": False, "amplitude_v": axis.amplitude_v,
                "frequency_hz": axis.frequency_hz} for axis in self.config.axes}}

    def _read_initial_state(self, *, allow_enabled=False):
        return self.read_state()

    def initialize_outputs_off(self, *, guard, checkpoint=None):
        guard()
        if checkpoint is not None:
            checkpoint()
        if self.owner.backend is not None:
            self.owner.assertEqual(self.owner.backend.source["emission_state"], 0)
        self.owner.position_log.append(("initialize_outputs_off",))
        self.enabled = dict.fromkeys("xyz", False)
        return self.read_state()

    def initialize_drive(self, *, guard=None, checkpoint=None):
        if guard is not None:
            guard()
        if checkpoint is not None:
            checkpoint()
        self.owner.position_log.append(("initialize_drive",))
        return self.read_state()

    def prepare_axis(self, role):
        self.enabled[role] = True
        self.owner.position_log.append(("prepare", role))

    def move_to(self, role, target):
        if self.owner.backend is not None:
            self.owner.assertEqual(self.owner.backend.source["emission_state"], 0)
        self.owner.position_log.append(("move", role, target))
        self.position[role] = target

    def stop(self, role):
        self.owner.position_log.append(("stop", role))
        self.moving = False

    def complete_target_if_reached(self, role, *, guard, checkpoint=None):
        guard()
        if checkpoint is not None:
            checkpoint()
        self.owner.assertEqual(self.owner.backend.source["emission_state"], 0)
        from attodry_control.combination_store import utc_now
        arrival = self.read_state()
        arrival["axes"][role]["target_reached"] = True
        arrival["axes"][role]["moving"] = True
        self.enabled[role] = False
        self.owner.position_log.append(("target_output_off", role))
        return {"reached": True, "arrival_state": arrival,
                "arrival_captured_at_utc": utc_now(), "state": self.read_state(),
                "captured_at_utc": utc_now()}

    def close(self):
        self.closed = True


class PositionerCombinationTests(unittest.TestCase):
    write_config = fixtures.InternalOpticalCombinationTests.write_config
    write_document = fixtures.InternalOpticalCombinationTests.write_document
    factory = fixtures.InternalOpticalCombinationTests.factory
    events = fixtures.InternalOpticalCombinationTests.events
    backend_factory = fixtures.InternalOpticalCombinationTests.backend_factory
    meter_factory = fixtures.InternalOpticalCombinationTests.meter_factory
    chopper_factory = fixtures.InternalOpticalCombinationTests.chopper_factory
    execute = fixtures.InternalOpticalCombinationTests.execute
    assert_closed_laser_off = fixtures.InternalOpticalCombinationTests.assert_closed_laser_off

    def setUp(self):
        fixtures.InternalOpticalCombinationTests.setUp(self)
        self.position_log, self.motor = [], None
        self.document.update(positioner_document())
        self.document["combination_scan"]["order"] = ["positioner", "optical", "lockin"]
        self.write_document()

    def motor_factory(self, config):
        self.motor = FakePositioner(config, self)
        return self.motor

    def samples(self):
        with closing(open_readonly(self.database)) as db:
            return [json.loads(row[0]) for row in db.execute("SELECT payload_json FROM combination_samples ORDER BY rowid")]

    def test_absolute_xyz_dark_and_light_visits_and_off_before_motion(self):
        self.document["optical_background"] = dict(enabled=True, samples=2, max_age_s=86400)
        self.write_document()
        result = self.execute(positioner_device_factory=self.motor_factory)
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["cleanup"]["clean"], result)
        rows = load_combination_rows(self.database)
        self.assertEqual([r["actual.positioner_x_um"] for r in rows], [0, 0, 10, 10])
        darks = [p for name, p in self.events() if name == "optical_dark_background"]
        self.assertEqual(len(darks), 2)
        self.assertEqual([d["context"]["positioner"]["target"]["positioner_x_um"] for d in darks], [0, 10])
        self.assertTrue(self.motor.closed)
        self.assert_closed_laser_off()

    def test_target_output_off_policy_archives_real_actual_and_arrival_proof(self):
        self.document["positioner"]["arrival_policy"] = "target_reached_output_off"
        self.document["optical_background"] = dict(enabled=True, samples=2, max_age_s=86400)
        self.write_document()
        original = HardwareCombinationStation.read
        def noisy(station, module):
            result = original(station, module)
            if module == "lockin":
                self.motor.position["z"] += 1.  # No target-error rejection after SDK arrival.
                self.motor.moving = True
            return result
        with patch.object(HardwareCombinationStation, "read", noisy):
            result = self.execute(positioner_device_factory=self.motor_factory)
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["cleanup"]["clean"], result)
        samples = self.samples()
        self.assertEqual(len(samples), 4)
        for sample in samples:
            status = next(r for r in sample["reads"] if r["module"] == "positioner")["status"]["positioner"]
            self.assertEqual(status["arrival_policy"], "target_reached_output_off")
            self.assertEqual(set(status["arrival_evidence"]), set("xyz"))
            self.assertTrue(all(not row["output_enabled"] for row in status["readback"]["axes"].values()))
        self.assertGreater(samples[-1]["actual"]["positioner_z_um"], 0.)
        darks = [p for n, p in self.events() if n == "optical_dark_background"]
        self.assertTrue(all(p["context"]["positioner"]["arrival_policy"] == "target_reached_output_off" for p in darks))
        self.assertFalse(any(self.motor.enabled.values()))
        self.assert_closed_laser_off()

    def test_output_on_after_lockin_rejects_raw_reading_with_new_policy(self):
        self.document["positioner"]["arrival_policy"] = "target_reached_output_off"
        self.write_document()
        original = HardwareCombinationStation.read
        def output_on(station, module):
            result = original(station, module)
            if module == "lockin":
                self.motor.enabled["x"] = True
            return result
        with patch.object(HardwareCombinationStation, "read", output_on):
            result = self.execute(positioner_device_factory=self.motor_factory)
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("OFF", result["error"])
        self.assertEqual(load_combination_rows(self.database), ())
        self.assertTrue(any(n == "raw_reading" for n, _ in self.events()))
        self.assert_closed_laser_off()

    def test_held_outputs_are_released_once_with_light_off_before_new_origin(self):
        def held(config):
            motor = self.motor_factory(config)
            motor.enabled = dict.fromkeys("xyz", True)
            return motor
        result = self.execute(positioner_device_factory=held)
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["cleanup"]["clean"], result)
        names = [name for name, payload in self.events()]
        self.assertLess(names.index("positioner_outputs_off_requested"), names.index("positioner_outputs_off"))
        self.assertLess(names.index("positioner_outputs_off"), names.index("positioner_origin_frozen"))
        self.assertLess(names.index("positioner_origin_frozen"), names.index("positioner_drive_initialized"))
        self.assertEqual(sum(item[0] == "initialize_outputs_off" for item in self.position_log), 1)
        self.assertEqual(len(load_combination_rows(self.database)), 4)
        self.assert_closed_laser_off()

    def test_relative_origin_is_once_per_run_across_repeats_and_actual_is_absolute(self):
        self.document.update(positioner_document(mode="relative"))
        self.document["combination_scan"]["repeats"] = 2
        self.write_document()
        def initial(config):
            motor = self.motor_factory(config)
            motor.position.update(x=20., y=30., z=5.)
            return motor
        result = self.execute(positioner_device_factory=initial)
        self.assertEqual(result["status"], "completed", result)
        samples = self.samples()
        self.assertEqual([s["actual"]["positioner_x_um"] for s in samples], [20, 20, 30, 30] * 2)
        self.assertTrue(all("positioner_x_offset_um" not in s["actual"] for s in samples))
        origins = [next(r for r in s["reads"] if r["module"] == "positioner")["status"]["positioner"]["origin"] for s in samples]
        self.assertTrue(all(p == origins[0] for p in origins))
        self.assertEqual(sum(name == "positioner_origin_frozen" for name, _ in self.events()), 1)

    def test_focus_z_and_error_are_formal_records(self):
        self.document.update(positioner_document(focus=True))
        self.write_document()
        result = self.execute(positioner_device_factory=self.motor_factory)
        self.assertEqual(result["status"], "completed", result)
        samples = self.samples()
        # A within-tolerance move need not rewrite the actuator. Preserve the
        # actual readback, never replace it with the planned focus Z.
        self.assertAlmostEqual(samples[-1]["actual"]["positioner_z_um"], 10.)
        self.assertAlmostEqual(samples[-1]["measurements"]["positioner_focus_error_um"], -0.1)
        status = next(r for r in samples[-1]["reads"] if r["module"] == "positioner")["status"]["positioner"]
        self.assertEqual(len(status["focus_plane_id"]), 64)
        self.assertEqual(status["coordinate_frame"], "fixture-frame")

    def test_post_lockin_drift_rejects_raw_attempt_and_cleanup_still_runs(self):
        original = HardwareCombinationStation.read
        def drift(station, module):
            result = original(station, module)
            if module == "lockin":
                self.motor.position["x"] += 1.
            return result
        with patch.object(HardwareCombinationStation, "read", drift):
            result = self.execute(positioner_device_factory=self.motor_factory)
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("drift", result["error"])
        self.assertEqual(load_combination_rows(self.database), ())
        self.assertTrue(any(name == "raw_reading" for name, _ in self.events()))
        self.assertTrue(self.motor.closed)
        self.assert_closed_laser_off()

    def test_identity_preflight_failure_before_any_settings(self):
        def wrong(config):
            motor = self.motor_factory(config)
            state = motor.read_state
            motor.read_state = lambda: {**state(), "serial_number": "other"}
            return motor
        result = self.execute(positioner_device_factory=wrong)
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(self.xx.writes + self.xy.writes, [])
        self.assertFalse(any(e[0] in ("move", "prepare") for e in self.position_log))
        self.assertTrue(self.motor.closed)

    def test_success_return_start_keeps_fresh_off_access_until_return_and_then_closes(self):
        self.document["positioner"]["finish"] = "return_start"
        self.write_document()
        result = self.execute(positioner_device_factory=self.motor_factory)
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["cleanup"]["clean"], result)
        self.assertEqual(self.motor.position, dict(x=0., y=0., z=0.))
        self.assert_closed_laser_off()
        kinds = [name for name, _ in self.events()]
        self.assertLess(kinds.index("optical_positioner_return_off"), kinds.index("optical_final_cleanup"))

    def test_electrical_cleanup_failure_blocks_success_return_but_stops_owned_feedback(self):
        self.document["positioner"]["finish"] = "return_start"
        self.write_document()
        from attodry_control.lockin_points import LockinPointSession
        with patch.object(LockinPointSession, "cleanup", side_effect=OSError("electrical cleanup failure")):
            result = self.execute(positioner_device_factory=self.motor_factory)
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(self.motor.position, dict(x=10., y=10., z=0.))
        self.assertTrue(self.motor.closed)
        self.assertTrue(any(item[0] == "stop" for item in self.position_log))
        self.assertEqual(self.backend.source["emission_state"], 0)

    def test_temperature_bound_plane_waits_for_outer_temperature_before_origin(self):
        from tests.test_attodry import FakeAttoDryDll
        self.document.update(positioner_document(focus=True))
        self.document["positioner"]["focus_plane"].update(
            calibration_temperature_k=3., temperature_tolerance_k=0.05)
        self.document["temperature_scan"].pop("temperature_ranges")
        self.document["temperature_scan"].update(start_k=3., stop_k=3., step_k=0.1)
        self.document["temperature_stability"].update(stable_dwell_s=0.2,
            poll_interval_s=0.05, wait_timeout_s=10.)
        self.document["combination_scan"]["order"] = ["temperature", "positioner", "lockin"]
        self.document["lockin_xx"]["sine_output_connected"] = True
        self.write_document()
        dll = FakeAttoDryDll()
        dll.temperature_follows_setpoint = True
        self.assertEqual(dll.sample_temperature_k, 2.)
        original_factory = self.motor_factory
        def motor(config):
            device = original_factory(config)
            prepare = device.prepare_axis
            def qualified_prepare(role):
                self.assertAlmostEqual(dll.sample_temperature_k, 3.)
                prepare(role)
            device.prepare_axis = qualified_prepare
            return device
        result = self.execute(positioner_device_factory=motor, dll=dll, authorize_cryostat=True)
        self.assertEqual(result["status"], "completed", result)
        kinds = [kind for kind, _ in self.events()]
        self.assertLess(kinds.index("temperature_qualification_completed"), kinds.index("positioner_origin_frozen"))
        self.assertEqual(dll.events.count("connect"), 1)
        self.assertEqual(dll.events.count("disconnect"), 1)

    def test_magnetic_outer_reuses_single_cryo_and_positioner_session(self):
        from tests.test_attodry import FakeAttoDryDll
        self.document["combination_scan"]["order"] = ["magnetic", "positioner", "lockin"]
        self.document["lockin_xx"]["sine_output_connected"] = True
        self.document["magnetic_field_run"]["points"] = [dict(bx_t=0., bz_t=0.), dict(bx_t=0., bz_t=0.01)]
        self.document["magnet"].update(stable_dwell_s=0.2, poll_interval_s=0.05, wait_timeout_s=10.)
        self.write_document()
        dll = FakeAttoDryDll()
        result = self.execute(positioner_device_factory=self.motor_factory, dll=dll, authorize_cryostat=True)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(dll.events.count("connect"), 1)
        self.assertEqual(dll.events.count("disconnect"), 1)
        self.assertEqual(sum(item[0] == "open" for item in self.position_log), 1)
        self.assertEqual(sum(name == "positioner_origin_frozen" for name, _ in self.events()), 1)
        self.assertEqual(len(load_combination_rows(self.database)), 8)

    def test_standalone_positioner_and_offline_describe(self):
        self.document["combination_scan"]["order"] = ["positioner"]
        self.write_document()
        with redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli(["describe-hardware", "--config", str(self.path)]), 0)
        self.assertEqual(len(json.loads(output.getvalue())["conditions"]), 2)
        self.assertIsNone(self.motor)
        result = self.execute(positioner_device_factory=self.motor_factory)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(len(load_combination_rows(self.database)), 4)
        self.assertEqual(self.manager.opened, [])

    def test_off_guard_order_and_background_mode_rejected_offline(self):
        for order in (["optical", "positioner", "lockin"], ["lockin", "positioner"]):
            self.document["combination_scan"]["order"] = order
            self.write_document()
            with self.assertRaisesRegex(ValueError, "Positioner requires"):
                load_hardware_combination(self.path)
        self.document["combination_scan"]["order"] = ["positioner"]
        self.document["positioner"]["external_light_off_confirmed"] = False
        self.write_document()
        with self.assertRaisesRegex(ValueError, "external_light_off"):
            load_hardware_combination(self.path)
        self.document["combination_scan"]["order"] = ["positioner", "optical", "lockin"]
        self.document["optical_background"] = dict(enabled=True, samples=2, max_age_s=86400, capture_mode="once_before_scan")
        self.write_document()
        with self.assertRaisesRegex(ValueError, "per_condition"):
            load_hardware_combination(self.path)

    def test_launch_summary_units_and_range_rules_use_requested_position(self):
        config = load_hardware_combination(self.path)
        summary = launch_summary(config, self.database, "fixture")
        text = launch_text(summary)
        self.assertIn("SDK 0/1/2 = X/Y/Z", text)
        self.assertIn("um", text)
        rules = [{"name": "low", "match": {"positioner_x_um": [0.]},
                  "lockin_xx_full_scale_v": 0.001, "lockin_xy_full_scale_v": 0.001},
                 {"name": "high", "match": {"positioner_x_um": [10.]},
                  "lockin_xx_full_scale_v": 0.01, "lockin_xy_full_scale_v": 0.01}]
        schedule = compile_lockin_range_rules(rules, config.plan,
            {role: (0.001, 0.01) for role in ("lockin_xx", "lockin_xy")})
        self.assertEqual([r["full_scales_v"]["lockin_xx"] for r in schedule.preview()], [0.001, 0.01])


class PositionerLeaseAndSimulationTests(unittest.TestCase):
    def test_controller_aliases_share_lease_across_config_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            with controller_hardware_lease("FIXTURE 01 0001", directory=tmp):
                with self.assertRaisesRegex(ValueError, "owns"):
                    with controller_hardware_lease("fixture010001", directory=tmp):
                        self.fail("second controller ownership admitted")
                with controller_hardware_lease("other", directory=tmp):
                    pass
            with controller_hardware_lease("FIXTURE010001", directory=tmp):
                pass

    def test_simulator_relative_xyz_does_not_promote_offset_as_actual(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "simulation.sqlite"
            plan = CombinationPlan((ScanAxis("positioner", (AxisPoint(dict(
                positioner_x_offset_um=1., positioner_y_offset_um=2., positioner_z_offset_um=3.)),)),))
            with CombinationStore(path) as store:
                result = run_simulated_combination(plan, store, "synthetic")
            self.assertEqual(result["status"], "completed", result)
            row = load_combination_rows(path)[0]
            self.assertEqual(row["requested.positioner_x_offset_um"], 1.)
            self.assertEqual(row["actual.positioner_x_um"], 1.)
            self.assertNotIn("actual.positioner_x_offset_um", row)


if __name__ == "__main__":
    unittest.main()
