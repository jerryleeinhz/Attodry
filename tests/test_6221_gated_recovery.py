"""File-only version migration and fake 6221 whole-condition recovery."""
from contextlib import closing, contextmanager
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from attodry_control import combination_resume as resume
from attodry_control.combination_store import (
    CombinationStore, open_readonly, validate_accepted_records,
)
from attodry_control.keithley6221_ac import Keithley6221Error
from tests import test_combination_6221 as fixtures


def source_maps():
    """Real destination byte hashes, with a distinct synthetic reviewed parent."""
    current = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
               for p in Path(resume.__file__).parent.glob("*.py")}
    previous = dict(current)
    for name in (*resume.GATED_6221_TARGET_SHA256, "combination_resume.py"):
        previous[name] = hashlib.sha256(("reviewed fixture: " + name).encode()).hexdigest()
    target = {name: frozenset({current[name]}) for name in resume.GATED_6221_TARGET_SHA256}
    return previous, current, target


@contextmanager
def reviewed_migration(previous, targets):
    with patch.object(resume, "GATED_6221_PREDECESSOR_SETS", {resume.digest(previous)}), \
            patch.object(resume, "GATED_6221_TARGET_SHA256", targets):
        yield


class GatedSourceMigrationTests(unittest.TestCase):
    def setUp(self):
        self.before, self.after, self.targets = source_maps()

    def test_exact_reviewed_parent_and_actual_destination_bytes(self):
        self.assertIsNone(resume.source_compatibility(self.before, self.after))
        with reviewed_migration(self.before, self.targets):
            self.assertEqual(resume.source_compatibility(self.before, self.after),
                             resume.GATED_6221_COMPATIBILITY)
            self.assertEqual(resume.source_compatibility(self.after, self.after), "unchanged")

    def test_unreviewed_source_files_and_destination_bytes_are_refused(self):
        with reviewed_migration(self.before, self.targets):
            for name in (*self.targets, "combination_resume.py", "attodry.py"):
                after = dict(self.after, **{name: "0" * 64})
                with self.subTest(name=name):
                    self.assertIsNone(resume.source_compatibility(self.before, after))
            self.assertIsNone(resume.source_compatibility(
                self.before, dict(self.after, **{"extra.py": "1" * 64})))
            missing = dict(self.after)
            missing.pop("combination_scan.py")
            self.assertIsNone(resume.source_compatibility(self.before, missing))
            unknown = dict(self.before, **{"combination_hardware.py": "2" * 64})
            self.assertIsNone(resume.source_compatibility(unknown, self.after))

    def test_unfilled_destination_review_fails_closed(self):
        with reviewed_migration(self.before, {name: frozenset() for name in self.targets}):
            self.assertIsNone(resume.source_compatibility(self.before, self.after))

    def snapshots(self):
        original = {"source_sha256": self.before, "excitation_source": "keithley6221",
                    "axes": [{"module": "excitation"}, {"module": "lockin"}],
                    "hardware": {"excitation": {"compliance_voltage_v": 20.0}}}
        current = deepcopy(original)
        current.update(source_sha256=self.after,
                       excitation_output_policy=resume.GATED_6221_OUTPUT_POLICY)
        return original, current

    def test_output_lifecycle_change_is_explicit_and_scoped(self):
        before, after = self.snapshots()
        with reviewed_migration(self.before, self.targets):
            self.assertEqual(resume.configuration_changes(before, after), [{
                "path": "excitation_output_policy", "before": None,
                "after": resume.GATED_6221_OUTPUT_POLICY, "category": "output_lifecycle"}])
            for mutate in (
                lambda a, b: a.update(excitation_output_policy=resume.GATED_6221_OUTPUT_POLICY),
                lambda a, b: b.pop("excitation_output_policy"),
                lambda a, b: b.update(excitation_output_policy="unchecked-output"),
                lambda a, b: a.update(excitation_source="sr830"),
                lambda a, b: b.update(excitation_source="sr830"),
                lambda a, b: b.update(axes=[{"module": "lockin"}]),
            ):
                old, new = deepcopy(before), deepcopy(after)
                mutate(old, new)
                with self.subTest(old=old, new=new), self.assertRaisesRegex(ValueError, "scope"):
                    resume.configuration_changes(old, new)

    def test_lifecycle_cannot_change_under_unchanged_source(self):
        before, after = self.snapshots()
        before["source_sha256"] = self.after
        with self.assertRaisesRegex(ValueError, "lifecycle"):
            resume.configuration_changes(before, after)
        before["excitation_output_policy"] = resume.GATED_6221_OUTPUT_POLICY
        self.assertEqual(resume.configuration_changes(before, after), [])
        after["excitation_output_policy"] = "unchecked-output"
        with self.assertRaisesRegex(ValueError, "lifecycle"):
            resume.configuration_changes(before, after)

    def test_migration_does_not_allow_protection_or_grid_change(self):
        before, after = self.snapshots()
        with reviewed_migration(self.before, self.targets):
            for mutate in (
                lambda s: s["hardware"]["excitation"].update(compliance_voltage_v=21.0),
                lambda s: s["axes"].reverse(),
                lambda s: s.update(samples_per_condition=2),
            ):
                changed = deepcopy(after)
                mutate(changed)
                with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, "new run"):
                    resume.configuration_changes(before, changed)


class GatedWholeConditionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.Combination6221Tests("test_failed_second_condition_resume_repeats_whole_condition")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.text = self.fixture.text.replace("compliance_voltage_v = 1.00",
                                                      "compliance_voltage_v = 20.0")
        self.fixture.path.write_text(self.fixture.text, encoding="utf-8")
        self.before, self.after, self.targets = source_maps()
        current = self.fixture.config()
        snapshot = deepcopy(current.snapshot)
        snapshot.update(source_sha256=self.after,
                        excitation_output_policy=resume.GATED_6221_OUTPUT_POLICY)
        self.current = replace(current, snapshot=snapshot)
        previous = deepcopy(snapshot)
        previous["source_sha256"] = self.before
        previous.pop("excitation_output_policy")
        self.previous = replace(current, snapshot=previous)

    def failed_parent(self):
        original = CombinationStore.event
        failed = [False]
        def event(store, run_id, kind, payload):
            original(store, run_id, kind, payload)
            if kind == "condition_started" and payload["condition_id"] == "condition-000001" and not failed[0]:
                failed[0] = True
                raise Keithley6221Error("6221 compliance, unsafe or unknown condition")
        with patch.object(CombinationStore, "event", event):
            result = self.fixture.execute(self.previous)
        self.assertEqual(result["status"], "failed", result)
        self.assertFalse(result["cleanup"]["clean"])
        self.assertTrue(result["cleanup"]["manual_verification_required"])
        with closing(open_readonly(self.fixture.database)) as db:
            old_execution = dict(db.execute("SELECT * FROM combination_executions").fetchone())
            prefix = [tuple(r) for r in db.execute("SELECT * FROM combination_samples")]
        return old_execution, prefix

    def test_review_and_atomic_resume_preserve_failed_prefix_without_manual_claim(self):
        old_execution, prefix = self.failed_parent()
        database_before = self.fixture.database.read_bytes()
        with reviewed_migration(self.before, self.targets):
            review = resume.build_recovery(self.current, self.fixture.database, "current-run",
                                           acknowledge_failure=True)
            self.assertTrue(review["review"]["ready"], review)
            self.assertFalse(review["review"]["manual_verification"])
            self.assertEqual(review["accepted_conditions"], 1)
            self.assertEqual(review["next_condition"]["condition_id"], "condition-000001")
            self.assertEqual(review["review"]["configuration_changes"][0]["category"], "output_lifecycle")
            self.assertEqual(self.fixture.database.read_bytes(), database_before)
            self.fixture.devices()
            result = self.fixture.execute(self.current, recovery=review)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(self.fixture.source.state["SOUR:CURR:COMP"], "20")
        with closing(open_readonly(self.fixture.database)) as db:
            self.assertEqual(dict(db.execute("SELECT * FROM combination_executions ORDER BY sequence_index").fetchone()), old_execution)
            current_prefix = [tuple(r) for r in db.execute("SELECT * FROM combination_samples WHERE condition_id='condition-000000'")]
            self.assertEqual(current_prefix, prefix)
            self.assertEqual(validate_accepted_records(db, "current-run")["accepted_conditions"], 4)
            attempts = [tuple(r) for r in db.execute("SELECT condition_id,attempt_index,status FROM combination_attempts ORDER BY condition_id,attempt_index")]
        self.assertIn(("condition-000001", 0, "rejected"), attempts)
        self.assertIn(("condition-000001", 1, "accepted"), attempts)
        self.assertEqual(sum(row[0] == "condition-000000" for row in attempts), 1)

    def test_atomic_claim_rechecks_destination_instead_of_trusting_ready_review(self):
        self.failed_parent()
        with reviewed_migration(self.before, self.targets):
            review = resume.build_recovery(self.current, self.fixture.database, "current-run",
                                           acknowledge_failure=True)
            forged = deepcopy(self.current.snapshot)
            forged["source_sha256"]["keithley6221_ac.py"] = "0" * 64
            self.fixture.devices()
            with self.assertRaisesRegex(ValueError, "lifecycle|compatibility"):
                self.fixture.execute(replace(self.current, snapshot=forged), recovery=review)
            self.assertEqual(self.fixture.source.commands, [])

    def test_atomic_claim_rechecks_lifecycle_policy(self):
        self.failed_parent()
        with reviewed_migration(self.before, self.targets):
            review = resume.build_recovery(self.current, self.fixture.database, "current-run",
                                           acknowledge_failure=True)
            forged = deepcopy(self.current.snapshot)
            forged["excitation_output_policy"] = "unchecked-output"
            self.fixture.devices()
            with self.assertRaisesRegex(ValueError, "scope"):
                self.fixture.execute(replace(self.current, snapshot=forged), recovery=review)
            self.assertEqual(self.fixture.source.commands, [])

    def test_verified_parent_cleanup_does_not_allow_current_source_compliance(self):
        self.failed_parent()
        with reviewed_migration(self.before, self.targets):
            review = resume.build_recovery(self.current, self.fixture.database, "current-run",
                                           acknowledge_failure=True)
            self.fixture.devices()
            self.fixture.source.state["STAT:MEAS:COND"] = "8"
            result = self.fixture.execute(self.current, recovery=review)
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(self.fixture.source.commands, [])
        self.assertIn("compliance", result["error"])

    def test_unknown_parent_cleanup_remains_blocked_without_manual_claim(self):
        self.failed_parent()
        with CombinationStore(self.fixture.database) as store:
            cleanup = json.loads(store.connection.execute(
                "SELECT cleanup_json FROM combination_runs WHERE run_id='current-run'").fetchone()[0])
            cleanup["communication_uncertain"] = True
            store.connection.execute("UPDATE combination_runs SET cleanup_json=? WHERE run_id='current-run'",
                                     (json.dumps(cleanup),))
            store.connection.commit()
        with reviewed_migration(self.before, self.targets):
            review = resume.build_recovery(self.current, self.fixture.database, "current-run",
                                           acknowledge_failure=True)
        self.assertFalse(review["review"]["ready"])
        self.assertFalse(review["review"]["manual_verification"])
        self.assertTrue(review["review"]["cleanup_review"]["manual_verification_required"])
        self.assertTrue(any("confirm-manual" in item for item in review["review"]["blockers"]))


if __name__ == "__main__":
    unittest.main()
