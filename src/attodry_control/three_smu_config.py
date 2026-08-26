from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from itertools import product
import math
from pathlib import Path
import tomllib
from typing import Any, Mapping


SEMANTIC_ROLES = ("smu_bias", "gate_top", "gate_bottom")


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
    timeout_ms: int | None
    source_mode: SourceMode
    compliance_current_a: float | None
    compliance_voltage_v: float | None
    max_abs_voltage_v: float | None
    max_abs_current_a: float | None
    source_min_v: float | None
    source_max_v: float | None
    ramp_step_v: float | None
    readback_tolerance_v: float | None
    source_min_a: float | None
    source_max_a: float | None
    ramp_step_a: float | None
    readback_tolerance_a: float | None
    settle_s: float | None
    nplc: float | None
    source_auto_range: bool
    measure_auto_range: bool
    four_wire: bool
    leakage_limit_a: float | None = None

    def __post_init__(self) -> None:
        if self.role not in SEMANTIC_ROLES:
            raise ThreeSmuConfigError(f"Unknown semantic SMU role {self.role!r}")
        if self.timeout_ms is not None and self.timeout_ms < 1:
            raise ThreeSmuConfigError(f"{self.role}.timeout_ms must be positive")
        for field_name in (
            "compliance_current_a",
            "compliance_voltage_v",
            "max_abs_voltage_v",
            "max_abs_current_a",
            "ramp_step_v",
            "readback_tolerance_v",
            "ramp_step_a",
            "readback_tolerance_a",
            "nplc",
        ):
            value = getattr(self, field_name)
            if value is not None and (not math.isfinite(value) or value <= 0):
                raise ThreeSmuConfigError(
                    f"{self.role}.{field_name} must be finite and positive"
                )
        if self.settle_s is not None and (
            not math.isfinite(self.settle_s) or self.settle_s < 0
        ):
            raise ThreeSmuConfigError(
                f"{self.role}.settle_s must be finite and non-negative"
            )
        self._validate_source_range("v", self.max_abs_voltage_v)
        self._validate_source_range("a", self.max_abs_current_a)
        if (
            self.compliance_current_a is not None
            and self.max_abs_current_a is not None
            and self.compliance_current_a > self.max_abs_current_a
        ):
            raise ThreeSmuConfigError(
                f"{self.role}.compliance_current_a cannot exceed max_abs_current_a"
            )
        if (
            self.compliance_voltage_v is not None
            and self.max_abs_voltage_v is not None
            and self.compliance_voltage_v > self.max_abs_voltage_v
        ):
            raise ThreeSmuConfigError(
                f"{self.role}.compliance_voltage_v cannot exceed max_abs_voltage_v"
            )
        if self.leakage_limit_a is not None:
            if not math.isfinite(self.leakage_limit_a) or self.leakage_limit_a <= 0:
                raise ThreeSmuConfigError(
                    f"{self.role}.leakage_limit_a must be finite and positive"
                )
            if (
                self.compliance_current_a is not None
                and self.leakage_limit_a > self.compliance_current_a
            ):
                raise ThreeSmuConfigError(
                    f"{self.role}.leakage_limit_a cannot exceed compliance_current_a"
                )
            if (
                self.max_abs_current_a is not None
                and self.leakage_limit_a > self.max_abs_current_a
            ):
                raise ThreeSmuConfigError(
                    f"{self.role}.leakage_limit_a cannot exceed max_abs_current_a"
                )

    @property
    def source_min(self) -> float | None:
        return (
            self.source_min_v
            if self.source_mode is SourceMode.VOLTAGE
            else self.source_min_a
        )

    @property
    def source_max(self) -> float | None:
        return (
            self.source_max_v
            if self.source_mode is SourceMode.VOLTAGE
            else self.source_max_a
        )

    @property
    def ramp_step(self) -> float | None:
        return (
            self.ramp_step_v
            if self.source_mode is SourceMode.VOLTAGE
            else self.ramp_step_a
        )

    @property
    def readback_tolerance(self) -> float | None:
        return (
            self.readback_tolerance_v
            if self.source_mode is SourceMode.VOLTAGE
            else self.readback_tolerance_a
        )

    def _validate_source_range(self, unit: str, absolute_limit: float | None) -> None:
        minimum = getattr(self, f"source_min_{unit}")
        maximum = getattr(self, f"source_max_{unit}")
        if minimum is None or maximum is None:
            return
        if not (
            math.isfinite(minimum)
            and math.isfinite(maximum)
            and minimum < maximum
        ):
            raise ThreeSmuConfigError(
                f"{self.role}.source_min_{unit} must be below source_max_{unit}"
            )
        if not minimum <= 0 <= maximum:
            raise ThreeSmuConfigError(
                f"{self.role} {unit.upper()} source range must include zero"
            )
        if absolute_limit is not None and (
            abs(minimum) > absolute_limit or abs(maximum) > absolute_limit
        ):
            raise ThreeSmuConfigError(
                f"{self.role} {unit.upper()} source range exceeds max_abs_"
                f"{'voltage_v' if unit == 'v' else 'current_a'}"
            )


