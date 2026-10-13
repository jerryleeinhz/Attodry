"""Photoresponse point adapter for the pinned, unchanged Three-SMU engine.

Gate leakage and the optional SMU excitation readbacks remain separate from the
selected photoresponse detector. No SCPI or replacement SMU driver lives here.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import math
import re
import time

from .nkt_config import NktError, keys
from .photonic_smu import SOURCE_COMMIT, SOURCE_SHA256
from .photonic_smu.keithley2400 import (
    KeithleyConfigurationReadback, KeithleyPreflight, KeithleyReading, open_keithley2400,
)
from .photonic_smu.three_smu import ThreeSmuSession
from .photonic_smu.three_smu_config import (
    FinishAction, ScanMode, SourceMode, ThreeSmuHardwareConfig, ThreeSmuScanPlan, ThreeSmuConfigError,
    active_smu_roles, load_three_smu_operation_config, validate_plan_targets,
)


SMU_MODES = {"smu_current_voltage": SourceMode.CURRENT, "smu_voltage_voltage": SourceMode.VOLTAGE}


@dataclass(frozen=True)
class PhotoresponseSmuConfig:
    hardware: ThreeSmuHardwareConfig
    plan: ThreeSmuScanPlan
    wiring_confirmed: bool = False
    expected_idn: dict | None = None

    @property
    def roles(self):
        return active_smu_roles(self.plan)

    @property
    def points(self):
        try: return validate_plan_targets(self.hardware, self.plan)
        except ThreeSmuConfigError as exc: raise NktError(str(exc)) from exc

    def validate(self, photo):
        if type(self.wiring_confirmed) is not bool:
            raise NktError("photoresponse_smu.wiring_confirmed must be boolean")
        if not self.roles:
            raise NktError("Enabled photoresponse_smu requires at least one active role")
        if self.plan.finish_action is not FinishAction.ZERO_DISABLE:
            raise NktError("Photoresponse SMUs must finish zero_disable")
        if self.plan.samples_per_point != 1 or self.plan.mode is ScanMode.SOFTWARE_PULSE:
            raise NktError("Photoresponse owns window sampling; SMU points require samples_per_point=1 and no pulse mode")
        self.points
        expected = self.expected_idn or {}
        if not isinstance(expected, dict) or set(expected) - set(self.roles):
            raise NktError("SMU expected identities must refer only to active roles")
        for role, identity in expected.items():
            if not isinstance(identity, str) or not identity.strip():
                raise NktError(f"{role} expected identity must be a nonempty exact string")
        for role in self.roles:
            h, channel = self.hardware.require_role(role), self.plan.by_role()[role]
            if role.startswith("gate_"):
                if h.source_mode is not SourceMode.VOLTAGE:
                    raise NktError("Photoresponse gate roles require voltage source mode")
                if channel.ramp is None and channel.zero_readback_tolerance_v is None:
                    raise NktError("Each active gate requires the reused ramp or explicit direct zero readback tolerance")
        if "smu_bias" in self.roles:
            if photo.mode not in SMU_MODES:
                raise NktError("Active smu_bias requires an explicit SMU excitation mode; dual sample sources are forbidden")
            h = self.hardware.require_role("smu_bias")
            if h.source_mode is not SMU_MODES[photo.mode] or photo.detector != "keithley2182a":
                raise NktError("SMU excitation requires matching source mode and a separate 2182A voltage detector")
            if h.max_abs_voltage_v > photo.max_abs_voltage_v or h.max_abs_current_a > photo.max_abs_current_a:
                raise NktError("SMU sample excitation limits exceed photoresponse sample limits")
        elif photo.mode in SMU_MODES:
            raise NktError("SMU excitation requires an active smu_bias role")

    def require_hardware(self):
        if not self.wiring_confirmed:
            raise NktError("SMU wiring/role isolation must be confirmed before hardware operation")
        if set(self.expected_idn or {}) != set(self.roles):
            raise NktError("Every active SMU requires an exact expected_idn before hardware operation")
        if any("CHANGE_ME" in value.upper() for value in self.expected_idn.values()):
            raise NktError("Replace placeholder SMU identities before hardware operation")

    def snapshot(self):
        return {**asdict(self), "points": [asdict(p) for p in self.points],
                "source_commit": SOURCE_COMMIT, "source_sha256": dict(SOURCE_SHA256)}


def load_photoresponse_smu(doc, path):
    if "photoresponse_smu" not in doc:
        return None  # A dormant independent Three-SMU plan is never activated implicitly.
    table = doc["photoresponse_smu"]
    keys(table, "photoresponse_smu", {"enabled"}, {"wiring_confirmed", "expected_idn"})
    if type(table["enabled"]) is not bool:
        raise NktError("photoresponse_smu.enabled must be boolean")
    if not table["enabled"]:
        return None
    operation = load_three_smu_operation_config(path)
    return PhotoresponseSmuConfig(operation.hardware, operation.plan,
                                 table.get("wiring_confirmed", False), table.get("expected_idn"))


def normalized_resource(value):
    value = value.strip().upper()
    serial = re.fullmatch(r"(?:ASRL(\d+)::INSTR|COM(\d+))", value)
    if serial:
        return "COM" + str(int(serial.group(1) or serial.group(2)))
    # The optional zero secondary address and INSTR suffix describe one GPIB endpoint.
    gpib = re.fullmatch(r"GPIB(\d*)::(\d+)(?:::(\d+))?(?:::INSTR)?", value)
    if gpib:
        return f"GPIB{int(gpib.group(1) or 0)}::{int(gpib.group(2))}::{int(gpib.group(3) or 0)}"
    return value


def validate_selected_resources(config):
    """Reject duplicate ownership before any selected controller is constructed."""
    resources = dict(config.hardware.addresses()) if config.hardware else {}
    if hasattr(config.nkt, "address"):
        resources["light_source"] = config.nkt.address
    elif config.nkt.port:
        resources["light_source"] = config.nkt.port
    if config.power:
        resources["pm100d"] = config.power.meter.resource
    if config.environment:
        resources["environment"] = config.environment.cryostat.com_port
    if config.smu:
        resources.update({role: config.smu.hardware.require_role(role).address for role in config.smu.roles})
    seen = {}
    for role, address in resources.items():
        if not address:
            continue
        resource = normalized_resource(address)
        if resource in seen:
            raise NktError(f"Selected roles {seen[resource]} and {role} share resource {resource}")
        seen[resource] = role


class SimulatedSmu:
    """In-memory implementation of the reused adapter contract, never opens VISA."""
    def __init__(self, role, config):
        self.role, self.config = role, config
        self.source, self.output, self.closed = 0.0, False, False

    def preflight(self):
        return KeithleyPreflight(f"SIMULATED,2400,{self.role},1", self.config.source_mode,
            self.source, self.output, status="0,No error", status_query_consumed=True)

    def zero_residual(self, mode): self.source = 0.0
    def configure(self, config):
        return KeithleyConfigurationReadback(
            config.max_abs_current_a if config.source_mode is SourceMode.VOLTAGE else config.max_abs_voltage_v,
            max(abs(self.source), 1.0), 1.0)
    def set_source(self, value): self.source = value
    def set_output(self, enabled): self.output = enabled
    def read(self):
        voltage, current = ((self.source, self.source * 1e-12) if self.config.source_mode is SourceMode.VOLTAGE
                            else (self.source * 1000, self.source))
        return KeithleyReading(voltage, current, self.source, self.output, False, "0,No error", True)
    def close(self): self.closed = True


class PhotoresponseSmu:
    def __init__(self, config, *, simulated, on_event=None, authorize_writes=False,
                 adapter_factory=None, sleep=time.sleep, clock=time.perf_counter):
        config.validate()
        if config.smu is None:
            raise NktError("SMU controller requires an enabled SMU configuration")
        self.config, self.photo = config.smu, config
        self.is_hardware = not simulated
        if self.is_hardware != (config.nkt.backend != "simulation"):
            raise NktError("Mixed SMU and photoresponse backends are forbidden")
        if self.is_hardware:
            self.config.require_hardware()
            if not authorize_writes:
                raise NktError("SMU connection, settings, READ and consuming status queries require --authorize-smu")
        self.factory = adapter_factory or (SimulatedSmu if simulated else open_keithley2400)
        self.sleep, self.clock, self.on_event = sleep, clock, on_event
        self.session, self.point, self.begun = None, None, False
        self.closed = False
        self.audit = {"source_commit": SOURCE_COMMIT, "events": [], "cleanups": [], "last_confirmed": None,
                      "state_current": False}
        self._open()

    def event(self, kind, payload):
        entry = {"phase": "smu_" + kind, "payload": deepcopy(payload)}
        self.audit["events"].append(entry)
        if self.on_event:
            self.on_event(entry)

    def _preflight(self, role, state):
        self.event("preflight", {"role": role, "state": asdict(state)})
        if self.is_hardware and state.identity != self.config.expected_idn[role]:
            raise NktError(f"{role} exact identity mismatch")

    def _open(self):
        self.session = ThreeSmuSession.open(self.config.hardware, self.config.plan,
            authorize_writes=True, authorize_status_consumption=True, adapter_factory=self.factory,
            on_preflight=self._preflight, sleep=self.sleep, monotonic=self.clock)

    def begin(self):
        if self.closed or self.begun or self.audit.get("errors"):
            raise NktError("SMU controller is closed, already begun, or has unresolved cleanup errors")
        if self.session is None:
            self._open()  # Fresh preflight after a safe environment transition.
        self.begun = True  # Own partial configuration even if begin_points fails.
        self.session.begin_points(self)

    def prepare(self, point, guard):
        if point not in self.config.points or not self.begun:
            raise NktError("SMU point is outside the approved plan or session is inactive")
        self.audit["state_current"] = False
        self.point = None
        self.session.set_point(point.coordinates, index=point.index, segment=point.segment,
                               transition_guard=guard)
        self.point = point
        return self.verify()

    def verify(self):
        self.audit["state_current"] = False
        if self.point is None:
            raise NktError("No confirmed SMU point")
        raw = self.session.sample_point(self.point.coordinates, index=self.point.index, segment=self.point.segment)
        result = asdict(raw)
        problems = list(raw.problems)
        for role, timed in raw.readings.items():
            channel = self.config.plan.by_role()[role]
            tolerance = (channel.ramp.readback_tolerance_v if channel.ramp else channel.zero_readback_tolerance_v)
            tolerance = tolerance if tolerance is not None else 1e-15
            target, reading = self.point.coordinates[role], timed.reading
            if not math.isclose(reading.source_setpoint, target, rel_tol=1e-9, abs_tol=tolerance):
                problems.append(f"{role} source setpoint differs from requested leaf")
            if role.startswith("gate_") and not math.isclose(reading.voltage_v, target, rel_tol=0, abs_tol=tolerance):
                problems.append(f"{role} measured voltage differs from requested leaf")
        result.update(clean=raw.clean and not problems, problems=problems)
        self.audit["last_confirmed"] = deepcopy(result)
        self.event("point_readback", result)  # Keep rejected values before raising.
        if not result["clean"]:
            raise NktError("SMU leaf rejected: " + "; ".join(problems))
        self.audit["state_current"] = True
        return result

    def suspend(self, *, failed=False):
        """Release configured outputs before environment movement; never touch OFF roles."""
        errors, result = [], None
        self.audit["state_current"] = False
        try:
            if self.begun:
                result = self.session.cleanup_points(self, reason="photoresponse transition" if not failed else "photoresponse failure",
                                                     failed=failed)
                self.audit["cleanups"].append(deepcopy(result))
                if result["manual_verification_required"]:
                    errors.append("SMU cleanup requires manual verification")
        except (Exception, KeyboardInterrupt) as exc:
            errors.append(f"SMU cleanup: {type(exc).__name__}: {exc}")
        finally:
            if self.session:
                try: self.session.close()
                except (Exception, KeyboardInterrupt) as exc:
                    errors.append(f"SMU close: {type(exc).__name__}: {exc}")
            self.session, self.point, self.begun = None, None, False
        if errors:
            self.audit.setdefault("errors", []).extend(errors)
        if self.audit.get("errors"):
            raise NktError("; ".join(self.audit["errors"]))
        return result

    def finish(self, *, failed=False):
        if not self.closed:
            try: self.suspend(failed=failed)
            finally: self.closed = True
        if self.audit.get("errors"):
            raise NktError("; ".join(self.audit["errors"]))
        return deepcopy(self.audit)

    def close(self):
        if not self.closed:
            self.finish(failed=True)
