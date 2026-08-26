from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from itertools import product
import math
from pathlib import Path
import tomllib
from typing import Any, Mapping


SEMANTIC_ROLES = ("smu_bias", "gate_top", "gate_bottom")
KEITHLEY_2400_MAX_VOLTAGE_V = 210.0
KEITHLEY_2400_MAX_CURRENT_A = 1.05
KEITHLEY_2400_MAX_POWER_W = 22.05
KEITHLEY_2400_MIN_CURRENT_COMPLIANCE_A = 1e-9
KEITHLEY_2400_MIN_VOLTAGE_COMPLIANCE_V = 200e-6


class ThreeSmuConfigError(ValueError):
    pass


class SourceMode(StrEnum):
    VOLTAGE = "voltage"
    CURRENT = "current"


class ChannelRole(StrEnum):
    OFF = "off"
    FIXED = "fixed"
    SWEEP = "sweep"


class ScanMode(StrEnum):
    TIME_TRACE = "time_trace"
    BIAS_IV = "bias_iv"
    TOP_GATE_TRANSFER = "top_gate_transfer"
    BOTTOM_GATE_TRANSFER = "bottom_gate_transfer"
    PAIRED_GATE = "paired_gate"
    MULTI_SMU_MAP = "multi_smu_map"
    SOFTWARE_PULSE = "software_pulse"


class FinishAction(StrEnum):
    ZERO_DISABLE = "zero_disable"
    HOLD = "hold"


@dataclass(frozen=True, slots=True)
class SmuHardwareConfig:
    role: str
    model: str
    address: str
    source_mode: SourceMode
    max_abs_voltage_v: float | None
    max_abs_current_a: float | None
    nplc: float | None
    source_auto_range: bool
    measure_auto_range: bool
    four_wire: bool

    def __post_init__(self) -> None:
        if self.role not in SEMANTIC_ROLES:
            raise ThreeSmuConfigError(f"Unknown semantic SMU role {self.role!r}")
        for field_name in (
            "max_abs_voltage_v",
            "max_abs_current_a",
            "nplc",
        ):
            value = getattr(self, field_name)
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise ThreeSmuConfigError(
                    f"{self.role}.{field_name} must be finite and positive"
                )
        if self.nplc is not None and not 0.01 <= self.nplc <= 10.0:
            raise ThreeSmuConfigError(f"{self.role}.nplc must be between 0.01 and 10")
        if (
            self.max_abs_voltage_v is not None
            and self.max_abs_voltage_v > KEITHLEY_2400_MAX_VOLTAGE_V
        ):
            raise ThreeSmuConfigError(
                f"{self.role}.max_abs_voltage_v exceeds Keithley 2400 capability"
            )
        if (
            self.max_abs_current_a is not None
            and self.max_abs_current_a > KEITHLEY_2400_MAX_CURRENT_A
        ):
            raise ThreeSmuConfigError(
                f"{self.role}.max_abs_current_a exceeds Keithley 2400 capability"
            )
        if self.max_abs_voltage_v is not None and self.max_abs_current_a is not None:
            if self.max_abs_voltage_v * self.max_abs_current_a > (
                KEITHLEY_2400_MAX_POWER_W * (1.0 + 1e-12)
            ):
                raise ThreeSmuConfigError(
                    f"{self.role} max_abs_voltage_v * max_abs_current_a exceeds "
                    "the Keithley 2400 operating envelope"
                )
        compliance = (
            self.max_abs_current_a
            if self.source_mode is SourceMode.VOLTAGE
            else self.max_abs_voltage_v
        )
        minimum = (
            KEITHLEY_2400_MIN_CURRENT_COMPLIANCE_A
            if self.source_mode is SourceMode.VOLTAGE
            else KEITHLEY_2400_MIN_VOLTAGE_COMPLIANCE_V
        )
        if compliance is not None and compliance < minimum:
            unit_field = (
                "max_abs_current_a"
                if self.source_mode is SourceMode.VOLTAGE
                else "max_abs_voltage_v"
            )
            raise ThreeSmuConfigError(
                f"{self.role}.{unit_field} is below the Keithley 2400 minimum "
                "programmable compliance"
            )
        if not self.source_auto_range or not self.measure_auto_range:
            raise ThreeSmuConfigError(
                f"{self.role} requires source_auto_range=true and "
                "measure_auto_range=true; fixed ranges are not configured"
            )