@dataclass(frozen=True, slots=True)
class ThreeSmuHardwareConfig:
    smu_bias: SmuHardwareConfig
    gate_top: SmuHardwareConfig
    gate_bottom: SmuHardwareConfig

    def by_role(self) -> dict[str, SmuHardwareConfig]:
        return {role: getattr(self, role) for role in SEMANTIC_ROLES}

    def readiness_errors(self) -> tuple[str, ...]:
        errors: list[str] = []
        configured_addresses: list[str] = []
        for role, config in self.by_role().items():
            if config.model != "Keithley2400":
                errors.append(f"{role}.model must be 'Keithley2400'")
            if _is_placeholder(config.address):
                errors.append(f"{role}.address is not configured")
            else:
                configured_addresses.append(config.address)
            for field_name in (
                "timeout_ms",
                "compliance_current_a",
                "compliance_voltage_v",
                "max_abs_voltage_v",
                "max_abs_current_a",
                "settle_s",
                "nplc",
            ):
                if getattr(config, field_name) is None:
                    errors.append(f"{role}.{field_name} is not configured")
            suffix = "v" if config.source_mode is SourceMode.VOLTAGE else "a"
            for field_name in (
                f"source_min_{suffix}",
                f"source_max_{suffix}",
                f"ramp_step_{suffix}",
                f"readback_tolerance_{suffix}",
            ):
                if getattr(config, field_name) is None:
                    errors.append(f"{role}.{field_name} is not configured")
            if (
                role != "smu_bias"
                and config.source_mode is SourceMode.VOLTAGE
                and config.leakage_limit_a is None
            ):
                errors.append(f"{role}.leakage_limit_a is not configured")
        if len(set(configured_addresses)) != len(configured_addresses):
            errors.append("all three SMU addresses must be distinct")
        return tuple(errors)

    def require_ready(self) -> None:
        errors = self.readiness_errors()
        if errors:
            raise ThreeSmuConfigError(
                "Three-SMU hardware configuration is not ready: " + "; ".join(errors)
            )


@dataclass(frozen=True, slots=True)
class ChannelPlan:
    role: ChannelRole
    fixed: float
    start: float
    stop: float
    step: float

    def __post_init__(self) -> None:
        for field_name in ("fixed", "start", "stop", "step"):
            value = getattr(self, field_name)
            if not math.isfinite(value):
                raise ThreeSmuConfigError(f"channel {field_name} must be finite")
        if self.step <= 0:
            raise ThreeSmuConfigError("channel step must be positive")


@dataclass(frozen=True, slots=True)
class ThreeSmuScanPlan:
    mode: ScanMode
    samples_per_point: int
    delay_s: float
    bidirectional: bool
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


def load_three_smu_hardware(path: str | Path) -> ThreeSmuHardwareConfig:
    document = _load_toml(path)
    _strict_keys(document, "top level", set(SEMANTIC_ROLES))
    configs = {
        role: _parse_hardware_role(_table(document, role), role)
        for role in SEMANTIC_ROLES
    }
    hardware = ThreeSmuHardwareConfig(**configs)
    addresses = [
        config.address
        for config in configs.values()
        if not _is_placeholder(config.address)
    ]
    if len(set(addresses)) != len(addresses):
        raise ThreeSmuConfigError("all three SMU addresses must be distinct")
    return hardware


