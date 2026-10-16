"""Regression of the 594.4 nm failure; no hardware, all clocks injected."""
from dataclasses import replace
import math
import json
from pathlib import Path
import tempfile
import unittest

from attodry_control.nkt_config import NktError
from attodry_control.power_config import WindowConfig
from attodry_control.power_feedback import PowerWindow
from attodry_control.photoresponse_monitor import initial_progress, update_progress
from attodry_control.photoresponse_analysis import results_from_record
from attodry_control.photoresponse_power import PowerDeviation
from attodry_control.photoresponse_plotting import _record_rows
from attodry_control.unified_plotting import observation_column_label
from test_photoresponse_dark_scan import shared
from test_photoresponse_power import config_for, setup, ROOT
from test_photoresponse_speed import hardware_scan
from test_photoresponse_hardware import Resource
from attodry_control.photoresponse_devices import Meter2400
from attodry_control.photoresponse_transport import VisaTransport, Serial2182Transport
from attodry_control.photoresponse_config import load_photoresponse_config
from attodry_control.photoresponse_storage import PhotoStore
from attodry_control.photoresponse_plotting import load_photo_plot_rows
from attodry_control.photoresponse_monitor import photo_run_snapshot


def trial(targets=(55e-6, 65e-6, 75e-6), **changes):
    c = shared(config_for(), warm="previous", mode="open_circuit_voltage", bias_points=(None,),
               samples_per_window=3, max_abs_current_a=None)
    f = replace(c.power.feedback, target_powers_w=targets, target_tolerance_w=None,
                target_tolerance_fraction=.10, tuning_tolerance_fraction=.05,
                source_current_min_pct=26., source_current_max_pct=50., source_current_step_pct=3.,
                minimum_signal_power_w=.1e-6, max_power_w=.05, timeout_s=60., hold_s=.2,
                window=WindowConfig(1., 4, .25, 5e-6, .05, 0., .002))
    point = replace(c.nkt.points[0], wavelength_nm=594.4, bandwidth_nm=10., source_level_pct=36.5)
    return replace(c, nkt=replace(c.nkt, points=(point,), max_level_pct=100.),
        power=replace(c.power, feedback=f, power_points=("dark", *targets),
                      meter=replace(c.power.meter, max_power_w=.05), max_retries=2, point_timeout_s=180.), **changes)


def experiment(config=None):
    runner, backend, electrical, power, clock = setup(config=config or trial())
    power.meter.resource.power_provider = lambda: 44.2e-6 * math.exp((backend.source["level_pct"]-36.5)/3.)
    power.on_event = lambda event: runner._event(**event)
    return runner, backend, electrical, power, clock


def sample(index, time_s, value):
    return dict(valid=True, unit="W", power_w=value, sequence=index,
                started_monotonic_s=time_s, finished_monotonic_s=time_s)


