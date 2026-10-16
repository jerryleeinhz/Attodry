"""Offline optics lifecycle, optional PEM and shared-feedback regression."""
from dataclasses import replace
from pathlib import Path
import unittest

from attodry_control.nkt_config import NktError
from attodry_control.nkt_control import NktController, SimulatedNkt
from attodry_control.optics_points import OpticsPoint, OpticsSession
from attodry_control.pem import Pem, SimulatedPemTransport
from attodry_control.pem_config import PemConfig, load_pem_config
from attodry_control.photoresponse_cld import CldController, SimulatedCld
from attodry_control.photoresponse_power import PowerDeviation, open_photoresponse_power
from test_photoresponse import Clock, example
from test_photoresponse_power import config_for


def pem_config():
    return PemConfig(backend="simulation", io_timeout_s=1., settle_timeout_s=1.,
        poll_interval_s=.01, wavelength_min_nm=400., wavelength_max_nm=900.,
        amplitude_min_nm=10., amplitude_max_nm=550., amplitude_tolerance_nm=.1,
        frequency_min_hz=49000., frequency_max_hz=51000., finish_action="disable_ack")


def session(kind="nkt", *, feedback=False, pem=False, **kwargs):
    config = config_for(kind) if feedback or kind == "cld" else example()
    clock = Clock()
    backend_type, laser_type = ((SimulatedNkt, NktController) if kind == "nkt"
                               else (SimulatedCld, CldController))
    backend = backend_type(config.nkt)
    laser = laser_type(config.nkt, backend, clock=clock, sleep=clock.sleep)
    power = open_photoresponse_power(config.power, laser, clock=clock, sleep=clock.sleep) if feedback else None
    modulator = (Pem(pem_config(), SimulatedPemTransport(), clock=clock, sleep=clock.sleep)
                 if pem else None)
    optics = OpticsSession(laser, power=power, pem=modulator,
        peak_retardance_waves=.25 if pem else None, clock=clock, sleep=clock.sleep, **kwargs)
    return optics, backend, clock


class OpticsLifecycleTests(unittest.TestCase):
    def test_construction_and_invalid_configuration_never_open_or_write(self):
        optics, backend, _ = session()
        self.assertEqual(backend.commands, [])
        with self.assertRaisesRegex(NktError, "not open"):
            optics.prepare(OpticsPoint(optics.laser.config.points[0]))
        self.assertEqual(backend.commands, [])
        with self.assertRaisesRegex(NktError, "retardance"):
            OpticsSession(optics.laser, peak_retardance_waves=.25)
        self.assertEqual(backend.commands, [])

    def test_direct_source_keeps_actual_power_missing_and_no_pm_queries(self):
        optics, backend, _ = session()
        optics.open()
        row = optics.prepare(OpticsPoint(optics.laser.config.points[0]))
        optics.qualify()
        self.assertTrue(optics.begin_sample()["source"]["emission_state"])
        optics.end_sample()
        optics.finish_point()
        self.assertIsNone(row["measured_power_w"])
        self.assertIsNone(row["measurement_plane"])
        self.assertIsNone(row["power"])
        result = optics.cleanup()
        self.assertTrue(result["verified"], result)
        self.assertTrue(result["off_confirmed"])
        if optics.power:
            self.assertIn("power_meter_audit", optics.record)
        before = len(backend.commands)
        self.assertIs(result, optics.cleanup())
        self.assertEqual(len(backend.commands), before)

    def test_target_w_without_power_meter_rejected_before_point_io(self):
        optics, backend, _ = session()
        optics.open()
        before = list(backend.commands)
        with self.assertRaisesRegex(NktError, "calibrated power meter"):
            optics.prepare(OpticsPoint(optics.laser.config.points[0], target_power_w=1e-6))
        self.assertEqual(backend.commands, before)
        optics.cleanup()

    def test_sample_freezes_point_and_rejects_nested_sampling(self):
        optics, _, _ = session()
        point = OpticsPoint(optics.laser.config.points[0])
        optics.open()
        optics.prepare(point)
        optics.qualify()
        optics.begin_sample()
        for operation in (lambda: optics.prepare(point), optics.qualify,
                          optics.begin_sample, optics.suspend, optics.finish_point):
            with self.assertRaises(NktError):
                operation()
        optics.end_sample()
        optics.finish_point()
        optics.cleanup()

    def test_remaining_wait_uses_elapsed_pem_and_guard_work(self):
        optics, _, clock = session(settle_s=2.)
        optics.open()
        optics.prepare(OpticsPoint(optics.laser.config.points[0], optical_state="dark"))
        optics.qualify(lambda: clock.sleep(1.5))
        self.assertAlmostEqual(optics.row["settling"]["requested_wait_s"], .5)
        self.assertAlmostEqual(clock() - optics.last_change_monotonic_s, 2.)
        optics.cleanup()

    def test_last_confirmed_state_not_zeroed_after_fault(self):
        optics, backend, _ = session()
        optics.open()
        optics.prepare(OpticsPoint(optics.laser.config.points[0]))
        optics.qualify()
        confirmed = optics.record["last_confirmed_state"]
        backend.source["interlock"] = 0
        with self.assertRaisesRegex(NktError, "interlock"):
            optics.verify()
        self.assertFalse(optics.record["state_current"])
        self.assertEqual(optics.record["last_confirmed_state"], confirmed)
        self.assertFalse(optics.qualified)
        optics.cleanup()

    def test_prepoint_suspend_reads_fresh_state_and_rejects_external_activation(self):
        for kind in ("nkt", "cld"):
            with self.subTest(kind=kind):
                optics, backend, _ = session(kind)
                optics.open()
                self.assertTrue(optics.suspend()["off_confirmed"])
                if kind == "nkt":
                    backend.source["emission_state"] = 3
                    backend.source["status_bits"] |= 1
                else:
                    backend.state["getLdOutputState"] = True
                with self.assertRaisesRegex(NktError, "became active"):
                    optics.suspend()
                self.assertFalse(optics.record["state_current"])
                optics.cleanup()

    def test_repeated_off_suspend_uses_readback_and_preserves_change_clock(self):
        optics, backend, clock = session()
        optics.open()
        optics.prepare(OpticsPoint(optics.laser.config.points[0], optical_state="dark"))
        changed = optics.last_change_monotonic_s
        clock.sleep(1.)
        before = len([c for c in backend.commands if c[0] == "emission"])
        optics.suspend()
        self.assertEqual(len([c for c in backend.commands if c[0] == "emission"]), before)
        self.assertEqual(optics.last_change_monotonic_s, changed)
        optics.cleanup()


