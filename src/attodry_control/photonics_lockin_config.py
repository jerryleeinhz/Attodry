"""Strict, offline-only configuration for the PEM-owned lock-in reference chain."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import tomllib
from typing import Mapping

from .config import _parse_sweep_ranges
from .lockin_backend import LockinModel, capabilities_for
from .models import LockinRole
from .sr865a_settings import INPUT_RANGES_V_PEAK

PHOTONICS_REFERENCE_TOPOLOGIES = frozenset({"pem_xx_xy", "pem_xy_xx_sine"})


@dataclass(frozen=True, slots=True)
class PhotonicsLockinRoleConfig:
    role: LockinRole
    model: str
    address: str
    reference_source: str
    external_reference_edge: str
    sine_output_connected: bool
    input_mode: str
    shield_grounding: str
    input_coupling: str
    time_constant_s: float
    filter_slope_db_oct: int
    sensitivity_mode: str
    sensitivity_full_scale_v: float
    phase_shift_deg: float
    harmonics: tuple[int, ...]
    reserve_mode: str | None
    input_range_v_peak: float | None
    reference_input_impedance_ohm: float | None
    sync_output_mode: str | None
    current_status_supported: bool


@dataclass(frozen=True, slots=True)
class PhotonicsSourceConfig:
    wiring: str
    load: str
    amplitude_definition: str
    dc_mode: str
    dc_offset_v: float


@dataclass(frozen=True, slots=True)
class PhotonicsReferenceOutputConfig:
    """Explicit, read-only contract for XY SINE OUT feeding XX REF IN."""
    wiring: str
    load: str
    amplitude_definition: str
    amplitude_v_rms: float
    dc_mode: str
    dc_offset_v: float
    destination: str
    sample_connected: bool


@dataclass(frozen=True, slots=True)
class PhotonicsVisaConfig:
    backend: str
    timeout_ms: int


@dataclass(frozen=True, slots=True)
class PhotonicsLockinConfig:
    schema_version: int
    lockin_xx: PhotonicsLockinRoleConfig
    lockin_xy: PhotonicsLockinRoleConfig
    visa: PhotonicsVisaConfig
    source: PhotonicsSourceConfig
    excitation_points_v_rms: tuple[float, ...]
    reference_min_hz: float
    reference_max_hz: float
    reference_expected_hz: float
    pair_tolerance_hz: float
    settle_time_constants: float
    sample_interval_s: float
    minimum_source_voltage_v: float
    maximum_source_voltage_v: float
    cleanup_source_voltage_v: float
    safety_path: str
    safety_sha256: str
    reference_topology: str = "pem_xx_xy"
    reference_output: PhotonicsReferenceOutputConfig | None = None
    reference_lock_timeout_s: float = 0.0
    overload_policy: str = "abort"
    reference_unlock_policy: str = "abort"
    reference_transient_policy: str = "abort"
    reference_recovery_timeout_s: float = 45.0
    reference_recovery_consecutive_good: int = 3
    pem_reference_harmonic: int = 1

    @property
    def reference_lock_wait_s(self):
        """Compatibility alias; the resolved value is now a polling timeout."""
        return self.reference_lock_timeout_s

    @property
    def settle_s(self) -> float:
        return max(self.lockin_xx.time_constant_s, self.lockin_xy.time_constant_s) * self.settle_time_constants


def _table(document, name):
    value = document.get(name)
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a TOML table.")
    return value


def _keys(table, required, optional=(), *, name):
    missing = set(required) - set(table)
    unknown = set(table) - set(required) - set(optional)
    if missing or unknown:
        raise ValueError(f"{name}: missing fields {sorted(missing)}, unsupported fields {sorted(unknown)}.")


def _number(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number.")
    if positive and value <= 0:
        raise ValueError(f"{name} must be positive.")
    return float(value)


def _choice(value, allowed, name):
    if value not in allowed:
        raise ValueError(f"Unsupported {name}: {value!r}.")
    return value


def _role_config(table, role, allowed_scales, reference_max_hz, topology):
    name = "lockin_" + role.value
    common = {
        "model", "address", "reference_source", "external_reference_edge", "sine_output_connected",
        "input_mode", "shield_grounding", "input_coupling", "time_constant_s", "filter_slope_db_oct",
        "sensitivity_mode", "sensitivity_full_scale_v", "phase_shift_deg", "harmonics",
    }
    model = LockinModel(table.get("model"))
    subname = "sr830" if model is LockinModel.SR830 else "sr865a"
    _keys(table, common | {subname}, name=name)
    address = table["address"]
    if not isinstance(address, str) or not address.strip() or "CHANGE_ME" in address:
        raise ValueError(f"{name}.address must be an explicitly configured VISA address.")
    sine_reference = topology == "pem_xy_xx_sine" and role is LockinRole.XX
    source = _choice(table["reference_source"], ("external_sine",) if sine_reference else ("external_ttl",), name + ".reference_source")
    edge = _choice(table["external_reference_edge"], ("sine_zero_crossing",) if sine_reference else ("rising", "falling"), name + ".edge")
    connected = table["sine_output_connected"]
    if type(connected) is not bool or connected != (role is LockinRole.XX or topology == "pem_xy_xx_sine"):
        raise ValueError("SINE OUT physical connection does not match the explicit reference topology.")
    if topology == "pem_xy_xx_sine" and model is not (LockinModel.SR830 if role is LockinRole.XX else LockinModel.SR865A):
        raise ValueError("pem_xy_xx_sine requires XX SR830 and XY SR865A.")
    _choice(table["input_mode"], ("a_minus_b",), name + ".input_mode")
    grounding = _choice(table["shield_grounding"], ("float", "ground") if topology == "pem_xy_xx_sine" else ("float",), name + ".shield_grounding")
    _choice(table["input_coupling"], ("ac",), name + ".input_coupling")
    _choice(table["sensitivity_mode"], ("fixed",), name + ".sensitivity_mode")
    if type(table["filter_slope_db_oct"]) is not int or table["filter_slope_db_oct"] != 24:
        raise ValueError("The initial fixed-range profile requires 24 dB/oct RC filters.")
    caps = capabilities_for(model)
    tau = _number(table["time_constant_s"], name + ".time_constant_s", positive=True)
    scale = _number(table["sensitivity_full_scale_v"], name + ".sensitivity_full_scale_v", positive=True)
    if tau not in caps.time_constants_s or scale not in caps.sensitivity_full_scales_v or scale not in allowed_scales:
        raise ValueError(f"{name} has a time constant/full scale outside hardware or safety policy.")
    phase = _number(table["phase_shift_deg"], name + ".phase_shift_deg")
    if not -180 <= phase <= 180:
        raise ValueError("Initial profile phase shift must be between -180 and 180 degrees.")
    harmonics = table["harmonics"]
    if not isinstance(harmonics, list) or not harmonics or any(type(h) is not int for h in harmonics):
        raise ValueError(f"{name}.harmonics must be a nonempty integer list.")
    if harmonics != sorted(set(harmonics)):
        raise ValueError(f"{name}.harmonics must be unique and increasing.")
    for harmonic in harmonics:
        caps.validate_detection(reference_max_hz, harmonic)
    sub = _table(table, subname)
    reserve = input_range = impedance = sync = None
    current_status = False
    if model is LockinModel.SR830:
        _keys(sub, {"reserve_mode"}, name=name + ".sr830")
        reserve = _choice(sub["reserve_mode"], ("high_reserve", "normal", "low_noise"), "reserve mode")
    else:
        _keys(sub, {"input_range_v_peak", "reference_input_impedance_ohm", "sync_output_mode", "current_status_supported"}, name=name + ".sr865a")
        input_range = _number(sub["input_range_v_peak"], "input_range_v_peak", positive=True)
        _choice(input_range, INPUT_RANGES_V_PEAK, "SR865A input range")
        impedance = _number(sub["reference_input_impedance_ohm"], "reference impedance", positive=True)
        _choice(impedance, (50.0, 1_000_000.0), "reference impedance")
        sync = _choice(sub["sync_output_mode"], ("preserve",) if topology == "pem_xy_xx_sine" else ("bipolar_sync", "unipolar_sync"), "BlazeX sync mode")
        current_status = sub["current_status_supported"]
        if current_status is not True:
            raise ValueError("SR865A formal acquisition requires explicitly verified current-status support.")
    return PhotonicsLockinRoleConfig(
        role, model.value, address.strip(), source, edge, connected,
        "a_minus_b", grounding, "ac", tau, 24, "fixed", scale, phase, tuple(harmonics),
        reserve, input_range, impedance, sync, current_status,
    )


def load_photonics_lockin_config(path, *, document=None, safety_document=None) -> PhotonicsLockinConfig:
    """Parse just the lock-in profile within the unified TOML; never connect hardware."""
    path = Path(path)
    if document is None:
        with path.open("rb") as stream:
            document = tomllib.load(stream)
    profile = _table(document, "photonics_lockin")
    topology = document.get("combination_scan", {}).get("reference_topology", "pem_xx_xy")
    _choice(topology, PHOTONICS_REFERENCE_TOPOLOGIES, "photonics reference topology")
    _keys(profile, {
        "schema_version", "safety_file", "reference_min_hz", "reference_max_hz", "reference_expected_hz",
        "pair_tolerance_hz", "settle_time_constants", "sample_interval_s", "source",
    }, {"reference_lock_wait_s", "reference_lock_timeout_s", "overload_policy", "reference_unlock_policy",
        "reference_transient_policy", "reference_recovery_timeout_s", "reference_recovery_consecutive_good",
        "pem_reference_harmonic"}
       | ({"reference_output"} if topology == "pem_xy_xx_sine" else set()), name="photonics_lockin")
    if type(profile["schema_version"]) is not int or profile["schema_version"] != 1:
        raise ValueError("Unsupported photonics_lockin schema version.")
    pem_reference_harmonic = profile.get("pem_reference_harmonic", 1)
    if type(pem_reference_harmonic) is not int or pem_reference_harmonic not in (1, 2):
        raise ValueError("photonics_lockin.pem_reference_harmonic must be integer 1 or 2.")
    if not isinstance(profile["safety_file"], str) or not profile["safety_file"].strip():
        raise ValueError("A separate photonics lock-in safety policy is required.")
    safety_path = (path.parent / profile["safety_file"]).resolve()
    if safety_document is None:
        raw_safety = safety_path.read_bytes()
        safety = tomllib.loads(raw_safety.decode("utf-8"))
    else:
        safety = safety_document
        # Injected fake configs retain an explicit provenance hash as well.
        raw_safety = repr(safety_document).encode("utf-8")
    _keys(safety, {"schema_version", "minimum_source_voltage_v", "maximum_source_voltage_v",
                   "cleanup_source_voltage_v", "lockin_xx", "lockin_xy"}, name="photonics lock-in safety")
    if type(safety["schema_version"]) is not int or safety["schema_version"] != 1:
        raise ValueError("Unsupported photonics lock-in safety schema version.")
    lo = _number(safety["minimum_source_voltage_v"], "minimum source", positive=True)
    hi = _number(safety["maximum_source_voltage_v"], "maximum source", positive=True)
    cleanup = _number(safety["cleanup_source_voltage_v"], "cleanup source", positive=True)
    if not lo <= cleanup <= hi:
        raise ValueError("Cleanup amplitude must be inside the explicit approved source bounds.")
    ref_min = _number(profile["reference_min_hz"], "reference_min_hz", positive=True)
    ref_max = _number(profile["reference_max_hz"], "reference_max_hz", positive=True)
    ref_expected = _number(profile["reference_expected_hz"], "reference_expected_hz", positive=True)
    if not ref_min <= ref_expected <= ref_max:
        raise ValueError("Expected external reference must lie within the explicit reference interval.")
    pair_tolerance = _number(profile["pair_tolerance_hz"], "pair_tolerance_hz", positive=True)
    if "reference_lock_wait_s" in profile and "reference_lock_timeout_s" in profile:
        raise ValueError("Use only reference_lock_timeout_s or its legacy reference_lock_wait_s alias.")
    timeout_key = "reference_lock_wait_s" if "reference_lock_wait_s" in profile else "reference_lock_timeout_s"
    reference_wait = _number(profile.get(timeout_key, 0.0), timeout_key)
    if not 0 <= reference_wait <= 600:
        raise ValueError(f"{timeout_key} must be between 0 and 600 seconds.")
    overload_policy = _choice(profile.get("overload_policy", "abort"), ("abort", "record_continue"), "photonics_lockin.overload_policy")
    reference_unlock_policy = _choice(profile.get("reference_unlock_policy", "abort"),
                                      ("abort", "record_continue"), "photonics_lockin.reference_unlock_policy")
    reference_transient_policy = _choice(profile.get("reference_transient_policy", "abort"),
                                         ("abort", "wait_stable"), "photonics_lockin.reference_transient_policy")
    recovery_timeout = _number(profile.get("reference_recovery_timeout_s", 45.0),
                               "reference_recovery_timeout_s", positive=True)
    if recovery_timeout > 600:
        raise ValueError("reference_recovery_timeout_s must be positive and at most 600 seconds.")
    recovery_good = profile.get("reference_recovery_consecutive_good", 3)
    if type(recovery_good) is not int or not 1 <= recovery_good <= 100:
        raise ValueError("reference_recovery_consecutive_good must be an integer between 1 and 100.")
    settle = _number(profile["settle_time_constants"], "settle_time_constants", positive=True)
    # SR865A manual rev. 2.11: four RC stages need 10 tau for 1% settling.
    if settle < 10:
        raise ValueError("24 dB/oct RC filter settling requires at least ten time constants.")
    interval = _number(profile["sample_interval_s"], "sample_interval_s")
    if interval < 0:
        raise ValueError("sample_interval_s must be nonnegative.")
    roles = []
    for role in (LockinRole.XX, LockinRole.XY):
        name = "lockin_" + role.value
        policy = _table(safety, name)
        _keys(policy, {"allowed_fixed_full_scales_v"}, name="safety." + name)
        scales = policy["allowed_fixed_full_scales_v"]
        if not isinstance(scales, list) or not scales:
            raise ValueError("Safety policy requires explicit nonempty full-scale allowlists.")
        scales = tuple(_number(v, "allowed full scale", positive=True) for v in scales)
        roles.append(_role_config(_table(document, name), role, scales, ref_max, topology))
    xx, xy = roles
    if xx.address.upper() == xy.address.upper():
        raise ValueError("XX and XY must have distinct instrument addresses.")
    hardware_min, hardware_max = (1e-9, 2.0) if xx.model == "SR865A" else (0.004, 5.0)
    if not hardware_min <= lo <= hi <= hardware_max:
        raise ValueError("Source policy exceeds the selected XX model's hardware limits.")
    for role in roles:
        capabilities_for(role.model).validate_detection(ref_min, role.harmonics[0])
    source = _table(profile, "source")
    _keys(source, {"wiring", "load", "amplitude_definition", "dc_mode", "dc_offset_v"}, name="photonics_lockin.source")
    wiring = _choice(source["wiring"], ("single_ended", "differential") if xx.model == "SR865A" else ("single_ended",), "source wiring")
    load = _choice(source["load"], ("50_ohm", "high_impedance"), "source load")
    definition = "differential_rms_into_50_ohm_loads" if xx.model == "SR865A" else "sr830_instrument_rms_setting"
    _choice(source["amplitude_definition"], (definition,), "source amplitude definition")
    dc_mode = _choice(source["dc_mode"], ("common", "difference") if xx.model == "SR865A" else ("not_supported",), "source DC mode")
    offset = _number(source["dc_offset_v"], "source DC offset")
    if offset != 0:
        raise ValueError("The initial photonics profile requires AC excitation with verified zero DC offset.")
    reference_output = None
    if topology == "pem_xy_xx_sine":
        refout = _table(profile, "reference_output")
        _keys(refout, {"wiring", "load", "amplitude_definition", "amplitude_v_rms", "dc_mode", "dc_offset_v", "destination", "sample_connected"}, name="photonics_lockin.reference_output")
        ref_wiring = _choice(refout["wiring"], ("single_ended", "differential"), "reference output wiring")
        ref_load = _choice(refout["load"], ("high_impedance",), "SR830 REF IN reference load")
        ref_definition = _choice(refout["amplitude_definition"], ("differential_rms_into_50_ohm_loads",), "reference output amplitude definition")
        ref_amplitude = _number(refout["amplitude_v_rms"], "reference output amplitude", positive=True)
        if not 1e-9 <= ref_amplitude <= 2.0:
            raise ValueError("Reference output amplitude exceeds the SR865A setting range.")
        ref_mode = _choice(refout["dc_mode"], ("common", "difference"), "reference output DC mode")
        ref_offset = _number(refout["dc_offset_v"], "reference output DC offset")
        if ref_offset != 0:
            raise ValueError("Reference SINE output requires explicitly verified zero DC offset.")
        destination = _choice(refout["destination"], ("lockin_xx_ref_in",), "reference output destination")
        if refout["sample_connected"] is not False:
            raise ValueError("XY reference output must explicitly be disconnected from the sample.")
        reference_output = PhotonicsReferenceOutputConfig(ref_wiring, ref_load, ref_definition, ref_amplitude, ref_mode, ref_offset, destination, False)
    sweep = _table(document, "lockin_sweep")
    _keys(sweep, set(), {"excitation_points_v_rms", "excitation_ranges"}, name="photonics lockin_sweep")
    if ("excitation_points_v_rms" in sweep) == ("excitation_ranges" in sweep):
        raise ValueError("photonics lockin_sweep requires exactly one of excitation_points_v_rms or excitation_ranges.")
    if "excitation_points_v_rms" in sweep:
        points = sweep["excitation_points_v_rms"]
    else:
        _, point_specs = _parse_sweep_ranges(
            sweep["excitation_ranges"], "photonics lockin_sweep.excitation_ranges",
            minimum=lo, maximum=hi, safety=None, lockin_xx=None, lockin_xy=None,
            maximum_points=100_000, allow_full_scale_overrides=False,
        )
        points = [spec.value for spec in point_specs]
    if not isinstance(points, list) or not 1 <= len(points) <= 100_000:
        raise ValueError("photonics lockin_sweep requires 1–100000 source points.")
    points = tuple(_number(v, "source point", positive=True) for v in points)
    if any(not lo <= value <= hi for value in points) or cleanup > min(points):
        raise ValueError("Source points must be within policy and cleanup may not increase any point.")
    visa = _table(document, "visa")
    _keys(visa, {"backend", "timeout_ms"}, name="visa")
    if not isinstance(visa["backend"], str) or not visa["backend"]:
        raise ValueError("visa.backend must be explicit.")
    timeout = visa["timeout_ms"]
    if type(timeout) is not int or timeout <= 0:
        raise ValueError("visa.timeout_ms must be a positive integer.")
    return PhotonicsLockinConfig(
        1, xx, xy, PhotonicsVisaConfig(visa["backend"], timeout),
        PhotonicsSourceConfig(wiring, load, definition, dc_mode, offset), points,
        ref_min, ref_max, ref_expected, pair_tolerance, settle, interval, lo, hi, cleanup,
        str(safety_path), hashlib.sha256(raw_safety).hexdigest(),
        topology, reference_output, reference_wait, overload_policy, reference_unlock_policy,
        reference_transient_policy, recovery_timeout, recovery_good, pem_reference_harmonic,
    )
