from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import json
import math
import os
import tempfile
import unittest
from unittest.mock import patch

from attodry_control.attodry import AttoDryError
from attodry_control.models import CryostatState, VectorField
from attodry_control.temperature_experiment import (
    Archive, CRITERIA, Experiment, Point, execute, load_plan, sha256,
)


class Clock:
    def __init__(self):
        self.t = 0.0
        self.epoch = 1792520000.0

    def monotonic(self):
        return self.t

    def wall(self):
        return self.epoch+self.t

    def sleep(self, seconds):
        if seconds < 0:
            raise AssertionError("negative sleep")
        self.t += seconds


class MemoryArchive:
    def __init__(self):
        self.events, self.states = [], []
        self.closed = False
        self.fail_event = None

    def emit(self, event, **values):
        if event == self.fail_event:
            raise OSError("simulated audit failure")
        self.events.append((event, values))

    def snapshot(self, value):
        self.states.append(value)

    def close(self):
        self.closed = True


def config():
    return SimpleNamespace(cryostat=SimpleNamespace(temperature_min_k=1.6, temperature_max_k=300),
        temperature_run=SimpleNamespace(max_delta_k=260, max_overshoot_k=15))


class Driver:
    def __init__(self, experiment, clock):
        self.experiment, self.clock = experiment, clock
        self.state = CryostatState(50, 50, 45, VectorField(0, 0), VectorField(0, 0), True, False, 0)
        self.commands = []
        self.disable_calls = self.heater_calls = self.reads = 0
        self.closed = False
        self.on_read = lambda driver: None
        self.command_delay_s = 0
        self.heater_value = SimpleNamespace(sample_w=0.2, vti_w=1.0)
        self.close_error = False

    def connect(self, **kwargs):
        pass

    def read_state(self):
        self.reads += 1
        self.on_read(self)
        self.experiment.captured(self.state)
        return self.state

    def read_heater_powers(self):
        from attodry_control.attodry import HeaterPowerState
        self.heater_calls += 1
        return HeaterPowerState(self.heater_value.sample_w, self.heater_value.vti_w)

    def set_temperature_and_enable(self, target, **kwargs):
        self.set_temperature(target, **kwargs)

    def set_temperature(self, target, **kwargs):
        self.commands.append((self.clock.t, target))
        self.clock.sleep(self.command_delay_s)
        self.state = replace(self.state, user_temperature_k=target, sample_temperature_k=target,
                             temperature_control_enabled=True)

    def ensure_temperature_control(self, enabled, **kwargs):
        if enabled:
            raise AssertionError("unexpected enable outside command")
        self.disable_calls += 1
        self.state = replace(self.state, temperature_control_enabled=False)

    def close(self):
        self.closed = True
        if self.close_error:
            raise OSError("close failed")


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.archive = MemoryArchive()
        self.driver = None
        self.setup_driver = lambda driver: None

    def factory(self, experiment):
        self.driver = Driver(experiment, self.clock)
        self.setup_driver(self.driver)
        return self.driver

    def run_points(self, points=None, **kwargs):
        with patch("builtins.print"):
            return execute(config(), points or [Point("test", 70, hold_s=0)], self.archive,
                driver_factory=self.factory, monotonic=self.clock.monotonic,
                wall_time=self.clock.wall, sleep=self.clock.sleep,
                deadline_s=self.clock.wall()+20000, **kwargs)

    def test_default_fixed_restores_original_full_qualification(self):
        result = self.run_points()
        self.assertEqual(result["status"], "completed")
        self.assertEqual(self.driver.commands, [(0, 70)])
        self.assertGreaterEqual(result["points"][0]["qualification_s"], 120)
        self.assertEqual(result["points"][0]["first_arrival_s"], 0)
        self.assertEqual(self.driver.disable_calls, 0)
        self.assertTrue(self.driver.closed and self.archive.closed)

    def test_20_and_40k_points_get_independent_windows(self):
        result = self.run_points([Point("20K", 70, hold_s=0), Point("40K", 110, hold_s=0)])
        self.assertEqual(result["completed_points"], 2)
        self.assertEqual([p["actual_step_k"] for p in result["points"]], [20, 40])
        self.assertTrue(all(p["qualification_s"] >= 120 for p in result["points"]))
        self.assertGreaterEqual(self.clock.t, 240)

    def test_readiness_has_no_temperature_writes(self):
        result = self.run_points(readiness=True)
        self.assertEqual(result["status"], "readiness_complete")
        self.assertEqual(self.driver.commands, [])
        self.assertEqual(self.driver.disable_calls, 0)
        self.assertEqual(self.driver.heater_calls, 1)

    def test_heater_disabled_adds_no_getters(self):
        self.run_points(heater_interval=None)
        self.assertEqual(self.driver.heater_calls, 0)

    def test_heater_rate_is_bounded(self):
        self.run_points(poll=1.5, heater_interval=5)
        times = [v["monotonic_s"] for e, v in self.archive.events if e == "heater_power"]
        self.assertTrue(all(b-a >= 5 for a, b in zip(times, times[1:])))
        self.assertLessEqual(len(times), 25)

    def test_shadow_has_identical_commands_waits_and_heater_io(self):
        fixed = self.run_points()
        commands, duration, getters = list(self.driver.commands), self.clock.t, self.driver.heater_calls
        self.setUp()
        shadow = self.run_points([Point("test", 70, method="shadow", hold_s=0)])
        self.assertEqual(self.driver.commands, commands)
        self.assertEqual(self.clock.t, duration)
        self.assertEqual(self.driver.heater_calls, getters)
        self.assertEqual(shadow["points"][0]["qualification_s"], fixed["points"][0]["qualification_s"])
        self.assertTrue(any(e == "shadow_prediction" for e, _ in self.archive.events))

    def test_sequence_window_begins_after_final_ack(self):
        point = Point("sequence", 70, "sequence", 1800, 0, ((0, 60), (180, 70)))
        result = self.run_points([point])
        self.assertEqual([t for _, t in self.driver.commands], [60, 70])
        self.assertGreaterEqual(result["points"][0]["qualification_s"], 300)

    def test_slow_ack_does_not_compress_write_intervals(self):
        self.setup_driver = lambda d: setattr(d, "command_delay_s", 50)
        point = Point("sequence", 70, "sequence", 1800, 0, ((0, 55), (30, 60), (60, 70)))
        self.run_points([point])
        times = [t for t, _ in self.driver.commands]
        self.assertTrue(all(b-a >= 80 for a, b in zip(times, times[1:])))

    def test_ack_is_inside_point_timeout(self):
        self.setup_driver = lambda d: setattr(d, "command_delay_s", 180)
        result = self.run_points([Point("late", 70, max_wait_s=120, hold_s=0)])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["completed_points"], 0)
        self.assertEqual(self.driver.disable_calls, 1)
        self.assertTrue(self.driver.closed)

    def test_external_target_change_aborts_without_retry(self):
        def setup(d):
            def changed(driver):
                if self.clock.t > 5:
                    driver.state = replace(driver.state, user_temperature_k=71)
            d.on_read = changed
        self.setup_driver = setup
        result = self.run_points()
        self.assertEqual(result["status"], "failed")
        self.assertIn("target changed", result["primary_error"])
        self.assertEqual(len(self.driver.commands), 1)
        self.assertEqual(self.driver.disable_calls, 1)

    def test_temperature_control_off_aborts(self):
        def setup(d):
            def off(driver):
                if self.clock.t > 5:
                    driver.state = replace(driver.state, temperature_control_enabled=False)
            d.on_read = off
        self.setup_driver = setup
        self.assertEqual(self.run_points()["status"], "failed")
        self.assertEqual(self.driver.disable_calls, 1)

    def test_strict_3t_rejects_above_boundary_before_writes(self):
        self.setup_driver = lambda d: setattr(d, "state", replace(d.state, field=VectorField(0, 3.000001)))
        result = self.run_points()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.driver.commands, [])
        self.assertTrue(self.driver.closed)

    def test_external_magnetic_setpoint_change_aborts(self):
        def setup(d):
            def changed(driver):
                if self.clock.t > 5:
                    driver.state = replace(driver.state, field_setpoint=VectorField(0.001, 0))
            d.on_read = changed
        self.setup_driver = setup
        self.assertEqual(self.run_points()["status"], "failed")
        self.assertEqual(self.driver.disable_calls, 1)

    def test_fault_read_does_not_infer_zero(self):
        def setup(d):
            d.state = replace(d.state, field=VectorField(0.2, 0))
            def fail(driver):
                if self.clock.t > 5:
                    raise AttoDryError("communication failed")
            d.on_read = fail
        self.setup_driver = setup
        result = self.run_points()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["final_state"]["field"]["bx_t"], 0.2)
        self.assertTrue(result["cleanup"]["errors"])

    def test_nan_heater_aborts_and_closes(self):
        self.setup_driver = lambda d: setattr(d.heater_value, "sample_w", math.nan)
        result = self.run_points()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.driver.commands, [])
        self.assertTrue(self.driver.closed)

    def test_overshoot_is_relative_to_final_scientific_target(self):
        def setup(d):
            def hot(driver):
                if self.clock.t > 5:
                    driver.state = replace(driver.state, sample_temperature_k=85)
            d.on_read = hot
        self.setup_driver = setup
        point = Point("sequence", 70, "sequence", 1800, 0, ((0, 60), (180, 70)))
        result = self.run_points([point])
        self.assertEqual(result["status"], "failed")
        self.assertIn("overshoot", result["primary_error"])

    def test_candidate_outside_initial_final_interval_never_writes(self):
        point = Point("bad", 70, "sequence", 1800, 0, ((0, 75), (180, 70)))
        self.assertEqual(self.run_points([point])["status"], "failed")
        self.assertEqual(self.driver.commands, [])

    def test_diagnostic_hold_does_not_retune_temperature(self):
        result = self.run_points([Point("hold", 70, hold_s=30)])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(self.driver.commands), 1)
        self.assertGreaterEqual(self.clock.t, 150)

    def test_owned_cancellation_uses_disable_and_close(self):
        result = self.run_points(stop_requested=lambda: self.clock.t > 5)
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(self.driver.disable_calls, 1)
        self.assertTrue(self.driver.closed)

    def test_audit_fault_still_attempts_disable_and_close(self):
        self.archive.fail_event = "temperature_command_confirmed"
        result = self.run_points()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.driver.disable_calls, 1)
        self.assertTrue(self.driver.closed)

    def test_snapshot_delay_cannot_qualify_after_point_deadline(self):
        original = self.archive.snapshot

        def delayed(value):
            original(value)
            if self.clock.t >= 120:
                self.clock.sleep(3)

        self.archive.snapshot = delayed
        result = self.run_points([Point("deadline", 70, max_wait_s=121.5, hold_s=0)])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["completed_points"], 0)
        self.assertFalse(any(event == "point_qualified" for event, _ in self.archive.events))
        self.assertEqual(self.driver.commands, [(0, 70)])
        self.assertEqual(self.driver.disable_calls, 1)
        self.assertTrue(self.driver.closed)

    def test_transient_snapshot_retry_does_not_repeat_temperature_command(self):
        with tempfile.TemporaryDirectory() as directory:
            self.archive = Archive(directory, "fixed-run")
            original = os.replace
            error = PermissionError("simulated Windows sharing error")
            error.winerror = 32
            attempts = []

            def replace_file(source, destination):
                attempts.append((source, destination))
                if len(attempts) == 1:
                    raise error
                return original(source, destination)

            with patch("attodry_control.temperature_experiment.os.replace", side_effect=replace_file), patch("attodry_control.temperature_experiment.time.sleep", side_effect=self.clock.sleep):
                result = self.run_points()
            self.assertEqual(result["status"], "completed")
            self.assertEqual(self.driver.commands, [(0, 70)])
            self.assertEqual(self.driver.disable_calls, 0)
            rows = [json.loads(line) for line in (Path(directory)/"events.jsonl").read_text().splitlines()]
            retries = [row for row in rows if row["event"] == "state_snapshot_replace_retry"]
            self.assertTrue(retries)
            self.assertEqual(retries[0]["winerrors"][0], 32)
            self.assertTrue(all(row["recovered"] for row in retries))
            self.assertGreaterEqual(result["points"][0]["qualification_s"], 120)

    def test_persistent_running_snapshot_fault_cleans_without_next_point(self):
        original = self.archive.snapshot

        def failed_running(value):
            if value["status"] == "running":
                raise PermissionError("persistent snapshot fault")
            original(value)

        self.archive.snapshot = failed_running
        result = self.run_points([Point("20K", 70, hold_s=0), Point("40K", 110, hold_s=0)])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["completed_points"], 0)
        self.assertEqual(self.driver.commands, [(0, 70)])
        self.assertEqual(self.driver.disable_calls, 1)
        self.assertTrue(self.driver.closed)

    def test_close_failure_is_not_completed(self):
        self.setup_driver = lambda d: setattr(d, "close_error", True)
        result = self.run_points()
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any("close" in x for x in result["cleanup"]["errors"]))

    def test_new_point_must_fit_complete_budget(self):
        experiment = Experiment(config(), self.archive, monotonic=self.clock.monotonic,
            wall_time=self.clock.wall, sleep=self.clock.sleep, deadline_s=self.clock.wall()+200,
            cleanup_reserve=100)
        self.assertIsNone(experiment.run_point(Point("no budget", 70), 0))

    def test_original_criteria_are_explicit(self):
        self.assertEqual((CRITERIA.tolerance, CRITERIA.stable_range, CRITERIA.dwell_s), (0.1, 0.05, 120))

    def test_real_adapter_with_fake_dll_never_writes_field_or_other_devices(self):
        from tests.test_attodry import FakeAttoDryDll
        from attodry_control.config import load_temperature_operation_config
        dll = FakeAttoDryDll()
        dll.temperature_follows_setpoint = True
        c = load_temperature_operation_config("config/hardware.example.toml")
        with patch("attodry_control.attodry.load_attodry_dll", return_value=dll), patch("builtins.print"):
            result = execute(c, [Point("adapter", 3, hold_s=0)], self.archive,
                monotonic=self.clock.monotonic, wall_time=self.clock.wall, sleep=self.clock.sleep,
                deadline_s=self.clock.wall()+20000)
        self.assertEqual(result["status"], "completed", str(result))
        self.assertIn("set_temperature", dll.events)
        self.assertIn("get_sample_heater_power", dll.events)
        self.assertIn("disconnect", dll.events)
        self.assertFalse(any("set_field" in event or "toggle_field" in event for event in dll.events))


