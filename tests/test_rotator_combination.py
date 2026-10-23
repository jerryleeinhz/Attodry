"""Optical waveplate coordination against owned fake devices only."""
import tomllib
import unittest
from unittest.mock import patch

from attodry_control.combination_analysis import load_combination_rows
from attodry_control.combination_hardware import HardwareCombinationStation, load_hardware_combination
from attodry_control.combination_launch import launch_summary
from attodry_control.combination_terminal import launch_text
from tests import test_internal_optical_combination as fixtures
from tests.test_three_smu_config import OPERATION_TEXT


class FakeRotator:
    def __init__(self, config, owner):
        self.config, self.owner = config, owner
        self.angle, self.homed, self.enabled = 0.0, False, False
        self.closed = False
        self.moving = False

    def open(self):
        self.owner.motor_log.append(("open", self.config.role))

    def read_state(self):
        self.owner.motor_log.append(("read", self.config.role))
        return dict(serial_number=self.config.serial_number, controller_model="KDC101",
                    stage_model="PRM1/MZ8", angle_deg=self.angle, homed=self.homed,
                    moving=self.moving, enabled=self.enabled, fault=False)

    def prepare(self):
        self.owner.motor_log.append(("prepare", self.config.role))
        self.enabled = True

    def home(self):
        self.owner.assertEqual(self.owner.backend.source["emission_state"], 0)
        self.owner.motor_log.append(("home", self.config.role))
        self.homed, self.angle = True, 0.0

    def move_to(self, angle):
        self.owner.assertEqual(self.owner.backend.source["emission_state"], 0)
        self.owner.motor_log.append(("move", self.config.role, angle))
        self.angle = angle

    def stop(self):
        self.owner.motor_log.append(("stop", self.config.role))
        self.moving = False

    def close(self):
        self.closed = True


