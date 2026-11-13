"""Failed/interrupted accepted prefixes remain partial, audited observations."""
from contextlib import closing
import hashlib
import json
import sqlite3
import unittest
from unittest.mock import patch

from attodry_control.combination_analysis import load_combination_rows, load_combination_snapshot
from attodry_control.commissioning_6221_analysis import load_6221_sweep
from tests import test_commissioning_6221 as fixtures


class TerminalPrefixBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures.CurrentCommissioningTests.setUpClass.__func__(cls)

    @classmethod
    def tearDownClass(cls):
        fixtures.CurrentCommissioningTests.tearDownClass.__func__(cls)

    setUp = fixtures.CurrentCommissioningTests.setUp
    mutate_sample = fixtures.CurrentCommissioningTests.mutate_sample

    def reset_fixture(self):
        from attodry_control.combination_store import open_readonly
        with closing(sqlite3.connect(self.database)) as dest, closing(open_readonly(self.fixture.database)) as source:
            source.backup(dest)

    def terminal(self, status="failed"):
        self.cleanup = {"clean": False, "manual_verification_required": True,
                        "communication_uncertain": True,
                        "errors": ["original source cleanup could not be confirmed"]}
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE combination_runs SET status=?,cleanup_json=?,error=?",
                       (status, json.dumps(self.cleanup), "original terminal failure"))

    def native(self, **kwargs):
        return load_6221_sweep(self.database, "current-run", **kwargs)

    def wide(self, **kwargs):
        return load_combination_snapshot(self.database, run_ids=("current-run",), **kwargs)

    def test_shared_default_and_adapter_opt_out_keep_final_only_behavior(self):
        self.terminal()
        self.assertEqual(self.wide().rows, ())
        self.assertEqual(load_combination_rows(self.database, run_id="current-run"), ())
        self.assertEqual(self.native(terminal_prefix=False).groups, {})
        completed = self.wide(audit=True)
        self.assertTrue(all("terminal_prefix" not in row for row in completed.rows))
        self.assertNotIn("terminal_prefix", completed.metadata)

    def test_failed_and_interrupted_prefixes_preserve_original_failure_and_cleanup(self):
        for status in ("failed", "interrupted"):
            with self.subTest(status=status):
                self.terminal(status)
                before = hashlib.sha256(self.database.read_bytes()).hexdigest()
                snapshot = self.wide(terminal_prefix=True)
                self.assertEqual(len(snapshot.rows), 5)
                self.assertTrue(all(row["terminal_prefix"] is True and not row["accepted"]
                                    and not row["preview"] and row["attempt_status"] == "accepted"
                                    and row["run_status"] == status for row in snapshot.rows))
                run = snapshot.metadata["runs"][0]
                self.assertEqual(run["status"], status)
                self.assertEqual(run["error"], "original terminal failure")
                self.assertEqual(run["cleanup"], self.cleanup)
                self.assertFalse(run["final"])
                native = self.native()
                rows = tuple(row for group in native.groups.values() for row in group)
                self.assertEqual(len(rows), 10)
                self.assertTrue(all(row.record_status == "terminal_prefix" and row.terminal_prefix
                                    and row.run_status == status and row.attempt_status == "accepted"
                                    for row in rows))
                self.assertEqual(native.metadata["terminal_prefix_channel_rows"], 10)
                executions = native.metadata["execution_segments"]
                self.assertEqual({item["execution_status"] for item in executions}, {"failed", "completed"})
                original = next(item for item in executions if item["execution_status"] == "failed")
                self.assertIn("injected second condition failure", original["execution_error"])
                self.assertIsInstance(original["execution_cleanup"], dict)
                self.assertEqual(native.metadata["runs"][0]["cleanup"], self.cleanup)
                self.assertEqual(hashlib.sha256(self.database.read_bytes()).hexdigest(), before)

    def test_rejected_attempt_and_unclean_sample_are_never_prefixes(self):
        self.terminal()
        with closing(sqlite3.connect(self.database)) as db, db:
            condition, attempt = db.execute(
                "SELECT condition_id,attempt_index FROM combination_samples ORDER BY rowid LIMIT 1").fetchone()
            db.execute("UPDATE combination_attempts SET status='rejected' WHERE condition_id=? AND attempt_index=?",
                       (condition, attempt))
            key, raw = db.execute("SELECT rowid,payload_json FROM combination_samples WHERE condition_id != ? "
                                  "ORDER BY rowid LIMIT 1", (condition,)).fetchone()
            sample = json.loads(raw)
            sample["clean"] = False
            db.execute("UPDATE combination_samples SET payload_json=? WHERE rowid=?", (json.dumps(sample), key))
        selected = self.wide(terminal_prefix=True)
        self.assertEqual(len(selected.rows), 3)
        self.assertTrue(all(row["attempt_status"] == "accepted" and row["clean"] is True
                            for row in selected.rows))
        audited = self.wide(terminal_prefix=True, audit=True)
        rejected = [row for row in audited.rows if not row.get("terminal_prefix")]
        self.assertEqual(len(rejected), 2)
        self.assertTrue(all(not row["accepted"] for row in rejected))
        native = self.native(include_rejected=True, include_problem=True)
        rows = tuple(row for group in native.groups.values() for row in group)
        self.assertEqual(sum(row.record_status == "rejected" for row in rows), 4)
        self.assertFalse(any(row.terminal_prefix for row in rows if row.record_status == "rejected"))

    def test_channel_quality_still_excludes_invalid_formal_reading(self):
        self.terminal()
        def invalid(sample):
            pair = next(pair for reading in sample["reads"] if reading["module"] == "lockin"
                        for pair in reading["status"]["samples"] if pair["harmonic"] == 2)
            pair["valid_for_analysis_by_role"]["lockin_xy"] = False
        self.mutate_sample(invalid)
        native = self.native()
        self.assertEqual(native.metadata["selected_channel_rows"], 9)
        self.assertEqual(native.metadata["quality_exclusions"][0]["role"], "xy")
        audited = self.native(include_problem=True)
        rows = tuple(row for group in audited.groups.values() for row in group)
        self.assertEqual(len(rows), 10)
        self.assertEqual(sum(row.statuses != ("clean",) for row in rows), 1)

    def test_unknown_run_status_and_active_run_are_not_terminal_prefixes(self):
        for status in ("active", "unknown", "stopped", "completed"):
            with self.subTest(status=status):
                self.terminal(status)
                self.assertEqual(self.wide(terminal_prefix=True).rows, ())
                self.assertEqual(self.native().groups, {})
        self.terminal("active")
        snapshot = self.wide(terminal_prefix=True, preview=True)
        self.assertEqual(len(snapshot.rows), 5)
        self.assertTrue(all(row["preview"] is True and "terminal_prefix" not in row for row in snapshot.rows))
        self.assertTrue(all(row.record_status == "other" and not row.terminal_prefix
                            for group in self.native(preview=True).groups.values() for row in group))

    def test_prefix_source_proof_is_validated_before_and_after_even_in_audit(self):
        for stage in ("before", "after"):
            with self.subTest(stage=stage):
                # Each subtest starts with a fresh valid copy of the fixture.
                self.reset_fixture()
                self.terminal()
                def corrupt(sample):
                    source = next(reading["status"] for reading in sample["reads"]
                                  if reading["module"] == "excitation")
                    evidence = source if stage == "before" else source["after_sample"]
                    evidence["actual"]["excitation_current_rms_a"] *= 1.1
                self.mutate_sample(corrupt)
                for audit in (False, True):
                    with self.subTest(audit=audit), self.assertRaises(ValueError):
                        self.wide(terminal_prefix=True, audit=audit)
                # A raw audit without prefix eligibility remains available.
                self.assertTrue(self.wide(audit=True).rows)

    def test_prefix_requires_complete_formal_indices(self):
        self.terminal()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE combination_samples SET sample_index=2 WHERE rowid="
                       "(SELECT rowid FROM combination_samples LIMIT 1)")
        with self.assertRaisesRegex(ValueError, "incomplete sample indices"):
            self.native()

    def test_prefix_requires_raw_modules_acquisition_flag_and_matching_summary(self):
        def missing_source(sample):
            sample["reads"] = [reading for reading in sample["reads"] if reading["module"] != "excitation"]

        def unaccepted(sample):
            sample["acquisition_accepted"] = False

        def inconsistent_summary(sample):
            key = next(iter(sample["measurements"]))
            sample["measurements"][key] += 1

        def missing_pair(sample):
            detector = next(reading for reading in sample["reads"] if reading["module"] == "lockin")
            detector["status"]["samples"].pop()

        def unbracketed(sample):
            source = next(reading["status"] for reading in sample["reads"] if reading["module"] == "excitation")
            source["captured_at_utc"] = source["after_sample"]["captured_at_utc"]

        for change in (missing_source, unaccepted, inconsistent_summary, missing_pair, unbracketed):
            with self.subTest(case=change.__name__):
                self.reset_fixture()
                self.terminal()
                self.mutate_sample(change)
                for audit in (False, True):
                    with self.subTest(audit=audit), self.assertRaisesRegex(ValueError, "incomplete/invalid raw evidence"):
                        self.wide(terminal_prefix=True, audit=audit)
                self.assertTrue(self.wide(audit=True).rows)

    def test_full_evidence_validation_only_for_opted_in_terminal_accepted_runs(self):
        from attodry_control import combination_store
        validator = combination_store.validate_accepted_records
        with patch.object(combination_store, "validate_accepted_records", wraps=validator) as validate:
            self.wide(terminal_prefix=True)  # Completed archive keeps its existing behavior.
            self.terminal()
            self.wide()
            self.wide(audit=True)
            validate.assert_not_called()
            self.wide(terminal_prefix=True)
            validate.assert_called_once()
            with closing(sqlite3.connect(self.database)) as db, db:
                db.execute("UPDATE combination_attempts SET status='rejected'")
            self.wide(terminal_prefix=True)
            validate.assert_called_once()

    def test_legitimate_legacy_record_continue_is_preserved_but_not_fit_eligible(self):
        from tests import test_combination_resume_analysis as legacy
        from attodry_control.combination_store import CombinationStore
        fixture = legacy.ResumeAnalysisTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        with CombinationStore(fixture.database) as store:
            store.begin_run(legacy.RUN, fixture.old_plan, fixture.conditions)
            fixture.append(store, fixture.conditions[0])
            fixture.append(store, fixture.conditions[1], invalid=True)
            store.finish_run(legacy.RUN, "failed", fixture.failure_cleanup, legacy.FAULT)
        snapshot = load_combination_snapshot(fixture.database, terminal_prefix=True)
        self.assertEqual(len(snapshot.rows), 1)
        self.assertEqual(snapshot.metadata["terminal_prefix_validation"][legacy.RUN],
                         {"accepted_conditions": 2, "accepted_samples": 2})
        audit = load_combination_snapshot(fixture.database, terminal_prefix=True, audit=True)
        invalid = next(row for row in audit.rows if row["sequence_index"] == 1)
        self.assertEqual(invalid["attempt_status"], "accepted")
        self.assertFalse(invalid["clean"])
        self.assertNotIn("terminal_prefix", invalid)


if __name__ == "__main__":
    unittest.main()