class StabilityRegressionTests(unittest.TestCase):
    def test_actual_75uW_downward_trace_passed_legacy_but_fails_trend(self):
        trace = [47.282163e-6, 46.4849363e-6, 45.8648719e-6, 45.3598732e-6]
        old = WindowConfig(2., 4, .5, 5e-6)
        results = []
        for config in (old, replace(old, max_drift_fraction_per_s=.002)):
            window = PowerWindow(config, epoch_s=0, max_power_w=.05, target_power_w=75e-6)
            for i, value in enumerate(trace):
                result = window.add(sample(i, i*.7, value))
            results.append(result)
        self.assertTrue(results[0]["power_window_stable"])
        self.assertFalse(results[1]["power_window_stable"])
        self.assertIn("drift trend", results[1]["reason"])
        self.assertLess(results[1]["drift_w_per_s"], -0.8e-6)

    def test_both_drift_directions_fail_then_fresh_plateau_passes(self):
        for sign in (-1, 1):
            w = PowerWindow(WindowConfig(2, 3, .5, 5e-6, .1, 0, .002), epoch_s=0,
                            max_power_w=.05, target_power_w=5e-6)
            for i in range(5): r = w.add(sample(i, i*.5, 5e-6 + sign*i*.05e-6))
            self.assertFalse(r["power_window_stable"])
            for i in range(5, 11): r = w.add(sample(i, i*.5, 5e-6))
            self.assertTrue(r["power_window_stable"])

    def test_low_power_relative_variation_and_explicit_noise_floor(self):
        c = WindowConfig(1, 3, .5, 5e-6, .05, .05e-6)
        self.assertAlmostEqual(c.variation_limit_w(5e-6), .25e-6)
        self.assertAlmostEqual(c.variation_limit_w(.1e-6), .05e-6)
        w = PowerWindow(c, epoch_s=0, max_power_w=.05, target_power_w=5e-6)
        for i, p in enumerate((4.8e-6, 5.2e-6, 4.8e-6)): r = w.add(sample(i, i*.5, p))
        self.assertFalse(r["power_window_stable"])

    def test_overlapping_wide_formal_bands_still_seek_each_center(self):
        c = trial()
        c = replace(c, power=replace(c.power, feedback=replace(c.power.feedback, target_tolerance_fraction=.4)))
        runner, _, _, _, _ = experiment(c)
        record = runner.run()
        self.assertTrue(record["completed"], record["error"])
        for row in record["conditions"][1:]:
            self.assertTrue(row["power"]["adjustments"])
            self.assertLessEqual(abs(row["result"]["measured_power_w"] / row["target_power_w"] - 1), .05)
        self.assertEqual(record["conditions"][2]["power"]["initial_actuator"]["source"], "previous")

    def test_overshoot_reversal_fine_steps_and_bounds(self):
        runner, backend, _, power, _ = experiment(trial((55e-6,)))
        power.meter.resource.power_provider = lambda: 44.2e-6 * math.exp((backend.source["level_pct"]-36.5)*1.2)
        r = runner.run()
        self.assertTrue(r["completed"], r["error"])
        adjustments = r["conditions"][1]["power"]["adjustments"]
        self.assertTrue(any(a["direction_reversed"] for a in adjustments))
        self.assertTrue(any(a["control_phase"] == "fine_adjustment" for a in adjustments))
        for a in adjustments:
            self.assertLessEqual(abs(a["requested"] - a["before"]), 3.00001)
            self.assertTrue(26 <= a["requested"] <= 50)

    def test_unreachable_target_does_not_expand_limits(self):
        runner, backend, _, power, _ = experiment(trial((75e-6,)))
        power.meter.resource.power_provider = lambda: 20e-6
        r = runner.run()
        self.assertFalse(r["completed"])
        self.assertIn("boundary", r["error"])
        self.assertLessEqual(backend.source["level_pct"], 50)
        self.assertFalse(any(e["phase"] == "power_retry" for e in r["events"]))


