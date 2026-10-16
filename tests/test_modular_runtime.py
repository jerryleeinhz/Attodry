"""Selected-only device contracts and independent environment visits, all offline."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tomllib
import unittest
from unittest.mock import patch

from attodry_control.combination_plan import AxisPoint
from attodry_control.models import VectorField
from attodry_control.modular_config import parse_modular_config
from attodry_control.modular_environment import EnvironmentAxes, load_environment_axes
from attodry_control.modular_runtime import RuntimeConfig
from attodry_control.nkt_config import NktError
from attodry_control.photoresponse_smu import PhotoresponseSmu
from test_optics_points import pem_config


ROOT = Path(__file__).resolve().parents[1]


def document(kind="nkt", *, feedback=False, source=False):
    path = ROOT / (f"config/photoresponse_power_{kind}_simulation.toml" if feedback
                   else "config/photoresponse_simulation.toml" if kind == "nkt"
                   else "config/photoresponse_cld_simulation.toml")
    doc = tomllib.loads(path.read_text())
    doc.pop("photoresponse_combination", None)
    doc["combination_scan"] = {"order": ["optics", "nanovoltmeter"], "backend": "simulation",
                               "samples_per_condition": 2, "repeats": 1,
                               "output_directory": "../run_data/modular-runtime-tests"}
    doc["optics"] = {"laser": "nkt" if kind == "nkt" else "cld1010lp", "use_pem": False,
                     "use_power_meter": feedback, "mode": "power_stabilized" if feedback else "direct"}
    doc["optics_scan"] = {"dark_first": True}
    if kind == "nkt":
        doc["optics_scan"].update(wavelength_points_nm=[630., 640.], bandwidth_points_nm=[10.])
        doc["optics"]["initial_source_level_pct"] = 5.
    else:
        doc["optics"]["initial_current_a"] = .01
    if feedback:
        doc["optics_scan"]["power_points_w"] = [.0008, .0012]
        for key in ("target_power_w", "target_powers_w", "power_points"):
            doc["power_feedback"].pop(key, None)
    doc["nanovoltmeter"] = {"model": "keithley2182a", "channel": 2, "voltage_range_v": 10., "nplc": 1.}
    doc["measurement"] = {"max_abs_voltage_v": 1., "max_abs_current_a": 1e-8,
                          "settle_s": .01, "sample_interval_s": .01}
    if source:
        doc["combination_scan"]["order"].insert(-1, "current_source")
        doc["current_source_scan"] = {"points_a": [0., 1e-8]}
    return doc, path


def environment_document(*modules):
    raw = tomllib.loads((ROOT / "config/photoresponse_environment_simulation.toml").read_text())
    raw["environment"] = raw.pop("photoresponse_environment")
    raw["environment"].pop("points")
    return raw


class RuntimeContractTests(unittest.TestCase):
    def test_direct_nkt_pm_feedback_and_cld_runtime_grids(self):
        for kind in ("nkt", "cld"):
            for feedback in (False, True):
                with self.subTest(kind=kind, feedback=feedback):
                    doc, path = document(kind, feedback=feedback)
                    config = RuntimeConfig(parse_modular_config(doc, path))
                    count = (2 if kind == "nkt" else 1) * (3 if feedback else 2)
                    self.assertEqual(config.plan.condition_count, count)
                    self.assertEqual(len(config.nkt.points), count)
                    self.assertEqual(config.power is not None, feedback)
                    self.assertTrue(config.shared_dark)
                    self.assertEqual(config.excitation, "none")
                    self.assertIsNone(config.hardware)
                    snapshot = json.loads(json.dumps(config.snapshot(), allow_nan=False))
                    self.assertEqual(snapshot["power"] is not None, feedback)
                    if feedback:
                        self.assertEqual(config.power.power_points, ("dark", .0008, .0012))

    def test_no_dark_multiple_wave_feedback_uses_explicit_local_target_indices(self):
        from attodry_control.nkt_control import NktController, SimulatedNkt
        from attodry_control.photoresponse_power import open_photoresponse_power
        from attodry_control.optics_points import OpticsSession, OpticsPoint
        from test_photoresponse import Clock
        doc, path = document(feedback=True)
        doc["optics_scan"]["dark_first"] = False
        config = RuntimeConfig(parse_modular_config(doc, path))
        clock = Clock()
        laser = NktController(config.nkt, SimulatedNkt(config.nkt), clock=clock, sleep=clock.sleep)
        power = open_photoresponse_power(config.power, laser, clock=clock, sleep=clock.sleep)
        optics = OpticsSession(laser, power=power, clock=clock, sleep=clock.sleep)
        optics.open()
        self.assertEqual(len(config.nkt.points), 4)
        self.assertEqual(config.power.targets, (.0008, .0012))
        for i, item in enumerate(config.axis("optics").points):
            optics.prepare(OpticsPoint(config.nkt.points[i], "light", item.values["optical_target_power_w"],
                                      item.metadata["power_index"], item.metadata["optical_group_index"], i))
            optics.qualify()
            optics.preacquisition_check()
            optics.begin_sample()
            optics.end_sample()
            optics.finish_point()
            self.assertEqual(power.feedback.target_power_w, item.values["optical_target_power_w"])
        self.assertTrue(optics.cleanup()["verified"])

    def test_paired_and_power_outer_feedback_keep_original_power_indices(self):
        from attodry_control.nkt_control import NktController, SimulatedNkt
        from attodry_control.photoresponse_power import open_photoresponse_power
        from attodry_control.optics_points import OpticsSession, OpticsPoint
        from test_photoresponse import Clock
        for scan_options, expected in (({"pattern": "paired"}, [.0008, .0012]),
                                       ({"order": ["power", "wavelength", "bandwidth"]},
                                        [.0008, .0008, .0012, .0012])):
            with self.subTest(scan_options=scan_options):
                doc, path = document(feedback=True)
                doc["optics_scan"].update(dark_first=False, **scan_options)
                config = RuntimeConfig(parse_modular_config(doc, path))
                self.assertEqual(config.power.targets, (.0008, .0012))
                clock = Clock()
                laser = NktController(config.nkt, SimulatedNkt(config.nkt), clock=clock, sleep=clock.sleep)
                power = open_photoresponse_power(config.power, laser, clock=clock, sleep=clock.sleep)
                optics = OpticsSession(laser, power=power, clock=clock, sleep=clock.sleep)
                optics.open()
                actual = []
                for i, item in enumerate(config.axis("optics").points):
                    optics.prepare(OpticsPoint(config.nkt.points[i], "light", item.values["optical_target_power_w"],
                        item.metadata["power_index"], item.metadata["optical_group_index"], i))
                    optics.qualify()
                    actual.append(power.feedback.target_power_w)
                    optics.begin_sample()
                    optics.end_sample()
                    optics.finish_point()
                self.assertEqual(actual, expected)
                self.assertTrue(optics.cleanup()["verified"])

    def test_detector_only_has_no_laser_requirement_or_instantiation(self):
        doc = {"combination_scan": {"order": ["nanovoltmeter"], "backend": "simulation",
                                   "output_directory": "../run_data/modular-runtime-tests"},
               "measurement": {"max_abs_voltage_v": 1.},
               "nanovoltmeter": {"channel": 2, "voltage_range_v": 10., "nplc": 1.}}
        with patch("attodry_control.modular_runtime.load_nkt_config", side_effect=AssertionError("unselected laser")):
            config = RuntimeConfig(parse_modular_config(doc))
        self.assertEqual(config.detector, "keithley2182a")
        self.assertFalse(config.shared_dark)
        self.assertIsNone(config.snapshot()["nkt"])
        self.assertEqual(config.nkt.backend, "simulation")

    def test_current_source_with_and_without_detector(self):
        doc, path = document(source=True)
        doc["combination_scan"]["order"] = ["current_source", "nanovoltmeter"]
        config = RuntimeConfig(parse_modular_config(doc, path))
        self.assertEqual(config.bias_points, (0., 1e-8))
        self.assertEqual(config.quantity, "voltage_v")
        doc["combination_scan"]["order"] = ["current_source"]
        config = RuntimeConfig(parse_modular_config(doc, path))
        self.assertEqual(config.detector, "none")
        self.assertIsNone(config.quantity)
        self.assertIsNone(config.snapshot()["nkt"])

    def test_simulation_still_rejects_invalid_selected_detector_settings(self):
        for setting in ({"nplc": 0.}, {"channel": 2, "voltage_range_v": 100.},
                        {"digital_filter": {"enabled": True, "count": 101}},
                        {"analog_lowpass_enabled": "yes"}, {"unknown_setting": 1}):
            doc, path = document()
            doc["nanovoltmeter"].update(setting)
            with self.subTest(setting=setting), self.assertRaises((NktError, TypeError)):
                RuntimeConfig(parse_modular_config(doc, path))

    def test_unselected_invalid_device_settings_are_ignored(self):
        doc, path = document()
        doc["combination_scan"]["order"] = ["optics"]
        doc["nanovoltmeter"] = {"unknown_setting": "inactive"}
        doc["current_source"] = {"address": "invalid inactive"}
        config = RuntimeConfig(parse_modular_config(doc, path))
        self.assertFalse(config.detector_selected)
        self.assertIsNone(config.hardware)

    def test_sample_current_bound_and_hardware_source_compliance_without_meter(self):
        for current_bound, compliance in ((.106, .1), (1e-8, 1.1)):
            doc, path = document(source=True)
            doc["combination_scan"].update(order=["current_source"], backend="hardware")
            doc["measurement"]["max_abs_current_a"] = current_bound
            doc["current_source"] = {"address": "FAKE::6221::INSTR", "expected_idn": "FAKE,MODEL 6221,1,1",
                                     "compliance_v": compliance, "max_step_a": 1e-8,
                                     "settle_s": 0., "tolerance_a": 0.}
            doc["photoresponse_hardware"] = {"timeout_ms": 10000, "wiring_confirmed": True}
            with self.subTest(current_bound=current_bound), self.assertRaises(NktError):
                RuntimeConfig(parse_modular_config(doc, path))

    def test_hardware_source_only_is_validated_and_does_not_open(self):
        doc, path = document(source=True)
        doc["combination_scan"].update(order=["current_source"], backend="hardware")
        doc["current_source"] = {"address": "FAKE::6221::INSTR", "expected_idn": "FAKE,MODEL 6221,1,1",
                                 "compliance_v": .1, "max_step_a": 1e-8,
                                 "settle_s": 0., "tolerance_a": 0.}
        doc["photoresponse_hardware"] = {"timeout_ms": 10000, "wiring_confirmed": True}
        config = RuntimeConfig(parse_modular_config(doc, path))
        config.require_run()
        self.assertEqual(config.hardware.addresses(), {"6221": "FAKE::6221::INSTR"})
        self.assertEqual(config.nkt.backend, "unselected")

    def test_independent_temperature_and_field_runtime_snapshot_is_json(self):
        doc, path = document()
        doc.update(environment_document())
        doc["combination_scan"]["order"] = ["temperature", "magnetic", "nanovoltmeter"]
        doc["temperature_scan"] = {"points_k": [1.6, 2.]}
        doc["magnetic_scan"] = {"points": [{"bx_t": 0., "bz_t": 0.},
                                                 {"bx_t": .01, "bz_t": .02}]}
        config = RuntimeConfig(parse_modular_config(doc, path))
        self.assertEqual(config.environment.modules, ("temperature", "magnetic"))
        self.assertEqual(config.plan.condition_count, 4)
        json.dumps(config.snapshot(), allow_nan=False)

    def test_pem_capabilities_backend_and_resources_are_validated_offline(self):
        doc, path = document()
        doc["optics"].update(use_pem=True, peak_retardance_waves=.25)
        doc["pem"] = dict(pem_config().__dict__)
        config = RuntimeConfig(parse_modular_config(doc, path))
        self.assertEqual(config.pem, pem_config())
        self.assertEqual(config.snapshot()["pem"]["finish_action"], "disable_ack")
        doc["pem"]["wavelength_max_nm"] = 600.
        with self.assertRaisesRegex(NktError, "PEM wavelength"):
            RuntimeConfig(parse_modular_config(doc, path))
        doc["pem"]["wavelength_max_nm"] = 900.
        doc["pem"]["backend"] = "serial"
        with self.assertRaisesRegex(NktError, "PEM backend"):
            RuntimeConfig(parse_modular_config(doc, path))

    def test_source_only_smu_bias_uses_source_readback_without_external_detector(self):
        path = ROOT / "config/photoresponse_smu_cld_simulation.toml"
        doc = tomllib.loads(path.read_text())
        doc.pop("photoresponse_combination")
        doc["combination_scan"] = {"order": ["smu"], "backend": "simulation",
                                   "output_directory": "../run_data/modular-runtime-tests"}
        doc["smu_scan"] = {"gate_top_points_v": [0.], "gate_bottom_points_v": [0.],
                           "smu_bias_points_a": [1e-9]}
        config = RuntimeConfig(parse_modular_config(doc, path))
        self.assertIsNone(config.quantity)
        self.assertEqual(config.detector, "none")
        self.assertEqual(config.mode, "smu_current_voltage")
        smu = PhotoresponseSmu(config, simulated=True, sleep=lambda _: None)
        smu.begin()
        state = smu.prepare(config.smu.points[0], lambda: None)
        self.assertIn("smu_bias", state["readings"])
        smu.finish(failed=False)

    def test_selected_smu_reuses_pinned_engine_points_and_keeps_readbacks_separate(self):
        path = ROOT / "config/photoresponse_smu_nkt_simulation.toml"
        doc = tomllib.loads(path.read_text())
        doc.pop("photoresponse_combination")
        doc["combination_scan"] = {"order": ["smu", "nanovoltmeter"], "backend": "simulation",
                                   "output_directory": "../run_data/modular-runtime-tests"}
        doc["smu_scan"] = {"gate_top_points_v": [0.], "gate_bottom_points_v": [-.001, .001]}
        config = RuntimeConfig(parse_modular_config(doc, path))
        self.assertEqual(config.smu.roles, ("gate_top", "gate_bottom"))
        self.assertEqual([p.coordinates["gate_bottom"] for p in config.smu.points], [-.001, .001])
        smu = PhotoresponseSmu(config, simulated=True, sleep=lambda _: None)
        smu.begin()
        state = smu.prepare(config.smu.points[0], lambda: None)
        self.assertIsNotNone(state)
        self.assertEqual(config.quantity, "voltage_v")
        smu.finish(failed=False)

    def test_smu_rejects_different_source_document_instead_of_reloading_other_limits(self):
        path = ROOT / "config/photoresponse_smu_nkt_simulation.toml"
        doc = tomllib.loads(path.read_text())
        doc.pop("photoresponse_combination")
        doc["combination_scan"] = {"order": ["smu"], "backend": "simulation",
                                   "output_directory": "../run_data/modular-runtime-tests"}
        doc["smu_scan"] = {"gate_top_points_v": [0.], "gate_bottom_points_v": [0.]}
        doc["gate_top"]["max_abs_voltage_v"] = .02
        with self.assertRaisesRegex(NktError, "source TOML differs"):
            RuntimeConfig(parse_modular_config(doc, path))


class EnvironmentalVisitTests(unittest.TestCase):
    def test_field_only_requires_no_temperature_policy_and_ignores_inactive_values(self):
        doc = environment_document()
        doc.pop("temperature_stability")
        doc["environment"] = {"max_field_step_t": .01, "normal_end_field_policy": "hold"}
        config = load_environment_axes(doc, ("magnetic",), hardware=False)
        self.assertIsNone(config.temperature_stability)
        self.assertIsNone(config.max_temperature_step_k)
        self.assertIsNone(config.max_temperature_overshoot_k)
        doc["temperature_stability"] = {"invalid_inactive_setting": "unused"}
        doc["environment"].update(max_temperature_step_k="unused", max_temperature_overshoot_k=False)
        self.assertEqual(load_environment_axes(doc, ("magnetic",), hardware=False), config)
        self.assertIsNone(config.snapshot()["temperature_stability"])

    def test_temperature_only_requires_no_field_policy_but_retains_field_bounds(self):
        doc = environment_document()
        doc["environment"] = {"max_temperature_step_k": 1., "max_temperature_overshoot_k": .2}
        doc["magnet"] = {key: doc["magnet"][key] for key in
                         ("hardware_x_max_t", "hardware_z_max_t", "experiment_vector_max_t")}
        config = load_environment_axes(doc, ("temperature",), hardware=False)
        self.assertIsNone(config.max_field_step_t)
        self.assertIsNone(config.magnet.stability)
        self.assertEqual(config.normal_end_field_policy, "hold")
        self.assertEqual(config.magnet.limits.hardware_z_max_t, 9.)
        doc["magnet"].update(stable_dwell_s="unused", field_tolerance_t=False)
        doc["environment"].update(max_field_step_t=False, normal_end_field_policy="unused")
        self.assertEqual(load_environment_axes(doc, ("temperature",), hardware=False), config)
        self.assertIsNone(config.snapshot()["magnet"]["stability"])
        doc["magnet"]["experiment_vector_max_t"] = 3.1
        with self.assertRaises((ValueError, NktError)):
            load_environment_axes(doc, ("temperature",), hardware=False)

    def test_public_field_only_run_without_temperature_table_never_controls_temperature(self):
        from attodry_control.modular_runner import run_modular
        from attodry_control.photoresponse_environment import open_environment
        doc = environment_document()
        doc.pop("temperature_stability")
        doc["environment"] = {"max_field_step_t": .01, "normal_end_field_policy": "hold"}
        doc["combination_scan"] = {"order": ["magnetic"], "backend": "simulation",
            "samples_per_condition": 2, "output_directory": "../run_data/modular-runtime-tests"}
        doc["magnetic_scan"] = {"points": [{"bx_t": 0., "bz_t": .01}, {"bx_t": .01, "bz_t": .02}]}
        config = RuntimeConfig(parse_modular_config(doc))
        driver = open_environment(config.environment).driver
        with patch("attodry_control.modular_environment.open_environment", return_value=type("Owned", (), {"driver": driver})()):
            record = run_modular(config)
        self.assertTrue(record["completed"], record["error"] or record["cleanup"])
        self.assertEqual(len(record["conditions"]), 2)
        self.assertFalse(any("temperature" in operation for operation in driver.event_log))
        self.assertEqual(driver.event_log.count("cryostat.wait_for_field"), 2)
        self.assertFalse(record["cleanup"]["environment"]["field_zero_confirmed"])

    def open(self, *modules):
        config = load_environment_axes(environment_document(), modules, hardware=False)
        controller = EnvironmentAxes(config)
        controller.begin()
        self.addCleanup(controller.finish, failed=False)
        return controller, controller.driver.event_log

    def test_each_axis_visit_qualifies_once_inner_guards_never_wait(self):
        env, log = self.open("temperature", "magnetic")
        t = AxisPoint({"temperature_k": 2.})
        b = AxisPoint({"field_x_t": .01, "field_z_t": .02})
        env.prepare_axis("temperature", t, (0, 0))
        env.prepare_axis("magnetic", b, (0, 0, 0))
        for _ in range(5):
            env.verify()
            env.prepare_axis("temperature", t, (0, 0))
            env.prepare_axis("magnetic", b, (0, 0, 0))
        self.assertEqual(log.count("cryostat.wait_for_temperature"), 1)
        self.assertEqual(log.count("cryostat.wait_for_field"), 1)
        env.prepare_axis("temperature", t, (1, 0))
        env.prepare_axis("magnetic", b, (1, 0, 0))
        self.assertEqual(log.count("cryostat.wait_for_temperature"), 2)
        self.assertEqual(log.count("cryostat.wait_for_field"), 2)

    def test_temperature_only_never_writes_field_even_failed_cleanup(self):
        env, log = self.open("temperature")
        env.prepare_axis("temperature", AxisPoint({"temperature_k": 2.}), (0,))
        result = env.finish(failed=True)
        self.assertFalse(result["field_zero_confirmed"])
        self.assertFalse(result["manual_verification_required"])
        self.assertFalse(any("field" in op for op in log))

    def test_magnetic_only_never_writes_temperature(self):
        env, log = self.open("magnetic")
        env.prepare_axis("magnetic", AxisPoint({"field_x_t": .01, "field_z_t": .02}), (0,))
        env.finish(failed=True)
        self.assertIn("cryostat.request_zero_field", log)
        self.assertFalse(any("temperature" in op for op in log))

    def test_fault_keeps_last_confirmed_field_and_requires_manual_without_zero_write(self):
        env, log = self.open("magnetic")
        env.prepare_axis("magnetic", AxisPoint({"field_x_t": .01, "field_z_t": .02}), (0,))
        confirmed = deepcopy(env.last_confirmed_state)
        env.driver.fail_next("read_state")
        with self.assertRaises(Exception):
            env.verify()
        self.assertFalse(env.state_current)
        self.assertEqual(env.last_confirmed_state, confirmed)
        result = env.finish(failed=True)
        self.assertFalse(result["field_zero_confirmed"])
        self.assertTrue(result["manual_verification_required"])
        self.assertEqual(result["last_confirmed_state"]["field"], {"bx_t": .01, "bz_t": .02})
        self.assertNotIn("cryostat.request_zero_field", log)

    def test_wrong_visit_coordinate_and_field_over_three_t_fail_before_write(self):
        env, log = self.open("temperature", "magnetic")
        env.prepare_axis("temperature", AxisPoint({"temperature_k": 2.}), (0,))
        before = list(log)
        with self.assertRaisesRegex(NktError, "cannot change"):
            env.prepare_axis("temperature", AxisPoint({"temperature_k": 2.1}), (0,))
        self.assertEqual(log, before)
        with self.assertRaises(ValueError):
            env.prepare_axis("magnetic", AxisPoint({"field_x_t": 3., "field_z_t": .1}), (0,))
        self.assertNotIn("cryostat.set_vector_field", log)


if __name__ == "__main__":
    unittest.main()
