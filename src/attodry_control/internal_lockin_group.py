"""Three owned detectors: an internal XX SR830 clock and two TTL receivers.

Uses the existing physical-unit adapters and audited photonics sampling windows.
The legacy dual-SR830 session remains the default when AUX is disabled.
"""
from dataclasses import asdict, replace
import math

from .models import LockinRole
from .photonics_lockin_config import (
    PhotonicsLockinConfig, PhotonicsLockinRoleConfig, PhotonicsSourceConfig,
    PhotonicsVisaConfig, _role_config, _keys, _number,
)
from .photonics_lockin_points import PhotonicsLockinPointSession
from .lockin_points import LockinPointSession
from . import lockin_test as daily
from .lockin_backend import capabilities_for


def load_auxiliary_config(document, safety_document, reference_hz):
    table = document.get("lockin_aux")
    if table is None:
        return None
    if not isinstance(table, dict) or type(table.get("enabled")) is not bool:
        raise ValueError("lockin_aux.enabled must be explicitly true or false")
    if not table["enabled"]:
        return None
    if table.get("model") != "SR865A":
        raise ValueError("The optional third channel requires SR865A")
    label = table.get("channel_label")
    if not isinstance(label, str) or not label.strip():
        raise ValueError("lockin_aux.channel_label must be a nonempty name")
    policy = safety_document.get("lockin_aux")
    if not isinstance(policy, dict):
        raise ValueError("Enabled AUX requires an explicit lockin_aux safety policy")
    _keys(policy, {"allowed_fixed_full_scales_v"}, name="lockin_aux safety")
    scales = policy["allowed_fixed_full_scales_v"]
    if not isinstance(scales, list) or not scales or len(set(scales)) != len(scales):
        raise ValueError("AUX allowed full scales must be a nonempty unique list")
    for v in scales:
        if _number(v, "AUX full scale", positive=True) not in capabilities_for("SR865A").sensitivity_full_scales_v:
            raise ValueError("AUX policy includes an unsupported SR865A sensitivity")
    fields = {k: v for k, v in table.items() if k not in ("enabled", "channel_label")}
    cfg = _role_config(fields, LockinRole.AUX, scales, reference_hz, "internal_xx_xy")
    if cfg.reference_input_impedance_ohm != 1_000_000:
        raise ValueError("Passive XX TTL fan-out requires the AUX high-impedance reference input")
    return replace(cfg, channel_label=label.strip())


def group_configuration(config, prepared):
    """Resolve only the explicitly enabled fixed-range internal-clock profile."""
    sweep = config.lockin_sweep
    if prepared.mode != "excitation":
        raise ValueError("Three-channel internal reference currently supports excitation points only")
    roles = []
    for cfg in (config.lockin_xx, config.lockin_xy):
        if (cfg.sensitivity_mode.value != "fixed" or cfg.harmonic_settings
                or cfg.filter_slope_db_oct != 24 or cfg.input_mode.value != "a_minus_b"
                or cfg.input_coupling.value != "ac"):
            raise ValueError("Three-channel profile requires fixed sensitivity, A-B/AC, 24 dB RC; per-harmonic ranges are unsupported")
        if not prepared.harmonics_by_role[cfg.role.value]:
            raise ValueError("Every enabled detector requires at least one measurement harmonic")
        roles.append(PhotonicsLockinRoleConfig(
            cfg.role, cfg.model, cfg.address, cfg.reference_source.value,
            "" if cfg.external_reference_edge is None else cfg.external_reference_edge.value,
            cfg.sine_output_connected, cfg.input_mode.value, cfg.shield_grounding.value,
            cfg.input_coupling.value, cfg.time_constant_s, cfg.filter_slope_db_oct,
            "fixed", cfg.sensitivity_full_scale_v, 0.0,
            tuple(prepared.harmonics_by_role[cfg.role.value]), cfg.reserve_mode.value,
            None, None, None, False, cfg.channel_label))
    if any(p.range_spec.xx_full_scale_v is not None or p.range_spec.xy_full_scale_v is not None
           for p in prepared.grid):
        raise ValueError("Three-channel profile does not accept per-point range overrides")
    aux = config.lockin_aux
    if aux is not None:
        caps = capabilities_for(aux.model)
        for harmonic in aux.harmonics:
            caps.validate_detection(config.lockin_xx.frequency_hz, harmonic)
    safety = config.lockin_safety
    tau = max(c.time_constant_s for c in (*roles, aux) if c is not None)
    frequency = config.lockin_xx.frequency_hz
    return PhotonicsLockinConfig(
        schema_version=1, lockin_xx=roles[0], lockin_xy=roles[1],
        visa=PhotonicsVisaConfig(config.visa.backend, config.visa.timeout_ms),
        source=PhotonicsSourceConfig("single_ended", "high_impedance", "sr830_instrument_rms_setting", "not_supported", 0),
        excitation_points_v_rms=tuple(p.source_v_rms for p in prepared.grid),
        reference_min_hz=frequency, reference_max_hz=frequency, reference_expected_hz=frequency,
        pair_tolerance_hz=max(0.001, frequency * 1e-5),
        settle_time_constants=sweep.settle_time_constants,
        sample_interval_s=tau * sweep.sample_interval_time_constants,
        minimum_source_voltage_v=safety.minimum_source_voltage_v_rms,
        maximum_source_voltage_v=safety.maximum_source_voltage_v_rms,
        cleanup_source_voltage_v=safety.cleanup_source_voltage_v_rms,
        safety_path="", safety_sha256="", reference_topology="internal_xx_xy",
        overload_policy=sweep.overload_policy, reference_unlock_policy="abort",
        lockin_aux=aux)


