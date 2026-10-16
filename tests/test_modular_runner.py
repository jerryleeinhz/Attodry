"""Modular production coordinator acceptance with synthetic controllers only."""
from copy import deepcopy
from collections import deque
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import tomllib
import unittest
from unittest.mock import patch

from attodry_control.modular_config import parse_modular_config
from attodry_control.modular_environment import EnvironmentAxes
from attodry_control.modular_runner import ModularRunner, SourceOnlyElectrical, run_modular
from attodry_control.modular_runtime import RuntimeConfig
from attodry_control.nkt_config import NktError
from attodry_control.nkt_control import NktController, SimulatedNkt
from attodry_control.optics_points import OpticsSession
from attodry_control.photoresponse import PhotoresponseRunner, SimulatedElectrical
from attodry_control.photoresponse_hardware import HardwareElectrical
from attodry_control.photoresponse_power import PowerDeviation, open_photoresponse_power
from attodry_control.photoresponse_transport import VisaTransport
from test_photoresponse import Clock
from test_photoresponse_hardware import Resource
from test_photoresponse_integration import hardware_setup


ROOT = Path(__file__).resolve().parents[1]


def document(order=("optics", "current_source", "nanovoltmeter"), *, repeats=1, wavelengths=(750.,)):
    doc = tomllib.loads((ROOT / "config/photoresponse_dark_nkt_simulation.toml").read_text())
    measurement = doc.pop("photoresponse")
    measurement = {k: v for k, v in measurement.items() if k in {
        "sample_interval_s", "settle_s", "dark_recovery_s", "max_abs_voltage_v", "max_abs_current_a"}}
    doc.pop("photoresponse_combination")
    doc["power_feedback"].pop("power_points")
    doc["power_feedback"].update(max_retries=2, point_timeout_s=100.)
    doc["combination_scan"] = {"order": list(order), "samples_per_condition": 2,
        "repeats": repeats, "backend": "simulation", "run_name": "modular_test",
        "output_directory": str(ROOT / "run_data/modular_runner_tests")}
    doc["measurement"] = measurement
    doc["optics"] = {"laser": "nkt", "use_pem": False, "use_power_meter": True,
                     "mode": "power_stabilized", "initial_source_level_pct": 5.}
    doc["optics_scan"] = {"order": ["wavelength", "bandwidth", "power"],
        "wavelength_points_nm": list(wavelengths), "bandwidth_points_nm": [20.],
        "power_points_w": [.0008, .0012], "dark_first": True}
    doc["current_source_scan"] = {"points_a": [0., 1e-6]}
    doc["nanovoltmeter"] = {"model": "keithley2182a", "channel": 2, "nplc": 1., "voltage_range_v": .1}
    if {"temperature", "magnetic"} & set(order):
        env = tomllib.loads((ROOT / "config/photoresponse_environment_simulation.toml").read_text())
        doc.update(env)
        doc["environment"] = doc.pop("photoresponse_environment")
        doc["environment"].pop("points")
        doc["temperature_scan"] = {"points_k": [1.6, 2.]}
        doc["magnetic_scan"] = {"points": [{"bx_t": 0., "bz_t": 0.}, {"bx_t": .01, "bz_t": .02}]}
    return doc


def runtime(doc):
    return RuntimeConfig(parse_modular_config(doc, ROOT / "run_data/modular_runner_tests/source.toml"))


def setup(doc=None, *, electrical_class=SimulatedElectrical, on_event=None):
    config = runtime(document() if doc is None else doc)
    clock = Clock()
    electrical = electrical_class(config, clock=clock)
    optics, backend = None, None
    if "optics" in config.plan.order:
        backend = SimulatedNkt(config.nkt)
        laser = NktController(config.nkt, backend, clock=clock, sleep=clock.sleep)
        power = open_photoresponse_power(config.power, laser, clock=clock, sleep=clock.sleep) if config.power else None
        optics = OpticsSession(laser, power=power, clock=clock, sleep=clock.sleep,
                               settle_s=config.settle_s, dark_recovery_s=config.dark_recovery_s)
    environment = EnvironmentAxes(config.environment) if config.environment else None
    runner = ModularRunner(config, electrical, optics=optics, environment=environment,
                           clock=clock, sleep=clock.sleep, on_event=on_event)
    return runner, electrical, optics, environment, backend, clock


