"""Strict photoresponse contract; loading never opens instruments."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import tomllib

from .nkt_config import NktConfig, NktError, integer, keys, load_nkt_config, number


MODES = ("open_circuit_voltage", "dc_current_voltage", "voltage_bias_current",
         "smu_current_voltage", "smu_voltage_voltage")


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
    max_abs_current_a: float | None
    bias_points: tuple[float | None, ...]
    hardware: object | None = None
    environment: object | None = None
    database_path: Path | None = None
    power: object | None = None
    combination_order: tuple[str, ...] | None = None
    smu: object | None = None

    @property
    def quantity(self):
        return "current_a" if self.mode == "voltage_bias_current" else "voltage_v"

    @property
    def excitation(self):
        return {"open_circuit_voltage": "none", "dc_current_voltage": "dc_current",
                "voltage_bias_current": "dc_voltage", "smu_current_voltage": "smu_current",
                "smu_voltage_voltage": "smu_voltage"}[self.mode]

    def validate(self):
        self.nkt.validate()
        if self.nkt.backend != "simulation" and self.hardware is None:
            raise NktError("Hardware acquisition requires explicit electrical hardware configuration")
        if self.nkt.backend == "simulation" and self.hardware is not None:
            raise NktError("Simulation cannot include an active electrical hardware configuration")
        if not self.nkt.emit:
            raise NktError("Photoresponse requires emit=true for the light window")
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
        if self.max_abs_current_a is not None or not (
                self.excitation == "none" and self.detector == "keithley2182a"):
            number(self.max_abs_current_a, "max_abs_current_a", 1e-15, 1e6)
        if not self.bias_points or len(self.bias_points) > 10000:
            raise NktError("Provide 1 to 10000 bias points")
        if self.mode in ("open_circuit_voltage", "smu_current_voltage", "smu_voltage_voltage"):
            if self.bias_points != (None,):
                raise NktError("This mode has no independent electrical bias axis; SMU excitation uses only the SMU plan")
        else:
            limit = self.max_abs_current_a if self.excitation == "dc_current" else self.max_abs_voltage_v
            for value in self.bias_points:
                number(value, "bias point", -limit, limit)
        if self.power:
            self.power.validate(self.nkt)
            if self.hardware:
                resources = list(self.hardware.addresses().values())
                if hasattr(self.nkt, "address"):
                    resources.append(self.nkt.address)
                if self.power.meter.resource.upper() in [r.upper() for r in resources]:
                    raise NktError("PM100D cannot share a VISA address with a selected device")
        from .photoresponse_smu import validate_selected_resources
        validate_selected_resources(self)
        if self.smu:
            self.smu.validate(self)
        elif self.mode in ("smu_current_voltage", "smu_voltage_voltage"):
            raise NktError("SMU excitation requires an enabled photoresponse_smu plan")
        from .photoresponse_combination import PhotoresponseCombinationPlan
        PhotoresponseCombinationPlan(self)
        count = len(self.nkt.points) * len(self.bias_points) * self.repeats * (len(self.environment.points) if self.environment else 1)
        count *= len(self.power.targets) if self.power else 1
        count *= len(self.smu.points) if self.smu else 1
        if count > 10000:
            raise NktError("Photoresponse plan exceeds 10000 conditions")
        if self.hardware is not None:
            self.hardware.validate(self)
        if self.environment is not None:
            self.environment.validate()

    def snapshot(self):
        result = asdict(self)
        result["nkt"] = self.nkt.snapshot()
        result["environment"] = self.environment.snapshot() if self.environment else None
        result["smu"] = self.smu.snapshot() if self.smu else None
        result["database_path"] = str(self.database_path) if self.database_path else None
        result.update(excitation=self.excitation, quantity=self.quantity)
        from .photoresponse_combination import PhotoresponseCombinationPlan
        result["combination"] = PhotoresponseCombinationPlan(self).snapshot()
        return result


def load_photoresponse_config(path: str | Path) -> PhotoresponseConfig:
    with Path(path).open("rb") as stream:
        doc = tomllib.load(stream)
        table = doc.get("photoresponse", {})
    required = {"mode", "detector", "samples_per_window", "repeats", "sample_interval_s",
                "settle_s", "max_abs_voltage_v"}
    keys(table, "photoresponse", required, {"bias_current_points_a", "bias_voltage_points_v", "light_source", "max_abs_current_a"})
    light_source = table.get("light_source", "nkt")
    if light_source == "nkt":
        nkt = load_nkt_config(path)
    elif light_source == "cld1010lp":
        from .photoresponse_cld import load_cld_config
        from .nkt_config import NKT_TABLES, OTHER_TABLES
        keys(doc, "top level", {"photoresponse", "cld1010"}, NKT_TABLES | OTHER_TABLES)
        nkt = load_cld_config(doc, path)
    else:
        raise NktError("light_source must be nkt or cld1010lp")
    mode = table["mode"]
    if mode not in MODES:
        raise NktError(f"Unsupported photoresponse mode: {mode}")
    field = {"open_circuit_voltage": None, "dc_current_voltage": "bias_current_points_a",
             "voltage_bias_current": "bias_voltage_points_v", "smu_current_voltage": None,
             "smu_voltage_voltage": None}[mode]
    for candidate in ("bias_current_points_a", "bias_voltage_points_v"):
        if candidate in table and candidate != field:
            raise NktError(f"{candidate} is not allowed in {mode}")
    values = table.get(field) if field else [None]
    if not isinstance(values, list):
        raise NktError(f"{field} must be an explicit array")
    config = PhotoresponseConfig(nkt=nkt, bias_points=tuple(values), max_abs_current_a=table.get("max_abs_current_a"),
                                 **{key: table[key] for key in required})
    from dataclasses import replace
    from .power_config import load_photoresponse_power
    config = replace(config, power=load_photoresponse_power(doc))
    from .photoresponse_smu import load_photoresponse_smu
    config = replace(config, smu=load_photoresponse_smu(doc, path))
    if "photoresponse_combination" in doc:
        combination = doc["photoresponse_combination"]
        keys(combination, "photoresponse_combination", {"order"})
        if type(combination["order"]) is not list:
            raise NktError("Combination order must be an explicit array")
        config = replace(config, combination_order=tuple(combination["order"]))
    if "photoresponse_storage" in doc:
        storage = doc["photoresponse_storage"]
        keys(storage, "photoresponse_storage", {"database_path"})
        from .nkt_config import text
        db = Path(text(storage["database_path"], "database_path"))
        config = replace(config, database_path=db if db.is_absolute() else Path(path).resolve().parent / db)
    if "photoresponse_environment" in doc:
        from .photoresponse_environment import load_environment
        config = replace(config, environment=load_environment(doc, nkt.backend != "simulation"))
    if nkt.backend != "simulation":
        from .photoresponse_hardware import load_electrical_hardware
        config = replace(config, hardware=load_electrical_hardware(doc, config))
    config.validate()
    return config
