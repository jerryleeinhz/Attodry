"""One optics owner for modular Combination, using the current light/power engines.

This is a lifecycle adapter, not another feedback or scan loop.  The coordinator
owns whole-window rejection, finite recovery and dark-coordinate matching.
Construction performs no I/O; injected controllers retain their original safety,
authorization, serial ownership and raw-audit rules.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import time

from .nkt_config import NktError, number
from .nkt_control import utc_now
from .power_feedback import actuator_domain


@dataclass(frozen=True)
class OpticsPoint:
    laser_point: object
    optical_state: str = "light"
    target_power_w: float | None = None
    power_index: int = 0
    scan_identity: object = None
    index: int = 0

    def validate(self, laser, power):
        if self.optical_state not in ("dark", "light"):
            raise NktError("Optics state must be dark or light")
        if self.laser_point not in laser.config.points:
            raise NktError("Optics point is outside the configured laser plan")
        if type(self.index) is not int or self.index < 0:
            raise NktError("Optics index must be a nonnegative integer")
        if type(self.power_index) is not int or self.power_index < 0:
            raise NktError("Power index must be a nonnegative integer")
        if power is None:
            if self.target_power_w is not None:
                raise NktError("Target W requires a calibrated power meter and feedback")
        elif self.optical_state == "dark":
            if self.target_power_w is not None or self.power_index != 0 or power.config.power_points is None:
                raise NktError("Dark power point requires dark-first feedback and no target W")
        else:
            feedback = power.config.for_target(self.power_index)
            if self.target_power_w != feedback.target_power_w:
                raise NktError("Optics target does not match the archived power index")


class _GuardedLaser:
    """The existing acquisition window can use this facade without new PM reads."""
    def __init__(self, owner):
        self.owner = owner

    def __getattr__(self, name):
        return getattr(self.owner.laser, name)

    def verify_point(self, *, emitting, phase="formal"):
        return self.owner._verify(emitting=emitting, phase=phase)


class OpticsSession:
    def __init__(self, laser, *, power=None, pem=None, peak_retardance_waves=None,
                 settle_s=0., dark_recovery_s=None, clock=time.perf_counter,
                 sleep=time.sleep, on_event=None):
        laser.config.validate()
        if power is not None and power.laser is not laser:
            raise NktError("Optics power session must own the same laser controller")
        if pem is not None:
            pem.config.validate()
            if bool(pem.is_hardware) != bool(laser.backend.is_hardware):
                raise NktError("Optics PEM/laser hardware mode mismatch")
            for point in laser.config.points:
                pem.config.target(point.wavelength_nm, peak_retardance_waves)
            if (pem.is_hardware and getattr(laser.config, "port", "").upper()
                    == pem.config.port.upper()):
                raise NktError("PEM and laser cannot own the same serial port")
        elif peak_retardance_waves is not None:
            raise NktError("PEM retardance requires a selected PEM")
        number(settle_s, "optics settle_s", 0, 86400)
        if dark_recovery_s is not None:
            number(dark_recovery_s, "dark_recovery_s", 0, 86400)
        self.laser, self.power, self.pem = laser, power, pem
        self.peak_retardance_waves = peak_retardance_waves
        self.settle_s, self.dark_recovery_s = settle_s, dark_recovery_s
        self.clock, self.sleep, self.on_event = clock, sleep, on_event
        self.guarded_laser = _GuardedLaser(self)
        self.opened = self.closed = self.qualified = self.sample_active = False
        self._opening_attempted = False
        self.point = self.row = None
        self.last_change_monotonic_s = None
        self._previous = self._pem_key = None
        self._suspended = True
        self._cleanup = None
        self.record = {"points": [], "events": [], "preflight": None,
                       "last_confirmed_state": None, "state_current": False}

    def _event(self, phase, **values):
        event = {"phase": phase, "captured_at_utc": utc_now(), **deepcopy(values)}
        self.record["events"].append(event)
        if self.on_event:
            self.on_event(event)

    def _point_event(self, phase):
        # Point evidence already lives once in record.points. Progress messages
        # identify it rather than repeatedly embedding every PM transcript.
        self._event(phase, point_record_index=len(self.record["points"])-1,
            optical_index=self.point.index, optical_state=self.point.optical_state,
            target_power_w=self.point.target_power_w, qualified=self.qualified,
            evidence_reference="optics_audit.points[point_record_index]")

    def _require_open(self):
        if not self.opened or self.closed:
            raise NktError("Optics session is not open")

    def open(self, *, authorize_writes=False, confirm_manual_route=False):
        if self._opening_attempted:
            raise NktError("Optics sessions are single-use")
        # Reject every selected component before the first read, including PEM.
        if self.laser.backend.is_hardware:
            if authorize_writes is not True or confirm_manual_route is not True:
                raise NktError("Optics requires authorized writes and confirmed optical route")
            self.laser.config.require_hardware(writes=True)
            if self.pem:
                self.pem.config.require_hardware(writes=True)
                if self.pem.authorize_writes is not True:
                    raise NktError("PEM setting authorization required")
        self._opening_attempted = True
        self.laser.begin(authorize_writes=authorize_writes,
                         confirm_manual_route=confirm_manual_route)
        evidence = {"laser": deepcopy(self.laser.record["last_confirmed_state"])}
        if self.pem:
            evidence["pem"] = self.pem.preflight()
        if self.power:
            evidence["power_meter"] = {"identity": self.power.meter.identify(),
                                      "sensor": self.power.meter.read_sensor()}
        self.opened = True
        self.record["preflight"] = deepcopy(evidence)
        self._event("optics_preflight", evidence=evidence)
        return evidence

    def _context(self, point):
        laser_point = point.laser_point
        return (point.scan_identity, getattr(laser_point, "wavelength_nm", None),
                getattr(laser_point, "bandwidth_nm", None),
                getattr(laser_point, "nd_pct", None),
                getattr(laser_point, "pulse_picker_ratio", None),
                self.peak_retardance_waves)

    def prepare(self, point):
        self._require_open()
        if self.sample_active:
            raise NktError("Cannot prepare optics inside a formal sample")
        point.validate(self.laser, self.power)
        self.point, self.qualified = point, False
        self.record["state_current"] = False
        self.row = {"index": point.index, "optical_state": point.optical_state,
                    "target_power_w": point.target_power_w,
                    "requested": asdict(point.laser_point), "qualified": False,
                    "valid": False, "started_monotonic_s": self.clock(),
                    "measurement_plane": (self.power.config.meter.measurement_plane
                                          if self.power else None),
                    "measured_power_w": None, "power": None}
        self.record["points"].append(self.row)
        try:
            previous = self._previous
            self._previous = None
            initial = None
            if (point.optical_state == "light" and self.power
                    and self.power.config.initial_actuator_mode == "previous"
                    and previous is not None and previous["context"] == self._context(point)):
                initial = previous["value"]
            self.row["initial_actuator"] = {"source": "previous" if initial is not None else "point",
                                            "value": initial}
            if self.power:
                readback = self.laser.prepare_power_point(point.laser_point, self.power.config,
                                                         initial_value=initial)
                condition = {"power_index": point.power_index}
                self.power.prepare(condition, point.laser_point)
                self.row["power"] = self.power.row
            else:
                readback = self.laser.prepare_point(point.laser_point)
            filt = readback.get("filter")
            wavelength = ((filt["lower_edge_nm"] + filt["upper_edge_nm"]) / 2
                          if filt and filt.get("kind") == "varia"
                          else point.laser_point.wavelength_nm)
            self.row["wavelength_nm"] = wavelength
            if self.pem:
                key = (wavelength, self.peak_retardance_waves)
                if key != self._pem_key:
                    self.row["pem_preparation"] = self.pem.prepare(wavelength, self.peak_retardance_waves)
                    self._pem_key = key
                else:
                    self.row["pem_preparation"] = {"reused_setting": True,
                                                    "readback": self.pem.verify_ready()}
            self.last_change_monotonic_s = self.clock()
            self._suspended = True
            self.row["prepared_monotonic_s"] = self.clock()
            self._point_event("optics_prepared")
            return self.row
        except BaseException as exc:
            self._invalidate(exc)
            raise

    def _invalidate(self, exc):
        self.qualified = False
        self._previous = None
        self.record["state_current"] = False
        if self.row is not None:
            self.row["qualified"] = False
            self.row["error"] = f"{type(exc).__name__}: {exc}"

    def _changed(self):
        self.last_change_monotonic_s = self.clock()
        self.qualified = False

    def _module_guard(self, guard):
        def checked():
            result = guard() if guard else {}
            if self.pem:
                result = {"modules": result, "pem": self.pem.verify_ready()}
            return result
        return checked

    def qualify(self, guard=None):
        self._require_open()
        if self.point is None or self.sample_active:
            raise NktError("Prepare optics before qualification; no tuning during samples")
        if self.qualified:
            return {"ready": True, "evidence": self.verify("qualification_reused")}
        try:
            self._module_guard(guard)()
            bright = self.point.optical_state == "light"
            self.laser.verify_point(emitting=False, phase="optics_before_qualification")
            if bright:
                self.laser.set_emission(True)
                self._suspended = False
                self._changed()
                if self.power:
                    self.power.qualify(self._module_guard(guard), on_emission_changed=self._changed)
            required = (self.dark_recovery_s if not bright and self.dark_recovery_s is not None
                        else self.settle_s)
            elapsed = self.clock() - self.last_change_monotonic_s
            remaining = max(0., required - elapsed)
            if remaining:
                self.sleep(remaining)
            self.row["settling"] = {"required_s": required, "elapsed_before_wait_s": elapsed,
                                     "requested_wait_s": remaining,
                                     "last_change_monotonic_s": self.last_change_monotonic_s}
            evidence = self.verify("optics_qualified")
            self.qualified = True
            self.row.update(qualified=True, qualified_monotonic_s=self.clock())
            self._point_event("optics_qualified")
            return {"ready": True, "evidence": evidence}
        except BaseException as exc:
            self._invalidate(exc)
            raise

    def _verify(self, *, emitting, phase):
        self._require_open()
        self.record["state_current"] = False
        try:
            state = self.laser.verify_point(emitting=emitting, phase=phase)
            if self.pem:
                state = {**state, "pem": self.pem.verify_ready()}
            self.record.update(last_confirmed_state=deepcopy(state), state_current=True)
            return state
        except BaseException as exc:
            self._invalidate(exc)
            raise

    def verify(self, phase="optics_verify"):
        if self.point is None:
            raise NktError("No prepared optics point")
        return self._verify(emitting=self.point.optical_state == "light", phase=phase)

    def preacquisition_check(self, guard=None):
        self._require_open()
        if not self.qualified or self.sample_active:
            raise NktError("Qualified optics required before acquisition")
        try:
            if self.power and self.point.optical_state == "light":
                return self.power.preacquisition_check(self._module_guard(guard))
            self._module_guard(guard)()
            return self.verify("optics_preacquisition")
        except BaseException as exc:
            self._invalidate(exc)
            raise

    def begin_sample(self, guard=None):
        if not self.qualified or self.sample_active:
            raise NktError("A fresh qualified point and no active sample are required")
        self.sample_active = True
        try:
            if self.power:
                return self.power.formal_sample("optics_before", illuminated=self.point.optical_state == "light",
                                                guard=self._module_guard(guard))
            self._module_guard(guard)()
            return self.verify("optics_before")
        except BaseException as exc:
            self.sample_active = False
            self._invalidate(exc)
            raise

    def end_sample(self, guard=None):
        if not self.sample_active:
            raise NktError("No active optics sample")
        try:
            if self.power:
                return self.power.formal_sample("optics_after", illuminated=self.point.optical_state == "light",
                                                guard=self._module_guard(guard))
            self._module_guard(guard)()
            return self.verify("optics_after")
        except BaseException as exc:
            self._invalidate(exc)
            raise
        finally:
            self.sample_active = False

    def finish_point(self, *, valid=True):
        if self.sample_active:
            raise NktError("Cannot finish optics with an active sample")
        if not valid:
            self.qualified = False
            self._previous = None
            self.row.update(valid=False, qualified=False, finished_monotonic_s=self.clock())
            return self.row
        if not self.qualified:
            raise NktError("Cannot accept unqualified optics")
        state = self.verify("optics_point_complete")
        if self.power and self.point.optical_state == "light":
            self.row["measured_power_w"] = self.power.row["formal_mean_power_w"]
            field, _, _, _ = actuator_domain(self.power.config)
            value = (state["source"]["getLdCurrSetpoint"] if field == "current_a" else
                     state["filter"]["nd_pct"] if field == "nd_pct" else state["source"]["level_pct"])
            self._previous = {"context": self._context(self.point), "value": value}
        self.row.update(valid=True, finished_monotonic_s=self.clock())
        return self.row

    def suspend(self):
        self._require_open()
        if self.sample_active:
            raise NktError("Cannot suspend optics during a formal sample")
        self.qualified = False
        if self.row is not None:
            self.row["qualified"] = False
        if self.point is None:
            # An external operator could have changed output since open(). A
            # fresh pre-point read must prove OFF; never reuse old preflight.
            try:
                if hasattr(self.laser.config, "diode_model"):
                    raw = self.laser.capture("optics_suspend_prepoint")
                    self.laser.healthy(raw)
                    if raw["getLdOutputState"]:
                        raise NktError("CLD became active before an optics point; no takeover")
                    evidence = {"source": raw, "filter": None}
                else:
                    source, filt = self.laser.capture("optics_suspend_prepoint")
                    self.laser._healthy(source, filt)
                    if not self.laser._emission_matches(source, False):
                        raise NktError("NKT became active before an optics point; no takeover")
                    evidence = {"source": source, "filter": filt}
                self.record.update(last_confirmed_state=deepcopy(evidence), state_current=True)
                return {"off_confirmed": True, "evidence": evidence}
            except BaseException as exc:
                self._invalidate(exc)
                raise
        try:
            if self._suspended:
                state = self.laser.verify_point(emitting=False, phase="optics_suspend_already_off")
            else:
                state = self.laser.set_emission(False)
                self.last_change_monotonic_s = self.clock()
                self._suspended = True
            return {"off_confirmed": True, "evidence": state}
        except BaseException as exc:
            self._invalidate(exc)
            raise

    def cleanup(self):
        if self._cleanup is not None:
            return self._cleanup
        result = {"verified": False, "off_confirmed": False, "errors": [],
                  "laser": None, "pem": None, "power": None}
        # Best effort for every owned resource, irrespective of earlier failures.
        try:
            result["laser"] = self.laser.finish()
            result["off_confirmed"] = result["laser"]["off_confirmed"]
            result["errors"].extend("laser: " + e for e in result["laser"]["errors"])
        except BaseException as exc:
            result["errors"].append(f"laser: {type(exc).__name__}: {exc}")
        if self.pem:
            try:
                result["pem"] = self.pem.finish()
                result["errors"].extend("PEM: " + e for e in result["pem"]["errors"])
            except BaseException as exc:
                result["errors"].append(f"PEM: {type(exc).__name__}: {exc}")
        if self.power:
            try:
                self.power.close()
                # Raw commands remain in the owned audit once. Cleanup and its
                # progress event contain an explicit reference, with no cycle.
                self.record["power_meter_audit"] = self.power.audit
                result["power"] = {"closed": bool(self.power.meter.closed), "state_current": False,
                    "last_confirmed_state": deepcopy(self.power.audit["last_confirmed_state"]),
                    "transcript_reference": "optics_audit.power_meter_audit.transcript",
                    "transaction_count": len(self.power.audit["transcript"])}
            except BaseException as exc:
                result["errors"].append(f"power: {type(exc).__name__}: {exc}")
        self.closed = True
        self.qualified = self.sample_active = False
        self.record["state_current"] = False
        result["verified"] = result["off_confirmed"] and not result["errors"]
        self._cleanup = result
        self.record["cleanup"] = result
        return result
