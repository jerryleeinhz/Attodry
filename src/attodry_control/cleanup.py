from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .interfaces import CryostatController, GateController, LockinController
from .models import CryostatState


class CleanupAction(StrEnum):
    LOCKIN_XX_MINIMUM = "lockin_xx_minimum"
    GATE_TOP_ZERO = "gate_top_zero"
    GATE_BOTTOM_ZERO = "gate_bottom_zero"
    GATE_TOP_DISABLE = "gate_top_disable"
    GATE_BOTTOM_DISABLE = "gate_bottom_disable"
    FIELD_ZERO_REQUEST = "field_zero_request"
    FIELD_ZERO_VERIFY = "field_zero_verify"
    FIELD_HOLD_VERIFY = "field_hold_verify"
    FINAL_STATE_RECORDED = "final_state_recorded"
    DISCONNECT_LOCKIN_XX = "disconnect_lockin_xx"
    DISCONNECT_LOCKIN_XY = "disconnect_lockin_xy"
    DISCONNECT_GATE_TOP = "disconnect_gate_top"
    DISCONNECT_GATE_BOTTOM = "disconnect_gate_bottom"
    DISCONNECT_CRYOSTAT = "disconnect_cryostat"


@dataclass(frozen=True, slots=True)
class CleanupEvent:
    action: CleanupAction
    succeeded: bool
    detail: str


@dataclass(frozen=True, slots=True)
class CleanupReport:
    events: tuple[CleanupEvent, ...]
    field_zero_confirmed: bool
    last_confirmed_cryostat_state: CryostatState
    field_zero_required: bool = True

    @property
    def succeeded(self) -> bool:
        required = {
            CleanupAction.LOCKIN_XX_MINIMUM,
            CleanupAction.GATE_TOP_ZERO,
            CleanupAction.GATE_BOTTOM_ZERO,
            CleanupAction.GATE_TOP_DISABLE,
            CleanupAction.GATE_BOTTOM_DISABLE,
        }
        if self.field_zero_required:
            required |= {
                CleanupAction.FIELD_ZERO_REQUEST,
                CleanupAction.FIELD_ZERO_VERIFY,
            }
        else:
            required.add(CleanupAction.FIELD_HOLD_VERIFY)
        successes = {
            event.action for event in self.events if event.succeeded
        }
        field_safe = self.field_zero_confirmed if self.field_zero_required else True
        return field_safe and required <= successes


def cleanup_after_failure(
    *,
    lockin_xx: LockinController,
    lockin_xy: LockinController,
    gate_top: GateController,
    gate_bottom: GateController,
    cryostat: CryostatController,
    last_confirmed_cryostat_state: CryostatState,
) -> CleanupReport:
    """Run every cleanup step, retain failures, and disconnect only after logging."""

    events: list[CleanupEvent] = []
    _attempt(
        events,
        CleanupAction.LOCKIN_XX_MINIMUM,
        lockin_xx.set_minimum_excitation,
        "lockin_xx excitation set to its minimum",
    )
    top_zero = _zero_gate(events, CleanupAction.GATE_TOP_ZERO, gate_top)
    bottom_zero = _zero_gate(events, CleanupAction.GATE_BOTTOM_ZERO, gate_bottom)
    _disable_gate(events, CleanupAction.GATE_TOP_DISABLE, gate_top, top_zero)
    _disable_gate(events, CleanupAction.GATE_BOTTOM_DISABLE, gate_bottom, bottom_zero)

    _attempt(
        events,
        CleanupAction.FIELD_ZERO_REQUEST,
        cryostat.request_zero_field,
        "field-zero request issued",
    )
    field_zero_confirmed = False
    current_last_confirmed = last_confirmed_cryostat_state
    try:
        state = cryostat.read_state()
        current_last_confirmed = state
        field_zero_confirmed = state.field.magnitude_t <= 1e-9
        detail = (
            "field readback confirmed zero"
            if field_zero_confirmed
            else (
                "field readback is not zero: "
                f"Bx={state.field.bx_t:g} T, Bz={state.field.bz_t:g} T"
            )
        )
        events.append(
            CleanupEvent(
                CleanupAction.FIELD_ZERO_VERIFY,
                field_zero_confirmed,
                detail,
            )
        )
    except BaseException as exc:
        events.append(
            CleanupEvent(
                CleanupAction.FIELD_ZERO_VERIFY,
                False,
                f"field readback failed: {type(exc).__name__}: {exc}",
            )
        )

    events.append(
        CleanupEvent(
            CleanupAction.FINAL_STATE_RECORDED,
            True,
            (
                "recorded confirmed zero field"
                if field_zero_confirmed
                else "recorded last confirmed field; zero remains unconfirmed"
            ),
        )
    )

    for action, device in (
        (CleanupAction.DISCONNECT_LOCKIN_XX, lockin_xx),
        (CleanupAction.DISCONNECT_LOCKIN_XY, lockin_xy),
        (CleanupAction.DISCONNECT_GATE_TOP, gate_top),
        (CleanupAction.DISCONNECT_GATE_BOTTOM, gate_bottom),
        (CleanupAction.DISCONNECT_CRYOSTAT, cryostat),
    ):
        _attempt(events, action, device.close, "device disconnected")

    return CleanupReport(
        events=tuple(events),
        field_zero_confirmed=field_zero_confirmed,
        last_confirmed_cryostat_state=current_last_confirmed,
    )


