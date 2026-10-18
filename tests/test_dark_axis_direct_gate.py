"""Explicit optical direct gates retain protection when changing axes in dark."""
from dataclasses import replace
import tomllib
import unittest

from attodry_control.three_smu import ThreeSmuSafetyError
from tests import test_continuous_gate_direct as direct_fixture
from tests import test_combination_continuous_gate as continuous_fixture
from tests.test_combination_photonics import toml_document
from tests.test_three_smu_config import OPERATION_TEXT


class DarkAxisDirectGateTests(unittest.TestCase):
    make = direct_fixture.ContinuousDirectGateTests.make
    execute = direct_fixture.ContinuousDirectGateTests.execute
    session_fixture = direct_fixture.ContinuousDirectGateTests.session_fixture

    def test_off_axis_optical_gate_records_before_and_after_checks_with_laser_off(self):
        case = self.make(policy="off_before_axis_change")
        result = self.execute(case)
        self.assertEqual(result["status"], "completed", result)
        reads = [p for kind, p in case.events() if kind == "gate_direct_readback"]
        self.assertEqual(len(reads), 24)  # two gates x six conditions x two boundaries
        self.assertEqual({p["phase"] for p in reads}, {"before_write", "after_delay"})
        writes = [x for x in case.gate_writes if x[2] != 0]
        self.assertTrue(writes)
        self.assertTrue(all(x[3] == 0 for x in writes))
        smu = next(x for x in result["cleanup"]["actions"] if x["module"] == "smu")
        self.assertEqual(smu["result"]["result"], "confirmed_safe")
        self.assertTrue(all(x["zero_readback_recorded"] for x in smu["result"]["actions"]))

    def test_registered_direct_session_checks_without_transition_callback(self):
        session, _adapter, recorder, clock, log = self.session_fixture()
        session._guarded_direct_roles.add("gate_bottom")
        session.plan = replace(session.plan, delay_s=.1)
        session.set_point({"gate_bottom": 1.0})
        phases = [p["phase"] for kind, p in recorder.events if kind == "gate_direct_readback"]
        self.assertEqual(phases, ["before_write", "after_delay"])
        self.assertEqual(clock.sleeps, [.1])
        self.assertEqual([x for x in log if x[0] == "source"], [("source", "gate_bottom", 1.0)])
        result = session.cleanup_points(recorder, reason="completed")
        self.assertFalse(result["manual_verification_required"])
        session.close()

    def test_no_callback_source_ack_and_current_faults_stop_before_formal(self):
        for changes in ({"source_setpoint": .5}, {"current_a": 1e-5 + 1e-12},
                        {"compliance_trip": True}):
            with self.subTest(changes=changes):
                session, adapter, recorder, _clock, _log = self.session_fixture()
                session._guarded_direct_roles.add("gate_bottom")
                adapter.transform = lambda r: replace(r, **changes) if r.source_setpoint == 1 else r
                with self.assertRaises(ThreeSmuSafetyError):
                    session.set_point({"gate_bottom": 1.0})
                errors = [p for kind, p in recorder.events if kind == "gate_direct_error"]
                self.assertEqual(len(errors), 1)
                result = session.cleanup_points(recorder, reason="failed", failed=True)
                self.assertTrue(result["manual_verification_required"])
                self.assertFalse(result["actions"][0]["zero_readback_recorded"])
                session.close()

    def test_no_callback_cleanup_rejects_unverified_actual_zero(self):
        session, adapter, recorder, _clock, _log = self.session_fixture()
        session._guarded_direct_roles.add("gate_bottom")
        session.set_point({"gate_bottom": 1.0})
        adapter.transform = lambda r: replace(r, voltage_v=.050001) if r.source_setpoint == 0 else r
        result = session.cleanup_points(recorder, reason="completed")
        self.assertTrue(result["manual_verification_required"])
        self.assertFalse(result["actions"][0]["zero_readback_recorded"])
        self.assertTrue(result["actions"][0]["output_off_confirmed"])
        session.close()

    def test_unregistered_session_keeps_legacy_single_write_without_extra_reads(self):
        session, _adapter, recorder, _clock, log = self.session_fixture()
        session.set_point({"gate_bottom": 1.0})
        self.assertFalse(any(kind.startswith("gate_direct_") for kind, _ in recorder.events))
        self.assertFalse(any(x[0] == "read" for x in log))
        session.cleanup_points(recorder, reason="completed")
        session.close()

    def test_legacy_off_axis_gates_without_zero_policy_remain_unregistered(self):
        case = continuous_fixture.ContinuousGateCombinationTests.make(self, policy="off_before_axis_change")
        result = self.execute(case)
        self.assertEqual(result["status"], "completed", result)
        self.assertFalse(any(kind.startswith("gate_direct_") for kind, _ in case.events()))

    def test_mixed_bias_optical_configuration_keeps_legacy_path(self):
        case = self.make(policy="off_before_axis_change")
        doc = tomllib.loads(case.path.read_text(encoding="utf-8"))
        smu = tomllib.loads(OPERATION_TEXT)
        doc["smu_bias"] = smu["smu_bias"]
        doc["three_smu_run"]["smu_bias"] = {"role": "fixed", "fixed": 0.0, "bidirectional": False}
        case.path.write_text(toml_document(doc), encoding="utf-8")
        result = self.execute(case)
        self.assertEqual(result["status"], "completed", result)
        self.assertFalse(any(kind.startswith("gate_direct_") for kind, _ in case.events()))

    def test_inactive_bias_hardware_does_not_block_explicit_gate_protection(self):
        case = self.make(policy="off_before_axis_change")
        doc = tomllib.loads(case.path.read_text(encoding="utf-8"))
        doc["smu_bias"] = tomllib.loads(OPERATION_TEXT)["smu_bias"]
        self.assertEqual(doc["three_smu_run"]["smu_bias"]["role"], "off")
        case.path.write_text(toml_document(doc), encoding="utf-8")
        result = self.execute(case)
        self.assertEqual(result["status"], "completed", result)
        self.assertNotIn("smu_bias", case.adapters)
        self.assertTrue(any(kind == "gate_direct_readback" for kind, _ in case.events()))


if __name__ == "__main__":
    unittest.main()
