"""Single-owner frequency/excitation points using the daily SR830 safety engine.

No resource is opened by construction. Settings, bounded autorange and harmonic
sampling reuse lockin_test; a point never performs whole-sweep cleanup.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace

from . import lockin_test as daily
from .combination_store import utc_now
from .sr830 import DualSr830Controller
from .lockin_settling import LockinSettling


@dataclass(frozen=True)
class LockinGridPoint:
    frequency_hz: float
    source_v_rms: float
    frequency_index: int
    excitation_index: int
    frequency_segment: int | None
    excitation_segment: int | None
    range_spec: object

    def metadata(self):
        return {key: value for key, value in asdict(self).items() if key != "range_spec"}


class _AuditedInstrument:
    """Retain each complete role read, even if the companion read later fails."""

    def __init__(self, instrument, event):
        self.instrument = instrument
        self.event = event

    def __getattr__(self, name):
        method = getattr(self.instrument, name)
        if name not in {"read_diagnostic", "read_harmonic_sample", "read_fixed_setting", "read_status_latches"} and not name.startswith("set_"):
            return method

        def call(*args, **kwargs):
            role = "lockin_" + self.instrument.role.value
            write = name.startswith("set_")
            if write:
                self.event("lockin_command_attempt", {"role": role, "method": name,
                                                      "arguments": args})
            result = method(*args, **kwargs)
            self.event("lockin_command_return" if write else "lockin_raw_role", {
                "role": role, "method": name, "captured_at_utc": utc_now(),
                "reading": (None if write else result if name == "read_fixed_setting"
                            else {"lia_status": asdict(result[0]), "error_status": result[1]}
                            if name == "read_status_latches" else asdict(result)),
            })
            return result
        return call


class LockinPointSession:
    def __init__(self, config_path, config, event, *, manager_factory=None,
                 mode="excitation", monotonic=None, sleep=None):
        self.config = config
        self.mode = mode
        self.args, self.settings, self.points, self.safety = (
            daily.prepare_configured_lockin_sweep(config_path, config, scan=mode)
        )
        self.event = event
        self.clock = monotonic or daily.time.monotonic
        self.sleep = sleep or daily.time.sleep
        self.wait_guard = None
        self.wait_deadline = None
        self.settling = LockinSettling(monotonic=self.clock, sleep=self._wait)
        self.manager_factory = manager_factory
        self.context = None
        self.pair = None
        self.preflight = None
        self.writes_started = False
        self.harmonic_control = None
        self.sensitivity_setup = None
        self.reserve_setup = None
        self.fixed_setup = None
        self.frequency_setup = None
        self.point_record = None
        self.sample_count = 0
        self.harmonics_by_role = daily._requested_sweep_harmonics_by_role(self.args)
        self.harmonics = daily._requested_sweep_harmonics(self.args)
        self.target_frequency_hz = config.lockin_xx.frequency_hz
        sweep = config.lockin_sweep
        fixed = daily.SweepPointConfig(config.lockin_xx.frequency_hz, None, None, None)
        frequency_specs = ((fixed,) if mode == "excitation" else sweep.frequency_point_specs)
        excitation_specs = ((daily.SweepPointConfig(self.points[0], None, None, None),)
                            if mode == "frequency" else sweep.excitation_point_specs)
        if len(frequency_specs) * len(excitation_specs) > 100000:
            raise ValueError("Offline Lock-in grid exceeds 100000 conditions")
        self.grid = tuple(LockinGridPoint(f.value, u.value, fi, ui,
            f.segment_index, u.segment_index,
            replace(f, value=u.value) if mode == "frequency" else u if mode == "excitation"
            else daily._combined_point_spec(f, u))
            for fi, f in enumerate(frequency_specs) for ui, u in enumerate(excitation_specs))
        self.skip_unsupported = mode != "excitation" and sweep.skip_unsupported_harmonics
        self.skipped_by_frequency = {}
        for frequency in frequency_specs:
            # Includes the all-selected-orders-unsupported case before any I/O.
            _, skipped = daily._harmonics_for_frequency(frequency.value, self.harmonics,
                                                       skip_unsupported=self.skip_unsupported)
            self.skipped_by_frequency[str(frequency.value)] = skipped
        self.point_harmonics = self.harmonics

    def _check_wait(self):
        if self.wait_deadline is not None and self.clock() >= self.wait_deadline:
            raise TimeoutError("Lock-in settling exhausted the operation deadline")
        if self.wait_guard is not None:
            self.wait_guard()
        if self.wait_deadline is not None and self.clock() >= self.wait_deadline:
            raise TimeoutError("Lock-in settling exhausted the operation deadline")

    def _wait(self, seconds):
        monitor = getattr(self, "monitor_wait_context", None)
        if monitor is None or seconds <= 0:
            return LockinPointSession._wait_unmonitored(self, seconds)
        with monitor():
            return LockinPointSession._wait_unmonitored(self, seconds)

    def _wait_unmonitored(self, seconds):
        """Allow the optical owner to supervise every second of detector wait."""
        until = self.clock() + seconds
        self._check_wait()
        if self.wait_guard is None and self.wait_deadline is None:
            self.sleep(seconds)
            return
        budget = seconds
        while budget > 0:
            remaining = min(budget, max(0.0, until - self.clock()))
            if remaining <= 0:
                break
            if self.wait_deadline is not None:
                remaining = min(remaining, self.wait_deadline - self.clock())
                if remaining <= 0:
                    raise TimeoutError("Lock-in settling exhausted the operation deadline")
            chunk = min(1.0, remaining)
            self.sleep(chunk)
            budget -= chunk
            self._check_wait()

    def disturbed(self, reason, *, roles=("lockin_xx", "lockin_xy")):
        self.settling.changed(reason, roles)
        self.event("lockin_settling_changed", dict(self.settling.changes[-1]))

    def _audit_transition_failure(self, error):
        self.point_record["error"] = str(error) or type(error).__name__
        try:
            self.event("lockin_point_transition", self.point_record)
        except BaseException as audit_error:
            error.add_note(f"Transition audit failed: {audit_error}")

    def _ensure_settled(self, record):
        start = len(record.get("settling_waits", ()))
        audit = {"point_index": record.get("point_index"), "sample_index": self.sample_count}
        try:
            self._check_wait()
            self.harmonic_control.ensure_settled(*self.pair, self.args.settle_s, record)
        except BaseException as error:
            audit.update(error=str(error) or type(error).__name__,
                         settling_waits=record.get("settling_waits", [])[start:])
            try:
                self.event("lockin_settling_wait", audit)
            except BaseException as audit_error:
                error.add_note(f"Settling audit failed: {audit_error}")
            raise
        audit["settling_waits"] = record.get("settling_waits", [])[start:]
        self.event("lockin_settling_wait", audit)

    def open(self):
        factory = self.manager_factory or daily._load_resource_manager_factory()
        self.context = daily._open_pair(self.settings, factory)
        self.pair = tuple(_AuditedInstrument(i, self.event) for i in self.context.__enter__())
        xx, xy = self.pair
        self.preflight = DualSr830Controller(xx, xy).verify_existing_configuration(
            frequency_hz=self.config.lockin_xx.frequency_hz,
            check_frequency=False,
            ignore_output_overload=True,
        )
        return {role: asdict(d) for role, d in
                zip(("lockin_xx", "lockin_xy"), self.preflight)}

    def configure(self):
        xx, xy = self.pair
        before_xx, before_xy = self.preflight
        self.writes_started = True
        self.fixed_setup = {}
        daily._configure_sweep_fixed_settings(
            xx, xy, config=self.config, preflight_xx=before_xx,
            preflight_xy=before_xy, settle_s=self.args.settle_s,
            record=self.fixed_setup,
        )
        self.frequency_setup = {}
        daily._configure_sweep_baseline_frequency(
            xx, xy, baseline_hz=self.config.lockin_xx.frequency_hz,
            initial_xx_frequency_hz=before_xx.frequency_hz,
            settle_s=self.args.settle_s, record=self.frequency_setup,
        )
        self.reserve_setup = daily._new_sweep_reserve_setup(
            self.config.lockin_xx, self.config.lockin_xy,
            original_xx_reserve_mode=before_xx.reserve_mode,
            original_xy_reserve_mode=before_xy.reserve_mode,
        )
        daily._configure_sweep_reserve_modes(
            xx, xy, reserve_setup=self.reserve_setup, settle_s=self.args.settle_s)
        self.sensitivity_setup = daily._new_sweep_sensitivity_setup(
            lockin_xx_config=self.config.lockin_xx, lockin_xy_config=self.config.lockin_xy,
            original_xx_sensitivity=before_xx.sensitivity,
            original_xy_sensitivity=before_xy.sensitivity,
            initial_full_scale_overrides=daily._initial_sweep_range_overrides(
                (self.grid[0].range_spec,)),
        )
        daily._configure_sweep_sensitivities(
            xx, xy, sensitivity_setup=self.sensitivity_setup, settle_s=self.args.settle_s)
        self.harmonic_control = daily.HarmonicSensitivitySession.create(
            self.config.lockin_xx, self.config.lockin_xy, self.sensitivity_setup, self.reserve_setup, self.harmonics_by_role,
            overload_policy=self.config.lockin_sweep.overload_policy)
        if self.harmonic_control is not None:
            self.harmonic_control.settling = self.settling
            self.settling.changed("acquisition_initialization")
            self.pair = xx, xy = self.harmonic_control.track_pair(xx, xy)
            self.harmonic_control.enter(
                xx, xy, harmonic=self.harmonic_control.initial_harmonics(self.config.lockin_xx.frequency_hz), settle_s=self.args.settle_s,
                record=self.sensitivity_setup.setdefault("harmonic_initialization", {}))
        self.event("lockin_configured", {"sensitivity": self.sensitivity_setup,
                                         "reserve": self.reserve_setup,
                                         "fixed": self.fixed_setup,
                                         "frequency": self.frequency_setup})

    def set_point(self, source_v, frequency_hz=None, *, point_index=None):
        target_hz = self.config.lockin_xx.frequency_hz if frequency_hz is None else frequency_hz
        if point_index is None:
            point_index = next((i for i, p in enumerate(self.grid)
                if (p.source_v_rms, p.frequency_hz) == (source_v, target_hz)), -1)
        if not 0 <= point_index < len(self.grid):
            raise ValueError("Lock-in point is outside the validated configured grid")
        point = self.grid[point_index]
        if (point.source_v_rms, point.frequency_hz) != (source_v, target_hz):
            raise ValueError("Lock-in grid index and coordinates differ")
        xx, xy = self.pair
        daily._validate_excitation_safety(self.args, (source_v,))
        self.point_record = {
            "point_index": point_index, "source_v_rms": source_v,
            "target_frequency_hz": target_hz, **point.metadata(),
            "range_spec": asdict(point.range_spec),
            "samples": [], "harmonic_transition_status": [],
        }
        try:
            daily._verify_frequency_readbacks(self.target_frequency_hz,
                xx.read_reference_frequency(), xy.read_reference_frequency(),
                rel_tolerance=1e-5,
                absolute_tolerance_hz=daily.SWEEP_FREQUENCY_ABS_TOLERANCE_HZ)
            if target_hz != self.target_frequency_hz:
                if not daily.math.isclose(xx.read_sine_output(), daily.MINIMUM_SINE_OUTPUT_V,
                                         rel_tol=0, abs_tol=1e-12):
                    xx.set_minimum_sine_output()
                minimum = xx.read_sine_output()
                self.point_record["source_before_frequency_v_rms"] = minimum
                if abs(minimum - daily.MINIMUM_SINE_OUTPUT_V) > 1e-9:
                    raise ValueError("4 mV bridge before frequency change was not confirmed")
                self.harmonic_control.prepare_frequency(xx, xy, target_hz,
                                                        self.args.settle_s, self.point_record)
                self.point_record["frequency_write_attempted"] = True
                xx.set_internal_reference_frequency(target_hz)
                self.target_frequency_hz = target_hz
            # Internal FREQ/source registers read back immediately; qualify XY
            # after the final-setting deadline rather than before it can lock.
            if not daily.math.isclose(xx.read_sine_output(), source_v, rel_tol=0, abs_tol=1e-12):
                xx.set_sine_output(source_v)
        except BaseException as error:
            self._audit_transition_failure(error)
            raise
        try:
            daily._apply_sweep_segment_ranges(
                xx, xy, sensitivity_setup=self.sensitivity_setup,
                point_spec=point.range_spec, lockin_xx_config=self.config.lockin_xx,
                lockin_xy_config=self.config.lockin_xy, settle_s=self.args.settle_s,
                record=self.point_record, harmonic=self.harmonic_control.current_harmonics,
                overload_policy=self.harmonic_control.overload,
                on_input_overload=lambda status: self.harmonic_control.diagnose_overload(
                    xx, xy, status, self.args.settle_s, self.point_record),
                settling=self.harmonic_control,
            )
            self.harmonic_control.enter(xx, xy,
                harmonic=self.harmonic_control.initial_harmonics(target_hz),
                settle_s=self.args.settle_s, record=self.point_record)
            self.sample_count = 0
            return self._read_coordinates(verify_companion=False)
        except BaseException as error:
            self._audit_transition_failure(error)
            raise

    def _read_coordinates(self, *, verify_companion=True):
        xx, xy = self.pair
        source = xx.read_sine_output()
        safety = daily._validate_excitation_safety(self.args, (source,))
        frequency, companion = xx.read_reference_frequency(), xy.read_reference_frequency()
        if verify_companion:
            daily._verify_frequency_readbacks(
                self.target_frequency_hz, frequency, companion,
                rel_tolerance=1e-5, absolute_tolerance_hz=daily.SWEEP_FREQUENCY_ABS_TOLERANCE_HZ)
        else:
            if (not daily.math.isfinite(frequency) or not 0 < frequency <= daily.MAXIMUM_REFERENCE_FREQUENCY_HZ
                    or not daily.math.isfinite(companion) or not 0 <= companion <= daily.MAXIMUM_REFERENCE_FREQUENCY_HZ):
                raise daily.Sr830Error("Invalid reference frequency during transition")
            daily._verify_frequency_readbacks(self.target_frequency_hz, frequency, frequency,
                rel_tolerance=1e-5, absolute_tolerance_hz=daily.SWEEP_FREQUENCY_ABS_TOLERANCE_HZ)
        record = self.point_record if self.point_record is not None else self.frequency_setup
        record.update(source_readback_v_rms=source,
                                 actual_frequency_hz=frequency,
                                 frequency_readback_hz={"lockin_xx": frequency, "lockin_xy": companion},
                                 companion_reference_qualified=verify_companion,
                                 formal_check_deferred=not verify_companion,
                                 source_readback_safety=safety)
        self.point_harmonics, skipped = daily._harmonics_for_frequency(
            max(self.target_frequency_hz, frequency, companion if verify_companion else frequency), self.harmonics,
            skip_unsupported=self.skip_unsupported)
        record["skipped_harmonics"] = skipped
        return {"lockin_excitation_v_rms": source, "lockin_frequency_hz": frequency}

    def read_coordinates(self, *, verify_companion=True):
        """Fresh coordinates; preparation may explicitly defer the XY pair check."""
        return self._read_coordinates(verify_companion=verify_companion)

    def verify_ready(self):
        """Check dark preparation without claiming that the response is settled.

        No setter, LIAS read or settling-clock change occurs here. The optical
        owner still verifies its reference/chopper before illumination. Only XX's
        source clock is qualified here; illuminated qualification/formal entry
        fills the remaining settle and strictly checks the XY frequency pair.
        """
        if self.harmonic_control is None:
            raise ValueError("Lock-in readiness requires completed configuration")
        audit = {"ready": False, "captured_at_utc": utc_now(),
                 "settling_performed": False, "status_latches_consumed": False,
                 "companion_reference_qualified": False, "formal_check_deferred": True}
        try:
            if self.wait_deadline is not None and self.clock() >= self.wait_deadline:
                raise TimeoutError("Lock-in readiness exhausted the operation deadline")
            audit["settings"] = self.harmonic_control.verify(*self.pair,
                self.harmonic_control.current_harmonics, audit)
            audit.pop("harmonic_setting_checks", None)  # Same complete readback is stored once.
            audit["actual"] = self._read_coordinates(verify_companion=False)
            point = self.point_record if self.point_record is not None else self.frequency_setup
            audit["reference_hz"] = dict(point["frequency_readback_hz"])
            if self.wait_deadline is not None and self.clock() >= self.wait_deadline:
                raise TimeoutError("Lock-in readiness exhausted the operation deadline")
            audit["ready"] = True
        except BaseException as error:
            audit["error"] = str(error) or type(error).__name__
            try:
                self.event("lockin_readiness", audit)
            except BaseException as audit_error:
                error.add_note(f"Readiness audit failed: {audit_error}")
            raise
        self.event("lockin_readiness", audit)
        return audit

    def qualify(self):
        self.harmonic_control.enter(*self.pair,
            harmonic=self.harmonic_control.initial_harmonics(self.target_frequency_hz),
            settle_s=self.args.settle_s, record=self.point_record)
        self._ensure_settled(self.point_record)
        actual = self._read_coordinates()
        self.event("lockin_point_qualification", self.point_record)
        self.sample_count = 0

    def sample_point(self, *, measurement_context=None):
        if self.sample_count:
            self._wait(self.args.sample_interval_s)
        self._ensure_settled(self.point_record)
        actual = self._read_coordinates()
        record = {**self.point_record, "samples": [], "harmonic_transition_status": []}
        try:
            daily._capture_sweep_point(
                *self.pair, target_frequency_hz=actual["lockin_frequency_hz"],
                harmonics=self.point_harmonics,
                selected_roles_by_harmonic=daily._roles_by_harmonic(
                    self.point_harmonics, self.harmonics_by_role),
                harmonic_settle_s=self.args.settle_s, samples=1,
                sample_interval_s=self.args.sample_interval_s, record=record,
                frequency_rel_tolerance=1e-5,
                harmonic_control=self.harmonic_control,
                measurement_context=measurement_context,
                on_formal_sample_recorded=lambda payload: self.event("lockin_formal_pair", payload),
            )
        finally:
            self.event("lockin_point_samples", record)
        self.sample_count += 1
        measured = {}
        for sample in record["samples"]:
            for role in sample["selected_roles"]:
                reading = sample["lockin_" + role]["reading"]
                for metric in ("x_v", "y_v", "amplitude_v", "phase_deg"):
                    measured[f"lockin_{role}_h{sample['harmonic']}_{metric}"] = reading[metric]
        from .lockin_overload import overload_summary
        summary = overload_summary(record)
        clean = all(s["valid_for_analysis_by_role"]["lockin_" + role]
                    for s in record["samples"] for role in s["selected_roles"])
        problems = [p for s in record["samples"] for role in s["selected_roles"]
                    for p in s["problems_by_role"]["lockin_" + role]]
        # Output-only flags already allowed by acquisition are conservatively
        # excluded by channel analysis, without changing the legacy trip policy.
        continued = any(s["continued_overload_problems"] for s in record["samples"])
        return {"module": "lockin", "captured_at_utc": utc_now(), "actual": actual,
                "measurements": measured, "clean": not problems,
                "valid_for_analysis": clean, "overload_summary": summary,
                "overload_continuation": continued, "problems": problems, "status": record}

    def cleanup(self):
        if not self.writes_started:
            return {"attempted": False, "verified": True, "errors": []}
        cleanup = daily._restore_scan_state(
            *self.pair, baseline_hz=self.config.lockin_xx.frequency_hz,
            original_xx_sensitivity=daily.sensitivity_code(self.config.lockin_xx.sensitivity_full_scale_v),
            original_xy_sensitivity=daily.sensitivity_code(self.config.lockin_xy.sensitivity_full_scale_v),
            restore_sensitivity=daily._range_write_attempted(self.sensitivity_setup, "lockin_xx"),
            restore_xy_sensitivity=daily._range_write_attempted(self.sensitivity_setup, "lockin_xy"),
            original_xx_reserve_mode=daily.RESERVE_MODE_CODES[self.config.lockin_xx.reserve_mode.value],
            original_xy_reserve_mode=daily.RESERVE_MODE_CODES[self.config.lockin_xy.reserve_mode.value],
            restore_xx_reserve=daily._reserve_write_attempted(self.reserve_setup, "lockin_xx"),
            restore_xy_reserve=daily._reserve_write_attempted(self.reserve_setup, "lockin_xy"),
            harmonic_control=self.harmonic_control,
            restore_frequency=self.mode != "excitation", settle_s=self.args.settle_s, writes_started=True,
            ignore_output_overload=True,
        )
        if (self.fixed_setup and self.fixed_setup.get("verified")
                and self.frequency_setup and self.frequency_setup.get("verified")
                and self.sensitivity_setup and self.reserve_setup):
            daily._verify_sweep_configured_cleanup(cleanup, self.config)
        final = cleanup.get("final", {}).get("lockin_xx", {})
        source = final.get("sine_output_v")
        cleanup["source_protection_verified"] = (isinstance(source, (float, int))
            and daily.math.isfinite(source)
            and daily.math.isclose(source, daily.MINIMUM_SINE_OUTPUT_V, rel_tol=0, abs_tol=1e-9))
        return cleanup

    def protect_source(self):
        """Protect XX with actual instrument readback, independent of wiring."""
        xx, _ = self.pair
        record = {"attempted": True, "write_attempted": False,
                  "source_protection_verified": False, "verified": False}
        try:
            before = xx.read_sine_output()
            record["before_v_rms"] = before
            if not daily.math.isclose(before, daily.MINIMUM_SINE_OUTPUT_V, rel_tol=0, abs_tol=1e-12):
                record["write_attempted"] = True
                self.writes_started = True
                xx.set_minimum_sine_output()
            actual = xx.read_sine_output()
            record["source_readback_v_rms"] = actual
            if not daily.math.isclose(actual, daily.MINIMUM_SINE_OUTPUT_V, rel_tol=0, abs_tol=1e-9):
                raise daily.Sr830Error("XX protection minimum output was not confirmed")
            record.update(verified=True, source_protection_verified=True)
            self.event("lockin_source_protection", record)
            return record
        except BaseException as error:
            record["error"] = str(error) or type(error).__name__
            try:
                self.event("lockin_source_protection", record)
            except BaseException as audit_error:
                error.add_note(f"Source protection audit failed: {audit_error}")
            raise

    def close(self):
        if self.context is not None:
            self.context.__exit__(None, None, None)
