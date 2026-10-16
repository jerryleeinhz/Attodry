"""Module-selected Combination execution using the shared acquisition window.

Traversal is independent of device enable order. Environmental visits, optics
qualification, fresh electrical verification and power barriers retain ownership
at their physical boundaries. Whole-window failure evidence is never promoted.
"""
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import asdict
from statistics import fmean
import time

from .nkt_config import NktError
from .nkt_control import utc_now
from .photoresponse import PhotoresponseRunner, SimulatedElectrical, single_dark_difference
from .photoresponse_power import PowerDeviation, PowerRecoveryExhausted


class _AbsentOptics:
    def verify_point(self, **kwargs):
        return {"selected": False, "measurement_performed": False}


class SourceOnlyElectrical:
    """6221 module without constructing an unused detector."""
    is_hardware = True

    def __init__(self, config, resources):
        from .photoresponse_devices import CurrentSource6221
        self.config = config
        self.source = CurrentSource6221(resources["6221"],
            **{k: v for k, v in config.hardware.source.items() if k != "address"},
            max_current_a=config.max_abs_current_a)
        self.started = False

    @property
    def last_change_monotonic_s(self):
        return self.source.last_change_monotonic_s

    def snapshot(self):
        from .photoresponse_keithley import _flag
        raw = self.source.snapshot()
        self.source.state_current = False
        coupled = {mode: self.source._query(f"SOUR:{mode}:ARM?") for mode in ("DELT", "PDEL", "DCON")}
        raw["coupled_arm_raw_responses"] = coupled
        if any(_flag(value) for value in coupled.values()):
            raise NktError("6221 coupled acquisition is armed; DC module takeover refused")
        if self.source.ready:
            self.source.check(raw)
        self.source._retain(raw)
        return {"detector": "none", "excitation": "dc_current", "source": {
            "model": "6221", "setpoint": raw["source_current_setpoint_a"],
            "output_enabled": raw["output_enabled"], "readback": raw},
            "compliance_tripped": raw["output_enabled"] and raw["compliance_tripped"],
            "error": "unsafe source" if raw["unsafe_condition"] else None}

    guard = snapshot

    def preflight(self):
        state = self.snapshot()
        if state["source"]["output_enabled"] or state["source"]["setpoint"] != 0:
            raise NktError("6221 preflight requires confirmed zero/OFF")

    def begin(self, **kwargs):
        self.preflight()
        self.started = True
        self.source.begin()
        return self.snapshot()

    def prepare(self, bias):
        self.source.set_bias(bias)
        return self.snapshot()

    def cleanup(self):
        if self.source.owned:
            result = self.source.cleanup()
            if not result["safe"]:
                raise NktError("; ".join(result["errors"]))
        return self.snapshot()


