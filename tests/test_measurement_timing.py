"""Offline timing/phase evidence must not alter acquisition or cleanup actions."""
from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from attodry_control.measurement_timing import MeasurementTiming
from attodry_control.combination_scan import (
    AxisPoint, ScanAxis, CombinationPlan, SimulatedCombinationStation,
    run_simulated_combination,
)
from attodry_control.combination_store import CombinationStore, run_snapshot
from tests.test_lockin_settling import Clock


class TimingTests(unittest.TestCase):
    def test_nested_stages_exclude_children_and_audit_from_sum(self):
        clock, events = Clock(), []
        def emit(kind, payload):
            clock.sleep(.5)
            events.append((kind, payload))
        timing = MeasurementTiming(emit, monotonic=clock.monotonic)
        with timing.timed_stage("parent"):
            clock.sleep(2)
            timing.emit("raw", {})
            with timing.timed_stage("child"):
                clock.sleep(3)
        frames = [p for k, p in events if k == "measurement_stage_timing"]
        child, parent = frames
        self.assertEqual(child["parent_id"], parent["stage_id"])
        self.assertEqual(parent["duration_s"], 6)
        self.assertEqual(parent["exclusive_duration_s"], 2)
        summary = timing.summary()
        self.assertEqual(summary["elapsed_s"], 6.5)
        self.assertEqual(summary["exclusive_stage_duration_s"], 5)
        self.assertEqual(summary["audit_duration_s"], 1.5)
        self.assertEqual(summary["unattributed_duration_s"], 0)

    def test_interrupted_stage_has_end_and_never_claims_completion(self):
        for error in (OSError("wire"), KeyboardInterrupt("operator")):
            clock, events = Clock(), []
            timing = MeasurementTiming(lambda k, p: events.append((k, p)),
                                       monotonic=clock.monotonic)
            with self.assertRaises(type(error)):
                with timing.timed_stage("read"):
                    clock.sleep(2)
                    raise error
            frame = events[0][1]
            self.assertFalse(frame["completed"])
            self.assertEqual(frame["duration_s"], 2)
            self.assertIn(type(error).__name__, frame["error"])
            self.assertEqual(timing.summary()["stages"]["read"]["failed"], 1)

    def test_failed_audit_does_not_replace_primary_error(self):
        def fail(*_):
            raise OSError("disk")
        timing = MeasurementTiming(fail)
        with self.assertRaisesRegex(ValueError, "primary") as caught:
            with timing.timed_stage("read"):
                raise ValueError("primary")
        self.assertIn("disk", str(caught.exception.__notes__))
        self.assertEqual(len(timing.audit_errors), 1)


class TimedStation(SimulatedCombinationStation):
    def __init__(self, clock, failure=None):
        super().__init__()
        self.clock, self.failure = clock, failure
        self.monotonic = clock.monotonic

    def open(self, modules):
        self.clock.sleep(3)
        return super().open(modules)

    def apply(self, module, point):
        self.clock.sleep(2)
        return super().apply(module, point)

    def qualify_axis(self, module, point, scope):
        self.clock.sleep(5)
        return super().qualify_axis(module, point, scope)

    def qualify(self, modules):
        self.clock.sleep(7)
        return super().qualify(modules)

    def begin_sample(self):
        self.clock.sleep(1)

    def end_sample(self, reads):
        self.clock.sleep(1)

    def read(self, module):
        self.clock.sleep(4)
        if self.failure is not None:
            raise self.failure
        return super().read(module)

    def cleanup(self, module, failed):
        self.clock.sleep(2)
        return super().cleanup(module, failed)

    def close(self):
        self.clock.sleep(1)
        super().close()


class CoordinatorTimingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name)/"scan.sqlite"
        self.plan = CombinationPlan((ScanAxis("smu", (AxisPoint({"smu_bias_v": .01}),)),
            ScanAxis("lockin", (AxisPoint({"lockin_excitation_v_rms": .004,
                                         "lockin_frequency_hz": 17.}),))))

    def execute(self, *, failure=None, audit_failure=None, run_id="test"):
        clock = Clock()
        station = TimedStation(clock, failure)
        with CombinationStore(self.database) as store:
            original = store.event
            def emit(run, kind, payload):
                if audit_failure and audit_failure(kind, payload):
                    raise OSError("timing disk failure")
                return original(run, kind, payload)
            with patch.object(store, "event", emit):
                result = run_simulated_combination(self.plan, store, run_id, station=station)
            events = [(r["event_type"], json.loads(r["payload_json"])) for r in
                      store.connection.execute("SELECT * FROM combination_events WHERE run_id=? "
                                               "ORDER BY event_id", (run_id,))]
        return result, station, clock, events

    def test_actual_stage_times_use_station_clock_and_sequential_reads(self):
        result, station, clock, events = self.execute()
        self.assertEqual(result["status"], "completed", result)
        frames = [p for k, p in events if k == "measurement_stage_timing"]
        self.assertEqual([p["duration_s"] for p in frames if p["stage"] == "module_read"], [4, 4])
        self.assertEqual(next(p for p in frames if p["stage"] == "sample_window")["duration_s"], 10)
        self.assertEqual([op[1] for op in station.operations if op[0] == "read"], ["smu", "lockin"])
        summary = next(p for k, p in events if k == "measurement_timing_summary")
        self.assertEqual(summary["elapsed_s"], clock.now)
        self.assertEqual(summary["exclusive_stage_duration_s"], clock.now)
        self.assertEqual(summary["unattributed_duration_s"], 0)
        phases = [p for k, p in events if k == "scan_phase"]
        self.assertEqual(phases[0]["phase"], "preflight")
        self.assertEqual(phases[-1]["phase"], "close")
        self.assertTrue(any(p["phase"] == "axis_apply" and p["module"] == "smu" for p in phases))
        self.assertTrue(all(p["condition_id"] == "condition-000000" for p in phases
                            if p["phase"] == "formal_sampling"))

    def test_timing_does_not_add_hardware_actions_or_waits(self):
        class NoTiming:
            def __init__(self, event, **kwargs):
                self.emit, self.audit_errors = event, []
            @contextmanager
            def timed_stage(self, *args, **kwargs):
                yield {}
            def summary(self):
                return {}
        baseline = self.execute(run_id="baseline")
        with patch("attodry_control.combination_scan.MeasurementTiming", NoTiming):
            disabled = self.execute(run_id="disabled")
        self.assertEqual(baseline[1].operations, disabled[1].operations)
        self.assertEqual(baseline[2].sleeps, disabled[2].sleeps)
        self.assertEqual(baseline[0]["status"], disabled[0]["status"])

    def test_timing_event_failure_preserves_raw_and_all_cleanup_actions(self):
        result, station, _, events = self.execute(audit_failure=lambda k, p:
            k == "measurement_stage_timing" and p["stage"] == "module_read")
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["cleanup"]["manual_verification_required"])
        self.assertEqual([op[1] for op in station.operations if op[0] == "cleanup"], ["lockin", "smu"])
        self.assertTrue(station.closed)
        self.assertEqual(sum(k == "raw_reading" for k, _ in events), 1)
        self.assertEqual(run_snapshot(self.database, "test")["accepted_conditions"], 0)

    def test_primary_read_failure_survives_timing_and_phase_audit_failure(self):
        result, station, _, _ = self.execute(failure=ValueError("primary wire"),
            audit_failure=lambda k, p: k == "measurement_stage_timing" and p["stage"] == "module_read")
        self.assertIn("primary wire", result["error"])
        self.assertTrue(station.closed)
        self.assertEqual(sum(op[0] == "cleanup" for op in station.operations), 2)

    def test_cleanup_phase_audit_failure_never_skips_cleanup_or_close(self):
        result, station, _, _ = self.execute(audit_failure=lambda k, p:
            k == "scan_phase" and p["phase"] in {"cleanup", "close"})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(sum(op[0] == "cleanup" for op in station.operations), 2)
        self.assertTrue(station.closed)


class WaitPhaseTests(unittest.TestCase):
    def test_wait_markers_restore_phase_and_do_not_change_sleep(self):
        from attodry_control.combination_hardware import HardwareCombinationStation, _Events
        from attodry_control.lockin_points import LockinPointSession
        clock, events = Clock(), []
        station = object.__new__(HardwareCombinationStation)
        station.events = _Events(lambda k, p: events.append((k, p)))
        station.set_monitor_phase("formal_sampling", module="lockin", sample_index=2)
        session = object.__new__(LockinPointSession)
        session.base_sleep = clock.sleep
        session.monitor_wait_context = station._monitor_lockin_wait
        session._wait(3)
        session._wait(0)
        self.assertEqual(clock.sleeps, [3, 0])
        self.assertEqual([p["phase"] for k, p in events],
                         ["formal_sampling", "lockin_wait", "formal_sampling"])
        self.assertEqual(station.events.monitor_phase["sample_index"], 2)

    def test_interrupted_wait_does_not_claim_sampling_resumed(self):
        from attodry_control.combination_hardware import HardwareCombinationStation, _Events
        station = object.__new__(HardwareCombinationStation)
        station.events = _Events(lambda *_: None)
        station.set_monitor_phase("formal_sampling", module="lockin")
        with self.assertRaises(KeyboardInterrupt):
            with station._monitor_lockin_wait():
                raise KeyboardInterrupt()
        self.assertEqual(station.events.monitor_phase["phase"], "lockin_wait")
