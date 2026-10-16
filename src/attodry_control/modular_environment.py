"""Independent environmental axes sharing the commissioned attoDRY driver.

Full dwell belongs to an axis visit; sample guards only read actual state.
Unselected controls are never enabled or assigned a fabricated target.
"""
from dataclasses import asdict, dataclass
from copy import deepcopy
from enum import Enum
import math
from pathlib import Path

from .config import RunMode, MagnetConfig, _parse_cryostat, _parse_magnet, _parse_stability
from .models import VectorField
from .nkt_config import NktError, keys, number
from .photoresponse_environment import open_environment
from .safety import MagnetLimits, validate_vector_field


@dataclass(frozen=True)
class EnvironmentAxesConfig:
    cryostat: object
    magnet: object
    temperature_stability: object | None
    modules: tuple
    max_field_step_t: float | None
    max_temperature_step_k: float | None
    max_temperature_overshoot_k: float | None
    normal_end_field_policy: str

    def snapshot(self):
        def serializable(value):
            if isinstance(value, Path):
                return str(value)
            if isinstance(value, Enum):
                return value.value
            if isinstance(value, dict):
                return {k: serializable(v) for k, v in value.items()}
            if isinstance(value, (tuple, list)):
                return [serializable(v) for v in value]
            return value
        return serializable(asdict(self))


def load_environment_axes(document, modules, *, hardware):
    selected = tuple(m for m in modules if m in ("temperature", "magnetic"))
    if not selected:
        return None
    raw = deepcopy(document.get("environment", document.get("photoresponse_environment", {})))
    raw.pop("points", None)  # Legacy paired coordinates are never expanded here.
    temperature = "temperature" in selected
    magnetic = "magnetic" in selected
    field_keys = {"max_field_step_t", "normal_end_field_policy"}
    temperature_keys = {"max_temperature_step_k", "max_temperature_overshoot_k"}
    required = (field_keys if magnetic else set()) | (temperature_keys if temperature else set())
    keys(raw, "environment", required, (field_keys | temperature_keys)-required)
    field_step = number(raw["max_field_step_t"], "max_field_step_t", 1e-6, .05) if magnetic else None
    temperature_step = number(raw["max_temperature_step_k"], "max_temperature_step_k", .001, 300) if temperature else None
    overshoot = number(raw["max_temperature_overshoot_k"], "max_temperature_overshoot_k", .001, 10) if temperature else None
    end_policy = raw["normal_end_field_policy"] if magnetic else "hold"
    if end_policy not in ("hold", "zero"):
        raise NktError("environment.normal_end_field_policy must be hold or zero")
    c = _parse_cryostat(document.get("cryostat", {}), RunMode.HARDWARE if hardware else RunMode.SIMULATION)
    if magnetic:
        m = _parse_magnet(document.get("magnet", {}))
    else:
        # Shared field readbacks still obey station limits, even without a
        # selected magnetic controller. Its unused dwell policy is not invented.
        table = document.get("magnet", {})
        bounds = {}
        for key in ("hardware_x_max_t", "hardware_z_max_t", "experiment_vector_max_t"):
            bounds[key] = number(table.get(key), "magnet."+key, 1e-15, 1e6)
        m = MagnetConfig(MagnetLimits(**bounds), None)
    if m.limits.experiment_vector_max_t > 3:
        raise NktError("Modular environment retains the 3 T vector limit")
    if (c.backend != "simulation") != hardware:
        raise NktError("Environment backend differs from the combination backend")
    stability = (_parse_stability(document.get("temperature_stability", {}), "temperature_stability", value_prefix="")
                 if temperature else None)
    return EnvironmentAxesConfig(c, m, stability, selected, field_step, temperature_step, overshoot, end_policy)


