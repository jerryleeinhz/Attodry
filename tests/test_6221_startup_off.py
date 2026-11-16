"""Authorized startup stop uses complete prior evidence; no real instrument I/O."""
from copy import deepcopy
import json
import unittest

from attodry_control.keithley6221_ac import Keithley6221Ac, Keithley6221Error
from tests.test_keithley6221_ac import Fake6221, source_config


class StartupOffTests(unittest.TestCase):
    def test_recovery_can_require_an_already_stopped_source(self):
        resource, driver, _events = self.make()
        resource.state['OUTP:STAT'] = '1'
        with self.assertRaises(Keithley6221Error):
            driver.preflight(stop_active_output=False)
        self.assertEqual(resource.commands, [])

    def make(self, *, authorize=True, event=None):
        resource = Fake6221()
        events = []
        driver = Keithley6221Ac(resource, source_config(),
            event or (lambda kind, payload: events.append((kind, deepcopy(payload)))),
            authorize_writes=authorize)
        return resource, driver, events

    def test_healthy_active_source_is_stopped_and_then_can_be_configured(self):
        resource, driver, events = self.make()
        resource.state.update({'OUTP:STAT':'1', 'STAT:OPER:COND':'16'})
        state = driver.preflight()
        self.assertEqual(resource.commands, ['SOUR:WAVE:ABOR', 'OUTP:STAT OFF', 'SOUR:CURR 0'])
        self.assertFalse(state['output_enabled'])
        self.assertFalse(state['wave_started'])
        self.assertEqual(state['dc_current_setpoint_a'], 0.)
        self.assertTrue(state['startup_output_stop']['before']['output_enabled'])
        self.assertTrue(state['startup_output_stop']['verified'])
        json.dumps(state, allow_nan=False)
        self.assertTrue(driver.preflight_passed)
        self.assertFalse(driver.owned)
        self.assertEqual(resource.state['SOUR:CURR:COMP'], '1')
        kinds = [kind for kind, payload in events]
        self.assertLess(kinds.index('excitation_startup_stop_requested'), kinds.index('excitation_command_attempt'))
        self.assertIn('excitation_startup_output_stopped', kinds)
        driver.configure()
        self.assertTrue(driver.ready)
        self.assertNotIn('SOUR:WAVE:INIT', resource.commands)
        self.assertNotIn('*RST', resource.commands)
        self.assertNotIn('*CLS', resource.commands)

    def test_nonzero_dc_is_zeroed_only_after_confirming_output_off(self):
        resource, driver, events = self.make()
        resource.state.update({'OUTP:STAT':'1', 'SOUR:CURR':'0.0001'})
        original = resource.write
        def write(command):
            previous = resource.state['SOUR:CURR']
            original(command)
            if command == 'SOUR:WAVE:ABOR':
                resource.state['SOUR:CURR'] = previous
            if command == 'SOUR:CURR 0':
                self.assertEqual(resource.state['OUTP:STAT'], 'OFF')
                self.assertEqual(int(resource.state['STAT:OPER:COND']) & 16, 0)
        resource.write = write
        state = driver.preflight()
        proof = state['startup_output_stop']
        self.assertEqual(proof['before']['dc_current_setpoint_a'], .0001)
        self.assertEqual(proof['stop_confirmation'][-1]['dc_current_setpoint_a'], .0001)
        self.assertEqual(proof['final']['dc_current_setpoint_a'], 0.)

    def test_already_off_zero_keeps_existing_query_count_and_no_writes(self):
        resource, driver, events = self.make()
        state = driver.preflight()
        self.assertEqual(resource.commands, [])
        self.assertNotIn('startup_output_stop', state)
        self.assertEqual(resource.queries.count('OUTP:STAT?'), 1)
        self.assertFalse(any(kind.startswith('excitation_startup') for kind, _ in events))

    def test_no_write_authorization_keeps_strict_readonly_preflight(self):
        resource, driver, events = self.make(authorize=False)
        resource.state['OUTP:STAT'] = '1'
        with self.assertRaisesRegex(Keithley6221Error, 'OFF'):
            driver.preflight()
        self.assertEqual(resource.commands, [])
        self.assertNotIn('STAT:MEAS:EVEN?', resource.queries)
        self.assertFalse(driver.cleanup()['attempted'])

    def test_protection_faults_are_retained_and_never_cleared_by_startup_stop(self):
        for key, value in (('STAT:MEAS:COND','8'), ('STAT:MEAS:EVEN','8'),
                           ('STAT:QUES:COND','1'), ('STAT:OPER:COND','8'),
                           ('OUTP:INT:TRIP','0'), ('SYST:ERR','-100,"fault"')):
            with self.subTest(key=key):
                resource, driver, events = self.make()
                resource.state.update({'OUTP:STAT':'1', key:value})
                with self.assertRaises(Keithley6221Error): driver.preflight()
                self.assertEqual(resource.commands, [])
                self.assertTrue(driver.faulted)
                self.assertFalse(driver.preflight_passed)
                self.assertTrue(any(kind == 'excitation_raw_state' for kind, _ in events))
                if key == 'STAT:MEAS:EVEN':
                    self.assertTrue(any(kind == 'excitation_raw_state' and payload['measurement_event'] == 8
                                        for kind, payload in events))

    def test_malformed_state_and_query_failure_never_start_takeover(self):
        for key, value in (('OUTP:STAT','?'), ('SOUR:CURR','nan'), ('STAT:OPER:COND','bad'),
                           ('OUTP:INT:TRIP','?'), ('TRIG:OLIN','3')):
            with self.subTest(key=key):
                resource, driver, events = self.make()
                resource.state.update({'OUTP:STAT':'1', key:value})
                with self.assertRaises(Keithley6221Error): driver.preflight()
                self.assertEqual(resource.commands, [])
        resource, driver, _ = self.make()
        resource.state['OUTP:STAT'] = '1'
        resource.fail_query = 'OUTP:STAT?'
        with self.assertRaises(OSError): driver.preflight()
        self.assertEqual(resource.commands, [])

    def test_wrong_identity_is_never_given_a_stop_command(self):
        resource, driver, _ = self.make()
        resource.state.update({'*IDN':'KEITHLEY,MODEL 6220,123,A03','OUTP:STAT':'1'})
        with self.assertRaisesRegex(Keithley6221Error, 'identity'): driver.preflight()
        self.assertEqual(resource.commands, [])

    def test_audit_failure_before_stop_blocks_commands(self):
        def event(kind, payload):
            if kind == 'excitation_startup_stop_requested': raise OSError('audit failed')
        resource, driver, _ = self.make(event=event)
        resource.state['OUTP:STAT'] = '1'
        with self.assertRaisesRegex(OSError, 'audit failed'): driver.preflight()
        self.assertEqual(resource.commands, [])
        self.assertFalse(driver.cleanup()['attempted'])

    def test_partial_startup_write_failure_is_owned_for_cleanup(self):
        for command in ('SOUR:WAVE:ABOR','OUTP:STAT OFF','SOUR:CURR 0'):
            with self.subTest(command=command):
                resource, driver, _ = self.make()
                resource.state['OUTP:STAT'] = '1'
                resource.fail_write = command
                with self.assertRaises(OSError): driver.preflight()
                self.assertFalse(driver.preflight_passed)
                resource.fail_write = None
                result = driver.cleanup()
                self.assertTrue(result['attempted'])
                self.assertTrue(result['output_off_confirmed'])
                self.assertFalse(driver.ready)
                self.assertNotIn('SOUR:WAVE:INIT',resource.commands)

    def test_final_audit_failure_cannot_allow_configuration(self):
        def event(kind, payload):
            if kind == 'excitation_startup_output_stopped': raise OSError('audit failed')
        resource, driver, _ = self.make(event=event)
        resource.state['OUTP:STAT'] = '1'
        with self.assertRaisesRegex(OSError, 'audit failed'): driver.preflight()
        with self.assertRaisesRegex(Keithley6221Error, 'faulted'): driver.configure()
        self.assertTrue(driver.cleanup()['output_off_confirmed'])

    def test_ignored_output_stop_times_out_without_programming_or_start(self):
        resource, driver, _ = self.make()
        resource.state.update({'OUTP:STAT':'1','STAT:OPER:COND':'16'})
        clock = [0.]
        driver.monotonic = lambda: clock[0]
        driver.sleep = lambda duration: clock.__setitem__(0,clock[0]+duration)
        original = resource.write
        def write(command):
            original(command)
            if command in ('SOUR:WAVE:ABOR','OUTP:STAT OFF'):
                resource.state.update({'OUTP:STAT':'1','STAT:OPER:COND':'16'})
        resource.write = write
        with self.assertRaisesRegex(Keithley6221Error,'timeout'): driver.preflight()
        self.assertEqual(resource.commands,['SOUR:WAVE:ABOR','OUTP:STAT OFF'])
        self.assertLessEqual(resource.queries.count('OUTP:STAT?'),22)
        self.assertFalse(driver.cleanup()['output_off_confirmed'])

    def test_fault_during_ack_blocks_zeroing_and_start(self):
        resource, driver, _ = self.make()
        resource.state['OUTP:STAT'] = '1'
        original = resource.write
        def write(command):
            original(command)
            if command == 'OUTP:STAT OFF': resource.state['STAT:MEAS:EVEN'] = '8'
        resource.write = write
        with self.assertRaisesRegex(Keithley6221Error,'latched compliance'): driver.preflight()
        self.assertEqual(resource.commands,['SOUR:WAVE:ABOR','OUTP:STAT OFF'])
        self.assertFalse(driver.preflight_passed)


if __name__ == '__main__':
    unittest.main()
