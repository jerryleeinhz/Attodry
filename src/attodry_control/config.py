from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math
from pathlib import Path
import tomllib
from typing import Any, Mapping

from .lockin_autorange import AutorangePolicy
from .models import LockinRole
from .safety import MagnetLimits
from .sr830_settings import (
    ExternalReferenceEdge,
    InputCoupling,
    InputMode,
    ReferenceSource,
    SensitivityMode,
    ShieldGrounding,
    map_sr830_settings,
)
from .stability import StabilityCriteria


CONFIRMED_EXPERIMENT_VECTOR_MAX_T = 3.0


class ConfigError(ValueError):
    """Raised when a configuration file is incomplete, unknown, or unsafe."""


class RunMode(StrEnum):
    SIMULATION = "simulation"
    HARDWARE = "hardware"


class FieldEndPolicy(StrEnum):
    HOLD = "hold"
    ZERO = "zero"


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    mode: RunMode
    database_path: Path


@dataclass(frozen=True, slots=True)
class CryostatConfig:
    backend: str
    temperature_min_k: float
    temperature_max_k: float
    com_port: str | None = None
    dll_path: Path | None = None
    device_type: int | None = None
    connection_timeout_s: float | None = None


@dataclass(frozen=True, slots=True)
class StabilityConfig:
    criteria: StabilityCriteria
    poll_interval_s: float
    wait_timeout_s: float


@dataclass(frozen=True, slots=True)
class TemperatureRunConfig:
    target_k: float
    max_delta_k: float
    max_overshoot_k: float
    pre_measure_wait_s: float
    poll_interval_s: float


@dataclass(frozen=True, slots=True)
class TemperatureOperationConfig:
    cryostat: CryostatConfig
    magnet: MagnetConfig
    temperature_stability: StabilityConfig
    temperature_run: TemperatureRunConfig


@dataclass(frozen=True, slots=True)
class MagnetConfig:
    limits: MagnetLimits
    stability: StabilityConfig


@dataclass(frozen=True, slots=True)
class CleanupConfig:
    normal_end_field_policy: FieldEndPolicy
    exception_field_policy: FieldEndPolicy


@dataclass(frozen=True, slots=True)
class VisaConfig:
    backend: str
    timeout_ms: int


@dataclass(frozen=True, slots=True)
class LockinConfig:
    role: LockinRole
    model: str
    address: str
    reference_source: ReferenceSource
    external_reference_edge: ExternalReferenceEdge | None
    sine_output_connected: bool
    source_voltage_v: float
    frequency_hz: float
    input_mode: InputMode
    shield_grounding: ShieldGrounding
    input_coupling: InputCoupling
    time_constant_s: float
    filter_slope_db_oct: int
    sensitivity_mode: SensitivityMode
    sensitivity_full_scale_v: float
    autorange_min_full_scale_v: float | None
    autorange_max_full_scale_v: float | None
    autorange_target_occupancy: float | None
    autorange_stable_samples: int | None
    autorange_max_steps: int | None
    settle_time_constants: float


@dataclass(frozen=True, slots=True)
class LockinSweepConfig:
    frequency_points_hz: tuple[float, ...]
    excitation_points_v_rms: tuple[float, ...]
    harmonics: tuple[int, ...]
    skip_unsupported_harmonics: bool
    run_name: str
    note: str
    settle_s: float
    samples_per_point: int
    sample_interval_s: float
    external_series_resistance_ohm: float
    approximate_device_resistance_ohm: float
    max_device_current_a_rms: float
    max_device_voltage_v_rms: float
    external_50_ohm_termination: bool
    output_directory: Path


@dataclass(frozen=True, slots=True)
class GateConfig:
    role: str
    compliance_a: float | None
    leakage_limit_a: float | None
    max_abs_voltage_v: float | None
    ramp_step_v: float | None
    readback_tolerance_v: float | None
    settle_s: float | None
    backend: str | None = None
    model: str | None = None
    address: str | None = None


