from dataclasses import replace
import unittest
from unittest.mock import Mock

from optical_helpers import EXAMPLE, Clock
from attodry_control.nkt_config import NktError
from attodry_control.optical_config import load_pm_config, WindowConfig, FeedbackConfig
from attodry_control.pm100d import Pm100d, SimulatedPmResource
from attodry_control.power_feedback import PowerWindow, next_nd_pct, target_in_tolerance


class PmTests(unittest.TestCase):
    def device(self, resource=None, hardware=False, **kwargs):
        return Pm100d(load_pm_config(EXAMPLE), resource or SimulatedPmResource(),
                      is_hardware=hardware, clock=Clock(), **kwargs)

    def test_new_read_is_watts_with_before_after_evidence(self):
        d = self.device()
        self.assertEqual(d.read_power_w(635), 0.001)
        self.assertIn(("write", "SENS:CORR:WAV 635"), d.resource.commands)
        self.assertIn(("query", "*OPC?"), d.resource.commands)
        self.assertEqual(d.last_sample["unit"], "W")
        self.assertEqual(d.last_sample["before"]["wavelength_nm"], 635)
        self.assertEqual(d.last_sample["after"]["wavelength_nm"], 635)
        self.assertNotIn(("query", "FETC?"), d.resource.commands)
        self.assertFalse(any(cmd[1] in ("*RST", "*CLS", "SYST:ERR?", "*ESR?") for cmd in d.resource.commands))

    def test_queries_only_and_correction_is_not_implicit_without_permission(self):
        d = self.device(hardware=True, authorize_measurement=True)
        d.capture()
        self.assertFalse(any(cmd[0] == "write" for cmd in d.resource.commands))
        with self.assertRaises(NktError):
            d.read_power_w(635)
        self.assertFalse(any(cmd[0] == "write" or cmd[1] == "READ?" for cmd in d.resource.commands))
        self.assertEqual(d.read_power_w(633), 0.001)

    def test_unauthorized_factory_is_never_constructed(self):
        c = replace(load_pm_config(EXAMPLE), backend="visa", resource="FAKE")
        factory = Mock()
        with self.assertRaises(NktError):
            Pm100d.open(c, manager_factory=factory)
        factory.assert_not_called()
        d = self.device(hardware=True)
        with self.assertRaises(NktError):
            d.read_sample(633)
        self.assertNotIn(("query", "READ?"), d.resource.commands)

    def test_invalid_measurements_including_saturation_rejected(self):
        for value in (float("nan"), float("inf"), -1e-6, 9.9e37, 0.02, "garbage"):
            d = self.device(SimulatedPmResource(lambda: value))
            with self.subTest(value=value), self.assertRaises((NktError, ValueError)):
                d.read_power_w(633)
            self.assertIsNone(d.last_sample)
        d = self.device(SimulatedPmResource(lambda: 0.001))
        d.resource.values["SENS:POW:RANG?"] = "0.001"
        with self.assertRaises(NktError):
            d.read_power_w(633)

    def test_units_mode_relative_loss_and_questionable_conditions(self):
        changes = [("SENS:POW:UNIT?", "DBM"), ("CONF?", "CURR"),
                   ("SENS:POW:REF:STAT?", "1"), ("SENS:CORR:LOSS:INP:MAGN?", "3"),
                   ("STAT:QUES:COND?", "1"), ("SENS:POW:RANG:AUTO?", "2")]
        for key, value in changes:
            d = self.device(hardware=True, authorize_measurement=True)
            d.resource.values[key] = value
            with self.subTest(key=key), self.assertRaises(NktError):
                d.read_power_w(633)
            self.assertNotIn(("query", "READ?"), d.resource.commands)

    def test_sensor_missing_wrong_identity_and_hot_swap(self):
        for value in ('"", "", "", 0, 0, 0', '"ENERGY","SN","CAL",3,0,2', 'bad'):
            d = self.device()
            d.resource.values["SYST:SENS:IDN?"] = value
            with self.subTest(value=value), self.assertRaises(NktError):
                d.read_sensor()
        d = self.device()
        d.resource.values["*IDN?"] = "THORLABS,PM100A,SN,FIRMWARE"
        with self.assertRaises(NktError):
            d.identify()
        d = self.device()
        d.capture()
        d.resource.values["SYST:SENS:IDN?"] = '"NEW SENSOR","NEW","CAL",1,0,33'
        with self.assertRaises(NktError):
            d.read_sensor()

    def test_invalid_sensor_codes_poison_session_and_preserve_evidence(self):
        for codes in ((-1, 0, 33), (1, -1, 33), (1, 0, -1),
                      (1, 0, "33.5"), (1, 0, 1)):
            d = self.device()
            last = d.capture()
            sensor = d.sensor
            reply = '"SIMULATED SENSOR","SIMULATION","uncalibrated",' + ",".join(map(str, codes))
            d.resource.values["SYST:SENS:IDN?"] = reply
            with self.subTest(codes=codes), self.assertRaises((NktError, ValueError)):
                d.read_sensor()
            self.assertTrue(d.poisoned)
            self.assertFalse(d.state_current)
            self.assertIs(d.last_confirmed_state, last)
            self.assertIs(d.sensor, sensor)
            self.assertEqual(d.transcript[-1]["reply"], reply)
            self.assertIn("error", d.transcript[-1])
            commands = list(d.resource.commands)
            with self.assertRaises(NktError):
                d.prepare(635)
            self.assertEqual(d.resource.commands, commands)
            d.close()

    def test_invalid_numeric_reply_poison_blocks_later_queries(self):
        for command, reply in (("SENS:CORR:WAV?", "not numeric"),
                               ("SENS:POW:RANG?", "NaN"),
                               ("SENS:AVER:COUN?", "Inf"),
                               ("SENS:AVER:COUN?", "1.5"),
                               ("STAT:QUES:COND?", "-1")):
            d = self.device()
            last = d.capture()
            d.resource.values[command] = reply
            with self.subTest(command=command, reply=reply), self.assertRaises((NktError, ValueError)):
                d.capture()
            self.assertTrue(d.poisoned)
            self.assertFalse(d.state_current)
            self.assertIs(d.last_confirmed_state, last)
            self.assertEqual(d.transcript[-1]["command"], command)
            self.assertEqual(d.transcript[-1]["reply"], reply)
            count = len(d.resource.commands)
            with self.assertRaises(NktError):
                d.capture()
            self.assertEqual(len(d.resource.commands), count)

    def test_identity_mismatch_poison_and_cleanup_retain_primary_evidence(self):
        d = self.device()
        last = d.capture()
        identity = d.identity
        d.resource.values["*IDN?"] = "THORLABS,PM100D,CHANGED,SIMULATION"
        with self.assertRaisesRegex(NktError, "identity changed"):
            d.capture()
        self.assertTrue(d.poisoned)
        self.assertIs(d.identity, identity)
        self.assertIs(d.last_confirmed_state, last)
        evidence = d.transcript[-1].copy()
        d.manager = Mock()
        d.resource.close = lambda: (_ for _ in ()).throw(OSError("close failure"))
        with self.assertRaisesRegex(NktError, "close failure"):
            d.close()
        self.assertEqual(d.transcript[-1], evidence)
        d.manager.close.assert_called_once()

    def test_illegal_completion_ack_poison_prevents_followup_settings(self):
        for reply in ("0", "1,unexpected", "not numeric"):
            d = self.device()
            last = d.capture()
            d.resource.values["*OPC?"] = reply
            with self.subTest(reply=reply), self.assertRaisesRegex(NktError, "not acknowledged"):
                d.prepare(635)
            self.assertTrue(d.poisoned)
            self.assertFalse(d.state_current)
            self.assertEqual({k: v for k, v in d.last_confirmed_state.items() if k != "captured_at_utc"},
                             {k: v for k, v in last.items() if k != "captured_at_utc"})
            self.assertEqual(d.transcript[-1]["reply"], reply)
            self.assertIn("error", d.transcript[-1])
            count = len(d.resource.commands)
            with self.assertRaises(NktError):
                d.prepare(633)
            self.assertEqual(len(d.resource.commands), count)

    def test_malformed_acquisition_preserves_last_sample_and_poison(self):
        d = self.device()
        d.read_power_w(633)
        last_sample = d.last_sample
        d.resource.power_provider = lambda: "not numeric"
        with self.assertRaises(ValueError):
            d.read_sample(633)
        self.assertTrue(d.poisoned)
        self.assertFalse(d.state_current)
        self.assertIs(d.last_sample, last_sample)
        self.assertEqual(d.transcript[-1]["command"], "READ?")
        self.assertEqual(d.transcript[-1]["reply"], "not numeric")
        count = len(d.resource.commands)
        with self.assertRaises(NktError):
            d.read_sample(633)
        self.assertEqual(len(d.resource.commands), count)

    def test_requested_settings_confirmed_including_fixed_range(self):
        d = self.device()
        d.config = replace(d.config, auto_range=False, range_w=0.01, average_count=10)
        state = d.prepare(633)
        self.assertEqual(state["range_w"], 0.01)
        self.assertEqual(state["auto_range"], 0)
        self.assertEqual(state["average_count"], 10)
        d = self.device()
        d.resource.write = lambda cmd: None
        with self.assertRaises(NktError):
            d.prepare(635)

    def test_timeout_poison_preserves_last_confirmed_and_closes_all(self):
        d = self.device()
        d.capture()
        last = d.last_confirmed_state
        d.resource.query = lambda cmd: (_ for _ in ()).throw(TimeoutError("lost"))
        with self.assertRaises(TimeoutError):
            d.capture()
        self.assertIs(d.last_confirmed_state, last)
        self.assertFalse(d.state_current)
        count = len(d.transcript)
        with self.assertRaises(NktError):
            d.capture()
        self.assertEqual(count, len(d.transcript))
        d.manager = Mock()
        d.resource.close = lambda: (_ for _ in ()).throw(OSError("close"))
        with self.assertRaises(NktError):
            d.close()
        d.manager.close.assert_called_once()

    def test_state_change_during_measurement_rejects_sample(self):
        resource = SimulatedPmResource()
        def power():
            resource.values["SENS:CORR:WAV?"] = "700"
            return 0.001
        resource.power_provider = power
        d = self.device(resource)
        with self.assertRaises(NktError):
            d.read_power_w(633)
        self.assertIsNone(d.last_sample)
        self.assertIn(("query", "READ?"), resource.commands)

    def test_broadband_rejected_without_setting_writes(self):
        d = self.device()
        with self.assertRaises(NktError):
            d.read_power_w(None)
        self.assertEqual(d.resource.commands, [])

    def test_exclusive_visa_owner_and_open_failure_release(self):
        c = replace(load_pm_config(EXAMPLE), backend="visa", resource="FAKE_OWNER")
        manager = Mock()
        manager.open_resource.return_value = SimulatedPmResource()
        d = Pm100d.open(c, authorize_connection=True, manager_factory=lambda: manager)
        manager.open_resource.assert_called_once_with("FAKE_OWNER", access_mode=1)
        with self.assertRaises(NktError):
            Pm100d.open(c, authorize_connection=True, manager_factory=lambda: manager)
        d.close()
        manager.open_resource.side_effect = OSError("open")
        with self.assertRaises(OSError):
            Pm100d.open(c, authorize_connection=True, manager_factory=lambda: manager)
        manager.open_resource.side_effect = None
        d = Pm100d.open(c, authorize_connection=True, manager_factory=lambda: manager)
        d.close()