class ModularRunner(PhotoresponseRunner):
    """Reuse the tested voltage window and safety checks, with modular traversal."""
    def __init__(self, config, electrical, *, optics=None, environment=None, smu=None,
                 clock=time.perf_counter, sleep=time.sleep, on_event=None, attempt_index=1):
        config.validate()
        if bool(electrical.is_hardware) != config.hardware_mode:
            raise NktError("Mixed combination controller modes")
        if electrical.config is not config:
            raise NktError("Electrical session does not own this combination configuration")
        for module, controller, selected in (("optics", optics, config.modular.optics),
                ("environment", environment, config.environment), ("smu", smu, config.smu)):
            if (controller is None) != (selected is None):
                raise NktError(f"{module} configuration/controller mismatch")
        if optics and (optics.laser.config != config.nkt
                or bool(optics.laser.backend.is_hardware) != config.hardware_mode
                or (optics.power.config if optics.power else None) != config.power):
            raise NktError("Optics configuration/controller mismatch")
        if environment and environment.config != config.environment:
            raise NktError("Environmental controller configuration differs from the run")
        if smu and (smu.config != config.smu or smu.is_hardware != config.hardware_mode):
            raise NktError("SMU configuration/controller mismatch")
        self.config, self.plan, self.electrical = config, config.plan, electrical
        self.optics, self.environment, self.smu = optics, environment, smu
        self.laser = optics.guarded_laser if optics else _AbsentOptics()
        self.power = optics.power if optics else None
        self.clock, self.sleep, self.on_event = clock, sleep, on_event
        self.attempt_index = attempt_index
        self._used, self._epoch = False, None
        self.record = {"schema_version": 1, "measurement_type": "dc_photoresponse",
            "record_contract": "modular-combination-v1", "simulated": not config.hardware_mode,
            "captured_at_utc": utc_now(), "completed": False, "outcome": "rejected",
            "measurement_config": config.snapshot(), "combination": self.plan.snapshot(),
            "conditions": [], "events": [], "timings": [], "error": None,
            "last_confirmed_electrical_state": None, "electrical_state_current": False,
            "cleanup": {"errors": [], "laser_off_confirmed": optics is None,
                        "electrical_safe_confirmed": False,
                        "unselected_modules": [m for m, present in (("optics", optics), ("environment", environment), ("smu", smu)) if present is None]}}
        self.baselines, self.visits = {}, {}
        self._electrical_begun = False
        self._optics_active = False
        self._retries_used = 0

    def _guard(self, bias=None, *, inactive=False):
        state = getattr(self.electrical, "guard", self.electrical.snapshot)()
        self._check_state(state, bias, inactive=inactive)
        result = {"electrical": state, "environment": self.environment.verify() if self.environment else None}
        if self.smu and self.smu.begun and self.smu.point is not None:
            result["smu"] = self.smu.verify()
        return result

    def _suspend(self):
        self._suspend_electrical()
        self._electrical_begun = False

    def _condition(self, condition):
        requested = condition["requested"]
        optical_axis = condition["axes"].get("optics")
        grid = optical_axis.get("grid", {}) if optical_axis else {}
        optical_state = grid.get("optical_state", "none")
        optical = {k.removeprefix("optical_"): v for k, v in requested.items()
                   if k.startswith("optical_") and k != "optical_target_power_w"}
        if "current_a" in optical:
            optical["current_a"] = optical.pop("current_a")
        row = {"condition_id": condition["condition_id"], "sequence_index": condition["sequence_index"],
            "attempt_index": self.attempt_index, "accepted": False, "repeat_index": condition["repeat_index"],
            "axis_indices": {m: p["index"] for m, p in condition["axes"].items()},
            "axes": deepcopy(condition["axes"]), "requested": deepcopy(requested),
            "combination_order": list(self.plan.order), "optical_state": optical_state,
            "requested_optical": optical, "excitation": self.config.excitation,
            "target_power_w": requested.get("optical_target_power_w") if optical_state == "light" else None,
            "requested_current_a": requested.get("bias_current_a", requested.get("smu_bias_a")),
            "requested_voltage_v": requested.get("smu_bias_v"),
            "requested_environment": {k: requested[v] for k, v in (("temperature_k", "temperature_k"),
                ("bx_t", "field_x_t"), ("bz_t", "field_z_t")) if v in requested},
            "requested_smu": None, "windows": [], "result": None, "power_attempts": []}
        if self.smu:
            point = self.config.smu.points[condition["axes"]["smu"]["index"]]
            row["requested_smu"] = asdict(point)
        # Coordinate identities, not just numeric values: duplicate points,
        # repeated rounds and revisits cannot silently share a dark baseline.
        outer = tuple((m, p["index"]) for m, p in condition["axes"].items() if m != "optics")
        row["baseline_key"] = [condition["repeat_index"], outer, grid.get("optical_group_index")]
        self.record["conditions"].append(row)
        self._event("condition_start", condition_id=row["condition_id"], attempt_index=self.attempt_index,
                    optical_state=optical_state, target_power_w=row["target_power_w"],
                    axis_indices=row["axis_indices"], module_path=condition["axes"], requested=requested)
        return row

    def _module_readings(self, sample):
        rows = []
        electrical = sample["electrical_after"]
        if self.optics:
            optical = sample["optical_after"]
            measurements = {}
            if self.power:
                powers = [sample[k]["power_w"] for k in ("power_before", "power_after") if sample[k]["power_w"] is not None]
                measurements["power_w"] = fmean(powers) if powers else None
            rows.append({"module": "optics", "actual": deepcopy(optical), "measurements": measurements,
                "status": {"before": sample["optical_before"], "after": optical,
                           "measurement_plane": self.power.config.meter.measurement_plane if self.power else None}})
        if self.config.detector_selected:
            rows.append({"module": "nanovoltmeter", "actual": electrical.get("detector_readback"),
                "measurements": {"voltage_v": sample["voltage_v"], "current_a": sample["current_a"]},
                "status": {"before": sample["electrical_before"], "after": electrical}})
        if "current_source" in self.plan.order:
            rows.append({"module": "current_source", "actual": electrical["source"],
                "measurements": {"current_a": None}, "status": {"setpoint_is_not_measured_current": True}})
        if self.environment:
            state = sample["environment_after"]
            for module in self.config.environment.modules:
                actual = ({"temperature_k": state["measurement_temperature_k"]} if module == "temperature" else
                          {"field_x_t": state["field"]["bx_t"], "field_z_t": state["field"]["bz_t"]})
                rows.append({"module": module, "actual": actual, "measurements": actual,
                             "status": {"quality": state["quality"], "axis_qualification": state["axis_qualification"]}})
        if self.smu:
            state = sample["smu_after"]
            measured = {}
            for role, timed in state["readings"].items():
                value = timed["reading"]
                measured[role+"_voltage_v"] = value["voltage_v"]
                measured[role+("_leakage_current_a" if role.startswith("gate_") else "_current_a")] = value["current_a"]
            rows.append({"module": "smu", "actual": state, "measurements": measured,
                         "status": {"before": sample["smu_before"], "after": state}})
        if {r["module"] for r in rows} != set(self.plan.order):
            raise NktError("A selected module has no fresh readback")
        return deepcopy(rows)

    def _readback_window(self, row, phase, bias):
        """Module-only acquisitions do not fabricate detector V/I observations."""
        window = {"phase": phase, "samples": []}
        row["windows"].append(window)
        for _ in range(self.config.samples_per_window):
            self.sleep(self.config.sample_interval_s)
            before = self._guard(bias)
            sample = {"valid": False, "accepted": False, "voltage_v": None, "current_a": None,
                "captured_at_utc": utc_now(), "started_s": self.clock()-self._epoch,
                "electrical_before": before["electrical"], "environment_before": before["environment"],
                "optical_before": self.optics.verify() if self.optics else {"selected": False}}
            window["samples"].append(sample)
            try:
                if self.smu:
                    sample["smu_before"] = self.smu.verify()
                if self.power:
                    sample["power_before"] = self.power.formal_sample(phase+"_before", illuminated=phase == "light", guard=lambda: self._guard(bias))
                    sample["power_after"] = self.power.formal_sample(phase+"_after", illuminated=phase == "light", guard=lambda: self._guard(bias))
                sample["optical_after"] = self.optics.verify() if self.optics else {"selected": False}
                after = self._guard(bias)
                sample.update(electrical_after=after["electrical"], environment_after=after["environment"],
                              finished_s=self.clock()-self._epoch)
                if self.smu:
                    sample["smu_after"] = self.smu.verify()
                sample["module_readings"] = self._module_readings(sample)
                sample["valid"] = True
            except (Exception, KeyboardInterrupt) as exc:
                sample["error"] = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                self._event("raw_sample", condition_id=row["condition_id"], window=phase, sample=sample)

    def _prepare_axes(self, condition, row):
        changed = [m for m in self.plan.order if self.visits.get(m) != self.plan.axis_visit(condition, m)]
        env_changed = [m for m in changed if m in ("temperature", "magnetic")]
        optics_changed = "optics" in changed
        if env_changed or optics_changed:
            if self._electrical_begun or self.smu:
                self._suspend()
            if self._optics_active:
                self.optics.finish_point(valid=True)
                self.optics.suspend()
                self._optics_active = False
        for module in env_changed:
            index = condition["axes"][module]["index"]
            point = self.config.axis(module).points[index]
            self._event("axis_start", module=module, scope=self.plan.axis_visit(condition, module), point=asdict(point))
            with self._timed(module+"_prepare", visit=self.plan.axis_visit(condition, module)):
                self.environment.prepare_axis(module, point, self.plan.axis_visit(condition, module))
            self._event("axis_qualified", module=module, scope=self.plan.axis_visit(condition, module))
        if optics_changed:
            from .optics_points import OpticsPoint
            axis = condition["axes"]["optics"]
            grid, i = axis["grid"], axis["index"]
            prefix = self.plan.axis_visit(condition, "optics")[:-1]
            point = OpticsPoint(self.config.nkt.points[i], row["optical_state"], row["target_power_w"],
                grid["power_index"], (prefix, grid["optical_group_index"]), i)
            if self.power:
                self.power.point_deadline = (self.clock()+self.config.power.point_timeout_s
                    if row["optical_state"] == "light" and self.config.power.point_timeout_s is not None else None)
            self._retries_used = 0
            with self._timed("optical_prepare", visit=self.plan.axis_visit(condition, "optics")):
                self.optics.prepare(point)
            with self._timed("power_qualification" if self.power else "optics_qualification"):
                self.optics.qualify(lambda: self._guard(inactive=True))
            self._optics_active = True
        self.visits.update({m: self.plan.axis_visit(condition, m) for m in changed})
        return changed

    def _prepare_electrical(self, condition, row, changed):
        bias = row["requested_current_a"] if self.config.excitation == "dc_current" else None
        if not self._electrical_begun:
            with self._timed("electrical_begin"):
                state = None
                if hasattr(self.electrical, "begin"):
                    callback = (lambda: self.optics.preacquisition_check(lambda: self._guard(inactive=True))) if self.optics else None
                    begin_checked = getattr(self.electrical, "begin_after_optical", None)
                    state = begin_checked(callback) if begin_checked else self.electrical.begin()
                self._electrical_begun = True
                if state is None:
                    state = self._state()
                else:
                    self._retain_state(state)
                self._check_state(state, None, inactive=True)
        if self.optics:
            state = getattr(self.electrical, "guard", self.electrical.snapshot)()
            source = state.get("source")
            previous_bias = source["setpoint"] if source and source["output_enabled"] else None
            with self._timed("power_preacquisition" if self.power else "optics_preacquisition"):
                self.optics.preacquisition_check(lambda: self._guard(
                    bias=previous_bias, inactive=previous_bias is None and self.config.excitation == "dc_current"))
        with self._timed("electrical_prepare"):
            state = self.electrical.prepare(bias)
            self._retain_state(state if state is not None else self._state())
            self._check_state(self.record["last_confirmed_electrical_state"]["readback"], bias)
            if self.smu:
                if not self.smu.begun:
                    self.smu.begin()
                if "smu" in changed or self.smu.point is None:
                    point = self.config.smu.points[condition["axes"]["smu"]["index"]]
                    row["smu_prepared"] = self.smu.prepare(point, lambda: self._formal_guard(bias))
        last_change = getattr(self.electrical, "last_change_monotonic_s", None)
        if self.smu and "smu" in changed:
            last_change = self.clock()
        if last_change is None:
            last_change = self.optics.last_change_monotonic_s if self.optics else self._epoch
        self._settle(last_change, self.config.settle_s, row)
        if changed or not row.get("filter_transition"):
            prepare_filter = getattr(self.electrical, "prepare_condition", None)
            if prepare_filter:
                with self._timed("detector_filter_preparation"):
                    try:
                        row["filter_transition"] = prepare_filter(guard=lambda: self._formal_guard(bias))
                    except (Exception, KeyboardInterrupt):
                        detector = getattr(self.electrical, "detector", None)
                        row["filter_transition"] = deepcopy(getattr(detector, "filter_transition", None))
                        raise
        # Both shared acquisition windows begin with a fresh PM bracket after
        # verification, warm-up and settling. That exact boundary supplies the
        # final power evidence; do not duplicate it here.
        return bias

    def _formal_guard(self, bias):
        if self.optics:
            self.optics.verify("condition_transition_guard")
        return self._guard(bias)

    def _finish_result(self, row):
        samples = [s for w in row["windows"] for s in w["samples"]]
        row["actual"] = samples[-1]["module_readings"]
        import json
        key = json.dumps(row["baseline_key"], sort_keys=True)
        if row["optical_state"] == "dark":
            self.baselines[key] = row
        if self.config.quantity is None:
            row["result"] = {"method": "module_readbacks_v1", "quantity": None}
        elif row["optical_state"] == "dark":
            row["result"] = {"method": "single_dark_reference_v1", "quantity": self.config.quantity,
                             "dark_mean": fmean(s[self.config.quantity] for s in samples)}
        elif self.config.shared_dark:
            if key not in self.baselines:
                raise NktError("No matching dark reference for this module coordinate and visit")
            row["result"] = single_dark_difference(self.baselines[key], row, self.config.quantity)
        else:
            row["result"] = {"method": "detector_reading_v1", "quantity": self.config.quantity,
                             "detector_mean": fmean(s[self.config.quantity] for s in samples)}
        values = {}
        for sample in samples:
            for reading in sample["module_readings"]:
                for name, value in reading["measurements"].items():
                    if type(value) in (int, float):
                        values.setdefault(reading["module"], {}).setdefault(name, []).append(value)
        means = {module: {name: fmean(entries) for name, entries in fields.items()}
                 for module, fields in values.items()}
        row["result"]["module_means"] = means
        if row["optical_state"] == "light" and self.config.shared_dark:
            if key not in self.baselines:
                raise NktError("No matching dark module readback for this visit")
            baseline = self.baselines[key]
            dark = baseline["result"]["module_means"]
            # Only genuine module measurements participate: source settings and
            # missing PM dark readings never become fabricated photo-current/W.
            row["result"]["module_delta"] = {module: {name: value-dark[module][name]
                for name, value in fields.items() if name in dark.get(module, {})}
                for module, fields in means.items() if module in dark}
            row["result"]["dark_condition_id"] = baseline["condition_id"]
            dark_samples = [s for w in baseline["windows"] for s in w["samples"]]
            dark_time = fmean((s["started_s"]+s["finished_s"])/2 for s in dark_samples)
            light_time = fmean((s["started_s"]+s["finished_s"])/2 for s in samples)
            if light_time <= dark_time:
                raise NktError("Light module readbacks must follow their dark reference")
            row["result"]["baseline_age_s"] = light_time-dark_time
        if self.power and row["optical_state"] == "light":
            powers = [s[k]["power_w"] for s in samples for k in ("power_before", "power_after")]
            row["result"].update(measured_power_w=fmean(powers), target_power_w=row["target_power_w"],
                measurement_plane=self.power.config.meter.measurement_plane,
                power_peak_to_peak_w=self.power.row["formal_peak_to_peak_w"],
                qualification_drift_w_per_s=self.power.row["qualification_result"]["drift_w_per_s"],
                power_attempt_count=len(row["power_attempts"]), power_window_valid=True)

    def _acquire(self, condition, row, changed):
        while True:
            attempt = {"point_attempt": self._retries_used+1, "valid": False,
                       "started_monotonic_s": self.clock(), "windows": []}
            row["power_attempts"].append(attempt)
            self._event("power_attempt_start", condition_id=row["condition_id"],
                point_attempt=attempt["point_attempt"], max_attempts=1+self.config.power.max_retries if self.power else 1)
            try:
                if self.power:
                    self.power.check_point_deadline()
                bias = self._prepare_electrical(condition, row, changed)
                with self._timed("formal_acquisition", condition_id=row["condition_id"]):
                    if self.config.detector_selected:
                        self._window(row, row["optical_state"], bias, manage_optics=False, prepare_detector=False)
                    else:
                        self._readback_window(row, row["optical_state"], bias)
                if self.power:
                    self.power.check_point_deadline()
                attempt.update(valid=True, finished_monotonic_s=self.clock())
                self._finish_result(row)
                return
            except PowerDeviation as exc:
                attempt.update(error=f"{type(exc).__name__}: {exc}", windows=row["windows"][:],
                               finished_monotonic_s=self.clock())
                for window in row["windows"]:
                    window["valid"] = False
                    for sample in window["samples"]:
                        sample["valid_before_window_rejection"] = sample["valid"]
                        sample.update(valid=False, accepted=False, rejection="whole power window rejected")
                row["windows"] = []
                if self.record["cleanup"]["errors"] or any(s.get("audit_error") for w in attempt["windows"] for s in w["samples"]):
                    raise NktError("Cannot retry after an audit failure") from exc
                self._suspend()
                self.optics.suspend()
                attempt["cleanup"] = {"laser_off_confirmed": True, "electrical_safe_confirmed": True}
                self._event("power_attempt_rejected", condition_id=row["condition_id"], attempt=attempt,
                            point_attempt=self._retries_used+1, reason=str(exc))
                if row["optical_state"] != "light" or self._retries_used >= self.config.power.max_retries:
                    raise PowerRecoveryExhausted(f"Power recovery attempts exhausted: {exc}") from exc
                self.power.check_point_deadline()
                self._retries_used += 1
                self._event("power_retry", condition_id=row["condition_id"], point_attempt=self._retries_used+1,
                            reason=str(exc), cleanup=attempt["cleanup"])
                self.optics.qualify(lambda: self._guard(inactive=True))
                changed = list(self.plan.order)  # Fresh setup/filter evidence after recovery.
            finally:
                if "filter_transition" in row:
                    # Keep this attempt's history even when a later retry
                    # replaces the current condition's preparation evidence.
                    attempt["filter_transition"] = deepcopy(row["filter_transition"])

    def run(self):
        if self._used:
            raise NktError("Combination sessions are single-use")
        self._used, self._epoch = True, self.clock()
        try:
            self._event("run_start", measurement_config=self.config.snapshot())
            if hasattr(self.electrical, "preflight"):
                self.electrical.preflight()
            if self.electrical.is_hardware:
                self._guard(inactive=True)
            else:
                self._check_state(self._state(), None, inactive=True)
            if self.optics:
                self.optics.open(authorize_writes=self.config.hardware_mode, confirm_manual_route=self.config.hardware_mode)
            if self.environment:
                self.environment.begin()
            for condition in self.plan.conditions():
                row = self._condition(condition)
                changed = self._prepare_axes(condition, row)
                row["power"] = self.power.row if self.power else {}
                self._acquire(condition, row, changed)
                for reading in row["actual"]:
                    self._event("module_readback", module=reading["module"], reading=reading)
                self._event("condition_complete", condition=row)
            if self._optics_active:
                self.optics.finish_point(valid=True)
            self.record["outcome"] = "completed"
        except (Exception, KeyboardInterrupt) as exc:
            self.record.update(outcome="interrupted" if isinstance(exc, KeyboardInterrupt) else "rejected",
                               error=f"{type(exc).__name__}: {exc}")
            self.record["electrical_failure_evidence"] = {
                role: {"partial_raw": deepcopy(getattr(device, "partial_raw", {})),
                       "last_confirmed_state": deepcopy(getattr(device, "last_confirmed_state", None)),
                       "state_current": bool(getattr(device, "state_current", False)),
                       "filter_transition": deepcopy(getattr(device, "filter_transition", None))}
                for role in ("source", "detector", "relay_host")
                if (device := getattr(self.electrical, role, None)) is not None}
            try:
                self._event("run_error", error=self.record["error"])
            except (Exception, KeyboardInterrupt) as audit_exc:
                self.record["cleanup"]["errors"].append("audit: " + str(audit_exc))
        finally:
            self._cleanup_modules()
        return self.record

    def _cleanup_modules(self):
        cleanup = self.record["cleanup"]
        def report(phase, **values):
            try:
                self._event(phase, **values)
            except (Exception, KeyboardInterrupt) as exc:
                cleanup["errors"].append("audit cleanup: " + str(exc))
        report("cleanup_start")
        # Every cleanup is independent, and a failed observation remains sticky.
        if self.optics:
            try:
                result = self.optics.cleanup()
                cleanup["optics"] = result
                cleanup["laser_off_confirmed"] = result["off_confirmed"] is True
                cleanup["errors"].extend("optics: "+e for e in result["errors"])
            except (Exception, KeyboardInterrupt) as exc:
                cleanup["errors"].append("optics: " + str(exc))
        try:
            self.record["electrical_state_current"] = False
            state = self.electrical.cleanup()
            self._retain_state(state)
            self._check_state(state, None, inactive=True)
            cleanup["electrical"] = deepcopy(state)
            cleanup["electrical_safe_confirmed"] = True
        except (Exception, KeyboardInterrupt) as exc:
            cleanup["errors"].append("electrical: " + str(exc))
        if self.smu:
            try:
                result = self.smu.finish(failed=self.record["outcome"] != "completed")
                cleanup["smu"] = result
                cleanup["smu_safe_confirmed"] = not result.get("errors")
                cleanup["errors"].extend("smu: "+e for e in result.get("errors", []))
            except (Exception, KeyboardInterrupt) as exc:
                cleanup["errors"].append("smu: " + str(exc))
        if self.environment:
            try:
                result = self.environment.finish(failed=self.record["outcome"] != "completed" or bool(cleanup["errors"]))
                self.record["environment"] = result
                cleanup["environment"] = result
                cleanup["errors"].extend("environment: "+e for e in result["errors"])
            except (Exception, KeyboardInterrupt) as exc:
                cleanup["environment"] = {"last_confirmed_state": deepcopy(self.environment.last_confirmed_state),
                    "state_current": False, "manual_verification_required": True}
                cleanup["errors"].append("environment: " + str(exc))
        if not cleanup["laser_off_confirmed"] or not cleanup["electrical_safe_confirmed"] or cleanup["errors"]:
            self.record["outcome"] = "rejected"
        report("cleanup_complete", cleanup=cleanup)
        if cleanup["errors"]:
            self.record["outcome"] = "rejected"
        self.record["completed"] = self.record["outcome"] == "completed"
        if self.optics:
            self.record["optics_audit"] = deepcopy(self.optics.record)
            self.record["laser_audit"] = deepcopy(self.optics.laser.record)
        for row in self.record["conditions"]:
            row["accepted"] = self.record["completed"] and row["result"] is not None
            for window in row["windows"]:
                for sample in window["samples"]:
                    sample["accepted"] = row["accepted"] and sample["valid"]