@dataclass(frozen=True, slots=True)
class ThreeSmuHardwareConfig:
    smu_bias: SmuHardwareConfig | None = None
    gate_top: SmuHardwareConfig | None = None
    gate_bottom: SmuHardwareConfig | None = None

    def by_role(self) -> dict[str, SmuHardwareConfig]:
        return {
            role: config
            for role in SEMANTIC_ROLES
            if (config := getattr(self, role)) is not None
        }

    def require_role(self, role: str) -> SmuHardwareConfig:
        config = self.by_role().get(role)
        if config is None:
            raise ThreeSmuConfigError(f"active role {role} has no [{role}] hardware table")
        return config

    def readiness_errors(self, roles: tuple[str, ...] | None = None) -> tuple[str, ...]:
        errors: list[str] = []
        configured_addresses: list[str] = []
        checked_roles = tuple(self.by_role()) if roles is None else roles
        for role in checked_roles:
            config = self.by_role().get(role)
            if config is None:
                errors.append(f"active role {role} has no [{role}] hardware table")
                continue
            if config.model != "Keithley2400":
                errors.append(f"{role}.model must be 'Keithley2400'")
            if _is_placeholder(config.address):
                errors.append(f"{role}.address is not configured")
            else:
                configured_addresses.append(config.address)
            for field_name in ("max_abs_voltage_v", "max_abs_current_a", "nplc"):
                if getattr(config, field_name) is None:
                    errors.append(f"{role}.{field_name} is not configured")
        if len(set(configured_addresses)) != len(configured_addresses):
            errors.append("all active SMU addresses must be distinct")
        return tuple(errors)

    def require_ready(self, roles: tuple[str, ...] | None = None) -> None:
        errors = self.readiness_errors(roles)
        if errors:
            raise ThreeSmuConfigError(
                "Three-SMU hardware configuration is not ready: " + "; ".join(errors)
            )


@dataclass(frozen=True, slots=True)
class ChannelPlan:
    role: ChannelRole
    bidirectional: bool
    fixed: float | None = None
    start: float | None = None
    stop: float | None = None
    step: float | None = None
    points: tuple[float, ...] | None = None

    def __post_init__(self) -> None:
        for field_name in ("fixed", "start", "stop", "step"):
            value = getattr(self, field_name)
            if value is not None and not math.isfinite(value):
                raise ThreeSmuConfigError(f"channel {field_name} must be finite")
        if self.points is not None:
            if not self.points or not all(math.isfinite(value) for value in self.points):
                raise ThreeSmuConfigError("channel points must be non-empty and finite")
        if self.role is ChannelRole.OFF:
            if self.bidirectional or any(
                value is not None
                for value in (self.fixed, self.start, self.stop, self.step, self.points)
            ):
                raise ThreeSmuConfigError(
                    "off channel allows only role='off' and bidirectional=false"
                )
            return
        if self.role is ChannelRole.FIXED:
            if self.bidirectional or self.fixed is None or any(
                value is not None for value in (self.start, self.stop, self.step, self.points)
            ):
                raise ThreeSmuConfigError(
                    "fixed channel requires only fixed and bidirectional=false"
                )
            return
        range_values = (self.start, self.stop, self.step)
        has_range = all(value is not None for value in range_values)
        if any(value is not None for value in range_values) and not has_range:
            raise ThreeSmuConfigError("sweep range requires start, stop, and step")
        if (self.points is None) == (not has_range):
            raise ThreeSmuConfigError(
                "sweep channel requires exactly one of points or start/stop/step"
            )
        if self.step is not None and self.step <= 0:
            raise ThreeSmuConfigError("channel step must be positive")