class EnvironmentAxes:
    def __init__(self, config, *, on_event=None, driver=None, authorize_hardware=False):
        self.config, self.on_event = config, on_event
        self.driver = driver or open_environment(config, authorize_connection=authorize_hardware,
            authorize_writes=authorize_hardware).driver
        self.targets, self.qualification = {}, {}
        self.last_confirmed_state, self.state_current, self.owned = None, False, False
        self._finished = None

    def _event(self, phase, **values):
        if self.on_event:
            self.on_event({"phase": phase, **deepcopy(values)})

    def snapshot(self):
        self.state_current = False
        state = self.driver.read_state()
        self.last_confirmed_state = asdict(state)
        self._event("environment_readback", state=self.last_confirmed_state)
        validate_vector_field(state.field, self.config.magnet.limits)
        validate_vector_field(state.field_setpoint, self.config.magnet.limits)
        if state.error_code:
            raise NktError("attoDRY error during modular scan")
        number(state.sample_temperature_k, "sample temperature", self.config.cryostat.temperature_min_k,
               self.config.cryostat.temperature_max_k)
        self.state_current = True
        return state

    def begin(self):
        if self.owned or self._finished is not None:
            raise NktError("Environmental sessions are single-use")
        self.driver.connect()
        self.owned = True  # Also owns a failed first read; cleanup cannot infer zero.
        return self.snapshot()

    def prepare_axis(self, module, point, visit):
        if module not in self.config.modules or not self.owned or self._finished is not None:
            raise NktError("Unselected/unowned environmental axis")
        existing = self.qualification.get(module)
        if existing is not None and existing["visit"] == visit:
            if existing["requested"] != point.values:
                raise NktError("An environmental axis visit cannot change its requested coordinate")
            return self.verify()
        state = self.snapshot()
        self.qualification.pop(module, None)
        if module == "temperature":
            target = point.values["temperature_k"]
            number(target, "temperature target", self.config.cryostat.temperature_min_k,
                   self.config.cryostat.temperature_max_k)
            if abs(target - state.user_temperature_k) > self.config.max_temperature_step_k:
                raise NktError("Temperature entry exceeds approved step")
            self.driver.ensure_temperature_control(True)
            self.driver.set_temperature(target)
            settled = self.driver.wait_for_temperature(target,
                max_overshoot_k=self.config.max_temperature_overshoot_k,
                minimum_response_k=self.config.temperature_stability.min_response_k,
                response_reference_k=state.sample_temperature_k)
            self.targets[module] = {"target": target, "reference": settled.sample_temperature_k}
        else:
            target = VectorField(point.values["field_x_t"], point.values["field_z_t"])
            validate_vector_field(target, self.config.magnet.limits)
            self.driver.ensure_field_control(True)
            self.driver.set_vector_field(target, max_step_t=self.config.max_field_step_t)
            self.driver.wait_for_field(target)
            self.targets[module] = target
        self.qualification[module] = {"visit": visit, "requested": deepcopy(point.values),
                                      "policy": "full_dwell_once_per_axis_entry"}
        self._event("axis_qualification", module=module, visit=visit, qualification=self.qualification[module])
        return self.verify()

    def verify(self):
        state = self.snapshot()
        quality = {}
        if "temperature" in self.targets:
            target = self.targets["temperature"]
            criteria = self.config.temperature_stability.criteria
            allowed = criteria.tolerance if criteria.tolerance is not None else criteria.stable_range
            quality["temperature_within_window"] = abs(state.sample_temperature_k-target["reference"]) <= allowed
            if (not state.temperature_control_enabled or abs(state.user_temperature_k-target["target"]) > 1e-4
                    or not quality["temperature_within_window"]
                    or state.sample_temperature_k > target["target"] + self.config.max_temperature_overshoot_k):
                raise NktError("Temperature left its qualified measurement state")
        if "magnetic" in self.targets:
            target = self.targets["magnetic"]
            tolerance = self.config.magnet.stability.criteria.tolerance
            quality["field_within_window"] = math.hypot(state.field.bx_t-target.bx_t, state.field.bz_t-target.bz_t) <= tolerance
            if (not state.field_control_enabled or not quality["field_within_window"]
                    or math.hypot(state.field_setpoint.bx_t-target.bx_t, state.field_setpoint.bz_t-target.bz_t) > 1e-6):
                raise NktError("Field left its qualified measurement state")
        result = {**asdict(state), "measurement_temperature_k": state.sample_temperature_k,
                  "quality": quality, "axis_qualification": deepcopy(self.qualification)}
        self._event("environment_quality", state=result)
        return result

    def finish(self, *, failed):
        if self._finished is not None:
            return self._finished
        result = {"errors": [], "field_zero_confirmed": False, "final_state": None,
                  "selected_controls": list(self.config.modules),
                  "manual_verification_required": False}
        try:
            if self.owned:
                if not self.state_current:
                    result["manual_verification_required"] = True
                    raise NktError("Environmental state is unknown; preserve last confirmed readback and require manual verification")
                if "magnetic" in self.config.modules and (failed or self.config.normal_end_field_policy == "zero"):
                    self.state_current = False
                    self.driver.request_zero_field()
                    self.driver.wait_for_field(VectorField(0., 0.))
                    state = self.snapshot()
                    if state.field.magnitude_t > self.config.magnet.stability.criteria.tolerance:
                        raise NktError("Environmental cleanup zero field not confirmed")
                    result["field_zero_confirmed"] = True
                result["final_state"] = asdict(self.snapshot())
        except (Exception, KeyboardInterrupt) as exc:
            result["errors"].append(f"{type(exc).__name__}: {exc}")
        finally:
            if not self.state_current and self.owned:
                result["manual_verification_required"] = True
            result.update(last_confirmed_state=deepcopy(self.last_confirmed_state), state_current=self.state_current)
            try:
                self.driver.close()
            except (Exception, KeyboardInterrupt) as exc:
                result["errors"].append("close: " + str(exc))
            self._finished = result
        return result
