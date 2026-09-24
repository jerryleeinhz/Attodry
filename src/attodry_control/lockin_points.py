"""Single-owner excitation points using the daily SR830 safety engine.

No resource is opened by construction. Settings, bounded autorange and harmonic
sampling reuse lockin_test; a point never performs whole-sweep cleanup.
"""
from __future__ import annotations

from dataclasses import asdict

from . import lockin_test as daily
from .combination_store import utc_now
from .sr830 import DualSr830Controller


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
    def __init__(self, config_path, config, event, *, manager_factory=None):
        self.config = config
        self.args, self.settings, self.points, self.safety = (
            daily.prepare_configured_excitation_sweep(config_path, config)
        )
        self.event = event
        self.manager_factory = manager_factory
        self.context = None
        self.pair = None
        self.preflight = None
        self.writes_started = False
        self.sensitivity_setup = None
        self.reserve_setup = None
        self.fixed_setup = None
        self.frequency_setup = None
        self.point_record = None
        self.sample_count = 0
        self.harmonics_by_role = daily._requested_sweep_harmonics_by_role(self.args)
        self.harmonics = daily._requested_sweep_harmonics(self.args)
        # The electrical milestone is fixed-frequency excitation, not frequency scan.
        daily._validate_harmonic_detection_frequencies(
            (config.lockin_xx.frequency_hz,), self.harmonics
        )

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
            initial_full_scale_overrides=daily._initial_sweep_range_overrides(self.args.point_specs),
        )
        daily._configure_sweep_sensitivities(
            xx, xy, sensitivity_setup=self.sensitivity_setup, settle_s=self.args.settle_s)
        self.policies, self.states = daily._new_sweep_autorange_controls(
            self.config.lockin_xx, self.config.lockin_xy,
            sensitivity_setup=self.sensitivity_setup)
        self.event("lockin_configured", {"sensitivity": self.sensitivity_setup,
                                         "reserve": self.reserve_setup,
                                         "fixed": self.fixed_setup,
                                         "frequency": self.frequency_setup})

    def set_point(self, source_v):
        if source_v not in self.points:
            raise ValueError("Excitation point is outside the validated configured grid")
        index = self.points.index(source_v)
        xx, xy = self.pair
        daily._validate_excitation_safety(self.args, (source_v,))
        xx.set_sine_output(source_v)
        daily.time.sleep(daily.EXCITATION_SOURCE_STEP_SETTLE_INTERVALS * self.args.settle_s)
        self.point_record = {
            "point_index": index, "source_v_rms": source_v,
            "target_frequency_hz": self.config.lockin_xx.frequency_hz,
            "samples": [], "harmonic_transition_status": [],
        }
        daily._apply_sweep_segment_ranges(
            xx, xy, sensitivity_setup=self.sensitivity_setup,
            point_spec=self.args.point_specs[index], lockin_xx_config=self.config.lockin_xx,
            lockin_xy_config=self.config.lockin_xy, settle_s=self.args.settle_s,
            record=self.point_record,
        )
        self.sample_count = 0
        return self._read_coordinates()

    def _read_coordinates(self):
        xx, xy = self.pair
        source = xx.read_sine_output()
        safety = daily._validate_excitation_safety(self.args, (source,))
        frequency, companion = xx.read_reference_frequency(), xy.read_reference_frequency()
        daily._verify_frequency_readbacks(
            self.config.lockin_xx.frequency_hz, frequency, companion,
            rel_tolerance=1e-5, absolute_tolerance_hz=daily.SWEEP_FREQUENCY_ABS_TOLERANCE_HZ)
        self.point_record.update(source_readback_v_rms=source,
                                 actual_frequency_hz=frequency,
                                 frequency_readback_hz={"lockin_xx": frequency, "lockin_xy": companion},
                                 source_readback_safety=safety)
        return {"lockin_excitation_v_rms": source, "lockin_frequency_hz": frequency}

    def qualify(self):
        # Also when SMU is the inner axis: response/range may have changed.
        daily.time.sleep(daily.EXCITATION_SOURCE_STEP_SETTLE_INTERVALS * self.args.settle_s)
        actual = self._read_coordinates()
        daily._apply_sweep_autorange(
            *self.pair, sensitivity_setup=self.sensitivity_setup,
            policies=self.policies, states=self.states,
            target_frequency_hz=actual["lockin_frequency_hz"],
            frequency_rel_tolerance=1e-5, settle_s=self.args.settle_s,
            record=self.point_record,
        )
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
                harmonics=self.harmonics,
                selected_roles_by_harmonic=daily._roles_by_harmonic(
                    self.harmonics, self.harmonics_by_role),
                harmonic_settle_s=self.args.settle_s, samples=1,
                sample_interval_s=self.args.sample_interval_s, record=record,
                frequency_rel_tolerance=1e-5,
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
        return {"module": "lockin", "captured_at_utc": utc_now(), "actual": actual,
                "measurements": measured, "clean": True, "problems": [], "status": record}

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
            restore_frequency=False, settle_s=self.args.settle_s, writes_started=True,
            ignore_output_overload=True,
        )
        if (self.fixed_setup and self.fixed_setup.get("verified")
                and self.frequency_setup and self.frequency_setup.get("verified")
                and self.sensitivity_setup and self.reserve_setup):
            daily._verify_sweep_configured_cleanup(cleanup, self.config)
        return cleanup

    def close(self):
        if self.context is not None:
            self.context.__exit__(None, None, None)
