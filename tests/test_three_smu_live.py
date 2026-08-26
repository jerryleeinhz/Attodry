from datetime import datetime, timezone
import unittest

from attodry_control.keithley2400 import KeithleyMonitorReading
from attodry_control.three_smu_config import (
    ChannelRole,
    SmuHardwareConfig,
    SourceMode,
)
from attodry_control.three_smu_live import (
    ThreeSmuLiveSnapshot,
    format_live_three_smu_snapshot,
    monitor_problems,
)


def smu(role: str) -> SmuHardwareConfig:
    return SmuHardwareConfig(
        role=role,
        model="Keithley2400",
        address=f"FAKE::{role}",
        source_mode=SourceMode.VOLTAGE,
        max_abs_voltage_v=5.0,
        max_abs_current_a=1e-3,
        nplc=1.0,
        source_auto_range=True,
        measure_auto_range=True,
        four_wire=False,
    )


def reading(role: str, **changes) -> KeithleyMonitorReading:
    values = dict(
        identity=f"KEITHLEY,MODEL 2400,{role},1.0",
        source_mode=SourceMode.VOLTAGE,
        source_setpoint=0.1,
        output_enabled=False,
        voltage_v=0.1,
        current_a=5e-7,
        compliance_limit=1e-3,
        source_range=1.0,
        measure_range=1e-3,
        four_wire=False,
        compliance_trip=False,
        status=None,
        status_queue_consumed=False,
    )
    values.update(changes)
    return KeithleyMonitorReading(**values)


class ThreeSmuLiveTests(unittest.TestCase):
    def test_terminal_panel_shows_three_roles_and_unqueried_status(self) -> None:
        readings = {role: reading(role) for role in ("smu_bias", "gate_top")}
        snapshot = ThreeSmuLiveSnapshot(
            sample_index=0,
            captured_at_utc=datetime(2026, 8, 26, tzinfo=timezone.utc),
            status_queue_consumed=False,
            plan_roles={
                "smu_bias": ChannelRole.FIXED,
                "gate_top": ChannelRole.SWEEP,
                "gate_bottom": ChannelRole.OFF,
            },
            readings=readings,
        )
        panel = format_live_three_smu_snapshot(snapshot)
        self.assertIn("smu_bias", panel)
        self.assertIn("gate_top", panel)
        self.assertIn("gate_bottom", panel)
        self.assertIn("physical state is unknown", panel)
        self.assertIn("output", panel)
        self.assertIn("error queues were not queried", panel)

    def test_gate_current_limit_is_reported_without_changing_instrument_state(self) -> None:
        problems = monitor_problems(
            "gate_top", smu("gate_top"), reading("gate_top", current_a=2e-3)
        )
        self.assertTrue(any("max_abs_current_a" in problem for problem in problems))


if __name__ == "__main__":
    unittest.main()
