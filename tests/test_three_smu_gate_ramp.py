from dataclasses import asdict, replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from attodry_control.three_smu import ThreeSmuSafetyError, ThreeSmuSession
from attodry_control.three_smu_config import (
    ChannelPlan, ChannelRole, FinishAction, GateVoltageRamp, ScanMode,
    SourceMode, ThreeSmuConfigError, ThreeSmuHardwareConfig, ThreeSmuScanPlan,
    load_three_smu_operation_config, validate_plan_targets,
)
from tests.test_three_smu import FakeAdapter
from tests.test_three_smu_config import BOTTOM_ONLY_OPERATION_TEXT, smu


RAMP = GateVoltageRamp(0.5, 0.2, 600.0, 0.05)
RAMP_TOML = """
[three_smu_run.gate_bottom.ramp]
max_step_v = 0.5
step_interval_s = 0.2
timeout_s = 600.0
readback_tolerance_v = 0.05
"""


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, duration):
        self.sleeps.append(duration)
        self.now += duration


class Recorder:
    def __init__(self, log):
        self.events = []
        self.log = log

    def event(self, kind, payload):
        self.events.append((kind, payload))
        self.log.append(("event", kind))


class FailingRecorder(Recorder):
    def __init__(self, log, fail_kind=None):
        super().__init__(log)
        self.fail_kind = fail_kind
        self.final = None

    def event(self, kind, payload):
        if self.fail_kind is None or kind == self.fail_kind:
            raise OSError(f"injected audit failure: {kind}")
        super().event(kind, payload)

    def finalize(self, status, *, cleanup, error=None):
        self.final = (status, cleanup, error)


class RampAdapter(FakeAdapter):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.transform = None
        self.fail_write = False

    def read(self):
        self.log.append(("read", self.role, self.source))
        result = super().read()
        return result if self.transform is None else self.transform(result)

    def set_source(self, value):
        super().set_source(value)
        if self.fail_write and value != 0:
            raise OSError("injected write communication failure")


def fixtures(*, points=(-1.0, 1.0), ramp=RAMP):
    off = ChannelPlan(ChannelRole.OFF, False)
    plan = ThreeSmuScanPlan(
        ScanMode.BOTTOM_GATE_TRANSFER, 1, 0.0, False, FinishAction.ZERO_DISABLE,
        1, 0.0, 0.0, off, off,
        ChannelPlan(ChannelRole.SWEEP, False, points=points, ramp=ramp),
    )
    config = replace(smu("gate_bottom", "FAKE::3"),
                     max_abs_voltage_v=35.0, max_abs_current_a=10e-6)
    hardware = ThreeSmuHardwareConfig(gate_bottom=config)
    log = []
    adapter = RampAdapter("gate_bottom", log, config)
    clock = Clock()
    session = ThreeSmuSession.open(
        hardware, plan, authorize_writes=True, authorize_status_consumption=True,
        adapter_factory=lambda _role, _config: adapter,
        sleep=clock.sleep, monotonic=clock.monotonic,
    )
    recorder = Recorder(log)
    session.begin_points(recorder)
    log.clear()
    recorder.events.clear()
    return session, adapter, recorder, clock, log