def cleanup_after_normal_completion(
    *,
    lockin_xx: LockinController,
    lockin_xy: LockinController,
    gate_top: GateController,
    gate_bottom: GateController,
    cryostat: CryostatController,
    last_confirmed_cryostat_state: CryostatState,
    zero_field: bool,
) -> CleanupReport:
    """Make electrical outputs safe; either verify held field or request zero."""

    if zero_field:
        return cleanup_after_failure(
            lockin_xx=lockin_xx,
            lockin_xy=lockin_xy,
            gate_top=gate_top,
            gate_bottom=gate_bottom,
            cryostat=cryostat,
            last_confirmed_cryostat_state=last_confirmed_cryostat_state,
        )

    events: list[CleanupEvent] = []
    _attempt(
        events,
        CleanupAction.LOCKIN_XX_MINIMUM,
        lockin_xx.set_minimum_excitation,
        "lockin_xx excitation set to its minimum",
    )
    top_zero = _zero_gate(events, CleanupAction.GATE_TOP_ZERO, gate_top)
    bottom_zero = _zero_gate(events, CleanupAction.GATE_BOTTOM_ZERO, gate_bottom)
    _disable_gate(events, CleanupAction.GATE_TOP_DISABLE, gate_top, top_zero)
    _disable_gate(events, CleanupAction.GATE_BOTTOM_DISABLE, gate_bottom, bottom_zero)

    current_last_confirmed = last_confirmed_cryostat_state
    try:
        current_last_confirmed = cryostat.read_state()
        events.append(
            CleanupEvent(
                CleanupAction.FIELD_HOLD_VERIFY,
                True,
                "normal-end hold policy; current field readback recorded",
            )
        )
    except BaseException as exc:
        events.append(
            CleanupEvent(
                CleanupAction.FIELD_HOLD_VERIFY,
                False,
                "field hold readback failed; retaining last confirmed state: "
                f"{type(exc).__name__}: {exc}",
            )
        )
    events.append(
        CleanupEvent(
            CleanupAction.FINAL_STATE_RECORDED,
            True,
            "recorded last confirmed field under normal-end hold policy",
        )
    )
    for action, device in (
        (CleanupAction.DISCONNECT_LOCKIN_XX, lockin_xx),
        (CleanupAction.DISCONNECT_LOCKIN_XY, lockin_xy),
        (CleanupAction.DISCONNECT_GATE_TOP, gate_top),
        (CleanupAction.DISCONNECT_GATE_BOTTOM, gate_bottom),
        (CleanupAction.DISCONNECT_CRYOSTAT, cryostat),
    ):
        _attempt(events, action, device.close, "device disconnected")
    return CleanupReport(
        events=tuple(events),
        field_zero_confirmed=False,
        last_confirmed_cryostat_state=current_last_confirmed,
        field_zero_required=False,
    )


def _zero_gate(
    events: list[CleanupEvent], action: CleanupAction, gate: GateController
) -> bool:
    try:
        gate.set_voltage(0.0)
        state = gate.read_state()
        confirmed = abs(state.voltage_read_v) <= 1e-9
        events.append(
            CleanupEvent(
                action,
                confirmed,
                "gate readback confirmed zero"
                if confirmed
                else f"gate readback remains {state.voltage_read_v:g} V",
            )
        )
        return confirmed
    except BaseException as exc:
        events.append(
            CleanupEvent(action, False, f"gate zero failed: {type(exc).__name__}: {exc}")
        )
        return False


def _disable_gate(
    events: list[CleanupEvent],
    action: CleanupAction,
    gate: GateController,
    zero_confirmed: bool,
) -> None:
    try:
        gate.disable_output()
        state = gate.read_state()
        disabled = not state.output_enabled
        detail = "gate output disabled"
        if not zero_confirmed:
            detail += "; voltage zero was not confirmed"
        events.append(CleanupEvent(action, disabled, detail))
    except BaseException as exc:
        events.append(
            CleanupEvent(
                action, False, f"gate disable failed: {type(exc).__name__}: {exc}"
            )
        )


def _attempt(
    events: list[CleanupEvent],
    action: CleanupAction,
    operation: object,
    success_detail: str,
) -> None:
    try:
        operation()
        events.append(CleanupEvent(action, True, success_detail))
    except BaseException as exc:
        events.append(
            CleanupEvent(action, False, f"{type(exc).__name__}: {exc}")
        )
