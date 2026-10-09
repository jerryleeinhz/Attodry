"""Single-owner frequency/excitation points using the daily SR830 safety engine.

No resource is opened by construction. Settings, bounded autorange and harmonic
sampling reuse lockin_test; a point never performs whole-sweep cleanup.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace

from . import lockin_test as daily
from .combination_store import utc_now


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
        if name not in {"read_diagnostic", "read_harmonic_sample", "read_fixed_setting", "read_status_latches", "read_receiver_invariants"} and not name.startswith("set_"):
            return method

        def call(*args, **kwargs):
            role = "lockin_" + self.instrument.role.value
            write = name.startswith("set_")
            if write:
                self.event("lockin_command_attempt", {"role": role, "method": name,
                                                      "arguments": args})
            try:
                result = method(*args, **kwargs)
            except Exception as exc:
                native_audit = daily._native_error_audit(exc)
                if native_audit:
                    self.event("lockin_native_error", {
                        "role": role, "method": name, "captured_at_utc": utc_now(),
                        "error": str(exc), **native_audit,
                    })
                raise
            self.event("lockin_command_return" if write else "lockin_raw_role", {
                "role": role, "method": name, "captured_at_utc": utc_now(),
                "reading": (None if write else result if name in {"read_fixed_setting", "read_receiver_invariants"}
                            else {"lia_status": asdict(result[0]), "error_status": result[1]}
                            if name == "read_status_latches" else asdict(result)),
            })
            return result
        return call


class LockinPointSession:
    def __init__(self, config_path, config, event, *, manager_factory=None,
                 mode="excitation", authorize_writes=False):
        self.config = config
        self.mode = mode
        self.args, self.settings, self.points, self.safety = (
            daily.prepare_configured_lockin_sweep(config_path, config, scan=mode)
        )
        self.event = event
        self.settings["authorize_writes"] = authorize_writes is True
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
            _, skipped = daily._harmonics_for_frequency(
                frequency.value, self.harmonics, skip_unsupported=self.skip_unsupported,
                config=config, harmonics_by_role=self.harmonics_by_role)
            self.skipped_by_frequency[str(frequency.value)] = skipped
        self.point_harmonics = self.harmonics

    def open(self):
        daily._require_mixed_write_authorization(self.settings)
        factory = self.manager_factory or daily._load_resource_manager_factory()
        self.context = daily._open_pair(self.settings, factory)
        self.pair = tuple(_AuditedInstrument(i, self.event) for i in self.context.__enter__())
        xx, xy = self.pair
        self.preflight = daily._pair_controller(xx, xy).verify_existing_configuration(
            frequency_hz=self.config.lockin_xx.frequency_hz,
            check_frequency=False,
            ignore_output_overload=True,
        )
        return {role: asdict(d) for role, d in
                zip(("lockin_xx", "lockin_xy"), self.preflight)}

    def configure(self):
        if (daily.model_name(self.config.lockin_xy) == "SR865A"
                and self.settings.get("authorize_writes") is not True):
            raise daily.AuthorizationRequired("Mixed lock-in setting writes were not authorized")
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
            if daily.model_name(xy) == "SR865A":
                self.harmonic_control.verify(xx, xy, self.harmonic_control.current_harmonics,
                                             self.point_record)
            daily._verify_frequency_readbacks(self.target_frequency_hz,
                xx.read_reference_frequency(), xy.read_reference_frequency(),
                rel_tolerance=1e-5,
                absolute_tolerance_hz=daily.SWEEP_FREQUENCY_ABS_TOLERANCE_HZ)
            if target_hz != self.target_frequency_hz:
                xx.set_minimum_sine_output()
                daily.time.sleep(daily.EXCITATION_SOURCE_STEP_SETTLE_INTERVALS * self.args.settle_s)
                minimum = xx.read_sine_output()
                self.point_record["source_before_frequency_v_rms"] = minimum
                if abs(minimum - daily.MINIMUM_SINE_OUTPUT_V) > 1e-9:
                    raise ValueError("4 mV bridge before frequency change was not confirmed")
                self.harmonic_control.prepare_frequency(xx, xy, target_hz,
                                                        self.args.settle_s, self.point_record)
                self.point_record["frequency_write_attempted"] = True
                xx.set_internal_reference_frequency(target_hz)
                daily.time.sleep(self.args.settle_s)
                transition, problems = daily._consume_frequency_transition(
                    xx, xy, harmonic=self.harmonic_control.current_harmonics)
                self.point_record["frequency_transition_status"] = transition
                if problems:
                    raise daily.Sr830Error("Unsafe frequency transition: " + "; ".join(problems))
                self.target_frequency_hz = target_hz
                daily.time.sleep(self.args.settle_s)
            # Verify frequency before restoring the requested source amplitude.
            self._read_coordinates()
            xx.set_sine_output(source_v)
            daily.time.sleep(daily.EXCITATION_SOURCE_STEP_SETTLE_INTERVALS * self.args.settle_s)
        except BaseException:
            self.event("lockin_point_transition", self.point_record)
            raise
        daily._apply_sweep_segment_ranges(
            xx, xy, sensitivity_setup=self.sensitivity_setup,
            point_spec=point.range_spec, lockin_xx_config=self.config.lockin_xx,
            lockin_xy_config=self.config.lockin_xy, settle_s=self.args.settle_s,
            record=self.point_record, harmonic=self.harmonic_control.current_harmonics,
            overload_policy=self.harmonic_control.overload,
            on_input_overload=lambda status: self.harmonic_control.diagnose_overload(
                xx, xy, status, self.args.settle_s, self.point_record),
        )
        self.sample_count = 0
        return self._read_coordinates()

    def _read_coordinates(self):
        xx, xy = self.pair
        source = xx.read_sine_output()
        safety = daily._validate_excitation_safety(self.args, (source,))
        frequency, companion = xx.read_reference_frequency(), xy.read_reference_frequency()
        daily._verify_frequency_readbacks(
            self.target_frequency_hz, frequency, companion,
            rel_tolerance=1e-5, absolute_tolerance_hz=daily.SWEEP_FREQUENCY_ABS_TOLERANCE_HZ)
        self.point_record.update(source_readback_v_rms=source,
                                 actual_frequency_hz=frequency,
                                 frequency_readback_hz={"lockin_xx": frequency, "lockin_xy": companion},
                                 source_readback_safety=safety)
        self.point_harmonics, skipped = daily._harmonics_for_frequency(
            max(self.target_frequency_hz, frequency, companion), self.harmonics,
            skip_unsupported=self.skip_unsupported, config=self.config,
            harmonics_by_role=self.harmonics_by_role)
        self.point_record["skipped_harmonics"] = skipped
        return {"lockin_excitation_v_rms": source, "lockin_frequency_hz": frequency}

    def qualify(self):
        # Also when SMU is the inner axis: response/range may have changed.
        daily.time.sleep(daily.EXCITATION_SOURCE_STEP_SETTLE_INTERVALS * self.args.settle_s)
        actual = self._read_coordinates()
        self.event("lockin_point_qualification", self.point_record)
        self.sample_count = 0

    def sample_point(self, *, measurement_context=None):
        if self.sample_count:
            daily.time.sleep(self.args.sample_interval_s)
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
        self.unavailable_phase_keys = set()
        for sample in record["samples"]:
            for role in sample["selected_roles"]:
                reading = sample["lockin_" + role]["reading"]
                for metric in ("x_v", "y_v", "amplitude_v", "phase_deg"):
                    key = f"lockin_{role}_h{sample['harmonic']}_{metric}"
                    if (metric == "phase_deg" and reading[metric] is None
                            and reading.get("model") == "SR865A"
                            and reading["x_v"] == reading["y_v"] == reading["amplitude_v"] == 0):
                        # atan2(0, 0) has no measured phase. Keep None in raw
                        # audit data; omit only this unavailable plotted metric.
                        self.unavailable_phase_keys.add(key)
                        continue
                    measured[key] = reading[metric]
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
            original_xx_sensitivity=daily.sensitivity_code_for(self.config.lockin_xx, self.config.lockin_xx.sensitivity_full_scale_v),
            original_xy_sensitivity=daily.sensitivity_code_for(self.config.lockin_xy, self.config.lockin_xy.sensitivity_full_scale_v),
            restore_sensitivity=daily._range_write_attempted(self.sensitivity_setup, "lockin_xx"),
            restore_xy_sensitivity=daily._range_write_attempted(self.sensitivity_setup, "lockin_xy"),
            original_xx_reserve_mode=daily.reserve_code_for(self.config.lockin_xx),
            original_xy_reserve_mode=daily.reserve_code_for(self.config.lockin_xy),
            restore_xx_reserve=daily._reserve_write_attempted(self.reserve_setup, "lockin_xx"),
            restore_xy_reserve=daily._reserve_write_attempted(self.reserve_setup, "lockin_xy"),
            harmonic_control=self.harmonic_control,
            restore_frequency=self.mode != "excitation", settle_s=self.args.settle_s, writes_started=True,
            ignore_output_overload=True,
        )
        if (self.fixed_setup and self.fixed_setup.get("verified")
                and self.frequency_setup and self.frequency_setup.get("verified")
                and self.sensitivity_setup and self.reserve_setup):
            daily._verify_sweep_configured_cleanup(cleanup, self.config,
                receiver_setup=self.fixed_setup.get("receiver_setup"))
        return cleanup

    def close(self):
        if self.context is not None:
            self.context.__exit__(None, None, None)