@dataclass(frozen=True, slots=True)
class ControlConfig:
    project: ProjectConfig
    cryostat: CryostatConfig
    magnet: MagnetConfig
    temperature_stability: StabilityConfig
    cleanup: CleanupConfig
    lockin_xx: LockinConfig
    lockin_xy: LockinConfig
    lockin_sweep: LockinSweepConfig
    gate_top: GateConfig
    gate_bottom: GateConfig
    visa: VisaConfig | None = None

    def hardware_readiness_errors(self) -> tuple[str, ...]:
        if self.project.mode is not RunMode.HARDWARE:
            return ("project.mode is not 'hardware'",)
        errors: list[str] = []
        for name, value in (
            ("cryostat.com_port", self.cryostat.com_port),
            ("cryostat.dll_path", str(self.cryostat.dll_path or "")),
            ("lockin_xx.address", self.lockin_xx.address),
            ("lockin_xy.address", self.lockin_xy.address),
            ("gate_top.model", self.gate_top.model),
            ("gate_top.address", self.gate_top.address),
            ("gate_bottom.model", self.gate_bottom.model),
            ("gate_bottom.address", self.gate_bottom.address),
        ):
            if not value or "CHANGE_ME" in value:
                errors.append(f"{name} is not configured")
        for table_name, gate in (
            ("gate_top", self.gate_top),
            ("gate_bottom", self.gate_bottom),
        ):
            for field_name in (
                "compliance_a",
                "leakage_limit_a",
                "max_abs_voltage_v",
                "ramp_step_v",
                "readback_tolerance_v",
                "settle_s",
            ):
                if getattr(gate, field_name) is None:
                    errors.append(f"{table_name}.{field_name} is not configured")
        return tuple(errors)

    def require_hardware_ready(self) -> None:
        errors = self.hardware_readiness_errors()
        if errors:
            raise ConfigError("Hardware configuration is not ready: " + "; ".join(errors))


def load_config(path: str | Path) -> ControlConfig:
    """Load and validate TOML without importing or opening hardware drivers."""

    document = _load_document(path)

    try:
        project = _parse_project(_table(document, "project"))
        expected_tables = {
            "project",
            "cryostat",
            "magnet",
            "temperature_stability",
            "cleanup",
            "lockin_xx",
            "lockin_xy",
            "lockin_sweep",
            "gate_top",
            "gate_bottom",
        }
        if project.mode is RunMode.HARDWARE:
            expected_tables.add("visa")
        _strict_keys_with_optional(
            document,
            "top level",
            expected_tables,
            {"temperature_run"} if project.mode is RunMode.HARDWARE else set(),
        )

        cryostat = _parse_cryostat(_table(document, "cryostat"), project.mode)
        magnet = _parse_magnet(_table(document, "magnet"))
        temperature_stability = _parse_stability(
            _table(document, "temperature_stability"),
            "temperature_stability",
            value_prefix="",
        )
        cleanup = _parse_cleanup(_table(document, "cleanup"))
        lockin_xx = _parse_lockin(
            _table(document, "lockin_xx"), LockinRole.XX, "lockin_xx"
        )
        lockin_xy = _parse_lockin(
            _table(document, "lockin_xy"), LockinRole.XY, "lockin_xy"
        )
        _validate_lockin_pair(lockin_xx, lockin_xy)
        lockin_sweep = _parse_lockin_sweep(_table(document, "lockin_sweep"))
        gate_top = _parse_gate(
            _table(document, "gate_top"), "top", project.mode, "gate_top"
        )
        gate_bottom = _parse_gate(
            _table(document, "gate_bottom"), "bottom", project.mode, "gate_bottom"
        )
        visa = (
            _parse_visa(_table(document, "visa"))
            if project.mode is RunMode.HARDWARE
            else None
        )
    except ConfigError:
        raise
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    return ControlConfig(
        project=project,
        cryostat=cryostat,
        magnet=magnet,
        temperature_stability=temperature_stability,
        cleanup=cleanup,
        lockin_xx=lockin_xx,
        lockin_xy=lockin_xy,
        lockin_sweep=lockin_sweep,
        gate_top=gate_top,
        gate_bottom=gate_bottom,
        visa=visa,
    )


def _parse_project(table: Mapping[str, Any]) -> ProjectConfig:
    name = "project"
    _strict_keys(table, name, {"mode", "database_path"})
    mode = _enum_value(RunMode, table["mode"], f"{name}.mode")
    database_path = Path(_string(table["database_path"], f"{name}.database_path"))
    return ProjectConfig(mode=mode, database_path=database_path)


