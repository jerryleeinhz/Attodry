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
        source_min=-2.0,
        source_max=2.0,
        ramp_step=0.25,
        readback_tolerance=1e-6,
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

    def test_gate_cannot_be_current_source(self) -> None:
        text = HARDWARE_EXAMPLE.read_text(encoding="utf-8")
        marker = "[gate_top]"
        before, after = text.split(marker, 1)
        after = after.replace(
            'source_mode = "voltage"', 'source_mode = "current"', 1
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hardware.toml"
            path.write_text(before + marker + after, encoding="utf-8")
            with self.assertRaisesRegex(ThreeSmuConfigError, "must be 'voltage'"):
                load_three_smu_hardware(path)

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
