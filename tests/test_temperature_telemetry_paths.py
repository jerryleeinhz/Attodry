"""Offline four-channel logging through existing standalone acquisition paths."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from attodry_control.attodry import AttoDryError
from attodry_control.config import load_temperature_operation_config
from attodry_control.temperature_excitation_scan import _temperature_measurement_context
from attodry_control.temperature_run import run as run_temperature
from attodry_control.temperature_scan import (
    _attach_temperature_telemetry, _wait_resume_recheck, run as run_scan,
    _JsonlWriter,
)
from tests import test_attodry as fixtures
from tests.test_attodry import StepClock


class MemoryWriter:
    def __init__(self):
        self.events = []

    def append(self, event):
        self.events.append(event)


class StandaloneTemperatureTelemetryTests(unittest.TestCase):
    setUp = fixtures.AttoDryDriverTests.setUp
    connect = fixtures.AttoDryDriverTests.connect
    temperature_scan_config = fixtures.AttoDryDriverTests.temperature_scan_config
    temperature_run_config = fixtures.AttoDryDriverTests.temperature_run_config

    def read_events(self, output):
        summary = json.loads(output.getvalue())
        events = [json.loads(line) for line in
                  Path(summary["progress_jsonl"]).read_text(encoding="utf-8").splitlines()]
        return summary, events

    def test_standalone_scan_links_raw_samples_and_fresh_four_channel_events(self):
        config_path, _ = self.temperature_scan_config(start_k=1.7, stop_k=1.7)
        self.dll.temperature_follows_setpoint = True
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(run_scan(
                ["--config", str(config_path), "--authorize-temperature-scan"],
                dll_loader=lambda _: self.dll, monotonic=StepClock(),
                sleeper=lambda _: None, wall_time=lambda: 123.0,
            ), 0)
        summary, events = self.read_events(output)
        completed = {event["sample_id"]: event for event in events
                     if event["event"] == "temperature_telemetry"}
        raw = [event for event in events if event["event"] == "temperature_sample"]
        self.assertTrue(raw)
        for sample in raw:
            self.assertEqual(sample["heater_power"], {"sample_w": None, "vti_w": None})
            telemetry = completed[sample["sample_id"]]
            self.assertEqual(telemetry["heater_power"], {"sample_w": .25, "vti_w": .5})
            self.assertTrue(telemetry["telemetry_complete"])
            self.assertEqual(telemetry["state"], sample["state"])
            self.assertLessEqual(sample["state_read_timing"]["finished_monotonic_s"],
                                 telemetry["heater_power_read_timing"]["started_monotonic_s"])
        self.assertTrue(summary["final_temperature_telemetry"]["telemetry_complete"])

    def test_daily_run_persists_samples_before_final_stdout(self):
        config_path = self.temperature_run_config(pre_measure_wait_s=2.0)
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(run_temperature(
                ["--config", str(config_path)], dll_loader=lambda _: self.dll,
                monotonic=StepClock(), sleeper=lambda _: None, wall_time=lambda: 123.0,
            ), 0)
        summary, events = self.read_events(output)
        self.addCleanup(Path(summary["progress_jsonl"]).unlink, missing_ok=True)
        self.assertEqual(events[0]["event"], "temperature_run_started")
        self.assertEqual(events[-1]["event"], "temperature_run_finished")
        self.assertTrue(events[-1]["completed"])
        for sample in summary["temperature_samples"]:
            self.assertTrue(sample["telemetry_complete"])
            self.assertEqual(sample["heater_power"], {"sample_w": .25, "vti_w": .5})
        self.assertEqual(sum(event["event"] == "temperature_sample" for event in events),
                         len(summary["temperature_samples"]))

    def test_power_query_interrupt_preserves_partial_and_rechecks_before_continuing(self):
        for policy in ("continue", "wait-confirmation"):
            with self.subTest(policy=policy):
                self.dll = fixtures.FakeAttoDryDll()
                config_path = self.temperature_run_config(pre_measure_wait_s=2.0)
                text = config_path.read_text(encoding="utf-8").replace(
                    'interrupt_policy = "abort"', f'interrupt_policy = "{policy}"',
                ).replace("resume_recheck_s = 30.0", "resume_recheck_s = 8.0")
                config_path.write_text(text, encoding="utf-8")
                original_power_getter = self.dll.AttoDRY_Interface_getVtiHeaterPower
                power_calls = 0

                def interrupt_once(pointer):
                    nonlocal power_calls
                    power_calls += 1
                    # First call belongs to preflight, second to conditioning.
                    if power_calls == 2:
                        raise KeyboardInterrupt
                    return original_power_getter(pointer)

                self.dll.AttoDRY_Interface_getVtiHeaterPower = interrupt_once
                now = [0.0]
                pauses = []
                confirmations = []

                def sleep(seconds):
                    pauses.append(seconds)
                    now[0] += seconds

                def confirm(prompt):
                    confirmations.append(prompt)
                    # Time waiting for the operator must not replace fresh recheck.
                    now[0] += 20.0
                    return "yes"

                output = io.StringIO()
                with redirect_stdout(output):
                    self.assertEqual(run_temperature(
                        ["--config", str(config_path)], dll_loader=lambda _: self.dll,
                        monotonic=lambda: now[0], sleeper=sleep,
                        wall_time=lambda: 123.0, confirmation=confirm,
                    ), 0)
                summary, events = self.read_events(output)
                self.addCleanup(Path(summary["progress_jsonl"]).unlink, missing_ok=True)
                partial = [event for event in events
                           if event["event"] == "temperature_telemetry"
                           and "KeyboardInterrupt" in event.get("heater_power_read_error", "")]
                self.assertEqual(len(partial), 1)
                self.assertEqual(partial[0]["heater_power"], {"sample_w": .25, "vti_w": None})
                self.assertFalse(partial[0]["telemetry_complete"])
                self.assertEqual(partial[0]["sample_id"], summary["temperature_samples"][0]["sample_id"])
                self.assertTrue(summary["completed"])
                self.assertEqual(summary["recovery_actions"], [])
                self.assertEqual(summary["interruptions"][0]["action"],
                                 "continue" if policy == "continue" else "continue-after-confirmation")
                self.assertGreaterEqual(sum(pauses), 8.0)
                self.assertGreaterEqual(summary["temperature_samples"][-1]["elapsed_s"], 8.0)
                self.assertTrue(summary["temperature_samples"][-1]["telemetry_complete"])
                self.assertEqual(len(confirmations), int(policy == "wait-confirmation"))

    def test_formal_unsafe_temperature_is_durable_before_rejection_without_power_read(self):
        config_path = self.temperature_run_config(target_k=2.1)
        request = load_temperature_operation_config(config_path).temperature_run
        self.connect()
        self.addCleanup(self.driver.close)
        self.dll.temperature_control = 1
        self.dll.user_temperature_k = 2.1
        self.dll.sample_temperature_k = 2.31
        writer = MemoryWriter()
        _attach_temperature_telemetry(self.driver, writer)
        capture = _temperature_measurement_context(
            self.driver, request=request, target_k=2.1, temperature_index=0,
            writer=writer, monotonic=StepClock(), wall_time=lambda: 123.0,
        )
        self.dll.events.clear()
        with self.assertRaisesRegex(AttoDryError, "overshoot"):
            capture("before", 2, 0)
        self.assertEqual(len(writer.events), 1)
        raw = writer.events[0]
        self.assertEqual(raw["event"], "measurement_temperature_sample")
        self.assertAlmostEqual(raw["state"]["sample_temperature_k"], 2.31, places=5)
        self.assertFalse(raw["telemetry_complete"])
        self.assertNotIn("get_sample_heater_power", self.dll.events)
        self.assertNotIn("get_vti_heater_power", self.dll.events)

    def test_daily_overshoot_disables_before_optional_power_diagnostic(self):
        config_path = self.temperature_run_config(target_k=2.1, pre_measure_wait_s=2.0)
        self.dll.sample_temperature_k = 2.31
        output = io.StringIO()
        unsafe_event_offsets = []
        original_append = _JsonlWriter.append

        def append(writer, event):
            if event.get("event") == "temperature_sample":
                unsafe_event_offsets.append(len(self.dll.events))
            original_append(writer, event)

        with patch.object(_JsonlWriter, "append", append), redirect_stdout(output), \
                self.assertRaisesRegex(AttoDryError, "overshoot"):
            run_temperature(["--config", str(config_path)], dll_loader=lambda _: self.dll,
                            monotonic=StepClock(), sleeper=lambda _: None,
                            wall_time=lambda: 123.0)
        summary, events = self.read_events(output)
        self.addCleanup(Path(summary["progress_jsonl"]).unlink, missing_ok=True)
        after_unsafe = self.dll.events[unsafe_event_offsets[-1]:]
        self.assertLess(after_unsafe.index("toggle_temperature_control"),
                        after_unsafe.index("get_sample_heater_power"))
        raw = [event for event in events if event["event"] == "temperature_sample"][-1]
        completed_ids = {event["sample_id"] for event in events
                         if event["event"] == "temperature_telemetry"}
        self.assertNotIn(raw["sample_id"], completed_ids)
        self.assertFalse(summary["failure_diagnostic"]["heater_power_state"]["temperature_control_enabled"])
        self.assertTrue(summary["failure_diagnostic"]["trigger_state"]["temperature_control_enabled"])

    def test_formal_valid_temperature_returns_completed_four_channel_context(self):
        config_path = self.temperature_run_config(target_k=2.0)
        request = load_temperature_operation_config(config_path).temperature_run
        self.connect()
        self.addCleanup(self.driver.close)
        self.dll.temperature_control = 1
        self.dll.user_temperature_k = 2.0
        writer = MemoryWriter()
        _attach_temperature_telemetry(self.driver, writer)
        capture = _temperature_measurement_context(
            self.driver, request=request, target_k=2.0, temperature_index=0,
            writer=writer, monotonic=StepClock(), wall_time=lambda: 123.0,
        )
        sample = capture("after", 2, 0)
        self.assertTrue(sample["telemetry_complete"])
        self.assertEqual(sample["heater_power"], {"sample_w": .25, "vti_w": .5})
        self.assertEqual([event["event"] for event in writer.events],
                         ["measurement_temperature_sample", "temperature_telemetry"])
        self.assertEqual(writer.events[0]["sample_id"], writer.events[1]["sample_id"])

    def test_partial_power_error_is_recorded_without_stale_vti_or_setting_write(self):
        config_path, _ = self.temperature_scan_config(start_k=1.7, stop_k=1.7)
        self.dll.return_codes["get_vti_heater_power"] = 6
        output = io.StringIO()
        with redirect_stdout(output), self.assertRaises(AttoDryError):
            run_scan(["--config", str(config_path), "--authorize-temperature-scan"],
                     dll_loader=lambda _: self.dll, monotonic=StepClock(),
                     sleeper=lambda _: None, wall_time=lambda: 123.0)
        summary, events = self.read_events(output)
        failed = [event for event in events if event["event"] == "temperature_telemetry"]
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["heater_power"], {"sample_w": .25, "vti_w": None})
        self.assertFalse(failed[0]["telemetry_complete"])
        self.assertIn("heater_power_read_error", failed[0])
        self.assertFalse(summary["completed"])
        self.assertNotIn("set_temperature", self.dll.events)
        self.assertTrue(summary["disconnected"])

    def test_resume_recheck_uses_same_four_channel_audit(self):
        config_path = self.temperature_run_config(target_k=2.0)
        request = load_temperature_operation_config(config_path).temperature_run
        self.connect()
        self.addCleanup(self.driver.close)
        self.dll.temperature_control = 1
        self.dll.user_temperature_k = 2.0
        writer = MemoryWriter()
        _attach_temperature_telemetry(self.driver, writer)
        _wait_resume_recheck(self.driver, request, point_index=0, attempt_index=1,
                             writer=writer, monotonic=StepClock(step_s=60),
                             sleeper=lambda _: None, wall_time=lambda: 123.0)
        raw = [event for event in writer.events if event["event"] == "temperature_sample"]
        telemetry = [event for event in writer.events if event["event"] == "temperature_telemetry"]
        self.assertTrue(raw)
        self.assertEqual(len(raw), len(telemetry))
        self.assertEqual(raw[0]["phase"], "resume_recheck")
        self.assertEqual(raw[0]["sample_id"], telemetry[0]["sample_id"])


if __name__ == "__main__":
    unittest.main()