def _parse_cryostat(
    table: Mapping[str, Any], mode: RunMode
) -> CryostatConfig:
    name = "cryostat"
    simulation_keys = {"backend", "temperature_min_k", "temperature_max_k"}
    hardware_keys = simulation_keys | {
        "com_port",
        "dll_path",
        "device_type",
        "connection_timeout_s",
    }
    keys = simulation_keys if mode is RunMode.SIMULATION else hardware_keys
    _strict_keys(table, name, keys)
    backend = _string(table["backend"], f"{name}.backend")
    expected_backend = "simulation" if mode is RunMode.SIMULATION else "legacy_dll"
    if backend != expected_backend:
        raise ConfigError(
            f"{name}.backend must be {expected_backend!r} in {mode.value} mode."
        )
    temperature_min_k = _number(table["temperature_min_k"], f"{name}.temperature_min_k")
    temperature_max_k = _number(table["temperature_max_k"], f"{name}.temperature_max_k")
    if temperature_min_k <= 0 or temperature_max_k <= temperature_min_k:
        raise ConfigError(
            "cryostat temperatures must satisfy 0 < temperature_min_k < "
            "temperature_max_k."
        )
    if mode is RunMode.SIMULATION:
        return CryostatConfig(backend, temperature_min_k, temperature_max_k)
    return CryostatConfig(
        backend=backend,
        temperature_min_k=temperature_min_k,
        temperature_max_k=temperature_max_k,
        com_port=_string(table["com_port"], f"{name}.com_port"),
        dll_path=Path(_string(table["dll_path"], f"{name}.dll_path")),
        device_type=_integer(table["device_type"], f"{name}.device_type", minimum=1),
        connection_timeout_s=_positive_number(
            table["connection_timeout_s"], f"{name}.connection_timeout_s"
        ),
    )


def _parse_magnet(table: Mapping[str, Any]) -> MagnetConfig:
    name = "magnet"
    _strict_keys(
        table,
        name,
        {
            "hardware_x_max_t",
            "hardware_z_max_t",
            "experiment_vector_max_t",
            "field_tolerance_t",
            "stable_range_t",
            "stable_dwell_s",
            "poll_interval_s",
            "wait_timeout_s",
        },
    )
    experiment_limit = _positive_number(
        table["experiment_vector_max_t"], f"{name}.experiment_vector_max_t"
    )
    if experiment_limit > CONFIRMED_EXPERIMENT_VECTOR_MAX_T:
        raise ConfigError(
            "magnet.experiment_vector_max_t cannot exceed the confirmed 3 T "
            "project limit."
        )
    limits = MagnetLimits(
        hardware_x_max_t=_positive_number(
            table["hardware_x_max_t"], f"{name}.hardware_x_max_t"
        ),
        hardware_z_max_t=_positive_number(
            table["hardware_z_max_t"], f"{name}.hardware_z_max_t"
        ),
        experiment_vector_max_t=experiment_limit,
    )
    stability = _parse_stability(table, name, value_prefix="field_")
    return MagnetConfig(limits=limits, stability=stability)


def _parse_stability(
    table: Mapping[str, Any], name: str, value_prefix: str
) -> StabilityConfig:
    tolerance_key = f"{value_prefix}tolerance_t" if value_prefix else "tolerance_k"
    expected = {
        tolerance_key,
        "stable_range_t" if value_prefix else "stable_range_k",
        "stable_dwell_s",
        "poll_interval_s",
        "wait_timeout_s",
    }
    if name != "magnet":
        _strict_keys(table, name, expected)
    range_key = "stable_range_t" if value_prefix else "stable_range_k"
    criteria = StabilityCriteria(
        tolerance=_positive_number(table[tolerance_key], f"{name}.{tolerance_key}"),
        stable_range=_nonnegative_number(table[range_key], f"{name}.{range_key}"),
        dwell_s=_positive_number(table["stable_dwell_s"], f"{name}.stable_dwell_s"),
    )
    poll_interval_s = _positive_number(
        table["poll_interval_s"], f"{name}.poll_interval_s"
    )
    wait_timeout_s = _positive_number(
        table["wait_timeout_s"], f"{name}.wait_timeout_s"
    )
    if wait_timeout_s < criteria.dwell_s:
        raise ConfigError(f"{name}.wait_timeout_s must cover stable_dwell_s.")
    return StabilityConfig(criteria, poll_interval_s, wait_timeout_s)


