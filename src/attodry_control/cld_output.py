"""Narrow, audited LD-only operations; never change TEC or current settings."""
from __future__ import annotations

from copy import deepcopy
import time

from .nkt_config import NktError, number
from .nkt_control import utc_now
from .photoresponse_cld import CldController, CldPoint


def _error(exc):
    return f"{type(exc).__name__}: {exc}"


def protected_ld_off(config, backend, *, record, phase, on_event=None, verified_identity=None):
    """Identify, attempt LD OFF once, and preserve actual readback evidence.

    No health/temperature/connection-diagnostic gate prevents an identified
    protective OFF. A failed initial LD query still permits the one OFF attempt,
    but its error is retained and the operation cannot certify normal success.
    A failure-cleanup caller may provide ``verified_identity`` containing the
    identity and identity_captured_at_utc previously verified on this same open
    backend. It must never come from another session or a historical run.
    This does not acquire normal CldController ownership or change TEC/current.
    """
    if record.get("started_at_utc") is not None:
        raise NktError("Protective LD OFF evidence is single-use")
    record.update(phase=phase, started_at_utc=utc_now(), identity_verified=False,
                  off_attempted=False, off_confirmed=False, errors=[],
                  manual_verification_required=True, before=None, after=None)

    def publish():
        if on_event is not None:
            try:
                on_event(deepcopy(record))
            except (Exception, KeyboardInterrupt) as exc:
                record["errors"].append("audit: " + _error(exc))

    publish()
    try:
        # Real backends validate manufacturer, exact model/serial and firmware.
        if verified_identity is None:
            record["identity"] = backend.identify()
            record["identity_captured_at_utc"] = utc_now()
            record["identity_source"] = "fresh_query"
        else:
            identity = verified_identity["identity"]
            if (len(identity) != 4 or identity[1] != config.expected_model
                    or not verified_identity.get("identity_captured_at_utc")):
                raise NktError("Invalid same-session CLD identity evidence")
            if backend.is_hardware and (identity[0] != "Thorlabs" or identity[2] != config.expected_serial):
                raise NktError("Same-session CLD identity mismatch")
            record["identity"] = deepcopy(identity)
            record["identity_captured_at_utc"] = verified_identity["identity_captured_at_utc"]
            record["identity_source"] = "same_open_session"
        record["identity_verified"] = True
    except (Exception, KeyboardInterrupt) as exc:
        record["errors"].append("identity: " + _error(exc))
    if record["identity_verified"]:
        try:
            initial = backend.get("getLdOutputState")
            if type(initial) is not bool:
                raise NktError("Unknown CLD LD output state")
            record["before"] = {"ld_on": initial, "captured_at_utc": utc_now()}
        except (Exception, KeyboardInterrupt) as exc:
            record["errors"].append("before: " + _error(exc))
        record["off_attempted"] = True
        record["off_attempted_at_utc"] = utc_now()
        publish()
        try:
            backend.set("switchLdOutput", False)
        except (Exception, KeyboardInterrupt) as exc:
            record["errors"].append("off: " + _error(exc))
        try:
            actual = backend.get("getLdOutputState")
            if type(actual) is not bool:
                raise NktError("Unknown CLD LD output state")
            record["after"] = {"ld_on": actual, "captured_at_utc": utc_now()}
            record["off_confirmed"] = actual is False
            if actual:
                raise NktError("CLD LD OFF readback mismatch")
        except (Exception, KeyboardInterrupt) as exc:
            record["errors"].append("after: " + _error(exc))
    record["finished_at_utc"] = utc_now()
    record["manual_verification_required"] = bool(record["errors"] or not record["off_confirmed"])
    publish()
    if record["errors"]:
        record["manual_verification_required"] = True
        raise NktError("Protective LD OFF not certified: " + "; ".join(record["errors"]))
    return record


def enable_existing_ld(config, backend, *, record, on_event=None,
                       clock=time.monotonic, sleep=time.sleep):
    """Qualify existing settings, then enable once without reconfiguring them.

    The caller owns failure protection and transport closure. Normal scan
    begin/finish must not be used here: they configure current and restore TEC.
    """
    controller = CldController(config, backend, clock=clock, sleep=sleep)
    controller.on_event = on_event
    deadline = clock() + config.timeout_s
    initial = controller.capture("cld_manual_on_preflight")
    controller.healthy(initial, require_ld_ready=True)
    if not initial["getTecOutputState"]:
        raise NktError("CLD TEC must already be ON; LD-only control does not enable TEC")
    limit = number(initial["getLdCurrLimit"], "LD current limit", 1e-9, config.max_current_a)
    current = number(initial["getLdCurrSetpoint"], "existing LD current setpoint", 0., limit)
    if initial["getLdOperatingMode"] != 0:
        raise NktError("CLD LD output requires constant-current mode")
    if abs(initial["getTempSetpoint"] - config.temperature_c) > .001:
        raise NktError("Existing TEC temperature target differs from configuration")
    frozen_names = ("getLdOperatingMode", "getTecOperatingMode", "getTempUnit",
                    "getTempProtMode", "getTecOutputState", "getLdOutpAutoOnState",
                    "getLdCurrLimit", "getLdCurrSetpoint", "getTempSetpoint",
                    "getTecCurrLimit", "getTempProtWindow")
    frozen = {name: initial[name] for name in frozen_names}
    record["preserved_settings"] = deepcopy(frozen)
    record["before"] = {"state": deepcopy(initial), "captured_at_utc": utc_now()}
    controller.temperature_protection_mode = initial["getTempProtMode"]
    controller.point = CldPoint(current, config.points[0].wavelength_nm)
    # Ownership is local to this LD-only command. Never call begin/finish.
    controller.owned = True
    stable_since = None
    state = initial
    while clock() <= deadline:
        controller.healthy(state, require_ld_ready=True)
        if any(state[name] != value for name, value in frozen.items()):
            raise NktError("CLD settings changed during LD-only qualification")
        if state["getLdOutputState"] != initial["getLdOutputState"]:
            raise NktError("CLD LD state changed during qualification")
        if controller.stable(state):
            if stable_since is None:
                stable_since = clock()
            if clock() - stable_since >= config.stable_dwell_s:
                break
        else:
            stable_since = None
        remaining = deadline - clock()
        if remaining <= 0:
            raise TimeoutError("CLD existing TEC did not qualify within timeout")
        sleep(min(config.poll_interval_s, remaining))
        state = controller.capture("cld_manual_on_thermal_qualification")
    else:
        raise TimeoutError("CLD existing TEC did not qualify within timeout")
    record["stable_qualified_at_utc"] = utc_now()
    record["already_on"] = initial["getLdOutputState"]
    record["on_attempted"] = not initial["getLdOutputState"]
    if on_event is not None:
        on_event({"phase": "cld_manual_on_qualified", "captured_at_utc": utc_now(),
                  "on_attempted": record["on_attempted"], "preserved_settings": deepcopy(frozen)})
    if initial["getLdOutputState"]:
        result = controller.verify_point(emitting=True, phase="cld_manual_on_already_on")
    else:
        # Reuses the bounded near-zero first-ON voltage recheck; no ON retries.
        result = controller.set_emission(True, deadline=deadline)
    actual = result["source"]
    if any(actual[name] != value for name, value in frozen.items()):
        raise NktError("CLD settings changed after LD ON")
    record["after"] = {"state": deepcopy(actual), "captured_at_utc": utc_now()}
    record["on_confirmed"] = True
    return record
