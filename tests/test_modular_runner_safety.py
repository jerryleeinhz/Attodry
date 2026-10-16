"""Hardware-mode coordinator safety using explicitly injected in-memory devices."""
from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import patch

from attodry_control.modular_config import parse_modular_config
from attodry_control.modular_runner import ModularRunner, SourceOnlyElectrical, run_modular
from attodry_control.modular_runtime import RuntimeConfig, SourceHardware
from attodry_control.nkt_config import NktError
from attodry_control.optics_points import OpticsSession
from attodry_control.photoresponse_cld import CldController, SimulatedCld
from attodry_control.photoresponse import SimulatedElectrical
from attodry_control.photoresponse_hardware import HardwareElectrical
from attodry_control.photoresponse_devices import Voltmeter2182
from attodry_control.photoresponse_power import PhotoresponsePower, PowerDeviation
from attodry_control.photoresponse_transport import VisaTransport
from attodry_control.pm100d import Pm100d, SimulatedPmResource
from test_modular_runtime import document, environment_document
from test_optics_points import pem_config
from test_photoresponse import Clock
from test_photoresponse_integration import hardware_setup
from test_photoresponse_speed import hardware_scan


def hardware_modular(*, current_source=False, on_event=None):
    """Reuse the existing hardware fake contracts; never invoke a factory."""
    legacy, host, meter, _, _ = hardware_setup(
        "dc_current_voltage" if current_source else "open_circuit_voltage", channel=2)
    old, _, _, _, _, _ = hardware_scan()
    doc, path = document("cld", feedback=True, source=current_source)
    doc["combination_scan"]["samples_per_condition"] = 3
    doc["measurement"].update(max_abs_voltage_v=.1, max_abs_current_a=1e-8 if current_source else None)
    if not current_source:
        doc["measurement"].pop("max_abs_current_a")
    config = RuntimeConfig(parse_modular_config(doc, path))
    config.hardware_mode = True
    config.nkt = replace(legacy.config.nkt, points=tuple(legacy.config.nkt.points[0] for _ in config.axis("optics").points))
    config.hardware = legacy.config.hardware
    config.power = replace(old.config.power, meter=replace(old.config.power.meter,
        backend="visa", resource="FAKE::PM::INSTR", expected_serial="SIMULATION", expected_sensor_serial="SIMULATION"))
    clock = Clock()
    backend = SimulatedCld(config.nkt)
    backend.is_hardware = True
    laser = CldController(config.nkt, backend, clock=clock, sleep=clock.sleep)
    transports = {"6221": VisaTransport(host, role="6221", authorize_writes=True)}
    electrical = HardwareElectrical(config, transports, clock=clock, sleep=clock.sleep)
    meter_resource = SimulatedPmResource(lambda: .002 * backend.state["getLdCurrSetpoint"] / .03)
    pm = Pm100d(config.power.meter, meter_resource, is_hardware=True,
        authorize_settings=True, authorize_measurement=True, clock=clock)
    power = PhotoresponsePower(config.power, laser, pm, clock=clock, sleep=clock.sleep)
    optics = OpticsSession(laser, power=power, clock=clock, sleep=clock.sleep)
    runner = ModularRunner(config, electrical, optics=optics, clock=clock, sleep=clock.sleep, on_event=on_event)
    return runner, host, meter, electrical, optics, clock


