"""Single-owner optical rotator lifecycle; no driver or DLL loads on import."""
from __future__ import annotations

from copy import deepcopy
import time

from .combination_store import utc_now
from .rotator_config import RotatorError


class RotatorPointSession:
    def __init__(self, config, *, device_factory=None, event_sink=None,
                 clock=time.monotonic, sleep=time.sleep):
        config.validate()
        self.config = config
        self.device_factory, self.event_sink = device_factory, event_sink
        self.clock, self.sleep = clock, sleep
        self.devices = {}
        self.last_confirmed = {}
        self.targets = {}
        self.preflight_complete = self.begun = False
        self._opening_attempted = False
        self._stationary_since = {}
        self._prepared = set()
        self._motion_owned = set()
        self._motion_errors = []
        self._stop_attempted = set()

    def _event(self, name, **payload):
        if self.event_sink:
            self.event_sink(name, deepcopy(payload))

    def _guard(self, guard):
        if guard is not None:
            guard()

    def _new_device(self, config):
        if self.device_factory:
            return self.device_factory(config)
        from .thorlabs_kdc101 import Kdc101Device
        return Kdc101Device(config, on_event=lambda raw: self._event("rotator_raw", **raw))

    def _validate(self, config, state, *, ready=False, allow_fault=False):
        if not isinstance(state, dict):
            raise RotatorError(config.role + ": incomplete/unknown controller state")
        expected = {"serial_number": config.serial_number, "controller_model": config.controller,
                    "stage_model": config.stage_model}
        if any(state.get(key) != value for key, value in expected.items()):
            raise RotatorError(config.role + ": controller or stage identity mismatch")
        for key in ("homed", "moving", "enabled", "fault"):
            if type(state.get(key)) is not bool:
                raise RotatorError(config.role + ": unknown " + key + " status")
        angle = state.get("angle_deg")
        if (angle is None and config.role not in self._prepared and
                state.get("unit_calibration_verified") is False):
            if state["fault"] and not allow_fault:
                raise RotatorError(config.role + ": controller fault")
            return state
        if isinstance(angle, bool) or not isinstance(angle, (int, float)):
            raise RotatorError(config.role + ": unknown mechanical angle")
        import math
        if not math.isfinite(angle):
            raise RotatorError(config.role + ": non-finite mechanical angle")
        if state["homed"]:
            config.validate_angle(angle)
        if state["fault"] and not allow_fault:
            raise RotatorError(config.role + ": controller fault")
        if ready and (state["moving"] or not state["homed"] or not state["enabled"]):
            raise RotatorError(config.role + ": controller is not ready/homed/stationary")
        return state

    def _read(self, config, *, ready=False, allow_fault=False):
        state = self.devices[config.role].read_state()
        self._event("rotator_state", role=config.role, captured_at_utc=utc_now(), state=state)
        self._validate(config, state, ready=ready, allow_fault=allow_fault)
        self.last_confirmed[config.role] = deepcopy(state)
        return state

    def preflight(self):
        if self._opening_attempted:
            raise RotatorError("Rotator preflight may only run once per session")
        self._opening_attempted = True
        # Every device is opened/read before any prepare/home/move write.
        for config in self.config.devices:
            device = self._new_device(config)
            self.devices[config.role] = device
            device.open()
            state = self._read(config)
            if state["moving"]:
                raise RotatorError(config.role + ": already moving during preflight")
            if config.home_policy == "require_homed" and not state["homed"]:
                raise RotatorError(config.role + ": requires a previously established Home reference")
        self.preflight_complete = True
        return deepcopy(self.last_confirmed)

    open = preflight

    def _wait(self, config, target, deadline, guard, *, stable_since=None):
        while True:
            self._guard(guard)
            state = self._read(config)
            now = self.clock()
            if now > deadline:
                raise RotatorError(config.role + ": motion/settling timeout")
            if not state["enabled"]:
                raise RotatorError(config.role + ": motor became disabled")
            good = (state["homed"] and not state["moving"] and
                    abs(state["angle_deg"] - target) <= config.position_tolerance_deg)
            if good:
                stable_since = now if stable_since is None else stable_since
                if now - stable_since + 1e-12 >= config.settle_s:
                    self._guard(guard)
                    if self.clock() > deadline:
                        raise RotatorError(config.role + ": deadline exceeded during final guard")
                    if guard is not None:
                        state = self._read(config)
                        if self.clock() > deadline:
                            raise RotatorError(config.role + ": deadline exceeded during final readback")
                        if not (state["homed"] and state["enabled"] and not state["moving"] and
                                abs(state["angle_deg"] - target) <= config.position_tolerance_deg):
                            stable_since = None
                            continue
                    self._stationary_since[config.role] = stable_since
                    return state
            else:
                stable_since = None
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise RotatorError(config.role + ": motion/settling timeout")
            wait = min(config.poll_interval_s, remaining)
            if stable_since is not None:
                wait = min(wait, max(0.0, config.settle_s - (self.clock() - stable_since)))
            if wait > 0:
                self.sleep(wait)

    def _stop_owned(self, config):
        """One best-effort normal stop after our failed motion; never retry/home."""
        if config.role not in self._motion_owned:
            return
        self._motion_owned.discard(config.role)
        self._stop_attempted.add(config.role)
        outcome = {"role": config.role, "requested": True, "confirmed_stationary": False}
        try:
            self.devices[config.role].stop()
            deadline = self.clock() + config.move_timeout_s
            while True:
                state = self._read(config, allow_fault=True)
                if self.clock() > deadline:
                    raise RotatorError(config.role + ": normal stop verification timeout")
                if not state["moving"]:
                    outcome["confirmed_stationary"] = True
                    break
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise RotatorError(config.role + ": normal stop verification timeout")
                self.sleep(min(config.poll_interval_s, remaining))
        except BaseException as exc:
            outcome["error"] = str(exc)
            self._motion_errors.append(config.role + ": normal stop not verified: " + str(exc))
        try:
            self._event("rotator_failed_motion_stop", **outcome)
        except Exception as exc:
            self._motion_errors.append(config.role + ": stop audit failed: " + str(exc))

    def _motion(self, config, target, timeout, guard, command):
        deadline = self.clock() + timeout
        self._motion_owned.add(config.role)
        try:
            command()
            result = self._wait(config, target, deadline, guard)
        except BaseException as exc:
            self._motion_errors.append(config.role + ": failed motion: " + str(exc))
            self._stop_owned(config)
            raise
        self._motion_owned.discard(config.role)
        return result

    def begin(self, guard=None):
        if not self.preflight_complete or self.begun:
            raise RotatorError("Rotator begin requires one successful preflight")
        # Recheck all identities and Home policy before the first settings write.
        states = self.observe()
        for config in self.config.devices:
            state = states[config.role]
            if state["moving"]:
                raise RotatorError(config.role + ": unexpected motion before begin")
            if config.home_policy == "require_homed" and not state["homed"]:
                raise RotatorError(config.role + ": Home reference lost before begin")
        changed = False
        for config in self.config.devices:
            self._guard(guard)
            fresh = self._read(config)
            if fresh["moving"]:
                raise RotatorError(config.role + ": unexpected motion before prepare")
            if config.home_policy == "require_homed" and not fresh["homed"]:
                raise RotatorError(config.role + ": Home reference lost before prepare")
            self.devices[config.role].prepare()
            self._prepared.add(config.role)
            self._guard(guard)
            state = self._read(config)
            if state["moving"] or not state["enabled"]:
                raise RotatorError(config.role + ": unexpected motion or disabled after prepare")
            if state["homed"]:
                self._stationary_since[config.role] = self.clock()
            home = config.home_policy == "always" or not state["homed"]
            if home:
                if config.home_policy == "require_homed":
                    raise RotatorError(config.role + ": Home reference lost during prepare")
                config.validate_angle(0.0)
                self._guard(guard)
                self._event("rotator_home_started", role=config.role)
                self._motion(config, 0.0, config.home_timeout_s, guard,
                             self.devices[config.role].home)
                self._event("rotator_home_completed", role=config.role,
                            state=self.last_confirmed[config.role])
                changed = True
        self.begun = True
        return {"changed": changed, "actual": self.actual(), "states": deepcopy(self.last_confirmed)}

    def set_point(self, values, guard=None):
        if not self.begun:
            raise RotatorError("Rotator point requires begin")
        if not isinstance(values, dict) or set(values) != set(self.config.active_keys):
            raise RotatorError("Rotator point requires exactly the enabled angle keys")
        targets = {config.role: config.validate_angle(values[config.angle_key])
                   for config in self.config.devices}
        self._guard(guard)
        states = self.observe()
        for config in self.config.devices:
            self._validate(config, states[config.role], ready=True)
        changed = False
        for config in self.config.devices:
            self._guard(guard)
            target = targets[config.role]
            state = self._read(config, ready=True)
            if abs(state["angle_deg"] - target) > config.position_tolerance_deg:
                self._guard(guard)
                self._event("rotator_move_started", role=config.role, target_angle_deg=target)
                self._motion(config, target, config.move_timeout_s, guard,
                             lambda: self.devices[config.role].move_to(target))
                self._event("rotator_move_completed", role=config.role, target_angle_deg=target,
                            state=self.last_confirmed[config.role])
                changed = True
            else:
                self._wait(config, target, self.clock() + config.move_timeout_s, guard,
                           stable_since=self._stationary_since.get(config.role))
        self.targets = targets
        self._guard(guard)
        actual = self.verify()
        return {"changed": changed, "actual": actual, "states": deepcopy(self.last_confirmed)}

    def observe(self):
        if not self.preflight_complete:
            raise RotatorError("Rotator observation requires successful preflight")
        return {config.role: deepcopy(self._read(config)) for config in self.config.devices}

    def actual(self):
        """Return fresh mechanical coordinates, never a cached target or angle."""
        if not self.begun:
            raise RotatorError("Rotator actual coordinates require begin")
        states = self.observe()
        return {config.angle_key: states[config.role]["angle_deg"] for config in self.config.devices}

    def verify(self):
        if not self.begun:
            raise RotatorError("Rotator verification requires begin")
        states = self.observe()
        for config in self.config.devices:
            state = self._validate(config, states[config.role], ready=True)
            if config.role in self.targets and abs(state["angle_deg"] - self.targets[config.role]) > config.position_tolerance_deg:
                raise RotatorError(config.role + ": actual angle drifted outside tolerance")
        return {config.angle_key: states[config.role]["angle_deg"] for config in self.config.devices}

    def cleanup(self, failed=False):
        """Preserve angles; stop only confirmed moving owned motors, no Home/disable."""
        errors, states = list(self._motion_errors), {}
        for config in reversed(self.config.devices):
            if config.role not in self.devices:
                continue
            try:
                state = self._read(config, allow_fault=True)
                if state["moving"]:
                    if config.role in self._stop_attempted:
                        raise RotatorError(config.role + ": motion remains unknown after one normal stop")
                    self._stop_attempted.add(config.role)
                    self.devices[config.role].stop()
                    deadline = self.clock() + config.move_timeout_s
                    while True:
                        state = self._read(config, allow_fault=True)
                        if self.clock() > deadline:
                            raise RotatorError(config.role + ": stop timeout")
                        if not state["moving"]:
                            break
                        remaining = deadline - self.clock()
                        if remaining <= 0:
                            raise RotatorError(config.role + ": stop timeout")
                        self.sleep(min(config.poll_interval_s, remaining))
                states[config.role] = deepcopy(state)
                if state["angle_deg"] is None:
                    errors.append(config.role + ": mechanical coordinate remains uncalibrated")
                if not state["homed"]:
                    errors.append(config.role + ": mechanical Home reference is not established")
                if state["fault"]:
                    errors.append(config.role + ": controller fault remains")
            except Exception as exc:
                errors.append(config.role + ": " + str(exc))
                self._stop_owned(config)
        errors.extend(error for error in self._motion_errors if error not in errors)
        result = {"verified": not errors and len(states) == len(self.devices),
                  "failed": bool(failed), "finish": "preserve", "states": states,
                  "errors": errors, "last_confirmed": deepcopy(self.last_confirmed)}
        return result

    def close(self):
        errors = []
        for role, device in reversed(tuple(self.devices.items())):
            try:
                device.close()
            except Exception as exc:
                errors.append(role + ": " + str(exc))
        self.devices.clear()
        self._prepared.clear()
        self.preflight_complete = self.begun = False
        return errors
