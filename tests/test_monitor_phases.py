"""Explicit monitor phases with SQLite, fake instruments and fake clocks only."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from attodry_control.combination_hardware import HardwareCombinationStation, _Events
from attodry_control.combination_scan import (
    AxisPoint, CombinationPlan, ScanAxis, SimulatedCombinationStation,
    run_simulated_combination,
)
from attodry_control.combination_store import CombinationStore
from attodry_control.internal_lockin_group import InternalLockinGroupSession
from attodry_control.lockin_points import LockinPointSession
from attodry_control.photonics_lockin_points import PhotonicsLockinPointSession
from tests.optical_helpers import Clock


class MonitorPhaseEngineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "phases.sqlite"
        self.plan = CombinationPlan((
            ScanAxis("temperature", (AxisPoint({"temperature_k": 4.2}),)),
            ScanAxis("lockin", (AxisPoint({"lockin_excitation_v_rms": .004,
                                           "lockin_frequency_hz": 17.0}),))),
            samples_per_condition=2)

    def test_markers_are_committed_before_blocking_operations_and_have_context(self):
        checks = []
        with CombinationStore(self.path) as store:
            def expect(phase, module=None, sample_index=None):
                row = store.connection.execute("SELECT payload_json FROM combination_events "
                    "WHERE event_type='scan_phase' ORDER BY event_id DESC LIMIT 1").fetchone()
                payload = json.loads(row[0])
                self.assertEqual(payload["phase"], phase)
                if module is not None:
                    self.assertEqual(payload["module"], module)
                if phase != "preflight":
                    self.assertEqual(payload["condition_id"], "condition-000000")
                    self.assertEqual(payload["attempt_index"], 0)
                if sample_index is not None:
                    self.assertEqual(payload["sample_index"], sample_index)
                checks.append(phase)

            class Checked(SimulatedCombinationStation):
                def open(self, modules):
                    expect("preflight")
                    return super().open(modules)
                def apply(self, module, point):
                    expect("axis_apply", module)
                    return super().apply(module, point)
                def qualify_axis(self, module, point, scope):
                    expect("axis_qualification", module)
                    return super().qualify_axis(module, point, scope)
                def read(self, module):
                    count = sum(op[:2] == ("read", module) for op in self.operations)
                    expect("formal_sampling", module, count)
                    return super().read(module)
                def cleanup(self, module, failed):
                    # Successful runs clear condition context before cleanup.
                    row = store.connection.execute("SELECT payload_json FROM combination_events "
                        "WHERE event_type='scan_phase' ORDER BY event_id DESC LIMIT 1").fetchone()
                    payload = json.loads(row[0])
                    self_test.assertEqual(payload, {"phase": "cleanup", "module": module})
                    return super().cleanup(module, failed)
            self_test = self
            result = run_simulated_combination(self.plan, store, "phases", station=Checked())
            self.assertEqual(result["status"], "completed", result)
            payloads = [json.loads(row[0]) for row in store.connection.execute(
                "SELECT payload_json FROM combination_events WHERE event_type='scan_phase'")]
        self.assertEqual(checks.count("formal_sampling"), 4)
        self.assertEqual(sum(p["phase"] == "condition_complete" for p in payloads), 1)

    def test_failure_and_keyboard_interrupt_enter_cleanup_without_resuming_sampling(self):
        for index, error in enumerate((OSError("communication"), KeyboardInterrupt("operator"))):
            class Broken(SimulatedCombinationStation):
                def read(self, module):
                    raise error
            station = Broken()
            with self.subTest(error=type(error).__name__), CombinationStore(self.path) as store:
                result = run_simulated_combination(self.plan, store, str(index), station=station)
                phases = [json.loads(row[0])["phase"] for row in store.connection.execute(
                    "SELECT payload_json FROM combination_events WHERE run_id=? AND event_type='scan_phase'",
                    (str(index),))]
                self.assertEqual(result["status"], "interrupted" if index else "failed")
                self.assertNotIn("condition_complete", phases)
                self.assertEqual(phases[-2:], ["cleanup", "cleanup"])
                self.assertTrue(station.closed)
                self.assertEqual(sum(op[0] == "cleanup" for op in station.operations), 2)

    def test_phase_audit_failure_before_preflight_blocks_open_but_still_cleans_every_module(self):
        station = SimulatedCombinationStation()
        with CombinationStore(self.path) as store:
            original = store.event
            def fail_phase(run, kind, payload):
                if kind == "scan_phase":
                    raise OSError("phase audit unavailable")
                return original(run, kind, payload)
            with patch.object(store, "event", side_effect=fail_phase):
                result = run_simulated_combination(self.plan, store, "disk", station=station)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(any(op[0] == "open" for op in station.operations))
        self.assertEqual([op[1] for op in station.operations if op[0] == "cleanup"],
                         ["lockin", "temperature"])
        self.assertTrue(station.closed)
        self.assertTrue(any("cleanup phase audit" in item for item in result["cleanup"]["errors"]))

    def test_cleanup_marker_failure_cannot_prevent_hardware_cleanup_or_close(self):
        station = SimulatedCombinationStation()
        with CombinationStore(self.path) as store:
            original = store.event
            def fail_cleanup_marker(run, kind, payload):
                if kind == "scan_phase" and payload["phase"] == "cleanup":
                    raise OSError("cleanup marker unavailable")
                return original(run, kind, payload)
            with patch.object(store, "event", side_effect=fail_cleanup_marker):
                result = run_simulated_combination(self.plan, store, "cleanup-disk", station=station)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(station.closed)
        self.assertEqual(sum(op[0] == "cleanup" for op in station.operations), 2)


class MonitorPhaseWaitTests(unittest.TestCase):
    def station(self):
        station = object.__new__(HardwareCombinationStation)
        events = []
        station.events = _Events(lambda kind, payload: events.append((kind, payload)))
        return station, events

    def test_actual_positive_wait_restores_dark_and_formal_context_without_io_changes(self):
        for method in (LockinPointSession._wait, PhotonicsLockinPointSession._wait,
                       InternalLockinGroupSession._wait):
            for phase in ("dark_acquisition", "formal_sampling"):
                with self.subTest(method=method.__qualname__, phase=phase):
                    station, events = self.station()
                    station.set_monitor_phase(phase, module="lockin", sample_index=1)
                    clock, calls = Clock(), []
                    session = SimpleNamespace(clock=clock, sleep=lambda seconds: (
                        calls.append(seconds), clock.sleep(seconds)), wait_deadline=None,
                        wait_guard=None, _reference_operation_context=None,
                        monitor_wait_context=station._monitor_lockin_wait)
                    session._check_wait = lambda: None
                    method(session, 2.5)
                    self.assertEqual(calls, [2.5])
                    self.assertEqual(clock(), 2.5)
                    phases = [p for k, p in events if k == "scan_phase"]
                    self.assertEqual([p["phase"] for p in phases],
                                     [phase, "lockin_settling", phase])
                    self.assertEqual([p.get("sample_index") for p in phases], [1, 1, 1])

    def test_zero_wait_has_no_settling_marker(self):
        station, events = self.station()
        station.set_monitor_phase("formal_sampling", sample_index=0)
        clock = Clock()
        calls = []
        session = SimpleNamespace(clock=clock, sleep=calls.append, wait_deadline=None,
            wait_guard=None, monitor_wait_context=station._monitor_lockin_wait,
            _check_wait=lambda: None)
        LockinPointSession._wait(session, 0)
        self.assertEqual([p["phase"] for k, p in events], ["formal_sampling"])
        self.assertEqual(calls, [0])  # Preserve the existing unmonitored zero-wait behavior.

    def test_failed_wait_keeps_settling_until_cleanup_and_preserves_exception(self):
        for error in (OSError("device"), KeyboardInterrupt("operator")):
            station, events = self.station()
            station.set_monitor_phase("dark_acquisition", sample_index=0)
            clock = Clock()
            def fail(_):
                raise error
            session = SimpleNamespace(clock=clock, sleep=fail, wait_deadline=None,
                wait_guard=None, monitor_wait_context=station._monitor_lockin_wait,
                _check_wait=lambda: None)
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)):
                LockinPointSession._wait(session, 2)
            self.assertEqual([p["phase"] for k, p in events],
                             ["dark_acquisition", "lockin_settling"])


class MonitorPhaseOpticalTests(unittest.TestCase):
    def make_fixture(self):
        from tests.test_internal_optical_combination import InternalOpticalCombinationTests
        fixture = InternalOpticalCombinationTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.document["optical_background"] = dict(enabled=True, samples=2,
            max_age_s=86400, capture_mode="per_condition")
        fixture.write_document()
        return fixture

    def test_dark_tuning_settling_formal_sequence_and_unchanged_instrument_trace(self):
        first = self.make_fixture()
        result = first.execute("phases")
        self.assertEqual(result["status"], "completed", result)
        events = first.events()
        phases = [payload for kind, payload in events if kind == "scan_phase"]
        names = [p["phase"] for p in phases]
        self.assertLess(names.index("dark_acquisition"), names.index("optical_tuning"))
        self.assertLess(names.index("optical_tuning"), names.index("formal_sampling"))
        self.assertIn("lockin_settling", names)
        dark = [p for p in phases if p["phase"] == "dark_acquisition" and "sample_index" in p]
        self.assertEqual(list(dict.fromkeys(p["sample_index"] for p in dark)), [0, 1])
        second = self.make_fixture()
        with patch.object(HardwareCombinationStation, "set_monitor_phase", lambda *_, **__: None):
            second_result = second.execute("without-phase-audit")
        self.assertEqual(second_result["status"], "completed", second_result)
        for left, right in ((first.xx, second.xx), (first.xy, second.xy)):
            self.assertEqual(left.queries, right.queries)
            self.assertEqual(left.writes, right.writes)
        self.assertEqual(first.optical_log, second.optical_log)
        self.assertEqual(first.clock(), second.clock())
