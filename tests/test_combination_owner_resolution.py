"""One owner-guard resolution under a fake live lease/readback provider only."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import pickle
import unittest
from unittest.mock import Mock, patch

from attodry_control.combination_hardware import load_hardware_combination
from attodry_control.combination_owner_resolution import LiveOwnerResolution, validate_archived_readback
from attodry_control.combination_ownership import RecoveryOwner
from attodry_control.combination_resume import build_recovery
from attodry_control.combination_store import CombinationStore, encode, hardware_recovery_state
from attodry_control.sr830 import RESERVE_MODE_CODES
from attodry_control.sr830_settings import map_sr830_settings
from tests import test_combination_cryostat as cryostat_fixtures
from tests import test_combination_resume_store as store_fixtures


ERROR = "ValueError: Competing or unknown Python owner: PID 12920"


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def utc():
    return datetime.now(timezone.utc).isoformat()


class LiveOwnerResolutionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = cryostat_fixtures.CryostatCombinationTests("test_all_64_nonempty_orders_share_one_connection")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.write_config(("temperature", "lockin"))
        actual = load_hardware_combination(self.fixture.path)
        self.store_fixture = store_fixtures.RecoveryStoreTests()
        self.store_fixture.setUp()
        self.addCleanup(self.store_fixture.doCleanups)
        self.plan = self.store_fixture.plan
        self.database = self.store_fixture.path
        self.run_id = "owner-resolution-342"
        self.old_sources = {f"unchanged_{index:02}.py": digest(str(index)) for index in range(77)}
        self.old_sources.update({"combination_resume.py": digest("old resume"),
                                 "combination_store.py": digest("old store")})
        self.new_sources = {**self.old_sources, "combination_resume.py": digest("new resume"),
                            "combination_store.py": digest("new store"),
                            "combination_owner_resolution.py": digest("new resolution")}
        self.old_plan = deepcopy(self.store_fixture.old_plan)
        self.old_plan.update(source_sha256=self.old_sources, config_sha256=actual.snapshot["config_sha256"])
        self.old_plan["hardware"]["lockin"]["safety_sha256"] = actual.snapshot["hardware"]["lockin"]["safety_sha256"]
        self.new_plan = deepcopy(self.old_plan)
        self.new_plan["source_sha256"] = self.new_sources
        self.config = replace(actual, plan=self.plan, snapshot=self.new_plan)
        self.identity = {"pid": os.getpid(), "parent": 1000, "name": "python.exe",
                         "created": "2026-10-14T19:00:00Z", "command": "python offline_resolution.py"}
        self.owner = RecoveryOwner(self.config, inventory=lambda: [self.identity], idle_reader=lambda rows: set())
        self.owner.locked = True  # Never acquire any real mutex or device.
        self.owner.identity = deepcopy(self.identity)
        self.owner.check = Mock(side_effect=self.checked_owner)
        self.proof = self.make_proof()
        self.readback = Mock(side_effect=self.current_proof)
        self.cleanup = {"clean": False, "communication_uncertain": True, "manual_verification_required": True,
            "errors": [f"{module}: cleanup not certified; review recorded actions" for module in ("lockin", "temperature")],
            "actions": [{"module": module, "clean": False, "failure_requires_manual_review": True,
                         "startup_completed": True, "audit_errors": [],
                         "result": {"attempted": False, "verified": False, "communication_uncertain": True,
                                    "manual_verification_required": True, "owner_guard_error": ERROR,
                                    "last_confirmed_state_not_current": {"not_current": True}}}
                        for module in ("lockin", "temperature")]}
        with CombinationStore(self.database) as store:
            store.begin_hardware_execution(self.run_id, self.old_plan, self.plan.conditions())
            self.execution = store.current_execution_id
            for condition in self.plan.conditions()[:23]:
                attempt = store.begin_attempt(self.run_id, condition["condition_id"])
                store.sample(self.run_id, condition["condition_id"], attempt, 0, store_fixtures.recorded_sample(condition))
                store.finish_attempt(self.run_id, condition["condition_id"], attempt, expected_samples=1)
            self.failed_condition = self.plan.conditions()[23]["condition_id"]
            self.failed_attempt = store.begin_attempt(self.run_id, self.failed_condition)
            store.event(self.run_id, "recovery_owner_verification_failed", {
                "condition_id": self.failed_condition, "attempt_index": self.failed_attempt,
                "error": ERROR, "further_instrument_writes_blocked": True,
                "hardware_zero_or_off_confirmed": False})
            store.finish_attempt(self.run_id, self.failed_condition, self.failed_attempt,
                                 expected_samples=1, error=ERROR)
            for action in self.cleanup["actions"]:
                store.event(self.run_id, "cleanup_result", action)
            store.finish_run(self.run_id, "failed", self.cleanup, ERROR)
        self.state = hardware_recovery_state(self.database, self.run_id)

    def checked_owner(self):
        return {"pid": self.owner.identity["pid"], "created": self.owner.identity["created"],
                "command": self.owner.identity["command"], "verified_owners": [self.owner.identity]}

    def current_proof(self):
        proof = deepcopy(self.proof)
        for value in (proof, proof["cryostat"], *proof["lockin"].values()):
            value.update(started_at_utc=utc(), captured_at_utc=utc())
        return proof

    def diagnostic(self, role):
        target = getattr(self.config.lockin, role)
        codes = map_sr830_settings(**{key: getattr(target, key) for key in
            ("reference_source", "external_reference_edge", "input_mode", "shield_grounding",
             "input_coupling", "time_constant_s", "filter_slope_db_oct", "sensitivity_full_scale_v")})
        return {"role": role.removeprefix("lockin_"), "identity": "Stanford Research Systems,SR830," + role,
                "reference_mode": codes.reference_source, "reference_slope": codes.external_reference_edge or 0,
                "frequency_hz": target.frequency_hz, "snapshot_frequency_hz": target.frequency_hz,
                "harmonic": 1, "sine_output_v": .004, "input_mode": codes.input_mode,
                "shield_grounding": codes.shield_grounding, "input_coupling": codes.input_coupling,
                "line_filter": 0, "sensitivity": codes.sensitivity,
                "reserve_mode": RESERVE_MODE_CODES[target.reserve_mode.value],
                "time_constant": codes.time_constant, "filter_slope": codes.filter_slope,
                "phase_shift_deg": 0.0, "x_v": .0001, "y_v": 0.0, "amplitude_v": .0001,
                "phase_deg": 0.0, "lia_status": {"raw": 0, **{key: False for key in
                    ("input_or_reserve_overload", "filter_overload", "output_overload", "reference_unlocked",
                     "frequency_range_changed", "time_constant_changed", "triggered")}}, "error_status": 0}

    def make_proof(self):
        roles = {role: self.diagnostic(role) for role in ("lockin_xx", "lockin_xy")}
        return {"captured_at_utc": utc(), "cryostat": {"state": {
                    "sample_temperature_k": 11.7, "user_temperature_k": 11.7, "vti_temperature_k": 4.8,
                    "temperature_control_enabled": True, "field_control_enabled": False, "error_code": 0,
                    "field": {"bx_t": 0.0, "bz_t": 0.0}, "field_setpoint": {"bx_t": 0.0, "bz_t": 0.0}},
                    "heater_power": {"sample_w": .1, "vti_w": .2}}, "lockin": roles,
                "baseline_restoration": {"attempted": True, "verified": True, "errors": [], "raw": {
                    "scan": "apply_toml", "completed": True, "outcome": "completed",
                    "communication_uncertain": False, "manual_verification_required": False,
                    "actions": [{"role": "lockin_xx", "method": "set_sine_output", "arguments": [.004]}],
                    "requested": {"harmonic": 1, "xx_source_voltage_v": .004,
                                  "frequency_hz": self.config.lockin.lockin_xx.frequency_hz},
                    "lockin_safety_sha256": self.config.snapshot["hardware"]["lockin"]["safety_sha256"],
                    "after": deepcopy(roles)}}}

    def capability(self, **changes):
        args = {"run_id": self.run_id, "parent_state_sha256": self.state["state_sha256"],
                "failed_execution_id": self.execution, "failed_condition_id": self.failed_condition,
                "failed_attempt_index": self.failed_attempt, "expected_primary_error": ERROR,
                "old_source_sha256": self.old_sources, "new_source_sha256": self.new_sources,
                "config_sha256": self.new_plan["config_sha256"], "live_readback": self.readback}
        args.update(changes)
        return LiveOwnerResolution(self.owner, **args)

    def review(self, cap):
        return build_recovery(self.config, self.database, self.run_id,
                              acknowledge_failure=True, live_owner_resolution=cap)

    def claim(self, recovery):
        with CombinationStore(self.database) as store:
            return store.begin_hardware_execution(self.run_id, self.new_plan,
                                                  self.plan.conditions(), recovery=recovery)

    def test_valid_capability_keeps_23_accepted_and_repeats_whole_24th_without_rewriting_cleanup(self):
        cap = self.capability()
        before = deepcopy(self.state)
        recovery = self.review(cap)
        self.assertTrue(recovery["review"]["ready"])
        self.assertFalse(recovery["review"]["manual_verification"])
        self.assertTrue(recovery["review"]["cleanup_review"]["communication_uncertain"])
        self.assertTrue(recovery["review"]["cleanup_review"]["manual_verification_required"])
        self.assertEqual(recovery["next_condition"], self.plan.conditions()[23])
        self.assertEqual(recovery["accepted_conditions"], 23)
        self.assertEqual(len(self.claim(recovery)), 23)
        after = hardware_recovery_state(self.database, self.run_id)
        self.assertEqual(after["attempts"], before["attempts"])
        self.assertEqual(after["sample_manifest"], before["sample_manifest"])
        self.assertEqual(after["executions"][0], before["executions"][0])
        archived = json.loads(after["executions"][-1]["review_json"])
        self.assertEqual(archived["cleanup_review"], recovery["review"]["cleanup_review"])
        self.assertFalse(archived["manual_verification"])
        self.assertIn("claim_readbacks", archived["live_owner_resolution"])
        with CombinationStore(self.database) as store:
            store.current_execution_id = after["executions"][-1]["execution_id"]
            self.assertEqual(store.begin_attempt(self.run_id, self.failed_condition), self.failed_attempt + 1)
        self.assertGreaterEqual(self.readback.call_count, 4)

    def test_serialized_or_forged_resolution_and_manual_flag_cannot_authorize(self):
        for forged in ({"ready": True}, "certificate.json", True, object()):
            with self.subTest(forged=forged), self.assertRaises(ValueError):
                self.review(forged)
        cap = self.capability()
        with self.assertRaises(ValueError):
            build_recovery(self.config, self.database, self.run_id, acknowledge_failure=True,
                           live_owner_resolution=cap, manual_verification=True)
        recovery = self.review(cap)
        recovery.pop("_live_owner_resolution")
        serialized = json.loads(encode(recovery))
        with self.assertRaisesRegex(ValueError, "ephemeral capability"):
            self.claim(serialized)
        for operation in (lambda: pickle.dumps(cap), lambda: deepcopy(cap)):
            with self.assertRaises(TypeError):
                operation()

    def test_wrong_parent_execution_attempt_run_and_config_are_rejected(self):
        cases = ({"parent_state_sha256": "0" * 64}, {"run_id": "other"},
                 {"failed_execution_id": "other"}, {"failed_condition_id": self.plan.conditions()[24]["condition_id"]},
                 {"failed_attempt_index": 99}, {"config_sha256": "0" * 64})
        for change in cases:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.review(self.capability(**change))

    def test_only_pinned_minimal_source_transition_is_allowed(self):
        cases = ({**self.new_sources, "unrelated.py": "0" * 64},
                 {**self.new_sources, "unchanged_00.py": "0" * 64},
                 {**self.new_sources, "combination_resume.py": self.old_sources["combination_resume.py"]})
        for sources in cases:
            with self.subTest(sources=sources), self.assertRaises(ValueError):
                self.capability(new_source_sha256=sources)

    def test_stale_capability_stale_current_readback_and_mutated_issued_proof_block(self):
        cap = self.capability()
        cap._issued_at -= timedelta(seconds=601)
        with self.assertRaisesRegex(ValueError, "stale"):
            self.review(cap)
        self.readback.side_effect = lambda: {**deepcopy(self.proof), "captured_at_utc":
                                            (datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat()}
        with self.assertRaisesRegex(ValueError, "stale"):
            self.capability()
        self.readback.side_effect = self.current_proof
        cap = self.capability()
        cap._issued_proof["cryostat"]["state"]["sample_temperature_k"] = 12.0
        with self.assertRaisesRegex(ValueError, "proof changed"):
            self.review(cap)
        cap = self.capability()
        def delayed_frame():
            cap._issued_at -= timedelta(seconds=601)
            return self.current_proof()
        self.readback.side_effect = delayed_frame
        with self.assertRaisesRegex(ValueError, "expired during"):
            self.review(cap)

    def test_active_lease_and_exact_current_pid_identity_are_required_at_issue_and_claim(self):
        self.owner.locked = False
        with self.assertRaisesRegex(ValueError, "exclusive lease"):
            self.capability()
        self.owner.locked = True
        cap = self.capability()
        recovery = self.review(cap)
        self.owner.locked = False
        with self.assertRaisesRegex(ValueError, "exclusive lease"):
            self.claim(recovery)
        self.owner.locked = True
        self.owner.identity["pid"] += 1
        with self.assertRaisesRegex(ValueError, "identity"):
            self.claim(recovery)

    def test_new_owner_guard_failure_between_file_review_and_atomic_claim_blocks(self):
        cap = self.capability()
        recovery = self.review(cap)
        self.owner.check.side_effect = ValueError("new unapproved Python")
        before = hardware_recovery_state(self.database, self.run_id)
        with self.assertRaisesRegex(ValueError, "unapproved"):
            self.claim(recovery)
        self.assertEqual(hardware_recovery_state(self.database, self.run_id), before)

    def test_unsafe_fresh_readback_between_review_and_claim_blocks_before_any_db_write(self):
        recovery = self.review(self.capability())
        before = hardware_recovery_state(self.database, self.run_id)
        self.proof["lockin"]["lockin_xx"]["sine_output_v"] = .38
        with self.assertRaisesRegex(ValueError, "4 mVrms"):
            self.claim(recovery)
        self.assertEqual(hardware_recovery_state(self.database, self.run_id), before)

    def test_secondary_failure_event_and_mutated_terminal_event_are_rejected(self):
        cap = self.capability()
        recovery = self.review(cap)
        with CombinationStore(self.database) as store:
            store.event(self.run_id, "environment_after_read_failed", {"execution_id": self.execution, "error": "USB lost"})
        with self.assertRaisesRegex(ValueError, "secondary"):
            self.claim(recovery)
        with CombinationStore(self.database) as store:
            store.connection.execute("DELETE FROM combination_events WHERE event_type='environment_after_read_failed'")
            store.connection.execute("UPDATE combination_events SET created_at_utc=? WHERE event_type='cleanup_result'", (utc(),))
            store.connection.commit()
        with self.assertRaisesRegex(ValueError, "event evidence changed"):
            self.claim(recovery)

    def test_claimed_capability_cannot_be_replayed_even_against_copied_parent_inputs(self):
        cap = self.capability()
        recovery = self.review(cap)
        self.claim(recovery)
        with CombinationStore(self.database) as store, self.assertRaisesRegex(ValueError, "consumed"):
            cap.verify(self.state, self.new_plan, store.connection)

    def test_unknown_hard_bound_field_control_power_and_baseline_readbacks_are_rejected(self):
        mutations = (
            lambda p: p["cryostat"]["state"].update(error_code=1),
            lambda p: p["cryostat"]["state"].update(sample_temperature_k=301),
            lambda p: p["cryostat"]["state"].update(user_temperature_k=None),
            lambda p: p["cryostat"]["state"].update(vti_temperature_k=float("nan")),
            lambda p: p["cryostat"]["state"].update(temperature_control_enabled=None),
            lambda p: p["cryostat"]["state"]["field"].update(bx_t=.1),
            lambda p: p["cryostat"]["state"]["field_setpoint"].update(bz_t=.1),
            lambda p: p["cryostat"]["heater_power"].pop("vti_w"),
            lambda p: p["lockin"]["lockin_xx"].update(sine_output_v=.38),
            lambda p: p["lockin"]["lockin_xy"].update(harmonic=2),
            lambda p: p["lockin"]["lockin_xy"].update(sensitivity=0),
            lambda p: p["lockin"]["lockin_xy"].update(error_status=None),
            lambda p: p["lockin"]["lockin_xy"]["lia_status"].update(raw=1, input_or_reserve_overload=True),
            lambda p: p["baseline_restoration"]["raw"].update(completed=False),
            lambda p: p["baseline_restoration"]["raw"].update(communication_uncertain=True),
            lambda p: p["baseline_restoration"]["raw"].update(manual_verification_required=True),
            lambda p: p["baseline_restoration"]["raw"].update(error="close failed"),
            lambda p: p.pop("baseline_restoration"),
        )
        original = deepcopy(self.proof)
        for mutate in mutations:
            self.proof = deepcopy(original)
            mutate(self.proof)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                self.capability()

    def test_owner_guard_primary_string_alone_cannot_resolve_attempted_or_secondary_cleanup(self):
        for mutate in (
            lambda cleanup: cleanup["actions"][0]["result"].update(attempted=True),
            lambda cleanup: cleanup["actions"][0]["audit_errors"].append("lost evidence"),
            lambda cleanup: cleanup["errors"].append("close: USB lost"),
        ):
            changed = deepcopy(self.cleanup)
            mutate(changed)
            with CombinationStore(self.database) as store:
                store.connection.execute("UPDATE combination_runs SET cleanup_json=? WHERE run_id=?", (encode(changed), self.run_id))
                store.connection.execute("UPDATE combination_executions SET cleanup_json=? WHERE run_id=?", (encode(changed), self.run_id))
                store.connection.commit()
            self.state = hardware_recovery_state(self.database, self.run_id)
            with self.subTest(mutation=mutate), self.assertRaises(ValueError):
                self.review(self.capability())

    def test_raw_review_tampering_does_not_override_live_capability(self):
        recovery = self.review(self.capability())
        recovery["review"]["cleanup_review"]["communication_uncertain"] = False
        with self.assertRaisesRegex(ValueError, "Unresolved cleanup"):
            self.claim(recovery)

    def test_archived_readback_validator_reuses_actual_observation_times_without_creating_capability(self):
        proof = self.current_proof()
        checked_at = utc()
        self.assertEqual(validate_archived_readback(proof, self.config, checked_at), proof)
        # A later wall clock does not invalidate an archived, timely claim.
        with patch("attodry_control.combination_owner_resolution.datetime") as clock:
            clock.fromisoformat.side_effect = datetime.fromisoformat
            self.assertEqual(validate_archived_readback(proof, self.config, checked_at), proof)
            clock.now.assert_not_called()
        stale = deepcopy(proof)
        stale["lockin"]["lockin_xy"]["started_at_utc"] = (
            datetime.now(timezone.utc) - timedelta(seconds=61)).isoformat()
        with self.assertRaisesRegex(ValueError, "observation timing"):
            validate_archived_readback(stale, self.config, checked_at)
        self.assertEqual(self.readback.call_count, 0)

    def test_line_filter_identity_and_phase_are_preserved_from_actual_repair(self):
        for key, value in (("line_filter", 1), ("line_filter", None),
                           ("phase_shift_deg", .1), ("identity", "another-SR830")):
            original = deepcopy(self.proof)
            self.proof["lockin"]["lockin_xy"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.capability()
            self.proof = original

    def test_real_station_terminal_cleanup_event_shape_is_accepted_without_rewriting_it(self):
        # Operational fixture stays uncommitted in run_data; normal repository
        # test runs remain independent of private experimental files.
        path = Path(__file__).resolve().parents[1] / "run_data" / \
            "operator_laser_owner_resume_20261014T190200Z" / "actual-terminal-events.json"
        if not path.is_file():
            self.skipTest("same-station terminal fixture is not available")
        fixture = json.loads(path.read_text(encoding="utf-8-sig"))
        cleanup_json = fixture["run"]["cleanup_json"]
        with CombinationStore(self.database) as store:
            store.connection.execute("UPDATE combination_runs SET cleanup_json=? WHERE run_id=?", (cleanup_json, self.run_id))
            store.connection.execute("UPDATE combination_executions SET cleanup_json=? WHERE run_id=?", (cleanup_json, self.run_id))
            store.connection.execute("DELETE FROM combination_events WHERE run_id=?", (self.run_id,))
            for event in fixture["events"]:
                payload = json.loads(event["payload_json"])
                payload["execution_id"] = self.execution
                store.connection.execute("INSERT INTO combination_events VALUES (?,?,?,?,?)", (
                    event["event_id"], self.run_id, event.get("created_at_utc", utc()), event["event_type"], encode(payload)))
            store.connection.commit()
        self.state = hardware_recovery_state(self.database, self.run_id)
        recovery = self.review(self.capability())
        self.assertTrue(recovery["review"]["ready"])
        self.assertTrue(recovery["review"]["cleanup_review"]["communication_uncertain"])
        self.claim(recovery)
        self.assertEqual(hardware_recovery_state(self.database, self.run_id)["executions"][0]["cleanup_json"], cleanup_json)

    def test_hidden_archived_hard_field_control_or_lock_faults_block_owner_only_resolution(self):
        state = deepcopy(self.proof["cryostat"]["state"])
        for mutation in (
            lambda s: s.update(sample_temperature_k=301),
            lambda s: s.update(user_temperature_k=None),
            lambda s: s.update(temperature_control_enabled=None),
            lambda s: s["field"].update(bz_t=3.01),
            lambda s: s["field_setpoint"].update(bx_t=3.01),
        ):
            changed = deepcopy(state)
            mutation(changed)
            with CombinationStore(self.database) as store:
                store.event(self.run_id, "cryostat_state", {"execution_id": self.execution, "state": changed})
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, "archived"):
                self.review(self.capability())
            with CombinationStore(self.database) as store:
                store.connection.execute("DELETE FROM combination_events WHERE event_type='cryostat_state'")
                store.connection.commit()


if __name__ == "__main__":
    unittest.main()
