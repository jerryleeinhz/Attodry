"""Dark/light/dark coordinator using injected, explicitly authorized controllers."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import math
from statistics import fmean
import time

from .nkt_config import NktError, number
from .nkt_control import NktController, utc_now
from .photoresponse_config import PhotoresponseConfig


WINDOWS = ("dark_before", "light", "dark_after")


def differential(windows: list[dict], quantity: str) -> dict:
    """Interpolate a linear dark baseline using actual acquisition midpoints."""
    if [w["phase"] for w in windows] != list(WINDOWS):
        raise NktError("A differential requires all three ordered windows")
    if quantity not in ("voltage_v", "current_a"):
        raise NktError("Unknown electrical quantity")
    means, times = [], []
    previous_end = -math.inf
    for window in windows:
        if not window["samples"]:
            raise NktError("Empty measurement window")
        values, midpoints = [], []
        for sample in window["samples"]:
            if not sample["valid"]:
                raise NktError("Invalid formal sample")
            start = number(sample["started_s"], "started_s", 0, math.inf)
            end = number(sample["finished_s"], "finished_s", start, math.inf)
            if start <= previous_end:
                raise NktError("Samples must have strictly ordered monotonic times")
            previous_end = end
            values.append(number(sample[quantity], quantity, -math.inf, math.inf))
            midpoints.append((start + end) / 2)
        means.append(fmean(values))
        times.append(fmean(midpoints))
    if not times[0] < times[1] < times[2]:
        raise NktError("Dark/light/dark acquisition times must be strictly increasing")
    weight = (times[1] - times[0]) / (times[2] - times[0])
    dark = means[0] + (means[2] - means[0]) * weight
    return {"method": "linear_dark_interpolation_v1", "quantity": quantity,
            "window_means": dict(zip(WINDOWS, means)),
            "window_mean_times_s": dict(zip(WINDOWS, times)), "interpolation_weight": weight,
            "dark_at_light": dark, "dark_drift": means[2] - means[0],
            "delta_" + quantity: means[1] - dark}


class SimulatedElectrical:
    """Synthetic electrical response, explicitly unrelated to calibrated sample power.

    none has no current-source object/commands; dc_current at zero is still an
    enabled simulated source. Instrument settings are never claimed as measured I.
    """
    is_hardware = False

    def __init__(self, config: PhotoresponseConfig, *, clock=time.perf_counter):
        config.validate()
        self.config, self.clock = config, clock
        self._epoch = clock()
        self.commands = []
        self.state = {"detector": "SIMULATED " + config.detector,
                      "excitation": config.excitation, "source": None,
                      "compliance_tripped": False, "error": None}
        if config.excitation != "none":
            self.state["source"] = {"model": "SIMULATED " + (
                "6221" if config.excitation == "dc_current" else "2400"),
                "output_enabled": False, "setpoint": 0.0}
        self.closed = False

    def snapshot(self):
        if self.closed:
            raise NktError("Electrical session is closed")
        self.commands.append(("snapshot",))
        return deepcopy(self.state)

    def prepare(self, bias):
        if self.closed:
            raise NktError("Electrical session is closed")
        if self.config.excitation == "none":
            if bias is not None:
                raise NktError("No excitation cannot have a bias setting")
            return
        limit = self.config.max_abs_current_a if self.config.excitation == "dc_current" else self.config.max_abs_voltage_v
        number(bias, "bias", -limit, limit)
        self.commands.append(("source_set", bias))
        self.state["source"].update(setpoint=bias, output_enabled=True)

    def read(self, *, illuminated):
        state = self.snapshot()
        source = state["source"]
        bias = source["setpoint"] if source else None
        elapsed = self.clock() - self._epoch
        if self.config.quantity == "voltage_v":
            voltage = (bias or 0.0) * 1000 + 1e-6 + elapsed * 1e-8 + int(illuminated) * 5e-6
            current = None  # Neither 2182A nor the 6221 source setpoint measures I.
        else:
            voltage = bias
            current = voltage / 1e6 + elapsed * 1e-11 + int(illuminated) * 2e-9
        self.commands.append(("read", illuminated))
        return {"voltage_v": voltage, "current_a": current, "state": state}

    def cleanup(self):
        if self.state["source"] is not None:
            self.commands.append(("source_zero_disable",))
            self.state["source"].update(setpoint=0.0, output_enabled=False)
        return self.snapshot()

    def close(self):
        self.commands.append(("close",))
        self.closed = True


class PhotoresponseRunner:
    def __init__(self, config, laser: NktController, electrical, *, clock=time.perf_counter,
                 sleep=time.sleep, on_event=None, authorize_writes=False,
                 confirm_manual_route=False, environment=None, attempt_index=1, power=None):
        config.validate()
        hardware = config.nkt.backend != "simulation"
        if bool(laser.backend.is_hardware) != hardware or bool(electrical.is_hardware) != hardware:
            raise NktError("Mixed simulation/hardware controllers are forbidden")
        if hardware:
            if not authorize_writes or not confirm_manual_route:
                raise NktError("Hardware run requires writes and optical route authorization")
            config.hardware.require_run(config)
        self.authorize_writes, self.confirm_manual_route = authorize_writes, confirm_manual_route
        if (config.environment is None) != (environment is None):
            raise NktError("Environment configuration/controller mismatch")
        if environment is not None and environment.config != config.environment:
            raise NktError("Environment controller configuration differs from the run")
        self.environment, self.attempt_index = environment, attempt_index
        if (config.power is None) != (power is None) or (power is not None and (
                power.config != config.power or power.laser is not laser)):
            raise NktError("Power configuration/controller mismatch")
        self.power = power
        from .photoresponse_combination import PhotoresponseCombinationPlan
        self.plan = PhotoresponseCombinationPlan(config)
        if laser.config != config.nkt or electrical.config != config:
            raise NktError("Controller configurations must match the run")
        self.config, self.laser, self.electrical = config, laser, electrical
        self.clock, self.sleep, self.on_event = clock, sleep, on_event
        self._used = False
        self._epoch = None
        self.record = {"schema_version": 1, "measurement_type": "dc_photoresponse",
                       "simulated": not hardware, "captured_at_utc": utc_now(), "completed": False,
                       "outcome": "rejected", "measurement_config": config.snapshot(),
                       "conditions": [], "events": [], "error": None,
                       "cleanup": {"errors": [], "laser_off_confirmed": False,
                                   "electrical_safe_confirmed": False},
                       "last_confirmed_electrical_state": None, "electrical_state_current": False,
                       "environment": None, "measured_power_w": None,
                       "combination": self.plan.snapshot()}

    def _event(self, phase, **values):
        event = {"phase": phase, "captured_at_utc": utc_now(), **deepcopy(values)}
        self.record["events"].append(event)
        if self.on_event:
            self.on_event(event)

    def _state(self):
        self.record["electrical_state_current"] = False
        state = self.electrical.snapshot()
        self._retain_state(state)
        return state

    def _retain_state(self, state):
        self.record["last_confirmed_electrical_state"] = {
            "captured_at_utc": utc_now(), "readback": deepcopy(state)}
        self.record["electrical_state_current"] = True

    def _check_state(self, state, bias, *, inactive=False):
        if state["error"] is not None or state["compliance_tripped"] is not False:
            raise NktError("Electrical error or compliance trip")
        if state["excitation"] != self.config.excitation:
            raise NktError("Excitation mode changed")
        expected_detector = ("SIMULATED " if self.record["simulated"] else "") + self.config.detector
        if state["detector"] != expected_detector:
            raise NktError("Detector identity changed")
        source = state["source"]
        if self.config.excitation == "none":
            if source is not None:
                raise NktError("No excitation requires an absent source")
        elif source is None or type(source["output_enabled"]) is not bool:
            raise NktError("Unknown source state")
        elif inactive:
            if source["output_enabled"] or source["setpoint"] != 0:
                raise NktError("Preflight/cleanup requires confirmed zero and disabled source")
        else:
            tolerance = 0
            if self.config.hardware:
                tolerance = (self.config.hardware.source["tolerance_a"] if self.config.excitation == "dc_current"
                             else self.config.hardware.detector["tolerance_v"])
            if not source["output_enabled"] or not math.isclose(source["setpoint"], bias, abs_tol=tolerance, rel_tol=1e-7):
                raise NktError("Source output or setpoint changed")

    def _window(self, condition, phase, bias):
        illuminated = phase == "light"
        window = {"phase": phase, "samples": []}
        condition["windows"].append(window)
        self.laser.set_emission(illuminated)
        def guard():
            state = self._state()
            self._check_state(state, bias)
            return {"electrical": state,
                    "environment": self.environment.verify() if self.environment else None}
        if self.power and illuminated:
            self.power.qualify(guard)
        self.sleep(self.config.settle_s)
        for _ in range(self.config.samples_per_window):
            # The interval also keeps one-sample windows strictly ordered with fake clocks.
            self.sleep(self.config.sample_interval_s)
            optical_before = self.laser.verify_point(emitting=illuminated, phase=phase + "_before")
            environment_before = self.environment.verify() if self.environment else None
            before = self._state()
            self._check_state(before, bias)
            sample = {"valid": False, "accepted": False, "captured_at_utc": utc_now(),
                      "started_s": self.clock() - self._epoch, "finished_s": None,
                      "voltage_v": None, "current_a": None,
                      "optical_before": optical_before, "electrical_before": before,
                      "environment_before": environment_before,
                      "axis_indices": deepcopy(condition["axis_indices"]),
                      "combination_order": list(self.plan.order)}
            window["samples"].append(sample)
            self.record["electrical_state_current"] = False
            try:
                if self.power:
                    sample["power_before"] = self.power.formal_sample(phase + "_before", illuminated=illuminated, guard=guard)
                sample["started_s"] = self.clock() - self._epoch
                reading = self.electrical.read(illuminated=illuminated)
                sample["finished_s"] = self.clock() - self._epoch
                sample["raw_reading"] = deepcopy(reading)
                if self.electrical.is_hardware:
                    sample["started_s"] = reading["acquisition_started_monotonic_s"] - self._epoch
                    sample["finished_s"] = reading["acquisition_finished_monotonic_s"] - self._epoch
                sample.update(voltage_v=reading["voltage_v"], current_a=reading["current_a"])
                self._retain_state(reading["state"])
                self._check_state(reading["state"], bias)
                for quantity, limit in (("voltage_v", self.config.max_abs_voltage_v),
                                        ("current_a", self.config.max_abs_current_a)):
                    value = sample[quantity]
                    if value is not None:
                        if limit is None:
                            raise NktError("Unexpected measured current without an approved current limit")
                        number(value, quantity, -limit, limit)
                number(sample[self.config.quantity], self.config.quantity, -math.inf, math.inf)
                if self.power:
                    sample["power_after"] = self.power.formal_sample(phase + "_after", illuminated=illuminated, guard=guard)
                sample["optical_after"] = self.laser.verify_point(emitting=illuminated, phase=phase + "_after")
                sample["electrical_after"] = self._state()
                self._check_state(sample["electrical_after"], bias)
                sample["environment_after"] = self.environment.verify() if self.environment else None
                sample["module_readings"] = self._module_readings(sample)
                sample["valid"] = True
            except (Exception, KeyboardInterrupt) as exc:
                sample["error"] = f"{type(exc).__name__}: {exc}"
                detector = getattr(self.electrical, "detector", None)
                sample["detector_partial_raw"] = deepcopy(getattr(detector, "partial_raw", {}))
                sample["detector_last_raw_reading"] = getattr(detector, "last_raw_reading", None)
                raise
            finally:
                try:
                    self._event("raw_sample", condition_id=condition["condition_id"],
                                window=phase, sample=sample)
                except (Exception, KeyboardInterrupt) as exc:
                    sample["audit_error"] = f"{type(exc).__name__}: {exc}"
                    if "error" not in sample:
                        raise

    def _module_readings(self, sample):
        """Every selected module's actual settings, measurements and guard status."""
        optical, electrical = sample["optical_after"], sample["electrical_after"]
        source = optical["source"]
        filt = optical["filter"]
        if hasattr(self.config.nkt, "diode_model"):
            actual = {"current_setpoint_a": source["getLdCurrSetpoint"],
                      "nominal_wavelength_nm": optical["nominal_wavelength_nm"]}
            optical_measurements = {"ld_current_a": source["measLdCurr"],
                                    "ld_voltage_v": source["measLdVolt"]}
            role = "cld1010lp"
        else:
            actual = {"source_level_pct": source["level_pct"], "pulse_picker_ratio": source["pulse_picker_ratio"]}
            if filt and filt.get("kind") == "varia":
                actual.update(wavelength_nm=(filt["lower_edge_nm"] + filt["upper_edge_nm"]) / 2,
                              bandwidth_nm=filt["upper_edge_nm"] - filt["lower_edge_nm"], nd_pct=filt["nd_pct"])
            optical_measurements, role = {}, "nkt"
        rows = [{"module": role, "actual": actual, "measurements": optical_measurements,
                 "status": {"before": sample["optical_before"], "after": optical}},
                {"module": self.config.detector, "actual": electrical.get("detector_readback"),
                 "measurements": {"voltage_v": sample["voltage_v"], "current_a": sample["current_a"]},
                 "status": {"before": sample["electrical_before"], "after": electrical}}]
        if self.config.excitation == "dc_current":
            rows.append({"module": "keithley6221_source", "actual": electrical["source"],
                         "measurements": {"current_a": None, "voltage_v": None},
                         "status": {"before": sample["electrical_before"], "after": electrical}})
        elif electrical.get("relay_host_readback") is not None:
            rows.append({"module": "keithley6221_relay_host", "actual": electrical["relay_host_readback"],
                         "measurements": {"current_a": None, "voltage_v": None},
                         "status": {"role": "query_only_transport_host", "output_control_created": False}})
        if self.power:
            rows.append({"module": "pm100d", "actual": {"wavelength_nm": self.power.wavelength},
                         "measurements": {"power_w": (fmean(sample[k]["power_w"] for k in ("power_before", "power_after"))
                                                        if sample["power_before"]["power_w"] is not None else None)},
                         "status": {"before": sample["power_before"], "after": sample["power_after"],
                                    "measurement_plane": self.power.config.meter.measurement_plane}})
        if self.environment:
            rows.append({"module": "environment", "actual": sample["environment_after"],
                         "measurements": sample["environment_after"],
                         "status": {"before": sample["environment_before"], "after": sample["environment_after"]}})
        if {r["module"] for r in rows} != set(self.plan.modules):
            raise NktError("Selected combination module readback is missing")
        return deepcopy(rows)

    def run(self):
        if self._used:
            raise NktError("Photoresponse sessions are single-use")
        self._used = True
        self._epoch = self.clock()
        electrical_owned = False
        try:
            self._event("run_start", measurement_config=self.config.snapshot())
            if self.electrical.is_hardware:
                self.electrical.preflight()
            else:
                self._check_state(self._state(), None, inactive=True)
            self.laser.begin(authorize_writes=self.authorize_writes,
                             confirm_manual_route=self.confirm_manual_route)
            if self.environment:
                self.environment.begin()
                self.environment.prepare(self.config.environment.points[0])
            if self.electrical.is_hardware:
                electrical_owned = True
                self.electrical.begin()
            else:
                electrical_owned = True
            last_environment_index = 0 if self.environment else None
            for coordinates in self.plan.conditions():
                environment_index, environment_point = coordinates["environment"].values()
                optical_index, point = coordinates["optical"].values()
                bias_index, bias = coordinates["bias"].values()
                power_index, target_power = coordinates["target_power_w"].values()
                repeat = coordinates["repeat"]["value"]
                if self.environment and last_environment_index != environment_index:
                    if last_environment_index is not None:
                        self.laser.set_emission(False)
                        self._check_state(self.electrical.cleanup(), None, inactive=True)
                    self.environment.prepare(environment_point)
                    if last_environment_index is not None and self.electrical.is_hardware:
                        self.electrical.begin()
                    last_environment_index = environment_index
                row = {"condition_id": f"photo-{len(self.record['conditions']):06d}",
                       "attempt_index": self.attempt_index, "accepted": False, "optical_index": optical_index,
                       "bias_index": bias_index, "repeat_index": repeat,
                       "power_index": power_index, "target_power_w": target_power,
                       "axis_indices": {name: v["index"] for name, v in coordinates.items()},
                       "combination_order": list(self.plan.order),
                       "environment_index": environment_index, "requested_environment": environment_point,
                       "requested_optical": asdict(point), "excitation": self.config.excitation,
                       "requested_current_a": bias if self.config.excitation == "dc_current" else None,
                       "requested_voltage_v": bias if self.config.excitation == "dc_voltage" else None,
                       "windows": [], "result": None}
                self.record["conditions"].append(row)
                self.laser.prepare_point(point)
                if self.power:
                    self.power.prepare(row, point)
                self.electrical.prepare(bias)
                self._check_state(self._state(), bias)
                for phase in WINDOWS:
                    self._window(row, phase, bias)
                row["result"] = differential(row["windows"], self.config.quantity)
                row["actual"] = row["windows"][1]["samples"][-1]["module_readings"]
                if self.power:
                    row["result"].update(target_power_w=target_power,
                        measured_power_w=self.power.row["formal_mean_power_w"],
                        measurement_plane=self.power.config.meter.measurement_plane)
                self._event("condition_complete", condition=row)
            self.record["outcome"] = "completed"
        except (Exception, KeyboardInterrupt) as exc:
            self.record["error"] = f"{type(exc).__name__}: {exc}"
            self.record["outcome"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "rejected"
        finally:
            cleanup = self.record["cleanup"]
            try:
                optical_cleanup = self.laser.finish()
                cleanup["laser_off_confirmed"] = optical_cleanup["off_confirmed"]
                cleanup["errors"].extend("laser: " + e for e in optical_cleanup["errors"])
            except (Exception, KeyboardInterrupt) as exc:
                cleanup["errors"].append(f"laser: {type(exc).__name__}: {exc}")
            if electrical_owned:
                self.record["electrical_state_current"] = False
                try:
                    state = self.electrical.cleanup()
                    self._retain_state(state)
                    self._check_state(state, None, inactive=True)
                    cleanup["electrical_safe_confirmed"] = True
                except (Exception, KeyboardInterrupt) as exc:
                    cleanup["errors"].append(f"electrical: {type(exc).__name__}: {exc}")
            try:
                self.electrical.close()
            except (Exception, KeyboardInterrupt) as exc:
                cleanup["errors"].append(f"electrical close: {type(exc).__name__}: {exc}")
            self.record["electrical_cleanup_audit"] = deepcopy(getattr(self.electrical, "cleanup_details", []))
            if self.power:
                try:
                    self.power.close()
                except (Exception, KeyboardInterrupt) as exc:
                    cleanup["errors"].append(f"PM100D close: {type(exc).__name__}: {exc}")
                self.record["power_audit"] = deepcopy(getattr(self.power, "audit", {}))
            if self.environment:
                try:
                    result = self.environment.finish(failed=self.record["outcome"] != "completed" or bool(cleanup["errors"]))
                    self.record["environment"] = result
                    cleanup["errors"].extend("environment: " + e for e in result["errors"])
                except (Exception, KeyboardInterrupt) as exc:
                    cleanup["errors"].append("environment: " + str(exc))
            if cleanup["errors"] or not (cleanup["laser_off_confirmed"] and cleanup["electrical_safe_confirmed"]):
                self.record["outcome"] = "rejected"
            # Standalone scan outcome/points do not describe this point session.
            self.record["laser_audit"] = {
                key: deepcopy(value) for key, value in self.laser.record.items()
                if key not in {"completed", "outcome", "error", "points"}}
            self.record["laser_audit"]["record_type"] = "cld_point_session" if hasattr(self.config.nkt, "diode_model") else "nkt_point_session"
            self.record["completed"] = self.record["outcome"] == "completed"
            for row in self.record["conditions"]:
                row["accepted"] = self.record["completed"]
                for window in row["windows"]:
                    for sample in window["samples"]:
                        sample["accepted"] = row["accepted"] and sample["valid"]
        return self.record
