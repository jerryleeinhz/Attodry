"""Strict offline photoresponse contract; loading never imports an instrument driver."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import tomllib

from .nkt_config import NktConfig, NktError, integer, keys, load_nkt_config, number


MODES = ("open_circuit_voltage", "dc_current_voltage", "voltage_bias_current")


@dataclass(frozen=True)
class PhotoresponseConfig:
    nkt: NktConfig
    mode: str
    detector: str
    samples_per_window: int
    repeats: int
    sample_interval_s: float
    settle_s: float
    max_abs_voltage_v: float
    max_abs_current_a: float
    bias_points: tuple[float | None, ...]

    @property
    def quantity(self):
        return "current_a" if self.mode == "voltage_bias_current" else "voltage_v"

    @property
    def excitation(self):
        return {"open_circuit_voltage": "none", "dc_current_voltage": "dc_current",
                "voltage_bias_current": "dc_voltage"}[self.mode]

    def validate(self):
        self.nkt.validate()
        if self.nkt.backend != "simulation":
            raise NktError("Photoresponse hardware acquisition is not implemented; simulation only")
        if not self.nkt.emit:
            raise NktError("Photoresponse requires emit=true for the simulated light window")
        if self.nkt.dwell_s != 0:
            raise NktError("Use photoresponse.settle_s; nkt_run.dwell_s must be zero")
        if self.mode not in MODES:
            raise NktError(f"Unsupported photoresponse mode: {self.mode}")
        if self.detector not in ("keithley2182a", "keithley2400"):
            raise NktError("Unsupported detector")
        if self.quantity == "current_a" and self.detector != "keithley2400":
            raise NktError("Current measurement requires keithley2400")
        integer(self.samples_per_window, "samples_per_window", 1, 10000)
        integer(self.repeats, "repeats", 1, 10000)
        number(self.sample_interval_s, "sample_interval_s", 0.001, 3600)
        number(self.settle_s, "settle_s", 0, 3600)
        # These are operator values, not vendor or sample ratings.
        number(self.max_abs_voltage_v, "max_abs_voltage_v", 1e-15, 1e6)
        number(self.max_abs_current_a, "max_abs_current_a", 1e-15, 1e6)
        if not self.bias_points or len(self.bias_points) > 10000:
            raise NktError("Provide 1 to 10000 bias points")
        if self.mode == "open_circuit_voltage":
            if self.bias_points != (None,):
                raise NktError("No excitation uses null, never a 0 A source setting")
        else:
            limit = self.max_abs_current_a if self.excitation == "dc_current" else self.max_abs_voltage_v
            for value in self.bias_points:
                number(value, "bias point", -limit, limit)
        count = len(self.nkt.points) * len(self.bias_points) * self.repeats
        if count > 10000:
            raise NktError("Photoresponse plan exceeds 10000 conditions")

    def snapshot(self):
        result = asdict(self)
        result["nkt"] = self.nkt.snapshot()
        result.update(excitation=self.excitation, quantity=self.quantity)
        return result


def load_photoresponse_config(path: str | Path) -> PhotoresponseConfig:
    nkt = load_nkt_config(path)
    with Path(path).open("rb") as stream:
        table = tomllib.load(stream).get("photoresponse", {})
    required = {"mode", "detector", "samples_per_window", "repeats", "sample_interval_s",
                "settle_s", "max_abs_voltage_v", "max_abs_current_a"}
    keys(table, "photoresponse", required, {"bias_current_points_a", "bias_voltage_points_v"})
    mode = table["mode"]
    if mode not in MODES:
        raise NktError(f"Unsupported photoresponse mode: {mode}")
    field = {"open_circuit_voltage": None, "dc_current_voltage": "bias_current_points_a",
             "voltage_bias_current": "bias_voltage_points_v"}[mode]
    for candidate in ("bias_current_points_a", "bias_voltage_points_v"):
        if candidate in table and candidate != field:
            raise NktError(f"{candidate} is not allowed in {mode}")
    values = table.get(field) if field else [None]
    if not isinstance(values, list):
        raise NktError(f"{field} must be an explicit array")
    config = PhotoresponseConfig(nkt=nkt, bias_points=tuple(values),
                                 **{key: table[key] for key in required})
    config.validate()
    return config
