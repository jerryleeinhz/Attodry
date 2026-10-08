"""Offline tests for the file-only temperature and SR830 progress monitors."""

import json
import os
from pathlib import Path
import tempfile
import unittest

from attodry_control.lockin_test import (
    _JsonlProgressWriter,
    _append_sweep_progress_point,
    _sweep_progress_formal_sample_callback,
)
from attodry_control.lockin_progress_monitor import LockinProgressView, run as run_lockin
from attodry_control.progress_monitor import (
    JsonlProgressTail, ProgressFormatError, resolve_progress_path,
)
from attodry_control.temperature_progress_monitor import (
    TemperatureProgressView,
    run as run_temperature,
)


class ProgressMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="progress-monitor-")
        self.directory = Path(self.temporary_directory.name)
        self.addCleanup(self.temporary_directory.cleanup)

    def test_tailer_defers_an_incomplete_last_jsonl_line(self) -> None:
        path = self.directory / "progress.jsonl"
        first = json.dumps({"event": "scan_started"}) + "\n"
        second = json.dumps({"event": "temperature_sample", "point_index": 1})
        path.write_text(first + second, encoding="utf-8")
        tail = JsonlProgressTail(path)

        self.assertEqual(tail.read_new_events(), ({"event": "scan_started"},))
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write("\n")
        self.assertEqual(
            tail.read_new_events(),
            ({"event": "temperature_sample", "point_index": 1},),
        )

    def test_tailer_defers_a_split_utf8_character_and_crlf(self) -> None:
        path = self.directory / "progress.jsonl"
        event = {"event": "scan_started", "name": "磁场"}
        data = (json.dumps(event, ensure_ascii=False) + "\r\n").encode("utf-8")
        split = data.index("磁".encode("utf-8")) + 1
        path.write_bytes(data[:split])
        tail = JsonlProgressTail(path)
        self.assertEqual(tail.read_new_events(), ())
        self.assertEqual(tail.read_new_events(), ())
        with path.open("ab") as stream:
            stream.write(data[split:])
        self.assertEqual(tail.read_new_events(), (event,))
        self.assertEqual(tail.read_new_events(), ())

    def test_tailer_does_not_skip_complete_corruption_on_refresh(self) -> None:
        for bad in (b'{bad}\n', b'[]\n', b'{"name":"\xff"}\n'):
            with self.subTest(bad=bad):
                path = self.directory / "progress.jsonl"
                path.write_bytes(b'{"event":"scan_started"}\n' + bad)
                tail = JsonlProgressTail(path)
                for _ in range(2):
                    with self.assertRaises(ProgressFormatError):
                        tail.read_new_events()

    def test_tailer_restarts_generation_after_replacement_or_truncation(self) -> None:
        for replacement in ("smaller", "same", "larger", "in_place"):
            with self.subTest(replacement=replacement):
                path = self.directory / "progress.jsonl"
                first = b'{"event":"scan_started","name":"old"}\n'
                path.write_bytes(first)
                tail = JsonlProgressTail(path)
                self.assertEqual(tail.read_new_events()[0]["name"], "old")
                second = b'{"event":"scan_started","name":"new"}\n'
                if replacement == "smaller":
                    second = b'{"event":"scan_started"}\n'
                elif replacement == "larger":
                    second += b'{"event":"point"}\n'
                if replacement == "in_place":
                    path.write_bytes(second)
                else:
                    other = self.directory / "replacement.jsonl"
                    other.write_bytes(second)
                    os.replace(other, path)
                events = tail.read_new_events()
                self.assertEqual(events[0]["event"], "scan_started")
                self.assertNotEqual(events[0].get("name"), "old")
                self.assertEqual(tail.generation, 1)
                self.assertEqual(tail.read_new_events(), ())
                self.assertEqual(tail.generation, 1)

    def test_tailer_append_does_not_reset_pending_or_generation(self) -> None:
        path = self.directory / "progress.jsonl"
        path.write_bytes(b'{"event":"scan_started"}\n{"event":"point"')
        tail = JsonlProgressTail(path)
        self.assertEqual(tail.read_new_events(), ({"event": "scan_started"},))
        with path.open("ab") as stream:
            stream.write(b'}\n')
        self.assertEqual(tail.read_new_events(), ({"event": "point"},))
        self.assertEqual(tail.generation, 0)

    def test_temperature_view_reports_latest_cryostat_state_and_lockin_phase(self) -> None:
        view = TemperatureProgressView()
        view.update(
            {
                "event": "temperature_sample",
                "command": "temperature-excitation-scan",
                "temperature_index": 2,
                "requested_temperature_k": 4.0,
                "state": {
                    "sample_temperature_k": 3.91,
                    "user_temperature_k": 4.0,
                    "vti_temperature_k": 4.12,
                    "temperature_control_enabled": True,
                    "error_code": 0,
                },
            }
        )
        view.update({"event": "excitation_started", "temperature_index": 2})

        rendered = view.format()
        self.assertIn("lockin excitation sweep", rendered)
        self.assertIn("target=4 K", rendered)
        self.assertIn("sample=3.91 K", rendered)
        self.assertIn("vti=4.12 K", rendered)
        self.assertIn("control=True", rendered)

    def test_lockin_view_includes_existing_sine_readback_and_phase(self) -> None:
        view = LockinProgressView()
        view.update(
            {
                "event": "lockin_formal_sample",
                "scan": "excitation",
                "temperature_index": 1,
                "requested_temperature_k": 2.0,
                "sweep_point": {
                    "point_index": 3,
                    "points_total": 11,
                    "source_v_rms": 0.04,
                    "source_readback_v_rms": 0.0399,
                    "nominal_current_a_rms": 3.97e-7,
                    "actual_frequency_hz": 17.777,
                },
                "sample": {
                    "harmonic": 2,
                    "sample_index": 0,
                    "measurement_temperature_k": 1.98,
                    "problems": [],
                    "lockin_xx": {
                        "reading": {
                            "amplitude_v": 0.0012,
                            "phase_deg": 12.5,
                            "locked": True,
                            "overload": False,
                        }
                    },
                    "lockin_xy": {
                        "reading": {
                            "amplitude_v": 2.5e-6,
                            "phase_deg": -91.0,
                            "locked": True,
                            "overload": False,
                        }
                    },
                },
            }
        )

        rendered = view.format()
        self.assertIn("point=4/11", rendered)
        self.assertIn("SINE target=0.04 V", rendered)
        self.assertIn("readback=0.0399 V", rendered)
        self.assertIn("I-nominal=397 nA", rendered)
        self.assertIn("h=2 sample=1", rendered)
        self.assertIn("Vxx: R=0.0012 V phase=12.5 deg [locked]", rendered)
        self.assertIn("Vxy: R=2.5e-06 V phase=-91 deg [locked]", rendered)

    def test_lockin_progress_events_reuse_point_readback_without_hardware_io(self) -> None:
        path = self.directory / "lockin_progress.jsonl"
        writer = _JsonlProgressWriter(path)
        point = {
            "point_index": 0,
            "points_total": 2,
            "source_v_rms": 0.004,
            "source_readback_v_rms": 0.0039,
            "nominal_current_a_rms": 3.88e-8,
            "actual_frequency_hz": 17.777,
        }
        _append_sweep_progress_point(
            writer, event="lockin_point_ready", scan="excitation", point=point
        )
        _sweep_progress_formal_sample_callback(writer, scan="excitation")(
            {
                "harmonic": 1,
                "sample_index": 0,
                "sweep_point": point,
                "lockin_xx": {"reading": {}},
                "lockin_xy": {"reading": {}},
                "problems": [],
            }
        )

        events = JsonlProgressTail(path).read_new_events()
        self.assertEqual(events[0]["event"], "lockin_point_ready")
        self.assertEqual(events[0]["sweep_point"]["source_readback_v_rms"], 0.0039)
        self.assertEqual(events[1]["event"], "lockin_formal_sample")
        self.assertEqual(events[1]["sweep_point"]["nominal_current_a_rms"], 3.88e-8)

    def test_cli_once_reads_file_without_hardware_configuration(self) -> None:
        temperature_path = self.directory / "a_temperature_scan_progress.jsonl"
        lockin_path = self.directory / "b_lockin_excitation_progress.jsonl"
        temperature_path.write_text(
            json.dumps({"event": "scan_started", "command": "temperature-scan"})
            + "\n",
            encoding="utf-8",
        )
        lockin_path.write_text(
            json.dumps({"event": "scan_started", "scan": "excitation"}) + "\n",
            encoding="utf-8",
        )
        temperature_lines: list[str] = []
        lockin_lines: list[str] = []

        self.assertEqual(
            run_temperature(
                ["--progress", str(temperature_path), "--once"],
                output=temperature_lines.append,
            ),
            0,
        )
        self.assertEqual(
            run_lockin(
                ["--progress", str(lockin_path), "--once"],
                output=lockin_lines.append,
            ),
            0,
        )
        self.assertIn("Monitoring file only", temperature_lines[0])
        self.assertIn("TEMPERATURE", temperature_lines[1])
        self.assertIn("Monitoring file only", lockin_lines[0])
        self.assertIn("LOCKIN", lockin_lines[1])

    def test_latest_resolution_uses_matching_newest_file(self) -> None:
        older = self.directory / "old_temperature_scan_progress.jsonl"
        newer = self.directory / "new_temperature_excitation_progress.jsonl"
        older.write_text("{}\n", encoding="utf-8")
        newer.write_text("{}\n", encoding="utf-8")
        os.utime(older, ns=(1_000_000_000, 1_000_000_000))
        os.utime(newer, ns=(2_000_000_000, 2_000_000_000))

        selected = resolve_progress_path(
            progress=None,
            directory=self.directory,
            patterns=("*_temperature*_progress.jsonl",),
        )
        self.assertEqual(selected, newer.resolve())


if __name__ == "__main__":
    unittest.main()