class OptionalPemTests(unittest.TestCase):
    def test_config_is_exact_validated_optional_contract(self):
        self.assertIsNone(load_pem_config({}))
        raw = dict(pem_config().__dict__)
        self.assertEqual(load_pem_config({"pem": raw}), pem_config())
        with self.assertRaises(NktError):
            load_pem_config({"pem": {**raw, "invented_voltage": 1}})
        with self.assertRaises(NktError):
            pem_config().target(950., .25)

    def test_pem_follows_wavelength_and_same_setting_writes_are_reused(self):
        optics, _, _ = session(feedback=True, pem=True)
        optics.open()
        points = optics.laser.config.points
        for index, (point, target_index) in enumerate(((points[0], 0), (points[0], 1), (points[1], 0))):
            target = optics.power.config.for_target(target_index).target_power_w
            optics.prepare(OpticsPoint(point, target_power_w=target, power_index=target_index, index=index))
            optics.qualify()
            optics.preacquisition_check()
            optics.begin_sample()
            optics.end_sample()
            optics.finish_point()
        commands = optics.pem.transport.commands
        self.assertEqual([c for c in commands if c.startswith(":MOD:AMP ")],
                         [":MOD:AMP 187.5", ":MOD:AMP 190"])
        result = optics.cleanup()
        self.assertTrue(result["verified"], result)
        self.assertTrue(result["pem"]["disable_acknowledged"])
        self.assertFalse(result["pem"]["physical_off_confirmed"])

    def test_guarded_window_facade_checks_pem_without_extra_pm_read(self):
        optics, _, _ = session(feedback=True, pem=True)
        optics.open()
        target = optics.power.config.for_target(0).target_power_w
        optics.prepare(OpticsPoint(optics.laser.config.points[0], target_power_w=target))
        optics.qualify()
        pm_reads = len(optics.power.meter.transcript)
        optics.pem.transport.stable = 0
        with self.assertRaisesRegex(NktError, "stability"):
            optics.guarded_laser.verify_point(emitting=True)
        self.assertEqual(len(optics.power.meter.transcript), pm_reads)
        self.assertFalse(optics.qualified)
        self.assertTrue(optics.cleanup()["off_confirmed"])

    def test_poisoned_pem_cleanup_still_closes_meter_and_laser(self):
        optics, backend, _ = session(feedback=True, pem=True)
        optics.open()
        target = optics.power.config.for_target(0).target_power_w
        optics.prepare(OpticsPoint(optics.laser.config.points[0], target_power_w=target))
        optics.qualify()
        optics.pem.poisoned = True
        result = optics.cleanup()
        self.assertFalse(result["verified"])
        self.assertTrue(result["off_confirmed"])
        self.assertTrue(result["errors"])
        self.assertTrue(optics.pem.closed)
        self.assertTrue(optics.power.meter.closed)
        self.assertIn(("close",), backend.commands)


