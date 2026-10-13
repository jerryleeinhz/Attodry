"""Standalone NKT orchestration. No vendor imports or hardware on import."""
from __future__ import annotations

from dataclasses import asdict, replace
from datetime import datetime, timezone
import math
import time
from typing import Protocol

from .nkt_config import NktConfig, NktError, NktPoint


SOURCE_FAULT_MASK = 0xE0FE  # EXTREME S4x2 SDK: bits 1..7, 13..15.
VARIA_FAULT_MASK = (1 << 1) | (1 << 5) | (1 << 15)
VARIA_MOVING_MASK = 0x7000


class NktBackend(Protocol):
    is_hardware: bool

    def read_source(self) -> dict: ...
    def read_filter(self) -> dict | None: ...
    def set_emission(self, enabled: bool) -> None: ...
    def configure(self, point: NktPoint) -> None: ...
    def close(self) -> None: ...


class PowerMeter(Protocol):
    """Optional user adapter: return watts at the documented measurement plane.

    The adapter owns detector wavelength correction/calibration. For broadband,
    wavelength_nm is None, never a fabricated effective wavelength.
    """
    def read_power_w(self, wavelength_nm: float | None) -> float: ...


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class SimulatedNkt:
    is_hardware = False

    def __init__(self, config: NktConfig):
        config.validate()
        self.config = config
        self.commands = []
        self.source = {
            "identity": {"model": "SIMULATED EXW-12 PP", "serial": "SIMULATION"},
            "emission_state": 0, "status_bits": 0, "interlock": 2,
            "control": config.source_control, "level_pct": 0.0,
            "pulse_picker_ratio": 1,
        }
        self.filter = None
        if config.mode == "varia_bandpass":
            self.filter = {"kind": "varia", "lower_edge_nm": 600.0,
                           "upper_edge_nm": 620.0, "nd_pct": 0.0,
                           "status_bits": 0, "moving": False, "monitor_pct": None}
        elif config.mode == "lltf_swir":
            self.filter = {"kind": "lltf", "wavelength_nm": 1500.0,
                           "moving": False, "fault": False,
                           "nominal_fwhm_upper_bound_nm": 5.0}

    def read_source(self):
        self.commands.append(("read_source",))
        return {**self.source, "identity": dict(self.source["identity"])}

    def read_filter(self):
        self.commands.append(("read_filter",))
        return dict(self.filter) if self.filter is not None else None

    def set_emission(self, enabled):
        self.commands.append(("emission", enabled))
        self.source["emission_state"] = 3 if enabled else 0
        self.source["status_bits"] = (self.source["status_bits"] & ~1) | int(enabled)

    def configure(self, point):
        point.validate(self.config.mode, self.config.max_level_pct, self.config.allowed_pp_ratios)
        if self.source["emission_state"] != 0:
            raise NktError("Configure only with confirmed emission off")
        self.commands.append(("configure", asdict(point)))
        self.source["level_pct"] = point.source_level_pct
        if point.pulse_picker_ratio is not None:
            self.source["pulse_picker_ratio"] = point.pulse_picker_ratio
        if self.config.mode == "varia_bandpass":
            low, high = point.edges_nm
            self.filter.update(lower_edge_nm=low, upper_edge_nm=high, nd_pct=point.nd_pct)
        elif self.config.mode == "lltf_swir":
            self.filter["wavelength_nm"] = point.wavelength_nm

    def close(self):
        self.commands.append(("close",))