@dataclass(frozen=True, slots=True)
class ThreeSmuScanPlan:
    mode: ScanMode
    samples_per_point: int
    delay_s: float
    serpentine: bool
    finish_action: FinishAction
    point_count: int
    pulse_high_s: float
    pulse_period_s: float
    smu_bias: ChannelPlan
    gate_top: ChannelPlan
    gate_bottom: ChannelPlan

    def by_role(self) -> dict[str, ChannelPlan]:
        return {role: getattr(self, role) for role in SEMANTIC_ROLES}

    def __post_init__(self) -> None:
        if self.samples_per_point < 1 or self.point_count < 1:
            raise ThreeSmuConfigError(
                "samples_per_point and point_count must be positive integers"
            )
        for name, value in (
            ("delay_s", self.delay_s),
            ("pulse_high_s", self.pulse_high_s),
            ("pulse_period_s", self.pulse_period_s),
        ):
            if not math.isfinite(value) or value < 0:
                raise ThreeSmuConfigError(f"{name} must be finite and non-negative")
        _validate_scan_shape(self)


@dataclass(frozen=True, slots=True)
class ThreeSmuOperationConfig:
    """One local daily-operation file: shared gate limits plus Three-SMU plan."""

    hardware: ThreeSmuHardwareConfig
    plan: ThreeSmuScanPlan
    output_directory: Path
    run_name: str
    note: str
    config_path: Path


@dataclass(frozen=True, slots=True)
class ScanPoint:
    index: int
    segment: str
    coordinates: dict[str, float]
    post_delay_s: float = 0.0


def load_three_smu_operation_config(path: str | Path) -> ThreeSmuOperationConfig:
    """Load only the Three-SMU portion of ``hardware.local.toml``."""

    config_path = Path(path).resolve()
    document = _load_toml(config_path)
    run = _table(document, "three_smu_run")
    expected = {
        "mode",
        "samples_per_point",
        "delay_s",
        "serpentine",
        "finish_action",
        "point_count",
        "pulse_high_s",
        "pulse_period_s",
        "output_directory",
        "run_name",
        "note",
        *SEMANTIC_ROLES,
    }
    _strict_keys(run, "three_smu_run", expected)
    plan = ThreeSmuScanPlan(
        mode=_enum(ScanMode, run["mode"], "three_smu_run.mode"),
        samples_per_point=_integer(
            run["samples_per_point"], "three_smu_run.samples_per_point", 1
        ),
        delay_s=_nonnegative(run["delay_s"], "three_smu_run.delay_s"),
        serpentine=_boolean(run["serpentine"], "three_smu_run.serpentine"),
        finish_action=_enum(
            FinishAction, run["finish_action"], "three_smu_run.finish_action"
        ),
        point_count=_integer(run["point_count"], "three_smu_run.point_count", 1),
        pulse_high_s=_nonnegative(
            run["pulse_high_s"], "three_smu_run.pulse_high_s"
        ),
        pulse_period_s=_nonnegative(
            run["pulse_period_s"], "three_smu_run.pulse_period_s"
        ),
        **{
            role: _parse_channel(_table(run, role), f"three_smu_run.{role}")
            for role in SEMANTIC_ROLES
        },
    )
    active_roles = active_smu_roles(plan)
    parsed_hardware = {
        role: _parse_hardware_role(_table(document, role), role)
        for role in active_roles
    }
    hardware = ThreeSmuHardwareConfig(
        **{role: parsed_hardware.get(role) for role in SEMANTIC_ROLES}
    )
    output_directory = Path(
        _string(run["output_directory"], "three_smu_run.output_directory")
    )
    if not output_directory.is_absolute():
        output_directory = config_path.parent / output_directory
    return ThreeSmuOperationConfig(
        hardware=hardware,
        plan=plan,
        output_directory=output_directory.resolve(),
        run_name=_string(run["run_name"], "three_smu_run.run_name"),
        note=_note(run["note"], "three_smu_run.note"),
        config_path=config_path,
    )


def validate_plan_targets(
    hardware: ThreeSmuHardwareConfig,
    plan: ThreeSmuScanPlan,
) -> tuple[ScanPoint, ...]:
    active_roles = active_smu_roles(plan)
    hardware.require_ready(active_roles)
    points = generate_scan_points(plan)
    for point in points:
        for role, target in point.coordinates.items():
            config = hardware.require_role(role)
            limit = (
                config.max_abs_voltage_v
                if config.source_mode is SourceMode.VOLTAGE
                else config.max_abs_current_a
            )
            assert limit is not None
            if abs(target) > limit:
                raise ThreeSmuConfigError(
                    f"{role} target {target:g} exceeds its source-mode absolute "
                    f"limit {limit:g}"
                )
    return points


