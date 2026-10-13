"""Narrow ordered combination plan for DC photoresponse.

Inherits the existing combination_scan semantics: ordered Cartesian axes,
every selected module read at each leaf, and whole-window acceptance. Optical
rows and environment rows remain paired settings within their own modules.
No legacy station/lock-in/field-driver dependencies are imported.
"""
from itertools import product

from .nkt_config import NktError


AXES = ("environment", "optical", "target_power_w", "bias", "repeat")


class PhotoresponseCombinationPlan:
    def __init__(self, config):
        self.values = {
            "environment": config.environment.points if config.environment else (None,),
            "optical": config.nkt.points,
            "target_power_w": config.power.targets if config.power else (None,),
            "bias": config.bias_points, "repeat": tuple(range(config.repeats))}
        order = config.combination_order
        if order is None:
            order = AXES  # Preserve the previous open-loop traversal by default.
        if not isinstance(order, tuple) or not order or any(type(v) is not str or v not in AXES for v in order):
            raise NktError("Combination order must contain known axis names")
        if len(order) != len(set(order)):
            raise NktError("Combination axes must be unique")
        missing = [axis for axis in AXES if axis not in order]
        if any(len(self.values[axis]) > 1 for axis in missing):
            raise NktError("Every multi-point axis must appear in combination order")
        self.order = order + tuple(missing)
        if config.environment:
            outer = self.order[:self.order.index("environment")]
            if any(len(self.values[axis]) > 1 for axis in outer):
                first, last = config.environment.points[0], config.environment.points[-1]
                if abs(last["temperature_k"] - first["temperature_k"]) > config.environment.max_temperature_step_k:
                    raise NktError("Combination environment wrap-around exceeds approved temperature step")
        self.modules = ["cld1010lp" if hasattr(config.nkt, "diode_model") else "nkt", config.detector]
        if config.excitation == "dc_current":
            self.modules.append("keithley6221_source")
        elif config.hardware and config.hardware.detector_transport == "via_6221":
            self.modules.append("keithley6221_relay_host")
        if config.power:
            self.modules.append("pm100d")
        if config.environment:
            self.modules.append("environment")

    def conditions(self):
        for entries in product(*(enumerate(self.values[axis]) for axis in self.order)):
            yield {axis: {"index": index, "value": value}
                   for axis, (index, value) in zip(self.order, entries)}

    def snapshot(self):
        return {"order": list(self.order), "selected_modules": self.modules,
                "axis_lengths": {axis: len(values) for axis, values in self.values.items()},
                "sampling": "sequential fresh reads bracketed by optical/electrical/environment guards",
                "acceptance": "complete dark/light/dark and successful cleanup required"}
