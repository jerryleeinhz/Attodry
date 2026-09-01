"""Offline tests for the temperature-outer / excitation-inner coordinator."""

from contextlib import redirect_stdout
import csv
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from attodry_control.attodry import AttoDryAuthorizationError
from attodry_control.lockin_test import _capture_sweep_point
from attodry_control.models import LockinReading, LockinRole
from attodry_control.sr830 import LiaStatus, Sr830Error, Sr830HarmonicSample
from attodry_control.temperature_excitation_scan import (
    _enrich_excitation_temperature_records,
    _load_resume_progress,
    _temperature_window_statistics,
    run as run_temperature_excitation_scan,
)
from test_attodry import FakeAttoDryDll, StepClock
from test_sr830 import FakeResourceManager, FakeVisaResource


class TemperatureExcitationScanTests(unittest.TestCase):
    """Exercise the coordinator without loading any vendor DLL or VISA backend."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix="temperature-excitation-"
        )
        self.workdir = Path(self.temporary_directory.name)
        self.addCleanup(self.temporary_directory.cleanup)

    def _config(self) -> tuple[Path, Path]:
        """Build the smallest valid hardware contract beside its safety TOML."""

        config_path = self.workdir / "hardware.local.toml"
        output_directory = self.workdir / "temperature-excitation-output"
        text = Path("config/hardware.example.toml").read_text(encoding="utf-8")
        text = (
            text.replace("CHANGE_ME_COM_PORT", "COM_FAKE")
            .replace("C:/CHANGE_ME/attoDRYxyz64bit.dll", "fake-attodry.dll")
            .replace("CHANGE_ME_SR830_XX_VISA_ADDRESS", "XX")
            .replace("CHANGE_ME_SR830_XY_VISA_ADDRESS", "XY")
            .replace(
                'temperature_ranges = [\n'
                '  { min = 1.7, max = 2.1, scale = "linear", points = 5 },\n'
                '  { min = 2.2, max = 2.7, scale = "linear", points = 6 },\n'
                ']\n',
                "start_k = 1.7\nstop_k = 1.7\nstep_k = 0.1\n",
                1,
            )
            .replace(
                "stable_dwell_s = 30.0\npoll_interval_s = 1.5\nwait_timeout_s = 1800.0",
                "stable_dwell_s = 1.0\npoll_interval_s = 0.1\nwait_timeout_s = 10.0",
                1,
            )
            .replace(
                "sensitivity_full_scale_v = 1.0",
                "sensitivity_full_scale_v = 0.020",
                1,
            )
            .replace(
                "sensitivity_full_scale_v = 0.010",
                "sensitivity_full_scale_v = 0.001",
                1,
            )
            .replace(
                'excitation_ranges = [\n'
                '  { min = 0.004, max = 0.400, scale = "linear", points = 11 },\n'
                '  { min = 0.45, max = 5.0, scale = "linear", points = 21 },\n'
                "]",
                "excitation_points_v_rms = [0.004]",
                1,
            )
            .replace("samples_per_point = 3", "samples_per_point = 1", 1)
            .replace(
                'output_directory = "../run_data/temperature_excitation_commissioning"',
                'output_directory = "temperature-excitation-output"',
                1,
            )
            # Keep generated fixture files below Windows' legacy path limit when
            # this test suite itself is run from a deeply nested worktree.
            .replace('run_name = "temperature_excitation"', 'run_name = "te"', 1)
        )
        config_path.write_text(text, encoding="utf-8")
        (self.workdir / "lockin_safety.toml").write_text(
            Path("config/lockin_safety.toml").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        return config_path, output_directory

    def test_requires_authorization_before_dll_visa_or_output_creation(self) -> None:
        config_path, output_directory = self._config()
        dll_loads: list[Path] = []
        visa_factories: list[object] = []

        with self.assertRaises(AttoDryAuthorizationError):
            run_temperature_excitation_scan(
                ["--config", str(config_path)],
                dll_loader=lambda path: dll_loads.append(Path(path)),
                resource_manager_factory=lambda: visa_factories.append(object()),
            )

        self.assertEqual(dll_loads, [])
        self.assertEqual(visa_factories, [])
        self.assertFalse(output_directory.exists())

    def test_temperature_window_statistics_uses_time_weighted_trapezoid_mean(self) -> None:
        statistics = _temperature_window_statistics(
            (
                {
                    "captured_monotonic_s": 5.0,
                    "state": {"sample_temperature_k": 3.0},
                },
                {
                    "captured_monotonic_s": 0.0,
                    "state": {"sample_temperature_k": 1.0},
                },
                {
                    "captured_monotonic_s": 2.0,
                    "state": {"sample_temperature_k": 3.0},
                },
            )
        )

        self.assertEqual(statistics["method"], "time-weighted-trapezoid")
        self.assertAlmostEqual(statistics["mean_k"], 2.6)
        self.assertEqual(statistics["minimum_k"], 1.0)
        self.assertEqual(statistics["maximum_k"], 3.0)
        self.assertEqual(statistics["sample_count"], 3)
        self.assertAlmostEqual(statistics["duration_s"], 5.0)

    def test_condition_temperature_excludes_gaps_between_formal_samples(self) -> None:
        def sample(start_s: float, start_k: float, end_s: float, end_k: float):
            return {
                "measurement_context": {
                    "before": {
                        "captured_monotonic_s": start_s,
                        "state": {"sample_temperature_k": start_k},
                    },
                    "after": {
                        "captured_monotonic_s": end_s,
                        "state": {"sample_temperature_k": end_k},
                    },
                }
            }

        record = {
            "points": [
                {
                    "samples": [
                        sample(0.0, 1.0, 1.0, 3.0),
                        # The 99-second gap is amplitude/harmonic preparation,
                        # not part of either formal paired-read window.
                        sample(100.0, 7.0, 101.0, 9.0),
                    ]
                }
            ]
        }

        statistics = _enrich_excitation_temperature_records(record)

        self.assertEqual(statistics["method"], "time-weighted-over-formal-windows")
        self.assertAlmostEqual(statistics["mean_k"], 5.0)
        self.assertAlmostEqual(statistics["duration_s"], 2.0)

    def test_formal_temperature_callback_brackets_actual_sr830_pair(self) -> None:
        events: list[str] = []

        class FakeLockin:
            def __init__(self, role: LockinRole) -> None:
                self.role = role

            def read_harmonic_sample(self, harmonic: int) -> Sr830HarmonicSample:
                events.append(self.role.value)
                return Sr830HarmonicSample(
                    reading=LockinReading(
                        role=self.role,
                        harmonic=harmonic,
                        x_v=1.0e-6,
                        y_v=0.0,
                        amplitude_v=1.0e-6,
                        phase_deg=0.0,
                        phase_shift_deg=0.0,
                        frequency_hz=17.777,
                        locked=True,
                        overload=False,
                    ),
                    lia_status=LiaStatus(
                        raw=0,
                        input_or_reserve_overload=False,
                        filter_overload=False,
                        output_overload=False,
                        reference_unlocked=False,
                        frequency_range_changed=False,
                        time_constant_changed=False,
                        triggered=False,
                    ),
                    error_status=0,
                    captured_at_utc=datetime.now(timezone.utc),
                )

        formal_samples: list[dict[str, object]] = []
        record: dict[str, object] = {
            "point_index": 0,
            "points_total": 2,
            "source_v_rms": 0.004,
            "source_readback_v_rms": 0.0039,
            "nominal_current_a_rms": 3.88e-8,
            "samples": [],
            "harmonic_transition_status": [],
        }

        def context(stage: str, _harmonic: int, _sample_index: int):
            events.append(stage)
            return {"stage": stage}

        _capture_sweep_point(
            FakeLockin(LockinRole.XX),
            FakeLockin(LockinRole.XY),
            target_frequency_hz=17.777,
            harmonics=(1,),
            selected_roles_by_harmonic={1: ("lockin_xx", "lockin_xy")},
            harmonic_settle_s=0.0,
            samples=1,
            sample_interval_s=0.0,
            record=record,
            frequency_rel_tolerance=1e-5,
            measurement_context=context,
            on_formal_sample_recorded=lambda sample: formal_samples.append(dict(sample)),
        )

        self.assertEqual(events, ["before", "xx", "xy", "after"])
        self.assertEqual(record["samples"][0]["harmonic"], 1)
        self.assertEqual(formal_samples[0]["measurement_context"], {
            "before": {"stage": "before"},
            "after": {"stage": "after"},
        })
        self.assertEqual(
            formal_samples[0]["sweep_point"],
            {
                "point_index": 0,
                "points_total": 2,
                "source_v_rms": 0.004,
                "source_readback_v_rms": 0.0039,
                "nominal_current_a_rms": 3.88e-8,
            },
        )

    def test_successful_small_scan_keeps_stability_and_formal_temperatures_distinct(
        self,
    ) -> None:
        config_path, _ = self._config()
        dll = FakeAttoDryDll()
        dll.sample_temperature_k = 1.7
        dll.user_temperature_k = 1.7
        dll.temperature_follows_setpoint = True
        manager = FakeResourceManager(
            {
                "XX": FakeVisaResource({}),
                "XY": FakeVisaResource({}),
            }
        )

        def execute_one_formal_pair(
            _lockin_xx,
            _lockin_xy,
            *,
            measurement_context,
            on_formal_sample_recorded,
            **_kwargs,
        ):
            # The stable window has already finished at 1.7 K.  Deliberately
            # change the fake readback only during the formal acquisition window.
            dll.sample_temperature_k = 1.75
            before = measurement_context("before", 1, 0)
            dll.sample_temperature_k = 1.80
            after = measurement_context("after", 1, 0)
            sample = {
                "harmonic": 1,
                "sample_index": 0,
                "measurement_context": {"before": before, "after": after},
                "lockin_xx": {
                    "reading": {
                        "x_v": 1.0e-6,
                        "y_v": 2.0e-7,
                        "amplitude_v": 1.02e-6,
                        "phase_deg": 11.0,
                        "frequency_hz": 17.777,
                    },
                    "lia_status": {"raw": 0},
                    "error_status": 0,
                },
                "lockin_xy": {
                    "reading": {
                        "x_v": 3.0e-7,
                        "y_v": 4.0e-8,
                        "amplitude_v": 3.03e-7,
                        "phase_deg": -5.0,
                        "frequency_hz": 17.777,
                    },
                    "lia_status": {"raw": 0},
                    "error_status": 0,
                },
            }
            on_formal_sample_recorded(sample)
            return (
                {
                    "completed": True,
                    "outcome": "completed",
                    "points": [
                        {
                            "source_v_rms": 0.004,
                            "source_readback_v_rms": 0.004,
                            "nominal_current_a_rms": 4.0e-8,
                            "samples": [sample],
                        }
                    ],
                    "cleanup": {"attempted": True, "verified": True, "errors": []},
                    "error": None,
                },
                None,
            )

        output = io.StringIO()
        with (
            patch(
                "attodry_control.temperature_excitation_scan."
                "_execute_excitation_sweep_on_open_pair",
                side_effect=execute_one_formal_pair,
            ),
            patch("attodry_control.lockin_test.time.sleep", return_value=None),
            redirect_stdout(output),
        ):
            exit_code = run_temperature_excitation_scan(
                [
                    "--config",
                    str(config_path),
                    "--authorize-temperature-excitation-scan",
                ],
                dll_loader=lambda _path: dll,
                resource_manager_factory=lambda: manager,
                monotonic=StepClock(step_s=0.1),
                sleeper=lambda _seconds: None,
                wall_time=lambda: 123.0,
            )

        result = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertTrue(result["completed"])
        self.assertEqual(result["outcome"], "completed")
        condition = result["temperature_conditions"][0]
        self.assertAlmostEqual(condition["stability_measurement_temperature_k"], 1.7)
        self.assertAlmostEqual(condition["measurement_temperature_k"], 1.775)
        self.assertNotEqual(
            condition["stability_measurement_temperature_k"],
            condition["measurement_temperature_k"],
        )
        formal_sample = condition["excitation"]["points"][0]["samples"][0]
        self.assertAlmostEqual(formal_sample["measurement_temperature_k"], 1.775)
        self.assertEqual(
            formal_sample["measurement_window_temperature"]["method"],
            "time-weighted-trapezoid",
        )

        progress_path = Path(result["progress_jsonl"])
        events = [
            json.loads(line)
            for line in progress_path.read_text(encoding="utf-8").splitlines()
        ]
        self.assertIn("temperature_stable", [event["event"] for event in events])
        self.assertEqual(
            sum(event["event"] == "measurement_temperature_sample" for event in events),
            2,
        )
        formal_event = next(
            event for event in events if event["event"] == "lockin_formal_sample"
        )
        self.assertAlmostEqual(
            formal_event["sample"]["measurement_temperature_k"], 1.775
        )
        self.assertIn(
            "temperature_condition_completed", [event["event"] for event in events]
        )

        with Path(result["formal_samples_csv"]).open(
            encoding="utf-8", newline=""
        ) as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([row["role"] for row in rows], ["lockin_xx", "lockin_xy"])
        for row in rows:
            self.assertAlmostEqual(float(row["measurement_temperature_k"]), 1.775)
            self.assertEqual(row["lia_status_raw"], "0")
            self.assertEqual(row["error_status"], "0")
        self.assertTrue(manager.closed)

    def test_inner_failure_closes_pair_then_disables_temperature_control(self) -> None:
        config_path, _ = self._config()
        dll = FakeAttoDryDll()
        dll.sample_temperature_k = 1.7
        dll.user_temperature_k = 1.7
        dll.temperature_follows_setpoint = True
        manager = FakeResourceManager(
            {"XX": FakeVisaResource({}), "XY": FakeVisaResource({})}
        )

        def reject_after_lockin_cleanup(*_args, **_kwargs):
            return (
                {
                    "completed": False,
                    "outcome": "rejected",
                    "points": [],
                    "cleanup": {"attempted": True, "verified": True, "errors": []},
                    "error": "injected inner failure",
                },
                Sr830Error("injected inner failure"),
            )

        output = io.StringIO()
        with (
            patch(
                "attodry_control.temperature_excitation_scan."
                "_execute_excitation_sweep_on_open_pair",
                side_effect=reject_after_lockin_cleanup,
            ),
            redirect_stdout(output),
        ):
            with self.assertRaisesRegex(Sr830Error, "injected inner failure"):
                run_temperature_excitation_scan(
                    [
                        "--config",
                        str(config_path),
                        "--authorize-temperature-excitation-scan",
                    ],
                    dll_loader=lambda _path: dll,
                    resource_manager_factory=lambda: manager,
                    monotonic=StepClock(step_s=0.1),
                    sleeper=lambda _seconds: None,
                    wall_time=lambda: 124.0,
                )

        summary = json.loads(output.getvalue())
        self.assertEqual(summary["outcome"], "rejected")
        self.assertIn(
            "temperature_control_disabled_after_failure", summary["recovery_actions"]
        )
        self.assertEqual(dll.temperature_control, 0)
        self.assertTrue(manager.closed)
        self.assertIn("set_temperature", dll.events)
        self.assertGreaterEqual(dll.events.count("toggle_temperature_control"), 2)

    def test_resume_only_accepts_completed_temperature_boundaries(self) -> None:
        progress_path = self.workdir / "resume_progress.jsonl"
        measurement_config = {"fixture": "temperature-excitation"}
        completed = {
            "temperature_index": 0,
            "requested_temperature_k": 1.7,
            "marker": "accepted-condition",
        }
        events = (
            {
                "event": "scan_started",
                "command": "temperature-excitation-scan",
                "measurement_config": measurement_config,
            },
            {"event": "temperature_condition_completed", "summary": completed},
            {
                "event": "excitation_finished",
                "temperature_index": 1,
                "completed": False,
            },
        )
        progress_path.write_text(
            "\n".join(json.dumps(event) for event in events) + "\n",
            encoding="utf-8",
        )

        restored = _load_resume_progress(
            progress_path, measurement_config, (1.7, 1.8)
        )

        self.assertEqual(restored, [completed])


if __name__ == "__main__":
    unittest.main()