class HardwareCoordinatorSafetyTests(unittest.TestCase):
    def test_successful_moving_filter_attempts_keep_distinct_retry_history(self):
        runner, _, meter, electrical, optics, clock = hardware_modular()
        hardware = replace(runner.config.hardware, detector={**runner.config.hardware.detector,
            "digital_filter": {"enabled": True, "type": "moving", "count": 3}})
        runner.config.hardware = hardware
        electrical.detector = Voltmeter2182(electrical.detector.resource,
            **{k: v for k, v in hardware.detector.items() if k != "address"},
            max_voltage_v=runner.config.max_abs_voltage_v, clock=clock)
        meter.values["STAT:OPER:COND"] = "1280"
        runner.config.power = replace(runner.config.power, max_retries=1, point_timeout_s=600.)
        optics.power.config = runner.config.power
        original = optics.power.formal_sample
        injected = []
        def drift(phase, **kwargs):
            sample = original(phase, **kwargs)
            if phase == "light_after" and not injected:
                injected.append(True)
                raise PowerDeviation("injected post-filter formal drift")
            return sample
        optics.power.formal_sample = drift
        record = runner.run()
        self.assertTrue(record["completed"], record["error"] or record["cleanup"])
        dark, first, second = record["conditions"]
        failed, success = first["power_attempts"]
        failed_transition = failed["filter_transition"]
        success_transition = success["filter_transition"]
        self.assertFalse(failed["valid"])
        self.assertTrue(success["valid"])
        self.assertTrue(failed_transition["complete"])
        self.assertTrue(success_transition["complete"])
        self.assertNotEqual(failed_transition["condition_generation"], success_transition["condition_generation"])
        self.assertIsNot(success_transition, first["filter_transition"])
        self.assertEqual(success_transition, first["filter_transition"])
        for row in (dark, first, second):
            self.assertEqual(len(row["filter_transition"]["readings"]), 2)
            self.assertTrue(all(not sample["valid"] for sample in row["filter_transition"]["readings"]))
        self.assertEqual(first["result"]["dark_condition_id"], dark["condition_id"])
        self.assertEqual(meter.commands.count("READ?"), 18)

    def test_partial_moving_filter_history_survives_preparation_failures(self):
        for failure in ("overload", "guard", "communication"):
            with self.subTest(failure=failure):
                runner, _, meter, electrical, _, clock = hardware_modular()
                hardware = replace(runner.config.hardware, detector={**runner.config.hardware.detector,
                    "digital_filter": {"enabled": True, "type": "moving", "count": 3}})
                runner.config.hardware = hardware
                electrical.detector = Voltmeter2182(electrical.detector.resource,
                    **{k: v for k, v in hardware.detector.items() if k != "address"},
                    max_voltage_v=runner.config.max_abs_voltage_v, clock=clock)
                original_query = meter.query
                read_count = []
                def query(command):
                    if command == "READ?":
                        read_count.append(command)
                        if len(read_count) == 2:
                            if failure == "communication":
                                raise OSError("injected second warmup communication failure")
                            if failure == "overload":
                                original_query(command)
                                return "9.9e37"
                    return original_query(command)
                meter.query = query
                original_guard = runner._formal_guard
                guard_count = []
                def guard(bias):
                    state = original_guard(bias)
                    guard_count.append(True)
                    if failure == "guard" and len(guard_count) == 3:
                        raise NktError("injected second warmup module guard failure")
                    return state
                runner._formal_guard = guard
                record = runner.run()
                self.assertFalse(record["completed"])
                self.assertFalse(any(e["phase"] == "power_retry" for e in record["events"]))
                row = record["conditions"][0]
                transition = row["filter_transition"]
                self.assertFalse(transition["complete"])
                self.assertEqual(len(transition["readings"]), 2)
                self.assertEqual(transition["readings"][0]["raw_response"], "1e-6")
                self.assertEqual(transition["readings"][1]["raw_response"], "9.9e37" if failure == "overload" else None)
                self.assertTrue(all(not sample["valid"] for sample in transition["readings"]))
                self.assertIn("error", transition["readings"][1])
                self.assertEqual(row["windows"], [])
                self.assertEqual(row["power_attempts"][0]["filter_transition"], transition)
                failed = record["electrical_failure_evidence"]["detector"]
                self.assertEqual(failed["filter_transition"], transition)
                self.assertIn("READ?", failed["partial_raw"])
                self.assertFalse(failed["state_current"])
                self.assertFalse(row["accepted"])

    def test_unselected_injected_controller_and_mismatched_config_reject_zero_io(self):
        doc, path = document()
        doc["combination_scan"]["order"] = ["nanovoltmeter"]
        config = RuntimeConfig(parse_modular_config(doc, path))
        electrical = SimulatedElectrical(config)
        for module in ("optics", "environment", "smu"):
            with self.subTest(module=module), self.assertRaisesRegex(NktError, "mismatch"):
                ModularRunner(config, electrical, **{module: object()})
        other = RuntimeConfig(parse_modular_config(doc, path))
        with self.assertRaisesRegex(NktError, "configuration"):
            ModularRunner(other, electrical)
        self.assertEqual(electrical.commands, [])

    def test_three_read_points_keep_seven_full_checks_and_off_zero_relay(self):
        runner, host, meter, electrical, _, _ = hardware_modular()
        counts = []
        original = electrical.begin
        def checked_begin():
            state = original()
            counts.append(meter.commands.count("INIT:CONT?"))
            return state
        electrical.begin = checked_begin
        record = runner.run()
        self.assertTrue(record["completed"], record["error"] or record["cleanup"])
        self.assertEqual(meter.commands.count("READ?"), 9)
        self.assertEqual([b-a for a, b in zip(counts, counts[1:])], [7, 7])
        self.assertNotIn("OUTP:STAT 1", host.commands)
        self.assertEqual(host.values["SOUR:CURR"], "0")
        for row in record["conditions"]:
            for sample in row["windows"][0]["samples"]:
                self.assertEqual(sample["electrical_before"]["detector_verification"], "fresh_acquisition_before")
                self.assertEqual(sample["electrical_after"]["detector_verification"], "fresh_acquisition_after")

    def test_new_optics_current_enable_follows_full_check_and_fresh_power(self):
        history = []
        runner, host, _, electrical, optics, _ = hardware_modular(current_source=True)
        original_write = host.write
        def write(command):
            history.append(("host", command))
            return original_write(command)
        host.write = write
        original_verify = electrical.detector.verify
        def verify():
            history.append(("detector", "verify"))
            return original_verify()
        electrical.detector.verify = verify
        original_qualify = optics.qualify
        def qualify(*args, **kwargs):
            result = original_qualify(*args, **kwargs)
            history.append(("optics", optics.point.optical_state + "_qualified"))
            return result
        optics.qualify = qualify
        original_barrier = optics.preacquisition_check
        def barrier(*args, **kwargs):
            result = original_barrier(*args, **kwargs)
            history.append(("optics", "fresh_power"))
            return result
        optics.preacquisition_check = barrier
        record = runner.run()
        self.assertTrue(record["completed"], record["error"] or record["cleanup"])
        for i, item in enumerate(history):
            if item != ("optics", "light_qualified"):
                continue
            enabled = next(j for j in range(i+1, len(history)) if history[j] == ("host", "OUTP:STAT 1"))
            checked = next(j for j in range(i+1, enabled) if history[j] == ("detector", "verify"))
            barrier_index = next(j for j in range(checked+1, enabled) if history[j] == ("optics", "fresh_power"))
            self.assertLess(i, checked)
            self.assertLess(barrier_index, enabled)

    def test_optical_preparation_detector_drift_stops_before_enable(self):
        runner, host, meter, _, optics, _ = hardware_modular(current_source=True)
        original = optics.qualify
        enable_count = []
        def qualify(*args, **kwargs):
            result = original(*args, **kwargs)
            if optics.point.optical_state == "light":
                enable_count.append(host.commands.count("OUTP:STAT 1"))
                meter.values["SENS:VOLT:DC:NPLC"] = "2"
            return result
        optics.qualify = qualify
        record = runner.run()
        self.assertFalse(record["completed"])
        self.assertIn("NPLC", record["error"])
        self.assertEqual(host.commands.count("OUTP:STAT 1"), enable_count[0])
        self.assertTrue(record["cleanup"]["laser_off_confirmed"])
        self.assertTrue(record["cleanup"]["electrical_safe_confirmed"])

    def test_hard_power_failure_never_enters_ordinary_retry(self):
        runner, _, _, _, optics, _ = hardware_modular()
        calls = []
        original = optics.power.formal_sample
        def no_signal(phase, **kwargs):
            if kwargs["illuminated"]:
                calls.append(phase)
                raise NktError("Signal below minimum_signal_power_w; no search for light")
            return original(phase, **kwargs)
        optics.power.formal_sample = no_signal
        record = runner.run()
        self.assertFalse(record["completed"])
        self.assertEqual(len(calls), 1)
        self.assertFalse(any(e["phase"] == "power_retry" for e in record["events"]))
        self.assertTrue(record["cleanup"]["laser_off_confirmed"])

    def test_formal_power_deviation_remeasures_whole_window_with_original_dark(self):
        runner, _, meter, _, optics, _ = hardware_modular()
        runner.config.power = replace(runner.config.power, max_retries=1, point_timeout_s=600.)
        optics.power.config = runner.config.power
        original = optics.power.formal_sample
        injected = []
        def drift(phase, **kwargs):
            sample = original(phase, **kwargs)
            if kwargs["illuminated"] and phase == "light_after" and not injected:
                injected.append(sample)
                raise PowerDeviation("injected ordinary formal drift")
            return sample
        optics.power.formal_sample = drift
        record = runner.run()
        self.assertTrue(record["completed"], record["error"] or record["cleanup"])
        dark, first_light, second_light = record["conditions"]
        self.assertEqual(len(first_light["power_attempts"]), 2)
        failed, success = first_light["power_attempts"]
        self.assertFalse(failed["valid"])
        self.assertTrue(success["valid"])
        self.assertEqual(len(failed["windows"][0]["samples"]), 1)
        self.assertTrue(all(not s["valid"] and not s["accepted"] for s in failed["windows"][0]["samples"]))
        self.assertEqual(len(first_light["windows"][0]["samples"]), 3)
        self.assertEqual(first_light["result"]["dark_condition_id"], dark["condition_id"])
        self.assertEqual(second_light["result"]["dark_condition_id"], dark["condition_id"])
        self.assertEqual(meter.commands.count("READ?"), 10)  # 9 accepted + 1 preserved failed.
        self.assertTrue(failed["cleanup"]["laser_off_confirmed"])
        self.assertTrue(failed["cleanup"]["electrical_safe_confirmed"])

    def test_raw_audit_failure_stays_fatal_with_safe_independent_cleanup(self):
        def event(row):
            if row["phase"] == "raw_sample":
                raise OSError("injected audit storage failure")
        runner, _, meter, _, _, _ = hardware_modular(on_event=event)
        record = runner.run()
        self.assertFalse(record["completed"])
        self.assertIn("audit storage failure", record["error"])
        self.assertEqual(meter.commands.count("READ?"), 1)
        self.assertFalse(any(e["phase"] == "power_retry" for e in record["events"]))
        self.assertTrue(record["cleanup"]["laser_off_confirmed"])
        self.assertTrue(record["cleanup"]["electrical_safe_confirmed"])
        sample = record["conditions"][0]["windows"][0]["samples"][0]
        self.assertIn("audit storage failure", sample["audit_error"])
        self.assertFalse(sample["accepted"])

    def test_source_only_records_fresh_source_state_without_detector_or_fictitious_i(self):
        doc, path = document(source=True)
        doc["combination_scan"].update(order=["current_source"], samples_per_condition=2)
        config = RuntimeConfig(parse_modular_config(doc, path))
        config.hardware_mode = True
        _, host, _, _, _ = hardware_setup()
        config.hardware = SourceHardware({"address": "FAKE::6221::INSTR", "expected_idn": host.values["*IDN"],
            "compliance_v": .1, "max_step_a": 1e-8, "settle_s": 0., "tolerance_a": 0.}, 10000, True)
        electrical = SourceOnlyElectrical(config, {"6221": VisaTransport(host, role="6221", authorize_writes=True)})
        clock = Clock()
        electrical.source.sleep, electrical.source.clock = clock.sleep, clock
        record = ModularRunner(config, electrical, clock=clock, sleep=clock.sleep).run()
        self.assertTrue(record["completed"], record["error"] or record["cleanup"])
        self.assertFalse(any(c.startswith("SYST:COMM:SER:SEND") for c in host.commands))
        for row in record["conditions"]:
            for sample in row["windows"][0]["samples"]:
                self.assertIsNone(sample["voltage_v"])
                self.assertIsNone(sample["current_a"])
                reading = sample["module_readings"][0]
                self.assertEqual(reading["module"], "current_source")
                self.assertEqual(reading["actual"]["setpoint"], row["requested_current_a"])
                self.assertTrue(reading["status"]["setpoint_is_not_measured_current"])
                self.assertIn("coupled_arm_raw_responses", reading["actual"]["readback"])

    def test_cleanup_retains_final_source_off_readback(self):
        runner, _, _, _, _, _ = hardware_modular(current_source=True)
        record = runner.run()
        self.assertTrue(record["completed"], record["error"] or record["cleanup"])
        self.assertTrue(record["cleanup"]["electrical_safe_confirmed"])
        final = record["last_confirmed_electrical_state"]["readback"]
        self.assertFalse(final["source"]["output_enabled"])
        self.assertEqual(final["source"]["setpoint"], 0.)

    def test_failed_cleanup_marks_current_state_unknown_and_preserves_last(self):
        runner, _, _, electrical, _, _ = hardware_modular()
        def cleanup_failure():
            raise OSError("injected cleanup communication failure")
        electrical.cleanup = cleanup_failure
        record = runner.run()
        self.assertFalse(record["completed"])
        self.assertFalse(record["cleanup"]["electrical_safe_confirmed"])
        self.assertFalse(record["electrical_state_current"])
        self.assertIsNotNone(record["last_confirmed_electrical_state"])

    def test_environment_cleanup_failure_keeps_completed_raw_conditions(self):
        doc, path = document()
        doc.update(environment_document())
        doc["combination_scan"]["order"].insert(0, "temperature")
        doc["temperature_scan"] = {"points_k": [1.6]}
        config = RuntimeConfig(parse_modular_config(doc, path))
        from attodry_control.modular_environment import EnvironmentAxes
        class FaultyCleanup(EnvironmentAxes):
            def finish(self, *, failed):
                super().finish(failed=failed)
                raise NktError("injected environment cleanup failure")
        with patch("attodry_control.modular_environment.EnvironmentAxes", FaultyCleanup):
            record = run_modular(config)
        self.assertFalse(record["completed"])
        self.assertEqual(len(record["conditions"]), config.plan.condition_count)
        self.assertTrue(all(row["windows"] for row in record["conditions"]))
        self.assertFalse(any(row["accepted"] for row in record["conditions"]))
        self.assertIn("environment cleanup failure", ";".join(record["cleanup"]["errors"]))

    def test_partial_factory_failure_closes_optional_pem_and_keeps_unconfirmed_outputs(self):
        doc, path = document()
        doc.update(environment_document())
        doc["combination_scan"]["order"].insert(0, "temperature")
        doc["temperature_scan"] = {"points_k": [1.6]}
        doc["optics"].update(use_pem=True, peak_retardance_waves=.25)
        doc["pem"] = dict(pem_config().__dict__)
        config = RuntimeConfig(parse_modular_config(doc, path))
        from attodry_control.pem import SimulatedPemTransport
        transport = SimulatedPemTransport()
        with patch("attodry_control.pem.SimulatedPemTransport", return_value=transport), \
             patch("attodry_control.modular_environment.EnvironmentAxes", side_effect=NktError("injected factory failure")):
            record = run_modular(config)
        self.assertFalse(record["completed"])
        self.assertIn("factory failure", record["error"])
        self.assertFalse(record["cleanup"]["laser_off_confirmed"])
        self.assertTrue(transport.closed)
        self.assertEqual(transport.commands, [])

    def test_partially_failed_nkt_open_is_closed_before_record_delivery(self):
        doc, path = document()
        doc["combination_scan"].update(order=["optics"], backend="hardware")
        doc["nkt_run"]["backend"] = "nkt_sdk"
        doc["nkt_source"].update(dll_path="FAKE_ONLY.dll", port="COM199", address=1,
                                 expected_serial="FAKE_ONLY_SOURCE")
        doc["nkt_varia"].update(address=2, expected_serial="FAKE_ONLY_FILTER")
        config = RuntimeConfig(parse_modular_config(doc, path))
        calls = []
        class PartialBackend:
            def __init__(self, *args, **kwargs):
                calls.append("constructed")
            def open(self, **kwargs):
                calls.append("open attempted")
                raise OSError("injected open failure after partial connection")
            def close(self):
                calls.append("closed")
        with patch("attodry_control.nkt_sdk.NktSdk", PartialBackend):
            record = run_modular(config)
        self.assertEqual(calls, ["constructed", "open attempted", "closed"])
        self.assertFalse(record["completed"])
        self.assertFalse(record["cleanup"]["laser_off_confirmed"])
        self.assertIn("partial connection", record["error"])

    def test_optics_cleanup_failure_does_not_skip_electrical_cleanup(self):
        runner, _, _, electrical, optics, _ = hardware_modular(current_source=True)
        optics_cleanup = optics.cleanup
        electrical_cleanup = electrical.cleanup
        calls = []
        def fail_optics_cleanup():
            optics_cleanup()
            raise OSError("injected optics cleanup verification failure")
        def safe_electrical_cleanup():
            calls.append("electrical cleanup")
            return electrical_cleanup()
        optics.cleanup, electrical.cleanup = fail_optics_cleanup, safe_electrical_cleanup
        record = runner.run()
        self.assertFalse(record["completed"])
        self.assertTrue(calls)
        self.assertTrue(record["cleanup"]["electrical_safe_confirmed"])
        self.assertFalse(record["cleanup"]["laser_off_confirmed"])
        self.assertIn("optics cleanup verification failure", ";".join(record["cleanup"]["errors"]))


if __name__ == "__main__":
    unittest.main()
