from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import math
from pathlib import Path
import tomllib
from typing import Any, Mapping

from .models import LockinRole
from .safety import MagnetLimits
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


class ReferenceSource(StrEnum):
    INTERNAL = "internal"
    EXTERNAL_TTL = "external_ttl"


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
    sine_output_connected: bool
    source_voltage_v: float
    frequency_hz: float


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

    config_path = Path(path)
    try:
        with config_path.open("rb") as file:
            document = tomllib.load(file)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid TOML in {config_path}: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Could not read configuration {config_path}: {exc}") from exc

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
            "gate_top",
            "gate_bottom",
        }
        # These optional tables are parsed by the independent Three-SMU module.
        # Keeping them admissible here permits one daily hardware.local.toml
        # without making an unrelated controller validate SMU-only details.
        expected_tables.update(
            {"smu_bias", "three_smu_run"}.intersection(document)
        )
        if project.mode is RunMode.HARDWARE:
            expected_tables.add("visa")
        _strict_keys(document, "top level", expected_tables)

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
    _strict_keys(
        table,
        name,
        {
            "model",
            "address",
            "reference_source",
            "sine_output_connected",
            "source_voltage_v",
            "frequency_hz",
        },
    )
    model = _string(table["model"], f"{name}.model")
    if model != "SR830":
        raise ConfigError(f"{name}.model must be 'SR830'.")
    reference_source = _enum_value(
        ReferenceSource, table["reference_source"], f"{name}.reference_source"
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
    return LockinConfig(
        role=role,
        model=model,
        address=_string(table["address"], f"{name}.address"),
        reference_source=reference_source,
        sine_output_connected=connected,
        source_voltage_v=source_voltage_v,
        frequency_hz=frequency_hz,
    )


def _validate_lockin_pair(xx: LockinConfig, xy: LockinConfig) -> None:
    if xx.address == xy.address:
        raise ConfigError("lockin_xx and lockin_xy must have distinct addresses.")
    if not math.isclose(xx.frequency_hz, xy.frequency_hz, rel_tol=0.0, abs_tol=1e-12):
        raise ConfigError("lockin_xx and lockin_xy frequencies must match.")


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
    if mode is RunMode.HARDWARE and "smu" in table and not isinstance(table["smu"], dict):
        raise ConfigError(f"{name}.smu must be a table.")
    optional_smu = {"smu"} if mode is RunMode.HARDWARE and "smu" in table else set()
    _strict_keys(table, name, common_keys | mode_keys | optional_smu)
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


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty string.")
    return value


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