class ModularRunnerTests(unittest.TestCase):
    def assert_completed(self, record):
        self.assertTrue(record["completed"], record.get("error") or record["cleanup"])
        self.assertEqual(record["outcome"], "completed")
        self.assertTrue(record["cleanup"]["laser_off_confirmed"])
        self.assertTrue(record["cleanup"]["electrical_safe_confirmed"])
        self.assertEqual(record["cleanup"]["errors"], [])
        self.assertTrue(all(row["accepted"] for row in record["conditions"]))

    def test_power_outer_bias_inner_matches_each_electrical_dark(self):
        runner, electrical, optics, _, backend, _ = setup()
        record = runner.run()
        self.assert_completed(record)
        self.assertEqual(len(record["conditions"]), 6)
        darks = {row["requested_current_a"]: row for row in record["conditions"] if row["optical_state"] == "dark"}
        self.assertEqual(len(darks), 2)
        for row in record["conditions"]:
            self.assertEqual(len(row["windows"][0]["samples"]), 2)
            if row["optical_state"] == "light":
                self.assertEqual(row["result"]["dark_condition_id"], darks[row["requested_current_a"]]["condition_id"])
                self.assertGreater(row["result"]["delta_voltage_v"], 0.)
                self.assertEqual(row["result"]["target_power_w"], row["target_power_w"])
            for sample in row["windows"][0]["samples"]:
                modules = {r["module"] for r in sample["module_readings"]}
                self.assertEqual(modules, set(runner.plan.order))
        self.assertEqual(len(optics.record["points"]), 3)
        self.assertFalse(electrical.state["source"]["output_enabled"])
        self.assertEqual(backend.source["emission_state"], 0)

    def test_bias_outer_repeated_wave_and_round_have_fresh_darks(self):
        doc = document(("current_source", "optics", "nanovoltmeter"), repeats=2, wavelengths=(750., 750.))
        runner, _, optics, _, _, _ = setup(doc)
        record = runner.run()
        self.assert_completed(record)
        self.assertEqual(len(record["conditions"]), 24)
        darks = {row["condition_id"]: row for row in record["conditions"] if row["optical_state"] == "dark"}
        self.assertEqual(len(darks), 8)
        for row in record["conditions"]:
            if row["optical_state"] == "light":
                dark = darks[row["result"]["dark_condition_id"]]
                self.assertEqual(row["baseline_key"], dark["baseline_key"])
                self.assertEqual(row["requested_current_a"], dark["requested_current_a"])
                self.assertEqual(row["repeat_index"], dark["repeat_index"])
        self.assertEqual(len(optics.record["points"]), 24)

    def test_independent_temperature_field_dwell_only_at_axis_entries(self):
        doc = document(("temperature", "magnetic", "optics", "nanovoltmeter"), repeats=2)
        runner, _, _, environment, _, _ = setup(doc)
        record = runner.run()
        self.assert_completed(record)
        self.assertEqual(len(record["conditions"]), 24)
        events = environment.driver.event_log
        self.assertEqual(events.count("cryostat.wait_for_temperature"), 4)
        self.assertEqual(events.count("cryostat.wait_for_field"), 8)
        self.assertGreater(events.count("cryostat.read_state"), 24)
        for row in record["conditions"]:
            sample = row["windows"][0]["samples"][0]
            self.assertEqual(sample["environment_after"]["measurement_temperature_k"], row["requested"]["temperature_k"])
            self.assertEqual(sample["environment_after"]["field"]["bz_t"], row["requested"]["field_z_t"])
        self.assertTrue(environment.driver.closed)

    def test_no_optics_nanovoltmeter_uses_no_laser_or_pm_factory(self):
        config = runtime(document(("nanovoltmeter",)))
        with patch("attodry_control.nkt_control.SimulatedNkt", side_effect=AssertionError("unused laser")), \
             patch("attodry_control.photoresponse_power.open_photoresponse_power", side_effect=AssertionError("unused PM")), \
             patch("attodry_control.photoresponse_transport.visa_resources", side_effect=AssertionError("unused VISA")):
            record = run_modular(config)
        self.assert_completed(record)
        self.assertEqual(len(record["conditions"]), 1)
        row = record["conditions"][0]
        self.assertEqual(row["result"]["method"], "detector_reading_v1")
        self.assertEqual({r["module"] for r in row["actual"]}, {"nanovoltmeter"})
        self.assertNotIn("optics_audit", record)

    def test_no_detector_source_only_uses_settings_not_measured_current(self):
        runner, electrical, _, _, _, _ = setup(document(("current_source",)))
        record = runner.run()
        self.assert_completed(record)
        self.assertEqual(len(record["conditions"]), 2)
        self.assertFalse(any(command[0] == "read" for command in electrical.commands))
        for row in record["conditions"]:
            self.assertIsNone(row["result"]["quantity"])
            for sample in row["windows"][0]["samples"]:
                self.assertIsNone(sample["voltage_v"])
                self.assertIsNone(sample["current_a"])
                source = sample["module_readings"][0]
                self.assertEqual(source["module"], "current_source")
                self.assertIsNone(source["measurements"]["current_a"])
                self.assertTrue(source["status"]["setpoint_is_not_measured_current"])

    def test_no_detector_temperature_and_field_only_run(self):
        record = run_modular(runtime(document(("temperature", "magnetic"))))
        self.assert_completed(record)
        self.assertEqual(len(record["conditions"]), 4)
        for row in record["conditions"]:
            self.assertEqual({r["module"] for r in row["actual"]}, {"temperature", "magnetic"})
            self.assertIsNone(row["result"]["quantity"])

    def test_actual_readbacks_are_not_replaced_with_requested_values(self):
        runner, electrical, _, _, _, _ = setup(document(("nanovoltmeter",)))
        original = electrical.read
        def actual(**kwargs):
            reading = original(**kwargs)
            reading["voltage_v"] = 7e-6
            reading["state"]["detector_readback"] = {"actual_nplc": 1., "acquisition_sequence": len(electrical.commands)}
            return reading
        electrical.read = actual
        record = runner.run()
        self.assert_completed(record)
        self.assertEqual(record["conditions"][0]["result"]["detector_mean"], 7e-6)
        for sample in record["conditions"][0]["windows"][0]["samples"]:
            self.assertEqual(sample["module_readings"][0]["measurements"]["voltage_v"], 7e-6)

    def test_recoverable_formal_drift_repeats_whole_window_and_same_dark(self):
        runner, electrical, optics, _, _, _ = setup(document(("optics", "nanovoltmeter")))
        original = optics.power.formal_sample
        injected = []
        def drift(phase, **kwargs):
            sample = original(phase, **kwargs)
            if phase == "light_after" and not injected:
                injected.append(True)
                raise PowerDeviation("synthetic formal drift")
            return sample
        optics.power.formal_sample = drift
        record = runner.run()
        self.assert_completed(record)
        dark, first, second = record["conditions"]
        self.assertEqual(len(first["power_attempts"]), 2)
        failed, succeeded = first["power_attempts"]
        self.assertFalse(failed["valid"])
        self.assertTrue(succeeded["valid"])
        self.assertEqual(len(failed["windows"][0]["samples"]), 1)
        self.assertFalse(failed["windows"][0]["samples"][0]["valid"])
        self.assertEqual(len(first["windows"][0]["samples"]), 2)
        self.assertEqual(first["result"]["dark_condition_id"], dark["condition_id"])
        self.assertEqual(second["result"]["dark_condition_id"], dark["condition_id"])
        self.assertEqual(len([r for r in record["conditions"] if r["optical_state"] == "dark"]), 1)
        self.assertTrue(failed["cleanup"]["laser_off_confirmed"])
        self.assertTrue(any(e["phase"] == "power_retry" for e in record["events"]))

    def test_ordinary_hardware_error_does_not_enter_power_retry(self):
        runner, _, optics, _, _, _ = setup(document(("optics", "nanovoltmeter")))
        original = optics.power.formal_sample
        def fault(phase, **kwargs):
            sample = original(phase, **kwargs)
            if phase == "light_before":
                raise NktError("synthetic protection fault")
            return sample
        optics.power.formal_sample = fault
        record = runner.run()
        self.assertFalse(record["completed"])
        self.assertIn("protection fault", record["error"])
        self.assertFalse(any(e["phase"] == "power_retry" for e in record["events"]))
        self.assertTrue(record["cleanup"]["laser_off_confirmed"])
        self.assertTrue(record["cleanup"]["electrical_safe_confirmed"])
        self.assertTrue(all(not row["accepted"] for row in record["conditions"]))

    def test_preacquisition_drift_requalifies_before_any_light_voltage(self):
        runner, electrical, optics, _, _, _ = setup(document(("optics", "nanovoltmeter")))
        original = optics.preacquisition_check
        observations = []
        def drift(*args, **kwargs):
            evidence = original(*args, **kwargs)
            if optics.point.optical_state == "light" and not observations:
                observations.append(len([c for c in electrical.commands if c == ("read", True)]))
                raise PowerDeviation("synthetic preacquisition drift")
            return evidence
        optics.preacquisition_check = drift
        record = runner.run()
        self.assert_completed(record)
        self.assertEqual(observations, [0])
        first = record["conditions"][1]
        self.assertEqual(len(first["power_attempts"]), 2)
        self.assertEqual(first["power_attempts"][0]["windows"], [])
        self.assertEqual(len(first["windows"][0]["samples"]), 2)

    def test_audit_error_preserves_raw_and_prevents_retry_or_promotion(self):
        failed = []
        def event(row):
            if row["phase"] == "raw_sample" and not failed:
                failed.append(True)
                raise OSError("synthetic audit write failed")
        runner, _, _, _, _, _ = setup(document(("optics", "nanovoltmeter")), on_event=event)
        record = runner.run()
        self.assertFalse(record["completed"])
        self.assertIn("audit write failed", record["error"])
        self.assertFalse(any(e["phase"] == "power_retry" for e in record["events"]))
        sample = record["conditions"][0]["windows"][0]["samples"][0]
        self.assertIn("raw_reading", sample)
        self.assertIn("audit_error", sample)
        self.assertFalse(sample["accepted"])

    def test_source_only_guard_checks_compliance_drift(self):
        resource = Resource()
        settings = {"expected_idn": resource.values["*IDN"], "address": "FAKE::SOURCE::INSTR",
                    "compliance_v": .1, "max_step_a": 2.5e-9, "settle_s": 0., "tolerance_a": 1e-13}
        config = SimpleNamespace(hardware=SimpleNamespace(source=settings), max_abs_current_a=1e-8)
        electrical = SourceOnlyElectrical(config, {"6221": VisaTransport(resource, role="source", authorize_writes=True)})
        electrical.begin()
        electrical.prepare(1e-9)
        resource.values["SOUR:CURR:COMP"] = ".2"
        with self.assertRaisesRegex(NktError, "compliance setting changed"):
            electrical.guard()

    def test_retry_limit_is_finite_and_preserves_all_invalid_windows(self):
        doc = document(("optics", "nanovoltmeter"))
        doc["power_feedback"]["max_retries"] = 1
        runner, _, optics, _, _, _ = setup(doc)
        original = optics.power.formal_sample
        def drift(phase, **kwargs):
            sample = original(phase, **kwargs)
            if phase == "light_after":
                raise PowerDeviation("always drifts")
            return sample
        optics.power.formal_sample = drift
        record = runner.run()
        self.assertFalse(record["completed"])
        self.assertIn("exhausted", record["error"])
        failed = record["conditions"][1]
        self.assertEqual(len(failed["power_attempts"]), 2)
        self.assertEqual(failed["windows"], [])
        self.assertTrue(all(not attempt["valid"] and attempt["windows"] for attempt in failed["power_attempts"]))
        self.assertTrue(record["cleanup"]["laser_off_confirmed"])

    def test_cleanup_still_runs_electrical_after_optics_failure(self):
        runner, electrical, optics, _, _, _ = setup(document(("optics", "nanovoltmeter")))
        original = optics.cleanup
        def cleanup():
            original()
            raise OSError("synthetic optics close failure")
        optics.cleanup = cleanup
        record = runner.run()
        self.assertFalse(record["completed"])
        self.assertTrue(record["cleanup"]["electrical_safe_confirmed"])
        self.assertTrue(any("optics close failure" in e for e in record["cleanup"]["errors"]))

    def test_cleanup_electrical_failure_does_not_prevent_environment_close(self):
        runner, electrical, _, environment, _, _ = setup(document(("temperature", "nanovoltmeter")))
        electrical.cleanup = lambda: (_ for _ in ()).throw(OSError("synthetic electrical cleanup failure"))
        record = runner.run()
        self.assertFalse(record["completed"])
        self.assertTrue(environment.driver.closed)
        self.assertFalse(record["cleanup"]["electrical_safe_confirmed"])
        self.assertTrue(any("electrical cleanup failure" in e for e in record["cleanup"]["errors"]))

    def test_session_is_single_use(self):
        runner, _, _, _, _, _ = setup(document(("nanovoltmeter",)))
        self.assert_completed(runner.run())
        with self.assertRaisesRegex(NktError, "single-use"):
            runner.run()


