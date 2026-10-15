"""Fabricated archived event shapes with fake leases, SQLite and readbacks."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import unittest

from attodry_control.combination_owner_resolution import (
    GUARD_IDENTITY_CONTRACT, GUARD_IDENTITY_ERROR, LiveGuardIdentityResolution,
    validate_archived_guard_identity_evidence,
)
from attodry_control.combination_resume import build_recovery, _cleanup_review
from attodry_control.combination_store import CombinationStore, encode, hardware_recovery_state
from tests import test_combination_initialisation_resolution as initialisation
from tests import test_combination_owner_resolution as owners


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class GuardIdentityResolutionTests(unittest.TestCase):
    def setUp(self):
        self.initial = initialisation.InitialisationResolutionTests()
        self.initial.setUp()
        self.addCleanup(self.initial.doCleanups)
        self.initial.claim(self.initial.review(self.initial.capability()))
        self.fixture = self.initial.fixture
        self.database, self.run_id = self.initial.database, self.initial.run_id
        self.old_sources = deepcopy(self.initial.new_sources)
        self.new_sources = {**self.old_sources, **{name: owners.digest("third " + name) for name in
            ("combination_owner_resolution.py", "combination_resume.py", "combination_store.py")}}
        state = hardware_recovery_state(self.database, self.run_id)
        self.execution = state["executions"][-1]["execution_id"]
        self.old_plan = json.loads(state["executions"][-1]["plan_json"])
        self.prior_proof = json.loads(state["executions"][-1]["review_json"])["live_owner_resolution"]
        self.new_plan = {**deepcopy(self.old_plan), "source_sha256": self.new_sources}
        self.config = replace(self.initial.config, snapshot=self.new_plan)
        self.fixture.owner.config = self.config
        identity = self.fixture.identity
        fixtures = Path(__file__).with_name("guard_identity_fixtures")
        actual = json.loads((fixtures / "seq4.json").read_text(encoding="utf-8-sig"))
        self.windows = json.loads((fixtures / "windows.json").read_text(encoding="utf-8-sig"))
        self.cleanup = deepcopy(actual["execution"]["cleanup"])
        self.cleanup["actions"][0]["result"]["final"].update(deepcopy(self.fixture.proof["lockin"]))
        events = deepcopy(actual["events"])
        for event in events:
            payload = event["payload"]
            payload["execution_id"] = self.execution
            if "pid" in payload:
                payload["pid"] = identity["pid"]
            if "run_id" in payload:
                payload["run_id"] = self.run_id
        events[1]["payload"] = {"plan": self.old_plan, "resume": True, "cleanup_only": False}
        events[26]["payload"] = self.cleanup["actions"][0]
        events[28]["payload"] = self.cleanup["actions"][1]
        with CombinationStore(self.database) as store:
            store.current_execution_id = self.execution
            for event in events[:29]:
                store.event(self.run_id, event["event_type"], event["payload"])
                self.set_last_time(store, event["created_at_utc"])
            store.finish_run(self.run_id, "failed", self.cleanup, GUARD_IDENTITY_ERROR)
            self.set_last_time(store, events[29]["created_at_utc"])
            store.event(self.run_id, "data_quality_summary", events[30]["payload"])
            self.set_last_time(store, events[30]["created_at_utc"])
        self.state = hardware_recovery_state(self.database, self.run_id)
        self.directory = self.fixture.fixture.directory / "guard-prior"
        self.directory.mkdir()
        self.paths = {prefix: self.directory / name for prefix, name in (
            ("operator_manifest", "manifest.json"), ("operator_result", "operator-result.json"),
            ("operator_progress", "operator_progress.jsonl"), ("machine_claim", "machine-claim-proof.json"),
            ("adoption_proof", "connection-adoption-proof.json"), ("baseline_proof", "live-baseline-proof.json"),
            ("canonical_apply", "canonical-apply-result.json"), ("windows_event_proof", "windows-pid-reuse-proof.json"))}
        operation = self.fixture.fixture.directory / "retired" / "operation_001"
        operation.mkdir(parents=True)
        self.history = deepcopy(self.windows["historical_identity"])
        self.history.update(run_id=self.run_id, operation_dir=str(operation))
        self.paths["historical_identity"] = operation / "launch-identity.json"
        self.write(self.paths["historical_identity"], self.history)
        state_path = operation.parent / "state.json"
        self.write(state_path, {"adjustments_used": 1, "history": [{"adjustment_number": 1,
            "operation_dir": str(operation), "old_stable_range_k": .05, "new_stable_range_k": .1}]})
        self.manifest = {"kind": "temperature_resume_after_initialisation", "run_id": self.run_id,
            "runtime_source_sha256": self.old_sources, "config_sha256": self.old_plan["config_sha256"],
            "parent_source_sha256": self.initial.old_sources, "initialisation_evidence": self.initial.evidence,
            "historical_evidence_sha256": {str(state_path).replace("\\", "/"): sha(state_path)}}
        self.write(self.paths["operator_manifest"], self.manifest)
        self.windows.update(run_id=self.run_id, failed_execution_id=self.execution, historical_identity=self.history)
        self.write(self.paths["windows_event_proof"], self.windows)
        proof = self.prior_proof
        canonical = encode(proof)
        self.claim_proof = {"run_id": self.run_id, "execution_id": self.execution, "sequence_index": 4,
            "config_sha256": self.old_plan["config_sha256"], "source_sha256": self.old_sources,
            "machine_proof_sha256": hashlib.sha256(canonical.encode()).hexdigest(), "proof_canonical_json": canonical,
            "identity": identity, "historical_cleanup_rewritten": False, "actual_resume_ack_verified": False}
        self.write(self.paths["machine_claim"], self.claim_proof)
        baseline = self.fixture.current_proof()
        self.write(self.paths["baseline_proof"], {"run_id": self.run_id, "identity": identity,
            "manifest_sha256": sha(self.paths["operator_manifest"]), "parent_state_sha256": state["executions"][-1]["parent_state_sha256"],
            "historical_cleanup_rewritten": False, "live_readback": baseline})
        self.write(self.paths["canonical_apply"], baseline["baseline_restoration"])
        self.adoption = {"run_id": self.run_id, "manifest_sha256": sha(self.paths["operator_manifest"]),
            **{key: value for key, value in events[4]["payload"].items() if key != "execution_id"}}
        self.write(self.paths["adoption_proof"], self.adoption)
        self.result = {"run_id": self.run_id, "manifest_sha256": sha(self.paths["operator_manifest"]),
            "cli_exit_code": 2, "error": None, "audit_errors": [], "identity": identity, "pid": identity["pid"],
            "historical_cleanup_rewritten": False, "manual_verification_asserted": False,
            "result": {"execution_id": self.execution, "status": "failed", "error": GUARD_IDENTITY_ERROR, "cleanup": self.cleanup}}
        self.write(self.paths["operator_result"], self.result)
        self.progress = []
        for name in ("begin", "Connect", "Disconnect", "end"):
            self.progress.extend([self.event("dll_attempt", symbol="AttoDRY_Interface_" + name),
                self.event("dll_return", symbol="AttoDRY_Interface_" + name, return_code=0)])
        for index, command in ((0, "SLVL 0.004"), (0, "RMOD 2"), (1, "RMOD 0"), (0, "SENS 26"), (1, "SENS 23")):
            self.progress.extend([self.event("visa_write_attempt", resource_index=index, command=command),
                self.event("visa_write_return", resource_index=index, command=command, result=10)])
        self.progress.append(self.event("owned_cryostat_close", closed=True, adopted_by_scan=True, error=None,
            dll_teardown={name: {"attempted": True, "return_code": 0, "error": None} for name in ("Disconnect", "end")}))
        self.write_progress()

    @staticmethod
    def set_last_time(store, value):
        store.connection.execute("UPDATE combination_events SET created_at_utc=? WHERE event_id=(SELECT max(event_id) FROM combination_events)", (value,))
        store.connection.commit()

    @staticmethod
    def write(path, value):
        Path(path).write_text(encode(value), encoding="utf-8")

    def event(self, name, **values):
        return {"event": name, "pid": self.fixture.identity["pid"], "captured_at_utc": "2020-01-02T12:00:20Z", **values}

    def write_progress(self):
        self.paths["operator_progress"].write_text("\n".join(encode(row) for row in self.progress) + "\n", encoding="utf-8")
        self.evidence = {key: value for prefix, path in self.paths.items() for key, value in
            ((prefix + "_path", str(path)), (prefix + "_sha256", sha(path)))}

    def capability(self, **changes):
        values = {"run_id": self.run_id, "parent_state_sha256": self.state["state_sha256"],
            "failed_execution_id": self.execution, "failed_condition_id": self.fixture.failed_condition,
            "failed_attempt_index": None, "expected_primary_error": GUARD_IDENTITY_ERROR,
            "old_source_sha256": self.old_sources, "new_source_sha256": self.new_sources,
            "config_sha256": self.new_plan["config_sha256"], "live_readback": self.fixture.readback,
            "guard_identity_evidence": self.evidence}
        values.update(changes)
        return LiveGuardIdentityResolution(self.fixture.owner, **values)

    def review(self, cap):
        return build_recovery(self.config, self.database, self.run_id, acknowledge_failure=True, live_owner_resolution=cap)

    def claim(self, recovery):
        with CombinationStore(self.database) as store:
            return store.begin_hardware_execution(self.run_id, self.new_plan, self.fixture.plan.conditions(), recovery=recovery)

    def test_synthetic_windows_xml_and_seq4_shapes_authorize_only_live_atomic_seq5_preserving23(self):
        before = deepcopy(self.state)
        cap = self.capability()
        recovery = self.review(cap)
        public = recovery["review"]["live_owner_resolution"]
        self.assertEqual(public["contract"], GUARD_IDENTITY_CONTRACT)
        self.assertFalse(recovery["review"]["manual_verification"])
        self.assertEqual(recovery["review"]["cleanup_review"], _cleanup_review(self.cleanup, ["temperature", "lockin"]))
        self.assertTrue(json.loads(before["run"]["cleanup_json"])["manual_verification_required"])
        self.assertFalse(public["windows_pid_reuse"]["full_failed_cim_snapshot_available"])
        self.assertIsNone(public["failed_attempt_index"])
        self.assertEqual(len(self.claim(recovery)), 23)
        after = hardware_recovery_state(self.database, self.run_id)
        self.assertEqual(after["executions"][:-1], before["executions"])
        self.assertEqual(after["attempts"], before["attempts"])
        self.assertEqual(after["sample_manifest"], before["sample_manifest"])
        self.assertEqual(after["executions"][-1]["sequence_index"], 5)
        with CombinationStore(self.database) as store:
            with self.assertRaisesRegex(ValueError, "consumed"):
                cap.verify(before, self.new_plan, store.connection)
            archived = validate_archived_guard_identity_evidence(store.connection, self.run_id, self.execution,
                self.config, self.old_sources, self.evidence)
        self.assertEqual(archived["terminal_event_sha256"], public["terminal_event_sha256"])

    def test_wrong_failure_attempt_parent_source_or_config_cannot_make_capability_ready(self):
        for changes in ({"expected_primary_error": "ValueError: different"}, {"failed_attempt_index": 0},
                {"parent_state_sha256": "0" * 64}, {"failed_execution_id": "wrong"},
                {"config_sha256": "0" * 64}, {"new_source_sha256": {**self.new_sources, "unchanged_00.py": "0" * 64}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.review(self.capability(**changes))

    def test_wrong_windows_command_pid_event_link_or_timeline_blocks_even_rehashed_proof(self):
        original = deepcopy(self.windows)
        variants = (lambda p: p.update(historical_pid=8212),
            lambda p: p.update(full_failed_cim_snapshot_available=True),
            lambda p: p["legacy_xml"].__setitem__(0, p["legacy_xml"][0].replace("Get-CimInstance", "Set-CimInstance")),
            lambda p: p["operational_xml"].__setitem__(0, p["operational_xml"][0].replace("18612", "18613")),
            lambda p: p.update(cleanup_started_at_utc="2020-01-02T12:01:00Z"))
        for mutate in variants:
            self.windows = deepcopy(original)
            mutate(self.windows)
            self.write(self.paths["windows_event_proof"], self.windows)
            self.write_progress()
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                self.review(self.capability())

    def test_any_ack_attempt_or_extra_fault_raw_event_blocks(self):
        with CombinationStore(self.database) as store:
            store.event(self.run_id, "recovery_preflight_verified", {"execution_id": self.execution})
        self.state = hardware_recovery_state(self.database, self.run_id)
        with self.assertRaisesRegex(ValueError, "unclassified raw events"):
            self.review(self.capability())

    def test_historical_file_mutation_or_fresh_unsafe_frame_rolls_back_atomic_claim(self):
        recovery = self.review(self.capability())
        before = hardware_recovery_state(self.database, self.run_id)
        self.paths["windows_event_proof"].write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "evidence changed"):
            self.claim(recovery)
        self.assertEqual(hardware_recovery_state(self.database, self.run_id), before)
        self.write(self.paths["windows_event_proof"], self.windows)
        recovery = self.review(self.capability())
        self.fixture.proof["cryostat"]["state"]["error_code"] = 1
        with self.assertRaises(ValueError):
            self.claim(recovery)
        self.assertEqual(hardware_recovery_state(self.database, self.run_id), before)

    def test_serialized_or_subclass_capability_and_expired_or_missing_lease_fail_closed(self):
        class Forged(LiveGuardIdentityResolution):
            pass
        with self.assertRaises(ValueError):
            self.review(object.__new__(Forged))
        cap = self.capability()
        recovery = self.review(cap)
        with self.assertRaisesRegex(ValueError, "ephemeral capability"):
            self.claim(json.loads(encode({k: v for k, v in recovery.items() if k != "_live_owner_resolution"})))
        cap._issued_at -= timedelta(seconds=601)
        before = hardware_recovery_state(self.database, self.run_id)
        with self.assertRaisesRegex(ValueError, "stale"):
            self.claim(recovery)
        self.assertEqual(hardware_recovery_state(self.database, self.run_id), before)
        self.fixture.owner.locked = False
        with self.assertRaisesRegex(ValueError, "exclusive lease"):
            self.capability()

    def test_dll_fault_second_connect_unknown_write_or_missing_return_blocks(self):
        original = deepcopy(self.progress)
        for mutate in (lambda p: p[1].update(return_code=1),
                lambda p: p.insert(0, self.event("dll_attempt", symbol="AttoDRY_Interface_setUserTemperature")),
                lambda p: p.pop(1),
                lambda p: p.extend([self.event("dll_attempt", symbol="AttoDRY_Interface_Connect"),
                                   self.event("dll_return", symbol="AttoDRY_Interface_Connect", return_code=0)]),
                lambda p: p.append(self.event("visa_query_return", query="ERRS?", raw_response="4"))):
            self.progress = deepcopy(original)
            mutate(self.progress)
            self.write_progress()
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                self.review(self.capability())

    def test_prior_initialisation_history_and_unsupported_cleanup_cannot_be_reclassified(self):
        with CombinationStore(self.database) as store:
            store.connection.execute("UPDATE combination_events SET created_at_utc=? WHERE event_type='recovery_owner_verification_failed'",
                                     (owners.utc(),))
            store.connection.commit()
        with self.assertRaisesRegex(ValueError, "raw events changed"):
            self.review(self.capability())

    def test_missing_or_different_adopted_connection_or_unknown_baseline_power_blocks(self):
        original = deepcopy(self.adoption)
        for key, value in (("begin_or_connect_replayed", True), ("same_exclusive_lease", False),
                           ("existing_driver_object_id", -1)):
            self.adoption = {**deepcopy(original), key: value}
            self.write(self.paths["adoption_proof"], self.adoption)
            self.write_progress()
            with self.subTest(key=key), self.assertRaises(ValueError):
                self.review(self.capability())
        self.write(self.paths["adoption_proof"], original)
        baseline = json.loads(self.paths["baseline_proof"].read_text())
        baseline["live_readback"]["cryostat"]["heater_power"]["sample_w"] = None
        self.write(self.paths["baseline_proof"], baseline)
        self.write_progress()
        with self.assertRaisesRegex(ValueError, "both heater powers"):
            self.review(self.capability())


if __name__ == "__main__":
    unittest.main()
