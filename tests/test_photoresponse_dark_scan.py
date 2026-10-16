"""Dark-first protocols, timing and safety tests; all instruments are fakes."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from attodry_control.nkt_config import NktError
from attodry_control.photoresponse import single_dark_difference
from attodry_control.photoresponse import PhotoresponseRunner
from attodry_control.photoresponse_cld import CldController, SimulatedCld
from attodry_control.photoresponse_hardware import HardwareElectrical
from attodry_control.photoresponse_power import PhotoresponsePower
from attodry_control.photoresponse_analysis import load_results, export_results
from attodry_control.photoresponse_config import load_photoresponse_config
from attodry_control.photoresponse_storage import PhotoStore
from attodry_control.photoresponse_transport import VisaTransport, Serial2182Transport
from attodry_control.pm100d import Pm100d, SimulatedPmResource
from test_photoresponse import Clock
from test_photoresponse_power import config_for, setup, ROOT
from test_photoresponse_integration import hardware_setup, environment_config
from test_photoresponse_hardware import Resource
from test_photoresponse_smu import configuration as smu_config, setup as smu_setup


ORDER = ("repeat", "environment", "optical", "target_power_w", "bias", "smu")


def shared(config=None, *, warm="point", **changes):
    c = config or config_for()
    return replace(c, power=replace(c.power, power_points=("dark", *c.power.feedback._targets()),
                   initial_actuator_mode=warm), combination_order=ORDER, **changes)


class DarkScanTests(unittest.TestCase):
    def test_production_hardware_path_with_serial_latency_and_2400_output(self):
        for mode, detector, relay in (("dc_current_voltage", "keithley2182a", True),
                                      ("voltage_bias_current", "keithley2400", False)):
            h, host, meter, _, _ = hardware_setup(mode, detector, relay)
            base = shared(config_for("cld"))
            pm_config = replace(base.power.meter, backend="visa", resource="FAKE::PM::INSTR",
                                expected_serial="SIMULATION", expected_sensor_serial="SIMULATION")
            c = replace(base, nkt=h.config.nkt, hardware=h.config.hardware, mode=mode, detector=detector,
                        bias_points=h.config.bias_points, max_abs_current_a=h.config.max_abs_current_a,
                        max_abs_voltage_v=h.config.max_abs_voltage_v, samples_per_window=1)
            c = replace(c, power=replace(c.power, meter=pm_config))
            clock = Clock()
            backend = SimulatedCld(c.nkt)
            backend.is_hardware = True
            laser = CldController(c.nkt, backend, clock=clock, sleep=clock.sleep)
            transports = {"6221": VisaTransport(host, role="6221", authorize_writes=True),
                          "detector": VisaTransport(meter, role="detector", authorize_writes=True)}
            electrical = HardwareElectrical(c, transports, sleep=clock.sleep, clock=clock)
            pm = Pm100d(pm_config, SimulatedPmResource(lambda: .002 * backend.state["getLdCurrSetpoint"] / .03),
                        is_hardware=True, authorize_settings=True, authorize_measurement=True, clock=clock)
            power = PhotoresponsePower(c.power, laser, pm, clock=clock, sleep=clock.sleep)
            runner = PhotoresponseRunner(c, laser, electrical, power=power, clock=clock, sleep=clock.sleep,
                                        authorize_writes=True, confirm_manual_route=True)
            record = runner.run()
            self.assertTrue(record["completed"], record["error"] or record["cleanup"])
            self.assertEqual(sum(t["phase"] == "power_qualification" for t in record["timings"]),
                             len(c.nkt.points) * (len(c.power.targets) - 1))
            if relay:
                # Full serial-detector verification would destroy a .02 s PM
                # window if it were still nested in each power guard.
                self.assertGreater(clock.value, 100010.)
            self.assertEqual(host.values["OUTP:STAT"], "0")
            self.assertEqual(meter.values["OUTP"], "0")

    def test_one_dark_scan_per_optical_visit_and_coordinate_differences(self):
        c = shared(samples_per_window=2)
        runner, _, electrical, power, _ = setup(config=c)
        original = electrical.read
        def read(**kw):
            row = original(**kw)
            row["voltage_v"] = row["state"]["source"]["setpoint"] * 1000 + (5e-6 if kw["illuminated"] else 0)
            return row
        electrical.read = read
        record = runner.run()
        self.assertTrue(record["completed"], record["error"] or record["cleanup"])
        dark = [r for r in record["conditions"] if r["optical_state"] == "dark"]
        light = [r for r in record["conditions"] if r["optical_state"] == "light"]
        self.assertEqual((len(dark), len(light)), (4, 8))
        for r in light:
            self.assertEqual(len(r["windows"]), 1)
            self.assertAlmostEqual(r["result"]["delta_voltage_v"], 5e-6)
            baseline = next(d for d in dark if d["condition_id"] == r["result"]["dark_condition_id"])
            self.assertEqual(baseline["bias_index"], r["bias_index"])
            self.assertEqual(baseline["optical_index"], r["optical_index"])
        self.assertEqual(sum(t["phase"] == "power_qualification" for t in record["timings"]), 4)
        self.assertTrue(all(not s["sample"]["measurement_performed"] for d in dark for s in d["power"]["samples"]))

    def test_power_qualification_always_precedes_output_enable_and_leaves_hold_light(self):
        runner, _, electrical, power, _ = setup(config=shared())
        original = power.qualify
        def qualify(guard, **kwargs):
            self.assertFalse(electrical.state["source"]["output_enabled"])
            return original(guard, **kwargs)
        power.qualify = qualify
        original_read = electrical.read
        def read(**kw):
            if kw["illuminated"]:
                self.assertTrue(power.row["qualified"])
            return original_read(**kw)
        electrical.read = read
        self.assertTrue(runner.run()["completed"])

    def test_full_repeats_requalify_environment_and_take_new_dark(self):
        c = shared(environment=environment_config(), repeats=2, samples_per_window=1)
        runner, _, _, _, _ = setup(config=c)
        with patch.object(runner.environment, "prepare", wraps=runner.environment.prepare) as prepare:
            record = runner.run()
        self.assertTrue(record["completed"], record["error"])
        self.assertEqual(prepare.call_count, len(c.environment.points) * c.repeats)
        self.assertEqual(sum(r["optical_state"] == "dark" for r in record["conditions"]),
                         len(c.environment.points) * c.repeats * len(c.nkt.points) * len(c.bias_points))
        self.assertTrue(record["conditions"][0]["windows"][0]["samples"][0]["environment_after"]["quality"]["field_within_window"])

    def test_warm_start_reduces_steps_and_never_crosses_wavelength_visit(self):
        results = []
        c = load_photoresponse_config(ROOT / "config/photoresponse_power_nkt_power_mode_simulation.toml")
        c = replace(c, power=replace(c.power, feedback=replace(c.power.feedback, target_powers_w=(.0012, .0016))))
        for mode in ("point", "previous"):
            runner, _, _, _, _ = setup(config=shared(c, warm=mode))
            record = runner.run()
            self.assertTrue(record["completed"], record["error"])
            sessions = {tuple(map(tuple, r["power_visit"])): r["power"] for r in record["conditions"]}
            results.append(sum(len(p["adjustments"]) for p in sessions.values()))
            if mode == "previous":
                for key, p in sessions.items():
                    self.assertEqual(p["initial_actuator"]["source"], "previous" if key[-1][1] == 2 else "point")
        self.assertLess(results[1], results[0])

    def test_cld_and_2400_current_modes_have_signed_differences(self):
        for kind in ("nkt", "cld"):
            c = shared(config_for(kind), mode="voltage_bias_current", detector="keithley2400", bias_points=(0., .001))
            runner, _, _, _, _ = setup(kind, config=c)
            record = runner.run()
            self.assertTrue(record["completed"], record["error"])
            self.assertTrue(all("delta_current_a" in r["result"] for r in record["conditions"] if r["optical_state"] == "light"))

    def test_smu_coordinates_are_matched_and_sources_suspended_during_tuning(self):
        runner, smu, _, _, _ = smu_setup(shared(smu_config(short=False), samples_per_window=1))
        original = runner.power.qualify
        def qualify(*args, **kwargs):
            self.assertFalse(smu.begun)
            return original(*args, **kwargs)
        runner.power.qualify = qualify
        record = runner.run()
        self.assertTrue(record["completed"], record["error"] or record["cleanup"])
        for row in record["conditions"]:
            if row["optical_state"] == "light":
                dark = next(d for d in record["conditions"] if d["condition_id"] == row["result"]["dark_condition_id"])
                self.assertEqual(dark["requested_smu"], row["requested_smu"])

    def test_remaining_settle_credits_qualification_but_resets_on_emission_change(self):
        c = shared(mode="open_circuit_voltage", bias_points=(None,), settle_s=.05)
        runner, _, _, power, clock = setup(config=c)
        original = power.qualify
        def qualify(*a, **kw):
            result = original(*a, **kw)
            clock.sleep(.02)
            kw["on_emission_changed"]()
            clock.sleep(.01)
            return result
        power.qualify = qualify
        record = runner.run()
        self.assertTrue(record["completed"], record["error"])
        light = next(r for r in record["conditions"] if r["optical_state"] == "light")
        self.assertAlmostEqual(light["power"]["settling_wait"]["sleep_requested_s"], .04, places=7)
        self.assertEqual(light["settling_wait"]["sleep_requested_s"], 0.)

    def test_dark_failure_blocks_all_lights_and_retains_rejected_raw(self):
        runner, _, electrical, _, _ = setup(config=shared())
        electrical.read = lambda **kw: (_ for _ in ()).throw(OSError("dark failed"))
        record = runner.run()
        self.assertFalse(record["completed"])
        self.assertEqual(len(record["conditions"]), 1)
        self.assertFalse(record["conditions"][0]["windows"][0]["samples"][0]["valid"])
        self.assertTrue(record["cleanup"]["laser_off_confirmed"])
        self.assertTrue(record["cleanup"]["electrical_safe_confirmed"])

    def test_light_drift_rejects_without_retuning_and_cleanup_failure_stays_rejected(self):
        for fault in ("drift", "cleanup"):
            runner, _, electrical, power, _ = setup(config=shared())
            if fault == "drift":
                original = electrical.read
                def read(**kw):
                    result = original(**kw)
                    if kw["illuminated"]:
                        power.meter.resource.power_provider = lambda: 0.
                    return result
                electrical.read = read
            else:
                original = electrical.cleanup
                count = [0]
                def cleanup():
                    count[0] += 1
                    if count[0] == 2:
                        raise OSError("transition cleanup failure")
                    return original()
                electrical.cleanup = cleanup
            record = runner.run()
            self.assertFalse(record["completed"])
            self.assertFalse(any(r["accepted"] for r in record["conditions"]))

    def test_analysis_csv_sqlite_and_resume_keep_baseline_provenance(self):
        c = shared(samples_per_window=1)
        record = setup(config=c)[0].run()
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "record.json"
            path.write_text(json.dumps(record))
            self.assertEqual(len(load_results(path)), 8)
            self.assertEqual(len(load_results(path, include_dark=True)), 12)
            self.assertEqual(export_results(path, Path(td) / "results.csv")["rows"], 8)
            store = PhotoStore(Path(td) / "data.sqlite")
            store.start(c)
            store.finalize(record)
            self.assertEqual(store.db.execute("select count(*) from photo_conditions where accepted=1").fetchone()[0], 12)
            store.close()
            record["cleanup"]["errors"] = ["failed close"]
            path.write_text(json.dumps(record))
            self.assertEqual(load_results(path), [])
            self.assertEqual(len(load_results(path, include_rejected=True)), 8)

    def test_resume_starts_a_fresh_dark_and_mismatched_pair_is_rejected(self):
        c = shared(samples_per_window=1)
        runner, _, electrical, _, _ = setup(config=c)
        electrical.read = lambda **kw: (_ for _ in ()).throw(OSError("failed dark"))
        with tempfile.TemporaryDirectory() as td:
            store = PhotoStore(Path(td) / "data.sqlite")
            original_id, _ = store.start(c)
            store.finalize(runner.run())
            _, attempt = store.start(c, resume=original_id)
            runner = setup(config=c)[0]
            runner.attempt_index = attempt
            record = runner.run()
            store.finalize(record)
            self.assertEqual(attempt, 2)
            self.assertTrue(record["completed"], record["error"])
            self.assertEqual(record["conditions"][0]["optical_state"], "dark")
            dark, light = record["conditions"][0], record["conditions"][2]
            light["baseline_key"] = ["different visit"]
            with self.assertRaises(NktError): single_dark_difference(dark, light, c.quantity)
            store.close()

    def test_final_close_failure_rejects_all_raw_data(self):
        runner, _, _, power, _ = setup(config=shared())
        power.meter.resource.close = lambda: (_ for _ in ()).throw(OSError("failed PM close"))
        record = runner.run()
        self.assertFalse(record["completed"])
        self.assertTrue(record["cleanup"]["errors"])
        self.assertTrue(all(not s["accepted"] for r in record["conditions"] for w in r["windows"] for s in w["samples"]))


class ConfigAndTimingTests(unittest.TestCase):
    def test_old_config_snapshot_keeps_resume_hash_shape(self):
        c = config_for()
        snapshot = c.snapshot()
        self.assertNotIn("dark_recovery_s", snapshot)
        self.assertNotIn("power_points", snapshot["power"])
        self.assertNotIn("initial_actuator_mode", snapshot["power"])

    def test_source_ramp_wait_is_credited_since_last_actual_change(self):
        from attodry_control.photoresponse_devices import CurrentSource6221
        clock, resource = Clock(), Resource()
        source = CurrentSource6221(VisaTransport(resource, role="source", authorize_writes=True),
            expected_idn=resource.values["*IDN"], max_current_a=1e-8, compliance_v=.1,
            max_step_a=2.5e-9, settle_s=.2, tolerance_a=1e-13, sleep=clock.sleep, clock=clock)
        source.begin()
        source.set_bias(2.5e-9)
        self.assertAlmostEqual(clock() - source.last_change_monotonic_s, .2)
        previous = source.last_change_monotonic_s
        source.set_bias(2.5e-9)
        self.assertEqual(source.last_change_monotonic_s, previous)
        runner = setup(config=shared())[0]
        runner.clock, runner.sleep = clock, clock.sleep
        destination = {}
        runner._settle(previous, .3, destination)
        self.assertAlmostEqual(destination["settling_wait"]["sleep_requested_s"], .1)

    def test_power_points_strict_loading_and_order(self):
        text = (ROOT / "config/photoresponse_power_nkt_power_mode_simulation.toml").read_text()
        text = text.replace('order = ["optical", "target_power_w", "bias", "repeat"]', 'order = ["repeat", "environment", "optical", "target_power_w", "bias", "smu"]')
        text = text.replace('target_powers_w = [0.0008, 0.0012]', 'power_points = ["dark", 0.0008, 0.0012]\ninitial_actuator_mode = "previous"')
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "config.toml"
            for value in ('["dark", 0.0008, 0.0012]', '[0.0008, "dark"]', '["dark", 0.0]', '["dark", "dark"]', '["dark"]'):
                path.write_text(text.replace('["dark", 0.0008, 0.0012]', value))
                if value == '["dark", 0.0008, 0.0012]':
                    self.assertTrue(load_photoresponse_config(path).shared_dark)
                else:
                    with self.assertRaises(NktError): load_photoresponse_config(path)
        for order in (("bias", "repeat", "environment", "optical", "target_power_w", "smu"),
                      ("environment", "optical", "target_power_w", "bias", "smu", "repeat")):
            with self.assertRaises(NktError): replace(shared(), combination_order=order).validate()

    def test_voltage_read_uses_two_full_verifications_and_guard_has_no_serial_queries(self):
        _, host, meter, electrical, _ = hardware_setup()
        electrical.begin()
        meter.commands.clear()
        electrical.guard()
        self.assertEqual(meter.commands, [])
        electrical.read(illuminated=False)
        self.assertEqual(meter.commands.count("*IDN?"), 2)
        host.values["OUTP:STAT"] = "1"
        with self.assertRaises(NktError): electrical.guard()

    def test_source_unsafe_status_blocks_before_dc_begin(self):
        _, host, _, electrical, _ = hardware_setup()
        host.values["STAT:QUES:COND"] = "1"
        with self.assertRaises(NktError): electrical.guard()

    def test_2400_guard_retains_complete_settings_checks(self):
        _, _, meter, electrical, _ = hardware_setup("voltage_bias_current", "keithley2400", False)
        electrical.begin()
        meter.values["SENS:CURR:RANG"] = ".01"
        with self.assertRaises(NktError): electrical.guard()

    def test_transport_timings_include_failure_but_exclude_event_callback(self):
        clock, resource = Clock(), Resource()
        def query(command):
            clock.sleep(.2)
            if command == "OUTP?": raise OSError("bus failure")
            return "1"
        resource.query = query
        transport = VisaTransport(resource, role="fake", clock=clock, on_event=lambda e: clock.sleep(.5))
        transport.query("*IDN?")
        with self.assertRaises(OSError): transport.query("OUTP?")
        for row in transport.transcript:
            self.assertAlmostEqual(row["duration_s"], .2)
        self.assertIn("error", transport.transcript[-1])

    def test_relay_counts_poll_wait_and_noop_pm_prepare_captures_once(self):
        clock, host = Clock(), Resource()
        host.downstream = Resource("2182A")
        relay = Serial2182Transport(VisaTransport(host, role="fake"), sleep=clock.sleep, clock=clock)
        relay.query("*IDN?")
        self.assertAlmostEqual(relay.transcript[0]["duration_s"], .1)
        self.assertEqual(relay.transcript[0]["poll_wait_requested_s"], .1)
        config = config_for().power.meter
        meter = Pm100d(config, SimulatedPmResource(), clock=clock)
        meter.prepare(633.)
        meter.resource.commands.clear()
        meter.prepare(633.)
        self.assertEqual(meter.resource.commands.count(("query", "*IDN?")), 1)