class RecoveryTests(unittest.TestCase):
    def test_no_excitation_nonzero_relay_setting_is_rejected_without_repair_writes(self):
        runner, host, meter, _, _, _ = hardware_scan()
        host.values["SOUR:CURR"] = "1e-9"
        r = runner.run()
        self.assertFalse(r["completed"])
        self.assertIn("current setting is not zero", r["error"])
        self.assertEqual(meter.commands.count("READ?"), 0)
        self.assertNotIn("OUTP:STAT 1", host.commands)
        self.assertFalse(any(c.startswith("SOUR:CURR ") for c in host.commands))

    def inject(self, phase, *, always=False, failure=None):
        runner, backend, electrical, power, clock = experiment(trial((55e-6,)))
        original = power.meter.resource.power_provider
        count = []
        def provider():
            if power.row["samples"][-1]["phase"] == phase and (always or not count):
                count.append(phase)
                if failure: raise failure
                return 40e-6
            return original()
        power.meter.resource.power_provider = provider
        return runner, backend, electrical, power, clock, count

    def test_preacquisition_deviation_recovers_before_any_light_voltage(self):
        runner, _, electrical, _, _, _ = self.inject("preacquisition")
        reads = []
        original = electrical.read
        def read(**kw):
            reads.append(kw["illuminated"])
            return original(**kw)
        electrical.read = read
        r = runner.run()
        self.assertTrue(r["completed"], r["error"])
        self.assertEqual(reads, [False]*3 + [True]*3)
        attempts = r["conditions"][1]["power_attempts"]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]["windows"], [])
        self.assertTrue(attempts[0]["cleanup"]["electrical_safe_confirmed"])

    def test_mid_window_drift_discards_whole_window_and_keeps_dark(self):
        runner, _, electrical, _, _, _ = self.inject("light_after")
        reads = []
        original = electrical.read
        def read(**kw):
            row = original(**kw)
            reads.append(kw["illuminated"])
            row["voltage_v"] = len(reads)*1e-6
            return row
        electrical.read = read
        r = runner.run()
        self.assertTrue(r["completed"], r["error"])
        dark, light = r["conditions"]
        self.assertEqual(reads, [False]*3 + [True]*4)
        self.assertEqual(light["result"]["dark_condition_id"], dark["condition_id"])
        self.assertAlmostEqual(light["result"]["light_mean"], 6e-6)
        self.assertAlmostEqual(light["result"]["delta_voltage_v"], 4e-6)
        self.assertEqual(len(light["windows"]), 1)
        self.assertEqual(len(light["windows"][0]["samples"]), 3)
        failed = light["power_attempts"][0]["windows"][0]
        self.assertFalse(failed["valid"])
        self.assertTrue(all(not s["valid"] and not s["accepted"] for s in failed["samples"]))
        self.assertIsNotNone(failed["samples"][0]["raw_reading"])
        rows = results_from_record(r)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["light_mean"], 6e-6)
        plot = _record_rows(r, ROOT / "fake.json", audit=False)[1]
        self.assertEqual(plot["detector_sample_count"], 3)
        self.assertAlmostEqual(plot["actual.optical_power_w"], light["result"]["measured_power_w"])

    def test_retries_exhaust_and_run_cleans_up_without_fake_success(self):
        runner, _, _, _, _, hits = self.inject("light_after", always=True)
        r = runner.run()
        self.assertFalse(r["completed"])
        self.assertEqual(len(hits), 3)
        self.assertIn("attempts exhausted", r["error"])
        self.assertEqual(len(r["conditions"][1]["power_attempts"]), 3)
        self.assertFalse(results_from_record(r))
        self.assertTrue(r["cleanup"]["laser_off_confirmed"])
        self.assertTrue(r["cleanup"]["electrical_safe_confirmed"])

    def test_total_point_budget_covers_electrical_waits_and_stops(self):
        c = trial((55e-6,))
        c = replace(c, power=replace(c.power, point_timeout_s=3.))
        runner, _, electrical, _, clock = experiment(c)
        original = electrical.prepare
        def prepare(bias):
            result = original(bias)
            if runner.power.row["qualified"]: clock.sleep(5.)
            return result
        electrical.prepare = prepare
        r = runner.run()
        self.assertFalse(r["completed"])
        self.assertIn("time budget", r["error"])
        self.assertTrue(r["cleanup"]["laser_off_confirmed"])

    def test_hard_failure_is_never_an_ordinary_power_retry(self):
        for error in (OSError("communication unknown"), NktError("instrument protection")):
            runner, _, _, _, _, _ = self.inject("light_after", failure=error)
            r = runner.run()
            self.assertFalse(r["completed"])
            self.assertFalse(any(e["phase"] == "power_retry" for e in r["events"]))
        for power_w in (0., .06):
            runner, _, _, power, _ = experiment(trial((55e-6,)))
            original = power.meter.resource.power_provider
            power.meter.resource.power_provider = lambda: power_w if power.row["samples"][-1]["phase"] == "light_after" else original()
            r = runner.run()
            self.assertFalse(r["completed"])
            self.assertFalse(any(e["phase"] == "power_retry" for e in r["events"]))

    def test_retry_cleanup_failure_is_fatal(self):
        runner, _, electrical, _, _, _ = self.inject("light_after")
        original = electrical.cleanup
        def cleanup():
            if runner.power.row["optical_state"] == "light": raise NktError("shutdown not confirmed")
            return original()
        electrical.cleanup = cleanup
        r = runner.run()
        self.assertFalse(r["completed"])
        self.assertFalse(any(e["phase"] == "power_retry" for e in r["events"]))
        self.assertFalse(r["cleanup"]["electrical_safe_confirmed"])

    def test_monitor_exposes_phase_target_current_trend_attempt_and_cleanup(self):
        runner, _, _, _, _, _ = self.inject("light_after")
        r = runner.run()
        p = initial_progress(runner.config.snapshot())
        phases = set()
        for event in r["events"]:
            update_progress(p, event)
            phases.add(p["phase"])
        self.assertIn("waiting_stability", phases)
        self.assertIn("fine_adjustment", phases)
        self.assertIn("power_retry", phases)
        self.assertEqual(p["power_control"]["point_attempt"], 2)
        self.assertTrue(p["power_control"]["cleanup"]["electrical_safe_confirmed"])

    def test_plot_labels_monitor_position_and_retains_target(self):
        runner, _, _, _, _ = experiment(trial((55e-6,)))
        r = runner.run()
        rows = _record_rows(r, ROOT / "fake.json", audit=False)
        label = observation_column_label("actual.optical_power_w", rows)
        self.assertIn("PM100D", label)
        self.assertIn(runner.config.power.meter.measurement_plane, label)
        self.assertIn("not sample-calibrated", label)
        self.assertEqual(rows[1]["requested.optical_target_power_w"], 55e-6)

    def test_production_relay_retry_rechecks_settings_and_preserves_no_excitation(self):
        def policy(c):
            return replace(c, power=replace(c.power, max_retries=2, point_timeout_s=180.))
        runner, host, meter, electrical, power, _ = hardware_scan(config_transform=policy)
        original = power.meter.resource.power_provider
        hits = []
        def provider():
            if power.row["samples"][-1]["phase"] == "light_after" and not hits:
                hits.append(True)
                return .0006
            return original()
        power.meter.resource.power_provider = provider
        checks = []
        begin = electrical.begin
        def checked_begin():
            before = meter.commands.count("INIT:CONT?")
            result = begin()
            checks.append(meter.commands.count("INIT:CONT?")-before)
            return result
        electrical.begin = checked_begin
        r = runner.run()
        self.assertTrue(r["completed"], r["error"])
        self.assertEqual(checks, [2, 1, 1, 1]) # dark, light, recovery, next light
        self.assertEqual(meter.commands.count("READ?"), 10)
        self.assertNotIn("OUTP:STAT 1", host.commands)
        self.assertEqual(host.values["SOUR:CURR"], "0")
        self.assertEqual(electrical.detector.resource.poll_interval_s, .1)

    def test_2400_pre_enable_power_rejection_does_not_poison_device(self):
        resource = Resource("2400")
        device = Meter2400(VisaTransport(resource, role="detector", authorize_writes=True),
            expected_idn=resource.values["*IDN"], current_mode=True, max_voltage_v=1., max_current_a=1e-8,
            voltage_range_v=2., current_range_a=1e-6, compliance=1e-8, nplc=1., max_step_v=.1, settle_s=0., tolerance_v=1e-6,
            sleep=lambda _: None)
        def reject():
            self.assertEqual(resource.values["OUTP"], "0")
            raise PowerDeviation("fresh power deviation before enable")
        with self.assertRaises(PowerDeviation): device.begin(before_enable=reject)
        self.assertFalse(device.faulted)
        self.assertNotIn("OUTP 1", resource.commands)
        self.assertTrue(device.cleanup()["safe"])
        device.begin()
        self.assertTrue(device.ready)
        self.assertTrue(device.cleanup()["safe"])

    def test_settings_changed_during_recovery_force_full_recheck_and_stop(self):
        def policy(c): return replace(c, power=replace(c.power, max_retries=2, point_timeout_s=180.))
        runner, _, meter, _, power, _ = hardware_scan(config_transform=policy)
        original = power.meter.resource.power_provider
        failed = []
        def provider():
            if power.row["samples"][-1]["phase"] == "light_after" and not failed:
                failed.append(True)
                return .0006
            return original()
        power.meter.resource.power_provider = provider
        qualify = power.qualify
        def requalify(*args, **kwargs):
            result = qualify(*args, **kwargs)
            if failed: meter.values["SENS:CHAN"] = "1"
            return result
        power.qualify = requalify
        r = runner.run()
        self.assertFalse(r["completed"])
        self.assertIn("SENS:CHAN", r["error"])
        self.assertEqual(meter.commands.count("READ?"), 4) # dark + rejected window only
        self.assertTrue(r["cleanup"]["electrical_safe_confirmed"])

    def test_actual_15_98s_gap_and_44_24uW_readback_cannot_enter_acquisition(self):
        runner, _, electrical, power, clock = experiment(trial((75e-6,)))
        original = power.meter.resource.power_provider
        first = []
        def provider():
            if power.row["samples"][-1]["phase"] == "preacquisition" and not first:
                first.append(True)
                clock.sleep(15.9846466)
                return 44.2411874e-6
            return original()
        power.meter.resource.power_provider = provider
        r = runner.run()
        self.assertTrue(r["completed"], r["error"])
        rejected = r["conditions"][1]["power_attempts"][0]
        self.assertFalse(rejected["valid"])
        self.assertEqual(rejected["windows"], [])
        self.assertEqual(len(r["conditions"][1]["windows"][0]["samples"]), 3)

    def test_persisted_retry_monitor_and_plot_exclude_failed_window_by_default(self):
        runner, _, _, power, _, _ = self.inject("light_after")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = PhotoStore(root / "audit.sqlite")
            try:
                run_id, _ = store.start(runner.config)
                runner.on_event = store.event
                power.on_event = store.event
                r = runner.run()
                store.finalize(r)
                path = root / "record.json"
                path.write_text(json.dumps(r), encoding="utf-8")
                self.assertTrue(r["completed"], r["error"])
                regular = load_photo_plot_rows(path)
                self.assertEqual(regular, load_photo_plot_rows(store.path, run_id=run_id))
                self.assertEqual(len(regular), 2)
                self.assertEqual(regular[1]["detector_sample_count"], 3)
                audit = load_photo_plot_rows(path, audit=True)
                self.assertEqual(len(audit), 3)
                self.assertFalse(audit[-1]["accepted"])
                self.assertIn("Formal power drift", audit[-1]["power_retry_reason"])
                progress = photo_run_snapshot(store.path, run_id)
                self.assertEqual(progress["accepted_conditions"], 2)
                self.assertEqual(progress["power_control"]["point_attempt"], 2)
                self.assertIn("source_current_pct", progress["power_control"])
                self.assertTrue(progress["cleanup"]["clean"])
            finally:
                store.close()