class WindowTests(unittest.TestCase):
    def sample(self, sequence, time, power=0.001):
        return {"sequence": sequence, "started_monotonic_s": time, "finished_monotonic_s": time,
                "power_w": power, "unit": "W", "valid": True}

    def window(self):
        return PowerWindow(WindowConfig(1, 3, 0.1, 1e-5), epoch_s=0, max_power_w=0.01)

    def feedback(self):
        return FeedbackConfig(0.001, 0.00002, 0.01, 10, 1, 10, 90, 5, False,
                              WindowConfig(1, 3, 0.1, 1e-5))

    def test_repeated_equal_new_acquisitions_are_stable(self):
        w = self.window()
        for i, t in enumerate((0, 0.5, 1)):
            result = w.add(self.sample(i + 1, t))
        self.assertTrue(result["power_window_stable"])
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["std_power_w"], 0)

    def test_window_stable_and_target_are_separate(self):
        w = self.window()
        for i, t in enumerate((0, 0.5, 1)):
            result = w.add(self.sample(i + 1, t, 0.002))
        self.assertTrue(result["power_window_stable"])
        self.assertFalse(target_in_tolerance(result["mean_power_w"], self.feedback()))
        w = self.window()
        for i, t in enumerate((0, 0.5, 1)):
            result = w.add(self.sample(i + 1, t, 0.001 + (1 if i % 2 else -1) * 0.0001))
        self.assertFalse(result["power_window_stable"])

    def test_stale_invalid_duplicate_and_power_limit(self):
        for sample in (self.sample(2, -1), self.sample(1, 1), self.sample(2, 1, 0.02),
                       {**self.sample(2, 1), "valid": False}, {**self.sample(2, 1), "unit": "DBM"}):
            w = self.window()
            w.add(self.sample(1, 0))
            with self.subTest(sample=sample), self.assertRaises(NktError):
                w.add(sample)

    def test_window_reset_drops_prior_stable_proof(self):
        w = self.window()
        for i in range(3):
            w.add(self.sample(i + 1, i / 2))
        new = PowerWindow(w.config, epoch_s=2, max_power_w=0.01)
        self.assertFalse(new.add(self.sample(4, 2))["power_window_stable"])
        with self.assertRaises(NktError):
            new.add(self.sample(5, 1))

    def test_long_acquisition_gap_invalidates_stable_window(self):
        w = self.window()
        for i in range(3):
            w.add(self.sample(i + 1, i / 2))
        result = w.add(self.sample(4, 10))
        self.assertFalse(result["power_window_stable"])
        self.assertEqual(result["count"], 1)

    def test_bounded_direction_quantization_unreachable_and_zero_signal(self):
        cfg = self.feedback()
        self.assertEqual(next_nd_pct(50, 0.002, cfg), 55)
        self.assertEqual(next_nd_pct(50, 0.0005, cfg), 45)
        self.assertEqual(next_nd_pct(50, 0.002, replace(cfg, increasing_nd_increases_power=True)), 45)
        self.assertEqual(next_nd_pct(88, 0.002, cfg), 90)
        for nd, power in ((90, 0.002), (10, 0.0005), (50, 0)):
            with self.assertRaises(NktError):
                next_nd_pct(nd, power, cfg)
