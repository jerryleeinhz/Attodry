from dataclasses import replace
import unittest
from unittest.mock import patch

from optical_helpers import EXAMPLE, Clock
from attodry_control.optical_config import load_pem_config, peak_retardance_nm
from attodry_control.nkt_config import NktError
from attodry_control.pem import Pem, SerialTransport, SimulatedPemTransport


class PemTests(unittest.TestCase):
    def device(self, transport=None, **kwargs):
        clock = Clock()
        return Pem(load_pem_config(EXAMPLE), transport or SimulatedPemTransport(),
                   clock=clock, sleep=clock.sleep, **kwargs)

    def test_quarter_wave_and_invalid_inputs(self):
        for wavelength, expected in ((400, 100), (633, 158.25), (840, 210)):
            self.assertEqual(peak_retardance_nm(wavelength, 0.25), expected)
        for value in (None, True, 0, -1, float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(NktError):
                peak_retardance_nm(value, 0.25)

    def test_protocol_ready_and_disable_ack_is_not_physical_off(self):
        d = self.device()
        d.preflight()
        evidence = d.prepare(633, 0.25)
        self.assertEqual(evidence["readback"]["amplitude_nm"], 158.25)
        self.assertIn(":MOD:AMP 158.25", d.transport.commands)
        self.assertIn(":SYS:PEMO 1", d.transport.commands)
        cleanup = d.finish()
        self.assertTrue(cleanup["disable_acknowledged"])
        self.assertFalse(cleanup["physical_off_confirmed"])
        self.assertTrue(d.transport.closed)

    def test_disabled_firmware_needs_activation_before_amp_reply(self):
        transport = SimulatedPemTransport()
        exchange = transport.exchange
        def require_active(command):
            if command.startswith(":MOD:AMP ") and not transport.active:
                raise TimeoutError("V01 AMP completion waits for active modulation")
            return exchange(command)
        transport.exchange = require_active
        d = self.device(transport)
        d.preflight()
        d.prepare(633, 0.25)
        self.assertTrue(d.verify_ready()["stable"])
        self.assertTrue(d.finish()["disable_acknowledged"])

    def test_device_amplitude_limit_checked_before_activation(self):
        d = self.device()
        d.preflight()
        exchange = d.transport.exchange
        d.transport.exchange = lambda cmd: "[AMPR](10,150)\n" if cmd == ":MOD:AMPR?" else exchange(cmd)
        with self.assertRaises(NktError):
            d.prepare(633, 0.25)
        self.assertFalse(any(cmd.startswith((":MOD:AMP ", ":SYS:PEMO "))
                             for cmd in d.transport.commands))
        d.finish()

    def test_existing_amplitude_limit_checked_before_activation(self):
        for current, configured_max in ((300, 200), (600, 800)):
            d = self.device()
            d.config = replace(d.config, amplitude_max_nm=configured_max)
            d.preflight()
            # A fresh query must catch a stored setting changed after preflight.
            d.transport.amplitude_nm = current
            with self.subTest(current=current), self.assertRaises(NktError):
                d.prepare(633, 0.25)
            self.assertFalse(any(cmd.startswith((":MOD:AMP ", ":SYS:PEMO "))
                                 for cmd in d.transport.commands))
            self.assertEqual(d.transcript[-1]["command"], ":MOD:AMP?")
            self.assertEqual(d.transcript[-1]["reply"], f"[AMP]({current})\n")
            self.assertTrue(d.finish()["disable_acknowledged"])

    def test_deadline_after_activation_allows_acknowledged_disable(self):
        d = self.device()
        d.preflight()
        exchange = d.transport.exchange
        def expire_after_activation(command):
            reply = exchange(command)
            if command == ":SYS:PEMO 1":
                d.clock.value = 1.0
            return reply
        d.transport.exchange = expire_after_activation
        with self.assertRaisesRegex(NktError, "before amplitude setting"):
            d.prepare(633, 0.25, deadline=1.0)
        self.assertTrue(d.output_command)
        self.assertEqual(d.transport.active, 1)
        self.assertFalse(d.poisoned)
        self.assertFalse(any(cmd.startswith(":MOD:AMP ") for cmd in d.transport.commands))
        cleanup = d.finish()
        self.assertTrue(cleanup["disable_acknowledged"])
        self.assertFalse(cleanup["physical_off_confirmed"])
        self.assertEqual(cleanup["errors"], [])
        self.assertEqual(d.transport.commands[-1], ":SYS:PEMO 0")
        self.assertEqual(d.transport.active, 0)
        self.assertTrue(d.transport.closed)

    def test_response_errors_poison_session_without_retry(self):
        for reply in ("<SCPINOP>(unknown)\n", "[AMP](1)\n", "[IDN](old)",
                      "[IDN](old)\n[IDN](new)\n", "garbage\n"):
            transport = SimulatedPemTransport()
            transport.exchange = lambda cmd: reply
            d = self.device(transport)
            with self.subTest(reply=reply), self.assertRaises(NktError):
                d.identify()
            with self.assertRaises(NktError):
                d.identify()
            self.assertEqual(len(d.transcript), 1)

    def test_frequency_anomaly_refuses_ownership(self):
        d = self.device()
        d.transport.frequency_hz = 32355.3
        with self.assertRaises(NktError):
            d.preflight()
        self.assertFalse(d.owned)
        d.finish()
        self.assertFalse(any(cmd.startswith(":SYS:PEMO") for cmd in d.transport.commands))

    def test_commissioned_identity_word_order_preserves_exact_match(self):
        d = self.device()
        identity = "Hinds PEM controller 200 V01"
        d.config = replace(d.config, expected_idn=identity)
        d.transport.exchange = lambda cmd: f"[IDN]({identity})\n"
        self.assertEqual(d.identify(), identity)
        self.assertFalse(d.poisoned)
        d.config = replace(d.config, expected_idn="Hinds PEM controller 200 V02")
        with self.assertRaisesRegex(NktError, "identity mismatch"):
            d.identify()
        self.assertTrue(d.poisoned)

    def test_malformed_values_poison_and_preserve_last_confirmed(self):
        cases = (("read_amplitude_range_nm", "[AMPR](550,10)\n"),
                 ("read_amplitude_range_nm", "[AMPR](10)\n"),
                 ("read_peak_retardance_nm", "[AMP](NaN)\n"),
                 ("read_modulation_frequency_hz", "[FREQUENCY](garbage)\n"),
                 ("read_stable", "[STABLE](2)\n"))
        for method, reply in cases:
            d = self.device()
            last = d.capture()
            d.transport.exchange = lambda cmd: reply
            with self.subTest(method=method, reply=reply), self.assertRaises(NktError):
                getattr(d, method)()
            self.assertTrue(d.poisoned)
            self.assertFalse(d.state_current)
            self.assertIs(d.last_confirmed_state, last)
            self.assertEqual(d.transcript[-1]["reply"], reply)
            self.assertIn("validation_error", d.transcript[-1])
            count = len(d.transcript)
            with self.assertRaises(NktError):
                d.capture()
            self.assertEqual(len(d.transcript), count)

    def test_invalid_setting_ack_blocks_cleanup_command(self):
        for method, value, reply in (("set_peak_retardance_nm", 100, "[AMP](99)\n"),
                                     ("set_peak_retardance_nm", 100, "[AMP](Inf)\n"),
                                     ("set_modulation", True, "[PEMOUT](0)\n")):
            d = self.device()
            d.preflight()
            exchange = d.transport.exchange
            d.transport.exchange = lambda cmd: exchange(cmd) if cmd.endswith("?") else reply
            with self.subTest(method=method, reply=reply), self.assertRaises(NktError):
                getattr(d, method)(value)
            self.assertTrue(d.poisoned)
            count = len(d.transcript)
            cleanup = d.finish()
            self.assertTrue(cleanup["errors"])
            self.assertFalse(cleanup["disable_acknowledged"])
            self.assertEqual(len(d.transcript), count)

    def test_amplitude_range_and_last_confirmed_on_failure(self):
        d = self.device()
        d.preflight()
        with self.assertRaises(NktError):
            d.set_peak_retardance_nm(600)
        d.prepare(633, 0.25)
        last = d.last_confirmed_state
        d.transport.exchange = lambda cmd: (_ for _ in ()).throw(TimeoutError("lost"))
        with self.assertRaises(TimeoutError):
            d.capture()
        self.assertIs(d.last_confirmed_state, last)
        self.assertFalse(d.state_current)
        result = d.finish()
        self.assertTrue(result["errors"])
        self.assertFalse(result["disable_acknowledged"])

    def test_unstable_timeout_and_ready_state_drift(self):
        d = self.device()
        d.preflight()
        d.transport.stable = 0
        with self.assertRaises(NktError):
            d.prepare(633, 0.25)
        d.transport.stable = 1
        d.prepare(633, 0.25)
        d.transport.amplitude_nm = 1
        with self.assertRaises(NktError):
            d.verify_ready()
        d.finish()

    def test_identity_exact_match_and_illegal_stable_flag(self):
        d = self.device()
        d.config = replace(d.config, expected_idn="Hinds PEM 200 controller V01")
        with self.assertRaises(NktError):
            d.identify()
        d = self.device()
        d.transport.stable = 2
        with self.assertRaises(NktError):
            d.read_stable()

    def test_unauthorized_connection_and_writes_zero_io(self):
        config = replace(load_pem_config(EXAMPLE), backend="serial", port="FAKE",
                         expected_idn="Hinds PEM 200 controller SIMULATION")
        factory = unittest.mock.Mock()
        with self.assertRaises(NktError):
            SerialTransport(config, factory=factory)
        factory.assert_not_called()
        transport = SimulatedPemTransport()
        d = Pem(config, transport, is_hardware=True)
        with self.assertRaises(NktError):
            d.preflight()
        self.assertEqual(transport.commands, [])

    def test_serial_exact_line_and_stale_input(self):
        resource = unittest.mock.Mock()
        resource.in_waiting = 0
        resource.readline.return_value = b"[STABLE](1)\n"
        resource.write.return_value = len(b":MOD:STABLE?\n")
        config = replace(load_pem_config(EXAMPLE), backend="serial", port="FAKE")
        transport = SerialTransport(config, authorize_connection=True, factory=lambda **kw: resource)
        self.assertEqual(transport.exchange(":MOD:STABLE?"), "[STABLE](1)\n")
        resource.write.assert_called_once_with(b":MOD:STABLE?\n")
        resource.in_waiting = 1
        with self.assertRaises(NktError):
            transport.exchange(":MOD:STABLE?")
        self.assertEqual(resource.write.call_count, 1)
        transport.close()

    def test_serial_single_owner_and_failed_open_release(self):
        config = replace(load_pem_config(EXAMPLE), backend="serial", port="FAKE_OWNER")
        resource = unittest.mock.Mock()
        transport = SerialTransport(config, authorize_connection=True, factory=lambda **kw: resource)
        try:
            with self.assertRaises(NktError):
                SerialTransport(config, authorize_connection=True, factory=lambda **kw: resource)
        finally:
            transport.close()
        with self.assertRaises(OSError):
            SerialTransport(config, authorize_connection=True,
                            factory=lambda **kw: (_ for _ in ()).throw(OSError("open")))
        transport = SerialTransport(config, authorize_connection=True, factory=lambda **kw: resource)
        transport.close()

    def test_leave_active_and_close_failure_explicit(self):
        d = self.device()
        d.config = replace(d.config, finish_action="leave_active")
        d.preflight()
        d.prepare(633, 0.25)
        result = d.finish()
        self.assertFalse(result["disable_acknowledged"])
        self.assertEqual(d.transport.active, 1)
        d = self.device()
        d.preflight()
        d.transport.close = lambda: (_ for _ in ()).throw(OSError("close"))
        self.assertTrue(d.finish()["errors"])