def _parse_temperature_run(
    table: Mapping[str, Any], cryostat: CryostatConfig
) -> TemperatureRunConfig:
    name = "temperature_run"
    _strict_keys(
        table,
        name,
        {
            "target_k",
            "max_delta_k",
            "max_overshoot_k",
            "pre_measure_wait_s",
            "poll_interval_s",
        },
    )
    target_k = _positive_number(table["target_k"], f"{name}.target_k")
    max_delta_k = _positive_number(table["max_delta_k"], f"{name}.max_delta_k")
    max_overshoot_k = _positive_number(
        table["max_overshoot_k"], f"{name}.max_overshoot_k"
    )
    pre_measure_wait_s = _positive_number(
        table["pre_measure_wait_s"], f"{name}.pre_measure_wait_s"
    )
    poll_interval_s = _positive_number(
        table["poll_interval_s"], f"{name}.poll_interval_s"
    )
    if not cryostat.temperature_min_k <= target_k <= cryostat.temperature_max_k:
        raise ConfigError("temperature_run.target_k is outside cryostat limits.")
    if target_k + max_overshoot_k > cryostat.temperature_max_k:
        raise ConfigError(
            "temperature_run target plus max_overshoot_k exceeds the cryostat limit."
        )
    if pre_measure_wait_s < poll_interval_s:
        raise ConfigError(
            "temperature_run.pre_measure_wait_s must cover poll_interval_s."
        )
    return TemperatureRunConfig(
        target_k=target_k,
        max_delta_k=max_delta_k,
        max_overshoot_k=max_overshoot_k,
        pre_measure_wait_s=pre_measure_wait_s,
        poll_interval_s=poll_interval_s,
    )


def load_temperature_operation_config(
    path: str | Path,
) -> TemperatureOperationConfig:
    """Load only the strict attoDRY tables needed by daily temperature operation."""

    document = _load_document(path)
    project = _parse_project(_table(document, "project"))
    if project.mode is not RunMode.HARDWARE:
        raise ConfigError("Daily temperature operation requires hardware mode.")
    known_tables = {
        "project",
        "cryostat",
        "magnet",
        "temperature_stability",
        "temperature_run",
        "cleanup",
        "visa",
        "lockin_xx",
        "lockin_xy",
        "lockin_sweep",
        "gate_top",
        "gate_bottom",
    }
    _strict_keys_with_optional(
        document,
        "top level",
        {
            "project",
            "cryostat",
            "magnet",
            "temperature_stability",
            "temperature_run",
        },
        known_tables,
    )
    cryostat = _parse_cryostat(_table(document, "cryostat"), project.mode)
    magnet = _parse_magnet(_table(document, "magnet"))
    temperature_stability = _parse_stability(
        _table(document, "temperature_stability"),
        "temperature_stability",
        value_prefix="",
    )
    temperature_run = _parse_temperature_run(
        _table(document, "temperature_run"), cryostat
    )
    return TemperatureOperationConfig(
        cryostat=cryostat,
        magnet=magnet,
        temperature_stability=temperature_stability,
        temperature_run=temperature_run,
    )


