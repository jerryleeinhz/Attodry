"""Dark/light/dark coordinator using injected, explicitly authorized controllers."""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from dataclasses import asdict
import math
from statistics import fmean
import time

from .nkt_config import NktError, number
from .nkt_control import NktController, utc_now
from .photoresponse_config import PhotoresponseConfig


WINDOWS = ("dark_before", "light", "dark_after")


def single_dark_difference(dark, light, quantity):
    """Match one freshly acquired baseline by electrical coordinate and visit."""
    if (dark.get("optical_state") != "dark" or light.get("optical_state") != "light"
            or dark["baseline_key"] != light["baseline_key"]):
        raise NktError("Dark/light baseline coordinates or visit differ")
    def summary(row):
        samples = row["windows"][0]["samples"]
        if not samples or any(not s["valid"] for s in samples):
            raise NktError("Difference requires valid dark and light samples")
        return (fmean(number(s[quantity], quantity, -math.inf, math.inf) for s in samples),
                fmean((s["started_s"] + s["finished_s"]) / 2 for s in samples))
    d, td = summary(dark)
    value, tl = summary(light)
    if tl <= td:
        raise NktError("Light must follow its dark baseline")
    return {"method": "single_dark_baseline_v1", "quantity": quantity,
            "dark_condition_id": dark["condition_id"], "dark_mean": d, "light_mean": value,
            "dark_mean_time_s": td, "light_mean_time_s": tl, "baseline_age_s": tl - td,
            "baseline_drift_corrected": False, "delta_" + quantity: value - d}


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
        if config.excitation in ("dc_current", "dc_voltage"):
            self.state["source"] = {"model": "SIMULATED " + (
                "6221" if config.excitation == "dc_current" else "2400"),
                "output_enabled": False, "setpoint": 0.0}
        self.closed = False
        self.last_change_monotonic_s = None

    def snapshot(self):
        if self.closed:
            raise NktError("Electrical session is closed")
        self.commands.append(("snapshot",))
        return deepcopy(self.state)

    def prepare(self, bias):
        if self.closed:
            raise NktError("Electrical session is closed")
        if self.config.excitation not in ("dc_current", "dc_voltage"):
            if bias is not None:
                raise NktError("No excitation cannot have a bias setting")
            return
        limit = self.config.max_abs_current_a if self.config.excitation == "dc_current" else self.config.max_abs_voltage_v
        number(bias, "bias", -limit, limit)
        if self.state["source"]["setpoint"] != bias or not self.state["source"]["output_enabled"]:
            self.last_change_monotonic_s = self.clock()
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
                 confirm_manual_route=False, environment=None, attempt_index=1, power=None, smu=None):
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
        if (config.smu is None) != (smu is None) or (smu is not None and (
                smu.config != config.smu or smu.is_hardware != hardware)):
            raise NktError("SMU configuration/controller mismatch")
        self.smu = smu
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
        self.record["timings"] = []

    @contextmanager
    def _timed(self, phase, **values):
        row = {"phase": phase, **values, "started_monotonic_s": self.clock(), "completed": False}
        self.record["timings"].append(row)
        try:
            yield row
            row["completed"] = True
        finally:
            row["finished_monotonic_s"] = self.clock()
            row["duration_s"] = row["finished_monotonic_s"] - row["started_monotonic_s"]

    def _settle(self, last_change, required, destination):
        now = self.clock()
        remaining = max(0., last_change + required - now)
        wait = {"policy": "last-change-remaining-settle-v1", "required_s": required,
                "last_change_monotonic_s": last_change, "checked_monotonic_s": now,
                "already_elapsed_s": now - last_change, "sleep_requested_s": remaining, "completed": False}
        destination["settling_wait"] = wait
        with self._timed("settle", requested_s=remaining):
            if remaining:
                self.sleep(remaining)
        wait.update(completed=True, finished_monotonic_s=self.clock())

    def _event(self, phase, **values):
        started = self.clock()
        event = {"phase": phase, "captured_at_utc": utc_now(), **deepcopy(values)}
        self.record["events"].append(event)
        try:
            if self.on_event:
                self.on_event(event)
        finally:
            self.record["timings"].append({"phase": "audit_event", "event_phase": phase,
                "started_monotonic_s": started, "finished_monotonic_s": self.clock(),
                "duration_s": self.clock() - started})

    def _state(self):
        self.record["electrical_state_current"] = False
        with self._timed("electrical_state"):
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
        if self.config.excitation not in ("dc_current", "dc_voltage"):
            if source is not None:
                raise NktError("This electrical detector session must not own a sample source")
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

    def _window(self, condition, phase, bias, *, manage_optics=True):
        illuminated = phase == "light"
        window = {"phase": phase, "samples": []}
        condition["windows"].append(window)
        if self.smu:
            self.smu.verify()
        last_change = None
        if manage_optics:
            self.laser.set_emission(illuminated)
            last_change = self.clock()
        def emission_changed():
            nonlocal last_change
            last_change = self.clock()
        def guard():
            self.record["electrical_state_current"] = False
            state = getattr(self.electrical, "guard", self.electrical.snapshot)()
            self._check_state(state, bias)
            return {"electrical": state,
                    "smu": self.smu.verify() if self.smu else None,
                    "environment": self.environment.verify() if self.environment else None}
        if manage_optics:
            if self.power and illuminated:
                self._check_state(self._state(), bias)
                self.power.qualify(guard, on_emission_changed=emission_changed)
                self._check_state(self._state(), bias)
            self._settle(last_change, self.config.settle_s, window)
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
                if self.smu:
                    sample["smu_before"] = self.smu.verify()
                if self.power:
                    sample["power_before"] = self.power.formal_sample(phase + "_before", illuminated=illuminated, guard=guard)
                sample["started_s"] = self.clock() - self._epoch
                with self._timed("electrical_acquisition", condition_id=condition["condition_id"]):
                    reading = self.electrical.read(illuminated=illuminated)
                sample["finished_s"] = self.clock() - self._epoch
                sample["raw_reading"] = deepcopy(reading)
                if self.electrical.is_hardware:
                    sample["started_s"] = reading["acquisition_started_monotonic_s"] - self._epoch
                    sample["finished_s"] = reading["acquisition_finished_monotonic_s"] - self._epoch
                sample.update(voltage_v=reading["voltage_v"], current_a=reading["current_a"])
                self._retain_state(reading["state"])
                self._check_state(reading["state"], bias)
                if self.smu:
                    sample["smu_after"] = self.smu.verify()
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
                if self.smu:
                    sample["smu_last_readback"] = deepcopy(self.smu.audit["last_confirmed"])
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
        if self.smu:
            for role in self.config.smu.roles:
                timed = sample["smu_after"]["readings"][role]
                reading = timed["reading"]
                rows.append({"module": role,
                    "actual": {"source_mode": self.config.smu.hardware.require_role(role).source_mode.value,
                               "source_setpoint": reading["source_setpoint"], "output_enabled": reading["output_enabled"]},
                    "measurements": {"voltage_v": reading["voltage_v"],
                                     "leakage_current_a" if role.startswith("gate_") else "current_a": reading["current_a"]},
                    "status": {"before": sample["smu_before"]["readings"][role], "after": timed,
                               "role": "gate" if role.startswith("gate_") else "sample_excitation"}})
        if {r["module"] for r in rows} != set(self.plan.modules):
            raise NktError("Selected combination module readback is missing")
        return deepcopy(rows)

    def _condition(self, coordinates):
        bias = coordinates["bias"]["value"]
        point, smu_point = coordinates["optical"]["value"], coordinates["smu"]["value"]
        target = coordinates["target_power_w"]["value"]
        row = {"condition_id": f"photo-{len(self.record['conditions']):06d}",
               "attempt_index": self.attempt_index, "accepted": False,
               **{name + "_index": coordinates[axis]["index"] for name, axis in (
                   ("optical", "optical"), ("bias", "bias"), ("repeat", "repeat"),
                   ("power", "target_power_w"), ("smu", "smu"), ("environment", "environment"))},
               "target_power_w": None if target == "dark" else target,
               "requested_smu": asdict(smu_point) if smu_point else None,
               "axis_indices": {name: v["index"] for name, v in coordinates.items()},
               "combination_order": list(self.plan.order),
               "requested_environment": coordinates["environment"]["value"],
               "requested_optical": asdict(point), "excitation": self.config.excitation,
               "requested_current_a": bias if self.config.excitation == "dc_current" else None,
               "requested_voltage_v": bias if self.config.excitation == "dc_voltage" else None,
               "windows": [], "result": None}
        if smu_point and self.config.excitation in ("smu_current", "smu_voltage"):
            row["requested_current_a" if self.config.excitation == "smu_current" else "requested_voltage_v"] = smu_point.coordinates["smu_bias"]
        self.record["conditions"].append(row)
        return row

    def _suspend_electrical(self):
        with self._timed("electrical_cleanup"):
            self._check_state(self.electrical.cleanup(), None, inactive=True)
            if self.smu:
                self.smu.suspend()

    def _run_shared_dark(self):
        """Outer optical points own illumination; inner leaves only acquire."""
        from itertools import groupby
        from .power_feedback import actuator_domain
        baselines, previous = {}, None
        environment_visit = self.plan.axis_visit(next(self.plan.conditions()), "environment")
        for visit, entries in groupby(self.plan.conditions(), lambda c: self.plan.axis_visit(c, "target_power_w")):
            coordinates = next(entries)
            leaves = [coordinates, *entries]
            point = coordinates["optical"]["value"]
            scan_visit = visit[:-1]
            new_environment = self.plan.axis_visit(coordinates, "environment")
            if self.environment and new_environment != environment_visit:
                with self._timed("environment_prepare", visit=new_environment):
                    self.environment.prepare(coordinates["environment"]["value"])
                environment_visit = new_environment
            dark = coordinates["target_power_w"]["value"] == "dark"
            phase = "dark" if dark else "light"
            if dark:
                baselines, previous = {}, None
            first = self._condition(coordinates)
            first["optical_state"] = phase
            first["power_visit"] = visit
            initial = previous["value"] if (previous and previous["scan_visit"] == scan_visit
                                             and self.config.power.initial_actuator_mode == "previous") else None
            with self._timed("optical_prepare", visit=visit):
                self.laser.prepare_power_point(point, self.config.power, initial_value=initial)
                self.power.prepare(first, point)
            self.power.row["initial_actuator"] = {"mode": self.config.power.initial_actuator_mode,
                "source": "previous" if initial is not None else "point", "value": initial,
                "previous_power_visit": previous["visit"] if initial is not None else None}
            optical_session = self.power.row
            last_change = self.clock()
            def changed():
                nonlocal last_change
                last_change = self.clock()
            def off_guard():
                state = self.electrical.guard() if hasattr(self.electrical, "guard") else self.electrical.snapshot()
                self._check_state(state, None, inactive=True)
                return {"electrical": state, "smu": "suspended" if self.smu else None,
                        "environment": self.environment.verify() if self.environment else None}
            off_guard()
            if not dark:
                self.laser.set_emission(True)
                changed()
                with self._timed("power_qualification", visit=visit):
                    self.power.qualify(off_guard, on_emission_changed=changed)
            # Recheck after the long optical operation, before enabling sources.
            with self._timed("electrical_begin", visit=visit):
                if self.electrical.is_hardware:
                    self.electrical.begin()
                active_zero = self.electrical.is_hardware and self.config.quantity == "current_a"
                self._check_state(self._state(), 0. if active_zero else None, inactive=not active_zero)
                if self.smu:
                    self.smu.begin()
            required = (self.config.dark_recovery_s if dark and self.config.dark_recovery_s is not None
                        else self.config.settle_s)
            self._settle(last_change, required, optical_session)
            last_electrical_change = max(last_change, getattr(self.electrical, "last_change_monotonic_s", None) or last_change)
            previous_electrical_key = None
            for index, leaf in enumerate(leaves):
                row = first if index == 0 else self._condition(leaf)
                bias, smu_point = leaf["bias"]["value"], leaf["smu"]["value"]
                row.update(optical_state=phase, power_visit=visit,
                           baseline_key=[scan_visit, leaf["bias"]["index"], leaf["smu"]["index"]],
                           power=optical_session)
                with self._timed("electrical_prepare", condition_id=row["condition_id"]):
                    self.electrical.prepare(bias)
                    self._check_state(self._state(), bias)
                    if self.smu:
                        def transition_guard():
                            self.laser.verify_point(emitting=not dark, phase="smu_transition")
                            self._check_state(self._state(), bias)
                            if self.environment:
                                self.environment.verify()
                        row["smu_prepared"] = self.smu.prepare(smu_point, transition_guard)
                electrical_key = (bias, tuple(smu_point.coordinates.items()) if smu_point else None)
                if (self.config.excitation != "none" or self.smu) and electrical_key != previous_electrical_key:
                    confirmed_change = getattr(self.electrical, "last_change_monotonic_s", None)
                    # The pinned SMU session has no setting-completion callback;
                    # retain a conservative post-prepare timestamp for that path.
                    last_electrical_change = (self.clock() if self.smu or confirmed_change is None
                                              else confirmed_change)
                previous_electrical_key = electrical_key
                # Source/SMU drivers retain their own ramp waits. This additional
                # sample-response wait starts after the latest electrical prepare.
                self._settle(last_electrical_change, self.config.settle_s, row)
                self._window(row, phase, bias, manage_optics=False)
                row["actual"] = row["windows"][0]["samples"][-1]["module_readings"]
                key = (leaf["bias"]["index"], leaf["smu"]["index"])
                if dark:
                    baselines[key] = row
                    samples = row["windows"][0]["samples"]
                    row["result"] = {"method": "single_dark_reference_v1", "quantity": self.config.quantity,
                                     "dark_mean": fmean(s[self.config.quantity] for s in samples)}
                else:
                    if key not in baselines:
                        raise NktError("No matching dark coordinate in this power scan")
                    row["result"] = single_dark_difference(baselines[key], row, self.config.quantity)
                    row["result"].update(target_power_w=row["target_power_w"],
                        measured_power_w=fmean(s[k]["power_w"] for s in row["windows"][0]["samples"]
                                              for k in ("power_before", "power_after")),
                        measurement_plane=self.config.power.meter.measurement_plane)
                self._event("condition_complete", condition=row)
            self._suspend_electrical()
            optical = self.laser.verify_point(emitting=not dark, phase="power_point_complete")
            if not dark:
                field, _, _, _ = actuator_domain(self.config.power)
                value = (optical["source"]["getLdCurrSetpoint"] if field == "current_a" else
                         optical["filter"]["nd_pct"] if field == "nd_pct" else optical["source"]["level_pct"])
                previous = {"scan_visit": scan_visit, "visit": visit, "value": value}
            self.laser.set_emission(False)

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
                with self._timed("environment_prepare", initial=True):
                    self.environment.prepare(self.config.environment.points[0])
            electrical_owned = True
            if self.config.shared_dark:
                self._run_shared_dark()
            else:
                if self.electrical.is_hardware:
                    electrical_owned = True
                    self.electrical.begin()
                else:
                    electrical_owned = True
                if self.smu:
                    self.smu.begin()
                last_environment_visit = self.plan.axis_visit(next(self.plan.conditions()), "environment")
                for coordinates in self.plan.conditions():
                    environment_index, environment_point = coordinates["environment"].values()
                    optical_index, point = coordinates["optical"].values()
                    bias_index, bias = coordinates["bias"].values()
                    power_index, target_power = coordinates["target_power_w"].values()
                    smu_index, smu_point = coordinates["smu"].values()
                    repeat = coordinates["repeat"]["value"]
                    environment_visit = self.plan.axis_visit(coordinates, "environment")
                    if self.environment and last_environment_visit != environment_visit:
                        if last_environment_visit is not None:
                            self.laser.set_emission(False)
                            self._check_state(self.electrical.cleanup(), None, inactive=True)
                            if self.smu:
                                self.smu.suspend()
                        self.environment.prepare(environment_point)
                        if last_environment_visit is not None and self.electrical.is_hardware:
                            self.electrical.begin()
                        if self.smu:
                            self.smu.begin()
                        last_environment_visit = environment_visit
                    row = self._condition(coordinates)
                    self.laser.prepare_point(point)
                    if self.power:
                        self.power.prepare(row, point)
                    self.electrical.prepare(bias)
                    self._check_state(self._state(), bias)
                    if self.smu:
                        def transition_guard():
                            self.laser.verify_point(emitting=False, phase="smu_transition")
                            self._check_state(self._state(), bias)
                            if self.environment: self.environment.verify()
                        row["smu_prepared"] = self.smu.prepare(smu_point, transition_guard)
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
            if self.smu:
                cleanup["smu_safe_confirmed"] = False
                try:
                    self.smu.finish(failed=self.record["outcome"] != "completed")
                    cleanup["smu_safe_confirmed"] = True
                except (Exception, KeyboardInterrupt) as exc:
                    cleanup["errors"].append(f"SMU: {type(exc).__name__}: {exc}")
                self.record["smu_audit"] = deepcopy(self.smu.audit)
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
