from contextlib import redirect_stdout
import ctypes
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from attodry_control.field_audit import JsonlEventWriter, read_jsonl_events
from attodry_control.magnetic_field_monitor import read_progress_snapshot, run


class MagneticFieldMonitorTests(unittest.TestCase):
    def test_writer_adds_canonical_metadata_and_fsyncs_each_append(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "field_progress.jsonl"
            with patch("attodry_control.field_audit.os.fsync") as fsync:
                with JsonlEventWriter(path, create=True) as writer:
                    writer.append({"event": "run_started", "note": "offline"})
                    writer.append(
                        {
                            "event": "point_completed",
                            "point_index": 0,
                            "requested_field": {"bx_t": 0.1, "bz_t": 0.2},
                        }
                    )

            events, trailing_line_incomplete = read_jsonl_events(path)

            self.assertFalse(trailing_line_incomplete)
            self.assertEqual([event["event_index"] for event in events], [0, 1])
            self.assertEqual([event["schema_version"] for event in events], [1, 1])
            self.assertTrue(events[0]["run_id"])
            self.assertEqual(events[0]["run_id"], events[1]["run_id"])
            self.assertIsInstance(events[0]["captured_unix_s"], (int, float))
            self.assertEqual(events[0]["note"], "offline")
            self.assertTrue(path.read_bytes().endswith(b"\n"))
            self.assertGreaterEqual(fsync.call_count, 2)

    def test_reader_preserves_order_and_duplicate_requested_points(self) -> None:
        points = (
            {"bx_t": 0.25, "bz_t": -0.5},
            {"bx_t": 0.25, "bz_t": -0.5},
            {"bx_t": 0.0, "bz_t": 0.75},
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ordered_points.jsonl"
            with JsonlEventWriter(path, create=True) as writer:
                writer.append(
                    {
                        "event": "run_started",
                        "command": "single-target",
                        "normal_end_field_policy": "hold",
                        "ordered_points": [{"bx_t": 0.2, "bz_t": 0.0}],
                    }
                )
                for point_index, requested_field in enumerate(points):
                    writer.append(
                        {
                            "event": "point_completed",
                            "point_index": point_index,
                            "requested_field": requested_field,
                        }
                    )

            events, trailing_line_incomplete = read_jsonl_events(path)

        completed = [
            event for event in events if event["event"] == "point_completed"
        ]
        self.assertFalse(trailing_line_incomplete)
        self.assertEqual([event["point_index"] for event in completed], [0, 1, 2])
        self.assertEqual(
            [event["requested_field"] for event in completed], list(points)
        )

    def test_reader_rejects_a_malformed_complete_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "malformed.jsonl"
            path.write_text(
                json.dumps({"event": "run_started"}) + "\n{not-json}\n",
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                read_jsonl_events(path)

    def test_reader_and_snapshot_tolerate_one_incomplete_trailing_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "truncated.jsonl"
            with JsonlEventWriter(path, create=True) as writer:
                writer.append(
                    {
                        "event": "run_started",
                        "ordered_points": [{"bx_t": 0.2, "bz_t": 0.0}],
                    }
                )
            with path.open("ab") as stream:
                stream.write(b'{"event":"field_sample"')

            events, trailing_line_incomplete = read_jsonl_events(path)
            snapshot = read_progress_snapshot(path)

        self.assertEqual(len(events), 1)
        self.assertTrue(trailing_line_incomplete)
        self.assertEqual(snapshot["event_count"], 1)
        self.assertTrue(snapshot["trailing_line_incomplete"])
        self.assertEqual(snapshot["outcome"], "incomplete")
        self.assertFalse(snapshot["audit_complete"])
        self.assertTrue(snapshot["manual_verification_required"])

    def test_valid_json_without_final_newline_is_not_a_durable_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unterminated.jsonl"
            path.write_text(
                json.dumps({"event": "run_finished", "outcome": "completed"}),
                encoding="utf-8",
            )

            events, trailing_line_incomplete = read_jsonl_events(path)
            snapshot = read_progress_snapshot(path)

        self.assertEqual(events, [])
        self.assertTrue(trailing_line_incomplete)
        self.assertEqual(snapshot["outcome"], "incomplete")
        self.assertFalse(snapshot["audit_complete"])
        self.assertTrue(snapshot["manual_verification_required"])

    def test_writer_latches_after_uncertain_fsync_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "failed.jsonl"
            writer = JsonlEventWriter(path, create=True)
            with patch(
                "attodry_control.field_audit.os.fsync",
                side_effect=OSError("injected fsync failure"),
            ):
                with self.assertRaisesRegex(OSError, "injected fsync failure"):
                    writer.append({"event": "run_started"})
            bytes_after_failure = path.read_bytes()

            with self.assertRaisesRegex(OSError, "previous JSONL audit write"):
                writer.append({"event": "point_completed", "point_index": 0})

            self.assertEqual(path.read_bytes(), bytes_after_failure)

    def test_snapshot_reports_progress_and_terminal_safety_outcome(self) -> None:
        last_confirmed_state = {
            "field": {"bx_t": 0.05, "bz_t": 0.0},
            "field_setpoint": {"bx_t": 0.0, "bz_t": 0.0},
            "field_control_enabled": True,
            "error_code": 0,
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "interrupted.jsonl"
            with JsonlEventWriter(path, create=True) as writer:
                writer.append(
                    {
                        "event": "run_started",
                        "command": "single-target",
                        "normal_end_field_policy": "hold",
                        "ordered_points": [{"bx_t": 0.2, "bz_t": 0.0}],
                    }
                )
                writer.append(
                    {
                        "event": "point_started",
                        "point_index": 0,
                        "requested_field": {"bx_t": 0.2, "bz_t": 0.0},
                    }
                )
                writer.append(
                    {
                        "event": "point_completed",
                        "point_index": 0,
                        "requested_field": {"bx_t": 0.2, "bz_t": 0.0},
                    }
                )
                writer.append(
                    {
                        "event": "run_finished",
                        "outcome": "interrupted",
                        "completed": False,
                        "zero_verified": False,
                        "manual_verification_required": True,
                        "disconnected": False,
                        "audit_complete": True,
                        "completed_points": 1,
                        "requested_points": 1,
                        "last_confirmed_state": last_confirmed_state,
                    }
                )

            snapshot = read_progress_snapshot(path)

        self.assertEqual(snapshot["event_count"], 4)
        self.assertEqual(snapshot["completed_points"], 1)
        self.assertEqual(snapshot["current_point_index"], 0)
        self.assertEqual(snapshot["outcome"], "interrupted")
        self.assertFalse(snapshot["zero_verified"])
        self.assertFalse(snapshot["disconnected"])
        self.assertTrue(snapshot["manual_verification_required"])
        self.assertEqual(snapshot["last_confirmed_state"], last_confirmed_state)

    def test_cli_prints_json_without_loading_a_hardware_dll(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "completed.jsonl"
            with JsonlEventWriter(path, create=True) as writer:
                writer.append(
                    {
                        "event": "run_started",
                        "command": "single-target",
                        "normal_end_field_policy": "hold",
                        "ordered_points": [{"bx_t": 0.0, "bz_t": 0.0}],
                    }
                )
                writer.append(
                    {
                        "event": "point_completed",
                        "point_index": 0,
                        "requested_field": {"bx_t": 0.0, "bz_t": 0.0},
                    }
                )
                writer.append(
                    {
                        "event": "run_finished",
                        "outcome": "completed",
                        "completed": True,
                        "zero_verified": True,
                        "manual_verification_required": False,
                        "disconnected": True,
                        "audit_complete": True,
                        "completed_points": 1,
                        "requested_points": 1,
                        "last_confirmed_state": {
                            "field": {"bx_t": 0.0, "bz_t": 0.0}
                        },
                    }
                )

            output = io.StringIO()
            with (
                patch.object(
                    ctypes,
                    "CDLL",
                    side_effect=AssertionError("monitor attempted DLL access"),
                ) as cdll,
                redirect_stdout(output),
            ):
                exit_code = run(["--progress", str(path)])

        self.assertEqual(exit_code, 0)
        cdll.assert_not_called()
        snapshot = json.loads(output.getvalue())
        self.assertEqual(snapshot["outcome"], "completed")
        self.assertEqual(snapshot["completed_points"], 1)
        self.assertTrue(snapshot["zero_verified"])
        self.assertTrue(snapshot["disconnected"])
        self.assertFalse(snapshot["manual_verification_required"])
        self.assertTrue(snapshot["audit_complete"])

    def test_monitor_import_does_not_import_hardware_driver_module(self) -> None:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; "
                    "import attodry_control.magnetic_field_monitor; "
                    "assert 'attodry_control.attodry' not in sys.modules"
                ),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_monitor_fails_closed_on_noncanonical_terminal_stream(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "corrupt.jsonl"
            with JsonlEventWriter(path, create=True) as writer:
                writer.append(
                    {
                        "event": "run_started",
                        "command": "single-target",
                        "normal_end_field_policy": "hold",
                        "ordered_points": [{"bx_t": 0.0, "bz_t": 0.0}],
                    }
                )
                writer.append(
                    {
                        "event": "run_finished",
                        "outcome": "completed",
                        "completed": True,
                        "completed_points": 0,
                        "requested_points": 1,
                        "zero_verified": True,
                        "manual_verification_required": False,
                        "disconnected": True,
                        "audit_complete": True,
                    }
                )
                writer.append({"event": "field_sample"})

            snapshot = read_progress_snapshot(path)

        self.assertEqual(snapshot["outcome"], "incomplete")
        self.assertFalse(snapshot["audit_complete"])
        self.assertTrue(snapshot["manual_verification_required"])
        self.assertIn(
            "run_finished is not the final durable record",
            snapshot["integrity_errors"],
        )
        self.assertIn(
            "completed outcome does not include every requested point",
            snapshot["integrity_errors"],
        )

    def test_monitor_rejects_float_encoded_integer_schema_and_indices(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "float-integers.jsonl"
            with JsonlEventWriter(path, create=True) as writer:
                writer.append(
                    {
                        "event": "run_started",
                        "command": "single-target",
                        "normal_end_field_policy": "hold",
                        "ordered_points": [{"bx_t": 0.0, "bz_t": 0.0}],
                    }
                )
                writer.append(
                    {
                        "event": "point_completed",
                        "point_index": 0,
                        "requested_field": {"bx_t": 0.0, "bz_t": 0.0},
                    }
                )
                writer.append(
                    {
                        "event": "run_finished",
                        "outcome": "completed",
                        "completed": True,
                        "completed_points": 1,
                        "requested_points": 1,
                        "zero_verified": True,
                        "manual_verification_required": False,
                        "disconnected": True,
                        "audit_complete": True,
                    }
                )
            records = [json.loads(line) for line in path.read_text().splitlines()]
            records[0]["schema_version"] = 1.0
            records[1]["event_index"] = 1.0
            records[1]["point_index"] = 0.0
            path.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )

            snapshot = read_progress_snapshot(path)

        self.assertEqual(snapshot["outcome"], "incomplete")
        self.assertFalse(snapshot["audit_complete"])
        self.assertTrue(snapshot["manual_verification_required"])
        self.assertIn(
            "record 0 has an unsupported schema_version",
            snapshot["integrity_errors"],
        )
        self.assertIn(
            "record 1 has a non-contiguous event_index",
            snapshot["integrity_errors"],
        )
        self.assertIn(
            "point_completed indices are not a contiguous prefix",
            snapshot["integrity_errors"],
        )

    def test_monitor_rejects_completed_zero_required_run_without_verified_zero(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unverified-zero.jsonl"
            with JsonlEventWriter(path, create=True) as writer:
                writer.append(
                    {
                        "event": "run_started",
                        "command": "single-target",
                        "normal_end_field_policy": "hold",
                        "ordered_points": [{"bx_t": 0.0, "bz_t": 0.0}],
                    }
                )
                writer.append(
                    {
                        "event": "point_completed",
                        "point_index": 0,
                        "requested_field": {"bx_t": 0.0, "bz_t": 0.0},
                    }
                )
                writer.append(
                    {
                        "event": "run_finished",
                        "outcome": "completed",
                        "completed": True,
                        "completed_points": 1,
                        "requested_points": 1,
                        "zero_verified": False,
                        "manual_verification_required": False,
                        "disconnected": True,
                        "audit_complete": True,
                        "last_confirmed_state": {
                            "field": {"bx_t": 0.0, "bz_t": 0.0}
                        },
                    }
                )

            snapshot = read_progress_snapshot(path)

        self.assertEqual(snapshot["outcome"], "incomplete")
        self.assertFalse(snapshot["audit_complete"])
        self.assertTrue(snapshot["manual_verification_required"])
        self.assertIn(
            "completed zero-required run lacks verified zero",
            snapshot["integrity_errors"],
        )

    def test_monitor_rejects_completed_run_without_last_confirmed_state(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing-final-state.jsonl"
            with JsonlEventWriter(path, create=True) as writer:
                writer.append(
                    {
                        "event": "run_started",
                        "command": "scan",
                        "normal_end_field_policy": "hold",
                        "ordered_points": [{"bx_t": 0.1, "bz_t": 0.0}],
                    }
                )
                writer.append(
                    {
                        "event": "point_completed",
                        "point_index": 0,
                        "requested_field": {"bx_t": 0.1, "bz_t": 0.0},
                    }
                )
                writer.append(
                    {
                        "event": "run_finished",
                        "outcome": "completed",
                        "completed": True,
                        "completed_points": 1,
                        "requested_points": 1,
                        "zero_verified": False,
                        "manual_verification_required": False,
                        "disconnected": True,
                        "audit_complete": True,
                        "last_confirmed_state": None,
                    }
                )

            snapshot = read_progress_snapshot(path)

        self.assertEqual(snapshot["outcome"], "incomplete")
        self.assertFalse(snapshot["audit_complete"])
        self.assertTrue(snapshot["manual_verification_required"])
        self.assertIn(
            "completed outcome lacks a last confirmed state",
            snapshot["integrity_errors"],
        )


if __name__ == "__main__":
    unittest.main()
