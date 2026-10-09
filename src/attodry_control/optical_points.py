"""Single-owner optical point lifecycle for the combination coordinator.

Construction and configuration loading perform no instrument I/O. The caller
preflights all modules before calling configure, prepares every axis before
qualify, and brackets sequential electrical reads with begin/end_sample.
Existing OpticalScan's verified preparation/feedback primitives are reused;
its independent run loop is never invoked.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, replace
import time
import statistics

from .nkt_config import NktError, load_nkt_config, number, tenths
from .nkt_control import SimulatedNkt, utc_now
from .optical_config import load_scan_config
from .optical_scan import OpticalScan
from .power_feedback import target_in_tolerance


@dataclass(frozen=True)
class OpticalPointConfig:
    nkt: object
    scan: object
    pem: object | None = None
    pm: object | None = None

    def validate(self):
        self.scan.validate(self.nkt, self.pem, self.pm)
        if self.pem:
            self.pem.validate()
        simulation = self.nkt.backend == "simulation"
        if any((cfg.backend == "simulation") != simulation
               for cfg in (self.pem, self.pm) if cfg is not None):
            raise NktError("Combination optical devices must use one hardware/simulation mode")
        keys = [set(point) for point in self.points]
        if any(value != keys[0] for value in keys):
            raise NktError("Optical points must keep identical coordinate keys")
        if self.pem and self.nkt.backend != "simulation" and self.pem.port.upper() == self.nkt.port.upper():
            raise NktError("PEM and NKT cannot own the same serial port")

    @property
    def nkt_points(self):
        feedback = self.scan.feedback
        factor = feedback.targets_per_optical_point if feedback else 1
        if len(self.nkt.points) * factor > 10000:
            raise NktError("Expanded optical point count exceeds 10000")
        points = tuple(point for point in self.nkt.points for _ in range(factor))
        if feedback and feedback.initial_current_mode in ("per_target", "grid"):
            return tuple(replace(point, source_level_pct=feedback.initial_source_level_pct(
                index, len(points), point.source_level_pct)) for index, point in enumerate(points))
        return points

    @property
    def points(self):
        result = []
        nkt_points = self.nkt_points
        for index, point in enumerate(nkt_points):
            values = {"optical_source_level_pct": point.source_level_pct}
            for field in ("wavelength_nm", "bandwidth_nm", "nd_pct", "pulse_picker_ratio"):
                value = getattr(point, field)
                if value is not None:
                    values["optical_" + field] = value
            if self.scan.feedback:
                feedback = self.scan.feedback.for_point(index, len(nkt_points))
                values["optical_target_power_w"] = feedback.target_power_w
            result.append(values)
        return tuple(result)

    def snapshot(self):
        self.validate()
        return {"nkt": self.nkt.snapshot(), "scan": asdict(self.scan),
                "pem": asdict(self.pem) if self.pem else None,
                "pm100d": asdict(self.pm) if self.pm else None,
                "formal_power_evidence": "before/during/after sequential electrical reads",
                "point_grid": {"target_mapping": self.scan.feedback.target_mapping if self.scan.feedback else None,
                               "initial_current_mode": self.scan.feedback.initial_current_mode if self.scan.feedback else None,
                               "input_point_count": len(self.nkt.points), "expanded_point_count": len(self.nkt_points),
                               "order": "optical_row_then_target_power"},
                "sampling_owner": "combination_scan.samples_per_condition"}


def load_optical_point_config(path):
    nkt = load_nkt_config(path)
    scan, pem, pm = load_scan_config(path, nkt)
    result = OpticalPointConfig(nkt, scan, pem, pm)
    result.validate()
    return result


class OpticalPointSession:
    """Factories receive one validated config and return an owned resource.

    Injected factories must not conceal real resources behind simulation flags.
    Factory defaults open real resources only after all stage checks succeed.
    ``qualify`` is the only emission barrier; ``set_point`` always leaves OFF.
    """

    def __init__(self, config, event_sink=None, *, backend_factory=None,
                 pem_factory=None, meter_factory=None, clock=time.monotonic, sleep=time.sleep):
        config.validate()
        self.config, self.event_sink = config, event_sink
        self.backend_factory, self.pem_factory, self.meter_factory = backend_factory, pem_factory, meter_factory
        self.clock, self.sleep = clock, sleep
        self.backend = self.pem = self.meter = self.engine = None
        self.opened = self.configured = self.qualified = self.sample_active = False
        self._opening_attempted = False
        self.index = self.point = self.row = self.wavelength = self.feedback = None
        self.deadline = None
        self.sample_evidence = []
        self.last_state = None
        self.cleanup_result = {"verified": False, "off_confirmed": False,
                               "reference_cleanup_pending": False, "meter_cleanup_pending": False,
                               "actions": {}, "errors": []}
        self._pem_finished = self._meter_closed = False
        self._previous_current = None

    def _event(self, kind, **payload):
        if self.event_sink:
            self.event_sink(kind, deepcopy(payload))

    def _engine_event(self, event):
        self._event("optical_event", evidence=event)

    def _backend(self):
        if self.backend_factory:
            return self.backend_factory(self.config.nkt)
        if self.config.nkt.backend == "simulation":
            return SimulatedNkt(self.config.nkt)
        from .nkt_sdk import NktSdk
        backend = NktSdk(self.config.nkt, authorize_writes=True)
        backend.open(authorize_connection=True)
        return backend

    def _pem(self):
        if self.pem_factory:
            return self.pem_factory(self.config.pem)
        from .pem import Pem, SerialTransport, SimulatedPemTransport
        simulation = self.config.pem.backend == "simulation"
        transport = (SimulatedPemTransport() if simulation else SerialTransport(
            self.config.pem, authorize_connection=True, authorize_writes=True))
        return Pem(self.config.pem, transport, is_hardware=not simulation, authorize_writes=True,
                   clock=self.clock, sleep=self.sleep)

    def _meter(self):
        if self.meter_factory:
            return self.meter_factory(self.config.pm)
        from .pm100d import Pm100d, SimulatedPmResource
        if self.config.pm.backend != "simulation":
            return Pm100d.open(self.config.pm, authorize_connection=True, authorize_settings=True,
                              authorize_measurement=True, clock=self.clock)

        def power():
            if self.backend.source["emission_state"] != 3:
                return 0.0
            feedback = self.config.scan.feedback
            if feedback and feedback.actuator == "source_current":
                return 2 * feedback.max_power_w * (self.backend.source["level_pct"] / self.config.nkt.max_level_pct) ** 2
            increasing = feedback.increasing_nd_increases_power if feedback else False
            return (0.002 * 10 ** ((self.backend.filter["nd_pct"] if increasing else -self.backend.filter["nd_pct"]) / 100)
                    if self.config.nkt.mode == "varia_bandpass" else 0.002)

        return Pm100d(self.config.pm, SimulatedPmResource(power), clock=self.clock)

    def open(self, *, authorize_writes=False, confirm_manual_route=False):
        if self._opening_attempted:
            raise NktError("Optical session cannot be opened twice")
        hardware = self.config.nkt.backend != "simulation"
        if hardware:
            if authorize_writes is not True or confirm_manual_route is not True:
                raise NktError("Optical connection/settings/emission and manual route require explicit authorization")
            self.config.nkt.require_hardware(writes=True)
            for cfg in (self.config.pem, self.config.pm):
                if cfg:
                    cfg.require_hardware(writes=True)
        self._opening_attempted = True
        try:
            self.backend = self._backend()
            if bool(self.backend.is_hardware) != hardware:
                raise NktError("Injected NKT backend does not match configured mode")
            if self.config.pem:
                self.pem = self._pem()
            if self.config.pm:
                self.meter = self._meter()
            for device in (self.pem, self.meter):
                if device and bool(device.is_hardware) != hardware:
                    raise NktError("Injected optical resource does not match configured mode")
            if hardware:
                if self.pem and self.pem.authorize_writes is not True:
                    raise NktError("Injected PEM lacks setting authorization")
                if self.meter and (self.meter.authorize_settings is not True or self.meter.authorize_measurement is not True):
                    raise NktError("Injected PM100D lacks settings/measurement authorization")
            self.engine = OpticalScan(self.config.nkt, self.config.scan, self.backend,
                pem=self.pem, meter=self.meter, clock=self.clock, sleep=self.sleep,
                on_event=self._engine_event)
            source, filt = self.engine.nkt.begin_session(authorize_writes=authorize_writes,
                                                        confirm_manual_route=confirm_manual_route)
            if self.config.scan.feedback and self.config.scan.feedback.actuator == "source_current":
                self.engine.fixed_feedback_settings = {"nd_pct": filt["nd_pct"],
                                                        "pulse_picker_ratio": source["pulse_picker_ratio"]}
            evidence = {"nkt": {"source": source, "filter": filt}}
            if self.pem:
                evidence["pem"] = self.pem.preflight()
            if self.meter:
                evidence["pm100d"] = {"identity": self.meter.identify(), "sensor": self.meter.read_sensor()}
            self.opened = True
            self._event("optical_preflight", evidence=evidence)
            return evidence
        except BaseException:
            # Keep resources for the coordinator's unconditional global cleanup.
            self.qualified = False
            raise

    def configure(self):
        self._require_open()
        self.configured = True
        return {"emission_started": False}

    def _require_open(self):
        if not self.opened or self.engine.nkt.session_closed:
            raise NktError("Optical point session is not open")

    def suspend(self):
        """OFF before any changed electrical/environment/optical axis; keep PEM reference."""
        self._require_open()
        if self.sample_active:
            raise NktError("Cannot change optical state inside a formal sample")
        self.qualified = False
        try:
            self.engine.nkt.turn_off()
        except BaseException:
            self._previous_current = None
            raise
        return {"off_confirmed": True, "pem_reference_preserved": self.pem is not None}

    def set_point(self, index):
        try:
            return self._prepare_point(index)
        except BaseException:
            self._previous_current = None
            self.qualified = False
            raise

    def _prepare_point(self, index):
        self._require_open()
        if not self.configured or self.sample_active:
            raise NktError("Configure first; no optical changes during formal samples")
        nkt_points = self.config.nkt_points
        if type(index) is not int or not 0 <= index < len(nkt_points):
            raise NktError("Optical point index is out of range")
        self.qualified = False
        self.index = index
        self.point = nkt_points[index]
        original_feedback = self.config.scan.feedback
        initial_current = None
        if original_feedback and original_feedback.actuator == "source_current":
            factor = original_feedback.targets_per_optical_point
            input_index, target_index = divmod(index, factor)
            mode = original_feedback.initial_current_mode
            previous = self._previous_current
            self._previous_current = None
            source = mode if mode in ("per_target", "grid") else "point"
            previous_index = None
            if (mode == "previous" and previous is not None
                    and previous["index"] + 1 == index
                    and previous["input_point_index"] == input_index):
                current = number(previous["source_level_pct"], "previous qualified source current",
                    original_feedback.source_current_min_pct, original_feedback.source_current_max_pct)
                tenths(current)
                self.point = replace(self.point, source_level_pct=current)
                source, previous_index = "previous", previous["index"]
            initial_current = {"mode": mode, "input_point_index": input_index, "target_index": target_index,
                "configured_source_level_pct": self.config.nkt.points[input_index].source_level_pct,
                "selected_source_level_pct": self.point.source_level_pct,
                "source": source, "previous_point_index": previous_index}
        self.feedback = (self.config.scan.feedback.for_point(index, len(nkt_points))
                         if self.config.scan.feedback else None)
        self.deadline = self.clock() + self.feedback.timeout_s if self.feedback else None
        self.row = {"condition_id": f"optical-{index:06d}", "attempt_index": 1, "accepted": False,
                    "requested": asdict(nkt_points[index]), "effective_requested": asdict(self.point),
                    "power_samples": [], "nd_iterations": [], "source_current_iterations": [],
                    "feedback_windows": [], "feedback_config": asdict(self.feedback) if self.feedback else None,
                    "measurement_plane": self.config.pm.measurement_plane if self.config.pm else None}
        if initial_current is not None:
            self.row["initial_current"] = initial_current
        self.engine.record["points"].append(self.row)
        readback = self.engine.nkt.prepare_point(self.point, deadline=self.deadline)
        filt = readback["filter"]
        self.wavelength = ((filt["lower_edge_nm"] + filt["upper_edge_nm"]) / 2
                           if self.config.nkt.mode == "varia_bandpass" else self.point.wavelength_nm)
        if self.pem:
            self.row["pem_preparation"] = self.pem.prepare(
                self.wavelength, self.config.scan.peak_retardance_waves, deadline=self.deadline)
        if self.meter:
            self.row["pm_preparation"] = self.meter.prepare(self.wavelength)
        self.last_state = self.engine._verify(self.point, self.wavelength, False, self.deadline)
        self._event("optical_point_prepared", index=index, evidence=self.row)
        return {"requested": self.config.points[index], "emission_started": False}

    def qualify(self):
        try:
            return self._qualify_point()
        except BaseException:
            self._previous_current = None
            self.qualified = False
            raise

    def _qualify_point(self):
        self._require_open()
        if self.point is None or self.sample_active:
            raise NktError("Prepare an optical point before qualification")
        if self.qualified:
            self._guard_sample("requalification")
            return {"ready": True, "evidence": deepcopy(self.row)}
        # Restart a bounded optical qualification after outer-axis movement.
        self.deadline = self.clock() + self.feedback.timeout_s if self.feedback else None
        self.engine._verify(self.point, self.wavelength, False, self.deadline)
        if self.config.nkt.emit:
            self.engine.nkt.start_emission(self.point, deadline=self.deadline)
        if self.feedback:
            self.point, result, _ = self.engine._stabilize(
                self.row, self.point, self.wavelength, self.deadline, self.feedback)
            self.row["feedback_result"] = result
        # Keep the tuned settings throughout dwell and acquisition. The selected
        # policy decides whether target drift is fatal or informational.
        until = self.clock() + self.config.nkt.dwell_s
        while True:
            self._guard_sample("qualification", deadline=self.deadline)
            if self.clock() >= until:
                break
            self.sleep(min(self.config.scan.sample_interval_s, until - self.clock()))
        self.qualified = True
        self.row["effective_requested"] = asdict(self.point)
        original_feedback = self.config.scan.feedback
        if original_feedback and original_feedback.initial_current_mode == "previous":
            current = number(self.last_state["nkt"]["source"]["level_pct"],
                "qualified source current readback", original_feedback.source_current_min_pct,
                original_feedback.source_current_max_pct)
            tenths(current)
            self._previous_current = {"index": self.index,
                "input_point_index": self.index // original_feedback.targets_per_optical_point,
                "source_level_pct": current}
        self._event("optical_condition_qualified", evidence=self.row)
        return {"ready": True, "evidence": deepcopy(self.row)}

    def _guard_sample(self, phase, *, deadline=None):
        try:
            return self._collect_guard_sample(phase, deadline=deadline)
        except BaseException:
            self.qualified = False
            self._previous_current = None
            raise

    def _collect_guard_sample(self, phase, *, deadline=None):
        self.last_state = self.engine._verify(self.point, self.wavelength, self.config.nkt.emit, deadline)
        sample = None
        assessment = None
        if self.meter:
            sample = self.engine._sample(self.row, self.point, self.wavelength, deadline, phase)
            if self.feedback:
                within_target = target_in_tolerance(sample["power_w"], self.feedback)
                deviation = sample["power_w"] - self.feedback.target_power_w
                assessment = {"policy": self.config.scan.target_deviation_policy,
                    "target_power_w": self.feedback.target_power_w,
                    "actual_power_w": sample["power_w"],
                    "target_tolerance_w": self.feedback.target_tolerance_w,
                    "target_deviation_w": deviation,
                    "target_deviation_fraction": deviation / self.feedback.target_power_w,
                    "target_in_tolerance": within_target,
                    "target_deviation_continued": (not within_target and
                        self.config.scan.target_deviation_policy == "record_continue" and
                        not sample["above_reduce_threshold"])}
        evidence = {"phase": phase, "captured_at_utc": utc_now(), "state": deepcopy(self.last_state),
                    "power_sample": deepcopy(sample), "power_target_assessment": assessment}
        self.sample_evidence.append(evidence)
        self._event("optical_sample_guard", evidence=evidence)
        if sample is not None and sample["above_reduce_threshold"]:
            raise NktError("Formal optical power exceeds reduction threshold; no automatic correction")
        if assessment and not assessment["target_in_tolerance"] and assessment["policy"] == "abort":
            raise NktError("Formal optical power left its qualified target; no automatic correction")
        return evidence

    def guard_reference_recovery(self, *, deadline):
        """Observe the held optical state without retuning or extending its budget."""
        self._require_open()
        if self.qualified:
            # The discarded bracket is closed by the coordinator before
            # recovery. During a fresh retry, timed electrical settling may
            # already be inside its new bracket; append fresh guards there.
            return self._guard_sample("reference_recovery", deadline=deadline)
        # Before illumination the prepared point must still be confirmed OFF.
        # A failed illuminated guard clears qualified; it must never enter here
        # and be treated as permission to illuminate or retune.
        self.last_state = self._verify_dark_point(deadline)
        self._event("optical_reference_recovery_guard", state=deepcopy(self.last_state),
                    captured_at_utc=utc_now(), emission_expected=False)
        return deepcopy(self.last_state)

    def _verify_dark_point(self, deadline):
        try:
            return self.engine._verify(self.point, self.wavelength, False, deadline)
        except BaseException:
            self.qualified = False
            self._previous_current = None
            raise

    def begin_sample(self):
        if not self.qualified or self.sample_active:
            raise NktError("Optical condition must qualify before a formal sample")
        self.sample_evidence = []
        self._guard_sample("before_electrical")
        self.sample_active = True

    def observe_hold(self, *, phase="held_optical_condition"):
        """Fresh checks between gate transitions, without retuning or a new dwell."""
        self._require_open()
        if not self.qualified or self.sample_active:
            raise NktError("Held optical checks require a qualified point outside formal sampling")
        return self._guard_sample(phase)

    def observe_dark(self, *, phase="diagnostic_dark", deadline=None):
        """Verify a prepared dark point, including PEM readiness, without power READ.

        A dark meter reading is not subjected to a lit target/minimum-signal
        gate. The unchanged NKT OFF, PEM and meter-settings guards still apply.
        """
        self._require_open()
        if self.point is None or self.sample_active or self.qualified:
            raise NktError("Dark observation requires a prepared, suspended optical point")
        self.last_state = self._verify_dark_point(deadline)
        evidence = {"phase": phase, "captured_at_utc": utc_now(), "state": deepcopy(self.last_state)}
        self._event("optical_diagnostic_dark_guard", evidence=evidence)
        return evidence

    def monitor_sample(self, *, phase="diagnostic_capture", deadline=None):
        """Fresh existing optical guards inside an already bracketed sample."""
        if not self.qualified or not self.sample_active:
            raise NktError("Sample monitoring requires a qualified, bracketed optical sample")
        return self._guard_sample(phase, deadline=deadline)

    def end_sample(self, reads=None):
        if not self.sample_active:
            raise NktError("Optical formal sample was not started")
        try:
            evidence = self._guard_sample("after_electrical")
            if reads is not None:
                for reading in reads:
                    if reading.get("module") == "optical":
                        reading["status"]["sample_brackets"] = deepcopy(self.sample_evidence)
                        samples = [item["power_sample"] for item in self.sample_evidence
                                   if item.get("power_sample") is not None]
                        if samples:
                            powers = [sample["power_w"] for sample in samples]
                            reading["status"]["power_bracket_summary"] = {
                                "count": len(samples), "mean_power_w": statistics.mean(powers),
                                "std_power_w": statistics.pstdev(powers),
                                "min_power_w": min(powers), "max_power_w": max(powers),
                                "sequences": [sample["sequence"] for sample in samples],
                                "target_deviation_count": sum(
                                    item["power_target_assessment"]["target_in_tolerance"] is False
                                    for item in self.sample_evidence
                                    if item.get("power_target_assessment") is not None)}
            return evidence
        finally:
            self.sample_active = False

    def read(self):
        if not self.qualified or not self.sample_active:
            raise NktError("Optical read requires a qualified, bracketed formal sample")
        evidence = self._guard_sample("formal")
        source, filt = self.last_state["nkt"]["source"], self.last_state["nkt"]["filter"]
        actual = {"optical_source_level_pct": source["level_pct"]}
        requested = self.config.points[self.index]
        if "optical_wavelength_nm" in requested:
            actual["optical_wavelength_nm"] = ((filt["lower_edge_nm"] + filt["upper_edge_nm"]) / 2
                if self.config.nkt.mode == "varia_bandpass" else filt["wavelength_nm"])
        if "optical_bandwidth_nm" in requested:
            actual["optical_bandwidth_nm"] = filt["upper_edge_nm"] - filt["lower_edge_nm"]
        if "optical_nd_pct" in requested:
            actual["optical_nd_pct"] = filt["nd_pct"]
        if "optical_pulse_picker_ratio" in requested:
            actual["optical_pulse_picker_ratio"] = source["pulse_picker_ratio"]
        measured = {}
        if evidence["power_sample"] is not None:
            measured["optical_power_w"] = evidence["power_sample"]["power_w"]
        if "optical_target_power_w" in requested:
            # This coordinate is a tuning request, not a wattmeter readback.
            # Actual watts retain their existing single formal READ semantics.
            actual["optical_target_power_w"] = self.feedback.target_power_w
        if self.pem:
            measured["pem_frequency_hz"] = self.last_state["pem"]["frequency_hz"]
        return {"module": "optical", "captured_at_utc": utc_now(), "actual": actual,
                "measurements": measured, "clean": True, "problems": [],
                "status": {"readback": deepcopy(self.last_state), "power_sample": evidence["power_sample"],
                           "power_record_contract": "requested-target-actual-power-v2",
                           "power_target_assessment": deepcopy(evidence["power_target_assessment"]),
                           "sample_brackets": deepcopy(self.sample_evidence), "sequential_reads": True,
                           "measurement_plane": self.config.pm.measurement_plane if self.config.pm else None,
                           "power_target_w": self.feedback.target_power_w if self.feedback else None,
                           "feedback": deepcopy(self.row)}}

    def expected_measurements(self):
        return ({"optical_power_w"} if self.meter else set()) | ({"pem_frequency_hz"} if self.pem else set())

    def cleanup(self, *, finish_pem=True, close_meter=True):
        """OFF first; retain reference/shared VISA manager until electrical protection.

        Each resource action runs independently even if another action or the
        event sink fails. A PEM disable acknowledgment is never physical-off proof.
        """
        if type(finish_pem) is not bool:
            raise NktError("finish_pem must be an explicit boolean")
        if type(close_meter) is not bool:
            raise NktError("close_meter must be an explicit boolean")
        self.qualified = self.sample_active = False
        self._previous_current = None
        result = self.cleanup_result
        actions, errors = result["actions"], result["errors"]
        if "nkt" not in actions:
            try:
                if self.engine:
                    actions["nkt"] = self.engine.nkt.finish_session()
                elif self.backend:
                    self.backend.close()
                    actions["nkt"] = {"off_confirmed": False, "errors": [], "ownership_not_acquired": True}
                else:
                    actions["nkt"] = {"off_confirmed": False, "errors": [], "not_opened": True}
            except BaseException as exc:
                actions["nkt"] = {"off_confirmed": False, "errors": [f"{type(exc).__name__}: {exc}"]}
        if self.pem and finish_pem and not self._pem_finished:
            self._pem_finished = True
            try:
                actions["pem"] = self.pem.finish()
            except BaseException as exc:
                actions["pem"] = {"errors": [f"{type(exc).__name__}: {exc}"]}
        if self.meter and close_meter and not self._meter_closed:
            self._meter_closed = True
            try:
                self.meter.close()
                actions["pm100d"] = {"errors": []}
            except BaseException as exc:
                actions["pm100d"] = {"errors": [f"{type(exc).__name__}: {exc}"]}
        result["reference_cleanup_pending"] = self.pem is not None and not self._pem_finished
        result["meter_cleanup_pending"] = self.meter is not None and not self._meter_closed
        result["off_confirmed"] = actions["nkt"].get("off_confirmed") is True
        # Failed parses may never reach a formal row. Keep device transcripts
        # and last-confirmed evidence even when preparation/sample/cleanup aborts.
        result["device_transcripts"] = {
            name: deepcopy(device.transcript) for name, device in
            (("pem", self.pem), ("pm100d", self.meter)) if device is not None}
        result["last_confirmed_state"] = {
            name: deepcopy(device.last_confirmed_state) for name, device in
            (("pem", self.pem), ("pm100d", self.meter)) if device is not None}
        result["state_current"] = False
        if self.engine:
            result["last_confirmed_state"]["nkt"] = deepcopy(self.engine.nkt.record["last_confirmed_state"])
            result["point_evidence"] = deepcopy(self.engine.record["points"])
            result["optical_events"] = deepcopy(self.engine.record["events"])
        result["nkt_write_attempts"] = deepcopy(getattr(self.backend, "command_log", []))
        for name, action in actions.items():
            for error in action.get("errors", []):
                item = f"{name}: {error}"
                if item not in errors:
                    errors.append(item)
        result["verified"] = (result["off_confirmed"] and not errors
                              and not result.get("reference_left_active_or_unknown", False))
        try:
            self._event("optical_cleanup", evidence=result)
        except BaseException as exc:
            errors.append(f"audit: {type(exc).__name__}: {exc}")
            result["verified"] = False
        return deepcopy(result)

    def close(self, *, finish_pem=True):
        if type(finish_pem) is not bool:
            raise NktError("finish_pem must be an explicit boolean")
        if not finish_pem and self.pem and not self._pem_finished:
            # Unknown/high XX excitation may still depend on the reference.
            # Close transport only; never send a PEM disable in that state.
            self.cleanup(finish_pem=False)
            self._pem_finished = True
            action = {"action": "close_without_disable", "disable_acknowledged": False,
                      "physical_off_confirmed": False, "errors": [],
                      "last_output_command": self.pem.output_command,
                      "reason": "XX excitation cleanup not verified"}
            try:
                self.pem.close()
            except BaseException as exc:
                action["errors"].append(f"close: {type(exc).__name__}: {exc}")
            self.cleanup_result["actions"]["pem"] = action
            self.cleanup_result["reference_left_active_or_unknown"] = True
            self.cleanup_result["manual_verification_required"] = True
        return self.cleanup(finish_pem=finish_pem)
