"""Pure modular Cartesian plan, adapted from integration CombinationPlan.

No device, VISA or DLL module is imported. Module order describes traversal;
runtime adapters remain responsible for safe apply/qualification/enable order.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
import itertools
import math

from .models import VectorField
from .nkt_config import NktError, integer
from .safety import MagnetLimits, validate_vector_field


MODULE_KEYS = {
    "temperature": {"temperature_k"},
    "magnetic": {"field_x_t", "field_z_t"},
    "optics": {"optical_wavelength_nm", "optical_bandwidth_nm",
               "optical_source_level_pct", "optical_current_a",
               "optical_target_power_w", "optical_nd_pct", "optical_pulse_picker_ratio"},
    "current_source": {"bias_current_a"},
    "smu": {"smu_bias_v", "smu_bias_a", "gate_top_v", "gate_top_a",
            "gate_bottom_v", "gate_bottom_a"},
    "nanovoltmeter": set(),
}


@dataclass(frozen=True)
class AxisPoint:
    values: dict[str, float]
    segment: str = "main"
    direction: str = "ordered"
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ScanAxis:
    module: str
    points: tuple[AxisPoint, ...]


@dataclass(frozen=True)
class CombinationPlan:
    axes: tuple[ScanAxis, ...]
    samples_per_condition: int = 1
    repeats: int = 1
    run_name: str = "combination"
    note: str = ""
    magnet_limits: MagnetLimits = field(default_factory=MagnetLimits)
    temperature_bounds_k: tuple[float, float] | None = None
    max_temperature_step_k: float | None = None

    @property
    def order(self):
        return tuple(axis.module for axis in self.axes)

    @property
    def condition_count(self):
        return self.repeats * math.prod(len(axis.points) for axis in self.axes)

    def validate(self):
        if not self.axes or len(set(self.order)) != len(self.axes):
            raise NktError("Combination requires distinct selected module axes")
        if not isinstance(self.magnet_limits, MagnetLimits) or self.magnet_limits.experiment_vector_max_t > 3:
            raise NktError("Combination must preserve the 3 T vector invariant")
        integer(self.samples_per_condition, "samples_per_condition", 1, 10000)
        integer(self.repeats, "repeats", 1, 10000)
        if type(self.run_name) is not str or not self.run_name.strip() or type(self.note) is not str:
            raise NktError("Combination run_name must be nonempty and note must be a string")
        if self.condition_count > 10000 or self.condition_count * self.samples_per_condition > 100000:
            raise NktError("Combination exceeds 10000 conditions or 100000 formal samples")
        seen_fast = False
        for axis in self.axes:
            if axis.module not in MODULE_KEYS or not axis.points:
                raise NktError("Unknown or empty Combination module")
            if axis.module in {"temperature", "magnetic"}:
                if seen_fast:
                    raise NktError("Temperature and magnetic modules must precede all fast scan modules")
            else:
                seen_fast = True
            if axis.module == "nanovoltmeter" and len(axis.points) != 1:
                raise NktError("Nanovoltmeter is a singleton measurement module")
            coordinates = set(axis.points[0].values)
            if not coordinates <= MODULE_KEYS[axis.module] or (not coordinates and axis.module != "nanovoltmeter"):
                raise NktError(f"Unsupported {axis.module} coordinate keys")
            if axis.module in {"temperature", "magnetic", "current_source"} and coordinates != MODULE_KEYS[axis.module]:
                raise NktError(f"Specify every {axis.module} coordinate")
            for point in axis.points:
                if set(point.values) != coordinates:
                    raise NktError("An axis must keep the same active coordinate keys")
                if type(point.metadata) is not dict or type(point.segment) is not str or type(point.direction) is not str:
                    raise NktError("Invalid axis metadata or labels")
                if any(type(v) not in (int, float) or not math.isfinite(v) for v in point.values.values()):
                    raise NktError("Coordinates must be finite real numbers")
                if axis.module == "magnetic":
                    validate_vector_field(VectorField(point.values["field_x_t"], point.values["field_z_t"]), self.magnet_limits)
                if axis.module == "temperature":
                    value = point.values["temperature_k"]
                    if value <= 0 or (self.temperature_bounds_k and not self.temperature_bounds_k[0] <= value <= self.temperature_bounds_k[1]):
                        raise NktError("Temperature point is outside configured bounds")
                if axis.module == "optics":
                    self._validate_optics(point)
                if axis.module == "smu":
                    for role in ("smu_bias", "gate_top", "gate_bottom"):
                        if {role+"_v", role+"_a"} <= coordinates:
                            raise NktError("An SMU role cannot source voltage and current together")
        self._validate_temperature_transitions()

    @staticmethod
    def _validate_optics(point):
        values = point.values
        for key in ("optical_wavelength_nm", "optical_bandwidth_nm"):
            if key in values and values[key] <= 0:
                raise NktError(f"{key} must be positive")
        for key in ("optical_source_level_pct", "optical_nd_pct"):
            if key in values and not 0 <= values[key] <= 100:
                raise NktError(f"{key} must be in 0..100 percent")
        if "optical_current_a" in values and values["optical_current_a"] < 0:
            raise NktError("Optical current cannot be negative")
        if "optical_target_power_w" in values:
            power = values["optical_target_power_w"]
            if power < 0 or (power == 0 and point.metadata.get("optical_state") != "dark"):
                raise NktError("Only an explicit dark point may have requested zero target power")

    def _validate_temperature_transitions(self):
        axis = next((axis for axis in self.axes if axis.module == "temperature"), None)
        if axis is None or self.max_temperature_step_k is None:
            return
        if type(self.max_temperature_step_k) not in (int, float) or not math.isfinite(self.max_temperature_step_k) or self.max_temperature_step_k <= 0:
            raise NktError("max_temperature_step_k must be positive")
        values = [point.values["temperature_k"] for point in axis.points]
        transitions = list(zip(values, values[1:]))
        depth = self.order.index("temperature")
        if self.repeats > 1 or any(len(a.points) > 1 for a in self.axes[:depth]):
            transitions.append((values[-1], values[0]))
        if any(abs(second-first) > self.max_temperature_step_k for first, second in transitions):
            raise NktError("Combination temperature transition/wrap-around exceeds approved step")

    def conditions(self):
        self.validate()
        conditions = []
        for repeat in range(self.repeats):
            for indices in itertools.product(*(range(len(axis.points)) for axis in self.axes)):
                requested, axes = {}, {}
                for axis, index in zip(self.axes, indices):
                    point = axis.points[index]
                    requested.update(point.values)
                    axes[axis.module] = {"index": index, "segment": point.segment,
                                         "direction": point.direction}
                    if point.metadata:
                        axes[axis.module]["grid"] = deepcopy(point.metadata)
                sequence = len(conditions)
                conditions.append({"condition_id": f"condition-{sequence:06d}",
                    "sequence_index": sequence, "repeat_index": repeat,
                    "loop_order": list(self.order), "axes": axes, "requested": requested})
        return conditions

    def axis_visit(self, condition, module):
        """A revisit/repeat has a fresh scope even when numeric targets repeat."""
        if module not in self.order:
            raise NktError(f"Module {module!r} is not selected")
        return (("repeat", condition["repeat_index"]),) + tuple(
            (name, condition["axes"][name]["index"])
            for name in self.order[:self.order.index(module)+1])

    def snapshot(self):
        self.validate()
        return {"record_contract": "modular-combination-v1", "order": list(self.order),
                "selected_modules": list(self.order), "axes": asdict(self)["axes"],
                "axis_lengths": {a.module: len(a.points) for a in self.axes},
                "condition_count": self.condition_count,
                "formal_sample_count": self.condition_count*self.samples_per_condition,
                "samples_per_condition": self.samples_per_condition,
                "repeats": self.repeats, "run_name": self.run_name, "note": self.note,
                "magnet_limits": asdict(self.magnet_limits),
                "temperature_bounds_k": self.temperature_bounds_k,
                "max_temperature_step_k": self.max_temperature_step_k}
