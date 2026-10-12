"""Reuse the attoDRY driver, vector protection and stability contracts."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from copy import deepcopy
from pathlib import Path
import math

from .config import _parse_cryostat, _parse_magnet, _parse_stability, RunMode
from .models import VectorField
from .nkt_config import NktError, keys, number
from .safety import validate_vector_field


@dataclass(frozen=True)
class EnvironmentConfig:
    cryostat: object
    magnet: object
    temperature_stability: object
    points: tuple[dict, ...]
    max_field_step_t: float
    max_temperature_step_k: float
    max_temperature_overshoot_k: float
    normal_end_field_policy: str

    def validate(self):
        if self.normal_end_field_policy not in ("hold", "zero"):
            raise NktError("normal_end_field_policy must be hold or zero")
        if self.magnet.limits.experiment_vector_max_t != 3:
            raise NktError("Photoresponse retains the 3 T vector invariant")
        number(self.max_field_step_t, "max_field_step_t", 1e-6, .05)
        number(self.max_temperature_step_k, "max_temperature_step_k", .001, 300)
        number(self.max_temperature_overshoot_k, "max_temperature_overshoot_k", .001, 10)
        if not self.points or len(self.points)>10000: raise NktError("Provide bounded explicit environment points")
        for point in self.points:
            keys(point, "environment point", {"temperature_k", "bx_t", "bz_t"})
            number(point["temperature_k"], "temperature_k", self.cryostat.temperature_min_k, self.cryostat.temperature_max_k)
            validate_vector_field(VectorField(point["bx_t"], point["bz_t"]), self.magnet.limits)
        for first, second in zip(self.points, self.points[1:]):
            if abs(second["temperature_k"]-first["temperature_k"]) > self.max_temperature_step_k:
                raise NktError("Planned temperature step exceeds approved bound")

    def snapshot(self):
        def convert(value):
            if isinstance(value, Path): return str(value)
            if isinstance(value, dict): return {k:convert(v) for k,v in value.items()}
            if isinstance(value, (list,tuple)): return [convert(v) for v in value]
            return value
        return convert(asdict(self))


def load_environment(doc, hardware):
    t = doc["photoresponse_environment"]
    keys(t, "photoresponse_environment", {"points", "max_field_step_t", "max_temperature_step_k",
         "max_temperature_overshoot_k", "normal_end_field_policy"})
    if not isinstance(t["points"], list): raise NktError("Environment points must be an array")
    c = EnvironmentConfig(
        cryostat=_parse_cryostat(doc.get("cryostat", {}), RunMode.HARDWARE if hardware else RunMode.SIMULATION),
        magnet=_parse_magnet(doc.get("magnet", {})),
        temperature_stability=_parse_stability(doc.get("temperature_stability", {}), "temperature_stability", value_prefix=""),
        **{**t, "points": tuple(t["points"])})
    c.validate()
    return c


class EnvironmentController:
    def __init__(self, config, driver, *, on_event=None):
        self.config, self.driver, self.on_event = config, driver, on_event
        self.last_confirmed_state = None
        self.state_current = False
        self.target = None
        self.temperature_reference = None
        self.owned = False

    def snapshot(self):
        self.state_current = False
        state = self.driver.read_state()
        record = asdict(state)
        self.last_confirmed_state = record
        self.state_current = True
        if self.on_event: self.on_event({"phase":"environment_readback", "state":record})
        if state.error_code: raise NktError("attoDRY reports an error")
        number(state.sample_temperature_k, "sample temperature", self.config.cryostat.temperature_min_k,
               self.config.cryostat.temperature_max_k)
        validate_vector_field(state.field, self.config.magnet.limits)
        validate_vector_field(state.field_setpoint, self.config.magnet.limits)
        return state

    def begin(self):
        self.driver.connect()
        self.snapshot()
        self.owned = True

    def prepare(self, point):
        if not self.owned or point not in self.config.points:
            raise NktError("Environment point/session invalid")
        state = self.snapshot()
        if abs(point["temperature_k"]-state.user_temperature_k) > self.config.max_temperature_step_k:
            raise NktError("Environment temperature step exceeds approved bound")
        # Preserve the commissioned station sequence: enable feedback first.
        self.driver.ensure_temperature_control(True)
        self.driver.set_temperature(point["temperature_k"])
        self.driver.ensure_field_control(True)
        field = VectorField(point["bx_t"], point["bz_t"])
        self.driver.set_vector_field(field, max_step_t=self.config.max_field_step_t)
        self.driver.wait_for_field(field)
        settled = self.driver.wait_for_temperature(point["temperature_k"],
            max_overshoot_k=self.config.max_temperature_overshoot_k,
            minimum_response_k=self.config.temperature_stability.min_response_k,
            response_reference_k=state.sample_temperature_k)
        self.target = deepcopy(point)
        self.temperature_reference = settled.sample_temperature_k
        return self.verify()

    def verify(self):
        if self.target is None: raise NktError("Environment point has not settled")
        state = self.snapshot()
        if not state.field_control_enabled or not state.temperature_control_enabled:
            raise NktError("Environment feedback control is disabled")
        target = self.target
        tolerance = self.config.magnet.stability.criteria.tolerance
        if math.hypot(state.field.bx_t-target["bx_t"], state.field.bz_t-target["bz_t"]) > tolerance:
            raise NktError("Field left the accepted measurement window")
        if math.hypot(state.field_setpoint.bx_t-target["bx_t"], state.field_setpoint.bz_t-target["bz_t"]) > 1e-6:
            raise NktError("Field setpoint changed externally")
        if abs(state.user_temperature_k-target["temperature_k"]) > 1e-4:
            raise NktError("Temperature setpoint changed externally")
        criteria = self.config.temperature_stability.criteria
        allowed = criteria.tolerance if criteria.tolerance is not None else criteria.stable_range
        if abs(state.sample_temperature_k-self.temperature_reference) > allowed:
            raise NktError("Temperature left the accepted measurement window")
        if state.sample_temperature_k > target["temperature_k"] + self.config.max_temperature_overshoot_k:
            raise NktError("Temperature exceeded approved overshoot")
        result = asdict(state)
        result["measurement_temperature_k"] = state.sample_temperature_k
        return result

    def finish(self, *, failed):
        result = {"errors": [], "field_zero_confirmed": False, "final_state": None}
        try:
            if self.owned:
                if failed or self.config.normal_end_field_policy == "zero":
                    self.state_current = False
                    self.driver.request_zero_field()
                    self.driver.wait_for_field(VectorField(0.,0.))
                    state = self.snapshot()
                    if state.field.magnitude_t > self.config.magnet.stability.criteria.tolerance:
                        raise NktError("Zero field not confirmed")
                    result["field_zero_confirmed"] = True
                else: self.snapshot()
                result["final_state"] = self.last_confirmed_state
        except (Exception, KeyboardInterrupt) as exc:
            result["errors"].append(str(exc))
        finally:
            result["last_confirmed_state"] = self.last_confirmed_state
            result["state_current"] = self.state_current
            try: self.driver.close()
            except (Exception, KeyboardInterrupt) as exc: result["errors"].append("close: "+str(exc))
        return result


def open_environment(config, *, authorize_connection=False, authorize_writes=False, on_event=None):
    if config.cryostat.backend == "simulation":
        from .simulation import SimulationCryostat
        class FakeDriver(SimulationCryostat):
            def connect(self): pass
            def set_vector_field(self, target, *, max_step_t): super().set_vector_field(target)
            def wait_for_field(self, target): return super().wait_for_field(10)
            def wait_for_temperature(self, target, **kwargs): return super().wait_for_temperature(10)
        driver = FakeDriver(temperature_min_k=config.cryostat.temperature_min_k,
            temperature_max_k=config.cryostat.temperature_max_k, limits=config.magnet.limits, event_log=[])
        return EnvironmentController(config, driver, on_event=on_event)
    from .attodry import AttoDryDriver, load_attodry_dll
    c = config.cryostat
    if not authorize_connection or not authorize_writes:
        raise NktError("Environment control requires explicit connection and writes authorization")
    driver = AttoDryDriver(dll=load_attodry_dll(c.dll_path), com_port=c.com_port,
        device_type=c.device_type, connection_timeout_s=c.connection_timeout_s,
        temperature_min_k=c.temperature_min_k, temperature_max_k=c.temperature_max_k,
        limits=config.magnet.limits, field_stability=config.magnet.stability,
        temperature_stability=config.temperature_stability, connection_authorized=True, writes_authorized=True)
    return EnvironmentController(config, driver, on_event=on_event)
