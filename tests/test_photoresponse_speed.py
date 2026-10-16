"""Production-path timing budgets and faults, with fake clocks/resources only."""
from dataclasses import replace
import unittest

from attodry_control.nkt_config import NktError
from attodry_control.photoresponse import PhotoresponseRunner
from attodry_control.photoresponse_cld import CldController, SimulatedCld
from attodry_control.photoresponse_hardware import HardwareElectrical
from attodry_control.photoresponse_power import PhotoresponsePower
from attodry_control.photoresponse_transport import Serial2182Transport, VisaTransport
from attodry_control.pm100d import Pm100d, SimulatedPmResource
from test_photoresponse import Clock
from test_photoresponse_dark_scan import shared
from test_photoresponse_hardware import Resource
from test_photoresponse_integration import hardware_setup
from test_photoresponse_power import config_for


def hardware_scan(electrical_class=HardwareElectrical, *, config_transform=None):
    old, host, meter, _, _ = hardware_setup("open_circuit_voltage", channel=2)
    base = shared(config_for("cld"))
    pm_config = replace(base.power.meter, backend="visa", resource="FAKE::PM::INSTR",
                        expected_serial="SIMULATION", expected_sensor_serial="SIMULATION")
    config = replace(base, nkt=replace(old.config.nkt, points=(old.config.nkt.points[0],)), hardware=old.config.hardware,
                     mode="open_circuit_voltage", detector="keithley2182a", bias_points=(None,),
                     max_abs_voltage_v=.1, max_abs_current_a=None, samples_per_window=3,
                     power=replace(base.power, meter=pm_config))
    if config_transform:
        config = config_transform(config)
    clock = Clock()
    backend = SimulatedCld(config.nkt)
    backend.is_hardware = True
    laser = CldController(config.nkt, backend, clock=clock, sleep=clock.sleep)
    electrical = electrical_class(config, {"6221": VisaTransport(host, role="6221", authorize_writes=True)},
                                  clock=clock, sleep=clock.sleep)
    pm = Pm100d(pm_config, SimulatedPmResource(lambda: .002 * backend.state["getLdCurrSetpoint"] / .03),
                is_hardware=True, authorize_settings=True, authorize_measurement=True, clock=clock)
    power = PhotoresponsePower(config.power, laser, pm, clock=clock, sleep=clock.sleep)
    runner = PhotoresponseRunner(config, laser, electrical, power=power, clock=clock, sleep=clock.sleep,
                                authorize_writes=True, confirm_manual_route=True)
    return runner, host, meter, electrical, power, clock


class VerificationBudgetTests(unittest.TestCase):
    def test_fake_clock_comparison_preserves_results_and_reduces_transactions(self):
        class PriorBoundaryChecks(HardwareElectrical):
            def sample_boundary(self):
                return self.snapshot()
            def begin(self):
                super().begin()
                return None
            def prepare(self, bias):
                super().prepare(bias)
                return None
        results = []
        for electrical_class, poll in ((PriorBoundaryChecks, .1), (HardwareElectrical, .1),
                                       (HardwareElectrical, .02)):
            runner, _, meter, electrical, _, clock = hardware_scan(electrical_class)
            electrical.detector.resource.poll_interval_s = poll
            started = clock()
            record = runner.run()
            self.assertTrue(record["completed"], record["error"])
            results.append((clock()-started, meter.commands.count("INIT:CONT?"),
                            [row["result"] for row in record["conditions"]]))
        self.assertLess(results[1][0], results[0][0] * .65)
        self.assertLess(results[2][0], results[1][0] * .4)
        self.assertEqual(results[0][1] - results[1][1], 24)  # 8 repeated full checks per point
        self.assertEqual(results[1][1], results[2][1])
        for result in results[1:]:
            for actual, expected in zip(result[2], results[0][2]):
                for key in ("dark_mean", "delta_voltage_v"):
                    if key in expected:
                        self.assertEqual(actual[key], expected[key])

    def test_three_read_point_has_one_post_optical_and_two_checks_per_read(self):
        runner, host, meter, electrical, _, _ = hardware_scan()
        count_after_begin = []
        begin = electrical.begin
        def begin_with_budget():
            result = begin()
            count_after_begin.append(meter.commands.count("INIT:CONT?"))
            return result
        electrical.begin = begin_with_budget
        record = runner.run()
        self.assertTrue(record["completed"], record["error"] or record["cleanup"])
        self.assertEqual(meter.commands.count("READ?"), 9)
        # Every later point adds 6 checks around 3 READs and 1 after optical tuning.
        self.assertEqual([b-a for a,b in zip(count_after_begin, count_after_begin[1:])], [7, 7])
        self.assertNotIn("OUTP:STAT 1", host.commands)
        for row in record["conditions"]:
            for sample in row["windows"][0]["samples"]:
                self.assertEqual(sample["electrical_before"]["detector_readback"]["channel"], 2)
                self.assertEqual(sample["electrical_after"]["detector_readback"]["channel"], 2)
                self.assertEqual(sample["electrical_before"]["detector_verification"], "fresh_acquisition_before")
                self.assertEqual(sample["electrical_after"]["detector_verification"], "fresh_acquisition_after")

    def test_settings_drift_during_power_guard_is_caught_before_read(self):
        runner, host, meter, _, power, _ = hardware_scan()
        original = power.formal_sample
        def change(phase, **kwargs):
            result = original(phase, **kwargs)
            if phase == "light_before":
                meter.values["SENS:CHAN"] = "1"
            return result
        power.formal_sample = change
        record = runner.run()
        self.assertFalse(record["completed"])
        self.assertIn("SENS:CHAN", record["error"])
        self.assertEqual(meter.commands.count("READ?"), 3)  # dark only
        self.assertTrue(record["cleanup"]["laser_off_confirmed"])
        self.assertTrue(record["cleanup"]["electrical_safe_confirmed"])
        self.assertNotIn("OUTP:STAT 1", host.commands)

    def test_full_check_remains_after_long_optical_tuning(self):
        runner, _, meter, _, power, _ = hardware_scan()
        original = power.qualify
        def change(*args, **kwargs):
            result = original(*args, **kwargs)
            meter.values["SENS:VOLT:CHAN2:RANG"] = "10"
            return result
        power.qualify = change
        record = runner.run()
        self.assertFalse(record["completed"])
        self.assertIn("RANG", record["error"])
        self.assertEqual(meter.commands.count("READ?"), 3)  # dark only
        self.assertTrue(record["cleanup"]["laser_off_confirmed"])
        self.assertTrue(record["cleanup"]["electrical_safe_confirmed"])

    def test_setting_change_during_read_remains_a_failure(self):
        runner, _, meter, _, _, _ = hardware_scan()
        original = meter.query
        def drift(command):
            result = original(command)
            if command == "READ?":
                meter.values["SENS:VOLT:CHAN2:DFIL"] = "1"
            return result
        meter.query = drift
        record = runner.run()
        self.assertFalse(record["completed"])
        self.assertIn("DFIL", record["error"])
        self.assertEqual(meter.commands.count("READ?"), 1)
        self.assertFalse(record["conditions"][0]["windows"][0]["samples"][0]["valid"])
        self.assertTrue(record["cleanup"]["laser_off_confirmed"])