class RotatorCombinationTests(unittest.TestCase):
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
        self.motor_log, self.motors = [], {}
        self.document["combination_scan"]["order"] = ["rotator", "optical", "lockin"]
        self.document["rotator_1"] = dict(enabled=True, serial_number="27000001",
            label="λ/2波片／入射偏振", controller="KDC101", stage_model="PRM1/MZ8",
            settle_s=0.02, poll_interval_s=0.01)
        self.document["rotator_2"] = dict(enabled=False, serial_number="CHANGE_ME")
        self.document["rotator_scan"] = dict(rotator_1_angles_deg=[0.0, 45.0, 360.0])
        self.write_document()

    def motor_factory(self, config):
        result = FakeRotator(config, self)
        self.motors[config.role] = result
        return result

    def test_angles_light_dark_and_second_off(self):
        self.document["optical_background"] = dict(enabled=True, samples=2, max_age_s=86400)
        self.write_document()
        result = self.execute(rotator_device_factory=self.motor_factory)
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["cleanup"]["clean"], result)
        self.assertEqual(set(self.motors), {"rotator_1"})
        rows = load_combination_rows(self.database)
        self.assertEqual(len(rows), 6)
        self.assertEqual([r["actual.rotator_1_angle_deg"] for r in rows], [0, 0, 45, 45, 360, 360])
        self.assertTrue(all(r["rotator_label.rotator_1"] == "λ/2波片／入射偏振" for r in rows))
        darks = [p for name, p in self.events() if name == "optical_dark_background"]
        self.assertEqual(len(darks), 3)
        self.assertEqual([p["context"]["requested_conditions"]["rotator"]["rotator_1_angle_deg"]
                          for p in darks], [0, 45, 360])
        self.assertEqual([p["context"]["actual_conditions"]["rotator"]["rotator_1_angle_deg"]
                          for p in darks], [0, 45, 360])
        self.assertTrue(self.motors["rotator_1"].closed)
        self.assertEqual(self.motors["rotator_1"].angle, 360)
        self.assertEqual(sum(e[0] == "home" for e in self.motor_log), 1)
        self.assertEqual(sum(e[0] == "move" for e in self.motor_log), 2)
        self.assert_closed_laser_off()

    def test_two_rotators_cartesian_first_outer(self):
        self.document["rotator_2"] = dict(self.document["rotator_1"], serial_number="27000002", label="备用波片")
        self.document["rotator_scan"] = dict(rotator_1_angles_deg=[0, 90], rotator_2_angles_deg=[10, 20])
        self.write_document()
        config = load_hardware_combination(self.path)
        self.assertEqual([p.values for p in config.plan.axes[0].points], [
            {"rotator_1_angle_deg": 0, "rotator_2_angle_deg": 10},
            {"rotator_1_angle_deg": 0, "rotator_2_angle_deg": 20},
            {"rotator_1_angle_deg": 90, "rotator_2_angle_deg": 10},
            {"rotator_1_angle_deg": 90, "rotator_2_angle_deg": 20}])
        result = self.execute(rotator_device_factory=self.motor_factory)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(len(load_combination_rows(self.database)), 8)
        self.assertTrue(all(m.closed for m in self.motors.values()))

    def test_angle_drift_after_electrical_read_rejects_window_and_cleans(self):
        original = HardwareCombinationStation.read
        def drift(station, module):
            reading = original(station, module)
            if module == "lockin":
                self.motors["rotator_1"].angle = 15.0
            return reading
        with patch.object(HardwareCombinationStation, "read", drift):
            result = self.execute(rotator_device_factory=self.motor_factory)
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("drifted", result["error"])
        self.assertEqual(load_combination_rows(self.database), ())
        self.assert_closed_laser_off()
        self.assertTrue(self.motors["rotator_1"].closed)
        self.assertTrue(any(name == "raw_reading" for name, _ in self.events()))

    def test_rotator_preflight_failure_prevents_all_configuration(self):
        def bad(config):
            motor = self.motor_factory(config)
            original = motor.read_state
            motor.read_state = lambda: {**original(), "serial_number": "27009999"}
            return motor
        result = self.execute(rotator_device_factory=bad)
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(self.xx.writes + self.xy.writes, [])
        self.assertFalse(any(e[0] in {"home", "move", "prepare"} for e in self.motor_log))
        self.assert_closed_laser_off()

    def test_launch_labels_degrees_and_offline_order_guards(self):
        config = load_hardware_combination(self.path)
        summary = launch_summary(config, self.database, "fake")
        output = launch_text(summary)
        self.assertIn("λ/2波片／入射偏振", output)
        self.assertIn("deg", output)
        self.assertIn("uncalibrated", output)
        self.assertNotIn("rotator_2", summary["rotator"])
        for order in (["optical", "rotator", "lockin"], ["rotator", "lockin"]):
            self.document["combination_scan"]["order"] = order
            self.write_document()
            with self.assertRaisesRegex(ValueError, "Waveplate rotator"):
                load_hardware_combination(self.path)
        self.assertEqual(self.motor_log, [])

    def test_motion_during_power_tuning_stops_before_next_adjustment(self):
        self.writes_at_motion = None
        factory = self.meter_factory
        def moving_meter(config):
            meter = factory(config)
            read = meter.read_sample
            def sample(wavelength):
                value = read(wavelength)
                if self.backend.source["emission_state"] == 3 and self.writes_at_motion is None:
                    self.motors["rotator_1"].moving = True
                    self.writes_at_motion = sum(c[0] == "configure" for c in self.backend.commands)
                return value
            meter.read_sample = sample
            return meter
        self.meter_factory = moving_meter
        result = self.execute(rotator_device_factory=self.motor_factory)
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("not ready", result["error"])
        self.assertIsNotNone(self.writes_at_motion)
        self.assertEqual(sum(c[0] == "configure" for c in self.backend.commands), self.writes_at_motion)
        self.assertEqual(load_combination_rows(self.database), ())
        self.assertIn(("stop", "rotator_1"), self.motor_log)
        self.assert_closed_laser_off()

    def test_gate_variant_keeps_guarded_direct_voltage_and_zero_cleanup(self):
        smu = tomllib.loads(OPERATION_TEXT)
        smu.pop("smu_bias")
        smu.pop("gate_bottom")
        smu["three_smu_run"].update(delay_s=0.01, serpentine=False)
        smu["three_smu_run"]["smu_bias"] = dict(role="off", bidirectional=False)
        smu["three_smu_run"]["gate_bottom"] = dict(role="off", bidirectional=False)
        smu["three_smu_run"]["gate_top"] = dict(role="sweep", bidirectional=False,
            points=[0, .1, -.1], zero_readback_tolerance_v=.05)
        self.document.update(smu)
        self.document["rotator_scan"] = dict(rotator_1_angles_deg=[0])
        self.document["combination_scan"].update(order=["rotator", "optical", "smu", "lockin"],
                                                 samples_per_condition=1)
        self.write_document()
        guarded = []
        original = HardwareCombinationStation.open
        def opened(station, modules):
            result = original(station, modules)
            guarded.extend(station.smu._guarded_direct_roles)
            return result
        with patch.object(HardwareCombinationStation, "open", opened):
            result = self.execute(rotator_device_factory=self.motor_factory)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(guarded, ["gate_top"])
        self.assertEqual(len(load_combination_rows(self.database)), 3)
        self.assertFalse(self.adapters["gate_top"].output)
        self.assertEqual(self.adapters["gate_top"].source, 0)
        self.assertTrue(result["cleanup"]["clean"], result)

    def test_once_dark_mode_rejected_before_hardware(self):
        self.document["optical_background"] = dict(enabled=True, capture_mode="once_before_scan")
        self.write_document()
        with self.assertRaisesRegex(ValueError, "per_condition"):
            load_hardware_combination(self.path)
        self.assertEqual(self.manager.opened, [])
        self.assertEqual(self.motor_log, [])

    def test_real_driver_packet_path_preserves_full_turn_and_return_visit(self):
        from attodry_control.thorlabs_kdc101 import D2xxTransport
        from tests.test_thorlabs_kdc101 import FakeD2xx
        libraries = []
        transport = D2xxTransport
        def fake_transport(directory="", **kwargs):
            library = FakeD2xx()
            libraries.append(library)
            return transport(directory, library=library, clock=self.clock)
        self.document["rotator_scan"] = dict(rotator_1_angles_deg=[0, 360, 0])
        self.write_document()
        with patch("attodry_control.thorlabs_kdc101.D2xxTransport", side_effect=fake_transport):
            result = self.execute()
        self.assertEqual(result["status"], "completed", result)
        rows = load_combination_rows(self.database)
        self.assertEqual(len(rows), 6)
        actual = [r["actual.rotator_1_angle_deg"] for r in rows]
        self.assertTrue(all(0 <= value <= 360 for value in actual))
        self.assertAlmostEqual(actual[2], 360, delta=.001)
        self.assertEqual([rows[i]["requested.rotator_1_angle_deg"] for i in (0, 2, 4)], [0, 360, 0])
        self.assertEqual(len(libraries), 1)
        self.assertEqual(libraries[0].closed, 1)
        self.assertTrue(any(name == "rotator_raw" and payload.get("direction") == "rx"
                            for name, payload in self.events()))
        self.assert_closed_laser_off()


if __name__ == "__main__":
    unittest.main()