class InternalLockinGroupSession(PhotonicsLockinPointSession):
    """Single owner; all detectors share one settling deadline and serial bus."""
    _check_wait = LockinPointSession._check_wait
    _wait = LockinPointSession._wait

    def __init__(self, config_path, config, event, *, manager_factory=None,
                 monotonic=None, sleep=None, mode="excitation"):
        prepared = LockinPointSession(config_path, config, lambda *_: None,
                                     mode=mode, monotonic=monotonic, sleep=sleep)
        resolved = group_configuration(config, prepared)
        super().__init__(config_path, resolved, event, manager_factory=manager_factory,
                         clock=monotonic, sleep=sleep, mode=mode)
        self.grid = prepared.grid
        self._source_args = prepared.args
        self.target_frequency_hz = config.lockin_xx.frequency_hz
        self.wait_guard = self.wait_deadline = None

    def _expected_reference_source(self, cfg):
        return "internal" if cfg.role is LockinRole.XX else "external"

    def _check_source(self, source, *, expected):
        super()._check_source(source, expected=expected)
        daily._validate_excitation_safety(self._source_args, (source["source_voltage_v"],))

    def open(self):
        result = super().open()
        # The existing two SR830 phase settings are preserved, not reset by
        # introducing a third detector. AUX has its explicitly configured phase.
        self.roles = tuple(replace(cfg, phase_shift_deg=self.preflight[cfg.role.value]["native_settings"]["phase_shift_deg"])
                           if cfg.role is not LockinRole.AUX else cfg for cfg in self.roles)
        self.config = replace(self.config, lockin_xx=self.roles[0], lockin_xy=self.roles[1])
        return result

    def _check_frequencies(self, frequencies):
        for cfg in self.roles:
            f = frequencies[cfg.role.value]
            for harmonic in cfg.harmonics:
                capabilities_for(cfg.model).validate_detection(f, harmonic)
            if not math.isclose(f, self.target_frequency_hz, rel_tol=1e-5, abs_tol=0.001):
                raise ValueError(f"lockin_{cfg.role.value} reference differs from the XX internal clock")
            if not math.isclose(f, frequencies["xx"], rel_tol=1e-5, abs_tol=0.001):
                raise ValueError(f"lockin_{cfg.role.value} reference disagrees with the actual XX readback")

    def _coordinates(self, *, wait_for_lock=True, verify_companion=True):
        if self.wait_deadline is not None and self.clock() >= self.wait_deadline:
            raise TimeoutError("Lock-in readback exhausted the operation deadline")
        source = self.pair[0].read_source_state()
        self._check_source(source, expected=self.target_source)
        devices = self.pair if verify_companion else self.pair[:1]
        frequencies = {d.role.value: d.read_reference_frequency() for d in devices}
        if verify_companion:
            self._check_frequencies(frequencies)
        else:
            capabilities_for("SR830").validate_detection(frequencies["xx"], 1)
            if not math.isclose(frequencies["xx"], self.target_frequency_hz, rel_tol=1e-5, abs_tol=0.001):
                raise ValueError("XX internal source clock differs from the selected point")
        if self.wait_deadline is not None and self.clock() >= self.wait_deadline:
            raise TimeoutError("Lock-in readback exhausted the operation deadline")
        return {"lockin_excitation_v_rms": source["source_voltage_v"],
                "lockin_frequency_hz": frequencies["xx"]}, {"source": source, "reference_hz": frequencies}

    def read_coordinates(self, *, verify_companion=True):
        self._require_ready()
        try:
            self._verify_settings()
            return self._coordinates(verify_companion=verify_companion)[0]
        except BaseException as error:
            self._fail(error)
            raise
        finally:
            self._emit_audit("read_coordinates", suppress=self.failed)

    def configure(self):
        self._require_open()
        if self.configured or self.failed:
            raise RuntimeError("Cannot configure a finished/failed group")
        try:
            self.protect_source()
            for cfg, device in zip(self.roles, self.pair):
                if device.read_identity() != self.identities[cfg.role.value]:
                    raise ValueError("Instrument identity changed before configuration")
                start = len(device.audit)
                if cfg.role is LockinRole.XX:
                    device.configure_source_clock(self.target_frequency_hz, authorize_writes=True)
                else:
                    device.configure_external_reference(edge=cfg.external_reference_edge,
                        input_impedance_ohm=cfg.reference_input_impedance_ohm,
                        sync_output_mode=cfg.sync_output_mode, authorize_writes=True)
                device.configure_fixed_measurement(cfg, authorize_writes=True)
                if any(io.operation == "write" for io in device.audit[start:]):
                    self.disturbed("measurement_configuration", roles=("lockin_" + cfg.role.value,))
            # Owned configuration latches are archived before the formal window.
            # No detector settle is paid before the optical owner tunes the beam.
            for cfg, device in zip(self.roles, self.pair):
                status = device.read_reference_status(current_status_supported=cfg.current_status_supported)
                problems = self._status_problems(cfg, status, allow_reference_unlock=True, allow_settings_change=True)
                if self.status_policy.apply({}, problems):
                    raise ValueError("Unknown/faulted configuration status: " + "; ".join(problems))
                self.event("lockin_configuration_status_baseline", {
                    "role": cfg.role.value, "status": asdict(status), "valid_for_analysis": False})
            self.configured = True
            self._configuration_settings_applied = True
            self.disturbed("acquisition_initialization")
            self.event("lockin_configured", {"active_roles": [c.role.value for c in self.roles],
                       "reference_topology": "internal_xx_xy", "harmonics_by_role": self.harmonics_by_role})
        except BaseException as error:
            self._fail(error)
            raise
        finally:
            self._emit_audit("configure", suppress=self.failed)

    def verify_ready(self):
        self._require_ready()
        try:
            self._verify_settings()
            result = {"ready": True, "actual": self._coordinates(verify_companion=False)[0],
                      "companion_reference_qualified": False, "settling_performed": False}
            self.event("lockin_readiness", result)
            return result
        except BaseException as error:
            self._fail(error)
            raise
        finally:
            self._emit_audit("verify_ready", suppress=self.failed)

    def cleanup(self):
        already_finished = self.cleanup_result is not None
        result = super().cleanup()
        if already_finished or not self._configuration_settings_applied:
            return result
        try:
            result["final_group_settings"] = self._verify_settings()
        except BaseException as error:
            result["errors"].append(f"group settings: {type(error).__name__}: {error}")
        result["errors"].extend(self._emit_audit("group_cleanup_verification", suppress=True))
        result["verified"] = not result["errors"]
        result["manual_verification_required"] = not result["verified"]
        return result

    def set_point(self, source_v, frequency_hz=None, *, point_index=None):
        self._require_ready()
        frequency = self.target_frequency_hz if frequency_hz is None else frequency_hz
        if point_index is None:
            point_index = next((i for i,p in enumerate(self.grid)
                                if (p.source_v_rms,p.frequency_hz)==(source_v,frequency)), -1)
        if type(point_index) is not int or not 0 <= point_index < len(self.grid):
            raise ValueError("Point is outside the validated three-channel grid")
        point = self.grid[point_index]
        if isinstance(source_v, bool) or (point.source_v_rms, point.frequency_hz) != (source_v, frequency):
            raise ValueError("Point coordinates do not match the validated grid")
        try:
            self._verify_settings()
            self._source(source_v)
            self.target_source = source_v
            self.point_record = {"point_index": point_index, **point.metadata(),
                                 "samples": [], "source_wiring": asdict(self.config.source)}
            self.sample_count = 0
            return self._coordinates(verify_companion=False)[0]
        except BaseException as error:
            self._fail(error)
            raise
        finally:
            self._emit_audit("set_point", suppress=self.failed)

