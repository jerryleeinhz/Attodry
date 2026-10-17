"""Single-owner optical scan, direct or bounded power feedback; no hardware on import."""
from __future__ import annotations

from dataclasses import asdict, replace
from copy import deepcopy
import math
import time

from .nkt_config import NktError
from .nkt_control import NktController, utc_now
from .power_feedback import (PowerWindow, adaptive_feedback_step, next_cld_current_a,
                             next_nd_pct, next_source_current_pct, next_source_power_pct,
                             target_in_tolerance, tuning_in_tolerance)


class OpticalScan:
    def __init__(self, nkt_config, config, backend, *, pem=None, meter=None,
                 clock=time.monotonic, sleep=time.sleep, on_event=None, controller=None):
        config.validate(nkt_config, pem.config if pem else None, meter.config if meter else None)
        self.config, self.nkt_config = config, nkt_config
        self.pem, self.meter = pem, meter
        self.clock, self.sleep, self.on_event = clock, sleep, on_event
        self.fixed_feedback_settings = None
        self.nkt = controller or NktController(nkt_config, backend, clock=clock, sleep=sleep, on_event=self._nkt_event)
        self._response_history_key, self._response_history = None, []
        self.record = {"schema_version": 1, "captured_at_utc": utc_now(), "outcome": "rejected",
                       "completed": False, "simulated": not any(x.is_hardware for x in
                           (backend, pem, meter) if x is not None),
                       "measurement_config": {"nkt": nkt_config.snapshot(), "scan": asdict(config),
                            "pem": asdict(pem.config) if pem else None,
                            "pm100d": asdict(meter.config) if meter else None},
                       "points": [], "events": [], "error": None, "cleanup": {},
                       "last_confirmed_state": {}, "state_current": False}

    def _nkt_event(self, value):
        self._event("nkt", evidence=value)

    def _event(self, phase, **values):
        event = {"phase": phase, "captured_at_utc": utc_now(),
                 "monotonic_s": self.clock(), **deepcopy(values)}
        self.record["events"].append(event)
        if self.on_event:
            self.on_event(event)

    def _deadline(self, deadline):
        if deadline is not None and self.clock() >= deadline:
            raise NktError("Optical point total timeout (includes all preparation and tuning)")

    def _verify(self, point, wavelength, emitting, deadline, *, verify_meter=True):
        self._deadline(deadline)
        self.record["state_current"] = False
        state = {"nkt": self.nkt.verify_point(point, emitting=emitting),
                 "pem": self.pem.verify_ready() if self.pem else None,
                 "pm100d": self.meter.verify_ready(wavelength) if self.meter and verify_meter else None}
        # The other instrument queries take time; recheck NKT before accepting
        # the combined evidence or crossing the emission barrier.
        state["nkt"] = self.nkt.verify_point(point, emitting=emitting)
        if self.fixed_feedback_settings is not None:
            source, filt = state["nkt"]["source"], state["nkt"]["filter"]
            if (source["pulse_picker_ratio"] != self.fixed_feedback_settings["pulse_picker_ratio"]
                    or not math.isclose(filt["nd_pct"], self.fixed_feedback_settings["nd_pct"],
                                        abs_tol=0.051, rel_tol=0)):
                raise NktError("Source-current feedback requires unchanged preflight ND and pulse picker")
        self._deadline(deadline)
        self.record["last_confirmed_state"] = state
        self.record["state_current"] = True
        return state

    def _sample(self, row, point, wavelength, deadline, phase):
        # read_sample itself performs full PM settings/status checks on both
        # sides of this READ. Keep independent laser/PEM guards, without an
        # extra outer PM capture or any cross-sample settings cache.
        state = self._verify(point, wavelength, self.nkt_config.emit, deadline, verify_meter=False)
        self.record["state_current"] = False
        sample = self.meter.read_sample(wavelength)
        sample.update(condition_id=row["condition_id"], attempt_index=1, accepted=False, phase=phase,
                      nd_pct=getattr(point, "nd_pct", None),
                      requested_source_level_pct=getattr(point, "source_level_pct", None),
                      source_current_readback_pct=(state["nkt"]["source"]["level_pct"]
                          if getattr(self.nkt_config, "source_control", None) == "current" else None))
        if hasattr(point, "current_a"):
            sample.update(requested_current_a=point.current_a,
                          current_readback_a=state["nkt"]["source"]["getLdCurrSetpoint"])
        feedback = row["feedback_config"]
        sample["above_reduce_threshold"] = bool(feedback and feedback.get("reduce_above_power_w") is not None
                                                 and sample["power_w"] > feedback["reduce_above_power_w"])
        # Keep raw acquisition evidence even if a later device/state check fails.
        row["power_samples"].append(sample)
        self._event("power_sample", sample=sample.copy())
        self._deadline(deadline)
        if feedback:
            if sample["power_w"] > feedback["max_power_w"]:
                raise NktError("Power exceeds feedback limit; no corrective retry")
            if (feedback["actuator"] in ("source_current", "source_power", "cld_current")
                    and sample["power_w"] < feedback["minimum_signal_power_w"]):
                raise NktError("Power below minimum valid illumination; do not increase current to search for light")
        if sample["above_reduce_threshold"]:
            # Return directly to the reduction path; do not wait for a stable
            # window or perform another joint-state verification. The meter's
            # own post-READ status checks have already completed.
            return sample
        state = self._verify(point, wavelength, self.nkt_config.emit, deadline, verify_meter=False)
        state["pm100d"] = deepcopy(sample["after"])
        state["pm_sample_sequence"] = sample["sequence"]
        self.record["last_confirmed_state"] = state
        return sample

    def _stabilize(self, row, point, wavelength, deadline, cfg, *, initial_sample=None):
        try:
            return self._stabilize_point(row, point, wavelength, deadline, cfg, initial_sample=initial_sample)
        except BaseException:
            self.reset_feedback_history()
            raise

    def reset_feedback_history(self):
        """Invalid/unknown optical state cannot provide a local response model."""
        self._response_history_key, self._response_history = None, []

    def _stabilize_point(self, row, point, wavelength, deadline, cfg, *, initial_sample=None):
        epoch = initial_sample["started_monotonic_s"] if initial_sample else self.clock()
        window = PowerWindow(cfg.window, epoch_s=epoch, max_power_w=cfg.max_power_w,
                             target_power_w=cfg.target_power_w)
        stable_since = None
        previous_sign = None
        source_percentage = cfg.actuator in ("source_current", "source_power")
        cld_current = cfg.actuator == "cld_current"
        step = (cfg.source_bounds()[2] if source_percentage else
                cfg.current_step_a if cld_current else cfg.nd_step_pct)
        history_key = (wavelength, getattr(point, "bandwidth_nm", None), cfg.actuator,
                       getattr(point, "pulse_picker_ratio", None), getattr(point, "nd_pct", None),
                       tuple(sorted((self.fixed_feedback_settings or {}).items())))
        if history_key != self._response_history_key:
            self._response_history = []
        self._response_history_key = history_key
        while True:
            sample = initial_sample if initial_sample is not None else self._sample(row, point, wavelength, deadline, "tuning")
            initial_sample = None
            result = window.add(sample)
            reduce_now = sample["above_reduce_threshold"]
            target = target_in_tolerance(result["mean_power_w"], cfg)
            tuning_target = (all(tuning_in_tolerance(s["power_w"], cfg) for s in window.samples)
                             if cfg.tuning_tolerance_fraction is not None else target)
            result.update(target_in_tolerance=target, target_power_w=cfg.target_power_w,
                          target_tolerance_w=cfg.target_tolerance_w,
                          target_in_tuning_tolerance=tuning_target, tuning_tolerance_w=cfg.tuning_tolerance_w(),
                          nd_pct=getattr(point, "nd_pct", None), actuator=cfg.actuator,
                          above_reduce_threshold=reduce_now,
                          source_current_pct=point.source_level_pct if cfg.actuator == "source_current" else None,
                          source_power_pct=point.source_level_pct if cfg.actuator == "source_power" else None,
                          current_a=point.current_a if cld_current else None)
            row["feedback_windows"].append(result)
            self._event("feedback_window", condition_id=row["condition_id"], result=result)
            if result["power_window_stable"] and tuning_target and not reduce_now:
                if stable_since is None:
                    stable_since = self.clock()
                if self.clock() - stable_since >= cfg.hold_s - 1e-9:
                    if cfg.actuator == "source_current" and cfg.tuning_tolerance_fraction is not None:
                        self._response_history.append((point.source_level_pct, result["mean_power_w"]))
                        del self._response_history[:-16]
                    return point, result, window
            else:
                stable_since = None
            if (result["power_window_stable"] and
                    not tuning_in_tolerance(result["mean_power_w"], cfg)) or reduce_now:
                adjustment_power = sample["power_w"] if reduce_now else result["mean_power_w"]
                sign = adjustment_power < cfg.target_power_w
                minimum_step = (min(step, max(getattr(self.nkt_config, "current_tolerance_a", 0) * 2, 1e-12))
                                if cld_current else 0.1)
                reversed_direction = previous_sign is not None and sign != previous_sign
                if reversed_direction:
                    step = max(minimum_step, step / 2)
                    if not cld_current:
                        step = round(step, 1)
                previous_sign = sign
                current = (point.source_level_pct if source_percentage else
                           point.current_a if cld_current else point.nd_pct)
                effective_step, step_evidence = adaptive_feedback_step(
                    current, adjustment_power, cfg, step, self._response_history, minimum_step=minimum_step)
                if cfg.actuator == "source_current" and cfg.tuning_tolerance_fraction is not None:
                    self._response_history.append((current, adjustment_power))
                    del self._response_history[:-16]
                tuning = replace(cfg, target_tolerance_w=cfg.tuning_tolerance_w())
                if source_percentage:
                    next_value = next_source_current_pct if cfg.actuator == "source_current" else next_source_power_pct
                    revised = replace(point, source_level_pct=next_value(
                        point.source_level_pct, adjustment_power, tuning, step_pct=effective_step))
                    label = "source_current" if cfg.actuator == "source_current" else "source_power"
                    row.setdefault(label + "_iterations", []).append({"from_" + label + "_pct": point.source_level_pct,
                        "to_" + label + "_pct": revised.source_level_pct, "window": result,
                        "direction_reversed": reversed_direction, "step_evidence": step_evidence})
                    self._event(label + "_revision", condition_id=row["condition_id"], effective=asdict(revised))
                elif cld_current:
                    revised = replace(point, current_a=next_cld_current_a(
                        point.current_a, adjustment_power, tuning, step_a=effective_step))
                    row.setdefault("cld_current_iterations", []).append({"from_current_a": point.current_a,
                        "to_current_a": revised.current_a, "window": result,
                        "direction_reversed": reversed_direction, "step_evidence": step_evidence})
                    self._event("cld_current_revision", condition_id=row["condition_id"], effective=asdict(revised))
                else:
                    revised = replace(point, nd_pct=next_nd_pct(point.nd_pct, adjustment_power, tuning,
                                                               step_pct=effective_step))
                    row["nd_iterations"].append({"from_nd_pct": point.nd_pct, "to_nd_pct": revised.nd_pct,
                                                 "window": result, "direction_reversed": reversed_direction,
                                                 "step_evidence": step_evidence})
                    self._event("nd_revision", condition_id=row["condition_id"], effective=asdict(revised))
                self.nkt.turn_off(deadline=deadline)
                self._verify(point, wavelength, False, deadline)
                self.nkt.prepare_point(revised, deadline=deadline)
                point = revised
                row["effective_requested"] = asdict(point)
                self._verify(point, wavelength, False, deadline)
                self.nkt.start_emission(point, deadline=deadline)
                self._verify(point, wavelength, True, deadline)
                window = PowerWindow(cfg.window, epoch_s=self.clock(), max_power_w=cfg.max_power_w,
                                     target_power_w=cfg.target_power_w)
                stable_since = None
            self._deadline(deadline)
            self.sleep(min(cfg.window.sample_interval_s, deadline - self.clock()))

    def run(self, *, authorize_writes=False, confirm_manual_route=False):
        self.config.require_standalone_scan()
        # All enabled hardware permissions are checked before any device query.
        if self.nkt.backend.is_hardware:
            self.nkt_config.require_hardware(writes=True)
            if not authorize_writes or not confirm_manual_route:
                raise NktError("Joint hardware writes/manual route not authorized")
        for device in (self.pem, self.meter):
            if device and device.is_hardware:
                device.config.require_hardware(writes=True)
                authorized = device.authorize_writes if device is self.pem else (
                    device.authorize_settings and device.authorize_measurement)
                if not authorize_writes or not authorized:
                    raise NktError("Enabled optical device permissions missing")
        try:
            source, filt = self.nkt.begin_session(authorize_writes=authorize_writes, confirm_manual_route=confirm_manual_route)
            if self.config.feedback and self.config.feedback.actuator in ("source_current", "source_power"):
                self.fixed_feedback_settings = {"nd_pct": filt["nd_pct"],
                                                "pulse_picker_ratio": source["pulse_picker_ratio"]}
                self.record["feedback_fixed_settings"] = dict(self.fixed_feedback_settings)
            if self.pem:
                self.pem.preflight()
            self.nkt.configure_session()
            for i, requested in enumerate(self.nkt_config.points):
                feedback = (self.config.feedback.for_point(i, len(self.nkt_config.points))
                            if self.config.feedback else None)
                point_start = self.clock()
                deadline = point_start + feedback.timeout_s if feedback else None
                row = {"condition_id": f"optical-{i:06d}", "attempt_index": 1, "accepted": False,
                       "requested": asdict(requested), "effective_requested": asdict(requested),
                       "started_at_utc": utc_now(), "power_samples": [], "nd_iterations": [],
                       "source_current_iterations": [], "feedback_actuator": feedback.actuator if feedback else None,
                       "feedback_windows": [], "measured_power_w": None,
                       "target_power_w": feedback.target_power_w if feedback else None,
                       "target_tolerance_w": feedback.target_tolerance_w if feedback else None,
                       "feedback_config": asdict(feedback) if feedback else None,
                       "measurement_plane": self.meter.config.measurement_plane if self.meter else None}
                self.record["points"].append(row)
                readback = self.nkt.prepare_point(requested, deadline=deadline)
                filt = readback["filter"]
                wavelength = ((filt["lower_edge_nm"] + filt["upper_edge_nm"]) / 2
                              if getattr(self.nkt_config, "mode", None) == "varia_bandpass" else requested.wavelength_nm)
                row["center_setpoint_nm"] = wavelength
                self._deadline(deadline)
                if self.pem:
                    row["pem_preparation"] = self.pem.prepare(wavelength, self.config.peak_retardance_waves,
                                                              deadline=deadline)
                self._deadline(deadline)
                if self.meter:
                    row["pm_preparation"] = self.meter.prepare(wavelength)
                self._verify(requested, wavelength, False, deadline)
                self._event("prepared", condition_id=row["condition_id"])
                if self.nkt_config.emit:
                    self.nkt.start_emission(requested, deadline=deadline)
                point = requested
                window = None
                if feedback:
                    point, result, window = self._stabilize(row, point, wavelength, deadline, feedback)
                    row["feedback_result"] = result
                # Formal dwell starts only after feedback success. On drift,
                # prior formal samples become transition evidence and dwell restarts.
                until = self.clock() + self.nkt_config.dwell_s
                formal_start = len(row["power_samples"])
                while True:
                    if self.meter:
                        sample = self._sample(row, point, wavelength, deadline, "formal")
                        row["measured_power_w"] = sample["power_w"]
                        if window:
                            result = window.add(sample)
                            if (sample["above_reduce_threshold"] or not result["power_window_stable"] or
                                    not target_in_tolerance(result["mean_power_w"], feedback)):
                                for old in row["power_samples"][formal_start:]:
                                    old["phase"] = "rejected_formal_drift"
                                point, result, window = self._stabilize(row, point, wavelength, deadline, feedback,
                                                                       initial_sample=sample)
                                row["feedback_result"] = result
                                until = self.clock() + self.nkt_config.dwell_s
                                formal_start = len(row["power_samples"])
                                continue
                    row["readback"] = self._verify(point, wavelength, self.nkt_config.emit, deadline)
                    if self.clock() >= until:
                        break
                    self.sleep(min(self.config.sample_interval_s, until - self.clock()))
                row["effective_requested"] = asdict(point)
                row["finished_at_utc"] = utc_now()
                self._event("point_complete", point=row)
            self.record["outcome"] = "completed"
        except (Exception, KeyboardInterrupt) as exc:
            self.record["error"] = f"{type(exc).__name__}: {exc}"
            self.record["outcome"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "rejected"
            self.record["state_current"] = False
        finally:
            self.record["cleanup"]["nkt"] = self.nkt.finish_session()
            if self.pem:
                self.record["cleanup"]["pem"] = self.pem.finish()
            if self.meter:
                errors = []
                try:
                    self.meter.close()
                except (Exception, KeyboardInterrupt) as exc:
                    errors.append(f"{type(exc).__name__}: {exc}")
                self.record["cleanup"]["pm100d"] = {"errors": errors}
            clean = self.record["cleanup"]["nkt"]["off_confirmed"] and not any(
                item["errors"] for item in self.record["cleanup"].values())
            if not clean:
                self.record["outcome"] = "rejected"
            self.record["completed"] = self.record["outcome"] == "completed"
            for row in self.record["points"]:
                row["accepted"] = self.record["completed"]
                for sample in row["power_samples"]:
                    sample["accepted"] = self.record["completed"] and sample["phase"] == "formal"
            self.record["state_current"] = False
            self.record["nkt_last_confirmed_state"] = self.nkt.record["last_confirmed_state"]
            for name, device in (("pem", self.pem), ("pm100d", self.meter)):
                if device:
                    self.record[f"{name}_transcript"] = device.transcript
                    self.record[f"{name}_last_confirmed_state"] = device.last_confirmed_state
            self._event("run_end", outcome=self.record["outcome"], cleanup=self.record["cleanup"])
        return self.record


def accepted_points(record):
    """Default offline selection; never imports or opens a hardware backend."""
    if not record.get("completed") or record.get("outcome") != "completed":
        return []
    cleanup = record.get("cleanup", {})
    if not cleanup.get("nkt", {}).get("off_confirmed") or any(item.get("errors") for item in cleanup.values()):
        return []
    return [row for row in record.get("points", []) if row.get("accepted") is True]
