"""Gated four-module point execution; no I/O until explicit run authorization."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime
from enum import Enum
import hashlib
import re
from pathlib import Path
from types import SimpleNamespace

from . import config as configuration
from .combination_scan import AxisPoint, ScanAxis, CombinationPlan, _audit_value, _run_combination
from .combination_store import utc_now
from .lockin_points import LockinPointSession
from .models import LockinRole
from .config import RunMode
from .three_smu import ThreeSmuSession
from .three_smu_config import load_three_smu_operation_config, validate_plan_targets, SourceMode, ScanMode
from .cryostat_points import CryostatPointSession
from .models import VectorField
from .safety import MagnetLimits, plan_ordered_field_transitions
from .photonics_lockin_config import PHOTONICS_REFERENCE_TOPOLOGIES


def _json(value):
    if isinstance(value, dict):
        return {k: _json(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json(v) for v in value]
    if isinstance(value, (Path, datetime)):
        return str(value) if isinstance(value, Path) else value.isoformat()
    if isinstance(value, Enum):
        return value.value
    return _audit_value(value)


@dataclass(frozen=True)
class HardwareCombinationConfig:
    path: Path
    plan: CombinationPlan
    smu: object | None
    lockin: object | None
    snapshot: dict
    temperature: object | None = None
    magnetic: object | None = None
    cryostat: object | None = None
    database_path: Path | None = None
    run_id: str = "auto"
    lockin_mode: str = "excitation"
    optical: object | None = None
    reference_topology: str = "internal_xx_xy"


def load_hardware_combination(path: str | Path) -> HardwareCombinationConfig:
    """Reuse standalone grids/limits; do not parse or access inactive modules."""
    path = Path(path).resolve()
    document = configuration._load_document(path)
    known = {"project", "cryostat", "magnet", "temperature_stability", "temperature_run",
             "temperature_scan", "temperature_excitation_scan", "magnetic_field_run",
             "cleanup", "visa", "lockin_xx", "lockin_xy", "lockin_sweep",
             "smu_bias", "gate_top", "gate_bottom", "three_smu_run", "combination_scan"}
    known |= configuration.OPTICAL_CONFIG_TABLES
    if set(document) - known:
        raise ValueError("Unknown top-level combination configuration table")
    project = configuration._parse_project(configuration._table(document, "project"))
    if project.mode is not RunMode.HARDWARE:
        raise ValueError("Hardware combination requires project.mode='hardware'")
    table = configuration._table(document, "combination_scan")
    configuration._strict_keys_with_optional(
        table, "combination_scan",
        {"backend", "order", "samples_per_condition", "repeats", "run_name", "note"},
        {"lockin_mode", "run_id", "reference_topology"})
    topology = table.get("reference_topology", "internal_xx_xy")
    if topology not in {"internal_xx_xy", *PHOTONICS_REFERENCE_TOPOLOGIES}:
        raise ValueError("Unsupported combination reference_topology")
    photonics = topology in PHOTONICS_REFERENCE_TOPOLOGIES
    mode = table.get("lockin_mode", "excitation")
    if not isinstance(mode, str) or mode not in {"excitation", "frequency", "frequency_excitation"}:
        raise ValueError("combination_scan.lockin_mode must be excitation, frequency or frequency_excitation")
    if photonics and mode != "excitation":
        raise ValueError("PEM owns the reference frequency; internal frequency scans are unsupported")
    run_id = table.get("run_id", "auto")
    if not isinstance(run_id, str) or not run_id.strip() or run_id != run_id.strip():
        raise ValueError("combination_scan.run_id must be a nonempty string without outer whitespace")
    if table["backend"] != "hardware":
        raise ValueError("Hardware combination requires backend='hardware'")
    order = table["order"]
    if (not isinstance(order, list) or not order
            or not all(isinstance(m, str) and m in {"temperature", "magnetic", "smu", "lockin", "optical"} for m in order)
            or len(set(order)) != len(order)):
        raise ValueError("Hardware order requires distinct temperature/magnetic/smu/lockin/optical axes")
    if photonics and "lockin" not in order:
        raise ValueError("PEM reference topologies require the lockin axis")
    if "optical" in order and "lockin" in order and not photonics:
        raise ValueError("Optical/electrical combination requires an explicit PEM reference topology")
    axes = {}
    smu = lockin = None
    hardware = {}
    temperature = magnetic = cryostat = None
    optical = None
    lockin_limits = None
    if "temperature" in order:
        temperature = configuration.load_temperature_operation_config(path)
        points = (temperature.temperature_scan.points_k if temperature.temperature_scan is not None
                  else (temperature.temperature_run.target_k,))
        # An inner axis returns from its last point to its first on each outer
        # iteration. Include that reset (and repeats), not just ascending steps.
        transitions = list(zip(points, points[1:]))
        if order.index("temperature") > 0 or table["repeats"] != 1:
            transitions.append((points[-1], points[0]))
        if any(abs(b - a) > temperature.temperature_run.max_delta_k for a, b in transitions):
            raise ValueError("Temperature step/reset exceeds temperature_run.max_delta_k")
        axes["temperature"] = ScanAxis("temperature", tuple(
            AxisPoint({"temperature_k": v}, "main", "ordered") for v in points))
        hardware["temperature"] = asdict(temperature)
        hardware["temperature"].update(effective_interrupt_policy="abort",
            qualification="fresh_dwell_after_all_axis_settings", pre_measure_wait_s_used=False)
    if "magnetic" in order:
        magnetic = configuration.load_magnetic_field_operation_config(path)
        segments = magnetic.run.segment_plan
        axes["magnetic"] = ScanAxis("magnetic", tuple(AxisPoint(
            {"field_x_t": point.bx_t, "field_z_t": point.bz_t},
            str(segments.point_segment_indices[i]) if segments else "main",
            segments.segments[segments.point_segment_indices[i]].direction if segments else "ordered")
            for i, point in enumerate(magnetic.run.points)))
        hardware["magnetic"] = asdict(magnetic)
    if temperature is not None or magnetic is not None:
        selected = temperature or magnetic
        limits = selected.magnet.limits
        # A selected magnetic plan supplies its complete-plan axis policy.
        # Temperature-only operation keeps the existing ambient 3 T envelope.
        effective = (limits if magnetic is not None else
                     replace(limits, hardware_z_max_t=min(limits.hardware_z_max_t, 3.0)))
        if photonics or "optical" in order:
            effective = replace(effective,
                hardware_x_max_t=min(effective.hardware_x_max_t, effective.experiment_vector_max_t, 3.0),
                hardware_z_max_t=min(effective.hardware_z_max_t, effective.experiment_vector_max_t, 3.0))
            if magnetic is not None:
                magnetic = replace(magnetic, magnet=replace(magnetic.magnet, limits=effective))
                hardware["magnetic"] = asdict(magnetic)
        cryostat = SimpleNamespace(project=project, cryostat=selected.cryostat,
            magnet=replace(selected.magnet, limits=effective),
            temperature_stability=(temperature.temperature_stability if temperature is not None
                                   else selected.magnet.stability),
            strict_resultant=photonics or "optical" in order)
        hardware["effective_cryostat"] = {name: asdict(value) if hasattr(value, "__dataclass_fields__") else value
                                         for name, value in vars(cryostat).items()}
        if magnetic is not None:
            # Offline check exact float32 endpoints and every component corner
            # under the configured nominal limits; the parser already checked
            # complete-plan single-axis/vector classification.
            plan_ordered_field_transitions(VectorField(0.0, 0.0), magnetic.run.points,
                magnetic.run.max_step_t, magnetic.run.transition_policy, effective)
    if "smu" in order:
        smu = load_three_smu_operation_config(path)
        if smu.plan.mode is ScanMode.SOFTWARE_PULSE:
            raise ValueError("Software-pulse timing is not supported by combination scans")
        points = validate_plan_targets(smu.hardware, smu.plan)
        def values(point):
            return {role + ("_v" if smu.hardware.require_role(role).source_mode is SourceMode.VOLTAGE
                            else "_a"): value for role, value in point.coordinates.items()}
        axes["smu"] = ScanAxis("smu", tuple(
            AxisPoint(values(point), point.segment, "ordered") for point in points))
        hardware["smu"] = {"hardware": asdict(smu.hardware), "plan": asdict(smu.plan),
                           "effective_finish_action": "zero_disable",
                           "sampling_owner": "combination_scan.samples_per_condition"}
    if "lockin" in order and photonics:
        from .photonics_lockin_config import load_photonics_lockin_config
        from .photonics_lockin_points import PhotonicsLockinPointSession
        lockin = load_photonics_lockin_config(path, document=document)
        prepared = PhotonicsLockinPointSession(path, lockin, lambda *_: None, mode=mode)
        axes["lockin"] = ScanAxis("lockin", tuple(AxisPoint({
            "lockin_excitation_v_rms": point.source_v_rms, "lockin_frequency_hz": point.frequency_hz},
            "main", "ordered", point.metadata()) for point in prepared.grid))
        lockin_limits = (lockin.minimum_source_voltage_v, lockin.maximum_source_voltage_v,
                         lockin.reference_min_hz, lockin.reference_max_hz)
        hardware["lockin"] = _json(asdict(lockin))
        hardware["lockin"].update(point_mode=mode, harmonics_by_role=prepared.harmonics_by_role,
                                  skipped_harmonics_by_frequency={})
    elif "lockin" in order:
        safety_path = path.with_name("lockin_safety.toml")
        safety = configuration._parse_lockin_safety(configuration._load_document(safety_path))
        xx = configuration._parse_lockin(configuration._table(document, "lockin_xx"),
                                        LockinRole.XX, "lockin_xx", safety.lockin_xx, safety)
        xy = configuration._parse_lockin(configuration._table(document, "lockin_xy"),
                                        LockinRole.XY, "lockin_xy", safety.lockin_xy, safety)
        configuration._validate_lockin_pair(xx, xy)
        sweep = configuration._parse_lockin_sweep(configuration._table(document, "lockin_sweep"),
                                                  safety, xx, xy)
        lockin = SimpleNamespace(
            lockin_xx=xx, lockin_xy=xy, lockin_safety=safety, lockin_sweep=sweep,
            visa=configuration._parse_visa(configuration._table(document, "visa")))
        prepared = LockinPointSession(path, lockin, lambda *_: None, mode=mode)
        axes["lockin"] = ScanAxis("lockin", tuple(AxisPoint({
            "lockin_excitation_v_rms": point.source_v_rms, "lockin_frequency_hz": point.frequency_hz},
            (f"f{point.frequency_segment}:e{point.excitation_segment}" if mode != "excitation"
             else str(point.excitation_segment) if point.excitation_segment is not None else "main"),
            "ascending", point.metadata()) for point in prepared.grid))
        hardware["lockin"] = {name: asdict(getattr(lockin, name)) for name in vars(lockin)}
        hardware["lockin"]["safety_sha256"] = hashlib.sha256(safety_path.read_bytes()).hexdigest()
        hardware["lockin"]["point_mode"] = mode
        hardware["lockin"]["harmonics_by_role"] = prepared.harmonics_by_role
        hardware["lockin"]["skipped_harmonics_by_frequency"] = prepared.skipped_by_frequency
    if "optical" in order:
        from .optical_points import load_optical_point_config
        optical = load_optical_point_config(path)
        if photonics and not optical.scan.use_pem:
            raise ValueError("PEM reference optical scans require PEM enabled")
        axes["optical"] = ScanAxis("optical", tuple(AxisPoint(values, "main", "ordered")
                                                   for values in optical.points))
        hardware["optical"] = _json(asdict(optical))
    addresses = []
    if smu is not None:
        addresses += [h.address.strip().upper() for h in smu.hardware.by_role().values()]
    if lockin is not None:
        addresses += [lockin.lockin_xx.address.strip().upper(), lockin.lockin_xy.address.strip().upper()]
    if cryostat is not None and cryostat.cryostat.com_port:
        addresses.append(cryostat.cryostat.com_port)
    if optical is not None:
        addresses += [optical.nkt.port] if optical.nkt.port else []
        if optical.pem is not None and optical.pem.port:
            addresses.append(optical.pem.port)
        if optical.pm is not None and optical.pm.resource:
            addresses.append(optical.pm.resource)
    identities = [re.sub(r"^ASRL(\d+)::INSTR$", r"COM\1", value.strip().upper()) for value in addresses]
    if len(set(identities)) != len(identities):
        raise ValueError("All active resources, including optical ports and lock-in roles, must be distinct")
    plan = CombinationPlan(tuple(axes[m] for m in order), table["samples_per_condition"],
                           table["repeats"], table["run_name"], table["note"],
                           magnet_limits=(cryostat.magnet.limits if cryostat else
                                          MagnetLimits(3.0, 3.0, 3.0) if photonics or optical is not None else MagnetLimits()),
                           readback_tolerance_t=cryostat.magnet.readback_tolerance_t if cryostat else None,
                           lockin_coordinate_limits=lockin_limits,
                           strict_resultant=photonics or optical is not None)
    snapshot = plan.snapshot()
    snapshot.update(mode="hardware", hardware_scope="four-module-lockin-grid-v2",
                    lockin_mode=mode,
                    hardware=_json(hardware), config_path=str(path),
                    config_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                    cleanup_policy={"lockin": ("4mV_restore_ranges_h1" if mode == "excitation"
                        else "4mV_h1_restore_baseline_frequency_ranges_reserve"), "smu": "zero_disable",
                        "temperature": "normal_hold_failure_disable",
                        "magnetic": (magnetic.cleanup.normal_end_field_policy.value if magnetic else "inactive"),
                        "magnetic_failure": "monitored_zero_or_manual_verification"},
                    resume_supported=False)
    if photonics:
        snapshot.update(hardware_scope="photonics-lockin-v1", reference_topology=topology)
        snapshot["cleanup_policy"]["lockin"] = "explicit_source_cleanup_preserve_external_reference"
    if optical is not None:
        snapshot["hardware_scope"] = "photonics-combination-v1"
        snapshot["cleanup_policy"]["optical"] = "laser_off_then_electrical_protection_then_pem_finish"
    # Include point engines and shared safety code, not just the Cartesian loop.
    snapshot["source_sha256"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                for p in Path(__file__).parent.glob("*.py")}
    database = project.database_path
    if not database.is_absolute():
        database = path.parent / database
    return HardwareCombinationConfig(path, plan, smu, lockin, snapshot, temperature, magnetic,
                                     cryostat, database.resolve(), run_id, mode, optical, topology)


class _Events:
    def __init__(self, sink):
        self.sink = sink
        self.cleaning = False
        self.errors = []

    def event(self, kind, payload):
        try:
            self.sink(kind, _json(payload))
        except BaseException as exc:
            if not self.cleaning:
                raise
            # Disk/audit failure must never prevent later safety actions.
            self.errors.append(f"{kind}: {type(exc).__name__}: {exc}")


class HardwareCombinationStation:
    def __init__(self, config, *, smu_adapter_factory=None, manager_factory=None, sleep=None,
                 dll=None, monotonic=None, optical_backend_factory=None,
                 pem_factory=None, meter_factory=None):
        self.config = config
        self.smu_adapter_factory = smu_adapter_factory
        self.manager_factory = manager_factory
        self.sleep = sleep
        self.smu = self.lockin = None
        self.optical = None
        self.source_cleanup_verified = False
        self.optical_backend_factory = optical_backend_factory
        self.pem_factory, self.meter_factory = pem_factory, meter_factory
        self.point = None
        self.open_completed = False
        self.cryo = None
        self.dll, self.monotonic = dll, monotonic

    def set_event_sink(self, sink):
        self.events = _Events(sink)

    def open(self, modules):
        if modules != tuple(a.module for a in self.config.plan.axes):
            raise ValueError("Station modules differ from validated plan")
        preflight = {}
        if self.config.cryostat is not None:
            self.cryo = CryostatPointSession(self.config.cryostat, self.config.temperature,
                self.config.magnetic, self.events.event, dll=self.dll,
                monotonic=self.monotonic, sleep=self.sleep)
            preflight["cryostat"] = self.cryo.open()
        if self.config.smu is not None:
            kwargs = {}
            if self.smu_adapter_factory is not None:
                kwargs["adapter_factory"] = self.smu_adapter_factory
            if self.sleep is not None:
                kwargs["sleep"] = self.sleep
            self.smu = ThreeSmuSession.open(
                self.config.smu.hardware, self.config.smu.plan, authorize_writes=True,
                authorize_status_consumption=True,
                on_preflight=lambda role, state: self.events.event(
                    "smu_preflight_role", {"role": role, "state": asdict(state)}), **kwargs)
            preflight["smu"] = {r: asdict(v) for r, v in self.smu.preflight.items()}
            self.events.event("smu_preflight", preflight["smu"])
        if self.config.lockin is not None:
            session_type = LockinPointSession
            if self.config.reference_topology in PHOTONICS_REFERENCE_TOPOLOGIES:
                from .photonics_lockin_points import PhotonicsLockinPointSession
                session_type = PhotonicsLockinPointSession
            kwargs = {"sleep": self.sleep} if self.sleep is not None and session_type is not LockinPointSession else {}
            self.lockin = session_type(
                self.config.path, self.config.lockin, self.events.event,
                manager_factory=self.manager_factory, mode=self.config.lockin_mode, **kwargs)
            preflight["lockin"] = self.lockin.open()
        if self.config.optical is not None:
            from .optical_points import OpticalPointSession
            kwargs = {key: value for key, value in {
                "backend_factory": self.optical_backend_factory, "pem_factory": self.pem_factory,
                "meter_factory": self.meter_factory, "clock": self.monotonic,
                "sleep": self.sleep}.items() if value is not None}
            self.optical = OpticalPointSession(self.config.optical, self.events.event, **kwargs)
            preflight["optical"] = self.optical.open(authorize_writes=True, confirm_manual_route=True)
        # All active preflights must succeed before any configuration writes.
        if self.optical is not None:
            if self.lockin is not None:
                self.lockin.protect_source()
            self.optical.configure()
            # Prepare a reference while laser OFF; HARM/reference setup needs
            # the actual PEM frequency even when modulation starts disabled.
            self.optical.set_point(0)
        if self.lockin is not None:
            self.lockin.configure()
            # The first lock-in axis may request high excitation before an
            # optical axis is visited. Certify the prepared external clock now.
            if self.optical is not None:
                self._verify_optical_reference()
        if self.smu is not None:
            self.smu.begin_points(self.events)
        self.open_completed = True
        return _json({"mode": "hardware", "identities_and_initial_state": preflight})

    def apply(self, module, point):
        # All axis movement occurs with confirmed laser OFF, including electrical
        # inner-axis changes while the optical coordinates themselves stay fixed.
        if self.optical is not None:
            self.optical.suspend()
        if module == "optical":
            axis = next(a for a in self.config.plan.axes if a.module == "optical")
            if self.lockin is not None:
                self.lockin.prepare_reference_transition()
            return self.optical.set_point(axis.points.index(point))
        if module in {"temperature", "magnetic"}:
            return self.cryo.apply(module, point)
        if module == "smu":
            self.point = point
            self.smu.set_point({key[:-2]: v for key, v in point.values.items()},
                               segment=point.segment)
            # Actual sensed coordinates are produced only by the formal read.
            return {"requested": point.values}
        if module != "lockin":
            raise ValueError("Unknown combination module")
        lockin_axis = next(axis for axis in self.config.plan.axes if axis.module == "lockin")
        index = lockin_axis.points.index(point)
        return {"actual": self.lockin.set_point(point.values["lockin_excitation_v_rms"],
            point.values["lockin_frequency_hz"], point_index=index)}

    def qualify(self, modules):
        if self.optical is not None and self.lockin is not None:
            # Prepared PEM frequency must match the external clock while XX is
            # still protected, before restoring excitation or enabling emission.
            self._verify_optical_reference()
            self.lockin.restore_source_after_reference_transition()
        if self.lockin is not None:
            self.lockin.qualify()
        if self.cryo is not None:
            self.cryo.qualify()
        if self.optical is not None:
            self.optical.qualify()
            if self.lockin is not None:
                # PEM activation or power/wavelength changes may disturb the
                # reference; formal electrical qualification follows them.
                self.lockin.qualify()
            self._verify_optical_reference()
        return {"ready": True, "mode": "hardware", "active_modules": modules}

    def begin_sample(self):
        if self.optical is not None:
            self.optical.begin_sample()
            self._verify_optical_reference()
        if self.cryo is not None:
            self.cryo.begin_sample()

    def end_sample(self, reads):
        errors = []
        for device in (self.optical, self.cryo):
            if device is not None:
                try:
                    device.end_sample(reads)
                except BaseException as exc:
                    errors.append(exc)
        if errors:
            for extra in errors[1:]:
                errors[0].add_note(str(extra))
            raise errors[0]
        self._verify_optical_reference()

    def _verify_optical_reference(self):
        if self.config.reference_topology not in PHOTONICS_REFERENCE_TOPOLOGIES or self.optical is None:
            return
        if self.optical.pem is None or self.optical.last_state is None:
            raise ValueError("Prepared PEM reference evidence is missing")
        reference_frequencies = self.lockin.read_reference_frequencies()
        pem_frequency = self.optical.last_state["pem"]["frequency_hz"]
        self.events.event("photonics_reference_check", {
            "pem_frequency_hz": pem_frequency,
            "xx_reference_frequency_hz": reference_frequencies["xx"],
            "xy_reference_frequency_hz": reference_frequencies["xy"],
            "pair_tolerance_hz": self.config.lockin.pair_tolerance_hz,
            "captured_at_utc": utc_now()})
        if any(abs(pem_frequency - value) > self.config.lockin.pair_tolerance_hz for value in reference_frequencies.values()):
            raise ValueError("PEM frequency differs from the lock-in external reference")

    def read(self, module):
        if module == "optical":
            return self.optical.read()
        if module in {"temperature", "magnetic"}:
            return self.cryo.read(module)
        if module == "lockin":
            return _json(self.lockin.sample_point(
                measurement_context=self.cryo.capture if self.cryo is not None else None))
        sample = self.smu.sample_point({k[:-2]: v for k, v in self.point.values.items()},
                                       segment=self.point.segment)
        self.events.event("smu_formal_sample", asdict(sample))
        measured, actual = {}, {}
        for role, timed in sample.readings.items():
            reading = timed.reading
            measured.update({role + "_voltage_v": reading.voltage_v,
                             role + "_current_a": reading.current_a})
            key = role + ("_v" if self.config.smu.hardware.require_role(role).source_mode
                          is SourceMode.VOLTAGE else "_a")
            actual[key] = reading.voltage_v if key.endswith("_v") else reading.current_a
        return {"module": "smu", "captured_at_utc": utc_now(), "actual": actual,
                "measurements": measured, "clean": sample.clean,
                "problems": list(sample.problems), "status": _json(asdict(sample))}

    def expected_measurements(self, condition):
        expected = set()
        if self.config.smu is not None:
            for role in self.config.smu.hardware.by_role():
                expected.update({role + "_voltage_v", role + "_current_a"})
        if self.lockin is not None:
            for role, harmonics in self.lockin.harmonics_by_role.items():
                metrics = (("x_v", "y_v", "amplitude_v") if self.config.reference_topology in PHOTONICS_REFERENCE_TOPOLOGIES
                           else ("x_v", "y_v", "amplitude_v", "phase_deg"))
                expected.update(f"lockin_{role}_h{h}_{metric}" for h in harmonics
                                if h in self.lockin.point_harmonics for metric in metrics)
        if self.optical is not None:
            expected.update(self.optical.expected_measurements())
        return expected

    def cleanup(self, module, failed):
        self.events.cleaning = True
        if module == "optical" and self.optical is not None:
            # Keep the PEM reference active until the source cleanup below.
            result = self.optical.cleanup(finish_pem=False)
            clean = result["verified"]
        elif module == "lockin" and self.lockin is not None:
            result = self.lockin.cleanup()
            clean = result["verified"]
            self.source_cleanup_verified = result.get("source_protection_verified", False) is True
        elif module == "smu" and self.smu is not None:
            result = self.smu.cleanup_points(self.events, reason="failed" if failed else "completed")
            clean = not result["manual_verification_required"]
        elif module in {"temperature", "magnetic"} and self.cryo is not None:
            try:
                result = self.cryo.cleanup(module, failed)
                clean = result["verified"] and not result.get("scan_limits_violated", False)
            except BaseException as exc:
                state = self.cryo.driver.last_confirmed_state if self.cryo.driver is not None else None
                result = {"verified": False, "error": f"{type(exc).__name__}: {exc}",
                          "last_confirmed_state_not_current": asdict(state) if state else None}
                clean = False
        else:
            result, clean = {"attempted": False, "reason": "not opened"}, True
        return _json({"module": module,
                      # VISA backends need not raise OSError. Never certify a
                      # failed hardware attempt just because later cleanup reads
                      # succeed; keep those readbacks but require operator review.
                      "clean": clean and self.open_completed and not self.events.errors and not failed,
                      "failure_requires_manual_review": failed,
                      "startup_completed": self.open_completed,
                      "result": result, "audit_errors": list(self.events.errors)})

    def close(self):
        errors = []
        for device in (self.lockin, self.smu, self.cryo, self.optical):
            if device is not None:
                try:
                    result = (device.close(finish_pem=self.lockin is None or self.source_cleanup_verified)
                              if device is self.optical else device.close())
                    if device is self.optical:
                        self.events.event("optical_final_cleanup", result)
                        if result.get("verified") is not True:
                            errors.append("Optical final cleanup unverified: " + str(result))
                except BaseException as exc:
                    errors.append(f"{type(exc).__name__}: {exc}")
        errors.extend(self.events.errors)
        if errors:
            raise OSError("; ".join(errors))


def run_hardware_combination(config, store, run_id, *, authorize_hardware=False,
                               confirm_xy_sine_disconnected=False,
                               authorize_cryostat=False, dll=None, monotonic=None,
                               smu_adapter_factory=None, manager_factory=None, sleep=None,
                               on_registered=None, authorize_optical=False,
                               confirm_optical_route=False, optical_backend_factory=None,
                               pem_factory=None, meter_factory=None):
    if not authorize_hardware:
        raise ValueError("Explicit combined connection/write/status-consumption authorization required")
    if config.lockin is not None:
        if config.reference_topology == "pem_xy_xx_sine":
            reference_output = config.lockin.reference_output
            if (reference_output is None or reference_output.sample_connected is not False
                    or reference_output.destination != "lockin_xx_ref_in"
                    or config.lockin.lockin_xy.sine_output_connected is not True):
                raise ValueError("XY SINE reference must connect only to XX REF IN and be disconnected from the sample")
        elif not confirm_xy_sine_disconnected:
            raise ValueError("Confirm physical disconnection of XY SINE OUT before opening hardware")
    if config.cryostat is not None and not authorize_cryostat:
        raise ValueError("Separate cryostat temperature/field write authorization required")
    if config.optical is not None:
        if not authorize_optical or not confirm_optical_route:
            raise ValueError("Optical connection/settings/emission authorization and confirmed optical route required")
        config.optical.nkt.require_hardware(writes=True)
    station = HardwareCombinationStation(config, smu_adapter_factory=smu_adapter_factory,
                                           manager_factory=manager_factory, sleep=sleep,
                                           dll=dll, monotonic=monotonic,
                                           optical_backend_factory=optical_backend_factory,
                                           pem_factory=pem_factory, meter_factory=meter_factory)
    snapshot = {**config.snapshot, "authorization": {
        "combined_connection_writes_status_consumption": True,
        "xy_sine_disconnected": (False if config.reference_topology == "pem_xy_xx_sine" else confirm_xy_sine_disconnected),
        "xy_sine_reference_only": config.reference_topology == "pem_xy_xx_sine",
        "cryostat_connection_and_selected_axis_writes": authorize_cryostat,
        "optical_connection_settings_and_emission": authorize_optical,
        "optical_route_confirmed": confirm_optical_route}}
    return _run_combination(config.plan, store, run_id, station=station, snapshot=snapshot,
                            on_registered=on_registered)


# Compatibility for callers of the earlier electrical-only milestone.
ElectricalCombinationConfig = HardwareCombinationConfig
ElectricalCombinationStation = HardwareCombinationStation
load_electrical_combination = load_hardware_combination
run_electrical_combination = run_hardware_combination