def active_smu_roles(plan: ThreeSmuScanPlan) -> tuple[str, ...]:
    return tuple(
        role
        for role, channel in plan.by_role().items()
        if channel.role is not ChannelRole.OFF
    )


def generate_scan_points(plan: ThreeSmuScanPlan) -> tuple[ScanPoint, ...]:
    channels = plan.by_role()
    base = {
        role: float(channel.fixed)
        for role, channel in channels.items()
        if channel.role is ChannelRole.FIXED
    }
    points: list[tuple[str, dict[str, float], float]] = []

    if plan.mode is ScanMode.TIME_TRACE:
        points = [("time", dict(base), 0.0) for _ in range(plan.point_count)]
    elif plan.mode is ScanMode.SOFTWARE_PULSE:
        sweep_role = _sweep_roles(plan)[0]
        channel = channels[sweep_role]
        pulse_values = _sweep_values(channel)
        if len(pulse_values) != 2:
            raise ThreeSmuConfigError(
                "software_pulse sweep must resolve to exactly two values"
            )
        for _ in range(plan.point_count):
            high = dict(base)
            high[sweep_role] = pulse_values[1]
            points.append(
                ("pulse_high", high, max(plan.pulse_high_s - plan.delay_s, 0.0))
            )
            low = dict(base)
            low[sweep_role] = pulse_values[0]
            points.append(
                (
                    "pulse_low",
                    low,
                    max(plan.pulse_period_s - plan.pulse_high_s - plan.delay_s, 0.0),
                )
            )
    elif plan.mode is ScanMode.PAIRED_GATE:
        top = _expanded_sweep_values(channels["gate_top"])
        bottom = _expanded_sweep_values(channels["gate_bottom"])
        paired = [
            ("forward", {**base, "gate_top": top_value, "gate_bottom": bottom_value}, 0.0)
            for top_value, bottom_value in zip(top, bottom, strict=True)
        ]
        points = paired
    elif plan.mode is ScanMode.MULTI_SMU_MAP:
        points = _multi_map_points(plan, base)
    else:
        sweep_role = _sweep_roles(plan)[0]
        values = _sweep_values(channels[sweep_role])
        expanded = _expanded_sweep_values(channels[sweep_role])
        points = [
            (
                "reverse" if index >= len(values) else "forward",
                {**base, sweep_role: value},
                0.0,
            )
            for index, value in enumerate(expanded)
        ]

    return tuple(
        ScanPoint(index=index, segment=segment, coordinates=coordinates, post_delay_s=post)
        for index, (segment, coordinates, post) in enumerate(points)
    )


def _multi_map_points(
    plan: ThreeSmuScanPlan,
    base: dict[str, float],
) -> list[tuple[str, dict[str, float], float]]:
    channels = plan.by_role()
    roles = _sweep_roles(plan)
    values = [_expanded_sweep_values(channels[role]) for role in roles]
    rows: list[tuple[str, dict[str, float], float]] = []
    if len(roles) == 1:
        rows = [("forward", {**base, roles[0]: value}, 0.0) for value in values[0]]
    else:
        outer_ranges = values[:-1]
        for outer_index, outer_values in enumerate(product(*outer_ranges)):
            inner = values[-1]
            if plan.serpentine and outer_index % 2:
                inner = tuple(reversed(inner))
            for inner_value in inner:
                coordinates = dict(base)
                coordinates.update(dict(zip(roles[:-1], outer_values, strict=True)))
                coordinates[roles[-1]] = inner_value
                rows.append(("serpentine" if plan.serpentine else "forward", coordinates, 0.0))
    return rows


def _sweep_values(channel: ChannelPlan) -> tuple[float, ...]:
    if channel.points is not None:
        return channel.points
    assert channel.start is not None
    assert channel.stop is not None
    assert channel.step is not None
    direction = 1.0 if channel.stop >= channel.start else -1.0
    step = abs(channel.step) * direction
    values = [channel.start]
    tolerance = abs(step) * 1e-9 + 1e-15
    while (values[-1] + step - channel.stop) * direction < -tolerance:
        values.append(values[-1] + step)
    if not math.isclose(values[-1], channel.stop, rel_tol=0.0, abs_tol=tolerance):
        values.append(channel.stop)
    return tuple(float(value) for value in values)


