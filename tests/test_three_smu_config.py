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
    load_three_smu_hardware,
    load_three_smu_operation_config,
    load_three_smu_scan,
    validate_plan_targets,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
HARDWARE_EXAMPLE = PROJECT_ROOT / "config" / "three_smu_hardware.example.toml"
SCAN_EXAMPLE = PROJECT_ROOT / "config" / "three_smu_scan.example.toml"


def channel(
    role: ChannelRole,
    *,
    fixed: float = 0.0,
    start: float = 0.0,
    stop: float = 0.0,
    step: float = 1.0,
) -> ChannelPlan:
    return ChannelPlan(role, fixed, start, stop, step)


def plan(mode: ScanMode, **changes) -> ThreeSmuScanPlan:
    values = dict(
        mode=mode,
        samples_per_point=1,
        delay_s=0.0,
        bidirectional=False,
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
        timeout_ms=5000,
        source_mode=SourceMode.VOLTAGE,
        compliance_current_a=1e-3,
        compliance_voltage_v=10.0,
        max_abs_voltage_v=10.0,
        max_abs_current_a=1e-3,
        source_min_v=-2.0,
        source_max_v=2.0,
        ramp_step_v=0.25,
        readback_tolerance_v=1e-6,
        source_min_a=-1e-3,
        source_max_a=1e-3,
        ramp_step_a=1e-4,
        readback_tolerance_a=1e-9,
        settle_s=0.0,
        nplc=1.0,
        source_auto_range=True,
        measure_auto_range=True,
        four_wire=False,
        leakage_limit_a=None if role == "smu_bias" else 1e-6,
    )


def hardware() -> ThreeSmuHardwareConfig:
    return ThreeSmuHardwareConfig(
        smu("smu_bias", "FAKE::1"),
        smu("gate_top", "FAKE::2"),
        smu("gate_bottom", "FAKE::3"),
    )


