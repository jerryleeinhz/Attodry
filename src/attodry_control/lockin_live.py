"""Read-only, terminal-friendly presentation for paired SR830 diagnostics.

This module deliberately owns no VISA resources and sends no commands.  The
hardware-facing CLI collects :class:`~attodry_control.sr830.Sr830Diagnostic`
objects, then passes them here for a concise live display.  Keeping the
presentation separate makes it usable from a terminal today and from a future
notebook/widget without duplicating safety or instrument logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .sr830 import Sr830Diagnostic
from .sr830_settings import sensitivity_full_scale_v


@dataclass(frozen=True, slots=True)
class LiveLockinPairSnapshot:
    """One sequential, read-only diagnostic snapshot of both SR830 roles."""

    sample_index: int
    captured_at_utc: datetime
    status_latches_consumed: bool
    lockin_xx: Sr830Diagnostic
    lockin_xy: Sr830Diagnostic
    problems: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.sample_index < 0:
            raise ValueError("sample_index must be non-negative")
        if self.captured_at_utc.tzinfo is None or self.captured_at_utc.utcoffset() is None:
            raise ValueError("captured_at_utc must be timezone-aware")
        if self.captured_at_utc.utcoffset().total_seconds() != 0:
            raise ValueError("captured_at_utc must be UTC")


def format_live_lockin_snapshot(snapshot: LiveLockinPairSnapshot) -> str:
    """Return a stable, human-readable two-role status panel.

    When status latches were intentionally not queried, the panel says
    ``not queried`` rather than presenting a potentially stale lock/overload
    state as fact.
    """

    timestamp = snapshot.captured_at_utc.astimezone(timezone.utc).isoformat(
        timespec="seconds"
    )
    header = (
        f"SR830 live status | sample {snapshot.sample_index} | {timestamp} | "
        f"status latches: {'queried' if snapshot.status_latches_consumed else 'not queried'}"
    )
    columns = (
        "role  X (V)        Y (V)        R (V)        phase (deg)  "
        "FREQ? (Hz)   SNAP f (Hz)  harm  SENS         SINE OUT (V)  "
        "lock        overload    error"
    )
    rows = (
        _format_diagnostic_row(snapshot.lockin_xx),
        _format_diagnostic_row(snapshot.lockin_xy),
    )
    lines = [header, columns, *rows]
    if snapshot.problems:
        lines.append("warnings:")
        lines.extend(f"  - {problem}" for problem in snapshot.problems)
    elif not snapshot.status_latches_consumed:
        lines.append(
            "note: lock/overload/error are intentionally unknown until "
            "--consume-status-latches is supplied."
        )
    if any(
        _confirmed_sensitivity_text(diagnostic) is None
        for diagnostic in (snapshot.lockin_xx, snapshot.lockin_xy)
    ):
        lines.append(
            "note: SENS shown as `code N*` is a raw SR830 code outside this "
            "project's confirmed range policy; the monitor did not change it."
        )
    return "\n".join(lines)


def _format_diagnostic_row(diagnostic: Sr830Diagnostic) -> str:
    sensitivity = _sensitivity_text(diagnostic)
    return (
        f"{diagnostic.role.value.upper():<4}  "
        f"{diagnostic.x_v:>11.4e}  "
        f"{diagnostic.y_v:>11.4e}  "
        f"{diagnostic.amplitude_v:>11.4e}  "
        f"{diagnostic.phase_deg:>11.3f}  "
        f"{diagnostic.frequency_hz:>11.6g}  "
        f"{diagnostic.snapshot_frequency_hz:>11.6g}  "
        f"{diagnostic.harmonic:>4d}  "
        f"{sensitivity:>10}  "
        f"{diagnostic.sine_output_v:>12.4g}  "
        f"{_lock_text(diagnostic):<10}  "
        f"{_overload_text(diagnostic):<10}  "
        f"{_error_text(diagnostic)}"
    )


def _confirmed_sensitivity_text(diagnostic: Sr830Diagnostic) -> str | None:
    try:
        return f"{sensitivity_full_scale_v(diagnostic.sensitivity):.4g} V"
    except ValueError:
        return None


def _sensitivity_text(diagnostic: Sr830Diagnostic) -> str:
    return _confirmed_sensitivity_text(diagnostic) or f"code {diagnostic.sensitivity}*"


def _lock_text(diagnostic: Sr830Diagnostic) -> str:
    if diagnostic.locked is None:
        return "not queried"
    return "LOCKED" if diagnostic.locked else "UNLOCKED"


def _overload_text(diagnostic: Sr830Diagnostic) -> str:
    if diagnostic.overload is None:
        return "not queried"
    return "OVERLOAD" if diagnostic.overload else "clear"


def _error_text(diagnostic: Sr830Diagnostic) -> str:
    if diagnostic.error_status is None:
        return "not queried"
    return "clear" if diagnostic.error_status == 0 else str(diagnostic.error_status)
