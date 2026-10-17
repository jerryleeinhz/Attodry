"""Phase timing and mixed-fault boundaries with clocks and simulated stations."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from attodry_control.combination_scan import (
    AxisPoint, ScanAxis, CombinationPlan, SimulatedCombinationStation,
    run_simulated_combination,
)
from attodry_control.combination_store import CombinationStore
from attodry_control.measurement_timing import MeasurementTiming, TIMING_POLICY
from attodry_control.optical_recovery import OpticalPowerDeviation
from attodry_control.reference_transients import ReferenceTransientError
from tests.optical_helpers import Clock


def reference_error():
    return ReferenceTransientError("clock transient", kind="unlock", evidence={"raw": 8})


def power_error():
    return OpticalPowerDeviation("ordinary target drift", evidence={"actual_w": .001})


class StageTimingTests(unittest.TestCase):
    def test_nested_exclusive_time_does_not_double_count_child_or_audit(self):
        clock, events = Clock(), []
        def emit(kind, payload):
            clock.sleep(.5)
            events.append((kind, payload))
        timing = MeasurementTiming(emit, monotonic=clock)
        with timing.timed_stage("outer"):
            clock.sleep(1)
            timing.emit("raw", {})
            with timing.timed_stage("inner"):
                clock.sleep(2)
            clock.sleep(1)
        stages = [p for k, p in events if k == "measurement_stage_timing"]
        child, parent = stages
        self.assertEqual(child["parent_id"], parent["stage_id"])
        self.assertEqual(child["duration_s"], 2)
        self.assertEqual(parent["duration_s"], 5)
        self.assertEqual(parent["children_duration_s"], 2)
        self.assertEqual(parent["audit_duration_s"], 1)
        self.assertEqual(parent["exclusive_duration_s"], 2)
        summary = timing.summary()
        self.assertEqual(summary["elapsed_s"], 5.5)
        self.assertEqual(summary["audit_duration_s"], 1.5)
        self.assertEqual(summary["exclusive_stage_duration_s"], 4)
        self.assertEqual(summary["unattributed_duration_s"], 0)
        self.assertEqual(len({p["stage_id"] for p in stages}), 2)

    def test_failure_and_interrupt_have_end_and_incomplete_marker(self):
        for error in (OSError("device"), KeyboardInterrupt("operator")):
            clock, events = Clock(), []
            timing = MeasurementTiming(lambda k, p: events.append((k, p)), monotonic=clock)
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)):
                with timing.timed_stage("sample"):
                    clock.sleep(3)
                    raise error
            stage = events[-1][1]
            self.assertFalse(stage["completed"])
            self.assertEqual(stage["duration_s"], 3)
            self.assertIn(type(error).__name__, stage["error"])
            self.assertEqual(stage["policy"], TIMING_POLICY)
            self.assertTrue(stage["started_at_utc"] and stage["finished_at_utc"])

    def test_timing_audit_failure_preserves_primary_and_accounts_failed_callback(self):
        clock = Clock()
        def emit(*_):
            clock.sleep(.5)
            raise OSError("audit disk")
        timing = MeasurementTiming(emit, monotonic=clock)
        with self.assertRaisesRegex(ValueError, "primary instrument") as caught:
            with timing.timed_stage("read"):
                clock.sleep(2)
                raise ValueError("primary instrument")
        self.assertIn("audit disk", str(caught.exception.__notes__))
        self.assertEqual(timing.audit_duration_s, .5)
        self.assertEqual(timing.summary()["stages"]["read"]["failed"], 1)
        self.assertEqual(len(timing.audit_errors), 1)
        with self.assertRaisesRegex(OSError, "audit disk"):
            with timing.timed_stage("cleanup"):
                clock.sleep(1)


class TimedStation(SimulatedCombinationStation):
    def __init__(self, clock):
        super().__init__()
        self.monotonic = clock
        self.read_error = None
        self.after_error = None
        self.retries = 0

    def open(self, modules):
        self.monotonic.sleep(2)
        return super().open(modules)

    def apply(self, module, point):
        self.monotonic.sleep(1)
        return super().apply(module, point)

    def qualify_axis(self, module, point, scope):
        self.monotonic.sleep(3)
        return super().qualify_axis(module, point, scope)

    def qualify(self, modules):
        self.monotonic.sleep(4)
        return super().qualify(modules)

    def begin_sample(self):
        self.monotonic.sleep(.5)

    def read(self, module):
        self.monotonic.sleep(5 if module == "smu" else 6)
        if self.read_error is not None:
            raise self.read_error
        return super().read(module)

    def end_sample(self, reads):
        self.monotonic.sleep(.5)
        if self.after_error is not None:
            raise self.after_error

    def run_sample_operation(self, operation):
        try:
            return operation()
        except (ReferenceTransientError, OpticalPowerDeviation):
            self.retries += 1
            self.read_error = self.after_error = None
            return operation()

    def cleanup(self, module, failed):
        self.monotonic.sleep(7 if module == "smu" else 8)
        return super().cleanup(module, failed)

    def close(self):
        self.monotonic.sleep(9)
        super().close()


class CombinationTimingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "timing.sqlite"
        self.clock = Clock()
        self.station = TimedStation(self.clock)
        self.plan = CombinationPlan((
            ScanAxis("smu", (AxisPoint({"smu_bias_v": .1}),)),
            ScanAxis("lockin", (AxisPoint({"lockin_excitation_v_rms": .004,
                                          "lockin_frequency_hz": 17}),)),
        ))

    def execute(self, *, event_failure=None, run_id="timing"):
        with CombinationStore(self.path) as store:
            original = store.event
            def emit(run, kind, payload):
                if event_failure is not None and event_failure(kind, payload):
                    raise OSError("injected timing audit failure")
                return original(run, kind, payload)
            with patch.object(store, "event", emit):
                result = run_simulated_combination(self.plan, store, run_id, station=self.station)
            events = [(r[0], json.loads(r[1])) for r in store.connection.execute(
                "SELECT event_type,payload_json FROM combination_events WHERE run_id=? ORDER BY event_id", (run_id,))]
        return result, events

    def test_all_phases_and_per_module_reads_use_station_monotonic(self):
        result, events = self.execute()
        self.assertEqual(result["status"], "completed")
        stages = [p for k, p in events if k == "measurement_stage_timing"]
        expected = {"preflight", "axis_apply", "axis_qualification", "condition_qualification",
                    "sample", "sample_window", "sample_store", "module_read", "cleanup", "close"}
        self.assertEqual({p["stage"] for p in stages}, expected)
        self.assertTrue(all(p["completed"] for p in stages))
        sample = next(p for p in stages if p["stage"] == "sample")
        window = next(p for p in stages if p["stage"] == "sample_window")
        reads = [p for p in stages if p["stage"] == "module_read"]
        self.assertEqual(window["parent_id"], sample["stage_id"])
        self.assertTrue(all(p["parent_id"] == window["stage_id"] for p in reads))
        self.assertEqual([p["duration_s"] for p in reads], [5, 6])
        self.assertEqual(window["exclusive_duration_s"], 1)
        summary = next(p for k, p in events if k == "measurement_timing_summary")
        self.assertEqual(summary["elapsed_s"], 50)
        self.assertEqual(summary["exclusive_stage_duration_s"], 50)
        self.assertEqual(summary["unattributed_duration_s"], 0)

    def test_failed_timing_event_never_prevents_cleanup_or_close(self):
        result, _ = self.execute(event_failure=lambda k, p: k == "measurement_stage_timing")
        self.assertEqual(result["status"], "failed")
        self.assertTrue(self.station.closed)
        self.assertEqual([op[1] for op in self.station.operations if op[0] == "cleanup"],
                         ["lockin", "smu"])
        self.assertEqual(len(result["cleanup"]["actions"]), 2)
        self.assertTrue(any("timing audit" in error for error in result["cleanup"]["errors"]))

    def test_primary_read_fault_survives_timing_and_after_read_faults(self):
        self.station.read_error = OSError("primary wire open")
        self.station.after_error = power_error()
        result, _ = self.execute(event_failure=lambda k, p:
            k == "measurement_stage_timing" and p["stage"] == "module_read")
        self.assertEqual(result["error"], "OSError: primary wire open")
        self.assertEqual(self.station.retries, 0)
        self.assertTrue(self.station.closed)

    def test_retryable_read_cannot_hide_after_read_hard_or_mixed_fault(self):
        pairs = [(power_error(), OSError("after-read transport")),
                 (reference_error(), OSError("after-read transport")),
                 (power_error(), reference_error()), (reference_error(), power_error())]
        for index, (read_error, after_error) in enumerate(pairs):
            self.station = TimedStation(self.clock)
            self.station.read_error, self.station.after_error = read_error, after_error
            result, events = self.execute(run_id=str(index))
            self.assertEqual(result["status"], "failed")
            self.assertEqual(self.station.retries, 0)
            self.assertTrue(self.station.closed)
            failed = [p for k, p in events if k == "measurement_stage_timing" and not p["completed"]]
            self.assertTrue(failed)
            if isinstance(after_error, OSError):
                self.assertEqual(result["error"], "OSError: after-read transport")
            else:
                self.assertIn("Mixed reference and optical", result["error"])

    def test_same_category_is_retryable_but_timing_audit_failure_is_not(self):
        for index, factory in enumerate((power_error, reference_error)):
            self.station = TimedStation(self.clock)
            self.station.read_error, self.station.after_error = factory(), factory()
            result, _ = self.execute(run_id="pure-" + str(index))
            self.assertEqual(result["status"], "completed")
            self.assertEqual(self.station.retries, 1)
            self.station = TimedStation(self.clock)
            self.station.read_error = factory()
            result, _ = self.execute(run_id="audit-" + str(index), event_failure=lambda k, p:
                k == "measurement_stage_timing" and p["stage"] == "sample_window")
            self.assertEqual(result["status"], "failed")
            self.assertEqual(self.station.retries, 0)
            self.assertIn("timing audit failed", result["error"])


if __name__ == "__main__":
    unittest.main()
