"""Finite, guarded XYZ point motion; no hardware import or device I/O on import."""
from __future__ import annotations

from copy import deepcopy
import math
import time

from .combination_store import utc_now
from .positioner_config import PositionerError


_ROLES = ("x", "y", "z")
_KEYS = tuple("positioner_" + role + "_um" for role in _ROLES)
_BOOLS = ("sensor_connected", "output_enabled", "moving", "target_reached",
          "eot_forward", "eot_backward", "sensor_error")


class PositionerPointSession:
    """A run freezes its origin and validates every path before output writes.

    All point I/O, guards and complete stable windows share one monotonic point
    deadline. There is no retry after an uncertain command. Cleanup uses an
    independent one-shot normal stop only for this session's automatic motion;
    disconnect, stop and audit failures remain separate evidence.
    """
    def __init__(self, config, *, device_factory=None, event_sink=None,
                 clock=time.monotonic, sleep=time.sleep):
        config.validate()
        self.config = config
        self.device_factory, self.event_sink = device_factory, event_sink
        self.clock, self.sleep = clock, sleep
        self.device = None
        self.preflight_complete = self.begun = self._opening_attempted = False
        self.last_confirmed = None
        self.origin, self.targets = None, None
        self._failed = self._closed = False
        self._motion_owned, self._stop_attempted = set(), set()
        self._motion_errors, self._audit_errors = [], []
        self._close_errors = []
        self._unknown = False

    def _event(self, name, *, independent=False, **payload):
        if self.event_sink is not None:
            try:
                self.event_sink(name, deepcopy(payload))
            except Exception as exc:
                if not independent:
                    raise
                self._audit_errors.append(name + ": " + str(exc))

    def _guard(self, guard, deadline=None):
        if guard is None:
            if not self.config.external_light_off_confirmed:
                raise PositionerError("Positioner motion requires a fresh optical OFF guard or explicit external-light OFF declaration")
        else:
            guard()
        self._deadline(deadline)

    def _deadline(self, deadline):
        if deadline is not None and self.clock() > deadline:
            raise PositionerError("Positioner shared motion/settling deadline exceeded")

    def _new_device(self):
        if self.device_factory:
            return self.device_factory(self.config)
        from .anc350 import Anc350Device
        return Anc350Device(self.config, on_event=lambda raw: self._event("positioner_raw", **raw))

    def _validate(self, state, *, ready=False, allow_fault=False):
        if not isinstance(state, dict) or state.get("serial_number") != self.config.controller_serial_number:
            raise PositionerError("Positioner controller identity is unknown/changed")
        if (state.get("controller_model") != "ANC350 RES" or
                state.get("firmware_raw") != self.config.expected_firmware_raw):
            raise PositionerError("Positioner controller model/firmware differs from configuration")
        axes = state.get("axes")
        if not isinstance(axes, dict) or set(axes) != set(_ROLES):
            raise PositionerError("Positioner requires complete XYZ readback")
        for axis in self.config.axes:
            row = axes[axis.role]
            if not isinstance(row, dict) or any(type(row.get(key)) is not bool for key in _BOOLS):
                raise PositionerError(axis.role + ": incomplete/unknown positioner boolean state")
            if (row.get("axis_index") != axis.index or
                    row.get("actuator_name") != axis.expected_actuator_name or
                    row.get("lut_name") != axis.expected_lut_name):
                raise PositionerError(axis.role + ": positioner axis identity changed")
            for key in ("position_um", "amplitude_v", "frequency_hz"):
                value = row.get(key)
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise PositionerError(axis.role + ": unknown/nonfinite " + key)
            if not axis.min_position_um <= row["position_um"] <= axis.max_position_um:
                raise PositionerError(axis.role + ": actual position crossed configured limits")
            if (not math.isclose(row["amplitude_v"], axis.amplitude_v, rel_tol=1e-9, abs_tol=1e-9) or
                    not math.isclose(row["frequency_hz"], axis.frequency_hz, rel_tol=1e-9, abs_tol=1e-9)):
                raise PositionerError(axis.role + ": drive amplitude/frequency changed")
            if not allow_fault and (not row["sensor_connected"] or row["sensor_error"] or
                                    row["eot_forward"] or row["eot_backward"]):
                raise PositionerError(axis.role + ": position sensor/EOT fault")
            if ready and (row["moving"] or not row["output_enabled"]):
                raise PositionerError(axis.role + ": axis is moving or output disabled")
        return state

    def _read(self, *, ready=False, allow_fault=False, independent=False, deadline=None):
        try:
            reader = getattr(self.device, "read_state_independent", self.device.read_state) if independent else self.device.read_state
            state = reader()
            self._deadline(deadline)
            self._validate(state, ready=ready, allow_fault=allow_fault)
        except BaseException:
            self._unknown = True
            raise
        # Confirmed state is retained even when durable audit subsequently fails.
        self.last_confirmed = deepcopy(state)
        self._unknown = False
        self._event("positioner_state", independent=independent,
                    captured_at_utc=utc_now(), state=state)
        return state

    @staticmethod
    def _actual(state):
        return {"positioner_" + role + "_um": state["axes"][role]["position_um"] for role in _ROLES}

    def _focus_error(self, actual):
        if self.config.focus_plane is None:
            return None
        return self.config.focus_plane.focus_error(*(actual[key] for key in _KEYS))

    def _good(self, state, targets, *, require_output, focus):
        actual = self._actual(state)
        if any(row["moving"] or (require_output and not row["output_enabled"])
               for row in state["axes"].values()):
            return False
        if targets is not None and any(
                abs(actual["positioner_" + axis.role + "_um"] - targets["positioner_" + axis.role + "_um"])
                > axis.position_tolerance_um for axis in self.config.axes):
            return False
        if focus and self.config.focus_plane is not None:
            error = self._focus_error(actual)  # Also rejects actual XY outside the convex hull.
            if abs(error) > self.config.focus_tolerance_um:
                return False
        return True

    def _wait(self, targets, deadline, guard, *, require_output=True, focus=True):
        stable_since, window = None, []
        while True:
            self._guard(guard, deadline)
            state = self._read(deadline=deadline)
            now, actual = self.clock(), self._actual(state)
            good = self._good(state, targets, require_output=require_output, focus=focus)
            if good:
                if stable_since is None:
                    stable_since, window = now, []
                window.append(actual)
                # In addition to target error, enforce a complete stable range.
                if any(max(row[key] for row in window) - min(row[key] for row in window)
                       > axis.position_tolerance_um
                       for axis, key in zip(self.config.axes, _KEYS)):
                    stable_since, window = now, [actual]
                if now - stable_since + 1e-12 >= self.config.settle_s:
                    self._guard(guard, deadline)
                    # If the guard performs I/O, qualify a fresh final read afterward.
                    final = self._read(deadline=deadline)
                    final_actual = self._actual(final)
                    if (self._good(final, targets, require_output=require_output, focus=focus) and
                            all(max([row[key] for row in window] + [final_actual[key]]) -
                                min([row[key] for row in window] + [final_actual[key]])
                                <= axis.position_tolerance_um
                                for axis, key in zip(self.config.axes, _KEYS))):
                        return final
                    stable_since, window = None, []
            else:
                stable_since, window = None, []
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise PositionerError("Positioner motion/stability timeout")
            delay = min(self.config.poll_interval_s, remaining)
            if stable_since is not None:
                delay = min(delay, max(0., self.config.settle_s - (self.clock() - stable_since)))
            if delay > 0:
                self.sleep(delay)

    def preflight(self):
        if self._opening_attempted:
            raise PositionerError("Positioner preflight is one attempt per session")
        self._opening_attempted = True
        self.device = self._new_device()
        try:
            self.device.open()
            state = self._read()
            if any(row["moving"] or row["output_enabled"] for row in state["axes"].values()):
                raise PositionerError("Positioner preflight requires output OFF and stationary axes; operator must release previous feedback/output")
            self.preflight_complete = True
            return deepcopy(state)
        except BaseException as primary:
            self._failed = True
            errors = self.close()
            for error in errors:
                primary.add_note(error)
            raise

    open = preflight

    def _result(self, *, changed=False):
        plane = self.config.focus_plane
        snapshot = plane.snapshot() if plane is not None else None
        plane_id = snapshot.get("calibration_sha256") if snapshot else None
        actual = self._actual(self.last_confirmed)
        return {"changed": changed, "actual": actual, "states": deepcopy(self.last_confirmed),
                "origin": deepcopy(self.origin), "targets": deepcopy(self.targets),
                "target": deepcopy(self.targets), "coordinate_frame": self.config.coordinate_frame,
                "mode": self.config.mode, "z_mode": self.config.z_mode,
                "focus_plane_id": plane_id, "focus_error_um": self._focus_error(actual) if self.targets is not None else None}

    def begin(self, guard=None, *, wrap_required=False):
        if not self.preflight_complete or self.begun or self._failed:
            raise PositionerError("Positioner begin requires one successful preflight")
        deadline = self.clock() + self.config.move_timeout_s
        try:
            state = self._wait(None, deadline, guard, require_output=False, focus=False)
            self.origin = self._actual(state)
            targets = self.config.resolve_all_points(self.origin)
            self.config.validate_path(self.origin, targets)
            if wrap_required and targets:
                self.config.validate_path(targets[-1], [targets[0]])
            if self.config.finish == "return_start" and targets:
                self.config.validate_path(targets[-1], [self.origin])
            self._event("positioner_origin_frozen", origin=self.origin,
                        absolute_targets=targets, coordinate_frame=self.config.coordinate_frame)
            # All axes, origin and complete outgoing/return paths passed before
            # the first setting/output command. No calibration/drive repair.
            for axis in self.config.axes:
                self._guard(guard, deadline)
                fresh = self._read(deadline=deadline)
                if any(row["moving"] for row in fresh["axes"].values()):
                    raise PositionerError("Unexpected motion before output preparation")
                self._motion_owned.add(axis.role)
                self.device.prepare_axis(axis.role)
                self._deadline(deadline)
                state = self._read(deadline=deadline)
                if not state["axes"][axis.role]["output_enabled"]:
                    raise PositionerError(axis.role + ": output enable not confirmed")
            self._wait(self.origin, deadline, guard, focus=False)
            self.begun = True
            return self._result(changed=False)
        except BaseException:
            self._failed = True
            for role in reversed(self.config.move_order):
                self._stop_owned(role)
            raise

    def _stop_owned(self, role):
        if role not in self._motion_owned or role in self._stop_attempted:
            return
        self._stop_attempted.add(role)
        outcome = {"role": role, "requested": True, "confirmed_stationary": False}
        try:
            self.device.stop(role)
            deadline = self.clock() + self.config.move_timeout_s
            while True:
                # Audit failure cannot suppress post-stop state readback.
                state = self._read(allow_fault=True, independent=True, deadline=deadline)
                if not state["axes"][role]["moving"]:
                    outcome["confirmed_stationary"] = True
                    break
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise PositionerError(role + ": normal stop verification timeout")
                self.sleep(min(self.config.poll_interval_s, remaining))
        except BaseException as exc:
            self._motion_errors.append(role + ": normal stop unverified: " + str(exc))
            outcome["error"] = str(exc)
        self._motion_owned.discard(role)
        self._event("positioner_failed_motion_stop", independent=True, **outcome)

    def _move_targets(self, targets, guard, *, focus=True):
        deadline = self.clock() + self.config.move_timeout_s
        self._guard(guard, deadline)
        state = self._read(ready=True, deadline=deadline)
        path_start = self._actual(state)
        path = self.config.validate_path(path_start, [targets])
        changed = False
        try:
            previous = path_start
            for step in path:
                changed_roles = [role for role in self.config.move_order
                                 if step["positioner_" + role + "_um"] != previous["positioner_" + role + "_um"]]
                if len(changed_roles) != 1:
                    raise PositionerError("Positioner path step must change exactly one axis")
                for role in changed_roles:
                    key = "positioner_" + role + "_um"
                    # Each step is a full XYZ snapshot and changes one axis.
                    current = self._actual(self.last_confirmed)
                    if step[key] == current[key]:
                        continue
                    axis = next(axis for axis in self.config.axes if axis.role == role)
                    if abs(step[key] - current[key]) <= axis.position_tolerance_um:
                        continue
                    self._guard(guard, deadline)
                    self._motion_owned.add(role)
                    self._event("positioner_move_started", role=role, target_um=step[key], absolute_target=step)
                    self.device.move_to(role, step[key])
                    self._deadline(deadline)
                    self._wait(step, deadline, guard, focus=False)
                    self._event("positioner_move_completed", role=role,
                                target_um=step[key], actual=self._actual(self.last_confirmed))
                    changed = True
                previous = step
            self._wait(targets, deadline, guard, focus=focus)
            self.targets = deepcopy(targets)
            return changed
        except BaseException as exc:
            self._failed = True
            self._motion_errors.append("Positioner primary motion error: " + str(exc))
            for role in reversed(self.config.move_order):
                self._stop_owned(role)
            raise

    def set_point(self, values, guard=None):
        if not self.begun or self._failed:
            raise PositionerError("Positioner point requires begun, known session")
        try:
            targets = self.config.resolve_point(values, self.origin)
            changed = self._move_targets(targets, guard)
            return self._result(changed=changed)
        except BaseException:
            self._failed = True
            raise

    def observe(self):
        if not self.preflight_complete:
            raise PositionerError("Positioner observation requires preflight")
        return deepcopy(self._read())

    def actual(self):
        if not self.begun:
            raise PositionerError("Positioner actual requires begin")
        return self._actual(self._read())

    def verify(self):
        if not self.begun or self.targets is None or self._failed:
            raise PositionerError("Positioner verify requires a qualified target")
        try:
            state = self._read(ready=True)
            if not self._good(state, self.targets, require_output=True, focus=True):
                raise PositionerError("Positioner actual/focus drifted outside qualification")
            return self._actual(state)
        except BaseException:
            self._failed = True
            raise

    def cleanup(self, failed=False, guard=None):
        errors = list(self._motion_errors)
        if (not failed and not self._failed and self.begun and self.origin is not None
                and self.config.finish == "return_start"):
            try:
                self._move_targets(self.origin, guard, focus=False)
            except BaseException as exc:
                errors.append("Positioner return_start failed: " + str(exc))
        if failed or self._failed:
            for role in reversed(self.config.move_order):
                self._stop_owned(role)
        state = None
        if self.device is not None and self.preflight_complete:
            try:
                state = self._read(allow_fault=True, independent=True)
                for role, row in state["axes"].items():
                    if row["moving"] or not row["sensor_connected"] or row["sensor_error"] or row["eot_forward"] or row["eot_backward"]:
                        errors.append(role + ": final positioner state requires manual review")
                    if self.begun and not row["output_enabled"]:
                        errors.append(role + ": final holding output is disabled")
                if not failed and not self._failed and self.begun and self.targets is not None:
                    focus = self.config.finish != "return_start"
                    if not self._good(state, self.targets, require_output=True, focus=focus):
                        errors.append("Positioner final target/focus qualification was lost")
            except BaseException as exc:
                errors.append("Positioner final state unknown: " + str(exc))
        driver_audit = ["Positioner raw audit: " + str(error) for error in getattr(self.device, "audit_errors", ())]
        for message in self._motion_errors + self._audit_errors + driver_audit + self._close_errors:
            if message not in errors:
                errors.append(message)
        return {"verified": state is not None and not errors, "failed": bool(failed or self._failed),
                "finish": self.config.finish, "states": state, "errors": errors,
                "manual_review": bool(errors), "state_current": state is not None,
                "last_confirmed": deepcopy(self.last_confirmed), "position_unknown": self._unknown}

    def close(self):
        errors = self._close_errors
        if not self._closed and self.device is not None:
            self._closed = True
            try:
                result = self.device.close()
                if result:
                    errors.extend("Positioner disconnect: " + str(error) for error in result)
            except BaseException as exc:
                errors.append("Positioner disconnect failed: " + str(exc))
            errors.extend("Positioner raw audit: " + str(error) for error in getattr(self.device, "audit_errors", ()))
        self.preflight_complete = self.begun = False
        return list(errors)
