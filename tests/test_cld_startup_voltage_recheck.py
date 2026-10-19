"""A single two-second startup recheck, using only fake hardware and clock."""
import math
import unittest

from optical_helpers import Clock
from tests.test_cld_drivers import configuration
from attodry_control.cld_optical import CldOpticalController
from attodry_control.nkt_config import NktError
from attodry_control.photoresponse_cld import CldController, SimulatedCld


class StartupBackend(SimulatedCld):
    is_hardware = True

    def __init__(self, config, on_read=None):
        super().__init__(config)
        self.state['diagnostics'] = {'qualification': 'unqualified', 'ld_connection_raw': 0, 'tec_connection_raw': 0,
                                     'measurement_condition_raw': 0}
        self.on_read = on_read or (lambda n: {})
        self.on_reads = 0
        self.writes = []

    def set(self, name, value):
        self.writes.append((name, value))
        super().set(name, value)

    def snapshot(self):
        state = super().snapshot()
        if state['getLdOutputState']:
            self.on_reads += 1
            state['measLdVolt'] = 1.98
            state.update(self.on_read(self.on_reads))
        return state


def prepared(on_read=None, cls=CldController):
    backend = StartupBackend(configuration(poll_interval_s=.2), on_read)
    clock = Clock()
    session = cls(backend.config, backend, clock=clock, sleep=clock.sleep)
    session.begin(authorize_writes=True, confirm_manual_route=True)
    session.prepare_point(backend.config.points[0])
    return session, backend, clock


class CldStartupVoltageRecheckTests(unittest.TestCase):
    def test_observed_negative_startup_waits_two_seconds_once_and_preserves_raw(self):
        session, backend, clock = prepared(lambda n: {'measLdVolt': -9.205192e-6,
                                                      'measLdCurr': 25.79391e-6} if n == 1 else {})
        start = clock()
        result = session.set_emission(True)
        self.assertAlmostEqual(clock()-start, 2.)
        self.assertEqual(result['source']['measLdVolt'], 1.98)
        self.assertEqual(sum(row == ('switchLdOutput', True) for row in backend.writes), 1)
        self.assertTrue(any(row.get('cld', {}).get('measLdVolt') == -9.205192e-6
                            for row in session.record['events']))
        self.assertTrue(session.finish()['off_confirmed'])

    def test_normal_start_has_no_added_wait_or_snapshot(self):
        session, backend, clock = prepared()
        start = clock()
        session.set_emission(True)
        self.assertEqual(clock(), start)
        self.assertEqual(backend.on_reads, 1)
        session.finish()

    def test_persistent_small_negative_fails_after_two_seconds_and_cleanup_still_runs(self):
        session, backend, clock = prepared(lambda n: {'measLdVolt': -9e-6})
        start = clock()
        with self.assertRaisesRegex(NktError, 'diode voltage'):
            session.set_emission(True)
        self.assertAlmostEqual(clock()-start, 2.)
        self.assertTrue(session.failed)
        self.assertTrue(session.finish()['off_confirmed'])
        self.assertEqual(backend.state['getLdCurrSetpoint'], 0.)

    def test_large_or_nonfinite_negative_does_not_wait(self):
        for value in (-.001, math.nextafter(-.0004, -math.inf), -math.inf, math.nan):
            with self.subTest(value=value):
                session, _, clock = prepared(lambda n: {'measLdVolt': value})
                start = clock()
                with self.assertRaises(NktError): session.set_emission(True)
                self.assertEqual(clock(), start)
                session.finish()

    def test_negative_noise_boundary_is_inclusive_but_final_check_is_strict(self):
        session, _, clock = prepared(lambda n: {'measLdVolt': -.0004} if n == 1 else {})
        start = clock()
        session.set_emission(True)
        self.assertAlmostEqual(clock()-start, 2.)
        session.finish()

    def test_overlapping_fault_does_not_enter_wait(self):
        faults = ({'isTrippedLdOutputProtInterlock': True}, {'measLdCurr': .031},
                  {'measTemp': 31}, {'getLdOperatingMode': 1}, {'getLdOutputState': False},
                  {'getLdCurrSetpoint': .02}, {'getTempSetpoint': 26},
                  {'diagnostics': {'ld_connection_raw': 0, 'tec_connection_raw': 0,
                                   'measurement_condition_raw': 8200}})
        for fault in faults:
            with self.subTest(fault=fault):
                session, _, clock = prepared(lambda n: {'measLdVolt': -9e-6, **fault})
                start = clock()
                with self.assertRaises(NktError): session.set_emission(True)
                self.assertEqual(clock(), start)
                session.finish()

    def test_other_fault_during_wait_stops_before_two_seconds(self):
        def injected(n):
            return {'measLdVolt': -9e-6, **({'isTrippedTecOutputProtVoltage': True} if n > 1 else {})}
        session, _, clock = prepared(injected)
        start = clock()
        with self.assertRaisesRegex(NktError, 'TEC protection'):
            session.set_emission(True)
        self.assertLess(clock()-start, 2.)
        session.finish()

    def test_formal_read_and_off_state_keep_existing_strict_negative_guard(self):
        session, backend, clock = prepared()
        session.set_emission(True)
        backend.on_read = lambda n: {'measLdVolt': -9e-6}
        start = clock()
        with self.assertRaisesRegex(NktError, 'diode voltage'): session.verify_point(emitting=True)
        self.assertEqual(clock(), start)
        session.finish()
        session, backend, clock = prepared()
        backend.state['measLdVolt'] = -9e-6
        with self.assertRaisesRegex(NktError, 'diode voltage'): session.set_emission(True)
        self.assertNotIn(('switchLdOutput', True), backend.writes)
        session.finish()

    def test_shared_deadline_cannot_be_extended_to_pay_for_wait(self):
        session, _, clock = prepared(lambda n: {'measLdVolt': -9e-6}, CldOpticalController)
        start = clock()
        with self.assertRaisesRegex(NktError, 'deadline'):
            session.start_emission(session.point, deadline=start+1.)
        self.assertEqual(clock(), start)
        session.finish()

    def test_read_failure_during_wait_is_not_retried(self):
        def injected(n):
            if n > 1: raise OSError('lost startup response')
            return {'measLdVolt': -9e-6}
        session, backend, clock = prepared(injected)
        start = clock()
        with self.assertRaisesRegex(OSError, 'lost startup response'): session.set_emission(True)
        self.assertLess(clock()-start, 2.)
        self.assertEqual(backend.on_reads, 2)
        session.finish()

    def test_interrupt_during_wait_leaves_failed_state_for_independent_cleanup(self):
        session, _, _ = prepared(lambda n: {'measLdVolt': -9e-6})
        def interrupted(seconds): raise KeyboardInterrupt()
        session.sleep = interrupted
        with self.assertRaises(KeyboardInterrupt): session.set_emission(True)
        self.assertTrue(session.failed)
        self.assertTrue(session.finish()['off_confirmed'])


if __name__ == '__main__': unittest.main()
