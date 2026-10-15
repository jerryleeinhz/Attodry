"""Consistent, bounded SQLite analysis snapshots with synthetic formal data."""

import hashlib
import json
import math
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from attodry_control import combination_analysis as analysis
from attodry_control.combination_store import CombinationStore, encode, open_readonly


class CombinationAnalysisSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "synthetic.sqlite"
        with CombinationStore(self.path):
            pass
        self.writer = sqlite3.connect(self.path)
        self.addCleanup(self.writer.close)
        self.writer.execute("PRAGMA journal_mode=WAL")
        self.writer.execute("PRAGMA wal_autocheckpoint=0")
        self.insert_run("A", "active", 10)
        self.insert_run("B", "completed", 20)
        self.writer.commit()

    def insert_run(self, run_id, status, value):
        plan = {"config_sha256": "a" * 64, "source_sha256": {"synthetic.py": "c" * 64}}
        cleanup = {"clean": status == "completed", "errors": []}
        self.writer.execute("INSERT INTO combination_runs VALUES(?,?,?,?,?,?,?)",
                            (run_id, 1, encode(plan), "2026-10-15T00:00:00Z",
                             status, encode(cleanup), None))
        context = {"sequence_index": 0, "repeat_index": 0,
                   "loop_order": ["temperature", "lockin"],
                   "axes": {"temperature": {"index": 0}},
                   "requested": {"temperature_k": 100., "lockin_excitation_v_rms": 0.1}}
        self.writer.execute("INSERT INTO combination_conditions VALUES(?,?,?,?)",
                            (run_id, "0", 0, encode(context)))
        self.writer.execute("INSERT INTO combination_attempts VALUES(?,?,?,?,?,?,?)",
                            (run_id, "0", 0, "accepted", None, "start", "finish"))
        self.writer.execute("INSERT INTO combination_samples VALUES(?,?,?,?,?)",
                            (run_id, "0", 0, 0, encode(self.sample(value))))
        self.writer.execute("INSERT INTO combination_executions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                            (run_id, "old-" + run_id, 0, "initial", status, encode(plan),
                             "{}", None, "2026-10-15T00:00:00Z", None, encode(cleanup), None))
        self.writer.execute("INSERT INTO combination_attempt_executions VALUES(?,?,?,?)",
                            (run_id, "0", 0, "old-" + run_id))

    @staticmethod
    def sample(value, *, clean=True):
        return {"clean": clean, "started_at_utc": "start", "finished_at_utc": "finish",
                "simulated": True, "actual": {"temperature_k": 100.01},
                "measurements": {"signal": value},
                "reads": [{"module": "lockin", "captured_at_utc": "formal",
                           "status": {"synthetic": True}}]}

    def test_final_default_unchanged_and_preview_does_not_promote_acceptance(self):
        self.assertEqual(analysis.load_combination_rows(self.path, run_id="A"), ())
        self.assertEqual(len(analysis.load_combination_rows(self.path, run_id="B")), 1)
        snapshot = analysis.load_combination_snapshot(self.path, run_ids=("A", "B"), preview=True)
        self.assertEqual(len(snapshot.rows), 2)
        active, completed = snapshot.rows
        self.assertEqual(active["run_id"], "A")
        self.assertTrue(active["preview"])
        self.assertFalse(active["accepted"])
        self.assertFalse(completed["preview"])
        self.assertTrue(completed["accepted"])
        self.assertEqual([run["final"] for run in snapshot.metadata["runs"]], [False, True])

    def test_preview_excludes_failed_interrupted_started_rejected_and_invalid(self):
        for run_id, status, attempt_status, clean in (
            ("failed", "failed", "accepted", True),
            ("interrupted", "interrupted", "accepted", True),
            ("started", "active", "started", True),
            ("rejected", "active", "rejected", True),
            ("invalid", "active", "accepted", False),
        ):
            self.insert_run(run_id, status, 30)
            self.writer.execute("UPDATE combination_attempts SET status=? WHERE run_id=?",
                                (attempt_status, run_id))
            self.writer.execute("UPDATE combination_samples SET payload_json=? WHERE run_id=?",
                                (encode(self.sample(30, clean=clean)), run_id))
        self.writer.commit()
        snapshot = analysis.load_combination_snapshot(self.path, preview=True)
        self.assertEqual({row["run_id"] for row in snapshot.rows}, {"A", "B"})
        audit = analysis.load_combination_snapshot(self.path, audit=True, preview=True)
        self.assertEqual(len(audit.rows), 7)
        self.assertEqual(sum(row["preview"] for row in audit.rows), 1)
        self.assertFalse(any(row["accepted"] for row in audit.rows if row["run_id"] != "B"))

    def test_selection_empty_unknown_and_unselected_bad_payload(self):
        self.writer.execute("UPDATE combination_samples SET payload_json='bad JSON' WHERE run_id='B'")
        self.writer.commit()
        snapshot = analysis.load_combination_snapshot(self.path, run_ids=("A", "A"), preview=True)
        self.assertEqual(snapshot.metadata["run_ids"], ["A"])
        self.assertEqual(len(snapshot.rows), 1)
        empty = analysis.load_combination_snapshot(self.path, run_ids=())
        self.assertEqual(empty.rows, ())
        self.assertEqual(empty.metadata["runs"], [])
        self.assertEqual(analysis.load_combination_rows(self.path, run_id="unknown"), ())
        with self.assertRaises(json.JSONDecodeError):
            analysis.load_combination_snapshot(self.path, audit=True)

    def test_metadata_hashes_bind_content_and_status_but_not_load_time_or_other_runs(self):
        first = analysis.load_combination_snapshot(self.path, run_ids=("A",), preview=True)
        self.writer.execute("UPDATE combination_runs SET error='unselected update' WHERE run_id='B'")
        self.writer.commit()
        second = analysis.load_combination_snapshot(self.path, run_ids=("A",), preview=True)
        self.assertEqual(first.metadata["snapshot_id"], second.metadata["snapshot_id"])
        self.assertEqual(first.metadata["rows_sha256"], second.metadata["rows_sha256"])
        self.assertNotEqual(first.metadata["loaded_at_utc"], second.metadata["loaded_at_utc"])
        digest = hashlib.sha256(json.dumps(first.rows, sort_keys=True, ensure_ascii=False,
                                          allow_nan=False, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(first.metadata["rows_sha256"], digest)
        self.writer.execute("UPDATE combination_samples SET payload_json=? WHERE run_id='A'",
                            (encode(self.sample(99)),))
        self.writer.commit()
        third = analysis.load_combination_snapshot(self.path, run_ids=("A",), preview=True)
        self.assertNotEqual(first.metadata["rows_sha256"], third.metadata["rows_sha256"])
        self.assertNotEqual(first.metadata["snapshot_id"], third.metadata["snapshot_id"])
        no_rows = analysis.load_combination_snapshot(self.path, run_ids=("A",))
        self.writer.execute("UPDATE combination_runs SET error='selected status update' WHERE run_id='A'")
        self.writer.commit()
        changed = analysis.load_combination_snapshot(self.path, run_ids=("A",))
        self.assertEqual(no_rows.rows, changed.rows)
        self.assertNotEqual(no_rows.metadata["snapshot_id"], changed.metadata["snapshot_id"])

    def test_nonfinite_audit_values_are_retained_and_digest_is_standard_json(self):
        sample = self.sample(float("nan"), clean=False)
        sample["actual"]["temperature_k"] = float("inf")
        self.writer.execute("UPDATE combination_samples SET payload_json=? WHERE run_id='A'",
                            (json.dumps(sample, allow_nan=True),))
        self.writer.execute("UPDATE combination_runs SET cleanup_json=? WHERE run_id='A'",
                            (json.dumps({"clean": False, "last": float("-inf")}),))
        self.writer.commit()
        first = analysis.load_combination_snapshot(self.path, run_ids=("A",), audit=True)
        self.assertTrue(math.isnan(first.rows[0]["measured.signal"]))
        self.assertTrue(math.isinf(first.rows[0]["actual.temperature_k"]))
        self.assertEqual(first.rows[0]["execution_cleanup"]["clean"], False)
        self.assertEqual(first.metadata["runs"][0]["cleanup"]["last"], {"__nonfinite_float__": "-inf"})
        json.dumps(first.metadata, allow_nan=False)
        second = analysis.load_combination_snapshot(self.path, run_ids=("A",), audit=True)
        self.assertEqual(first.metadata["rows_sha256"], second.metadata["rows_sha256"])
        self.assertEqual(first.metadata["snapshot_id"], second.metadata["snapshot_id"])
        self.assertEqual(analysis.load_combination_snapshot(self.path, run_ids=("A",), preview=True).rows, ())

    def test_concurrent_commit_does_not_mix_selected_runs_samples_mapping_or_configuration(self):
        original_open = open_readonly
        opened = []
        writer = self.writer

        class Reader:
            def __init__(self, path):
                self.connection = original_open(path)
                self.changed = self.closed = False
                self.statements = []
                opened.append(self)

            def execute(self, statement, parameters=()):
                self.statements.append(statement)
                cursor = self.connection.execute(statement, parameters)
                if "FROM combination_samples s" in statement and not self.changed:
                    self.changed = True
                    for run_id in ("A", "B"):
                        writer.execute("UPDATE combination_runs SET status='failed', error='later' WHERE run_id=?",
                                       (run_id,))
                        writer.execute("UPDATE combination_samples SET payload_json=? WHERE run_id=?",
                                       (encode(CombinationAnalysisSnapshotTests.sample(999)), run_id))
                        writer.execute("INSERT INTO combination_executions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                                       (run_id, "new-" + run_id, 1, "resume", "failed",
                                        encode({"config_sha256": "b" * 64}), "{}", None,
                                        "later", "later", "{}", "later"))
                        writer.execute("UPDATE combination_attempt_executions SET execution_id=? WHERE run_id=?",
                                       ("new-" + run_id, run_id))
                    writer.commit()
                return cursor

            def close(self):
                self.closed = True
                self.connection.close()

        with patch.object(analysis, "open_readonly", side_effect=Reader):
            snapshot = analysis.load_combination_snapshot(self.path, run_ids=("A", "B"), preview=True)
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0].closed)
        self.assertEqual(sum(statement == "BEGIN" for statement in opened[0].statements), 1)
        self.assertEqual([row["measured.signal"] for row in snapshot.rows], [10, 20])
        self.assertEqual({row["config_sha256"] for row in snapshot.rows}, {"a" * 64})
        self.assertEqual({row["execution_id"] for row in snapshot.rows}, {"old-A", "old-B"})
        self.assertEqual([run["status"] for run in snapshot.metadata["runs"]], ["active", "completed"])
        fresh = analysis.load_combination_snapshot(self.path, run_ids=("A", "B"), audit=True)
        self.assertEqual({row["measured.signal"] for row in fresh.rows}, {999})
        self.assertEqual({row["config_sha256"] for row in fresh.rows}, {"b" * 64})
        self.assertEqual({run["status"] for run in fresh.metadata["runs"]}, {"failed"})
        self.assertEqual(self.writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0], 0)

    def test_connection_is_query_only_and_closes_before_json_materialization_without_db_mutation(self):
        before = tuple(self.writer.iterdump())
        connections, state = [], {"closed": False}
        original_loads = json.loads

        class Reader:
            def __init__(self, path):
                self.connection = open_readonly(path)
                connections.append(self.connection)
                state["query_only"] = self.connection.execute("PRAGMA query_only").fetchone()[0]

            def execute(self, *args):
                return self.connection.execute(*args)

            def close(self):
                state["closed"] = True
                self.connection.close()

        def decode(*args, **kwargs):
            self.assertTrue(state["closed"])
            return original_loads(*args, **kwargs)

        with patch.object(analysis, "open_readonly", side_effect=Reader) as opened, patch.object(
            analysis.json, "loads", side_effect=decode
        ):
            snapshot = analysis.load_combination_snapshot(self.path, run_ids=("A", "B"), preview=True)
        opened.assert_called_once_with(self.path)
        self.assertEqual(state["query_only"], 1)
        self.assertEqual(len(snapshot.rows), 2)
        self.assertEqual(tuple(self.writer.iterdump()), before)
        with self.assertRaises(sqlite3.ProgrammingError):
            connections[0].execute("SELECT 1")
        self.assertEqual(self.writer.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0], 0)

    def test_missing_execution_mapping_and_partial_provenance_schema_fail_closed(self):
        self.writer.execute("DELETE FROM combination_attempt_executions WHERE run_id='A'")
        self.writer.commit()
        with self.assertRaisesRegex(ValueError, "archived execution"):
            analysis.load_combination_snapshot(self.path, run_ids=("A",), preview=True)
        self.writer.execute("DROP TABLE combination_attempt_executions")
        self.writer.commit()
        with self.assertRaisesRegex(ValueError, "Incomplete execution provenance"):
            analysis.load_combination_snapshot(self.path, preview=True)

    def test_legacy_schema_remains_readonly_and_unknown_schema_is_rejected(self):
        self.writer.executescript("DROP TABLE combination_attempt_executions; DROP TABLE combination_executions;")
        self.writer.commit()
        snapshot = analysis.load_combination_snapshot(self.path, run_ids=("A",), preview=True)
        self.assertEqual(snapshot.rows[0]["execution_kind"], "legacy_unsegmented")
        self.assertIsNone(snapshot.rows[0]["execution_id"])
        self.writer.execute("UPDATE combination_runs SET schema_version=999 WHERE run_id='A'")
        self.writer.commit()
        with self.assertRaisesRegex(ValueError, "Unsupported combination schema"):
            analysis.load_combination_snapshot(self.path, run_ids=("A",))


if __name__ == "__main__":
    unittest.main()