def _load_document(path: str | Path) -> Mapping[str, Any]:
    config_path = Path(path)
    try:
        with config_path.open("rb") as file:
            return tomllib.load(file)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {config_path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Could not read configuration {config_path}: {exc}") from exc


def _parse_cleanup(table: Mapping[str, Any]) -> CleanupConfig:
    name = "cleanup"
    _strict_keys(
        table, name, {"normal_end_field_policy", "exception_field_policy"}
    )
    normal = _enum_value(
        FieldEndPolicy,
        table["normal_end_field_policy"],
        f"{name}.normal_end_field_policy",
    )
    exception = _enum_value(
        FieldEndPolicy,
        table["exception_field_policy"],
        f"{name}.exception_field_policy",
    )
    if exception is not FieldEndPolicy.ZERO:
        raise ConfigError("cleanup.exception_field_policy must be 'zero'.")
    return CleanupConfig(normal, exception)


def _parse_visa(table: Mapping[str, Any]) -> VisaConfig:
    name = "visa"
    _strict_keys(table, name, {"backend", "timeout_ms"})
    return VisaConfig(
        backend=_string(table["backend"], f"{name}.backend"),
        timeout_ms=_integer(table["timeout_ms"], f"{name}.timeout_ms", minimum=1),
    )


def _parse_lockin(
    table: Mapping[str, Any], role: LockinRole, name: str
) -> LockinConfig:
    expected = {
        "model",
        "address",
        "reference_source",
        "sine_output_connected",
        "source_voltage_v",
        "frequency_hz",
        "input_mode",
        "shield_grounding",
        "input_coupling",
        "time_constant_s",
        "filter_slope_db_oct",
        "sensitivity_mode",
        "sensitivity_full_scale_v",
        "settle_time_constants",
    }
    if role is LockinRole.XY:
        expected.add("external_reference_edge")
    if table.get("sensitivity_mode") == SensitivityMode.BOUNDED_AUTO.value:
        expected.update(
            {
                "autorange_min_full_scale_v",
                "autorange_max_full_scale_v",
                "autorange_target_occupancy",
                "autorange_stable_samples",
                "autorange_max_steps",
            }
        )
    _strict_keys(
        table,
        name,
        expected,
    )
    model = _string(table["model"], f"{name}.model")
    if model != "SR830":
        raise ConfigError(f"{name}.model must be 'SR830'.")
    reference_source = _enum_value(
        ReferenceSource, table["reference_source"], f"{name}.reference_source"
    )
    external_reference_edge = (
        _enum_value(
            ExternalReferenceEdge,
            table["external_reference_edge"],
            f"{name}.external_reference_edge",
        )
        if role is LockinRole.XY
        else None
    )
    connected = _boolean(
        table["sine_output_connected"], f"{name}.sine_output_connected"
    )
    if role is LockinRole.XX:
        if reference_source is not ReferenceSource.INTERNAL:
            raise ConfigError("lockin_xx reference_source must be 'internal'.")
        if not connected:
            raise ConfigError("lockin_xx SINE OUT must be marked physically connected.")
    else:
        if reference_source is not ReferenceSource.EXTERNAL_TTL:
            raise ConfigError("lockin_xy reference_source must be 'external_ttl'.")
        if connected:
            raise ConfigError("lockin_xy SINE OUT must be marked physically disconnected.")
    source_voltage_v = _number(
        table["source_voltage_v"], f"{name}.source_voltage_v"
    )
    if not 0.004 <= source_voltage_v <= 5.0:
        raise ConfigError(f"{name}.source_voltage_v must be within 4 mVrms to 5 Vrms.")
    frequency_hz = _positive_number(table["frequency_hz"], f"{name}.frequency_hz")
    if frequency_hz > 102_000:
        raise ConfigError(f"{name}.frequency_hz cannot exceed 102000 Hz.")
    input_mode = _enum_value(InputMode, table["input_mode"], f"{name}.input_mode")
    shield_grounding = _enum_value(
        ShieldGrounding, table["shield_grounding"], f"{name}.shield_grounding"
    )
    input_coupling = _enum_value(
        InputCoupling, table["input_coupling"], f"{name}.input_coupling"
    )
    time_constant_s = _positive_number(
        table["time_constant_s"], f"{name}.time_constant_s"
    )
    filter_slope_db_oct = _integer(
        table["filter_slope_db_oct"], f"{name}.filter_slope_db_oct", minimum=1
    )
    sensitivity_mode = _enum_value(
        SensitivityMode, table["sensitivity_mode"], f"{name}.sensitivity_mode"
    )
    sensitivity_full_scale_v = _positive_number(
        table["sensitivity_full_scale_v"], f"{name}.sensitivity_full_scale_v"
    )
    autorange_min_full_scale_v = None
    autorange_max_full_scale_v = None
    autorange_target_occupancy = None
    autorange_stable_samples = None
    autorange_max_steps = None
    if sensitivity_mode is SensitivityMode.BOUNDED_AUTO:
        autorange_min_full_scale_v = _positive_number(
            table["autorange_min_full_scale_v"],
            f"{name}.autorange_min_full_scale_v",
        )
        autorange_max_full_scale_v = _positive_number(
            table["autorange_max_full_scale_v"],
            f"{name}.autorange_max_full_scale_v",
        )
        autorange_target_occupancy = _number(
            table["autorange_target_occupancy"],
            f"{name}.autorange_target_occupancy",
        )
        autorange_stable_samples = _integer(
            table["autorange_stable_samples"],
            f"{name}.autorange_stable_samples",
            minimum=1,
        )
        autorange_max_steps = _integer(
            table["autorange_max_steps"],
            f"{name}.autorange_max_steps",
            minimum=1,
        )
        try:
            AutorangePolicy(
                autorange_min_full_scale_v,
                autorange_max_full_scale_v,
                autorange_target_occupancy,
                autorange_stable_samples,
                autorange_max_steps,
            )
        except ValueError as exc:
            raise ConfigError(f"{name}: {exc}") from exc
        required_bounds = (
            (0.01, 0.02) if role is LockinRole.XX else (0.001, 0.01)
        )
        if (autorange_min_full_scale_v, autorange_max_full_scale_v) != required_bounds:
            raise ConfigError(
                f"{name} bounded_auto range must be "
                f"{required_bounds[0]:g}-{required_bounds[1]:g} V."
            )
        if sensitivity_full_scale_v != autorange_min_full_scale_v:
            raise ConfigError(
                f"{name}.sensitivity_full_scale_v must equal the autorange minimum."
            )
    settle_time_constants = _positive_number(
        table["settle_time_constants"], f"{name}.settle_time_constants"
    )
    if settle_time_constants < 5.0:
        raise ConfigError(f"{name}.settle_time_constants must be at least 5.0.")
    try:
        map_sr830_settings(
            reference_source=reference_source,
            external_reference_edge=external_reference_edge,
            input_mode=input_mode,
            shield_grounding=shield_grounding,
            input_coupling=input_coupling,
            time_constant_s=time_constant_s,
            filter_slope_db_oct=filter_slope_db_oct,
            sensitivity_full_scale_v=sensitivity_full_scale_v,
        )
    except ValueError as exc:
        raise ConfigError(f"{name}: {exc}") from exc
    return LockinConfig(
        role=role,
        model=model,
        address=_string(table["address"], f"{name}.address"),
        reference_source=reference_source,
        external_reference_edge=external_reference_edge,
        sine_output_connected=connected,
        source_voltage_v=source_voltage_v,
        frequency_hz=frequency_hz,
        input_mode=input_mode,
        shield_grounding=shield_grounding,
        input_coupling=input_coupling,
        time_constant_s=time_constant_s,
        filter_slope_db_oct=filter_slope_db_oct,
        sensitivity_mode=sensitivity_mode,
        sensitivity_full_scale_v=sensitivity_full_scale_v,
        autorange_min_full_scale_v=autorange_min_full_scale_v,
        autorange_max_full_scale_v=autorange_max_full_scale_v,
        autorange_target_occupancy=autorange_target_occupancy,
        autorange_stable_samples=autorange_stable_samples,
        autorange_max_steps=autorange_max_steps,
        settle_time_constants=settle_time_constants,
    )


def _validate_lockin_pair(xx: LockinConfig, xy: LockinConfig) -> None:
    if xx.address == xy.address:
        raise ConfigError("lockin_xx and lockin_xy must have distinct addresses.")
    if not math.isclose(xx.frequency_hz, xy.frequency_hz, rel_tol=0.0, abs_tol=1e-12):
        raise ConfigError("lockin_xx and lockin_xy frequencies must match.")


def _parse_lockin_sweep(table: Mapping[str, Any]) -> LockinSweepConfig:
    name = "lockin_sweep"
    _strict_keys(
        table,
        name,
        {
            "frequency_points_hz",
            "excitation_points_v_rms",
            "harmonics",
            "skip_unsupported_harmonics",
            "run_name",
            "note",
            "settle_s",
            "samples_per_point",
            "sample_interval_s",
            "external_series_resistance_ohm",
            "approximate_device_resistance_ohm",
            "max_device_current_a_rms",
            "max_device_voltage_v_rms",
            "external_50_ohm_termination",
            "output_directory",
        },
    )
    frequency_points_hz = _positive_number_tuple(
        table["frequency_points_hz"], f"{name}.frequency_points_hz"
    )
    excitation_points_v_rms = _positive_number_tuple(
        table["excitation_points_v_rms"], f"{name}.excitation_points_v_rms"
    )
    _require_strictly_increasing(frequency_points_hz, f"{name}.frequency_points_hz")
    _require_strictly_increasing(
        excitation_points_v_rms, f"{name}.excitation_points_v_rms"
    )
    if frequency_points_hz[0] < 0.001 or frequency_points_hz[-1] > 102_000:
        raise ConfigError(
            f"{name}.frequency_points_hz must remain within 0.001-102000 Hz."
        )
    if excitation_points_v_rms[0] < 0.004 or excitation_points_v_rms[-1] > 5.0:
        raise ConfigError(
            f"{name}.excitation_points_v_rms must remain within 0.004-5 V RMS."
        )
    harmonics = _integer_tuple(table["harmonics"], f"{name}.harmonics", minimum=1)
    if harmonics != (1, 2, 3):
        raise ConfigError(f"{name}.harmonics must be exactly [1, 2, 3].")
    settle_s = _positive_number(table["settle_s"], f"{name}.settle_s")
    if settle_s < 1.5:
        raise ConfigError(f"{name}.settle_s must be at least 1.5 seconds.")
    external_termination = _boolean(
        table["external_50_ohm_termination"],
        f"{name}.external_50_ohm_termination",
    )
    if external_termination:
        raise ConfigError(
            f"{name}.external_50_ohm_termination must be false for this wiring."
        )
    return LockinSweepConfig(
        frequency_points_hz=frequency_points_hz,
        excitation_points_v_rms=excitation_points_v_rms,
        harmonics=harmonics,
        skip_unsupported_harmonics=_boolean(
            table["skip_unsupported_harmonics"],
            f"{name}.skip_unsupported_harmonics",
        ),
        run_name=_sweep_run_name(table["run_name"], f"{name}.run_name"),
        note=_sweep_note(table["note"], f"{name}.note"),
        settle_s=settle_s,
        samples_per_point=_integer(
            table["samples_per_point"], f"{name}.samples_per_point", minimum=1
        ),
        sample_interval_s=_nonnegative_number(
            table["sample_interval_s"], f"{name}.sample_interval_s"
        ),
        external_series_resistance_ohm=_positive_number(
            table["external_series_resistance_ohm"],
            f"{name}.external_series_resistance_ohm",
        ),
        approximate_device_resistance_ohm=_nonnegative_number(
            table["approximate_device_resistance_ohm"],
            f"{name}.approximate_device_resistance_ohm",
        ),
        max_device_current_a_rms=_positive_number(
            table["max_device_current_a_rms"],
            f"{name}.max_device_current_a_rms",
        ),
        max_device_voltage_v_rms=_positive_number(
            table["max_device_voltage_v_rms"],
            f"{name}.max_device_voltage_v_rms",
        ),
        external_50_ohm_termination=external_termination,
        output_directory=_relative_directory(
            table["output_directory"], f"{name}.output_directory"
        ),
    )


def _parse_gate(
    table: Mapping[str, Any], role: str, mode: RunMode, name: str
) -> GateConfig:
    common_keys = {
        "compliance_a",
        "leakage_limit_a",
        "max_abs_voltage_v",
        "ramp_step_v",
        "readback_tolerance_v",
        "settle_s",
    }
    mode_keys = {"backend"} if mode is RunMode.SIMULATION else {"model", "address"}
    _strict_keys(table, name, common_keys | mode_keys)
    parser = _positive_number if mode is RunMode.SIMULATION else _hardware_number
    compliance = parser(table["compliance_a"], f"{name}.compliance_a")
    leakage = parser(table["leakage_limit_a"], f"{name}.leakage_limit_a")
    max_abs_voltage = parser(
        table["max_abs_voltage_v"], f"{name}.max_abs_voltage_v"
    )
    ramp_step = parser(table["ramp_step_v"], f"{name}.ramp_step_v")
    readback_tolerance = parser(
        table["readback_tolerance_v"], f"{name}.readback_tolerance_v"
    )
    settle = (
        _nonnegative_number(table["settle_s"], f"{name}.settle_s")
        if mode is RunMode.SIMULATION
        else _hardware_number(
            table["settle_s"], f"{name}.settle_s", allow_zero=True
        )
    )
    if compliance is not None and leakage is not None and leakage > compliance:
        raise ConfigError(f"{name}.leakage_limit_a cannot exceed compliance_a.")
    if mode is RunMode.SIMULATION:
        backend = _string(table["backend"], f"{name}.backend")
        if backend != "simulation":
            raise ConfigError(f"{name}.backend must be 'simulation'.")
        return GateConfig(
            role,
            compliance,
            leakage,
            max_abs_voltage,
            ramp_step,
            readback_tolerance,
            settle,
            backend=backend,
        )
    return GateConfig(
        role,
        compliance,
        leakage,
        max_abs_voltage,
        ramp_step,
        readback_tolerance,
        settle,
        model=_string(table["model"], f"{name}.model"),
        address=_string(table["address"], f"{name}.address"),
    )


def _table(document: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = document.get(name)
    if not isinstance(value, dict):
        raise ConfigError(f"Missing or invalid [{name}] table.")
    return value


def _strict_keys(
    table: Mapping[str, Any], name: str, expected: set[str]
) -> None:
    actual = set(table)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing:
        raise ConfigError(f"{name} is missing field(s): {', '.join(missing)}.")
    if unknown:
        raise ConfigError(f"{name} has unknown field(s): {', '.join(unknown)}.")


def _strict_keys_with_optional(
    table: Mapping[str, Any],
    name: str,
    required: set[str],
    optional: set[str],
) -> None:
    actual = set(table)
    missing = sorted(required - actual)
    unknown = sorted(actual - required - optional)
    if missing:
        raise ConfigError(f"{name} is missing field(s): {', '.join(missing)}.")
    if unknown:
        raise ConfigError(f"{name} has unknown field(s): {', '.join(unknown)}.")


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty string.")
    return value


def _sweep_run_name(value: Any, name: str) -> str:
    run_name = _string(value, name)
    if run_name != run_name.strip():
        raise ConfigError(f"{name} must not start or end with whitespace.")
    if len(run_name) > 80:
        raise ConfigError(f"{name} must be at most 80 characters.")
    if any(
        ord(character) < 32 or character in '\\\\/:*?\"<>|'
        for character in run_name
    ):
        raise ConfigError(
            f"{name} must be a safe filename label without path separators or "
            "Windows-reserved characters."
        )
    return run_name


def _sweep_note(value: Any, name: str) -> str:
    note = _string(value, name)
    if len(note) > 2000:
        raise ConfigError(f"{name} must be at most 2000 characters.")
    if "\x00" in note:
        raise ConfigError(f"{name} must not contain a NUL character.")
    return note


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{name} must be a boolean.")
    return value


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name} must be a number.")
    converted = float(value)
    if not math.isfinite(converted):
        raise ConfigError(f"{name} must be finite.")
    return converted


def _positive_number(value: Any, name: str) -> float:
    converted = _number(value, name)
    if converted <= 0:
        raise ConfigError(f"{name} must be positive.")
    return converted


def _nonnegative_number(value: Any, name: str) -> float:
    converted = _number(value, name)
    if converted < 0:
        raise ConfigError(f"{name} must be non-negative.")
    return converted


def _relative_directory(value: Any, name: str) -> Path:
    path = Path(_string(value, name))
    if path == Path(".") or path.is_absolute() or path.anchor:
        raise ConfigError(
            f"{name} must name a non-rooted relative directory."
        )
    return path


def _positive_number_tuple(value: Any, name: str) -> tuple[float, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{name} must be a non-empty array of numbers.")
    return tuple(_positive_number(item, f"{name}[{index}]") for index, item in enumerate(value))


def _integer_tuple(value: Any, name: str, *, minimum: int) -> tuple[int, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{name} must be a non-empty array of integers.")
    return tuple(
        _integer(item, f"{name}[{index}]", minimum=minimum)
        for index, item in enumerate(value)
    )


def _require_strictly_increasing(values: tuple[float, ...], name: str) -> None:
    if any(current <= previous for previous, current in zip(values, values[1:])):
        raise ConfigError(f"{name} must be strictly increasing.")


def _integer(value: Any, name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"{name} must be an integer of at least {minimum}.")
    return value


def _hardware_number(
    value: Any, name: str, *, allow_zero: bool = False
) -> float | None:
    if value == "CHANGE_ME":
        return None
    return _nonnegative_number(value, name) if allow_zero else _positive_number(value, name)


def _enum_value(enum_type: type[StrEnum], value: Any, name: str) -> Any:
    raw = _string(value, name)
    try:
        return enum_type(raw)
    except ValueError as exc:
        allowed = ", ".join(repr(item.value) for item in enum_type)
        raise ConfigError(f"{name} must be one of: {allowed}.") from exc