class GateRampConfigTests(unittest.TestCase):
    def load(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fake.toml"
            path.write_text(text, encoding="utf-8")
            return load_three_smu_operation_config(path)

    def test_optional_four_field_table_and_legacy_omission(self):
        self.assertIsNone(self.load(BOTTOM_ONLY_OPERATION_TEXT).plan.gate_bottom.ramp)
        operation = self.load(BOTTOM_ONLY_OPERATION_TEXT + RAMP_TOML)
        self.assertEqual(operation.plan.gate_bottom.ramp, RAMP)
        self.assertEqual(len(validate_plan_targets(operation.hardware, operation.plan)), 3)

    def test_missing_unknown_and_non_table_ramp_reject(self):
        for text in (
            RAMP_TOML.replace("timeout_s = 600.0\n", ""),
            RAMP_TOML + "typo = 1\n",
        ):
            with self.subTest(text=text), self.assertRaises(ThreeSmuConfigError):
                self.load(BOTTOM_ONLY_OPERATION_TEXT + text)
        text = BOTTOM_ONLY_OPERATION_TEXT.replace(
            '[three_smu_run.gate_bottom]\n',
            '[three_smu_run.gate_bottom]\nramp = 3\n',
        )
        with self.assertRaises(ThreeSmuConfigError):
            self.load(text)

    def test_all_fields_finite_positive_and_tolerance_less_than_half_step(self):
        for field in asdict(RAMP):
            for invalid in (0.0, -1.0, float("nan"), float("inf"), True):
                with self.subTest(field=field, invalid=invalid):
                    with self.assertRaises(ThreeSmuConfigError):
                        replace(RAMP, **{field: invalid})
        with self.assertRaises(ThreeSmuConfigError):
            replace(RAMP, readback_tolerance_v=0.25)

    def test_off_preserves_known_ramp_field_without_parsing_placeholder_values(self):
        for value in ('"fill this table before enabling"',
                      '{ max_step_v = "unset", typo_inside_inactive_value = true }'):
            text = BOTTOM_ONLY_OPERATION_TEXT.replace(
                '[three_smu_run.gate_top]\n',
                f'[three_smu_run.gate_top]\nramp = {value}\n',
            )
            operation = self.load(text)
            self.assertIsNone(operation.plan.gate_top.ramp)
            self.assertEqual(operation.plan.gate_top.role, ChannelRole.OFF)
            self.assertIsNone(operation.hardware.gate_top)
        text = BOTTOM_ONLY_OPERATION_TEXT.replace(
            '[three_smu_run.gate_top]\n',
            '[three_smu_run.gate_top]\nrampp = "misspelled"\n',
        )
        with self.assertRaisesRegex(ThreeSmuConfigError, "unknown"):
            self.load(text)

    def test_bias_current_source_off_and_pulse_reject_before_factory(self):
        session, _adapter, recorder, _clock, _log = fixtures()
        session.cleanup_points(recorder, reason="completed")
        session.close()
        base_plan = session.plan
        hardware = session.hardware
        invalid = [
            (hardware, replace(base_plan, mode=ScanMode.SOFTWARE_PULSE,
                               pulse_high_s=0.2, pulse_period_s=0.4)),
            (replace(hardware, gate_bottom=replace(hardware.gate_bottom,
                                                  source_mode=SourceMode.CURRENT)), base_plan),
            (ThreeSmuHardwareConfig(smu_bias=smu("smu_bias", "FAKE::1")),
             replace(base_plan, mode=ScanMode.BIAS_IV,
                     smu_bias=base_plan.gate_bottom,
                     gate_bottom=ChannelPlan(ChannelRole.OFF, False))),
        ]
        for hw, plan in invalid:
            with self.subTest(plan=plan), self.assertRaises(ThreeSmuConfigError):
                ThreeSmuSession.open(hw, plan, authorize_writes=True,
                                     authorize_status_consumption=True,
                                     adapter_factory=lambda *_: self.fail("hardware opened"))
        with self.assertRaises(ThreeSmuConfigError):
            ChannelPlan(ChannelRole.OFF, False, ramp=RAMP)


class GateRampSessionTests(unittest.TestCase):
    def test_changed_source_register_bounds_step_even_when_actual_voltage_lags(self):
        session, adapter, recorder, _clock, log = fixtures()
        adapter.source = 0.4
        first = True

        def lag_once(reading):
            nonlocal first
            if first:
                first = False
                return replace(reading, voltage_v=0.0)
            return reading

        adapter.transform = lag_once
        session.set_point({"gate_bottom": -1.0})
        attempts = [payload for kind, payload in recorder.events
                    if kind == "gate_ramp_write_attempt"]
        self.assertAlmostEqual(attempts[0]["target"], -0.1)
        self.assertEqual(attempts[0]["confirmed_source_setpoint_v"], 0.4)
        for attempt in attempts:
            for field in ("start_actual_v", "previous_requested_v", "confirmed_source_setpoint_v"):
                self.assertLessEqual(abs(attempt["target"] - attempt[field]), RAMP.max_step_v)
        session.cleanup_points(recorder, reason="completed")
        session.close()

    def test_changed_register_with_no_safe_join_rejects_before_write(self):
        session, adapter, recorder, _clock, log = fixtures()
        adapter.source = 2.0
        adapter.transform = lambda reading: replace(reading, voltage_v=0.0)
        with self.assertRaisesRegex(ThreeSmuSafetyError, "cannot safely join"):
            session.set_point({"gate_bottom": -1.0})
        self.assertFalse(any(item[0] == "source" for item in log))
        session.cleanup_points(recorder, reason="failed")
        session.close()

    def test_every_cleanup_audit_failure_still_attempts_off_and_marks_manual(self):
        for kind in (None, "cleanup_disable_attempt", "cleanup_disable",
                     "cleanup_zero_attempt", "cleanup_zero_register", "cleanup_complete"):
            with self.subTest(kind=kind):
                session, _adapter, recorder, clock, log = fixtures()
                session.set_point({"gate_bottom": 1.0})
                elapsed = clock.now
                log.clear()
                failing = FailingRecorder(log, fail_kind=kind)
                result = session.cleanup_points(failing, reason="failed")
                commands = [item for item in log if item[0] in ("source", "output")]
                self.assertEqual(commands[0], ("output", "gate_bottom", False))
                self.assertEqual(clock.now, elapsed)
                self.assertTrue(result["manual_verification_required"])
                self.assertTrue(any(error["stage"].startswith("audit:")
                                    for error in result["cleanup_errors"]))
                session.close()

    def test_failed_disable_and_failing_audit_do_not_prevent_later_gate_off(self):
        session, adapter, recorder, clock, log = fixtures()
        session.cleanup_points(recorder, reason="completed")
        session.close()
        top_config = replace(session.hardware.gate_bottom, role="gate_top", address="FAKE::2")
        hardware = replace(session.hardware, gate_top=top_config)
        plan = replace(session.plan, mode=ScanMode.PAIRED_GATE, gate_top=session.plan.gate_bottom)
        top = RampAdapter("gate_top", log, top_config)
        bottom = RampAdapter("gate_bottom", log, hardware.gate_bottom)
        session = ThreeSmuSession.open(
            hardware, plan, authorize_writes=True, authorize_status_consumption=True,
            adapter_factory=lambda role, _: {"gate_top": top, "gate_bottom": bottom}[role],
            sleep=clock.sleep, monotonic=clock.monotonic,
        )
        session.begin_points(recorder)
        session.set_point({"gate_top": 1.0, "gate_bottom": 1.0})
        log.clear()

        def failed_off(enabled):
            log.append(("output", "gate_top", enabled))
            raise OSError("injected top OFF failure")

        top.set_output = failed_off
        result = session.cleanup_points(FailingRecorder(log), reason="failed")
        self.assertEqual([item for item in log if item[0] == "output"],
                         [("output", "gate_top", False), ("output", "gate_bottom", False)])
        self.assertEqual([action["role"] for action in result["actions"]],
                         ["gate_top", "gate_bottom"])
        self.assertTrue(result["manual_verification_required"])
        self.assertTrue(any(error["stage"] == "disable" for error in result["cleanup_errors"]))
        session.close()

    def test_standalone_error_audit_failure_cannot_prevent_cleanup(self):
        session, _adapter, _recorder, _clock, log = fixtures()
        session.set_point({"gate_bottom": 1.0})
        log.clear()
        failing = FailingRecorder(log)
        session._run_active = True
        session._recorder = failing
        session._abort_active_run("injected primary failure", interrupted=False)
        self.assertEqual([item for item in log if item[0] in ("source", "output")][0],
                         ("output", "gate_bottom", False))
        self.assertEqual(failing.final[0], "rejected")
        self.assertTrue(failing.final[1]["manual_verification_required"])
        self.assertTrue(any(error["stage"] == "audit:error"
                            for error in failing.final[1]["cleanup_errors"]))
        session._points_cleaned = True
        session.close()

    def test_finalize_failure_preserves_acquisition_error_and_closes_all_adapters(self):
        base, _adapter, recorder, clock, log = fixtures()
        base.cleanup_points(recorder, reason="completed")
        base.close()
        top_config = replace(base.hardware.gate_bottom, role="gate_top", address="FAKE::2")
        hardware = replace(base.hardware, gate_top=top_config)
        plan = replace(base.plan, mode=ScanMode.PAIRED_GATE, gate_top=base.plan.gate_bottom)
        top = RampAdapter("gate_top", log, top_config)
        bottom = RampAdapter("gate_bottom", log, hardware.gate_bottom)
        bottom.fail_on_read = 2

        def fail_first_close():
            log.append(("close", "gate_top"))
            raise OSError("injected first adapter close failure")

        top.close = fail_first_close
        log.clear()
        with tempfile.TemporaryDirectory() as directory:
            session = ThreeSmuSession.open(
                hardware, plan, authorize_writes=True, authorize_status_consumption=True,
                adapter_factory=lambda role, _: {"gate_top": top, "gate_bottom": bottom}[role],
                sleep=clock.sleep, monotonic=clock.monotonic,
            )
            with patch("attodry_control.three_smu._RunRecorder.finalize",
                       side_effect=OSError("injected finalize failure")) as finalize:
                with self.assertRaisesRegex(OSError, "gate_bottom communication failure") as raised:
                    with session:
                        list(session.run(output_dir=directory))
            self.assertEqual(finalize.call_count, 1)
            self.assertFalse(session._run_active)
            self.assertTrue(session._closed)
            self.assertEqual([item for item in log if item[0] == "close"],
                             [("close", "gate_top"), ("close", "gate_bottom")])
            self.assertEqual([item for item in log if item[0] == "output" and item[2] is False],
                             [("output", "gate_top", False), ("output", "gate_bottom", False)])
            notes = " ".join(raised.exception.__notes__)
            self.assertIn("finalize failure", notes)
            self.assertIn("first adapter close failure", notes)
            metadata = json.loads((session.last_run_dir / "metadata.json").read_text())
            self.assertEqual(metadata["status"], "rejected")
            self.assertFalse(metadata["accepted"])
            self.assertTrue(metadata["cleanup"]["manual_verification_required"])
            self.assertTrue(any(error["stage"] == "audit:finalize"
                                for error in metadata["cleanup"]["cleanup_errors"]))
            # The injected mock did not execute the recorder's file-close body.
            session._recorder._raw.close()
            session._recorder._csv.close()

    def test_context_abort_finalize_failure_still_closes_adapters(self):
        session, _adapter, _recorder, _clock, log = fixtures()
        session.set_point({"gate_bottom": 1.0})
        failing = FailingRecorder(log, fail_kind="unused")
        failing.metadata = {}

        def fail_finalize(*_args, **_kwargs):
            raise OSError("injected context finalize failure")

        failing.finalize = fail_finalize
        session._run_active = True
        session._recorder = failing
        session._points_cleaned = True
        log.clear()
        with self.assertRaisesRegex(OSError, "context finalize failure"):
            session.__exit__(None, None, None)
        self.assertFalse(session._run_active)
        self.assertTrue(session._closed)
        self.assertIn(("output", "gate_bottom", False), log)
        self.assertIn(("close", "gate_bottom"), log)
        self.assertTrue(failing.metadata["cleanup"]["manual_verification_required"])

    def test_initial_voltage_within_tolerance_still_requires_final_target_ack(self):
        session, _adapter, recorder, _clock, log = fixtures(points=(0.05,))
        session.set_point({"gate_bottom": 0.05})
        self.assertEqual([item[2] for item in log if item[0] == "source"], [0.05])
        session.cleanup_points(recorder, reason="completed")
        session.close()

    def test_stale_last_command_cannot_substitute_for_fresh_target_ack(self):
        session, adapter, recorder, _clock, log = fixtures(points=(0.05,))
        session.last_commanded["gate_bottom"] = 0.05
        adapter.source = 0.0
        session.set_point({"gate_bottom": 0.05})
        self.assertEqual([item[2] for item in log if item[0] == "source"], [0.05])
        session.cleanup_points(recorder, reason="completed")
        session.close()

    def test_start_uses_fresh_actual_voltage_not_zero_command(self):
        session, adapter, recorder, clock, log = fixtures()
        reads = 0

        def actual_once(reading):
            nonlocal reads
            reads += 1
            return replace(reading, voltage_v=-0.2) if reads == 1 else reading

        adapter.transform = actual_once
        session.set_point({"gate_bottom": 1.0})
        commands = [item[2] for item in log if item[0] == "source"]
        self.assertEqual(commands, [0.3, 0.8, 1.0])
        self.assertEqual(log[0][0], "event")
        self.assertLess(log.index(("event", "gate_ramp_write_attempt")),
                        log.index(("source", "gate_bottom", 0.3)))
        self.assertAlmostEqual(clock.now, 0.6)
        session.cleanup_points(recorder, reason="completed")
        session.close()

    def test_startup_end_to_end_reset_and_normal_zero_keep_limits(self):
        session, _adapter, recorder, _clock, log = fixtures(points=(-35.0, 35.0))
        for target in (-35.0, 35.0, -35.0):
            session.set_point({"gate_bottom": target})
        result = session.cleanup_points(recorder, reason="completed", failed=False)
        self.assertFalse(result["manual_verification_required"])
        self.assertTrue(result["actions"][0]["zero_readback_recorded"])
        self.assertTrue(result["actions"][0]["output_off_confirmed"])
        attempts = [payload for kind, payload in recorder.events
                    if kind == "gate_ramp_write_attempt"]
        self.assertEqual(len(attempts), 420)
        for step in attempts:
            self.assertLessEqual(abs(step["target"]), 35.0)
            self.assertLessEqual(abs(step["target"] - step["start_actual_v"]), 0.5)
            self.assertLessEqual(abs(step["target"] - step["previous_requested_v"]), 0.5)
        source_commands = [item for item in log if item[0] == "source"]
        self.assertEqual(source_commands[-1][2], 0.0)
        session.close()

    def test_tolerance_accepts_physical_quantization_without_widening_current_limit(self):
        session, adapter, recorder, _clock, _log = fixtures()
        adapter.transform = lambda reading: replace(reading, voltage_v=reading.voltage_v + 0.02)
        session.set_point({"gate_bottom": 1.0})
        result = session.cleanup_points(recorder, reason="completed")
        self.assertFalse(result["manual_verification_required"])
        self.assertEqual(session.last_confirmed["gate_bottom"].reading.voltage_v, 0.02)
        self.assertEqual(adapter.config.max_abs_current_a, 10e-6)
        session.close()

    def test_bad_readback_prevents_next_step_and_failure_off_precedes_zero(self):
        session, adapter, recorder, clock, log = fixtures()
        adapter.transform = lambda reading: replace(reading, voltage_v=0.0)
        with self.assertRaisesRegex(ThreeSmuSafetyError, "actual voltage"):
            session.set_point({"gate_bottom": 1.0})
        self.assertEqual([item[2] for item in log if item[0] == "source"], [0.5])
        before = clock.now
        log.clear()
        result = session.cleanup_points(recorder, reason="failed", failed=True)
        actions = [item for item in log if item[0] in ("output", "source")]
        self.assertEqual(actions[0], ("output", "gate_bottom", False))
        self.assertEqual(clock.now, before)
        self.assertTrue(result["manual_verification_required"])
        self.assertFalse(result["actions"][0]["zero_readback_recorded"])
        self.assertEqual(session.last_confirmed["gate_bottom"].reading.voltage_v, 0.0)
        session.close()

    def test_every_step_rejects_compliance_limits_errors_unknown_or_nonfinite(self):
        faults = (
            {"compliance_trip": True}, {"current_a": 10e-6 + 1e-12},
            {"voltage_v": 35.01}, {"voltage_v": float("nan")},
            {"output_enabled": False}, {"output_enabled": None},
            {"compliance_trip": None}, {"status": None},
            {"status": '822,"error"'}, {"status": "0.5,unknown"},
            {"status_query_consumed": False}, {"source_setpoint": 35.01},
        )
        for fault in faults:
            with self.subTest(fault=fault):
                session, adapter, recorder, _clock, log = fixtures()
                adapter.transform = lambda reading: replace(reading, **fault)
                with self.assertRaises(ThreeSmuSafetyError):
                    session.set_point({"gate_bottom": 1.0})
                self.assertFalse(any(item[0] == "source" for item in log))
                self.assertTrue(session.cleanup_points(recorder, reason="failed")
                                ["manual_verification_required"])
                session.close()

    def test_post_step_trip_is_recorded_and_prevents_later_target_writes(self):
        session, adapter, recorder, _clock, log = fixtures()
        adapter.trip_on_read = adapter.read_count + 2
        with self.assertRaisesRegex(ThreeSmuSafetyError, "compliance trip"):
            session.set_point({"gate_bottom": 1.0})
        self.assertEqual([item[2] for item in log if item[0] == "source"], [0.5])
        rejected = [payload for kind, payload in recorder.events
                    if kind == "gate_ramp_readback" and payload["problems"]]
        self.assertTrue(rejected[0]["reading"]["compliance_trip"])
        session.cleanup_points(recorder, reason="failed")
        session.close()

    def test_cancellation_during_ramp_retains_last_sensed_voltage_and_disables(self):
        session, adapter, recorder, clock, log = fixtures()
        adapter.interrupt_on_read = adapter.read_count + 2
        with self.assertRaises(KeyboardInterrupt):
            session.set_point({"gate_bottom": 1.0})
        elapsed = clock.now
        log.clear()
        result = session.cleanup_points(recorder, reason="interrupted")
        self.assertEqual(clock.now, elapsed)
        self.assertEqual(result["last_confirmed"]["gate_bottom"]["voltage_v"], 0.0)
        self.assertEqual([item for item in log if item[0] in ("source", "output")][0],
                         ("output", "gate_bottom", False))
        self.assertTrue(result["manual_verification_required"])
        session.close()

    def test_close_after_partial_points_uses_failed_cleanup_without_long_ramp(self):
        session, _adapter, recorder, clock, log = fixtures()
        session.set_point({"gate_bottom": 1.0})
        elapsed = clock.now
        log.clear()
        with self.assertRaisesRegex(Exception, "manual verification"):
            session.close()
        self.assertEqual(clock.now, elapsed)
        self.assertEqual([item for item in log if item[0] in ("source", "output")][0],
                         ("output", "gate_bottom", False))
        cleanup = [payload for kind, payload in recorder.events if kind == "cleanup_complete"]
        self.assertTrue(cleanup[0]["manual_verification_required"])

    def test_communication_write_and_read_preserve_attempt_and_last_measured_voltage(self):
        for where in ("write", "read"):
            with self.subTest(where=where):
                session, adapter, recorder, _clock, _log = fixtures()
                adapter.transform = lambda reading: replace(reading, voltage_v=reading.voltage_v + 0.02)
                if where == "write":
                    adapter.fail_write = True
                else:
                    adapter.fail_on_read = adapter.read_count + 2
                with self.assertRaises(OSError):
                    session.set_point({"gate_bottom": 1.0})
                self.assertEqual(sum(kind == "gate_ramp_write_attempt" for kind, _ in recorder.events), 1)
                result = session.cleanup_points(recorder, reason="failed")
                self.assertEqual(result["last_confirmed"]["gate_bottom"]["voltage_v"], 0.02)
                self.assertTrue(result["manual_verification_required"])
                session.close()

    def test_guard_checks_during_long_interval_and_is_not_reused_in_failure_cleanup(self):
        session, _adapter, recorder, clock, log = fixtures(
            ramp=replace(RAMP, step_interval_s=3.0))
        guard_calls = []

        def guard():
            guard_calls.append(clock.now)
            if clock.now >= 2.0:
                raise RuntimeError("optical guard failed")

        with self.assertRaisesRegex(RuntimeError, "optical guard"):
            session.set_point({"gate_bottom": 1.0}, transition_guard=guard)
        self.assertEqual(clock.sleeps, [1.0, 1.0])
        calls = len(guard_calls)
        log.clear()
        result = session.cleanup_points(recorder, reason="failed")
        self.assertEqual(len(guard_calls), calls)
        self.assertEqual([item for item in log if item[0] == "output"],
                         [("output", "gate_bottom", False)])
        self.assertTrue(result["manual_verification_required"])
        session.close()

    def test_total_timeout_includes_guard_cost_and_retains_last_read(self):
        session, _adapter, recorder, clock, log = fixtures(ramp=replace(RAMP, timeout_s=0.3))

        def guard():
            clock.now += 0.2

        with self.assertRaisesRegex(ThreeSmuSafetyError, "timeout"):
            session.set_point({"gate_bottom": 1.0}, transition_guard=guard)
        self.assertFalse(any(item[0] == "source" for item in log))
        self.assertIn("gate_bottom", session.last_confirmed)
        session.cleanup_points(recorder, reason="failed")
        session.close()

    def test_sleep_and_step_count_cannot_extend_total_timeout(self):
        session, _adapter, recorder, clock, log = fixtures(ramp=replace(RAMP, timeout_s=0.3))
        with self.assertRaisesRegex(ThreeSmuSafetyError, "timeout"):
            session.set_point({"gate_bottom": 1.0})
        self.assertAlmostEqual(clock.now, 0.3)
        self.assertEqual([item[2] for item in log if item[0] == "source"], [0.5, 1.0])
        self.assertEqual(session.last_confirmed["gate_bottom"].reading.voltage_v, 0.5)
        self.assertTrue(session.cleanup_points(recorder, reason="failed")
                        ["manual_verification_required"])
        session.close()

    def test_normal_zero_requires_actual_voltage_and_unknown_cleanup_cannot_claim_zero(self):
        session, adapter, recorder, _clock, _log = fixtures()
        session.set_point({"gate_bottom": 1.0})
        adapter.transform = lambda reading: replace(reading, voltage_v=0.2)
        result = session.cleanup_points(recorder, reason="completed")
        self.assertTrue(result["manual_verification_required"])
        self.assertFalse(result["actions"][0]["zero_readback_recorded"])
        self.assertEqual(result["last_confirmed"]["gate_bottom"]["voltage_v"], 0.2)
        session.close()

    def test_legacy_direct_write_and_cleanup_are_unchanged(self):
        session, _adapter, recorder, _clock, log = fixtures(ramp=None)
        session.set_point({"gate_bottom": 35.0})
        self.assertEqual([item[2] for item in log if item[0] == "source"], [35.0])
        log.clear()
        result = session.cleanup_points(recorder, reason="failed", failed=True)
        self.assertEqual([item[0] for item in log if item[0] in ("source", "output")],
                         ["source", "output"])
        self.assertFalse(result["manual_verification_required"])
        session.close()

    def test_standalone_formal_rows_and_metadata_exclude_transition_samples(self):
        session, adapter, _recorder, clock, _log = fixtures(points=(-1.0, 1.0))
        session.cleanup_points(_recorder, reason="completed")
        session.close()
        adapter = RampAdapter("gate_bottom", [], session.hardware.gate_bottom)
        with tempfile.TemporaryDirectory() as directory:
            with ThreeSmuSession.open(
                session.hardware, session.plan, authorize_writes=True,
                authorize_status_consumption=True,
                adapter_factory=lambda *_: adapter,
                sleep=clock.sleep, monotonic=clock.monotonic,
            ) as run_session:
                samples = list(run_session.run(output_dir=directory))
                metadata = json.loads((run_session.last_run_dir / "metadata.json").read_text())
                events = [json.loads(line) for line in
                          (run_session.last_run_dir / "raw.jsonl").read_text().splitlines()]
            self.assertEqual([sample.coordinates["gate_bottom"] for sample in samples], [-1.0, 1.0])
            self.assertEqual(sum(event["event"] == "sample" for event in events), 2)
            self.assertEqual(metadata["plan"]["gate_bottom"]["ramp"], asdict(RAMP))
            self.assertTrue(metadata["accepted"])


if __name__ == "__main__":
    unittest.main()