class PlanTests(unittest.TestCase):
    def test_invalid_modes_sequences_and_types_rejected(self):
        variants = [dict(method="magic"), dict(target_k=float("nan")), dict(target_k=True),
                    dict(max_wait_s=5401), dict(hold_s=-1), dict(unrecognized=1),
                    dict(method="sequence", commands=[{"after_s": 0, "target_k": 60}, {"after_s": 15, "target_k": 70}]),
                    dict(method="sequence", commands=[{"after_s": 0, "target_k": 60}, {"after_s": 30, "target_k": 69}])]
        for change in variants:
            with self.subTest(change=change), self.assertRaises(ValueError):
                Point.parse({"label": "test", "target_k": 70, **change})

    def test_atomic_archive_refuses_overwrite_and_preserves_run_context(self):
        with tempfile.TemporaryDirectory() as directory:
            a = Archive(directory, "fixed-run")
            a.emit("temperature_sample", actual_k=50)
            a.snapshot({"status": "running"})
            a.close()
            self.assertEqual(json.loads((Path(directory)/"state.json").read_text())["run_id"], "fixed-run")
            with self.assertRaises(FileExistsError):
                Archive(directory, "another-run")

    @unittest.skipUnless(os.name == "nt", "Windows file sharing regression")
    def test_snapshot_recovers_from_actual_windows_reader_without_delete_share(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Archive(directory, "fixed-run")
            archive.snapshot({"status": "before"})
            reader = (Path(directory)/"state.json").open("rb")
            try:
                with patch("attodry_control.temperature_experiment.time.sleep", side_effect=lambda _: reader.close()) as sleep:
                    archive.snapshot({"status": "after"})
                self.assertGreaterEqual(sleep.call_count, 1)
                self.assertLessEqual(sleep.call_count, 4)
                self.assertEqual(json.loads((Path(directory)/"state.json").read_text())["status"], "after")
                rows = [json.loads(line) for line in (Path(directory)/"events.jsonl").read_text().splitlines()]
                self.assertEqual(rows[-1]["event"], "state_snapshot_replace_retry")
                self.assertTrue(rows[-1]["recovered"])
                self.assertEqual(rows[-1]["attempts"], sleep.call_count+1)
            finally:
                reader.close()
                archive.close()

    def test_persistent_windows_snapshot_error_remains_bounded_and_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Archive(directory, "fixed-run")
            archive.snapshot({"status": "before"})
            error = PermissionError("simulated Windows sharing/permission error")
            error.winerror = 5
            try:
                with patch("attodry_control.temperature_experiment.os.replace", side_effect=error) as replace_file, patch("attodry_control.temperature_experiment.time.sleep") as sleep:
                    with self.assertRaises(PermissionError):
                        archive.snapshot({"status": "after"})
                self.assertEqual(replace_file.call_count, 5)
                self.assertAlmostEqual(sum(call.args[0] for call in sleep.call_args_list), 0.37)
                self.assertEqual(json.loads((Path(directory)/"state.json").read_text())["status"], "before")
                self.assertEqual(json.loads((Path(directory)/"state.pending.json").read_text())["status"], "after")
                rows = [json.loads(line) for line in (Path(directory)/"events.jsonl").read_text().splitlines()]
                self.assertFalse(rows[-1]["recovered"])
            finally:
                archive.close()

    def test_other_snapshot_errors_are_not_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Archive(directory, "fixed-run")
            try:
                with patch("attodry_control.temperature_experiment.os.replace", side_effect=OSError("disk fault")) as replace_file, patch("attodry_control.temperature_experiment.time.sleep") as sleep:
                    with self.assertRaisesRegex(OSError, "disk fault"):
                        archive.snapshot({"status": "after"})
                self.assertEqual(replace_file.call_count, 1)
                sleep.assert_not_called()
            finally:
                archive.close()

    def test_plan_checks_hashes_and_rejects_unvalidated_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            station = root/"station.toml"
            station.write_text("placeholder")
            source = root/"source.py"
            source.write_text("placeholder")
            plan = {"run_id": "test", "root": str(root), "station_config": str(station),
                "station_sha256": sha256(station), "output_directory": str(root/"run"),
                "deadline_utc": "2026-10-21T07:00:00+00:00", "code_hashes": {"source.py": sha256(source)},
                "points": [{"label": "20K", "target_k": 70}]}
            path = root/"plan.json"
            path.write_text(json.dumps(plan))
            with patch("attodry_control.temperature_experiment.load_temperature_operation_config", return_value=config()):
                parsed = load_plan(path, now=0)
                self.assertEqual(parsed[2][0].target_k, 70)
                source.write_text("changed")
                with self.assertRaisesRegex(ValueError, "code hash"):
                    load_plan(path, now=0)
                source.write_text("placeholder")
                evidence = root/"evidence.json"
                evidence.write_text(json.dumps({"independent_validation_passed": False}))
                plan["candidate_evidence"] = {"path": str(evidence), "sha256": sha256(evidence)}
                plan["points"] = [{"label": "20K", "target_k": 70, "method": "sequence", "commands":
                    [{"after_s": 0, "target_k": 60}, {"after_s": 180, "target_k": 70}]}]
                path.write_text(json.dumps(plan))
                with self.assertRaisesRegex(ValueError, "no independent validation"):
                    load_plan(path, now=0)


if __name__ == "__main__":
    unittest.main()
