"""Offline dark/light/dark coordinator. No VISA/DLL imports or hardware entry point."""
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
                 sleep=time.sleep, on_event=None):
        config.validate()
        # No factory or resource open exists here. Reject injected hardware too.
        if laser.backend.is_hardware or electrical.is_hardware:
            raise NktError("PhotoresponseRunner is offline-only; real hardware is not commissioned")
        if laser.config != config.nkt or electrical.config != config:
            raise NktError("Controller configurations must match the run")
        self.config, self.laser, self.electrical = config, laser, electrical
        self.clock, self.sleep, self.on_event = clock, sleep, on_event
        self._used = False
        self._epoch = None
        self.record = {"schema_version": 1, "measurement_type": "dc_photoresponse",
                       "simulated": True, "captured_at_utc": utc_now(), "completed": False,
                       "outcome": "rejected", "measurement_config": config.snapshot(),
                       "conditions": [], "events": [], "error": None,
                       "cleanup": {"errors": [], "laser_off_confirmed": False,
                                   "electrical_safe_confirmed": False},
                       "last_confirmed_electrical_state": None, "electrical_state_current": False,
                       "environment": None, "measured_power_w": None}

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
        if state["detector"] != "SIMULATED " + self.config.detector:
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
        elif not source["output_enabled"] or source["setpoint"] != bias:
            raise NktError("Source output or setpoint changed")

    def _window(self, condition, phase, bias):
        illuminated = phase == "light"
        window = {"phase": phase, "samples": []}
        condition["windows"].append(window)
        self.laser.set_emission(illuminated)
        self.sleep(self.config.settle_s)
        for _ in range(self.config.samples_per_window):
            # The interval also keeps one-sample windows strictly ordered with fake clocks.
            self.sleep(self.config.sample_interval_s)
            optical_before = self.laser.verify_point(emitting=illuminated, phase=phase + "_before")
            before = self._state()
            self._check_state(before, bias)
            sample = {"valid": False, "accepted": False, "captured_at_utc": utc_now(),
                      "started_s": self.clock() - self._epoch, "finished_s": None,
                      "voltage_v": None, "current_a": None,
                      "optical_before": optical_before, "electrical_before": before}
            window["samples"].append(sample)
            self.record["electrical_state_current"] = False
            try:
                reading = self.electrical.read(illuminated=illuminated)
                sample["finished_s"] = self.clock() - self._epoch
                sample["raw_reading"] = deepcopy(reading)
                sample.update(voltage_v=reading["voltage_v"], current_a=reading["current_a"])
                self._retain_state(reading["state"])
                self._check_state(reading["state"], bias)
                for quantity, limit in (("voltage_v", self.config.max_abs_voltage_v),
                                        ("current_a", self.config.max_abs_current_a)):
                    value = sample[quantity]
                    if value is not None:
                        number(value, quantity, -limit, limit)
                number(sample[self.config.quantity], self.config.quantity, -math.inf, math.inf)
                sample["optical_after"] = self.laser.verify_point(emitting=illuminated, phase=phase + "_after")
                sample["electrical_after"] = self._state()
                self._check_state(sample["electrical_after"], bias)
                sample["valid"] = True
            except (Exception, KeyboardInterrupt) as exc:
                sample["error"] = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                try:
                    self._event("raw_sample", condition_id=condition["condition_id"],
                                window=phase, sample=sample)
                except (Exception, KeyboardInterrupt) as exc:
                    sample["audit_error"] = f"{type(exc).__name__}: {exc}"
                    if "error" not in sample:
                        raise

    def run(self):
        if self._used:
            raise NktError("Photoresponse sessions are single-use")
        self._used = True
        self._epoch = self.clock()
        electrical_owned = False
        try:
            self._event("run_start", measurement_config=self.config.snapshot())
            self.laser.begin()
            self._check_state(self._state(), None, inactive=True)
            electrical_owned = True
            for optical_index, point in enumerate(self.config.nkt.points):
                for bias_index, bias in enumerate(self.config.bias_points):
                    for repeat in range(self.config.repeats):
                        row = {"condition_id": f"photo-{len(self.record['conditions']):06d}",
                               "attempt_index": 1, "accepted": False, "optical_index": optical_index,
                               "bias_index": bias_index, "repeat_index": repeat,
                               "requested_optical": asdict(point), "excitation": self.config.excitation,
                               "requested_current_a": bias if self.config.excitation == "dc_current" else None,
                               "requested_voltage_v": bias if self.config.excitation == "dc_voltage" else None,
                               "windows": [], "result": None}
                        self.record["conditions"].append(row)
                        self.laser.prepare_point(point)
                        self.electrical.prepare(bias)
                        self._check_state(self._state(), bias)
                        for phase in WINDOWS:
                            self._window(row, phase, bias)
                        row["result"] = differential(row["windows"], self.config.quantity)
                        self._event("condition_complete", condition=row)
            self.record["outcome"] = "completed"
        except (Exception, KeyboardInterrupt) as exc:
            self.record["error"] = f"{type(exc).__name__}: {exc}"
            self.record["outcome"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "rejected"
        finally:
            cleanup = self.record["cleanup"]
            optical_cleanup = self.laser.finish()
            cleanup["laser_off_confirmed"] = optical_cleanup["off_confirmed"]
            cleanup["errors"].extend("laser: " + e for e in optical_cleanup["errors"])
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
            if cleanup["errors"] or not (cleanup["laser_off_confirmed"] and cleanup["electrical_safe_confirmed"]):
                self.record["outcome"] = "rejected"
            # Standalone scan outcome/points do not describe this point session.
            self.record["laser_audit"] = {
                key: deepcopy(value) for key, value in self.laser.record.items()
                if key not in {"completed", "outcome", "error", "points"}}
            self.record["laser_audit"]["record_type"] = "nkt_point_session"
            self.record["completed"] = self.record["outcome"] == "completed"
            for row in self.record["conditions"]:
                row["accepted"] = self.record["completed"]
                for window in row["windows"]:
                    for sample in window["samples"]:
                        sample["accepted"] = row["accepted"] and sample["valid"]
        return self.record
