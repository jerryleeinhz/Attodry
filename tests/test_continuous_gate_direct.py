"""Direct guarded gate transitions; fake instruments and clocks only."""
from dataclasses import replace
import tomllib
import unittest
from unittest.mock import patch

from attodry_control.combination_hardware import load_hardware_combination
from attodry_control.combination_launch import launch_summary
from attodry_control.three_smu import ThreeSmuSafetyError
from attodry_control.three_smu_config import ThreeSmuConfigError, SourceMode, load_three_smu_operation_config
from attodry_control.optical_points import OpticalPointSession
from tests.test_three_smu_gate_ramp import fixtures, FailingRecorder
from tests import test_combination_continuous_gate as continuous_fixture
import test_combination_photonics as fixture


class ContinuousDirectGateTests(unittest.TestCase):
    def make(self, **kwargs):
        case = continuous_fixture.ContinuousGateCombinationTests.make(self, **kwargs)
        doc = tomllib.loads(case.path.read_text(encoding="utf-8"))
        for role in ("gate_top", "gate_bottom"):
            doc["three_smu_run"][role].pop("ramp", None)
            if doc["three_smu_run"][role]["role"] != "off":
                doc["three_smu_run"][role]["zero_readback_tolerance_v"] = .05
        case.path.write_text(fixture.toml_document(doc), encoding="utf-8")
        return case

    def execute(self, case, run_id="direct", **kwargs):
        return continuous_fixture.ContinuousGateCombinationTests.execute(self, case, run_id, **kwargs)

    def session_fixture(self):
        session, *others = fixtures(ramp=None)
        session.plan = replace(session.plan, gate_bottom=replace(
            session.plan.gate_bottom, zero_readback_tolerance_v=.05))
        return (session, *others)

    def test_no_ramp_loads_offline_without_opening_resources_or_changing_limits(self):
        case = self.make()
        config = load_hardware_combination(case.path)
        self.assertEqual(config.illumination_policy, "continuous_gate_scan")
        for role in ("gate_top", "gate_bottom"):
            self.assertIsNone(config.smu.plan.by_role()[role].ramp)
            original = tomllib.loads(case.path.read_text(encoding="utf-8"))[role]
            self.assertEqual(config.smu.hardware.require_role(role).max_abs_current_a,
                             original["max_abs_current_a"])
            self.assertEqual(config.smu.hardware.require_role(role).max_abs_voltage_v,
                             original["max_abs_voltage_v"])
            self.assertEqual(launch_summary(config, config.database_path, "direct")["smu"][role]
                             ["zero_readback_tolerance_v"], .05)
        self.assertEqual(case.manager.opened, [])
        self.assertEqual(case.adapters, {})
        self.assertIsNone(case.backend)

    def test_continuous_light_direct_targets_have_no_intermediate_writes(self):
        case = self.make()
        doc = tomllib.loads(case.path.read_text(encoding="utf-8"))
        doc["three_smu_run"]["gate_top"]["points"] = [-2.0, 2.0]
        case.path.write_text(fixture.toml_document(doc), encoding="utf-8")
        result = self.execute(case)
        self.assertEqual(result["status"], "completed", result)
        events = case.events()
        groups = [payload for kind, payload in events if kind == "continuous_optical_group_qualified"]
        self.assertEqual(len(groups), 2)
        lit = [entry for entry in case.gate_writes if entry[3] == 3]
        self.assertEqual([entry[2] for entry in lit if entry[1] == "gate_top"], [-2.0, 2.0] * 2)
        self.assertEqual([entry[2] for entry in lit if entry[1] == "gate_bottom"], [.05] * 4)
        self.assertFalse(any(kind.startswith("gate_ramp_") for kind, _ in events))
        attempts = [payload for kind, payload in events if kind == "gate_direct_write_attempt"]
        self.assertEqual(len(attempts), 8)
        readings = [payload for kind, payload in events if kind == "gate_direct_readback"]
        self.assertEqual(len(readings), 16)
        self.assertEqual({payload["phase"] for payload in readings}, {"before_write", "after_delay"})
        for index, entry in enumerate(case.log):
            if entry[0] != "gate_write" or entry[3] != 3:
                continue
            read = next(item for item in case.log[index + 1:]
                        if item[:2] == ("gate_read", entry[1]))
            self.assertGreaterEqual(read[3] - entry[4] + 1e-12, .03)
        samples = continuous_fixture.ContinuousGateCombinationTests.samples(self, case)
        self.assertEqual(len(samples), 8)
        for sample in samples:
            optical = next(read for read in sample["reads"] if read["module"] == "optical")
            self.assertEqual([item["phase"] for item in optical["status"]["sample_brackets"]],
                             ["before_electrical", "formal", "after_electrical"])
            self.assertTrue(sample["acquisition_accepted"])
        self.assertTrue(case.manager.closed and case.meter.closed and case.pem.closed)
        self.assertEqual(case.backend.source["emission_state"], 0)

    def test_guarded_direct_session_reads_before_write_and_after_original_delay(self):
        session, _adapter, recorder, clock, log = self.session_fixture()
        session.plan = replace(session.plan, delay_s=.1)

        def guard():
            log.append(("guard", clock.now))

        session.set_point({"gate_bottom": 35.0}, transition_guard=guard)
        commands = [item for item in log if item[0] == "source"]
        self.assertEqual(commands, [("source", "gate_bottom", 35.0)])
        reads = [i for i, item in enumerate(log) if item[0] == "read"]
        write = log.index(commands[0])
        self.assertLess(reads[0], write)
        self.assertGreater(reads[1], write)
        self.assertEqual(clock.sleeps, [.1])
        self.assertTrue(any(item[0] == "guard" for item in log[reads[0] + 1:write]))
        self.assertTrue(any(item[0] == "guard" for item in log[reads[1] + 1:]))
        self.assertEqual(session.hardware.gate_bottom.max_abs_current_a, 1e-5)
        session.cleanup_points(recorder, reason="completed")
        session.close()

    def test_direct_pre_write_faults_fail_before_command(self):
        for changes in ({"current_a": 1e-5 + 1e-12}, {"compliance_trip": True},
                        {"compliance_trip": None}, {"status": "822,error"},
                        {"status": None}, {"status_query_consumed": False},
                        {"voltage_v": 35.01}, {"source_setpoint": 35.01},
                        {"voltage_v": float("nan")}, {"output_enabled": None}):
            with self.subTest(changes=changes):
                session, adapter, recorder, _clock, log = self.session_fixture()
                adapter.transform = lambda reading: replace(reading, **changes)
                with self.assertRaises(ThreeSmuSafetyError):
                    session.set_point({"gate_bottom": 1.0}, transition_guard=lambda: None)
                self.assertFalse(any(item[0] == "source" for item in log))
                self.assertTrue(any(kind == "gate_direct_readback" and payload["problems"]
                                    for kind, payload in recorder.events))
                session.cleanup_points(recorder, reason="failed")
                session.close()

    def test_direct_post_write_trip_is_detected_before_formal_sampling(self):
        session, adapter, recorder, _clock, log = self.session_fixture()
        adapter.trip_on_read = adapter.read_count + 2
        with self.assertRaisesRegex(ThreeSmuSafetyError, "compliance trip"):
            session.set_point({"gate_bottom": 1.0}, transition_guard=lambda: None)
        self.assertEqual([item[2] for item in log if item[0] == "source"], [1.0])
        rejected = [payload for kind, payload in recorder.events
                    if kind == "gate_direct_readback" and payload["problems"]]
        self.assertEqual(rejected[0]["phase"], "after_delay")
        self.assertTrue(rejected[0]["reading"]["compliance_trip"])
        self.assertFalse(any(kind == "sample" for kind, _ in recorder.events))
        session.cleanup_points(recorder, reason="failed")
        session.close()

    def test_direct_quantization_is_recorded_without_new_tolerance_policy(self):
        session, adapter, recorder, _clock, _log = self.session_fixture()
        adapter.transform = lambda reading: replace(reading, voltage_v=reading.voltage_v + .02)
        session.set_point({"gate_bottom": 1.0}, transition_guard=lambda: None)
        reading = [payload for kind, payload in recorder.events
                   if kind == "gate_direct_readback" and payload["phase"] == "after_delay"][0]
        self.assertEqual(reading["target"], 1.0)
        self.assertEqual(reading["reading"]["voltage_v"], 1.02)
        self.assertEqual(reading["problems"], [])
        session.cleanup_points(recorder, reason="completed")
        session.close()

    def test_direct_target_requires_fresh_exact_source_acknowledgement(self):
        for ignored_write in (True, False):
            with self.subTest(ignored_write=ignored_write):
                session, adapter, recorder, _clock, log = self.session_fixture()
                if ignored_write:
                    adapter.set_source = lambda value: log.append(("source", "gate_bottom", value))
                else:
                    adapter.transform = lambda reading: replace(
                        reading, source_setpoint=reading.source_setpoint + .001
                        if reading.source_setpoint == 1.0 else reading.source_setpoint)
                with self.assertRaisesRegex(ThreeSmuSafetyError, "acknowledge direct target"):
                    session.set_point({"gate_bottom": 1.0}, transition_guard=lambda: None)
                rejected = [payload for kind, payload in recorder.events
                            if kind == "gate_direct_readback" and payload["problems"]]
                self.assertEqual(rejected[0]["phase"], "after_delay")
                self.assertEqual(rejected[0]["target"], 1.0)
                self.assertEqual(rejected[0]["reading"]["source_setpoint"],
                                 0.0 if ignored_write else 1.001)
                self.assertEqual(rejected[0]["reading"]["voltage_v"],
                                 0.0 if ignored_write else 1.0)
                self.assertFalse(any(kind == "sample" for kind, _ in recorder.events))
                result = session.cleanup_points(recorder, reason="failed", failed=True)
                self.assertFalse(result["actions"][0]["zero_readback_recorded"])
                self.assertTrue(result["manual_verification_required"])
                session.close()

    def test_direct_zero_policy_is_explicit_and_strictly_validated_offline(self):
        for invalid in (None, 0.0, -0.05, True, "unknown", float("nan"), float("inf"), 3.01):
            with self.subTest(invalid=invalid):
                case = self.make()
                doc = tomllib.loads(case.path.read_text(encoding="utf-8"))
                if invalid is None:
                    del doc["three_smu_run"]["gate_top"]["zero_readback_tolerance_v"]
                else:
                    doc["three_smu_run"]["gate_top"]["zero_readback_tolerance_v"] = invalid
                case.path.write_text(fixture.toml_document(doc), encoding="utf-8")
                with self.assertRaises((ThreeSmuConfigError, ValueError)):
                    load_hardware_combination(case.path)
                self.assertEqual(case.manager.opened, [])
        case = self.make()
        doc = tomllib.loads(case.path.read_text(encoding="utf-8"))
        doc["gate_top"]["source_mode"] = "current"
        doc["three_smu_run"]["gate_top"]["points"] = [-1e-6, 1e-6]
        case.path.write_text(fixture.toml_document(doc), encoding="utf-8")
        with self.assertRaises(ThreeSmuConfigError):
            load_three_smu_operation_config(case.path)

    def test_normal_direct_cleanup_verifies_actual_zero_and_exact_source_ack(self):
        for voltage, source_offset, safe in ((.05, 0.0, True), (.050001, 0.0, False),
                                              (30.0, 0.0, False), (0.0, .001, False)):
            with self.subTest(voltage=voltage, source_offset=source_offset):
                session, adapter, recorder, _clock, log = self.session_fixture()
                session.set_point({"gate_bottom": 1.0}, transition_guard=lambda: None)
                adapter.transform = lambda reading: replace(reading, voltage_v=voltage,
                                                             source_setpoint=reading.source_setpoint + source_offset)
                log.clear()
                result = session.cleanup_points(recorder, reason="completed")
                self.assertEqual(result["actions"][0]["zero_readback_recorded"], safe)
                self.assertEqual(result["manual_verification_required"], not safe)
                self.assertTrue(result["actions"][0]["output_off_confirmed"])
                self.assertFalse(any(kind.startswith("gate_ramp") for kind, _ in recorder.events))
                if safe:
                    self.assertEqual([item for item in log if item[0] == "source"],
                                     [("source", "gate_bottom", 0.0)])
                session.close()

    def test_off_gate_rejects_new_zero_policy_even_when_value_is_placeholder(self):
        for value in (.05, "unset", {"placeholder": True}):
            with self.subTest(value=value):
                case = self.make()
                doc = tomllib.loads(case.path.read_text(encoding="utf-8"))
                doc["three_smu_run"]["gate_bottom"]["role"] = "off"
                doc["three_smu_run"]["gate_bottom"]["zero_readback_tolerance_v"] = value
                case.path.write_text(fixture.toml_document(doc), encoding="utf-8")
                with self.assertRaisesRegex(ThreeSmuConfigError, "requires an active voltage-source gate"):
                    load_hardware_combination(case.path)
                self.assertEqual(case.manager.opened, [])

    def test_failed_or_unknown_direct_cleanup_off_precedes_zero_without_delay(self):
        session, adapter, recorder, clock, log = self.session_fixture()
        session.set_point({"gate_bottom": 1.0}, transition_guard=lambda: None)
        adapter.fail_on_read = adapter.read_count + 1
        before = clock.now
        log.clear()
        result = session.cleanup_points(recorder, reason="failed", failed=True)
        commands = [item for item in log if item[0] in ("source", "output")]
        self.assertEqual(commands[0], ("output", "gate_bottom", False))
        self.assertEqual(clock.now, before)
        self.assertFalse(result["actions"][0]["zero_readback_recorded"])
        self.assertTrue(result["manual_verification_required"])
        self.assertEqual(result["last_confirmed"]["gate_bottom"]["voltage_v"], 1.0)
        session.close()

    def test_direct_cleanup_audit_failure_cannot_prevent_off_or_certify_clean(self):
        session, _adapter, _recorder, _clock, log = self.session_fixture()
        session.set_point({"gate_bottom": 1.0}, transition_guard=lambda: None)
        log.clear()
        result = session.cleanup_points(FailingRecorder(log), reason="completed")
        commands = [item for item in log if item[0] in ("source", "output")]
        self.assertEqual(commands[0], ("output", "gate_bottom", False))
        self.assertTrue(result["manual_verification_required"])
        self.assertTrue(any(item["stage"].startswith("audit:") for item in result["cleanup_errors"]))
        session.close()

    def test_optical_failure_before_first_gate_uses_registered_direct_cleanup(self):
        case = self.make()
        with patch.object(OpticalPointSession, "qualify", side_effect=RuntimeError("initial optical fault")):
            result = self.execute(case)
        self.assertEqual(result["status"], "failed")
        self.assertFalse(any(kind == "gate_direct_write_attempt" for kind, _ in case.events()))
        smu = next(action for action in result["cleanup"]["actions"] if action["module"] == "smu")
        self.assertTrue(smu["result"]["manual_verification_required"])
        self.assertTrue(all(action["output_off_confirmed"] and not action["zero_readback_recorded"]
                            for action in smu["result"]["actions"]))
        self.assertTrue(case.manager.closed and case.meter.closed and case.pem.closed)


if __name__ == "__main__":
    unittest.main()
