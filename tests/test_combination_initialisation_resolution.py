"""Exact pre-acquisition 1556/1026 recovery; fake leases, files and readbacks."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import unittest

from attodry_control.combination_owner_resolution import (
    INITIALISATION_CONTRACT, INITIALISATION_ERROR, LiveInitialisationResolution,
    validate_archived_initialisation_evidence,
)
from attodry_control.combination_resume import build_recovery
from attodry_control.combination_store import CombinationStore, encode, hardware_recovery_state
from tests import test_combination_owner_resolution as owner_fixtures


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class InitialisationResolutionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = owner_fixtures.LiveOwnerResolutionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.database, self.run_id = self.fixture.database, self.fixture.run_id
        with CombinationStore(self.database) as store:
            store.connection.execute("UPDATE combination_executions SET sequence_index=2 WHERE run_id=?", (self.run_id,))
            store.connection.commit()
        self.fixture.state = hardware_recovery_state(self.database, self.run_id)
        recovery = self.fixture.review(self.fixture.capability())
        self.fixture.claim(recovery)
        self.old_sources = deepcopy(self.fixture.new_sources)
        self.new_sources = {**self.old_sources, **{name: owner_fixtures.digest("second " + name) for name in
            ("combination_owner_resolution.py", "combination_resume.py", "combination_store.py")}}
        state = hardware_recovery_state(self.database, self.run_id)
        self.execution = state["executions"][-1]["execution_id"]
        self.old_review = json.loads(state["executions"][-1]["review_json"])
        self.old_plan = json.loads(state["executions"][-1]["plan_json"])
        self.cleanup = {"actions": [
            {"audit_errors": [], "clean": False, "failure_requires_manual_review": True, "module": "lockin",
             "result": {"attempted": False, "reason": "not opened"}, "startup_completed": False},
            {"audit_errors": [], "clean": False, "failure_requires_manual_review": True, "module": "temperature",
             "result": {"attempted": False, "reason": "no writes attempted", "verified": True}, "startup_completed": False}],
            "clean": False, "communication_uncertain": False, "errors": [
                "lockin: cleanup not certified; review recorded actions", "temperature: cleanup not certified; review recorded actions"],
            "manual_verification_required": True}
        identity = self.old_review["live_owner_resolution"]["current_owner"]
        with CombinationStore(self.database) as store:
            store.current_execution_id = self.execution
            store.event(self.run_id, "launch_registered", {"pid": identity["pid"], "run_id": self.run_id})
            store.event(self.run_id, "run_started", {"resume": True, "cleanup_only": False, "plan": self.old_plan})
            for action in self.cleanup["actions"]:
                store.event(self.run_id, "cleanup_started", {"module": action["module"]})
                store.event(self.run_id, "cleanup_result", action)
            store.finish_run(self.run_id, "failed", self.cleanup, INITIALISATION_ERROR)
            store.event(self.run_id, "data_quality_summary", {"rejected_attempt_audit": {"formal_pairs": 0}})
        self.new_plan = {**deepcopy(self.old_plan), "source_sha256": self.new_sources}
        self.config = replace(self.fixture.config, snapshot=self.new_plan)
        self.fixture.owner.config = self.config
        self.state = hardware_recovery_state(self.database, self.run_id)
        self.directory = self.fixture.fixture.directory / "prior-operator"
        self.directory.mkdir()
        self.paths = {prefix: self.directory / filename for prefix, filename in (
            ("operator_manifest", "manifest.json"), ("operator_result", "operator-result.json"),
            ("operator_progress", "operator_progress.jsonl"), ("machine_claim", "machine-claim-proof.json"))}
        manifest = {"version": 1, "kind": "laser_owner_proof_backed_resume", "run_id": self.run_id,
                    "runtime_source_sha256": self.old_sources, "config_sha256": self.new_plan["config_sha256"]}
        self.paths["operator_manifest"].write_text(encode(manifest), encoding="utf-8")
        self.result = {"run_id": self.run_id, "manifest_sha256": sha(self.paths["operator_manifest"]),
            "cli_exit_code": 2, "error": None, "audit_errors": [], "historical_cleanup_rewritten": False,
            "manual_verification_asserted": False, "pid": identity["pid"], "identity": identity,
            "result": {"execution_id": self.execution, "status": "failed", "error": INITIALISATION_ERROR, "cleanup": self.cleanup}}
        proof = self.old_review["live_owner_resolution"]
        canonical = encode(proof)
        claim = {"proof_canonical_json": canonical, "machine_proof_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
            "run_id": self.run_id, "execution_id": self.execution, "sequence_index": 3,
            "source_sha256": self.old_sources, "config_sha256": self.new_plan["config_sha256"],
            "historical_cleanup_rewritten": False, "actual_resume_ack_verified": False, "identity": identity}
        self.paths["machine_claim"].write_text(encode(claim), encoding="utf-8")
        self.progress = [self.event("dll_attempt", symbol="AttoDRY_Interface_getSampleTemperature"),
                         self.event("dll_return", symbol="AttoDRY_Interface_getSampleTemperature", return_code=0),
                         self.event("validation_connections_closed", closed=True, errors=[])]
        for name, code in (("begin", 0), ("Connect", 0), ("isDeviceInitialised", 1556), ("Disconnect", 0), ("end", 1026)):
            self.progress.extend((self.event("dll_attempt", symbol="AttoDRY_Interface_" + name),
                                  self.event("dll_return", symbol="AttoDRY_Interface_" + name, return_code=code)))
        self.write_evidence()

    def event(self, name, **values):
        return {"event": name, "pid": self.fixture.identity["pid"], "captured_at_utc": owner_fixtures.utc(), **values}

    def write_evidence(self):
        self.paths["operator_result"].write_text(encode(self.result), encoding="utf-8")
        self.paths["operator_progress"].write_text("\n".join(encode(event) for event in self.progress) + "\n", encoding="utf-8")
        self.evidence = {key: value for prefix, path in self.paths.items()
                         for key, value in ((prefix + "_path", str(path)), (prefix + "_sha256", sha(path)))}

    def capability(self, **changes):
        values = {"run_id": self.run_id, "parent_state_sha256": self.state["state_sha256"],
            "failed_execution_id": self.execution, "failed_condition_id": self.fixture.failed_condition,
            "failed_attempt_index": None, "expected_primary_error": INITIALISATION_ERROR,
            "old_source_sha256": self.old_sources, "new_source_sha256": self.new_sources,
            "config_sha256": self.new_plan["config_sha256"], "live_readback": self.fixture.readback,
            "initialisation_evidence": self.evidence}
        values.update(changes)
        return LiveInitialisationResolution(self.fixture.owner, **values)

    def review(self, cap):
        return build_recovery(self.config, self.database, self.run_id, acknowledge_failure=True, live_owner_resolution=cap)

    def claim(self, recovery):
        with CombinationStore(self.database) as store:
            return store.begin_hardware_execution(self.run_id, self.new_plan, self.fixture.plan.conditions(), recovery=recovery)

    def test_exact_startup_resolution_retains23_and_repeats_whole24th_preserving_both_failed_executions(self):
        before = deepcopy(self.state)
        recovery = self.review(self.capability())
        review = recovery["review"]
        self.assertTrue(review["ready"])
        self.assertFalse(review["manual_verification"])
        self.assertFalse(review["cleanup_review"]["communication_uncertain"])
        self.assertTrue(review["cleanup_review"]["manual_verification_required"])
        self.assertEqual(review["live_owner_resolution"]["contract"], INITIALISATION_CONTRACT)
        self.assertIsNone(review["live_owner_resolution"]["failed_attempt_index"])
        self.assertFalse(review["live_owner_resolution"]["historical_communication_certified"])
        self.assertEqual(review["source_compatibility"], INITIALISATION_CONTRACT)
        self.assertEqual(recovery["next_condition"], self.fixture.plan.conditions()[23])
        self.assertEqual(len(self.claim(recovery)), 23)
        after = hardware_recovery_state(self.database, self.run_id)
        self.assertEqual(after["executions"][:-1], before["executions"])
        self.assertEqual(after["attempts"], before["attempts"])
        self.assertEqual(after["sample_manifest"], before["sample_manifest"])
        self.assertEqual(after["executions"][-1]["sequence_index"], 4)
        with CombinationStore(self.database) as store:
            store.current_execution_id = after["executions"][-1]["execution_id"]
            self.assertEqual(store.begin_attempt(self.run_id, self.fixture.failed_condition), 1)

    def test_pure_archived_classifier_still_valid_after_new_execution_claim_without_authorizing_anything(self):
        recovery = self.review(self.capability())
        self.claim(recovery)
        with CombinationStore(self.database) as store:
            proof = validate_archived_initialisation_evidence(store.connection, self.run_id, self.execution,
                                                             self.config, self.old_sources, self.evidence)
        self.assertEqual([row["return_code"] for row in proof["startup_dll_returns"]], [0, 0, 1556, 0, 1026])

    def test_wrong_primary_attempt_identity_source_or_config_is_rejected(self):
        changes = ({"expected_primary_error": INITIALISATION_ERROR.replace("1556", "1557")},
                   {"failed_attempt_index": 0}, {"parent_state_sha256": "0" * 64},
                   {"failed_execution_id": "wrong"}, {"config_sha256": "0" * 64},
                   {"new_source_sha256": {**self.new_sources, "unchanged_00.py": "0" * 64}})
        for value in changes:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.review(self.capability(**value))

    def test_any_attempt_preflight_ack_or_extra_fault_event_blocks_initialisation_classification(self):
        for kind in ("recovery_preflight_verified", "condition_started", "raw_reading", "another_error"):
            with CombinationStore(self.database) as store:
                store.event(self.run_id, kind, {"execution_id": self.execution})
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, "extra fault events"):
                self.review(self.capability())
            with CombinationStore(self.database) as store:
                store.connection.execute("DELETE FROM combination_events WHERE event_type=?", (kind,))
                store.connection.commit()
        with CombinationStore(self.database) as store:
            store.current_execution_id = self.execution
            # Simulate a corrupted/reclassified startup that actually opened an attempt.
            store.connection.execute("UPDATE combination_executions SET status='active' WHERE execution_id=?", (self.execution,))
            store.begin_attempt(self.run_id, self.fixture.failed_condition)
            store.connection.execute("UPDATE combination_executions SET status='failed' WHERE execution_id=?", (self.execution,))
            store.connection.commit()
        self.state = hardware_recovery_state(self.database, self.run_id)
        with self.assertRaises(ValueError):
            self.review(self.capability())

    def test_raw_unwind_other_errors_extra_io_or_preceding_dll_writes_block_even_with_rehashed_files(self):
        original = deepcopy(self.progress)
        variants = (
            lambda rows: rows[-1].update(return_code=0),
            lambda rows: rows[-5].update(return_code=1557),
            lambda rows: rows.insert(0, self.event("dll_attempt", symbol="AttoDRY_Interface_setUserTemperature")),
            lambda rows: rows.insert(0, self.event("dll_return", symbol="AttoDRY_Interface_getSampleTemperature", return_code=1)),
            lambda rows: rows.append(self.event("dll_attempt", symbol="AttoDRY_Interface_getSampleTemperature")),
            lambda rows: rows.insert(0, self.event("secondary_error", error="communication lost")),
        )
        for mutate in variants:
            self.progress = deepcopy(original)
            mutate(self.progress)
            self.write_evidence()
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                self.review(self.capability())

    def test_frozen_evidence_change_between_review_and_atomic_claim_blocks_without_db_writes(self):
        recovery = self.review(self.capability())
        before = hardware_recovery_state(self.database, self.run_id)
        self.paths["operator_progress"].write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "evidence changed"):
            self.claim(recovery)
        self.assertEqual(hardware_recovery_state(self.database, self.run_id), before)

    def test_prior_seq2_machine_claim_and_raw_event_history_cannot_be_replaced(self):
        with CombinationStore(self.database) as store:
            store.connection.execute("UPDATE combination_events SET created_at_utc=? WHERE event_type='recovery_owner_verification_failed'",
                                     (owner_fixtures.utc(),))
            store.connection.commit()
        with self.assertRaisesRegex(ValueError, "prior owner-resolution raw events changed"):
            self.review(self.capability())

    def test_serialized_initialisation_review_cannot_claim_and_capability_is_single_use(self):
        cap = self.capability()
        recovery = self.review(cap)
        public = {key: value for key, value in recovery.items() if key != "_live_owner_resolution"}
        with self.assertRaisesRegex(ValueError, "ephemeral capability"):
            self.claim(json.loads(encode(public)))
        self.claim(recovery)
        with CombinationStore(self.database) as store, self.assertRaisesRegex(ValueError, "consumed"):
            cap.verify(self.state, self.new_plan, store.connection)

    def test_subclass_or_missing_lease_cannot_authorize_initialisation_resolution(self):
        class Forged(LiveInitialisationResolution):
            pass
        with self.assertRaisesRegex(ValueError, "ephemeral capability"):
            self.review(object.__new__(Forged))
        self.fixture.owner.locked = False
        with self.assertRaisesRegex(ValueError, "exclusive lease"):
            self.capability()

    def test_expired_capability_or_failed_fresh_frame_rolls_back_atomic_claim(self):
        cap = self.capability()
        recovery = self.review(cap)
        before = hardware_recovery_state(self.database, self.run_id)
        cap._issued_at -= timedelta(seconds=601)
        with self.assertRaisesRegex(ValueError, "stale"):
            self.claim(recovery)
        self.assertEqual(hardware_recovery_state(self.database, self.run_id), before)
        cap = self.capability()
        recovery = self.review(cap)
        self.fixture.proof["cryostat"]["state"]["error_code"] = 1
        with self.assertRaisesRegex(ValueError, "error/control state"):
            self.claim(recovery)
        self.assertEqual(hardware_recovery_state(self.database, self.run_id), before)

    def test_cleanup_that_attempted_writes_or_claimed_startup_success_is_not_eligible(self):
        for mutate in (lambda c: c["actions"][0].update(startup_completed=True),
                       lambda c: c["actions"][1]["result"].update(attempted=True),
                       lambda c: c.update(communication_uncertain=True)):
            changed = deepcopy(self.cleanup)
            mutate(changed)
            with CombinationStore(self.database) as store:
                store.connection.execute("UPDATE combination_runs SET cleanup_json=? WHERE run_id=?", (encode(changed), self.run_id))
                store.connection.execute("UPDATE combination_executions SET cleanup_json=? WHERE execution_id=?", (encode(changed), self.execution))
                store.connection.commit()
            self.state = hardware_recovery_state(self.database, self.run_id)
            with self.subTest(mutate=mutate), self.assertRaisesRegex(ValueError, "unattempted startup failure"):
                self.review(self.capability())

    def test_actual_small_station_fixture_matches_exact_cleanup_and_1556_1026_tail(self):
        path = Path(__file__).resolve().parents[1] / "run_data" / \
            "operator_temperature_resume_after_initialisation_20261014T200500Z/seq3-startup-failure-fixture.json"
        if not path.is_file():
            self.skipTest("same-station startup fixture is unavailable")
        actual = json.loads(path.read_text(encoding="utf-8-sig"))
        self.assertEqual(actual["execution"]["cleanup"], self.cleanup)
        self.assertEqual(actual["execution"]["error"], INITIALISATION_ERROR)
        self.assertEqual([(row["symbol"].removeprefix("AttoDRY_Interface_"), row["return_code"])
                          for row in actual["dll_returns"][-5:]],
                         [("begin", 0), ("Connect", 0), ("isDeviceInitialised", 1556), ("Disconnect", 0), ("end", 1026)])


if __name__ == "__main__":
    unittest.main()