class PolicyCompatibilityTests(unittest.TestCase):
    def test_monitor_displays_rejection_criterion_and_exact_attempt_cleanup(self):
        from attodry_control.combination_terminal import snapshot_text
        for reason in ("target deviation", "power variation", "drift trend"):
            progress = initial_progress(trial().snapshot())
            update_progress(progress, {"phase": "power_window_rejected", "assessment": {"reason": reason}})
            self.assertEqual(progress["power_control"]["reason"], reason)
            self.assertIn(reason, snapshot_text({**progress, "measurement_type": "dc_photoresponse", "status": "active"}))
            update_progress(progress, {"phase": "power_attempt_rejected", "point_attempt": 3,
                "reason": reason, "attempt": {"cleanup": {"electrical_safe_confirmed": True, "laser_off_confirmed": True}}})
            self.assertTrue(progress["power_control"]["cleanup"]["laser_off_confirmed"])
            self.assertEqual(progress["power_control"]["point_attempt"], 3)

    def test_legacy_policy_has_no_new_snapshot_keys_or_retry_semantics(self):
        c = shared(config_for())
        snapshot = c.snapshot()["power"]
        self.assertNotIn("max_retries", snapshot)
        self.assertNotIn("point_timeout_s", snapshot)
        self.assertNotIn("tuning_tolerance_fraction", snapshot["feedback"])
        self.assertNotIn("max_drift_fraction_per_s", snapshot["feedback"]["window"])
        f = c.power.for_target(1)
        self.assertEqual(f.tuning_tolerance_w(), f.target_tolerance_w)
        changed = replace(c, power=replace(c.power, max_retries=2, point_timeout_s=100.))
        self.assertNotEqual(c.snapshot(), changed.snapshot())

    def test_invalid_new_policy_rejected_before_io(self):
        c = trial()
        for power in (replace(c.power, max_retries=-1), replace(c.power, max_retries=11),
                      replace(c.power, point_timeout_s=None), replace(c.power, point_timeout_s=.1),
                      replace(c.power, feedback=replace(c.power.feedback, tuning_tolerance_fraction=.2)),
                      replace(c.power, feedback=replace(c.power.feedback,
                          window=replace(c.power.feedback.window, max_drift_fraction_per_s=-.1)))):
            with self.assertRaises(NktError): replace(c, power=power).validate()

    def test_response_history_is_not_reused_across_wavelengths(self):
        c = trial((55e-6,))
        c = replace(c, nkt=replace(c.nkt, points=(c.nkt.points[0], replace(c.nkt.points[0], wavelength_nm=630.))))
        runner, _, _, power, _ = experiment(c)
        original = power.prepare
        def prepare(condition, point):
            old_key = power._history_key
            original(condition, point)
            if old_key is not None and old_key != power._history_key:
                self.assertEqual(power._response_history, [])
        power.prepare = prepare
        r = runner.run()
        self.assertTrue(r["completed"], r["error"])
        self.assertEqual(r["conditions"][3]["power"]["initial_actuator"]["source"], "point")

    def test_public_trial_runs_offline_with_explicit_policies(self):
        c = load_photoresponse_config(ROOT / "config/photoresponse_stability_nkt_simulation.toml")
        runner, _, _, _, _ = setup(config=c)
        r = runner.run()
        self.assertTrue(r["completed"], r["error"])
        self.assertEqual(len(r["conditions"]), 5)
        self.assertEqual(c.power.feedback.window.duration_s, 5.)
        self.assertEqual(c.power.feedback.hold_s, 2.)
        self.assertEqual(c.power.max_retries, 2)

    def test_polling_default_stays_at_commissioned_100ms(self):
        r = Resource()
        self.assertEqual(Serial2182Transport(VisaTransport(r, role="relay")).poll_interval_s, .1)


if __name__ == "__main__":
    unittest.main()