class SharedPowerLifecycleTests(unittest.TestCase):
    def test_progress_and_cleanup_reference_retained_raw_audit_once(self):
        import json
        optics, _, _ = session(feedback=True)
        optics.open()
        target = optics.power.config.for_target(0).target_power_w
        optics.prepare(OpticsPoint(optics.laser.config.points[0], target_power_w=target))
        optics.qualify()
        optics.begin_sample()
        optics.end_sample()
        result = optics.cleanup()
        self.assertEqual(optics.record["power_meter_audit"]["transcript"], optics.power.meter.transcript)
        self.assertEqual(result["power"]["transaction_count"], len(optics.power.meter.transcript))
        self.assertNotIn("transcript", result["power"])
        self.assertEqual(result["power"]["transcript_reference"], "optics_audit.power_meter_audit.transcript")
        for event in optics.record["events"]:
            if event["phase"] in ("optics_prepared", "optics_qualified"):
                self.assertNotIn("evidence", event)
                self.assertEqual(event["point_record_index"], 0)
        json.dumps(optics.record, allow_nan=False)  # No cleanup/audit reference cycle.

    def test_both_lasers_reuse_current_power_engine_and_brackets(self):
        for kind in ("nkt", "cld"):
            with self.subTest(kind=kind):
                optics, _, _ = session(kind, feedback=True)
                optics.open()
                target = optics.power.config.for_target(0).target_power_w
                optics.prepare(OpticsPoint(optics.laser.config.points[0], target_power_w=target))
                optics.qualify()
                optics.preacquisition_check()
                before = optics.begin_sample()
                after = optics.end_sample()
                optics.finish_point()
                self.assertAlmostEqual(before["power_w"], target, delta=1e-6)
                self.assertAlmostEqual(after["power_w"], target, delta=1e-6)
                self.assertIs(optics.row["power"], optics.power.row)
                self.assertEqual(optics.row["measured_power_w"], optics.power.row["formal_mean_power_w"])
                self.assertTrue(optics.cleanup()["verified"])

    def test_dark_first_and_previous_seed_only_same_wavelength_visit(self):
        optics, _, _ = session(feedback=True)
        f = optics.power.config
        optics.power.config = replace(f, power_points=("dark", *f.feedback._targets()), initial_actuator_mode="previous")
        optics.power.config.validate(optics.laser.config)
        optics.open()
        points = optics.laser.config.points
        for state, point, index, visit in (("dark", points[0], 0, "visit-1"),
                                          ("light", points[0], 1, "visit-1"),
                                          ("light", points[0], 2, "visit-1"),
                                          ("light", points[1], 1, "visit-2")):
            target = None if state == "dark" else optics.power.config.for_target(index).target_power_w
            optics.prepare(OpticsPoint(point, state, target, index, visit))
            source = optics.row["initial_actuator"]["source"]
            self.assertEqual(source, "previous" if state == "light" and index == 2 else "point")
            optics.qualify()
            optics.preacquisition_check()
            optics.begin_sample()
            optics.end_sample()
            optics.finish_point()
        dark = optics.record["points"][0]
        self.assertIsNone(dark["measured_power_w"])
        self.assertEqual(dark["power"]["optical_state"], "dark")
        self.assertFalse(any(s["sample"].get("measurement_performed", False) for s in dark["power"]["samples"]))
        self.assertTrue(optics.cleanup()["verified"])

    def test_preacquisition_deviation_is_typed_and_not_automatically_retried(self):
        optics, backend, _ = session(feedback=True)
        optics.open()
        target = optics.power.config.for_target(0).target_power_w
        optics.prepare(OpticsPoint(optics.laser.config.points[0], target_power_w=target))
        optics.qualify()
        original = optics.power.meter.read_sample
        def drift(wavelength):
            value = original(wavelength)
            value["power_w"] = target * 1.2
            return value
        optics.power.meter.read_sample = drift
        writes = len([c for c in backend.commands if c[0] == "configure"])
        with self.assertRaises(PowerDeviation):
            optics.preacquisition_check()
        self.assertEqual(len([c for c in backend.commands if c[0] == "configure"]), writes)
        self.assertFalse(optics.qualified)
        self.assertFalse(optics.power.row["samples"][-1]["window_valid"])
        self.assertTrue(optics.cleanup()["verified"])

    def test_no_signal_is_hard_error_not_power_retry(self):
        optics, _, _ = session(feedback=True)
        optics.open()
        target = optics.power.config.for_target(0).target_power_w
        optics.prepare(OpticsPoint(optics.laser.config.points[0], target_power_w=target))
        original = optics.power.meter.read_sample
        def no_signal(wavelength):
            value = original(wavelength)
            value["power_w"] = 0.
            return value
        optics.power.meter.read_sample = no_signal
        with self.assertRaisesRegex(NktError, "minimum_signal") as error:
            optics.qualify()
        self.assertNotIsInstance(error.exception, PowerDeviation)
        self.assertEqual(optics.power.row["adjustments"], [])
        self.assertFalse(optics.power.row["samples"][-1]["valid"])
        self.assertTrue(optics.cleanup()["verified"])


if __name__ == "__main__":
    unittest.main()