class NktController:
    def __init__(self, config, backend, *, power_meter: PowerMeter | None = None,
                 measurement_plane: str | None = None, clock=time.monotonic,
                 sleep=time.sleep, on_event=None):
        config.validate()
        if power_meter is not None and not measurement_plane:
            raise NktError("Power meter requires a named measurement plane")
        self.config, self.backend = config, backend
        self.power_meter, self.measurement_plane = power_meter, measurement_plane
        self.clock, self.sleep, self.on_event = clock, sleep, on_event
        self._owned = False
        self._closed = False
        self._faulted = False
        self._started = False
        self._prepared_point = None
        self._expected_emitting = False
        self.record = {"schema_version": 1, "captured_at_utc": utc_now(),
                       "simulated": not backend.is_hardware,
                       "measurement_config": config.snapshot(), "completed": False,
                       "outcome": "rejected", "points": [], "events": [],
                       "last_confirmed_state": {}, "state_current": False,
                       "source_state_current": False,
                       "error": None, "cleanup": {"errors": [], "off_confirmed": False}}

    def _event(self, phase, **values):
        event = {"phase": phase, "captured_at_utc": utc_now(), **values}
        self.record["events"].append(event)
        if self.on_event:
            self.on_event(event)

    def capture(self, phase, source_only=False):
        self.record["state_current"] = False
        self.record["source_state_current"] = False
        source = self.backend.read_source()
        self.record["last_confirmed_state"]["source"] = {
            "captured_at_utc": utc_now(), "readback": source}
        self.record["source_state_current"] = True
        self._event(phase, role="source", readback=source)
        filt = None if source_only else self.backend.read_filter()
        if not source_only:
            self.record["last_confirmed_state"]["filter"] = {
                "captured_at_utc": utc_now(), "readback": filt}
            self._event(phase, role="filter", readback=filt)
        self.record["state_current"] = not source_only or self.config.mode == "broadband"
        return source, filt

    def _healthy(self, source, filt):
        if source["interlock"] != 2 or source["status_bits"] & SOURCE_FAULT_MASK:
            raise NktError("EXTREME interlock/status prevents operation")
        if source["control"] != self.config.source_control:
            raise NktError("Source control mode changed or unsupported")
        if not math.isfinite(source["level_pct"]) or not 0 <= source["level_pct"] <= 100:
            raise NktError("Invalid source level readback")
        if source["emission_state"] != 0 and source["level_pct"] > self.config.max_level_pct:
            raise NktError("Active source output exceeds configured limit")
        # The SDK documents transient values without enumerating all of them.
        # Only 0/off and 3/on are accepted as settled; other U8 values time out.
        if type(source["emission_state"]) is not int or not 0 <= source["emission_state"] <= 255:
            raise NktError("Malformed emission state")
        if filt is not None:
            if filt.get("fault") or filt.get("status_bits", 0) & VARIA_FAULT_MASK:
                raise NktError("Filter status prevents operation")

    @staticmethod
    def _matches(point, source, filt):
        if not math.isclose(source["level_pct"], point.source_level_pct, abs_tol=0.051, rel_tol=0):
            return False
        if point.pulse_picker_ratio is not None and source["pulse_picker_ratio"] != point.pulse_picker_ratio:
            return False
        if filt is None:
            return point.wavelength_nm is None
        if filt["moving"]:
            return False
        if filt["kind"] == "varia":
            low, high = point.edges_nm
            return all(math.isclose(a, b, abs_tol=0.051, rel_tol=0) for a, b in (
                (low, filt["lower_edge_nm"]), (high, filt["upper_edge_nm"]),
                (point.nd_pct, filt["nd_pct"])))
        return math.isclose(point.wavelength_nm, filt["wavelength_nm"], abs_tol=0.05, rel_tol=0)

    def _wait(self, phase, predicate, *, source_only=False, healthy=True):
        deadline = self.clock() + self.config.timeout_s
        while True:
            source, filt = self.capture(phase, source_only)
            if healthy:
                self._healthy(source, filt)
            if predicate(source, filt):
                return source, filt
            if self.clock() >= deadline:
                raise NktError(f"Timeout waiting for {phase}")
            self.sleep(self.config.poll_interval_s)

    @staticmethod
    def _emission_matches(source, enabled):
        return source["emission_state"] == (3 if enabled else 0) and bool(source["status_bits"] & 1) == enabled

    def _off(self, phase):
        # This method is used only after ownership of a confirmed-off device.
        self.record["state_current"] = False
        self.record["source_state_current"] = False
        self.backend.set_emission(False)
        self._wait(phase, lambda s, f: self._emission_matches(s, False),
                   source_only=True, healthy=False)

    def _check_access(self, authorize_writes, confirm_manual_route):
        self.config.validate()
        if self.backend.is_hardware:
            self.config.require_hardware(writes=True)
            if not authorize_writes or not confirm_manual_route:
                raise NktError("Writes and the selected manual optical route require explicit authorization")

    def begin(self, *, authorize_writes=False, confirm_manual_route=False):
        """Acquire a confirmed-off source; no writes or implicit resource open.

        A coordinator must call finish() in finally after entering this session.
        Every public setting operation requires this successful preflight.
        """
        self._check_access(authorize_writes, confirm_manual_route)
        if self._started or self._closed:
            raise NktError("Controller sessions are single-use")
        self._started = True
        self._faulted = True
        source, filt = self.capture("preflight")
        self._healthy(source, filt)
        if not self._emission_matches(source, False):
            raise NktError("Preflight requires confirmed emission off; no automatic takeover")
        self._owned = True
        self._faulted = False

    def _require_session(self, *, prepared=False):
        if not self._owned or self._closed or self._faulted:
            raise NktError("A successful authorized begin() is required")
        if prepared and self._prepared_point is None:
            raise NktError("prepare_point() must complete before emission or sampling")

    def prepare_point(self, point: NktPoint):
        """Configure one approved point with emission off, then verify settling."""
        self._require_session()
        self._faulted = True
        point.validate(self.config.mode, self.config.max_level_pct, self.config.allowed_pp_ratios)
        if point not in self.config.points:
            raise NktError("Point is outside the configured scan")
        self._feedback_anchor = point
        return self._prepare_validated_point(point)

    def _prepare_validated_point(self, point):
        self._prepared_point = None
        self._off("before_configure")
        self._event("configure_request", requested=asdict(point))
        self.backend.configure(point)
        source, filt = self._wait("filter_settle", lambda s, f: self._emission_matches(s, False)
                                  and self._matches(point, s, f))
        self._prepared_point = point
        self._expected_emitting = False
        self._faulted = False
        return {"source": source, "filter": filt}

    def adjust_power(self, value, policy):
        """One approved actuator step, preserving the configured optical anchor."""
        from .nkt_config import number
        self._require_session(prepared=True)
        policy.validate(self.config)
        f = policy.feedback
        if f.actuator == "source_current":
            field, low, high, step = ("source_level_pct", f.source_current_min_pct,
                                    f.source_current_max_pct, f.source_current_step_pct)
        elif f.actuator == "varia_nd":
            field, low, high, step = "nd_pct", f.nd_min_pct, f.nd_max_pct, f.nd_step_pct
        else:
            raise NktError("Unsupported NKT power actuator")
        number(value, "power actuator", low, high)
        if abs(value - getattr(self._prepared_point, field)) > step + 1e-10:
            raise NktError("Power adjustment exceeds approved single step")
        point = replace(self._feedback_anchor, **{field: value})
        point.validate(self.config.mode, self.config.max_level_pct, self.config.allowed_pp_ratios)
        self.verify_point(emitting=False, phase="before_power_adjustment")
        self._faulted = True
        return self._prepare_validated_point(point)

    def set_emission(self, enabled: bool):
        """Set and verify the optical state without releasing point ownership."""
        self._require_session(prepared=True)
        self._faulted = True
        if type(enabled) is not bool or (enabled and not self.config.emit):
            raise NktError("Emission requires a boolean and emit=true in the approved config")
        if enabled:
            # Integration may spend time preparing other instruments. Recheck the
            # actual point before giving light, not only after the emission write.
            source, filt = self.capture("before_emission")
            self._healthy(source, filt)
            if not self._matches(self._prepared_point, source, filt):
                raise NktError("Prepared settings changed before emission")
            if not self._emission_matches(source, self._expected_emitting):
                raise NktError("Unexpected emission state before emission request")
            if self._expected_emitting:
                self._faulted = False
                return {"source": source, "filter": filt}
        self.record["state_current"] = False
        self.record["source_state_current"] = False
        self.backend.set_emission(enabled)
        source, filt = self._wait("emission_settle", lambda s, f: self._emission_matches(s, enabled)
                                  and self._matches(self._prepared_point, s, f))
        self._expected_emitting = enabled
        self._faulted = False
        return {"source": source, "filter": filt}

    def verify_point(self, *, emitting: bool, phase="formal"):
        """Fresh readback barrier before/after an external detector acquisition."""
        self._require_session(prepared=True)
        self._faulted = True
        if type(emitting) is not bool:
            raise NktError("emitting must be a boolean")
        source, filt = self.capture(phase)
        self._healthy(source, filt)
        if not self._matches(self._prepared_point, source, filt):
            raise NktError("Formal readback differs from requested settings")
        if not self._emission_matches(source, emitting):
            raise NktError("Formal emission state mismatch")
        self._faulted = False
        return {"source": source, "filter": filt}

    def finish(self):
        """Best-effort off/readback/close. Never take over a failed preflight."""
        if self._closed:
            return self.record["cleanup"]
        try:
            if self._owned:
                try:
                    self._off("cleanup")
                    self.record["cleanup"]["off_confirmed"] = True
                except (Exception, KeyboardInterrupt) as exc:
                    self.record["state_current"] = False
                    self.record["source_state_current"] = False
                    self.record["cleanup"]["errors"].append(f"{type(exc).__name__}: {exc}")
        finally:
            try:
                self.backend.close()
            except (Exception, KeyboardInterrupt) as exc:
                self.record["cleanup"]["errors"].append(f"close: {exc}")
            self._closed = True
            self._owned = False
        return self.record["cleanup"]

    def run(self, *, authorize_writes=False, confirm_manual_route=False):
        # Authorization failure retains the original zero-I/O run contract.
        self._check_access(authorize_writes, confirm_manual_route)
        try:
            self.begin(authorize_writes=authorize_writes, confirm_manual_route=confirm_manual_route)
            for i, point in enumerate(self.config.points):
                row = {"condition_id": f"nkt-{i:06d}", "attempt_index": 1,
                       "requested": asdict(point), "accepted": False,
                       "readback": None, "measured_power_w": None,
                       "measurement_plane": self.measurement_plane}
                self.record["points"].append(row)
                self.prepare_point(point)
                if self.config.emit:
                    self.set_emission(True)
                until = self.clock() + self.config.dwell_s
                while True:
                    row["readback"] = self.verify_point(emitting=self.config.emit)
                    if self.clock() >= until:
                        break
                    self.sleep(min(self.config.poll_interval_s, until - self.clock()))
                if self.power_meter is not None:
                    value = self.power_meter.read_power_w(point.wavelength_nm)
                    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                        raise NktError("Power meter returned invalid watts")
                    row["measured_power_w"] = value
                    # Meter acquisition may take time; verify the state again.
                    self.verify_point(emitting=self.config.emit, phase="after_power_measurement")
                    row["measured_power_captured_at_utc"] = utc_now()
                self._event("point_complete", point=row.copy())
            self.record["outcome"] = "completed"
        except (Exception, KeyboardInterrupt) as exc:
            self.record["error"] = f"{type(exc).__name__}: {exc}"
            self.record["outcome"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "rejected"
        finally:
            self.finish()
            if self.record["cleanup"]["errors"]:
                self.record["outcome"] = "rejected"
            self.record["completed"] = self.record["outcome"] == "completed"
            for row in self.record["points"]:
                row["accepted"] = self.record["completed"]
        return self.record