def load_three_smu_scan(path: str | Path) -> ThreeSmuScanPlan:
    document = _load_toml(path)
    _strict_keys(document, "top level", {"scan", *SEMANTIC_ROLES})
    scan = _table(document, "scan")
    _strict_keys(
        scan,
        "scan",
        {
            "mode",
            "samples_per_point",
            "delay_s",
            "bidirectional",
            "serpentine",
            "finish_action",
            "point_count",
            "pulse_high_s",
            "pulse_period_s",
        },
    )
    channels = {
        role: _parse_channel(_table(document, role), role)
        for role in SEMANTIC_ROLES
    }
    plan = ThreeSmuScanPlan(
        mode=_enum(ScanMode, scan["mode"], "scan.mode"),
        samples_per_point=_integer(scan["samples_per_point"], "scan.samples_per_point", 1),
        delay_s=_nonnegative(scan["delay_s"], "scan.delay_s"),
        bidirectional=_boolean(scan["bidirectional"], "scan.bidirectional"),
        serpentine=_boolean(scan["serpentine"], "scan.serpentine"),
        finish_action=_enum(FinishAction, scan["finish_action"], "scan.finish_action"),
        point_count=_integer(scan["point_count"], "scan.point_count", 1),
        pulse_high_s=_nonnegative(scan["pulse_high_s"], "scan.pulse_high_s"),
        pulse_period_s=_nonnegative(scan["pulse_period_s"], "scan.pulse_period_s"),
        **channels,
    )
    return plan