class PollBudgetTests(unittest.TestCase):
    def test_invalid_poll_policy_rejected_before_io(self):
        host = Resource()
        for poll in (0, -.1, .11, float("inf"), float("nan")):
            with self.assertRaises(NktError):
                Serial2182Transport(VisaTransport(host, role="relay"), poll_interval_s=poll)
        self.assertEqual(host.commands, [])

    def relay(self, *, timeout=.5):
        host, clock = Resource(), Clock()
        host.downstream = Resource("2182A")
        relay = Serial2182Transport(VisaTransport(host, role="relay", authorize_writes=True),
                                    timeout_s=timeout, sleep=clock.sleep, clock=clock, poll_interval_s=.02)
        return relay, host, clock

    def test_fast_ready_reply_has_short_first_wait(self):
        relay, host, clock = self.relay()
        relay.query("*IDN?")
        self.assertAlmostEqual(clock.value - 100000., .02)
        self.assertAlmostEqual(relay.transcript[-1]["poll_wait_requested_s"], .02)
        self.assertEqual(host.commands.count("SYST:COMM:SER:ENT?"), 1)

    def test_empty_replies_back_off_without_resending_command(self):
        relay, host, _ = self.relay()
        original = host.query
        attempts = []
        def delayed(command):
            if command == "SYST:COMM:SER:ENT?":
                attempts.append(command)
                if len(attempts) < 4:
                    return ""
            return original(command)
        host.query = delayed
        self.assertIn("2182A", relay.query("*IDN?"))
        self.assertEqual(len(attempts), 4)
        self.assertEqual(sum(c.startswith("SYST:COMM:SER:SEND") for c in host.commands), 1)
        self.assertAlmostEqual(relay.transcript[-1]["poll_wait_requested_s"], .02+.04+.08+.1)

    def test_late_nonempty_response_poisoned_and_never_reused(self):
        relay, host, clock = self.relay(timeout=.05)
        original = host.query
        def late(command):
            if command == "SYST:COMM:SER:ENT?":
                clock.sleep(.1)
            return original(command)
        host.query = late
        with self.assertRaises(TimeoutError):
            relay.query("*IDN?")
        self.assertTrue(relay.poisoned)
        commands = list(host.commands)
        with self.assertRaises(NktError): relay.query("*IDN?")
        self.assertEqual(host.commands, commands)

    def test_deadline_clamps_wait_and_retains_failed_raw_reply(self):
        relay, host, clock = self.relay(timeout=.05)
        host.downstream.query = lambda _: ""
        with self.assertRaises(TimeoutError): relay.query("*IDN?")
        self.assertLessEqual(clock.value - 100000., .050001)
        self.assertAlmostEqual(relay.transcript[-1]["poll_wait_requested_s"], .05)
        self.assertIn("error", relay.transcript[-1])


if __name__ == "__main__":
    unittest.main()