class LegacyFilterBoundaryTests(unittest.TestCase):
    def test_moving_history_restarts_at_every_dark_light_dark_window(self):
        prior, host, meter, _, backend = hardware_setup("dc_current_voltage", relay=True, channel=2)
        hardware = replace(prior.config.hardware, detector={**prior.config.hardware.detector,
            "digital_filter": {"enabled": True, "type": "moving", "count": 3, "window_percent": 10.}})
        config = replace(prior.config, hardware=hardware)
        clock = prior.laser.clock
        electrical = HardwareElectrical(config, {"6221": VisaTransport(host, role="relay", authorize_writes=True)},
                                        sleep=clock.sleep, clock=clock)
        meter.values["STAT:OPER:COND"] = "1280"
        original = meter.query
        history = deque([.009] * 3, maxlen=3)
        def moving(command):
            raw = original(command)
            if command == "READ?":
                history.append(6e-6 if backend.state["getLdOutputState"] else 1e-6)
                return str(sum(history) / len(history))
            return raw
        meter.query = moving
        runner = PhotoresponseRunner(config, prior.laser, electrical, clock=clock, sleep=clock.sleep,
                                     authorize_writes=True, confirm_manual_route=True)
        record = runner.run()
        self.assertTrue(record["completed"], record.get("error") or record["cleanup"])
        generations = []
        windows = [window for row in record["conditions"] for window in row["windows"]]
        for window in windows:
            transition = window["filter_transition"]
            self.assertTrue(transition["complete"])
            self.assertEqual(len(transition["readings"]), 2)
            self.assertTrue(all(not raw["valid"] for raw in transition["readings"]))
            generations.append(transition["condition_generation"])
            expected = 6e-6 if window["phase"] == "light" else 1e-6
            self.assertAlmostEqual(window["samples"][0]["voltage_v"], expected)
        self.assertEqual(len(generations), len(set(generations)))
        self.assertEqual(meter.commands.count("READ?"), len(windows) * 3)
        self.assertTrue(record["cleanup"]["laser_off_confirmed"])
        self.assertTrue(record["cleanup"]["electrical_safe_confirmed"])


if __name__ == "__main__":
    unittest.main()