def load_three_smu_operation_config(path: str | Path) -> ThreeSmuOperationConfig:
    """Load the Three-SMU portion of the ordinary ``hardware.local.toml``.

    ``gate_top`` and ``gate_bottom`` retain the shared gate-safety fields used by
    the rest of the project.  Their nested ``[gate_*.smu]`` tables contain only
    2400-specific settings, so voltage and leakage limits have one source of
    truth.
    """

    config_path = Path(path).resolve()
    document = _load_toml(config_path)
    hardware = ThreeSmuHardwareConfig(
        smu_bias=_parse_hardware_role(_table(document, "smu_bias"), "smu_bias"),
        gate_top=_parse_operation_gate(_table(document, "gate_top"), "gate_top"),
        gate_bottom=_parse_operation_gate(
            _table(document, "gate_bottom"), "gate_bottom"
        ),
    )
    run = _table(document, "three_smu_run")
    expected = {
        "mode",
        "samples_per_point",
        "delay_s",
        "bidirectional",
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
        bidirectional=_boolean(run["bidirectional"], "three_smu_run.bidirectional"),
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
    hardware.require_ready()
    points = generate_scan_points(plan)
    for point in points:
        for role, target in point.coordinates.items():
            config = hardware.by_role()[role]
            assert config.source_min is not None and config.source_max is not None
            if not config.source_min <= target <= config.source_max:
                raise ThreeSmuConfigError(
                    f"{role} target {target:g} is outside configured source range "
                    f"[{config.source_min:g}, {config.source_max:g}]"
                )
    return points


def generate_scan_points(plan: ThreeSmuScanPlan) -> tuple[ScanPoint, ...]:
    channels = plan.by_role()
    base = {
        role: channel.fixed
        for role, channel in channels.items()
        if channel.role is ChannelRole.FIXED
    }
    points: list[tuple[str, dict[str, float], float]] = []

    if plan.mode is ScanMode.TIME_TRACE:
        points = [("time", dict(base), 0.0) for _ in range(plan.point_count)]
    elif plan.mode is ScanMode.SOFTWARE_PULSE:
        sweep_role = _sweep_roles(plan)[0]
        channel = channels[sweep_role]
        for _ in range(plan.point_count):
            high = dict(base)
            high[sweep_role] = channel.stop
            points.append(
                ("pulse_high", high, max(plan.pulse_high_s - plan.delay_s, 0.0))
            )
            low = dict(base)
            low[sweep_role] = channel.start
            points.append(
                (
                    "pulse_low",
                    low,
                    max(plan.pulse_period_s - plan.pulse_high_s - plan.delay_s, 0.0),
                )
            )
    elif plan.mode is ScanMode.PAIRED_GATE:
        top = _sweep_values(channels["gate_top"])
        bottom = _sweep_values(channels["gate_bottom"])
        paired = [
            ("forward", {**base, "gate_top": top_value, "gate_bottom": bottom_value}, 0.0)
            for top_value, bottom_value in zip(top, bottom, strict=True)
        ]
        points = _with_reverse(paired) if plan.bidirectional else paired
    elif plan.mode is ScanMode.MULTI_SMU_MAP:
        points = _multi_map_points(plan, base)
    else:
        sweep_role = _sweep_roles(plan)[0]
        forward = [
            ("forward", {**base, sweep_role: value}, 0.0)
            for value in _sweep_values(channels[sweep_role])
        ]
        points = _with_reverse(forward) if plan.bidirectional else forward

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
    values = [_sweep_values(channels[role]) for role in roles]
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
    return _with_reverse(rows) if plan.bidirectional else rows


def _with_reverse(
    rows: list[tuple[str, dict[str, float], float]],
) -> list[tuple[str, dict[str, float], float]]:
    if len(rows) < 2:
        return rows
    reverse = [
        ("reverse", dict(coordinates), post)
        for _segment, coordinates, post in reversed(rows[:-1])
    ]
    return rows + reverse


def _sweep_values(channel: ChannelPlan) -> tuple[float, ...]:
    direction = 1.0 if channel.stop >= channel.start else -1.0
    step = abs(channel.step) * direction
    values = [channel.start]
    tolerance = abs(step) * 1e-9 + 1e-15
    while (values[-1] + step - channel.stop) * direction < -tolerance:
        values.append(values[-1] + step)
    if not math.isclose(values[-1], channel.stop, rel_tol=0.0, abs_tol=tolerance):
        values.append(channel.stop)
    return tuple(float(value) for value in values)


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
    elif plan.pulse_high_s != 0 or plan.pulse_period_s != 0:
        raise ThreeSmuConfigError(
            "pulse timing values must be zero outside software_pulse mode"
        )
    if plan.mode is ScanMode.PAIRED_GATE:
        top = _sweep_values(plan.gate_top)
        bottom = _sweep_values(plan.gate_bottom)
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
        "timeout_ms",
        "source_mode",
        "compliance_current_a",
        "compliance_voltage_v",
        "max_abs_voltage_v",
        "max_abs_current_a",
        "source_min_v",
        "source_max_v",
        "ramp_step_v",
        "readback_tolerance_v",
        "source_min_a",
        "source_max_a",
        "ramp_step_a",
        "readback_tolerance_a",
        "settle_s",
        "nplc",
        "source_auto_range",
        "measure_auto_range",
        "four_wire",
    }
    expected = common | ({"leakage_limit_a"} if role != "smu_bias" else set())
    _strict_keys(table, role, expected)
    source_mode = _enum(SourceMode, table["source_mode"], f"{role}.source_mode")
    config = SmuHardwareConfig(
        role=role,
        model=_string(table["model"], f"{role}.model"),
        address=_string(table["address"], f"{role}.address"),
        timeout_ms=_placeholder_integer(table["timeout_ms"], f"{role}.timeout_ms"),
        source_mode=source_mode,
        compliance_current_a=_placeholder_positive(
            table["compliance_current_a"], f"{role}.compliance_current_a"
        ),
        compliance_voltage_v=_placeholder_positive(
            table["compliance_voltage_v"], f"{role}.compliance_voltage_v"
        ),
        max_abs_voltage_v=_placeholder_positive(
            table["max_abs_voltage_v"], f"{role}.max_abs_voltage_v"
        ),
        max_abs_current_a=_placeholder_positive(
            table["max_abs_current_a"], f"{role}.max_abs_current_a"
        ),
        source_min_v=_placeholder_number(table["source_min_v"], f"{role}.source_min_v"),
        source_max_v=_placeholder_number(table["source_max_v"], f"{role}.source_max_v"),
        ramp_step_v=_placeholder_positive(table["ramp_step_v"], f"{role}.ramp_step_v"),
        readback_tolerance_v=_placeholder_positive(
            table["readback_tolerance_v"], f"{role}.readback_tolerance_v"
        ),
        source_min_a=_placeholder_number(table["source_min_a"], f"{role}.source_min_a"),
        source_max_a=_placeholder_number(table["source_max_a"], f"{role}.source_max_a"),
        ramp_step_a=_placeholder_positive(table["ramp_step_a"], f"{role}.ramp_step_a"),
        readback_tolerance_a=_placeholder_positive(
            table["readback_tolerance_a"], f"{role}.readback_tolerance_a"
        ),
        settle_s=_placeholder_nonnegative(table["settle_s"], f"{role}.settle_s"),
        nplc=_placeholder_positive(table["nplc"], f"{role}.nplc"),
        source_auto_range=_boolean(
            table["source_auto_range"], f"{role}.source_auto_range"
        ),
        measure_auto_range=_boolean(
            table["measure_auto_range"], f"{role}.measure_auto_range"
        ),
        four_wire=_boolean(table["four_wire"], f"{role}.four_wire"),
        leakage_limit_a=(
            None
            if role == "smu_bias"
            else _placeholder_positive(
                table["leakage_limit_a"], f"{role}.leakage_limit_a"
            )
        ),
    )
    return config