class ThreeSmuConfigTests(unittest.TestCase):
    def test_single_daily_operation_config_reuses_gate_safety_limits(self) -> None:
        text = """
[smu_bias]
model = "Keithley2400"
address = "FAKE::1"
timeout_ms = 1000
source_mode = "voltage"
compliance_current_a = 0.001
compliance_voltage_v = 10.0
max_abs_voltage_v = 10.0
max_abs_current_a = 0.001
source_min_v = -1.0
source_max_v = 1.0
ramp_step_v = 0.1
readback_tolerance_v = 0.000001
source_min_a = -0.001
source_max_a = 0.001
ramp_step_a = 0.0001
readback_tolerance_a = 0.000000001
settle_s = 0.0
nplc = 1.0
source_auto_range = true
measure_auto_range = true
four_wire = false

[gate_top]
model = "Keithley2400"
address = "FAKE::2"
source_mode = "voltage"
compliance_a = 0.001
compliance_voltage_v = 3.0
leakage_limit_a = 0.000001
max_abs_voltage_v = 3.0
max_abs_current_a = 0.001
source_min_v = -2.0
source_max_v = 2.0
ramp_step_v = 0.1
readback_tolerance_v = 0.000001
source_min_a = -0.001
source_max_a = 0.001
ramp_step_a = 0.0001
readback_tolerance_a = 0.000000001
settle_s = 0.0

[gate_top.smu]
timeout_ms = 1000
nplc = 1.0
source_auto_range = true
measure_auto_range = true
four_wire = false

[gate_bottom]
model = "Keithley2400"
address = "FAKE::3"
source_mode = "voltage"
compliance_a = 0.0005
compliance_voltage_v = 4.0
leakage_limit_a = 0.000001
max_abs_voltage_v = 4.0
max_abs_current_a = 0.0005
source_min_v = -2.0
source_max_v = 2.0
ramp_step_v = 0.1
readback_tolerance_v = 0.000001
source_min_a = -0.0005
source_max_a = 0.0005
ramp_step_a = 0.0001
readback_tolerance_a = 0.000000001
settle_s = 0.0

[gate_bottom.smu]
timeout_ms = 1000
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
bidirectional = false
serpentine = true
finish_action = "zero_disable"
point_count = 1
pulse_high_s = 0.0
pulse_period_s = 0.0

[three_smu_run.smu_bias]
role = "fixed"
fixed = 0.001
start = 0.0
stop = 0.0
step = 1.0

[three_smu_run.gate_top]
role = "sweep"
fixed = 0.0
start = -1.0
stop = 1.0
step = 1.0

[three_smu_run.gate_bottom]
role = "sweep"
fixed = 0.0
start = -1.0
stop = 1.0
step = 1.0
"""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hardware.local.toml"
            path.write_text(text, encoding="utf-8")
            operation = load_three_smu_operation_config(path)
            self.assertEqual(operation.output_directory, Path(directory) / "runs")
            self.assertEqual(operation.hardware.gate_top.source_max, 2.0)
            self.assertEqual(operation.hardware.gate_top.max_abs_voltage_v, 3.0)
            self.assertEqual(operation.hardware.gate_bottom.max_abs_voltage_v, 4.0)
            self.assertEqual(operation.hardware.gate_bottom.max_abs_current_a, 0.0005)
            self.assertEqual(operation.plan.gate_bottom.role, ChannelRole.SWEEP)
            self.assertEqual(len(validate_plan_targets(operation.hardware, operation.plan)), 9)

    def test_checked_in_hardware_template_is_intentionally_not_ready(self) -> None:
        config = load_three_smu_hardware(HARDWARE_EXAMPLE)
        with self.assertRaisesRegex(ThreeSmuConfigError, "not ready"):
            config.require_ready()

    def test_checked_in_scan_example_loads_offline(self) -> None:
        loaded = load_three_smu_scan(SCAN_EXAMPLE)
        self.assertEqual(loaded.mode, ScanMode.BIAS_IV)
        self.assertEqual(loaded.smu_bias.role, ChannelRole.SWEEP)

    def test_unknown_hardware_field_is_rejected(self) -> None:
        text = HARDWARE_EXAMPLE.read_text(encoding="utf-8").replace(
            'model = "Keithley2400"',
            'model = "Keithley2400"\nunknown = true',
            1,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hardware.toml"
            path.write_text(text, encoding="utf-8")
            with self.assertRaisesRegex(ThreeSmuConfigError, "unknown"):
                load_three_smu_hardware(path)

    def test_gate_can_be_current_source_with_current_unit_limits(self) -> None:
        top = replace(
            smu("gate_top", "FAKE::2"),
            source_mode=SourceMode.CURRENT,
            leakage_limit_a=None,
        )
        configured = ThreeSmuHardwareConfig(
            smu("smu_bias", "FAKE::1"),
            top,
            smu("gate_bottom", "FAKE::3"),
        )
        configured.require_ready()
        self.assertEqual(configured.gate_top.source_min, -1e-3)
        self.assertEqual(configured.gate_top.ramp_step, 1e-4)

    def test_compliance_cannot_exceed_matching_absolute_limit(self) -> None:
        with self.assertRaisesRegex(
            ThreeSmuConfigError, "compliance_current_a cannot exceed"
        ):
            replace(
                smu("gate_top", "FAKE::2"),
                compliance_current_a=2e-3,
            )

    def test_source_range_cannot_exceed_matching_absolute_limit(self) -> None:
        with self.assertRaisesRegex(ThreeSmuConfigError, "source range exceeds"):
            replace(
                smu("gate_top", "FAKE::2"),
                source_max_a=2e-3,
            )

    def test_duplicate_configured_address_is_rejected(self) -> None:
        top = smu("gate_top", "FAKE::1")
        with self.assertRaisesRegex(ThreeSmuConfigError, "distinct"):
            ThreeSmuHardwareConfig(
                smu("smu_bias", "FAKE::1"),
                top,
                smu("gate_bottom", "FAKE::3"),
            ).require_ready()

    def test_bidirectional_bias_scan_has_single_turnaround_point(self) -> None:
        points = generate_scan_points(
            plan(
                ScanMode.BIAS_IV,
                bidirectional=True,
                smu_bias=channel(
                    ChannelRole.SWEEP, start=-1.0, stop=1.0, step=1.0
                ),
            )
        )
        self.assertEqual(
            [point.coordinates["smu_bias"] for point in points],
            [-1.0, 0.0, 1.0, 0.0, -1.0],
        )
        self.assertEqual(points[-1].segment, "reverse")

    def test_serpentine_map_reverses_inner_axis(self) -> None:
        points = generate_scan_points(
            plan(
                ScanMode.MULTI_SMU_MAP,
                serpentine=True,
                smu_bias=channel(
                    ChannelRole.SWEEP, start=0.0, stop=1.0, step=1.0
                ),
                gate_top=channel(
                    ChannelRole.SWEEP, start=10.0, stop=20.0, step=10.0
                ),
            )
        )
        self.assertEqual(
            [
                (point.coordinates["smu_bias"], point.coordinates["gate_top"])
                for point in points
            ],
            [(0.0, 10.0), (0.0, 20.0), (1.0, 20.0), (1.0, 10.0)],
        )

    def test_software_pulse_generates_high_and_low_per_cycle(self) -> None:
        points = generate_scan_points(
            plan(
                ScanMode.SOFTWARE_PULSE,
                point_count=2,
                pulse_high_s=0.1,
                pulse_period_s=0.4,
                smu_bias=channel(
                    ChannelRole.SWEEP, start=0.0, stop=0.5, step=1.0
                ),
            )
        )
        self.assertEqual([point.segment for point in points], [
            "pulse_high", "pulse_low", "pulse_high", "pulse_low"
        ])
        self.assertEqual(points[0].coordinates["smu_bias"], 0.5)
        self.assertEqual(points[1].coordinates["smu_bias"], 0.0)

    def test_paired_gate_requires_matching_point_counts(self) -> None:
        with self.assertRaisesRegex(ThreeSmuConfigError, "same point count"):
            plan(
                ScanMode.PAIRED_GATE,
                gate_top=channel(ChannelRole.SWEEP, start=0, stop=1, step=1),
                gate_bottom=channel(ChannelRole.SWEEP, start=0, stop=2, step=1),
            )

    def test_target_range_is_checked_before_any_driver_factory(self) -> None:
        outside = plan(
            ScanMode.BIAS_IV,
            smu_bias=channel(ChannelRole.SWEEP, start=0.0, stop=3.0, step=1.0),
        )
        with self.assertRaisesRegex(ThreeSmuConfigError, "outside configured"):
            validate_plan_targets(hardware(), outside)


if __name__ == "__main__":
    unittest.main()
