"""Snapshot optimizations retain archived values, digests and row ownership."""
from collections import UserDict
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import tempfile
from types import MappingProxyType
import unittest
from unittest.mock import patch

from attodry_control import combination_analysis as analysis
from attodry_control.combination_store import CombinationStore, encode


class SnapshotPerformanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "snapshot.sqlite"
        with CombinationStore(self.path):
            pass
        self.plan = {"config_sha256": "original-config", "source_sha256": {"source": "original"}}
        self.execution_cleanup = {"clean": True, "audit": {"unique_execution_cleanup": [1, 2, 3]}}
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO combination_runs VALUES(?,?,?,?,?,?,?)",
                       ("run", 1, encode(self.plan), "created", "completed", encode({"clean": True}), None))
            for index in range(3):
                condition = str(index)
                context = {"sequence_index": index, "repeat_index": 0, "loop_order": ["magnetic"],
                           "axes": {"magnetic": {"index": index}}, "requested": {"field_x_t": index}}
                sample = {"clean": True, "simulated": True, "started_at_utc": "start", "finished_at_utc": "end",
                          "actual": {"field_x_t": index}, "measurements": {"signal": index + 0.5},
                          "reads": [{"module": "lockin", "captured_at_utc": "sample", "status": {"raw": [0, 1]}}]}
                db.execute("INSERT INTO combination_conditions VALUES(?,?,?,?)", ("run", condition, index, encode(context)))
                db.execute("INSERT INTO combination_attempts VALUES(?,?,?,?,?,?,?)",
                           ("run", condition, 0, "accepted", None, "start", "end"))
                db.execute("INSERT INTO combination_samples VALUES(?,?,?,?,?)", ("run", condition, 0, 0, encode(sample)))

    def add_execution(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO combination_executions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       ("run", "execution", 0, "initial", "completed", encode(self.plan), "{}", None,
                        "created", "end", encode(self.execution_cleanup), None))
            for index in range(3):
                db.execute("INSERT INTO combination_attempt_executions VALUES(?,?,?,?)", ("run", str(index), 0, "execution"))

    def expected_rows(self, *, execution=False, status="completed", error=None, preview=False, terminal=False):
        plan_sha = hashlib.sha256(json.dumps(self.plan, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()
        rows = []
        for index in range(3):
            row = {"schema_version": 1, "source_path": str(self.path.resolve()), "run_id": "run", "condition_id": str(index),
                   "attempt_index": 0, "sample_index": 0, "accepted": status == "completed", "run_status": status,
                   "run_error": error, "preview": preview, "attempt_status": "accepted", "clean": True,
                   "sequence_index": index, "repeat_index": 0, "loop_order": ["magnetic"], "started_at_utc": "start",
                   "finished_at_utc": "end", "simulated": True, "execution_id": "execution" if execution else None,
                   "execution_index": 0, "execution_kind": "initial" if execution else "legacy_unsegmented",
                   "execution_status": "completed" if execution else status, "execution_error": None if execution else error,
                   "execution_cleanup": self.execution_cleanup if execution else {"clean": True},
                   "config_sha256": "original-config", "plan_sha256": plan_sha, "configuration_revision": "original-config",
                   "source_sha256": {"source": "original"}, "excitation_source": None, "measurement_contract": None,
                   "axes.magnetic.index": index, "requested.field_x_t": index, "actual.field_x_t": index,
                   "measured.signal": index + 0.5, "timestamps.lockin": "sample", "status.lockin": {"raw": [0, 1]}}
            if terminal:
                row["terminal_prefix"] = True
            rows.append(row)
        return tuple(rows)

    def assert_snapshot_rows(self, snapshot, expected):
        self.assertEqual(snapshot.rows, expected)
        # Independent canonical JSON of fully enumerated expected rows retains
        # the existing digest protocol; no updated helper constructs the oracle.
        expected_digest = hashlib.sha256(json.dumps(expected, sort_keys=True, ensure_ascii=False,
                                                   allow_nan=False, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(snapshot.metadata["rows_sha256"], expected_digest)

    def test_legacy_and_execution_rows_and_digest_are_unchanged(self):
        self.assert_snapshot_rows(analysis.load_combination_snapshot(self.path), self.expected_rows())
        self.add_execution()
        self.assert_snapshot_rows(analysis.load_combination_snapshot(self.path), self.expected_rows(execution=True))

    def test_execution_cleanup_is_parsed_once_but_rows_keep_independent_mutability(self):
        self.add_execution()
        cleanup_json = encode(self.execution_cleanup)
        with patch.object(analysis.json, "loads", wraps=json.loads) as parser:
            snapshot = analysis.load_combination_snapshot(self.path)
        self.assertEqual(sum(call.args[0] == cleanup_json for call in parser.call_args_list), 1)
        snapshot.rows[0]["execution_cleanup"]["audit"]["unique_execution_cleanup"].append(99)
        self.assertEqual(snapshot.rows[1]["execution_cleanup"], self.execution_cleanup)
        self.assertEqual(analysis.load_combination_snapshot(self.path).rows[0]["execution_cleanup"], self.execution_cleanup)

    def test_selection_preview_and_audit_keep_original_eligibility(self):
        self.assertEqual(analysis.load_combination_snapshot(self.path, run_ids=[]).rows, ())
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE combination_runs SET status='active',error='original'")
        self.assertEqual(analysis.load_combination_snapshot(self.path).rows, ())
        self.assert_snapshot_rows(analysis.load_combination_snapshot(self.path, preview=True),
                                  self.expected_rows(status="active", error="original", preview=True))
        self.assert_snapshot_rows(analysis.load_combination_snapshot(self.path, audit=True),
                                  self.expected_rows(status="active", error="original"))

    def test_terminal_prefix_still_requires_accepted_record_validation(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE combination_runs SET status='interrupted',error='original'")
        with patch("attodry_control.combination_store.validate_accepted_records", return_value={"verified": True}) as validator:
            snapshot = analysis.load_combination_snapshot(self.path, terminal_prefix=True)
        validator.assert_called_once()
        self.assert_snapshot_rows(snapshot, self.expected_rows(status="interrupted", error="original", terminal=True))
        self.assertEqual(snapshot.metadata["terminal_prefix_validation"], {"run": {"verified": True}})
        with patch("attodry_control.combination_store.validate_accepted_records", side_effect=ValueError("invalid raw evidence")):
            with self.assertRaisesRegex(ValueError, "invalid raw evidence"):
                analysis.load_combination_snapshot(self.path, terminal_prefix=True)

    def test_digest_preserves_custom_mappings_containers_nonfinite_and_unicode(self):
        value = UserDict({"原始": [float("nan"), float("inf"), -float("inf"), -0.0, 1.25, True, None],
                          "nested": (MappingProxyType({"n": 12}),)})
        expected = {"原始": [{"__nonfinite_float__": "nan"}, {"__nonfinite_float__": "inf"},
                            {"__nonfinite_float__": "-inf"}, -0.0, 1.25, True, None], "nested": [{"n": 12}]}
        self.assertEqual(analysis._snapshot_json_value(value), expected)
        digest = hashlib.sha256(json.dumps(expected, sort_keys=True, ensure_ascii=False,
                                           allow_nan=False, separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(analysis._snapshot_digest(value), digest)
        self.assertTrue(math.isnan(value["原始"][0]))


if __name__ == "__main__":
    unittest.main()