def _expanded_sweep_values(channel: ChannelPlan) -> tuple[float, ...]:
    values = _sweep_values(channel)
    if not channel.bidirectional or len(values) < 2:
        return values
    return values + tuple(reversed(values[:-1]))


def _validate_scan_shape(plan: ThreeSmuScanPlan) -> None:
    sweeps = _sweep_roles(plan)
    expected: dict[ScanMode, tuple[str, ...]] = {
        ScanMode.BIAS_IV: ("smu_bias",),
        ScanMode.TOP_GATE_TRANSFER: ("gate_top",),
        ScanMode.BOTTOM_GATE_TRANSFER: ("gate_bottom",),
        ScanMode.PAIRED_GATE: ("gate_top", "gate_bottom"),
    }
    if plan.mode in expected and tuple(sweeps) != expected[plan.mode]:
        raise ThreeSmuConfigError(
            f"{plan.mode.value} requires sweep role(s): {', '.join(expected[plan.mode])}"
        )
    if plan.mode is ScanMode.TIME_TRACE and sweeps:
        raise ThreeSmuConfigError("time_trace does not allow sweep channels")
    if plan.mode is ScanMode.MULTI_SMU_MAP and not 1 <= len(sweeps) <= 3:
        raise ThreeSmuConfigError("multi_smu_map requires one to three sweep channels")
    if plan.mode is ScanMode.SOFTWARE_PULSE:
        if len(sweeps) != 1:
            raise ThreeSmuConfigError("software_pulse requires exactly one sweep channel")
        if plan.pulse_high_s <= 0 or plan.pulse_period_s < plan.pulse_high_s:
            raise ThreeSmuConfigError(
                "software_pulse requires pulse_high_s > 0 and pulse_period_s >= pulse_high_s"
            )
        if plan.by_role()[sweeps[0]].bidirectional:
            raise ThreeSmuConfigError(
                "software_pulse does not allow bidirectional sweep channels"
            )
        if len(_sweep_values(plan.by_role()[sweeps[0]])) != 2:
            raise ThreeSmuConfigError(
                "software_pulse sweep must resolve to exactly two values"
            )
    elif plan.pulse_high_s != 0 or plan.pulse_period_s != 0:
        raise ThreeSmuConfigError(
            "pulse timing values must be zero outside software_pulse mode"
        )
    if plan.mode is ScanMode.PAIRED_GATE:
        top = _expanded_sweep_values(plan.gate_top)
        bottom = _expanded_sweep_values(plan.gate_bottom)
        if len(top) != len(bottom):
            raise ThreeSmuConfigError(
                "paired_gate top and bottom sweeps must have the same point count"
            )


def _sweep_roles(plan: ThreeSmuScanPlan) -> list[str]:
    return [
        role
        for role, channel in plan.by_role().items()
        if channel.role is ChannelRole.SWEEP
    ]


def _parse_hardware_role(
    table: Mapping[str, Any],
    role: str,
) -> SmuHardwareConfig:
    common = {
        "model",
        "address",
        "source_mode",
        "max_abs_voltage_v",
        "max_abs_current_a",
        "nplc",
        "source_auto_range",
        "measure_auto_range",
        "four_wire",
    }
    _strict_keys(table, role, common)
    source_mode = _enum(SourceMode, table["source_mode"], f"{role}.source_mode")
    config = SmuHardwareConfig(
        role=role,
        model=_string(table["model"], f"{role}.model"),
        address=_string(table["address"], f"{role}.address"),
        source_mode=source_mode,
        max_abs_voltage_v=_placeholder_positive(
            table["max_abs_voltage_v"], f"{role}.max_abs_voltage_v"
        ),
        max_abs_current_a=_placeholder_positive(
            table["max_abs_current_a"], f"{role}.max_abs_current_a"
        ),
        nplc=_placeholder_positive(table["nplc"], f"{role}.nplc"),
        source_auto_range=_boolean(
            table["source_auto_range"], f"{role}.source_auto_range"
        ),
        measure_auto_range=_boolean(
            table["measure_auto_range"], f"{role}.measure_auto_range"
        ),
        four_wire=_boolean(table["four_wire"], f"{role}.four_wire"),
    )
    return config