def _parse_operation_gate(table: Mapping[str, Any], role: str) -> SmuHardwareConfig:
    expected = {
        "model",
        "address",
        "source_mode",
        "compliance_a",
        "compliance_voltage_v",
        "leakage_limit_a",
        "max_abs_voltage_v",
        "max_abs_current_a",
        "source_min_v",
        "source_max_v",
        "ramp_step_v",
        "readback_tolerance_v",
        "source_min_a",
        "source_max_a",
        "ramp_step_a",
        "readback_tolerance_a",
        "settle_s",
        "smu",
    }
    _strict_keys(table, role, expected)
    smu = _table(table, "smu")
    _strict_keys(
        smu,
        f"{role}.smu",
        {
            "timeout_ms",
            "nplc",
            "source_auto_range",
            "measure_auto_range",
            "four_wire",
        },
    )
    return SmuHardwareConfig(
        role=role,
        model=_string(table["model"], f"{role}.model"),
        address=_string(table["address"], f"{role}.address"),
        timeout_ms=_placeholder_integer(smu["timeout_ms"], f"{role}.smu.timeout_ms"),
        source_mode=_enum(SourceMode, table["source_mode"], f"{role}.source_mode"),
        compliance_current_a=_placeholder_positive(
            table["compliance_a"], f"{role}.compliance_a"
        ),
        compliance_voltage_v=_placeholder_positive(
            table["compliance_voltage_v"], f"{role}.compliance_voltage_v"
        ),
        max_abs_voltage_v=_placeholder_positive(
            table["max_abs_voltage_v"], f"{role}.max_abs_voltage_v"
        ),
        max_abs_current_a=_placeholder_positive(
            table["max_abs_current_a"], f"{role}.max_abs_current_a"
        ),
        source_min_v=_placeholder_number(table["source_min_v"], f"{role}.source_min_v"),
        source_max_v=_placeholder_number(table["source_max_v"], f"{role}.source_max_v"),
        ramp_step_v=_placeholder_positive(table["ramp_step_v"], f"{role}.ramp_step_v"),
        readback_tolerance_v=_placeholder_positive(
            table["readback_tolerance_v"], f"{role}.readback_tolerance_v"
        ),
        source_min_a=_placeholder_number(table["source_min_a"], f"{role}.source_min_a"),
        source_max_a=_placeholder_number(table["source_max_a"], f"{role}.source_max_a"),
        ramp_step_a=_placeholder_positive(table["ramp_step_a"], f"{role}.ramp_step_a"),
        readback_tolerance_a=_placeholder_positive(
            table["readback_tolerance_a"], f"{role}.readback_tolerance_a"
        ),
        settle_s=_placeholder_nonnegative(table["settle_s"], f"{role}.settle_s"),
        nplc=_placeholder_positive(smu["nplc"], f"{role}.smu.nplc"),
        source_auto_range=_boolean(
            smu["source_auto_range"], f"{role}.smu.source_auto_range"
        ),
        measure_auto_range=_boolean(
            smu["measure_auto_range"], f"{role}.smu.measure_auto_range"
        ),
        four_wire=_boolean(smu["four_wire"], f"{role}.smu.four_wire"),
        leakage_limit_a=_placeholder_positive(
            table["leakage_limit_a"], f"{role}.leakage_limit_a"
        ),
    )


def _parse_channel(table: Mapping[str, Any], role: str) -> ChannelPlan:
    _strict_keys(table, role, {"role", "fixed", "start", "stop", "step"})
    return ChannelPlan(
        role=_enum(ChannelRole, table["role"], f"{role}.role"),
        fixed=_number(table["fixed"], f"{role}.fixed"),
        start=_number(table["start"], f"{role}.start"),
        stop=_number(table["stop"], f"{role}.stop"),
        step=_positive(table["step"], f"{role}.step"),
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


def _placeholder_number(value: Any, name: str) -> float | None:
    return None if value == "CHANGE_ME" else _number(value, name)


def _placeholder_positive(value: Any, name: str) -> float | None:
    return None if value == "CHANGE_ME" else _positive(value, name)


def _placeholder_nonnegative(value: Any, name: str) -> float | None:
    return None if value == "CHANGE_ME" else _nonnegative(value, name)


def _placeholder_integer(value: Any, name: str) -> int | None:
    return None if value == "CHANGE_ME" else _integer(value, name, 1)


def _is_placeholder(value: str) -> bool:
    return value == "CHANGE_ME" or "CHANGE_ME" in value


def _enum(enum_type: type[StrEnum], value: Any, name: str) -> Any:
    raw = _string(value, name)
    try:
        return enum_type(raw)
    except ValueError as exc:
        choices = ", ".join(repr(item.value) for item in enum_type)
        raise ThreeSmuConfigError(f"{name} must be one of: {choices}") from exc
