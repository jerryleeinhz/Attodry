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
    """One sequential, query-only snapshot of the three semantic SMU roles."""

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
        for role in SEMANTIC_ROLES:
            if role not in self.plan_roles or role not in self.readings:
                raise ValueError(f"live snapshot is missing {role}")


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
    if config.max_abs_voltage_v is not None and (
        abs(reading.voltage_v) > config.max_abs_voltage_v
    ):
        problems.append(
            f"{role} voltage {reading.voltage_v:g} V exceeds max_abs_voltage_v "
            f"{config.max_abs_voltage_v:g} V"
        )
    if config.max_abs_current_a is not None and (
        abs(reading.current_a) > config.max_abs_current_a
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
        reading = snapshot.readings[role]
        source_unit = "V" if reading.source_mode is SourceMode.VOLTAGE else "A"
        resistance = _format_resistance(reading.resistance_ohm)
        lines.append(
            f"{role:<12} {snapshot.plan_roles[role].value:<6} "
            f"{reading.source_mode.value:<12} "
            f"{reading.source_setpoint:>9.4g} {source_unit:<1} "
            f"{reading.voltage_v:>11.4e} V "
            f"{reading.current_a:>11.4e} A "
            f"{resistance:>11} "
            f"{'ON' if reading.output_enabled else 'OFF':<7} "
            f"{'TRIP' if reading.compliance_trip else 'clear':<6} "
            f"{'4W' if reading.four_wire else '2W'}"
        )
        measure_unit = "A" if reading.source_mode is SourceMode.VOLTAGE else "V"
        status = reading.status if reading.status_queue_consumed else "not queried"
        lines.append(
            f"  {role}: IDN={reading.identity}; compliance={reading.compliance_limit:g} "
            f"{measure_unit}; source range={reading.source_range:g} {source_unit}; "
            f"measure range={reading.measure_range:g} {measure_unit}; status={status}"
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
    return "—" if value is None else f"{value:.4e} ohm"


def _status_is_clean(status: str | None) -> bool:
    if status is None:
        return False
    prefix = status.strip().split(",", 1)[0].strip()
    try:
        return int(float(prefix)) == 0
    except ValueError:
        return False
