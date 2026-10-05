"""Single-owner, fixed-range point acquisition with PEM-owned external frequency."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import math
import time

from .lockin_backend import create_lockin_backend, capabilities_for
from .models import LockinRole


def _now():
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class PhotonicsLockinGridPoint:
    source_v_rms: float
    frequency_hz: float
    excitation_index: int
    frequency_index: int = 0
    excitation_segment: int | None = None
    frequency_segment: int | None = None

    def metadata(self):
        return {**asdict(self), "frequency_coordinate_source": "expected_external_reference"}


class PhotonicsLockinPointSession:
    def __init__(self, config_path, config, event, *, manager_factory=None, sleep=None, mode="excitation"):
        if mode != "excitation":
            raise ValueError("PEM owns frequency; this profile only scans excitation amplitude.")
        self.config = config
        self.event = event
        self.manager_factory = manager_factory
        self.sleep = time.sleep if sleep is None else sleep
        self.grid = tuple(PhotonicsLockinGridPoint(v, config.reference_expected_hz, i)
                          for i, v in enumerate(config.excitation_points_v_rms))
        self.roles = (config.lockin_xx, config.lockin_xy)
        self.harmonics_by_role = {c.role.value: c.harmonics for c in self.roles}
        self.point_harmonics = tuple(sorted(set(h for c in self.roles for h in c.harmonics)))
        self.skipped_by_frequency = {}
        self.manager = None
        self.resources = []
        self.pair = None
        self.preflight = None
        self.identities = {}
        self.current_harmonics = {c.role.value: c.harmonics[0] for c in self.roles}
        self.audit_cursors = {"xx": 0, "xy": 0}
        self.writes_started = False
        self.configured = False
        self.failed = False
        self.closed = False
        self.cleanup_result = None
        self.point_record = None
        self.target_source = None
        self.sample_count = 0
        self.reference_transition_pending = False
        self.reference_transition_target = None

    def _emit_audit(self, stage, *, suppress=False):
        errors = []
        if self.pair:
            for device in self.pair:
                role = device.role.value
                raw = device.audit[self.audit_cursors[role]:]
                if raw:
                    try:
                        self.event("lockin_model_io", {"stage": stage, "role": "lockin_" + role,
                            "model": device.capabilities.model.value, "raw": [asdict(item) for item in raw]})
                        self.audit_cursors[role] = len(device.audit)
                    except BaseException as exc:
                        if not suppress:
                            self.failed = True
                            raise
                        errors.append(f"audit {role}: {exc}")
        return errors

    def _require_open(self):
        if self.pair is None or self.closed:
            raise RuntimeError("Lock-in session is not open.")

    def _require_ready(self):
        self._require_open()
        if not self.configured or self.failed or self.cleanup_result is not None:
            raise RuntimeError("Lock-in session is not configured or has failed/finished.")

    def open(self):
        if self.manager is not None or self.closed:
            raise RuntimeError("A lock-in session can only be opened once.")
        factory = self.manager_factory
        if factory is None:
            def factory():
                import pyvisa
                return pyvisa.ResourceManager() if self.config.visa.backend == "default" else pyvisa.ResourceManager(self.config.visa.backend)
        try:
            self.manager = factory()
            devices = []
            for cfg in self.roles:
                resource = self.manager.open_resource(cfg.address)
                self.resources.append(resource)
                resource.timeout = self.config.visa.timeout_ms
                resource.read_termination = "\n"
                resource.write_termination = "\n"
                devices.append(create_lockin_backend(model=cfg.model, role=cfg.role, resource=resource))
            self.pair = tuple(devices)
            # Both identities are verified before any configuration is attempted.
            self.identities = {device.role.value: device.read_identity() for device in self.pair}
            if len(set(self.identities.values())) != 2:
                raise ValueError("Both roles returned the same physical instrument identity.")
            self.preflight = {device.role.value: asdict(device.read_settings()) for device in self.pair}
            source = self.pair[0].read_source_state()
            self._check_source(source, expected=None)
            reference_output = self._verify_reference_output()
            self.event("lockin_preflight", {"roles": self.preflight, "source": source,
                "source_wiring": asdict(self.config.source), "reference_topology": self.config.reference_topology,
                "reference_output": reference_output})
            self._emit_audit("open")
            return {"roles": self.preflight, "source": source, "source_wiring": asdict(self.config.source),
                    "reference_output": reference_output}
        except BaseException as exc:
            self.failed = True
            self._emit_audit("open_failed", suppress=True)
            try:
                self.close()
            except BaseException as close_error:
                exc.add_note(f"Resource cleanup also failed: {close_error}")
            raise

    def _check_source(self, source, *, expected):
        value = source["source_voltage_v"]
        if not self.config.minimum_source_voltage_v <= value <= self.config.maximum_source_voltage_v:
            raise ValueError("Actual XX source amplitude exceeds the approved policy.")
        if source["dc_offset_v"] != 0 or source["dc_mode"] != self.config.source.dc_mode:
            raise ValueError("XX source zero-DC/mode readback is not confirmed.")
        if expected is not None and not math.isclose(value, expected, rel_tol=1e-6, abs_tol=1e-12):
            raise ValueError("XX source amplitude differs from the selected point.")

    def _source(self, value, *, protect=False):
        if self.pair[0].read_identity() != self.identities["xx"]:
            raise ValueError("XX identity changed; no excitation write is allowed.")
        if not protect:
            self._verify_reference_output()
        return self.pair[0].set_source_amplitude(value,
            minimum_v=self.config.minimum_source_voltage_v,
            maximum_v=self.config.maximum_source_voltage_v,
            expected_dc_mode=self.config.source.dc_mode,
            authorize_writes=True, protect=protect)

    def _verify_reference_output(self):
        """Preserve XY SINE amplitude/DC; it is a clock, never sample excitation."""
        cfg = self.config.reference_output
        if cfg is None:
            return None
        if self.pair[1].read_identity() != self.identities["xy"]:
            raise ValueError("XY reference identity changed.")
        state = self.pair[1].read_source_state()
        if (not math.isclose(state["source_voltage_v"], cfg.amplitude_v_rms, rel_tol=1e-6, abs_tol=1e-12)
                or state["dc_offset_v"] != cfg.dc_offset_v or state["dc_mode"] != cfg.dc_mode):
            raise ValueError("XY SINE reference amplitude/DC differs from its preserved configuration.")
        return {"configuration": asdict(cfg), "readback": state, "policy": "preserve_read_only"}

    def protect_source(self):
        """Confirm the approved low AC output before a PEM frequency transition."""
        self._require_open()
        if self.failed or self.cleanup_result is not None:
            raise RuntimeError("Cannot begin source protection after session failure/cleanup.")
        try:
            self.writes_started = True
            source = self._source(self.config.cleanup_source_voltage_v, protect=True)
            self._check_source(source, expected=None)
            if source["source_voltage_v"] > self.config.cleanup_source_voltage_v + 1e-12:
                raise ValueError("Source protection target is not confirmed.")
            result = {"verified": True, "source": source, "source_wiring": asdict(self.config.source)}
            self.event("lockin_source_protected", result)
            return result
        except BaseException:
            self.failed = True
            raise
        finally:
            self._emit_audit("protect_source", suppress=self.failed)

    def prepare_reference_transition(self):
        """Hold approved low AC while PEM settings can disturb the external clock."""
        self._require_ready()
        if self.reference_transition_pending:
            raise RuntimeError("A PEM reference transition is already pending.")
        try:
            self._verify_settings()
            self._coordinates()
            self._pair_samples(require_clean=True)
            desired = self.target_source
            protection = self.protect_source()
            self.reference_transition_target = desired
            self.target_source = protection["source"]["source_voltage_v"]
            self.reference_transition_pending = True
            return {**protection, "restore_source_voltage_v": desired}
        except BaseException:
            self.failed = True
            raise
        finally:
            self._emit_audit("prepare_reference_transition", suppress=self.failed)

    def restore_source_after_reference_transition(self):
        """Requalify the new reference before restoring the selected excitation."""
        self._require_ready()
        if not self.reference_transition_pending:
            return self.read_coordinates()
        try:
            self._verify_settings()
            self._coordinates()
            self._transition(allow_reference_unlock=True)
            target = self.reference_transition_target
            if target is not None:
                self._source(target)
                self.target_source = target
                self._transition()
            self.reference_transition_pending = False
            self.reference_transition_target = None
            result = self._coordinates()[0]
            self.event("lockin_reference_transition_completed", {"actual": result})
            return result
        except BaseException:
            self.failed = True
            raise
        finally:
            self._emit_audit("restore_after_reference_transition", suppress=self.failed)

    def _check_frequencies(self, frequencies):
        for cfg in self.roles:
            value = frequencies[cfg.role.value]
            if not self.config.reference_min_hz <= value <= self.config.reference_max_hz:
                raise ValueError("Actual external reference left the approved interval.")
            for harmonic in cfg.harmonics:
                capabilities_for(cfg.model).validate_detection(value, harmonic)
        if abs(frequencies["xx"] - frequencies["xy"]) > self.config.pair_tolerance_hz:
            raise ValueError("XX and XY external references disagree.")

    def _coordinates(self):
        source = self.pair[0].read_source_state()
        self._check_source(source, expected=self.target_source)
        reference_output = self._verify_reference_output()
        frequencies = {device.role.value: device.read_reference_frequency() for device in self.pair}
        self._check_frequencies(frequencies)
        return {"lockin_excitation_v_rms": source["source_voltage_v"],
                "lockin_frequency_hz": frequencies["xx"]}, {"source": source, "reference_hz": frequencies,
                                                           "reference_output": reference_output}

    def read_coordinates(self):
        """Guarded public readback for cross-checking the PEM reference owner."""
        self._require_ready()
        try:
            self._verify_settings()
            return self._coordinates()[0]
        except BaseException:
            self.failed = True
            raise
        finally:
            self._emit_audit("read_coordinates", suppress=self.failed)

    def read_reference_frequencies(self):
        """Read and validate both role references for comparison with the PEM."""
        self._require_ready()
        try:
            self._verify_settings()
            return self._coordinates()[1]["reference_hz"]
        except BaseException:
            self.failed = True
            raise
        finally:
            self._emit_audit("read_reference_frequencies", suppress=self.failed)

    def _verify_settings(self):
        result = {}
        for cfg, device in zip(self.roles, self.pair):
            actual = device.read_settings()
            if actual.identity != self.identities[cfg.role.value]:
                raise ValueError("Instrument identity changed inside the owned session.")
            if (actual.reference_source != "external" or actual.time_constant_s != cfg.time_constant_s
                    or actual.sensitivity_full_scale_v != cfg.sensitivity_full_scale_v
                    or actual.harmonic != self.current_harmonics[cfg.role.value]):
                raise ValueError("Lock-in reference/filter/range/harmonic settings changed unexpectedly.")
            native = actual.native_settings
            if cfg.model == "SR830":
                expected_edge = {"sine_zero_crossing": 0, "rising": 1, "falling": 2}[cfg.external_reference_edge]
                reserve = {"high_reserve": 0, "normal": 1, "low_noise": 2}[cfg.reserve_mode]
                correct = (native.reference_slope == expected_edge and native.input_mode == 1
                    and native.shield_grounding == (0 if cfg.shield_grounding == "float" else 1) and native.input_coupling == 0
                    and native.line_filter == 0 and native.filter_slope == 3 and native.reserve_mode == reserve)
            else:
                correct = (native.external_reference_edge == cfg.external_reference_edge
                    and native.reference_input_impedance_ohm == cfg.reference_input_impedance_ohm
                    and native.input_range_v_peak == cfg.input_range_v_peak
                    and not native.advanced_filter and not native.synchronous_filter
                    and native.filter_slope_db_oct == 24 and native.input_mode == "a_minus_b"
                    and native.input_coupling == "ac" and native.shield_grounding == cfg.shield_grounding
                    and native.sync_output_mode == (self.preflight[cfg.role.value]["native_settings"]["sync_output_mode"]
                        if cfg.sync_output_mode == "preserve" else cfg.sync_output_mode))
            if not correct or not math.isclose(native.phase_shift_deg, cfg.phase_shift_deg, abs_tol=0.001, rel_tol=0):
                raise ValueError("Lock-in model-specific settings differ from the configured profile.")
            result[cfg.role.value] = asdict(actual)
        return result

    def _pair_samples(self, *, require_clean, allow_reference_unlock=False):
        result = {}
        for cfg, device in zip(self.roles, self.pair):
            sample = device.read_sample(consume_status_latches=True,
                                       current_status_supported=cfg.current_status_supported)
            result[cfg.role.value] = sample
            # Emit each role immediately so a companion failure retains this sample.
            self.event("lockin_raw_role", {"role": "lockin_" + cfg.role.value,
                "formal_candidate": require_clean, "sample": asdict(sample)})
            if sample.harmonic != self.current_harmonics[cfg.role.value]:
                raise ValueError("Unexpected harmonic during lock-in acquisition.")
            if not require_clean and (sample.status.input_overload is True
                    or sample.status.output_scale_overload is True or sample.status.instrument_error is True):
                raise ValueError("Overload or instrument error cannot be discarded as a setting transition.")
            if not require_clean:
                if cfg.model == "SR830":
                    latched = sample.status.native_status[0]
                    if latched is None or latched.raw & 0x80 or latched.any_overload:
                        raise ValueError("Unknown/overloaded SR830 transition status.")
                    unlocked = latched.reference_unlocked
                else:
                    latched = sample.status.native_status
                    if (latched.unknown_status_bits or latched.input_overload_latched
                            or latched.output_scale_overload_latched or latched.filter_fault_latched
                            or latched.power_on_latched):
                        raise ValueError("Faulted/unknown SR865A transition status.")
                    unlocked = latched.reference_unlock_latched or latched.locked is False
                if unlocked and not allow_reference_unlock:
                    raise ValueError("Unexpected reference unlock during an excitation transition.")
            if require_clean and sample.status.clean is not True:
                raise ValueError(f"lockin_{cfg.role.value} status is faulted or unknown.")
        self._check_frequencies({role: sample.reference_frequency_hz for role, sample in result.items()})
        return result

    def _transition(self, *, allow_reference_unlock=False):
        # Transition samples retain the old latch window and are never accepted.
        self._pair_samples(require_clean=False, allow_reference_unlock=allow_reference_unlock)
        self.sleep(self.config.settle_s)
        self._pair_samples(require_clean=True)
        self._verify_settings()
        self._coordinates()

    def configure(self):
        self._require_open()
        if self.configured or self.failed or self.cleanup_result is not None:
            raise RuntimeError("Configuration cannot be repeated or resumed after failure.")
        try:
            for device in self.pair:
                if device.read_identity() != self.identities[device.role.value]:
                    raise ValueError("Instrument identity changed before configuration.")
            source = self.protect_source()["source"]
            for cfg, device in zip(self.roles, self.pair):
                device.configure_external_reference(edge=cfg.external_reference_edge,
                    input_impedance_ohm=cfg.reference_input_impedance_ohm,
                    sync_output_mode=cfg.sync_output_mode, reference_source=cfg.reference_source,
                    authorize_writes=True)
                device.configure_fixed_measurement(cfg, authorize_writes=True)
            # Validate every role before the first harmonic-setting write.
            frequencies = {device.role.value: device.read_reference_frequency() for device in self.pair}
            self._check_frequencies(frequencies)
            for cfg, device in zip(self.roles, self.pair):
                device.set_harmonic(self.current_harmonics[cfg.role.value], authorize_writes=True)
            self._transition(allow_reference_unlock=True)
            self.configured = True
            self.event("lockin_configured", {"reference_topology": self.config.reference_topology,
                "harmonics_by_role": self.harmonics_by_role, "source": source})
        except BaseException:
            self.failed = True
            raise
        finally:
            self._emit_audit("configure", suppress=self.failed)

    def set_point(self, source_v, frequency_hz=None, *, point_index=None):
        self._require_ready()
        frequency = self.config.reference_expected_hz if frequency_hz is None else frequency_hz
        if point_index is None:
            point_index = next((i for i, p in enumerate(self.grid)
                if (p.source_v_rms, p.frequency_hz) == (source_v, frequency)), -1)
        if type(point_index) is not int or not 0 <= point_index < len(self.grid):
            raise ValueError("Point is outside the validated photonics lock-in grid.")
        point = self.grid[point_index]
        if isinstance(source_v, bool) or (point.source_v_rms, point.frequency_hz) != (source_v, frequency):
            raise ValueError("Point coordinates do not match the configured grid; external frequency cannot be swept.")
        try:
            if self.reference_transition_pending:
                # An outer optical axis may move before the inner excitation
                # axis is selected. Keep AC protected until PEM requalification.
                self.reference_transition_target = source_v
                self.point_record = {"point_index": point_index, **point.metadata(), "samples": [],
                                     "source_wiring": asdict(self.config.source)}
                self.sample_count = 0
                self.event("lockin_excitation_deferred", {"target_source_voltage_v": source_v,
                    "reason": "PEM_reference_transition", "point_index": point_index})
                return self.read_coordinates()
            self._verify_settings()
            self._coordinates()
            self._pair_samples(require_clean=True)
            self._source(source_v)
            self.target_source = source_v
            self.point_record = {"point_index": point_index, **point.metadata(), "samples": [],
                                 "source_wiring": asdict(self.config.source)}
            self._transition()
            self.sample_count = 0
            return self._coordinates()[0]
        except BaseException:
            self.failed = True
            raise
        finally:
            self._emit_audit("set_point", suppress=self.failed)

    def qualify(self):
        self._require_ready()
        if self.reference_transition_pending:
            raise RuntimeError("PEM reference must be requalified before restoring excitation.")
        if self.point_record is None:
            raise RuntimeError("A source point must be selected before qualification.")
        try:
            self.sleep(self.config.settle_s)
            self._verify_settings()
            actual, raw = self._coordinates()
            self._pair_samples(require_clean=True)
            self.event("lockin_point_qualification", {"actual": actual, "raw": raw})
            self.sample_count = 0
            return actual
        except BaseException:
            self.failed = True
            raise
        finally:
            self._emit_audit("qualify", suppress=self.failed)

    def sample_point(self, *, measurement_context=None):
        self._require_ready()
        if self.reference_transition_pending:
            raise RuntimeError("No formal sample is allowed during a PEM reference transition.")
        if self.point_record is None:
            raise RuntimeError("A source point must be selected before sampling.")
        record = {**self.point_record, "schema_version": "photonics-lockin-v1", "samples": [],
                  "identities": dict(self.identities), "sequential_role_reads": True}
        measured = {}
        try:
            if self.sample_count:
                self.sleep(self.config.sample_interval_s)
            for index in range(max(len(cfg.harmonics) for cfg in self.roles)):
                selected = {cfg.role.value: cfg.harmonics[index] for cfg in self.roles if index < len(cfg.harmonics)}
                self._verify_settings()
                self._coordinates()
                self._pair_samples(require_clean=True)
                changed = any(self.current_harmonics[role] != harmonic for role, harmonic in selected.items())
                if changed:
                    for cfg, device in zip(self.roles, self.pair):
                        if cfg.role.value in selected:
                            harmonic = selected[cfg.role.value]
                            device.set_harmonic(harmonic, authorize_writes=True)
                            self.current_harmonics[cfg.role.value] = harmonic
                    self._transition(allow_reference_unlock=True)
                settings_before = self._verify_settings()
                actual, coordinate_before = self._coordinates()
                before = None if measurement_context is None else measurement_context("before", None, self.sample_count)
                pair = self._pair_samples(require_clean=True)
                after = None if measurement_context is None else measurement_context("after", None, self.sample_count)
                actual, coordinate_after = self._coordinates()
                settings_after = self._verify_settings()
                self._pair_samples(require_clean=True)
                entry = {"selected_harmonics": selected, "samples": {r: asdict(v) for r, v in pair.items()},
                    "coordinates_before": coordinate_before, "coordinates_after": coordinate_after,
                    "settings_before": settings_before, "settings_after": settings_after,
                    "measurement_context": {"before": before, "after": after}}
                record["samples"].append(entry)
                self.event("lockin_formal_pair", entry)
                for role, harmonic in selected.items():
                    reading = pair[role]
                    for metric in ("x_v", "y_v", "amplitude_v", "phase_deg"):
                        value = getattr(reading, metric)
                        if value is not None:
                            measured[f"lockin_{role}_h{harmonic}_{metric}"] = value
            self.sample_count += 1
            return {"module": "lockin", "captured_at_utc": _now(), "actual": actual,
                "measurements": measured, "clean": True, "valid_for_analysis": True,
                "problems": [], "status": record}
        except BaseException:
            self.failed = True
            raise
        finally:
            try:
                self.event("lockin_point_samples", record)
            except BaseException:
                self.failed = True
                raise
            finally:
                self._emit_audit("sample_point", suppress=self.failed)

    def cleanup(self):
        if self.cleanup_result is not None:
            return self.cleanup_result
        if not self.writes_started:
            return {"attempted": False, "verified": True, "source_protection_verified": False,
                    "errors": []}
        result = {"attempted": True, "verified": False, "errors": [],
                  "source_protection_verified": False,
                  "reference_policy": "preserve_external", "source_wiring": asdict(self.config.source)}
        try:
            source = self._source(self.config.cleanup_source_voltage_v, protect=True)
            result["source"] = source
            if source["source_voltage_v"] > self.config.cleanup_source_voltage_v + 1e-12:
                raise ValueError("Cleanup source amplitude is not confirmed below the explicit target.")
            if source["dc_offset_v"] != 0 or source["dc_mode"] != self.config.source.dc_mode:
                raise ValueError("Cleanup AC protection does not establish safe source DC state.")
            result["source_protection_verified"] = True
        except BaseException as exc:
            result["errors"].append(f"source: {type(exc).__name__}: {exc}")
        if self.config.reference_output is not None:
            result["reference_output_preserved"] = False
            try:
                result["reference_output"] = self._verify_reference_output()
                result["reference_output_preserved"] = True
            except BaseException as exc:
                result["errors"].append(f"reference output: {type(exc).__name__}: {exc}")
        for device in self.pair or ():
            try:
                state = device.read_settings()
                result["lockin_" + device.role.value] = asdict(state)
                if state.reference_source != "external":
                    raise ValueError("External-reference ownership was not preserved.")
                if self.config.reference_topology == "pem_xy_xx_sine":
                    native = state.native_settings
                    if device.role is LockinRole.XX:
                        if native.reference_slope != 0:
                            raise ValueError("XX SINE reference trigger was not preserved.")
                    elif (native.external_reference_edge != self.config.lockin_xy.external_reference_edge
                            or native.reference_input_impedance_ohm != self.config.lockin_xy.reference_input_impedance_ohm
                            or native.sync_output_mode != self.preflight["xy"]["native_settings"]["sync_output_mode"]):
                        raise ValueError("XY external reference or preserved BlazeX state changed.")
            except BaseException as exc:
                result["errors"].append(f"{device.role.value}: {type(exc).__name__}: {exc}")
        result["errors"].extend(self._emit_audit("cleanup", suppress=True))
        result["verified"] = not result["errors"]
        result["manual_verification_required"] = not result["verified"]
        self.cleanup_result = result
        self.configured = False
        return result

    def close(self):
        errors = []
        for resource in reversed(self.resources):
            try:
                resource.close()
            except BaseException as exc:
                errors.append(exc)
        self.resources = []
        if self.manager is not None:
            try:
                self.manager.close()
            except BaseException as exc:
                errors.append(exc)
        self.manager = None
        self.closed = True
        if errors:
            raise RuntimeError("Lock-in resource close failed: " + "; ".join(str(e) for e in errors))
