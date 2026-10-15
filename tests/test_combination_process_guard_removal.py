"""Fresh same-run recovery without OS process inspection; fake instruments only."""
from contextlib import ExitStack
from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import hashlib
import io
import json
from pathlib import Path
import pickle
import unittest
from unittest.mock import patch

from attodry_control.combination_hardware import load_hardware_combination
from attodry_control.combination_cli import run as cli
from attodry_control.combination_owner_resolution import (
    LiveProcessGuardRemovalResolution, PROCESS_GUARD_REMOVAL_ERROR,
    validate_process_guard_removal_parent,
)
from attodry_control.combination_resume import build_recovery
from attodry_control.combination_store import CombinationStore, encode, hardware_recovery_state, open_readonly
from tests import test_combination_owner_resolution as owner_fixtures
from tests.test_combination_resume_store import recorded_sample


CHANGED = {"combination_cli.py", "combination_hardware.py", "combination_resume.py",
           "combination_store.py", "combination_owner_resolution.py"}


class ProcessGuardRemovalTests(unittest.TestCase):
    def setUp(self):
        self.fixture = owner_fixtures.LiveOwnerResolutionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.database = self.fixture.database.with_name("process-guard.sqlite")
        self.run_id = "synthetic-process-guard-removal-342"
        actual = load_hardware_combination(self.fixture.config.path)
        self.new_plan = deepcopy(self.fixture.new_plan)
        self.new_plan["source_sha256"] = actual.snapshot["source_sha256"]
        self.old_sources = deepcopy(self.new_plan["source_sha256"])
        self.old_sources.update({name: hashlib.sha256(("old " + name).encode()).hexdigest() for name in CHANGED})
        self.old_plan = {**self.new_plan, "source_sha256": self.old_sources}
        self.config = replace(self.fixture.config, snapshot=self.new_plan)
        self.raw = json.loads((Path(__file__).parent / "guard_identity_fixtures" /
            "seq5-analysis-owner-terminal-events.json").read_text(encoding="utf-8"))
        self.cleanup = json.loads(next(e["payload_json"] for e in self.raw["events"]
            if e["event_type"] == "run_finished"))["cleanup"]
        with CombinationStore(self.database) as store:
            store.begin_hardware_execution(self.run_id, self.old_plan, self.config.plan.conditions())
            self.execution = store.current_execution_id
            for condition in self.config.plan.conditions()[:37]:
                attempt = store.begin_attempt(self.run_id, condition["condition_id"])
                store.sample(self.run_id, condition["condition_id"], attempt, 0, recorded_sample(condition))
                store.finish_attempt(self.run_id, condition["condition_id"], attempt, expected_samples=1)
            self.attempt = store.begin_attempt(self.run_id, "condition-000037")
            store.finish_attempt(self.run_id, "condition-000037", self.attempt,
                expected_samples=1, error=PROCESS_GUARD_REMOVAL_ERROR)
            store.finish_run(self.run_id, "failed", self.cleanup, PROCESS_GUARD_REMOVAL_ERROR)
            store.connection.execute("UPDATE combination_executions SET sequence_index=5 WHERE execution_id=?", (self.execution,))
            store.connection.execute("DELETE FROM combination_events WHERE run_id=?", (self.run_id,))
            for event in self.raw["events"]:
                payload = json.loads(event["payload_json"])
                payload["execution_id"] = self.execution
                store.connection.execute("INSERT INTO combination_events VALUES (?, ?, ?, ?, ?)",
                    (event["event_id"], self.run_id, event["created_at_utc"], event["event_type"], encode(payload)))
            store.connection.commit()
        self.state = hardware_recovery_state(self.database, self.run_id)

    def capability(self, **changes):
        bindings = dict(run_id=self.run_id, parent_state_sha256=self.state["state_sha256"],
            failed_execution_id=self.execution, failed_condition_id="condition-000037",
            failed_attempt_index=self.attempt, expected_primary_error=PROCESS_GUARD_REMOVAL_ERROR,
            old_source_sha256=self.old_sources, new_source_sha256=self.new_plan["source_sha256"],
            config_sha256=self.new_plan["config_sha256"], live_readback=self.fixture.current_proof)
        bindings.update(changes)
        return LiveProcessGuardRemovalResolution(self.config, **bindings)

    def review(self, cap):
        return build_recovery(self.config, self.database, self.run_id,
            acknowledge_failure=True, live_owner_resolution=cap)

    def claim(self, recovery):
        with CombinationStore(self.database) as store:
            return store.begin_hardware_execution(self.run_id, self.new_plan,
                self.config.plan.conditions(), recovery=recovery)

    def test_synthetic_terminal_shape_keeps_37_accepted_and_uncertain_history(self):
        before = deepcopy(self.state)
        with ExitStack() as stack:
            for name in ("process_inventory", "_idle_jupyter", "resource_mutexes"):
                stack.enter_context(patch("attodry_control.combination_ownership." + name,
                    side_effect=AssertionError("OS process checks must not run")))
            stack.enter_context(patch("attodry_control.combination_ownership.RecoveryOwner.check",
                side_effect=AssertionError("OS owner guard must not run")))
            recovery = self.review(self.capability())
            self.assertTrue(recovery["review"]["ready"])
            self.assertFalse(recovery["review"]["manual_verification"])
            self.assertTrue(recovery["review"]["cleanup_review"]["communication_uncertain"])
            self.assertEqual(recovery["accepted_conditions"], 37)
            self.assertEqual(recovery["next_condition"]["condition_id"], "condition-000037")
            self.assertEqual(len(self.claim(recovery)), 37)
        after = hardware_recovery_state(self.database, self.run_id)
        self.assertEqual(before["attempts"], after["attempts"])
        self.assertEqual(before["sample_manifest"], after["sample_manifest"])
        self.assertEqual(before["executions"][0], after["executions"][0])
        self.assertEqual(after["executions"][-1]["sequence_index"], 6)
        proof = json.loads(after["executions"][-1]["review_json"])["live_owner_resolution"]
        self.assertTrue(proof["process_guards_disabled"])
        self.assertTrue(proof["physical_checks_unchanged"])
        self.assertNotIn("current_owner", proof)
        self.assertIn("claim_readbacks", proof)

    def test_daily_run_and_resume_never_construct_process_owner_or_mutex(self):
        actual = load_hardware_combination(self.config.path)
        with CombinationStore(self.database) as store:
            store.begin_hardware_execution("daily-recovery", actual.snapshot, actual.plan.conditions())
            store.finish_run("daily-recovery", "failed", {"clean": False, "communication_uncertain": False,
                "actions": [{"module": module, "result": {"verified": True, "errors": []}}
                    for module in ("lockin", "temperature")]}, "prior failure")
        with ExitStack() as stack:
            for name in ("RecoveryOwner", "process_inventory", "_idle_jupyter", "resource_mutexes"):
                stack.enter_context(patch("attodry_control.combination_ownership." + name,
                    side_effect=AssertionError("Daily CLI must not inspect or lock OS processes")))
            backend = stack.enter_context(patch("attodry_control.combination_hardware.run_hardware_combination",
                return_value={"status": "completed"}))
            stack.enter_context(redirect_stdout(io.StringIO()))
            common = ["--config", str(self.config.path), "--database", str(self.database), "--json"]
            self.assertEqual(cli(["run", "--run-id", "new-daily", *common]), 0)
            self.assertEqual(cli(["resume", "--run-id", "daily-recovery", "--acknowledge-failure", *common]), 0)
            self.assertEqual(backend.call_count, 2)
            self.assertTrue(all("recovery_owner_guard" not in call.kwargs for call in backend.call_args_list))

    def test_file_flag_and_serialized_proof_cannot_replace_fresh_capability(self):
        for forged in ({"process_guards_disabled": True}, True, "proof.json"):
            with self.subTest(forged=forged), self.assertRaises(ValueError):
                self.review(forged)
        cap = self.capability()
        for operation in (lambda: pickle.dumps(cap), lambda: deepcopy(cap)):
            with self.assertRaises(TypeError):
                operation()
        recovery = self.review(cap)
        recovery.pop("_live_owner_resolution")
        with self.assertRaisesRegex(ValueError, "ephemeral capability"):
            self.claim(json.loads(encode(recovery)))

    def test_repeat_claim_consumed_capability_and_stale_frame_rejected(self):
        cap = self.capability()
        recovery = self.review(cap)
        self.claim(recovery)
        with self.assertRaises(ValueError):
            self.review(cap)
        with self.assertRaises(ValueError):
            self.claim(recovery)

    def test_expired_capability_rejected(self):
        cap = self.capability()
        cap._issued_at -= timedelta(seconds=601)
        with self.assertRaisesRegex(ValueError, "stale"):
            self.review(cap)

    def test_new_hard_field_temperature_error_and_baseline_faults_still_block(self):
        cases = [lambda p: p["cryostat"]["state"].update(sample_temperature_k=301),
                 lambda p: p["cryostat"]["state"]["field"].update(bx_t=3.1),
                 lambda p: p["cryostat"]["state"].update(error_code=1),
                 lambda p: p["cryostat"]["heater_power"].pop("sample_w"),
                 lambda p: p["lockin"]["lockin_xy"]["lia_status"].update(raw=1, input_or_reserve_overload=True)]
        for change in cases:
            def invalid():
                proof = self.fixture.current_proof()
                change(proof)
                return proof
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.capability(live_readback=invalid)

    def test_new_physical_fault_inside_atomic_claim_preserves_failed_parent(self):
        cap = self.capability()
        recovery = self.review(cap)
        before = hardware_recovery_state(self.database, self.run_id)
        def faulted():
            proof = self.fixture.current_proof()
            proof["cryostat"]["state"].update(error_code=9)
            return proof
        cap._readback = faulted
        with self.assertRaisesRegex(ValueError, "error/control"):
            self.claim(recovery)
        self.assertEqual(before, hardware_recovery_state(self.database, self.run_id))

    def test_source_config_parent_and_failed_attempt_bindings_are_enforced(self):
        for changes in ({"parent_state_sha256": "0" * 64}, {"failed_execution_id": "other"},
                        {"failed_attempt_index": 9}, {"config_sha256": "0" * 64},
                        {"new_source_sha256": {**self.new_plan["source_sha256"], "attodry.py": "0" * 64}}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.review(self.capability(**changes))

    def test_environment_after_read_other_error_cannot_hide_communication_fault(self):
        with CombinationStore(self.database) as store:
            row = store.connection.execute("SELECT payload_json FROM combination_events WHERE event_id=2002").fetchone()
            payload = json.loads(row[0])
            payload["error"] = "communication timeout"
            store.connection.execute("UPDATE combination_events SET payload_json=? WHERE event_id=2002", (encode(payload),))
            store.connection.commit()
        with self.assertRaisesRegex(ValueError, "secondary environment fault"):
            self.review(self.capability())

    def add_point_transition_error(self, error):
        payload = {"execution_id": self.execution, "condition_id": "condition-000037",
            "attempt_index": self.attempt, "harmonic_settings_transitions": [{"error": error}]}
        with CombinationStore(self.database) as store:
            store.connection.execute("INSERT INTO combination_events VALUES (?, ?, ?, ?, ?)",
                (2000, self.run_id, "2020-01-02T12:01:38+00:00", "lockin_point_samples", encode(payload)))
            store.connection.commit()
        return payload

    def test_bare_guard_error_in_synthetic_point_transition_shape_is_same_failure(self):
        raw = self.add_point_transition_error(PROCESS_GUARD_REMOVAL_ERROR.removeprefix("ValueError: "))
        recovery = self.review(self.capability())
        self.assertTrue(recovery["review"]["ready"])
        self.assertEqual(len(self.claim(recovery)), 37)
        with open_readonly(self.database) as connection:
            preserved = json.loads(connection.execute("SELECT payload_json FROM combination_events WHERE event_id=2000").fetchone()[0])
        self.assertEqual(preserved, raw)

    def test_nested_transition_communication_error_is_still_secondary_fault(self):
        self.add_point_transition_error("communication timeout")
        with self.assertRaisesRegex(ValueError, "secondary fault"):
            self.review(self.capability())

    def test_raw_attempted_cleanup_cannot_be_relabelled_as_guard_only(self):
        with CombinationStore(self.database) as store:
            bad = deepcopy(self.cleanup)
            bad["actions"][0]["result"]["attempted"] = True
            for table in ("combination_runs", "combination_executions"):
                store.connection.execute(f"UPDATE {table} SET cleanup_json=? WHERE run_id=?", (encode(bad), self.run_id))
            store.connection.commit()
        self.state = hardware_recovery_state(self.database, self.run_id)
        with self.assertRaisesRegex(ValueError, "attempted or secondary cleanup"):
            self.review(self.capability())


if __name__ == "__main__":
    unittest.main()
