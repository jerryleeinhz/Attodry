from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from attodry_control.three_smu_config import (
    ChannelPlan,
    ChannelRole,
    FinishAction,
    ScanMode,
    SmuHardwareConfig,
    SourceMode,
    ThreeSmuConfigError,
    ThreeSmuHardwareConfig,
    ThreeSmuScanPlan,
    generate_scan_points,
    load_three_smu_operation_config,
    validate_plan_targets,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HARDWARE_EXAMPLE = PROJECT_ROOT / "config" / "hardware.example.toml"


def channel(
    role: ChannelRole,
    *,
    bidirectional: bool = False,
    fixed: float | None = None,
    start: float | None = None,
    stop: float | None = None,
    step: float | None = None,
    points: tuple[float, ...] | None = None,
) -> ChannelPlan:
    return ChannelPlan(role, bidirectional, fixed, start, stop, step, points)


def plan(mode: ScanMode, **changes) -> ThreeSmuScanPlan:
    values = dict(
        mode=mode,
        samples_per_point=1,
        delay_s=0.0,
        serpentine=False,
        finish_action=FinishAction.ZERO_DISABLE,
        point_count=1,
        pulse_high_s=0.0,
        pulse_period_s=0.0,
        smu_bias=channel(ChannelRole.OFF),
        gate_top=channel(ChannelRole.OFF),
        gate_bottom=channel(ChannelRole.OFF),
    )
    values.update(changes)
    return ThreeSmuScanPlan(**values)


def smu(role: str, address: str) -> SmuHardwareConfig:
    return SmuHardwareConfig(
        role=role,
        model="Keithley2400",
        address=address,
        source_mode=SourceMode.VOLTAGE,
        max_abs_voltage_v=10.0,
        max_abs_current_a=1e-3,
        nplc=1.0,
        source_auto_range=True,
        measure_auto_range=True,
        four_wire=False,
    )


def hardware() -> ThreeSmuHardwareConfig:
    return ThreeSmuHardwareConfig(
        smu("smu_bias", "FAKE::1"),
        smu("gate_top", "FAKE::2"),
        smu("gate_bottom", "FAKE::3"),
    )


OPERATION_TEXT = """
[smu_bias]
model = "Keithley2400"
address = "FAKE::1"
source_mode = "voltage"
max_abs_voltage_v = 10.0
max_abs_current_a = 0.001
nplc = 1.0
source_auto_range = true
measure_auto_range = true
four_wire = false

[gate_top]
model = "Keithley2400"
address = "FAKE::2"
source_mode = "voltage"
max_abs_voltage_v = 3.0
max_abs_current_a = 0.001
nplc = 1.0
source_auto_range = true
measure_auto_range = true
four_wire = false

[gate_bottom]
model = "Keithley2400"
address = "FAKE::3"
source_mode = "voltage"
max_abs_voltage_v = 4.0
max_abs_current_a = 0.0005
nplc = 1.0
source_auto_range = true
measure_auto_range = true
four_wire = false

[three_smu_run]
output_directory = "runs"
run_name = "fake-two-gate-map"
note = "offline only"
mode = "multi_smu_map"
samples_per_point = 1
delay_s = 0.0
serpentine = true
finish_action = "zero_disable"
point_count = 1
pulse_high_s = 0.0
pulse_period_s = 0.0

[three_smu_run.smu_bias]
role = "fixed"
bidirectional = false
fixed = 0.001

[three_smu_run.gate_top]
role = "sweep"
bidirectional = false
points = [-1.0, 0.0, 1.0]

[three_smu_run.gate_bottom]
role = "sweep"
bidirectional = false
ranges = [
  { min = -1.0, max = 1.0, scale = "linear", step = 1.0 },
]
"""


BOTTOM_ONLY_OPERATION_TEXT = """
[gate_bottom]
model = "Keithley2400"
address = "FAKE::3"
source_mode = "voltage"
max_abs_voltage_v = 4.0
max_abs_current_a = 0.0005
nplc = 1.0
source_auto_range = true
measure_auto_range = true
four_wire = false

[three_smu_run]
output_directory = "runs"
run_name = "bottom-only"
note = "offline only"
mode = "bottom_gate_transfer"
samples_per_point = 1
delay_s = 0.0
serpentine = false
finish_action = "zero_disable"
point_count = 1
pulse_high_s = 0.0
pulse_period_s = 0.0

[three_smu_run.smu_bias]
role = "off"
bidirectional = false

[three_smu_run.gate_top]
role = "off"
bidirectional = false

[three_smu_run.gate_bottom]
role = "sweep"
bidirectional = false
points = [-1.0, 0.0, 1.0]
"""


class ThreeSmuConfigTests(unittest.TestCase):
    def test_off_roles_need_no_hardware_table_and_active_gate_uses_one_table(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hardware.local.toml"
            path.write_text(BOTTOM_ONLY_OPERATION_TEXT, encoding="utf-8")
            operation = load_three_smu_operation_config(path)
        self.assertEqual(set(operation.hardware.by_role()), {"gate_bottom"})
        self.assertEqual(operation.hardware.gate_bottom.max_abs_voltage_v, 4.0)
        self.assertEqual(len(validate_plan_targets(operation.hardware, operation.plan)), 3)

    def test_off_role_ignores_recognized_stale_scan_values(self) -> None:
        text = BOTTOM_ONLY_OPERATION_TEXT.replace(
            """[three_smu_run.smu_bias]
role = "off"
bidirectional = false
""",
            """[three_smu_run.smu_bias]
role = "off"
bidirectional = "ignored while off"
fixed = "ignored while off"
points = "ignored while off"
ranges = "ignored while off"
start = "ignored while off"
stop = "ignored while off"
step = "ignored while off"
""",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hardware.local.toml"
            path.write_text(text, encoding="utf-8")
            operation = load_three_smu_operation_config(path)
        self.assertEqual(operation.plan.smu_bias, ChannelPlan(ChannelRole.OFF, False))
        self.assertEqual(set(operation.hardware.by_role()), {"gate_bottom"})

    def test_off_role_still_rejects_unknown_scan_field_names(self) -> None:
        text = BOTTOM_ONLY_OPERATION_TEXT.replace(
            'role = "off"\nbidirectional = false',
            'role = "off"\nbidirectional = false\npoitns = [1.0]',
            1,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hardware.local.toml"
            path.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(ThreeSmuConfigError, "unknown.*poitns"):
                load_three_smu_operation_config(path)

    def test_single_daily_operation_config_loads_minimal_safety_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hardware.local.toml"
            path.write_text(OPERATION_TEXT, encoding="utf-8")
            operation = load_three_smu_operation_config(path)
        self.assertEqual(operation.hardware.gate_top.max_abs_voltage_v, 3.0)
        self.assertEqual(operation.hardware.gate_bottom.max_abs_current_a, 0.0005)
        self.assertEqual(operation.hardware.smu_bias.nplc, 1.0)
        self.assertEqual(operation.plan.gate_bottom.points, (-1.0, 0.0, 1.0))
        self.assertEqual(len(validate_plan_targets(operation.hardware, operation.plan)), 9)

    def test_sweep_ranges_expand_linear_points_log_and_multiple_segments(self) -> None:
        original = '''ranges = [
  { min = -1.0, max = 1.0, scale = "linear", step = 1.0 },
]'''
        cases = (
            (
                '''ranges = [
  { min = -1.0, max = 1.0, scale = "linear", points = 5 },
]''',
                (-1.0, -0.5, 0.0, 0.5, 1.0),
            ),
            (
                '''ranges = [
  { min = 0.001, max = 1.0, scale = "log", points = 4 },
]''',
                (0.001, 0.01, 0.1, 1.0),
            ),
            (
                '''ranges = [
  { min = -1.0, max = 0.0, scale = "linear", step = 1.0 },
  { min = 0.0, max = 1.0, scale = "linear", points = 2 },
]''',
                (-1.0, 0.0, 0.0, 1.0),
            ),
        )
        for replacement, expected in cases:
            with self.subTest(replacement=replacement):
                text = OPERATION_TEXT.replace(original, replacement)
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "hardware.local.toml"
                    path.write_text(text, encoding="utf-8")
                    operation = load_three_smu_operation_config(path)
                self.assertEqual(
                    len(operation.plan.gate_bottom.points or ()), len(expected)
                )
                for actual, wanted in zip(
                    operation.plan.gate_bottom.points or (), expected, strict=True
                ):
                    self.assertAlmostEqual(actual, wanted)

    def test_bidirectional_applies_after_all_ranges_are_concatenated(self) -> None:
        text = BOTTOM_ONLY_OPERATION_TEXT.replace(
            "bidirectional = false\npoints = [-1.0, 0.0, 1.0]",
            '''bidirectional = true
ranges = [
  { min = -1.0, max = 0.0, scale = "linear", points = 2 },
  { min = 0.5, max = 1.0, scale = "linear", points = 2 },
]''',
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hardware.local.toml"
            path.write_text(text, encoding="utf-8")
            operation = load_three_smu_operation_config(path)
        points = generate_scan_points(operation.plan)
        self.assertEqual(
            [point.coordinates["gate_bottom"] for point in points],
            [-1.0, 0.0, 0.5, 1.0, 0.5, 0.0, -1.0],
        )

    def test_active_sweep_requires_exactly_one_of_points_or_ranges(self) -> None:
        original = '''ranges = [
  { min = -1.0, max = 1.0, scale = "linear", step = 1.0 },
]'''
        cases = (
            original + "\npoints = [-1.0, 0.0, 1.0]",
            "start = -1.0\nstop = 1.0\nstep = 1.0",
        )
        for replacement in cases:
            with self.subTest(replacement=replacement):
                text = OPERATION_TEXT.replace(original, replacement)
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "hardware.local.toml"
                    path.write_text(text, encoding="utf-8")
                    with self.assertRaisesRegex(
                        ThreeSmuConfigError, "points|ranges|unknown"
                    ):
                        load_three_smu_operation_config(path)

    def test_invalid_sweep_range_definitions_fail_closed(self) -> None:
        original = '''ranges = [
  { min = -1.0, max = 1.0, scale = "linear", step = 1.0 },
]'''
        cases = (
            '''ranges = [
  { min = -1.0, max = 1.0, scale = "linear", step = 1.0, points = 3 },
]''',
            '''ranges = [
  { min = -1.0, max = 1.0, scale = "log", points = 3 },
]''',
            '''ranges = [
  { min = 1.0, max = 10.0, scale = "log", step = 1.0 },
]''',
            "ranges = []",
        )
        for replacement in cases:
            with self.subTest(replacement=replacement):
                text = OPERATION_TEXT.replace(original, replacement)
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "hardware.local.toml"
                    path.write_text(text, encoding="utf-8")
                    with self.assertRaises(ThreeSmuConfigError):
                        load_three_smu_operation_config(path)

    def test_checked_in_unified_template_is_intentionally_not_ready(self) -> None:
        operation = load_three_smu_operation_config(HARDWARE_EXAMPLE)
        with self.assertRaisesRegex(ThreeSmuConfigError, "not ready"):
            operation.hardware.require_ready()
        self.assertEqual(operation.hardware.smu_bias.nplc, 1.0)

    def test_unified_template_has_no_legacy_safety_or_split_templates(self) -> None:
        text = HARDWARE_EXAMPLE.read_text(encoding="utf-8")
        for removed in (
            "compliance_current_a",
            "compliance_voltage_v",
            "leakage_limit_a",
            "source_min_v",
            "source_max_v",
            "ramp_step_v",
            "readback_tolerance_v",
            "settle_s",
            "[gate_top.smu]",
            "[gate_bottom.smu]",
        ):
            self.assertNotIn(removed, text)
        three_smu_text = text.split("[gate_top]", 1)[1]
        self.assertNotIn("timeout_ms", three_smu_text)
        self.assertIn(
            '{ min = -0.1, max = 0.1, scale = "linear", step = 0.05 }',
            three_smu_text,
        )
        self.assertIn(
            '#   { min = -0.1, max = 0.1, scale = "linear", points = 5 }',
            three_smu_text,
        )
        self.assertIn(
            '#   { min = 1e-6, max = 1e-3, scale = "log", points = 10 }',
            three_smu_text,
        )
        self.assertIn("# points = [-0.1, -0.05, 0.0, 0.05, 0.1]", three_smu_text)
        self.assertIn("# points = [1.0, 3.0, 7.0, 2.0, 2.0]", three_smu_text)
        self.assertNotIn("# start =", three_smu_text)
        self.assertNotIn("# stop =", three_smu_text)
        self.assertFalse((PROJECT_ROOT / "config" / "three_smu_hardware.example.toml").exists())
        self.assertFalse((PROJECT_ROOT / "config" / "three_smu_scan.example.toml").exists())

    def test_removed_hardware_field_is_rejected(self) -> None:
        text = OPERATION_TEXT.replace(
            'max_abs_voltage_v = 10.0',
            'max_abs_voltage_v = 10.0\nsettle_s = 0.1',
            1,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hardware.toml"
            path.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(ThreeSmuConfigError, "unknown"):
                load_three_smu_operation_config(path)

    def test_timeout_and_nested_gate_smu_tables_are_rejected_for_active_roles(self) -> None:
        cases = (
            OPERATION_TEXT.replace(
                'address = "FAKE::1"',
                'address = "FAKE::1"\ntimeout_ms = 5000',
                1,
            ),
            OPERATION_TEXT.replace(
                'four_wire = false\n\n[gate_bottom]',
                'four_wire = false\n\n[gate_top.smu]\nnplc = 1.0\n\n[gate_bottom]',
                1,
            ),
        )
        for text in cases:
            with self.subTest(text=text):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "hardware.toml"
                    path.write_text(text, encoding="utf-8")
                    with self.assertRaisesRegex(ThreeSmuConfigError, "unknown"):
                        load_three_smu_operation_config(path)

    def test_current_source_uses_current_target_limit(self) -> None:
        top = replace(smu("gate_top", "FAKE::2"), source_mode=SourceMode.CURRENT)
        configured = ThreeSmuHardwareConfig(
            smu("smu_bias", "FAKE::1"), top, smu("gate_bottom", "FAKE::3")
        )
        target_plan = plan(
            ScanMode.MULTI_SMU_MAP,
            gate_top=channel(ChannelRole.SWEEP, points=(0.0, 5e-4)),
        )
        self.assertEqual(len(validate_plan_targets(configured, target_plan)), 2)

    def test_keithley_capability_and_compliance_floor_are_validated(self) -> None:
        with self.assertRaisesRegex(ThreeSmuConfigError, "capability"):
            replace(smu("smu_bias", "FAKE::1"), max_abs_voltage_v=211.0)
        with self.assertRaisesRegex(ThreeSmuConfigError, "minimum programmable"):
            replace(smu("smu_bias", "FAKE::1"), max_abs_current_a=0.5e-9)
        with self.assertRaisesRegex(ThreeSmuConfigError, "operating envelope"):
            replace(
                smu("smu_bias", "FAKE::1"),
                max_abs_voltage_v=210.0,
                max_abs_current_a=1.0,
            )

    def test_fixed_range_without_explicit_range_is_rejected(self) -> None:
        with self.assertRaisesRegex(ThreeSmuConfigError, "range=true"):
            replace(smu("smu_bias", "FAKE::1"), measure_auto_range=False)

    def test_duplicate_configured_address_is_rejected(self) -> None:
        with self.assertRaisesRegex(ThreeSmuConfigError, "distinct"):
            ThreeSmuHardwareConfig(
                smu("smu_bias", "FAKE::1"),
                smu("gate_top", "FAKE::1"),
                smu("gate_bottom", "FAKE::3"),
            ).require_ready()

    def test_explicit_points_preserve_arbitrary_order_and_duplicates(self) -> None:
        points = generate_scan_points(
            plan(
                ScanMode.BIAS_IV,
                smu_bias=channel(
                    ChannelRole.SWEEP, points=(1.0, 3.0, 7.0, 2.0, 2.0)
                ),
            )
        )
        self.assertEqual(
            [point.coordinates["smu_bias"] for point in points],
            [1.0, 3.0, 7.0, 2.0, 2.0],
        )

    def test_per_smu_bidirectional_has_single_turnaround_point(self) -> None:
        points = generate_scan_points(
            plan(
                ScanMode.BIAS_IV,
                smu_bias=channel(
                    ChannelRole.SWEEP,
                    bidirectional=True,
                    points=(-1.0, 0.0, 1.0),
                ),
            )
        )
        self.assertEqual(
            [point.coordinates["smu_bias"] for point in points],
            [-1.0, 0.0, 1.0, 0.0, -1.0],
        )
        self.assertEqual(points[-1].segment, "reverse")

    def test_multi_map_expands_each_role_before_cartesian_product(self) -> None:
        points = generate_scan_points(
            plan(
                ScanMode.MULTI_SMU_MAP,
                smu_bias=channel(
                    ChannelRole.SWEEP, bidirectional=True, points=(0.0, 1.0)
                ),
                gate_top=channel(ChannelRole.SWEEP, points=(10.0, 20.0)),
            )
        )
        self.assertEqual(len(points), 6)

    def test_paired_gate_validates_lengths_after_independent_expansion(self) -> None:
        with self.assertRaisesRegex(ThreeSmuConfigError, "same point count"):
            plan(
                ScanMode.PAIRED_GATE,
                gate_top=channel(
                    ChannelRole.SWEEP, bidirectional=True, points=(0.0, 1.0)
                ),
                gate_bottom=channel(ChannelRole.SWEEP, points=(0.0, 1.0)),
            )

    def test_software_pulse_rejects_bidirectional(self) -> None:
        with self.assertRaisesRegex(ThreeSmuConfigError, "does not allow bidirectional"):
            plan(
                ScanMode.SOFTWARE_PULSE,
                point_count=2,
                pulse_high_s=0.1,
                pulse_period_s=0.4,
                smu_bias=channel(
                    ChannelRole.SWEEP,
                    bidirectional=True,
                    points=(0.0, 0.5),
                ),
            )

    def test_target_absolute_limit_is_checked_before_driver_factory(self) -> None:
        outside = plan(
            ScanMode.BIAS_IV,
            smu_bias=channel(ChannelRole.SWEEP, points=(0.0, 11.0)),
        )
        with self.assertRaisesRegex(ThreeSmuConfigError, "absolute limit"):
            validate_plan_targets(hardware(), outside)


if __name__ == "__main__":
    unittest.main()
