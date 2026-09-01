"""Query-only terminal presentation for the Three-SMU live monitor.

This module owns no VISA resources and sends no instrument commands.  The CLI
collects sequential Keithley snapshots and passes them here for safety warnings
and a stable terminal panel.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping

from .keithley2400 import KeithleyMonitorReading
from .three_smu_config import (
    ChannelRole,
    SEMANTIC_ROLES,
    SmuHardwareConfig,
    SourceMode,
)


@dataclass(frozen=True, slots=True)
class ThreeSmuLiveSnapshot:
    """One sequential, query-only snapshot of the active semantic SMU roles."""

    sample_index: int
    captured_at_utc: datetime
    status_queue_consumed: bool
    plan_roles: Mapping[str, ChannelRole]
    readings: Mapping[str, KeithleyMonitorReading]
    problems: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.sample_index < 0:
            raise ValueError("sample_index must be non-negative")
        if (
            self.captured_at_utc.tzinfo is None
            or self.captured_at_utc.utcoffset() is None
        ):
            raise ValueError("captured_at_utc must be timezone-aware")
        if self.captured_at_utc.utcoffset().total_seconds() != 0:
            raise ValueError("captured_at_utc must be UTC")
        if set(self.plan_roles) != set(SEMANTIC_ROLES):
            raise ValueError("live snapshot plan_roles must contain all semantic roles")
        active_roles = {
            role
            for role, channel_role in self.plan_roles.items()
            if channel_role is not ChannelRole.OFF
        }
        if set(self.readings) != active_roles:
            raise ValueError("live snapshot readings must contain exactly the active roles")


def monitor_problems(
    role: str,
    config: SmuHardwareConfig,
    reading: KeithleyMonitorReading,
) -> tuple[str, ...]:
    """Return warnings without taking over or changing the instrument state."""

    problems: list[str] = []
    if reading.source_mode is not config.source_mode:
        problems.append(
            f"{role} source mode is {reading.source_mode.value}, expected "
            f"{config.source_mode.value}"
        )
    if (
        reading.voltage_v is not None
        and config.max_abs_voltage_v is not None
        and abs(reading.voltage_v) > config.max_abs_voltage_v
    ):
        problems.append(
            f"{role} voltage {reading.voltage_v:g} V exceeds max_abs_voltage_v "
            f"{config.max_abs_voltage_v:g} V"
        )
    if (
        reading.current_a is not None
        and config.max_abs_current_a is not None
        and abs(reading.current_a) > config.max_abs_current_a
    ):
        problems.append(
            f"{role} current {reading.current_a:g} A exceeds max_abs_current_a "
            f"{config.max_abs_current_a:g} A"
        )
    active_max = (
        config.max_abs_current_a
        if reading.source_mode is SourceMode.VOLTAGE
        else config.max_abs_voltage_v
    )
    unit = "A" if reading.source_mode is SourceMode.VOLTAGE else "V"
    if active_max is not None and reading.compliance_limit > active_max:
        problems.append(
            f"{role} instrument compliance {reading.compliance_limit:g} {unit} "
            f"exceeds configured max_abs_{'current_a' if unit == 'A' else 'voltage_v'} "
            f"{active_max:g} {unit}"
        )
    if reading.compliance_trip:
        problems.append(f"{role} compliance trip is active")
    if reading.status_queue_consumed and not _status_is_clean(reading.status):
        problems.append(f"{role} error queue reports {reading.status}")
    if reading.output_enabled:
        problems.append(f"{role} output is ON; monitor will not change it")
    return tuple(problems)


def format_live_three_smu_snapshot(snapshot: ThreeSmuLiveSnapshot) -> str:
    """Return a compact, terminal-friendly three-role status panel."""

    timestamp = snapshot.captured_at_utc.astimezone(timezone.utc).isoformat(
        timespec="seconds"
    )
    status_text = "queried" if snapshot.status_queue_consumed else "not queried"
    lines = [
        f"Three-SMU live status | sample {snapshot.sample_index} | {timestamp} | "
        f"error queue: {status_text}",
        "role         plan   source        setpoint          V read          I read          R=V/I       output  trip   sense",
    ]
    for role in SEMANTIC_ROLES:
        if snapshot.plan_roles[role] is ChannelRole.OFF:
            lines.append(
                f"{role:<12} off    not connected; physical state is unknown"
            )
            continue
        reading = snapshot.readings[role]
        source_unit = "V" if reading.source_mode is SourceMode.VOLTAGE else "A"
        voltage = _format_measurement(reading.voltage_v, "V")
        current = _format_measurement(reading.current_a, "A")
        resistance = _format_resistance(reading.resistance_ohm)
        lines.append(
            f"{role:<12} {snapshot.plan_roles[role].value:<6} "
            f"{reading.source_mode.value:<12} "
            f"{reading.source_setpoint:>9.4g} {source_unit:<1} "
            f"{voltage:>13} "
            f"{current:>13} "
            f"{resistance:>11} "
            f"{'ON' if reading.output_enabled else 'OFF':<7} "
            f"{_format_trip(reading.compliance_trip):<6} "
            f"{'4W' if reading.four_wire else '2W'}"
        )
        measure_unit = "A" if reading.source_mode is SourceMode.VOLTAGE else "V"
        status = reading.status if reading.status_queue_consumed else "not queried"
        lines.append(
            f"  {role}: IDN={reading.identity}; compliance={reading.compliance_limit:g} "
            f"{measure_unit}; source range={reading.source_range:g} {source_unit}; "
            f"measure range={reading.measure_range:g} {measure_unit}; status={status}"
        )
        if not reading.output_enabled:
            lines.append(
                f"  {role}: live V/I/R and trip state unavailable while output "
                "is OFF; :READ? and protection-trip query not sent"
            )
    if snapshot.problems:
        lines.append("warnings:")
        lines.extend(f"  - {problem}" for problem in snapshot.problems)
    elif not snapshot.status_queue_consumed:
        lines.append(
            "note: error queues were not queried; error status is intentionally unknown."
        )
    return "\n".join(lines)


def _format_resistance(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4e} ohm"


def _format_measurement(value: float | None, unit: str) -> str:
    return "n/a" if value is None else f"{value:.4e} {unit}"


def _format_trip(value: bool | None) -> str:
    if value is None:
        return "n/a"
    return "TRIP" if value else "clear"


def _status_is_clean(status: str | None) -> bool:
    if status is None:
        return False
    prefix = status.strip().split(",", 1)[0].strip()
    try:
        return int(float(prefix)) == 0
    except ValueError:
        return False
