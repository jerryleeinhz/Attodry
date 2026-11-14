"""A complete fake no-write Connect-busy audit; no station data or hardware."""
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from attodry_control import combination_resume as resume
from attodry_control.combination_store import (
    CombinationStore, hardware_recovery_state, open_readonly, validate_accepted_records,
)
from tests.test_attodry import FakeAttoDryDll
from tests import test_temperature_excitation_off_guard as fixtures


class BusyPreflightSourceTests(unittest.TestCase):
    def test_only_reviewed_predecessor_and_running_recovery_file_are_supported(self):
        after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                 for p in Path(resume.__file__).parent.glob("*.py")}
        before = dict(after, **{"combination_resume.py": hashlib.sha256(b"reviewed parent").hexdigest()})
        with patch.object(resume, "BUSY_PREFLIGHT_PREDECESSOR_SETS", {resume.digest(before)}):
            self.assertEqual(resume.source_compatibility(before, after), resume.BUSY_PREFLIGHT_CONTRACT)
            for name in ("combination_resume.py", "combination_store.py", "attodry.py", "keithley6221_ac.py"):
                changed = dict(after, **{name: "0" * 64})
                with self.subTest(name=name):
                    self.assertIsNone(resume.source_compatibility(before, changed))
            self.assertIsNone(resume.source_compatibility(before, dict(after, **{"extra.py": "0" * 64})))
            missing = dict(after)
            missing.pop("combination_store.py")
            self.assertIsNone(resume.source_compatibility(before, missing))
            unknown = dict(before, **{"combination_resume.py": "1" * 64})
            self.assertIsNone(resume.source_compatibility(unknown, after))


class BusyPreflightRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.TemperatureSourceGridTests(
            "test_full_grid_source_is_off_at_every_condition_setting_and_dwell")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.dll = self.fixture.temperature_config()
        self.fixture.path.write_text(self.fixture.path.read_text(encoding="utf-8").replace(
            "compliance_voltage_v = 1.00", "compliance_voltage_v = 20.0"), encoding="utf-8")
        self.current = self.fixture.config()
        previous = deepcopy(self.current.snapshot)
        previous["source_sha256"]["combination_resume.py"] = hashlib.sha256(
            b"reviewed fake preflight predecessor").hexdigest()
        self.previous = replace(self.current, snapshot=previous)
        self.source_hash = resume.digest(previous["source_sha256"])
        self.patch = patch.object(resume, "BUSY_PREFLIGHT_PREDECESSOR_SETS", {self.source_hash})
        self.patch.start()
        self.addCleanup(self.patch.stop)
        original = CombinationStore.event
        failed = [False]
        def event(store, run_id, kind, payload):
            original(store, run_id, kind, payload)
            if kind == "condition_started" and payload["condition_id"] == "condition-000001" and not failed[0]:
                failed[0] = True
                raise RuntimeError("injected prior acquisition stop")
        with patch.object(CombinationStore, "event", event):
            first = self.execute(self.previous, self.dll)
        self.assertEqual(first["status"], "failed", first)
        review = resume.build_recovery(self.previous, self.fixture.database, "current-run",
                                       acknowledge_failure=True)
        self.assertTrue(review["review"]["ready"], review)
        self.fixture.devices()
        self.fixture.source.state.update({"SOUR:CURR:COMP": "20", "STAT:OPER:COND": "128",
                                          "STAT:MEAS:COND": "0"})
        busy_dll = FakeAttoDryDll()
        busy_dll.return_codes["connect"] = -1073807246
        busy = self.execute(self.previous, busy_dll, recovery=review)
        self.assertEqual(busy["error"], resume.BUSY_PREFLIGHT_ERROR)
        self.assertEqual(self.fixture.source.commands, [])
        self.assertNotIn("set_temperature", busy_dll.events)
        self.state = hardware_recovery_state(self.fixture.database, "current-run")
        self.parent = json.loads(self.state["executions"][-1]["plan_json"])
        self.execution_id = self.state["executions"][-1]["execution_id"]
        with closing(open_readonly(self.fixture.database)) as db:
            self.events = [dict(r) for r in db.execute(
                "SELECT * FROM combination_events WHERE json_extract(payload_json,'$.execution_id')=? ORDER BY event_id",
                (self.execution_id,))]
        self.assertEqual(len(self.events), 48)

    def execute(self, config, dll, recovery=None):
        return self.fixture.execute(config, authorize_cryostat=True, dll=dll, recovery=recovery,
                                    on_registered=lambda *_: {"fixture": "no hardware"})

    def proof(self, state=None, parent=None):
        with closing(open_readonly(self.fixture.database)) as db:
            return resume.busy_preflight_cleanup_resolution(
                state or self.state, parent or self.parent, db)

    def mutate_event(self, kind, mutate, *, index=0):
        candidates = [r for r in self.events if r["event_type"] == kind]
        row = candidates[index]
        payload = json.loads(row["payload_json"])
        mutate(payload)
        with CombinationStore(self.fixture.database) as store:
            store.connection.execute("UPDATE combination_events SET payload_json=? WHERE event_id=?",
                                     (json.dumps(payload), row["event_id"]))
            store.connection.commit()

    def rewritten_source_archive(self, *, state_changes, query_changes, config_changes=None):
        """Keep all redundant copies consistent to exercise raw decoding itself."""
        state, parent = deepcopy(self.state), deepcopy(self.parent)
        parent["hardware"]["excitation"].update(config_changes or {})
        cleanup = json.loads(state["run"]["cleanup_json"])
        source = cleanup["actions"][0]["result"]
        source["last_confirmed"].update(state_changes)
        state["run"]["cleanup_json"] = json.dumps(cleanup)
        state["executions"][-1]["cleanup_json"] = json.dumps(cleanup)
        state["executions"][-1]["plan_json"] = json.dumps(parent)
        with CombinationStore(self.fixture.database) as store:
            for row in self.events:
                payload = json.loads(row["payload_json"])
                kind = row["event_type"]
                if kind == "run_started":
                    payload["plan"] = parent
                elif kind == "excitation_query_return" and payload["command"] in query_changes:
                    payload["raw"] = query_changes[payload["command"]]
                elif kind == "excitation_raw_state":
                    payload = {k: v for k, v in source["last_confirmed"].items()
                               if k not in {"identity", "error_queue"}}
                    payload["execution_id"] = self.execution_id
                elif kind == "cleanup_result" and payload["module"] == "excitation":
                    payload["result"] = source
                elif kind == "run_finished":
                    payload["cleanup"] = cleanup
                store.connection.execute("UPDATE combination_events SET payload_json=? WHERE event_id=?",
                                         (json.dumps(payload), row["event_id"]))
            store.connection.commit()
        return state, parent

    def test_readonly_review_no_manual_claim_then_fresh_safe_resume(self):
        old_execution = deepcopy(self.state["executions"][-1])
        with closing(open_readonly(self.fixture.database)) as db:
            prefix = [tuple(r) for r in db.execute("SELECT * FROM combination_samples")]
        before = self.fixture.database.read_bytes()
        review = resume.build_recovery(self.current, self.fixture.database, "current-run",
                                       acknowledge_failure=True)
        self.assertTrue(review["review"]["ready"], review)
        self.assertFalse(review["review"]["manual_verification"])
        self.assertTrue(review["review"]["cleanup_review"]["manual_verification_required"])
        proof = review["review"]["deferred_cleanup_resolution"]
        self.assertEqual(proof["contract"], resume.BUSY_PREFLIGHT_CONTRACT)
        self.assertFalse(proof["original_cleanup_certified"])
        self.assertEqual(review["accepted_conditions"], 1)
        self.assertEqual(review["next_condition"]["condition_id"], "condition-000001")
        self.assertEqual(before, self.fixture.database.read_bytes())
        self.fixture.devices()
        fresh_dll = FakeAttoDryDll()
        fresh_dll.temperature_follows_setpoint = True
        result = self.execute(self.current, fresh_dll, recovery=review)
        self.assertEqual(result["status"], "completed", result)
        with closing(open_readonly(self.fixture.database)) as db:
            prior = dict(db.execute("SELECT * FROM combination_executions WHERE execution_id=?",
                                    (old_execution["execution_id"],)).fetchone())
            self.assertEqual(prior, old_execution)
            saved = [tuple(r) for r in db.execute("SELECT * FROM combination_samples WHERE condition_id='condition-000000'")]
            self.assertEqual(saved, prefix)
            self.assertEqual(validate_accepted_records(db, "current-run")["accepted_conditions"], 8)
        self.assertEqual(self.fixture.source.state["SOUR:CURR:COMP"], "20")

    def test_forged_ready_proof_does_not_pass_atomic_claim(self):
        review = resume.build_recovery(self.current, self.fixture.database, "current-run",
                                       acknowledge_failure=True)
        review["review"]["deferred_cleanup_resolution"]["evidence_sha256"] = "0" * 64
        self.fixture.devices()
        with self.assertRaisesRegex(ValueError, "Deferred cleanup"):
            self.execute(self.current, FakeAttoDryDll(), recovery=review)
        self.assertEqual(self.fixture.source.commands, [])

    def test_missing_extra_or_write_event_cannot_prove_no_writes(self):
        self.assertIsNotNone(self.proof())
        row = self.events[4]
        with CombinationStore(self.fixture.database) as store:
            store.connection.execute("DELETE FROM combination_events WHERE event_id=?", (row["event_id"],))
            store.connection.commit()
        self.assertIsNone(self.proof())
        with CombinationStore(self.fixture.database) as store:
            store.connection.execute("INSERT INTO combination_events VALUES(?,?,?,?,?)", tuple(row.values()))
            store.connection.execute("INSERT INTO combination_events(run_id,created_at_utc,event_type,payload_json) VALUES(?,?,?,?)",
                ("current-run", self.events[-1]["created_at_utc"], "excitation_command_attempt",
                 json.dumps({"execution_id": self.execution_id, "command": "OUTP:STAT ON"})))
            store.connection.commit()
        self.assertIsNone(self.proof())

    def test_source_raw_or_query_tampering_is_rejected(self):
        row = next(r for r in self.events if r["event_type"] == "excitation_raw_state")
        for key, value in (("output_enabled", True), ("dc_current_setpoint_a", 1e-9),
                           ("measurement_event", 8), ("compliance_voltage_v", 21.0)):
            self.mutate_event("excitation_raw_state", lambda p: p.update({key: value}))
            with self.subTest(key=key):
                self.assertIsNone(self.proof())
            with CombinationStore(self.fixture.database) as store:
                store.connection.execute("UPDATE combination_events SET payload_json=? WHERE event_id=?",
                                         (row["payload_json"], row["event_id"]))
                store.connection.commit()
        self.mutate_event("excitation_query_return", lambda p: p.update(raw="1"), index=1)
        self.assertIsNone(self.proof())

    def test_unknown_cleanup_secondary_errors_and_claimed_writes_reject(self):
        for mutate in (
            lambda c: c.update(communication_uncertain=True),
            lambda c: c["errors"].append("close: injected transport failure"),
            lambda c: c["actions"][0].update(startup_completed=True),
            lambda c: c["actions"][1]["result"].update(attempted=True),
            lambda c: c["actions"][2]["result"].update(reason="writes attempted"),
            lambda c: c["actions"][0]["audit_errors"].append("audit failed"),
        ):
            state = deepcopy(self.state)
            cleanup = json.loads(state["run"]["cleanup_json"])
            mutate(cleanup)
            state["run"]["cleanup_json"] = json.dumps(cleanup)
            state["executions"][-1]["cleanup_json"] = json.dumps(cleanup)
            with self.subTest(cleanup=cleanup):
                self.assertIsNone(self.proof(state))

    def test_unknown_shield_and_raw_derived_contradictions_are_rejected(self):
        for raw, derived, valid in (("GUARD", "guard", True), ("UNKNOWN", "guard", False),
                                    ("GUAR", "output_low", False), ("OLOW", "guard", False)):
            state, parent = self.rewritten_source_archive(
                state_changes={"inner_shield_raw": raw, "inner_shield": derived},
                query_changes={"OUTP:ISH?": raw}, config_changes={"inner_shield": derived})
            with self.subTest(raw=raw, derived=derived):
                self.assertEqual(self.proof(state, parent) is not None, valid)

    def test_raw_error_code_is_decoded_even_if_stored_derived_code_claims_zero(self):
        for raw, code in (('1,"Fault"', 0), ('-222,"Out of range"', 0), ("malformed", 0),
                          ('0,', 0), ('0,"No error"', False)):
            state, parent = self.rewritten_source_archive(
                state_changes={"error_queue": {"code": code, "raw": raw}},
                query_changes={"SYST:ERR?": raw})
            with self.subTest(raw=raw, code=code):
                self.assertIsNone(self.proof(state, parent))

    def test_unknown_raw_identity_cannot_be_replaced_by_a_model_substring(self):
        for raw in ("MODEL 6221", "OTHER,MODEL 6221,fixture,1", "KEITHLEY,MODEL 6221,,1"):
            state, parent = self.rewritten_source_archive(
                state_changes={"identity": raw}, query_changes={"*IDN?": raw})
            with self.subTest(raw=raw):
                self.assertIsNone(self.proof(state, parent))

    def test_wrong_error_source_scope_execution_or_attempt_mapping_rejects(self):
        for mutate in (
            lambda s: s["run"].update(error="AttoDryDllError: unknown error"),
            lambda s: s["executions"][-1].update(status="active"),
            lambda s: s["executions"][-1].update(kind="initial"),
            lambda s: s["executions"][-1].update(finished_at_utc=None),
            lambda s: s["attempt_executions"].append({"execution_id": self.execution_id}),
        ):
            state = deepcopy(self.state)
            mutate(state)
            with self.subTest(state=state["run"]["error"]):
                self.assertIsNone(self.proof(state))
        for mutate in (
            lambda p: p["source_sha256"].update(attodry_py="0" * 64),
            lambda p: p.update(excitation_source="sr830"),
            lambda p: p.update(axes=[{"module": "excitation"}, {"module": "lockin"}]),
        ):
            parent = deepcopy(self.parent)
            mutate(parent)
            self.assertIsNone(self.proof(parent=parent))

    def test_just_in_time_source_compliance_still_blocks_new_configuration(self):
        review = resume.build_recovery(self.current, self.fixture.database, "current-run",
                                       acknowledge_failure=True)
        self.fixture.devices()
        self.fixture.source.state["STAT:MEAS:COND"] = "8"
        dll = FakeAttoDryDll()
        result = self.execute(self.current, dll, recovery=review)
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(self.fixture.source.commands, [])
        self.assertNotIn("connect", dll.events)


if __name__ == "__main__":
    unittest.main()
