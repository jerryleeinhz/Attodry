from __future__ import annotations

from contextlib import redirect_stdout
import copy
import io
import json
from pathlib import Path
import re
import tomllib
import unittest
from unittest.mock import Mock

from attodry_control.config import (
    ConfigError, _parse_magnetic_field_run, load_magnetic_field_operation_config,
)
from attodry_control.field_audit import read_jsonl_events
from attodry_control.field_segments import (
    MAX_SEGMENT_POINTS, expand_angle_segments, expand_field_segments,
)
from attodry_control.magnetic_field_cli import run
from attodry_control.magnetic_field_monitor import read_progress_snapshot
from attodry_control.models import VectorField
from attodry_control.safety import MagnetLimits
from tests.test_attodry import FakeAttoDryDll, StepClock
from tests.test_magnetic_field import PROJECT_ROOT, _MagneticConfigFixture


class FieldSegmentTests(unittest.TestCase):
    def test_constant_magnitude_angle_segments_preserve_order_and_cardinal_zeros(self):
        plan = expand_angle_segments(0.1, [
            {"min_deg": 0, "max_deg": 180, "step_deg": 90},
            {"min_deg": 0, "max_deg": 180, "points": 3, "direction": "descending"},
        ], MagnetLimits())
        self.assertEqual(plan.angles_deg, (0.0, 90.0, 180.0, 180.0, 90.0, 0.0))
        self.assertEqual(plan.points, (
            VectorField(0.0, 0.1), VectorField(0.1, 0.0), VectorField(0.0, -0.1),
            VectorField(0.0, -0.1), VectorField(0.1, 0.0), VectorField(0.0, 0.1),
        ))
        self.assertEqual(plan.point_segment_indices, (0, 0, 0, 1, 1, 1))
        metadata = plan.metadata()
        self.assertEqual(metadata["version"], 2)
        self.assertEqual(metadata["magnitude_t"], 0.1)
        self.assertEqual(
            expand_angle_segments(metadata["magnitude_t"], metadata["segments"], MagnetLimits()),
            plan,
        )

    def test_angle_segments_fail_closed_on_bad_grid_and_field_limits(self):
        valid = [{"min_deg": 0, "max_deg": 90, "points": 3}]
        for magnitude in (0, -0.1, True, float("nan"), 3.1):
            with self.subTest(magnitude=magnitude), self.assertRaises(ValueError):
                expand_angle_segments(magnitude, valid, MagnetLimits())
        with self.assertRaisesRegex(ValueError, "vector-field limit"):
            expand_angle_segments(
                3.1, [{"min_deg": 0, "max_deg": 180, "step_deg": 180}],
                MagnetLimits(),
            )
        for segments in (
            [{"min_deg": 0, "max_deg": 90, "step_deg": 7}],
            [{"min_deg": 0, "max_deg": 90, "step_deg": 0}],
            [{"min_deg": 90, "max_deg": 0, "points": 3}],
            [{"min_deg": 0, "max_deg": 90, "points": 1}],
            [{"min_deg": 0, "max_deg": 90, "points": 3, "step_deg": 45}],
            [{"min_deg": 0, "max_deg": 90, "points": 3, "direction": "reverse"}],
            [{"min_deg": 0, "max_deg": 90, "points": 3, "bx_t": 0.1}],
            [{"min_deg": 0, "max_deg": 90, "points": 6000}] * 2,
        ):
            with self.subTest(segments=segments), self.assertRaises(ValueError):
                expand_angle_segments(0.1, segments, MagnetLimits())
        with self.assertRaises(ValueError):
            expand_angle_segments(
                0.2, [{"min_deg": 0, "max_deg": 90, "points": 3}],
                MagnetLimits(hardware_x_max_t=0.1),
            )

    def test_mixed_spacing_reverse_and_duplicate_boundaries(self):
        plan = expand_field_segments("x", [
            {"min": -0.1, "max": 0.0, "step": 0.05},
            {"min": 0.0, "max": 0.1, "points": 3},
            {"min": -0.1, "max": 0.1, "step": 0.1, "direction": "descending"},
        ], MagnetLimits())
        self.assertEqual([p.bx_t for p in plan.points],
                         [-0.1, -0.05, 0.0, 0.0, 0.05, 0.1, 0.1, 0.0, -0.1])
        self.assertTrue(all(p.bz_t == 0 for p in plan.points))
        self.assertEqual(plan.point_segment_indices, (0, 0, 0, 1, 1, 1, 2, 2, 2))
        restored = expand_field_segments("x", plan.metadata()["segments"], MagnetLimits())
        self.assertEqual(restored, plan)

    def test_pure_z_high_field_and_signed_limits(self):
        plan = expand_field_segments("z", [{"min": -9, "max": 9, "points": 3}], MagnetLimits())
        self.assertEqual(plan.points, (VectorField(0, -9), VectorField(0, 0), VectorField(0, 9)))
        with self.assertRaises(ValueError):
            expand_field_segments("x", [{"min": -9, "max": 9, "points": 3}], MagnetLimits())
        with self.assertRaises(ValueError):
            expand_field_segments("z", [{"min": 0, "max": 9.01, "points": 2}], MagnetLimits())
        with self.assertRaises(ValueError):
            expand_field_segments("x", [{"min": 0, "max": 0.2, "points": 2}],
                                  MagnetLimits(hardware_x_max_t=0.1))

    def test_invalid_segments_fail_closed(self):
        invalid = [None, [], [1], [{}],
            [{"min": 0, "max": 1}],
            [{"min": 0, "max": 1, "step": 0.1, "points": 11}],
            [{"min": 0, "max": 1, "points": True}],
            [{"min": 0, "max": 1, "points": 2.5}],
            [{"min": 0, "max": 1, "points": 1}],
            [{"min": 1, "max": 0, "points": 2}],
            [{"min": 0, "max": 0, "points": 2}],
            [{"min": True, "max": 1, "points": 2}],
            [{"min": 0, "max": float("nan"), "points": 2}],
            [{"min": 0, "max": float("inf"), "points": 2}],
            [{"min": 0, "max": 1, "step": 0}],
            [{"min": 0, "max": 1, "step": -0.1, "direction": "descending"}],
            [{"min": 0, "max": 1, "points": 2, "direction": "down"}],
            [{"min": 0, "max": 1, "points": 2, "scale": "log"}],
        ]
        for segments in invalid:
            with self.subTest(segments=segments), self.assertRaises(ValueError):
                expand_field_segments("x", segments, MagnetLimits())
        for axis in (None, "y", "X", 1, [], {}):
            with self.subTest(axis=axis), self.assertRaises(ValueError):
                expand_field_segments(axis, [{"min": 0, "max": 1, "points": 2}], MagnetLimits())

    def test_decimal_steps_include_endpoint_without_silent_short_last_step(self):
        with self.assertRaisesRegex(ValueError, "divide max-min"):
            expand_field_segments("z", [{"min": 0.45, "max": 5.0, "step": 0.5}], MagnetLimits())
        plan = expand_field_segments("z", [{"min": 0.45, "max": 4.95, "step": 0.5}], MagnetLimits())
        self.assertEqual(len(plan.points), 10)
        self.assertEqual(plan.points[-1], VectorField(0.0, 4.95))

    def test_counts_bounded_before_allocation(self):
        for segments in (
            [{"min": 0, "max": 1, "points": 10**100}],
            [{"min": 0, "max": 1, "step": 1e-300}],
            [{"min": 0, "max": 1, "points": 6000}] * 2,
        ):
            with self.subTest(segments=segments), self.assertRaisesRegex(ValueError, "expanded points"):
                expand_field_segments("x", segments, MagnetLimits())
        plan = expand_field_segments("x", [{"min": 0, "max": 1, "points": MAX_SEGMENT_POINTS}], MagnetLimits())
        self.assertEqual(len(plan.points), MAX_SEGMENT_POINTS)