class _AuditStack(ExitStack):
    """A transport-close error must reach the saved record, not erase it."""
    def __init__(self, errors):
        super().__init__()
        self.errors = errors

    def __exit__(self, *details):
        try:
            return super().__exit__(*details)
        except (Exception, KeyboardInterrupt) as exc:
            self.errors.append(f"resource close: {type(exc).__name__}: {exc}")
            return False


def run_modular(config, event=None, attempt_index=1):
    """Own all selected resources; factories are reached only after validation."""
    config.validate()
    hardware = config.hardware_mode
    if hardware:
        config.require_run()
    record, runner, close_errors = None, None, []
    with _AuditStack(close_errors) as stack:
        try:
            optics = None
            if config.modular.optics:
                from .nkt_control import NktController, SimulatedNkt
                from .photoresponse_cld import CldConfig, CldController, SimulatedCld, TL4000, VisaCld
                backend_registered = False
                if isinstance(config.nkt, CldConfig):
                    backend = (VisaCld if config.nkt.backend == "visa" else TL4000)(config.nkt,
                        authorize_connection=True, authorize_writes=True, on_event=event) if hardware else SimulatedCld(config.nkt)
                    laser = CldController(config.nkt, backend, on_event=event)
                else:
                    if hardware:
                        from .nkt_sdk import NktSdk
                        backend = NktSdk(config.nkt, authorize_writes=True)
                        stack.callback(backend.close)
                        backend_registered = True
                        backend.open(authorize_connection=True)
                    else:
                        backend = SimulatedNkt(config.nkt)
                    laser = NktController(config.nkt, backend, on_event=event)
                if not backend_registered:
                    stack.callback(backend.close)
                from .photoresponse_power import open_photoresponse_power
                power = open_photoresponse_power(config.power, laser, on_event=event,
                    authorize_connection=hardware, authorize_writes=hardware) if config.power else None
                if power:
                    stack.callback(lambda: power.close() if not power.meter.closed else None)
                pem, waves = None, None
                if config.modular.optics["use_pem"]:
                    from .pem_config import load_pem_config
                    from .pem import Pem, SerialTransport, SimulatedPemTransport
                    pc = load_pem_config(config.modular.document)
                    transport = SerialTransport(pc, authorize_connection=True, authorize_writes=True) if hardware else SimulatedPemTransport()
                    pem = Pem(pc, transport, is_hardware=hardware, authorize_writes=hardware)
                    stack.callback(pem.close)
                    waves = config.modular.optics["peak_retardance_waves"]
                from .optics_points import OpticsSession
                optics = OpticsSession(laser, power=power, pem=pem, peak_retardance_waves=waves,
                    settle_s=config.settle_s, dark_recovery_s=config.dark_recovery_s, on_event=event)
            if hardware and config.hardware:
                from .photoresponse_transport import visa_resources
                resources = stack.enter_context(visa_resources(config.hardware.addresses(),
                    timeout_ms=config.hardware.timeout_ms, authorize_connection=True, authorize_writes=True, on_event=event))
                if config.detector_selected:
                    from .photoresponse_hardware import HardwareElectrical
                    electrical = HardwareElectrical(config, resources)
                else:
                    electrical = SourceOnlyElectrical(config, resources)
            else:
                electrical = SimulatedElectrical(config)
                if hardware:
                    # No selected electrical hardware: this inert object owns no resource.
                    electrical.is_hardware = True
                    electrical.state["detector"] = config.detector
                stack.callback(electrical.close)
            from .modular_environment import EnvironmentAxes
            environment = EnvironmentAxes(config.environment, on_event=event, authorize_hardware=hardware) if config.environment else None
            if environment:
                stack.callback(environment.driver.close)
            from .photoresponse_smu import PhotoresponseSmu
            smu = PhotoresponseSmu(config, simulated=not hardware, on_event=event, authorize_writes=hardware) if config.smu else None
            if smu:
                stack.callback(smu.close)
            runner = ModularRunner(config, electrical, optics=optics, environment=environment, smu=smu,
                on_event=event, attempt_index=attempt_index)
            record = runner.run()
        except (Exception, KeyboardInterrupt) as exc:
            if record is None and runner is not None:
                record = runner.record
                record.update(completed=False, outcome="rejected")
                record["error"] = record.get("error") or f"{type(exc).__name__}: {exc}"
                record["cleanup"]["errors"].append("session finalization: " + str(exc))
                for row in record["conditions"]:
                    row["accepted"] = False
                    for window in row["windows"]:
                        for sample in window["samples"]:
                            sample["accepted"] = False
            elif record is None:
                # Partial factory ownership is not proof that outputs were shut down.
                record = {"schema_version": 1, "measurement_type": "dc_photoresponse",
                    "record_contract": "modular-combination-v1", "simulated": not hardware,
                    "captured_at_utc": utc_now(), "measurement_config": config.snapshot(),
                    "conditions": [], "completed": False, "outcome": "rejected",
                    "error": f"{type(exc).__name__}: {exc}", "cleanup": {
                        "laser_off_confirmed": False, "electrical_safe_confirmed": False,
                        "errors": ["Session construction failed; output state is not confirmed"]}}
    if close_errors:
        record["cleanup"]["errors"].extend(close_errors)
        record.update(completed=False, outcome="rejected")
        for row in record.get("conditions", []):
            row["accepted"] = False
            for window in row["windows"]:
                for sample in window["samples"]:
                    sample["accepted"] = False
    return record
