"""Single-owner frequency/excitation points using the daily SR830 safety engine.

No resource is opened by construction. Settings, bounded autorange and harmonic
sampling reuse lockin_test; a point never performs whole-sweep cleanup.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from collections.abc import Mapping
import math

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

    def __init__(self, instrument, event, write_guard=None, abort_guard=None):
        self.instrument = instrument
        self.event = event
        self.write_guard = write_guard
        self.abort_guard = abort_guard

    def __getattr__(self, name):
        method = getattr(self.instrument, name)
        tracked = (name in {"read_diagnostic", "read_harmonic_sample", "read_fixed_setting",
                           "read_status_latches", "read_receiver_invariants"}
                   or name.startswith("set_"))
        guarded = self.abort_guard is not None and (
            name.startswith(("read_", "set_")) or name == "configure_receiver")
        if not tracked and not guarded:
            return method

        def call(*args, **kwargs):
            if self.abort_guard is not None:
                self.abort_guard()
            role = "lockin_" + self.instrument.role.value
            write = name.startswith("set_")
            if (write or name == "configure_receiver") and self.write_guard is not None:
                self.write_guard()
            if not tracked:
                return method(*args, **kwargs)
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
                 mode="excitation", authorize_writes=False, recovery_owner_guard=None,
                 recovery_abort_guard=None, monotonic=None, sleep=None):
        self.config = config
        self.mode = mode
        self.args, self.settings, self.points, self.safety = (
            daily.prepare_configured_lockin_sweep(config_path, config, scan=mode)
        )
        self.event = event
        self.clock = {"monotonic": monotonic, "sleep": sleep}
        self.settings["authorize_writes"] = authorize_writes is True
        self.manager_factory = manager_factory
        self.recovery_owner_guard = recovery_owner_guard
        self.recovery_abort_guard = recovery_abort_guard
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
        self.range_profile = None
        self.range_profile_record = None
        self.range_profile_failed = False
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

    def open(self, *, recovery=False):
        daily._require_mixed_write_authorization(self.settings)
        factory = self.manager_factory or daily._load_resource_manager_factory()
        self.context = daily._open_pair(self.settings, factory)
        self.pair = tuple(_AuditedInstrument(i, self.event,
            write_guard=self._guard_recovery_write if self.recovery_owner_guard is not None else None,
            abort_guard=self.recovery_abort_guard)
            for i in self.context.__enter__())
        xx, xy = self.pair
        self.preflight = daily._pair_controller(xx, xy).verify_existing_configuration(
            frequency_hz=self.config.lockin_xx.frequency_hz,
            check_frequency=False,
            ignore_output_overload=True,
        )
        if recovery:
            self._verify_recovery_baseline()
        return {role: asdict(d) for role, d in
                zip(("lockin_xx", "lockin_xy"), self.preflight)}

    def _guard_recovery_write(self):
        self.recovery_owner_guard()

    def _verify_recovery_baseline(self):
        """Require independently clear minimum/h1 before a new execution segment."""
        from .lockin_apply import _status_problems
        problems = []
        for diagnostic in self.preflight:
            role = "lockin_" + diagnostic.role.value
            problems.extend(_status_problems(diagnostic))
            if diagnostic.lia_status is not None and diagnostic.lia_status.output_overload:
                problems.append(role + ": output overload at recovery baseline")
            if diagnostic.harmonic != 1:
                problems.append(role + ": recovery baseline must be h1")
            if daily.model_name(diagnostic) == "SR830" and abs(
                    diagnostic.sine_output_v - daily.MINIMUM_SINE_OUTPUT_V) > 1e-9:
                problems.append(role + ": recovery baseline must be 4 mVrms")
        self.event("lockin_recovery_baseline", {
            "roles": {"lockin_" + d.role.value: asdict(d) for d in self.preflight},
            "problems": problems})
        if problems:
            raise daily.Sr830Error("Recovery requires baseline repair: " + "; ".join(problems))
        xx, xy = self.preflight
        daily._verify_requested_sweep_frequency_readbacks(
            self.config.lockin_xx.frequency_hz, xx.frequency_hz, xy.frequency_hz)
        daily._verify_requested_sweep_frequency_readbacks(
            self.config.lockin_xx.frequency_hz, xx.snapshot_frequency_hz, xy.snapshot_frequency_hz)

    def _widen_recovery_baseline(self):
        """Use existing physical range mapping before Reserve/filter changes."""
        updated = []
        changed = False
        for instrument, before, target in zip(self.pair, self.preflight,
                (self.config.lockin_xx, self.config.lockin_xy)):
            code = daily.sensitivity_code_for(target, target.sensitivity_full_scale_v)
            widening = daily.sensitivity_full_scale_for(target, code) > (
                daily.sensitivity_full_scale_for(target, before.sensitivity))
            if widening:
                changed = True
                self.event("lockin_recovery_widen_attempt", {
                    "role": "lockin_" + target.role.value, "before_code": before.sensitivity,
                    "target_code": code})
                instrument.set_sensitivity(code)
                readback = instrument.read_sensitivity()
                self.event("lockin_recovery_widen_readback", {
                    "role": "lockin_" + target.role.value, "actual_code": readback})
                if readback != code:
                    raise daily.Sr830Error("Recovery sensitivity widening was not confirmed")
                before = replace(before, sensitivity=readback)
            updated.append(before)
        self.preflight = tuple(updated)
        if changed:
            daily.time.sleep(self.args.settle_s)
            self.preflight = tuple(instrument.read_diagnostic(consume_status_latches=True)
                                   for instrument in self.pair)
            self._verify_recovery_baseline()

    def configure(self, *, recovery=False, cleanup_only=False):
        if (daily.model_name(self.config.lockin_xy) == "SR865A"
                and self.settings.get("authorize_writes") is not True):
            raise daily.AuthorizationRequired("Mixed lock-in setting writes were not authorized")
        xx, xy = self.pair
        before_xx, before_xy = self.preflight
        self.writes_started = True
        if recovery:
            self._widen_recovery_baseline()
            before_xx, before_xy = self.preflight
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
            initial_full_scale_overrides=(None if cleanup_only else
                daily._initial_sweep_range_overrides((self.grid[0].range_spec,))),
        )
        daily._configure_sweep_sensitivities(
            xx, xy, sensitivity_setup=self.sensitivity_setup, settle_s=self.args.settle_s)
        self.harmonic_control = (None if cleanup_only else daily.HarmonicSensitivitySession.create(
            self.config.lockin_xx, self.config.lockin_xy, self.sensitivity_setup, self.reserve_setup, self.harmonics_by_role,
            overload_policy=self.config.lockin_sweep.overload_policy, **self.clock))
        if self.harmonic_control is not None:
            self.pair = xx, xy = self.harmonic_control.track_pair(xx, xy)
            self.harmonic_control.enter(
                xx, xy, harmonic=self.harmonic_control.initial_harmonics(self.config.lockin_xx.frequency_hz), settle_s=self.args.settle_s,
                record=self.sensitivity_setup.setdefault("harmonic_initialization", {}))
        self.event("lockin_configured", {"sensitivity": self.sensitivity_setup,
                                         "reserve": self.reserve_setup,
                                         "fixed": self.fixed_setup,
                                         "frequency": self.frequency_setup})

    def _require_range_profile_ready(self):
        if self.range_profile_failed:
            raise daily.Sr830Error("Lock-in range profile failed; acquisition cannot continue")

    def range_context(self):
        """Detached condition provenance; never infer an unconfirmed scale."""
        if self.range_profile_record is None:
            return None
        return deepcopy({key: value for key, value in self.range_profile_record.items()
                         if key not in {"harmonic_setting_checks", "final_setting_checks"}})

    def apply_range_profile(self, profile):
        """Apply one compiled fixed profile, outside formal acquisition.

        Validate the complete pair before I/O, publish new expectations only after
        both roles verify, and let the existing final-settings deadline settle it.
        The original config and cleanup baseline are deliberately not replaced.
        """
        self._require_range_profile_ready()
        if not isinstance(profile, Mapping) or set(profile) != {"name", "ranges"}:
            raise ValueError("Lock-in range profile must contain name and ranges")
        name, ranges = profile["name"], profile["ranges"]
        roles = ("lockin_xx", "lockin_xy")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Lock-in range profile name must be nonempty")
        if not isinstance(ranges, Mapping) or set(ranges) != set(roles):
            raise ValueError("Lock-in range profile must specify both active roles")
        codes, normalized = {}, {}
        for role in roles:
            config, value = getattr(self.config, role), ranges[role]
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= 0):
                raise ValueError(f"{role} range profile scale must be positive and finite")
            if (config.sensitivity_mode is not daily.SensitivityMode.FIXED
                    or config.harmonic_settings):
                raise ValueError("Lock-in range profiles require fixed mode without harmonic overrides")
            if value not in getattr(self.config.lockin_safety, role).allowed_fixed_full_scales_v:
                raise ValueError(f"{role} range profile scale is not allowed by lockin_safety.toml")
            codes[role] = daily.sensitivity_code_for(config, value)
            normalized[role] = float(value)
        if any(p.range_spec.xx_full_scale_v is not None or p.range_spec.xy_full_scale_v is not None
               for p in self.grid):
            raise ValueError("Lock-in range profiles cannot combine with segment or point range overrides")
        if self.pair is None or self.harmonic_control is None:
            raise ValueError("Lock-in range profile requires a configured session")
        record = {"contract": "outer-condition-lockin-range-v1", "rule_name": name,
                  "requested_full_scales_v": normalized, "confirmed_full_scales_v": {},
                  "verified": False, "changed_roles": [], "transitions": [],
                  "captured_at_utc": utc_now()}
        self.range_profile_record = record
        try:
            self.event("lockin_range_rule_apply_started", deepcopy(record))
            before = self.harmonic_control.verify(*self.pair,
                self.harmonic_control.current_harmonics, record)
            for role, instrument in zip(roles, self.pair):
                prior = before["roles"][role]["sensitivity_code"]
                transition = {"role": role, "before_sensitivity_code": prior,
                              "requested_sensitivity_code": codes[role],
                              "requested_full_scale_v": normalized[role],
                              "started_at_utc": utc_now(),
                              "write_attempted": False, "write_returned": False}
                record["transitions"].append(transition)
                if prior != codes[role]:
                    record["changed_roles"].append(role)
                    transition["write_attempted"] = True
                    self.sensitivity_setup["ranges"][role]["write_attempted"] = True
                    self.event("lockin_range_rule_write_attempt", deepcopy(transition))
                    instrument.set_sensitivity(codes[role])
                    transition["write_returned"] = True
                actual = instrument.read_sensitivity()
                transition["readback_sensitivity_code"] = actual
                transition["readback_at_utc"] = utc_now()
                transition["readback_full_scale_v"] = daily.sensitivity_full_scale_for(
                    getattr(self.config, role), actual)
                self.event("lockin_range_rule_readback", deepcopy(transition))
                if actual != codes[role]:
                    raise daily.Sr830Error(f"{role} range profile readback {actual} != {codes[role]}")
                record["confirmed_full_scales_v"][role] = transition["readback_full_scale_v"]
            targets = {role: (codes[role], self.harmonic_control.expected_reserves[role])
                       for role in roles}
            final = self.harmonic_control._read(dict(zip(roles, self.pair)),
                                               record.setdefault("final_setting_checks", []))
            self.harmonic_control._check(final, self.harmonic_control.current_harmonics, targets)
            for role in roles:
                self.sensitivity_setup["ranges"][role]["current_sensitivity_code"] = codes[role]
            self.harmonic_control.fixed_range_profile = dict(normalized)
            self.range_profile = {"name": name, "ranges": dict(normalized)}
            record["verified"] = True
            self.event("lockin_range_rule_applied", deepcopy(record))
            if self.point_record is not None:
                self.point_record["lockin_range_rule"] = self.range_context()
        except BaseException as exc:
            self.range_profile_failed = True
            record["verified"] = False
            record["error"] = str(exc) or type(exc).__name__
            self.event("lockin_range_rule_failed", deepcopy(record))
            raise
        return self.range_context()

    def set_point(self, source_v, frequency_hz=None, *, point_index=None):
        self._require_range_profile_ready()
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
        if self.range_profile is not None:
            self.point_record["lockin_range_rule"] = self.range_context()
        try:
            if daily.model_name(xy) == "SR865A":
                self.harmonic_control.verify(xx, xy, self.harmonic_control.current_harmonics,
                                             self.point_record)
            daily._verify_frequency_readbacks(self.target_frequency_hz,
                xx.read_reference_frequency(), xy.read_reference_frequency(),
                rel_tolerance=daily.SWEEP_FREQUENCY_REL_TOLERANCE,
                absolute_tolerance_hz=daily.SWEEP_FREQUENCY_ABS_TOLERANCE_HZ)
            if target_hz != self.target_frequency_hz:
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
            # Source and internal-register readbacks are immediate; the external
            # reference is qualified after the shared final-setting deadline.
            if not daily.math.isclose(xx.read_sine_output(), source_v, rel_tol=0, abs_tol=1e-12):
                xx.set_sine_output(source_v)
        except BaseException:
            self.event("lockin_point_transition", self.point_record)
            raise
        if self.range_profile is None:
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

    def _read_coordinates(self, *, verify_companion=True):
        xx, xy = self.pair
        source = xx.read_sine_output()
        safety = daily._validate_excitation_safety(self.args, (source,))
        frequency, companion = xx.read_reference_frequency(), xy.read_reference_frequency()
        if verify_companion:
            daily._verify_frequency_readbacks(
                self.target_frequency_hz, frequency, companion,
                rel_tolerance=daily.SWEEP_FREQUENCY_REL_TOLERANCE,
                absolute_tolerance_hz=daily.SWEEP_FREQUENCY_ABS_TOLERANCE_HZ)
        else:
            daily._validate_frequency_observations(self.target_frequency_hz, frequency, companion)
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
        self._require_range_profile_ready()
        try:
            self.harmonic_control.enter(*self.pair,
                harmonic=self.harmonic_control.initial_harmonics(self.target_frequency_hz),
                settle_s=self.args.settle_s, record=self.point_record)
            self.harmonic_control.ensure_settled(*self.pair, self.args.settle_s, self.point_record)
            self._read_coordinates()
        except BaseException as exc:
            self.event("lockin_point_qualification_failed", {
                "error": str(exc) or type(exc).__name__, "point": self.point_record})
            raise
        self.event("lockin_point_qualification", self.point_record)
        self.sample_count = 0

    def disturbed(self, module):
        """Called on the same acquisition thread after another axis changes."""
        if self.harmonic_control is not None:
            self.harmonic_control.settling.changed(module + ".axis_setting")
            self.harmonic_control.open_transition_window(*self.pair,
                self.point_record if self.point_record is not None else self.sensitivity_setup)

    def _formal_pair_recorded(self, payload):
        if self.range_profile is not None:
            payload["lockin_range_rule"] = self.range_context()
        self.event("lockin_formal_pair", payload)

    def sample_point(self, *, measurement_context=None):
        self._require_range_profile_ready()
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
                frequency_rel_tolerance=daily.SWEEP_FREQUENCY_REL_TOLERANCE,
                harmonic_control=self.harmonic_control,
                measurement_context=measurement_context,
                on_formal_sample_recorded=self._formal_pair_recorded,
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
            frequency_rel_tolerance=daily.SWEEP_FREQUENCY_REL_TOLERANCE,
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