def _parse_channel(table: Mapping[str, Any], role: str) -> ChannelPlan:
    channel_role = _enum(ChannelRole, table.get("role"), f"{role}.role")
    base = {"role", "bidirectional"}
    if channel_role is ChannelRole.OFF:
        expected = base
    elif channel_role is ChannelRole.FIXED:
        expected = base | {"fixed"}
    else:
        has_points = "points" in table
        expected = base | ({"points"} if has_points else {"start", "stop", "step"})
    _strict_keys(table, role, expected)
    return ChannelPlan(
        role=channel_role,
        bidirectional=_boolean(table["bidirectional"], f"{role}.bidirectional"),
        fixed=(
            _number(table["fixed"], f"{role}.fixed")
            if channel_role is ChannelRole.FIXED
            else None
        ),
        start=(
            _number(table["start"], f"{role}.start")
            if "start" in table
            else None
        ),
        stop=(
            _number(table["stop"], f"{role}.stop") if "stop" in table else None
        ),
        step=(
            _positive(table["step"], f"{role}.step") if "step" in table else None
        ),
        points=(
            _number_vector(table["points"], f"{role}.points")
            if "points" in table
            else None
        ),
    )


def _load_toml(path: str | Path) -> Mapping[str, Any]:
    config_path = Path(path)
    try:
        with config_path.open("rb") as file:
            return tomllib.load(file)
    except tomllib.TOMLDecodeError as exc:
        raise ThreeSmuConfigError(f"Invalid TOML in {config_path}: {exc}") from exc
    except OSError as exc:
        raise ThreeSmuConfigError(f"Could not read {config_path}: {exc}") from exc


def _table(document: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    value = document.get(name)
    if not isinstance(value, dict):
        raise ThreeSmuConfigError(f"Missing or invalid [{name}] table")
    return value


def _strict_keys(table: Mapping[str, Any], name: str, expected: set[str]) -> None:
    actual = set(table)
    missing = sorted(expected - actual)
    unknown = sorted(actual - expected)
    if missing:
        raise ThreeSmuConfigError(f"{name} is missing field(s): {', '.join(missing)}")
    if unknown:
        raise ThreeSmuConfigError(f"{name} has unknown field(s): {', '.join(unknown)}")


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ThreeSmuConfigError(f"{name} must be a non-empty string")
    return value.strip()


def _note(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ThreeSmuConfigError(f"{name} must be a string")
    return value.strip()


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ThreeSmuConfigError(f"{name} must be a boolean")
    return value


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ThreeSmuConfigError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ThreeSmuConfigError(f"{name} must be finite")
    return result


def _number_vector(value: Any, name: str) -> tuple[float, ...]:
    if not isinstance(value, list) or not value:
        raise ThreeSmuConfigError(f"{name} must be a non-empty array of numbers")
    return tuple(_number(item, f"{name}[{index}]") for index, item in enumerate(value))


def _positive(value: Any, name: str) -> float:
    result = _number(value, name)
    if result <= 0:
        raise ThreeSmuConfigError(f"{name} must be positive")
    return result


def _nonnegative(value: Any, name: str) -> float:
    result = _number(value, name)
    if result < 0:
        raise ThreeSmuConfigError(f"{name} must be non-negative")
    return result


def _integer(value: Any, name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ThreeSmuConfigError(f"{name} must be an integer of at least {minimum}")
    return value


def _placeholder_positive(value: Any, name: str) -> float | None:
    return None if value == "CHANGE_ME" else _positive(value, name)


def _is_placeholder(value: str) -> bool:
    return value == "CHANGE_ME" or "CHANGE_ME" in value


def _enum(enum_type: type[StrEnum], value: Any, name: str) -> Any:
    raw = _string(value, name)
    try:
        return enum_type(raw)
    except ValueError as exc:
        choices = ", ".join(repr(item.value) for item in enum_type)
        raise ThreeSmuConfigError(f"{name} must be one of: {choices}") from exc
