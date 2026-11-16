import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from attodry_control.temperature_qualification_analysis import (
    load_qualification_waits, main, qualification_wait_summary,
)


def policy(**criteria):
    return {"acceptance_mode": "target", "criteria": {
        "dwell_s": 2.0, "minimum_samples": 3, "stable_range": 0.1,
        "tolerance": 0.5, **criteria}}


class QualificationWaitTests(unittest.TestCase):
    def test_outlier_predecessor_cannot_lend_coverage(self):
        report = qualification_wait_summary({"target_k": 10., "samples": [
            [0., 9.], [1., 10.], [2., 10.], [3., 10.], [4., 10.]]}, policy())
        self.assertEqual(report["first_in_target_band_s"], 1.)
        self.assertEqual(report["first_joint_dwell_s"], 4.)
        self.assertTrue(report["replay_matches_recorded_completion"])
        self.assertFalse(report["qualification_certified"])

    def test_target_and_range_are_separate_overlapping_observations(self):
        report = qualification_wait_summary({"target_k": 10., "samples": [
            [0., 9.8], [1., 10.2], [2., 9.8], [3., 10.], [4., 10.], [5., 10.], [6., 10.]]}, policy())
        self.assertEqual(report["first_target_only_dwell_s"], 2.)
        self.assertEqual(report["first_range_only_dwell_s"], 6.)
        self.assertEqual(report["first_joint_dwell_s"], 6.)

    def test_jitter_and_readback_mode_and_unmet_window(self):
        payload = {"target_k": 10., "samples": [[0., 8.], [0.7, 8.], [1.4, 8.], [2.1, 8.]]}
        target = qualification_wait_summary(payload, policy())
        self.assertIsNone(target["first_in_target_band_s"])
        self.assertIsNone(target["first_joint_dwell_s"])
        readback = qualification_wait_summary(payload, {**policy(), "acceptance_mode": "stable-readback"})
        self.assertEqual(readback["first_joint_dwell_s"], 2.1)
        self.assertIsNone(readback["first_target_only_dwell_s"])

    def test_invalid_evidence_rejects(self):
        for samples in ([], [[1, 10], [0, 10]], [[True, 10]], [[0, float('nan')]], [[-1, 10]]):
            with self.subTest(samples=samples), self.assertRaises(ValueError):
                qualification_wait_summary({"target_k": 10, "samples": samples}, policy())

    def test_loader_uses_each_execution_policy_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / 'scan.sqlite'
            with closing(sqlite3.connect(db)) as connection:
                connection.executescript('CREATE TABLE combination_runs(run_id,status); '
                    'CREATE TABLE combination_executions(run_id,execution_id,plan_json); '
                    'CREATE TABLE combination_events(run_id,event_id,event_type,payload_json);')
                connection.execute('INSERT INTO combination_runs VALUES (?,?)', ('run', 'completed'))
                for i, span in enumerate((0.1, 1.0)):
                    execution = f'execution-{i}'
                    plan = {"hardware": {"temperature": {"temperature_stability": policy(stable_range=span)}}}
                    connection.execute('INSERT INTO combination_executions VALUES (?,?,?)', ('run', execution, json.dumps(plan)))
                    payload = {"execution_id": execution, "condition_id": "same", "attempt_index": i,
                               "target_k": 10, "samples": [[0, 9.8], [1, 10.2], [2, 9.8]]}
                    connection.execute('INSERT INTO combination_events VALUES (?,?,?,?)',
                        ('run', i, 'temperature_qualification_completed', json.dumps(payload)))
                connection.commit()
            before = db.read_bytes()
            report = load_qualification_waits(db, 'run')
            self.assertIsNone(report['qualifications'][0]['first_joint_dwell_s'])
            self.assertEqual(report['qualifications'][1]['first_joint_dwell_s'], 2)
            self.assertEqual([r['attempt_index'] for r in report['qualifications']], [0, 1])
            output = Path(folder) / 'report.json'
            self.assertEqual(main(['--database', str(db), '--run-id', 'run', '--output', str(output)]), 0)
            with self.assertRaises(FileExistsError):
                main(['--database', str(db), '--run-id', 'run', '--output', str(output)])
            with self.assertRaises(ValueError):
                load_qualification_waits(db, 'missing')
            self.assertEqual(db.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