class FieldSegmentConfigCliTests(_MagneticConfigFixture, unittest.TestCase):
    def segmented_config(self, *, axis="x", segments=None):
        path = self.write_config(((0.0, 0.0),), transition_policy="direct", normal_end_policy="zero")
        definition = segments or '[{ min = -0.1, max = 0.1, points = 3 }, { min = -0.1, max = 0.1, points = 3, direction = "descending" }]'
        path.write_text(path.read_text(encoding="utf-8").replace(
            "points = [{ bx_t = 0.0, bz_t = 0.0 }]",
            f'axis = "{axis}"\nsegments = {definition}',
        ), encoding="utf-8")
        return path

    def test_config_expansion_and_exclusive_input_modes(self):
        path = self.segmented_config()
        config = load_magnetic_field_operation_config(path)
        self.assertEqual(len(config.run.points), 6)
        self.assertIsNotNone(config.run.segment_plan)
        original = path.read_text(encoding="utf-8")
        for text in (
            original.replace('axis = "x"\n', ''),
            original.replace('axis = "x"', 'axis = "y"'),
            original + '\npoints = [{ bx_t = 0.0, bz_t = 0.0 }]\n',
        ):
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_magnetic_field_operation_config(path)
        path = self.write_config(((0.0, 0.0),), extra_run='axis = "x"')
        with self.assertRaisesRegex(ConfigError, "only allowed with segments"):
            load_magnetic_field_operation_config(path)

    def test_every_commented_config_example_parses_when_copied(self):
        source = (PROJECT_ROOT / "config/hardware.example.toml").read_text(encoding="utf-8")
        base = tomllib.loads(source)["magnetic_field_run"]
        examples = re.findall(r"# BEGIN EXAMPLE (\w+)\n(.*?)# END EXAMPLE \1", source, re.S)
        self.assertEqual(len(examples), 5)
        expected_counts = {"M4_X": 1, "M5_X_SEGMENTS": 17, "M5_X_HYSTERESIS": 40,
                           "Z_SEGMENTS_SYNTAX_ONLY": 21, "ANGLE_SEGMENTS_SYNTAX_ONLY": 14}
        for name, commented in examples:
            with self.subTest(name=name):
                fragment = "\n".join(line.removeprefix("# ") for line in commented.splitlines())
                values = {k: v for k, v in base.items() if k not in {"points", "axis", "segments", "magnitude_t", "angle_segments"}}
                values.update(tomllib.loads(fragment))
                parsed = _parse_magnetic_field_run(values, MagnetLimits())
                self.assertEqual(len(parsed.points), expected_counts[name])

    def test_angle_config_describe_and_fake_scan_retain_requested_angles(self):
        path = self.write_config(((0.0, 0.0),), transition_policy="direct", normal_end_policy="zero")
        path.write_text(path.read_text(encoding="utf-8").replace(
            "points = [{ bx_t = 0.0, bz_t = 0.0 }]",
            'magnitude_t = 0.05\nangle_segments = ['
            '{ min_deg = 0, max_deg = 180, step_deg = 90 }, '
            '{ min_deg = 0, max_deg = 180, step_deg = 90, direction = "descending" }]',
        ), encoding="utf-8")
        loader = Mock(side_effect=AssertionError("describe must not load DLL"))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(run(["describe", "--config", str(path)], dll_loader=loader), 0)
        description = json.loads(output.getvalue())
        self.assertEqual(description["segment_plan"]["angles_deg"], [0, 90, 180, 180, 90, 0])
        self.assertEqual(description["point_count"], 6)
        loader.assert_not_called()
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(run(
                ["scan", "--config", str(path), "--authorize-connection",
                 "--authorize-field-writes", "--authorize-ordered-field-scan"],
                dll_loader=lambda _: FakeAttoDryDll(),
                monotonic=StepClock(), sleeper=lambda _: None,
            ), 0)
        progress = Path(json.loads(output.getvalue())["progress_jsonl"])
        events, _ = read_jsonl_events(progress)
        self.assertEqual(read_progress_snapshot(progress)["integrity_errors"], [])
        completed = [event for event in events if event["event"] == "point_completed"]
        self.assertEqual([event["requested_angle_deg"] for event in completed],
                         [0, 90, 180, 180, 90, 0])
        altered = copy.deepcopy(events)
        altered[0]["segment_plan"]["angles_deg"][1] = 45
        progress.write_text("".join(json.dumps(event) + "\n" for event in altered), encoding="utf-8")
        self.assertEqual(read_progress_snapshot(progress)["outcome"], "incomplete")
        altered = copy.deepcopy(events)
        next(event for event in altered if event["event"] == "point_completed")[
            "requested_angle_deg"
        ] = 45
        progress.write_text("".join(json.dumps(event) + "\n" for event in altered), encoding="utf-8")
        self.assertEqual(read_progress_snapshot(progress)["outcome"], "incomplete")

    def test_angle_config_rejects_mixed_input_modes_before_dll(self):
        loader = Mock(side_effect=AssertionError("must not load DLL"))
        path = self.write_config(((0.0, 0.0),), transition_policy="direct")
        original = path.read_text(encoding="utf-8")
        angle_fields = (
            'magnitude_t = 0.05\nangle_segments = '
            '[{ min_deg = 0, max_deg = 90, points = 3 }]'
        )
        for replacement in (
            angle_fields + '\npoints = [{ bx_t = 0.0, bz_t = 0.0 }]',
            angle_fields + '\naxis = "x"',
            angle_fields.replace("magnitude_t = 0.05\n", ""),
        ):
            with self.subTest(replacement=replacement):
                path.write_text(original.replace(
                    "points = [{ bx_t = 0.0, bz_t = 0.0 }]", replacement
                ), encoding="utf-8")
                with self.assertRaises(ConfigError):
                    run(["describe", "--config", str(path)], dll_loader=loader)
        loader.assert_not_called()

    def test_describe_has_no_dll_or_output_directory_side_effect(self):
        path = self.segmented_config()
        loader = Mock(side_effect=AssertionError("must not load DLL"))
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(run(["describe", "--config", str(path)], dll_loader=loader), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["point_count"], 6)
        self.assertFalse(result["hardware_connected"])
        self.assertEqual(result["segment_plan"]["point_segment_indices"], [0, 0, 0, 1, 1, 1])
        loader.assert_not_called()
        self.assertFalse((self.directory / "field-output").exists())

    def test_bad_grid_and_single_target_rejected_before_dll(self):
        loader = Mock(side_effect=AssertionError("must not load DLL"))
        path = self.segmented_config()
        with self.assertRaisesRegex(ValueError, "exactly one"):
            run(["single-target", "--config", str(path)], dll_loader=loader)
        path = self.segmented_config(axis="z", segments='[{ min = 0.45, max = 5.0, step = 0.5 }]')
        with self.assertRaises(ConfigError):
            run(["scan", "--config", str(path), "--authorize-connection",
                 "--authorize-field-writes", "--authorize-ordered-field-scan"], dll_loader=loader)
        loader.assert_not_called()
        self.assertFalse((self.directory / "field-output").exists())

    def test_fake_scan_order_zero_and_segment_audit_tampering(self):
        path = self.segmented_config()
        output = io.StringIO()
        with redirect_stdout(output):
            code = run(["scan", "--config", str(path), "--authorize-connection",
                        "--authorize-field-writes", "--authorize-ordered-field-scan"],
                       dll_loader=lambda _: FakeAttoDryDll(), monotonic=StepClock(), sleeper=lambda _: None)
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertTrue(result["zero_verified"])
        progress = Path(result["progress_jsonl"])
        self.assertEqual(read_progress_snapshot(progress)["integrity_errors"], [])
        events, _ = read_jsonl_events(progress)
        completed = [event for event in events if event["event"] == "point_completed"]
        self.assertEqual([event["requested_field"]["bx_t"] for event in completed],
                         [-0.1, 0.0, 0.1, 0.1, 0.0, -0.1])
        self.assertEqual([event["segment_index"] for event in completed], [0, 0, 0, 1, 1, 1])
        for kind in ("version", "points", "indices", "direction", "definition"):
            altered = copy.deepcopy(events)
            plan = altered[0]["segment_plan"]
            if kind == "version":
                plan["version"] = True
            elif kind == "points":
                altered[0]["ordered_points"][0]["bx_t"] = 0.05
            elif kind == "indices":
                plan["point_segment_indices"][0] = True
            elif kind == "direction":
                next(e for e in altered if e["event"] == "point_completed")["sweep_direction"] = "descending"
            else:
                plan["segments"][0]["max"] = 0.2
            progress.write_text("".join(json.dumps(event) + "\n" for event in altered), encoding="utf-8")
            with self.subTest(kind=kind):
                snapshot = read_progress_snapshot(progress)
                self.assertEqual(snapshot["outcome"], "incomplete")
                self.assertTrue(any("segment_plan" in error for error in snapshot["integrity_errors"]))
