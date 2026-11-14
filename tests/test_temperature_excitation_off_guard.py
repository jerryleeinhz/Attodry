"""Condition-gated source output with fake transport/DLL only."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from attodry_control.combination_hardware import HardwareCombinationStation, _Events
from attodry_control.combination_resume import build_recovery
from attodry_control.combination_scan import AxisPoint, ScanAxis
from attodry_control.combination_store import open_readonly, validate_accepted_records
from attodry_control.keithley6221_ac import Keithley6221Ac, Keithley6221Error
from tests.test_attodry import FakeAttoDryDll
from tests import test_combination_6221 as fixtures
from tests.test_keithley6221_ac import Fake6221, source_config
from tests.test_lockin_settling import Clock


class SourcePauseTests(unittest.TestCase):
    def driver(self, *, active=True, event=None):
        resource = Fake6221()
        clock = Clock()
        driver = Keithley6221Ac(resource, source_config(compliance_voltage_v=20.0),
            event=event, authorize_writes=True, monotonic=clock.monotonic, sleep=clock.sleep)
        driver.preflight(); driver.configure()
        if active:
            driver.set_point(1e-6, 200.0)
        resource.commands.clear(); resource.queries.clear()
        return resource, driver, clock

    def test_active_stop_has_strict_proof_and_can_restart_same_point(self):
        resource, driver, clock = self.driver()
        proof = driver.stop_output()
        self.assertTrue(proof['verified'] and proof['output_off_confirmed'])
        self.assertEqual(proof['errors'], [])
        self.assertTrue(proof['before']['output_enabled'])
        self.assertFalse(proof['final']['output_enabled'])
        self.assertFalse(proof['final']['wave_started'])
        self.assertEqual(proof['final']['dc_current_setpoint_a'], 0)
        self.assertEqual(resource.commands, ['SOUR:WAVE:ABOR', 'OUTP:STAT OFF'])
        self.assertEqual(len(resource.queries), 35)
        self.assertEqual(clock.now, driver.last_change_monotonic_s)
        self.assertIsNone(driver.current_point)
        self.assertTrue(driver.ready)
        self.assertFalse(driver.faulted)
        driver.set_point(1e-6, 200.0)
        self.assertTrue(driver.readback()['output_enabled'])
        self.assertEqual(resource.commands.count('SOUR:WAVE:INIT'), 1)
        self.assertEqual(resource.state['SOUR:CURR:COMP'], '20')

    def test_already_off_is_fresh_read_only_proof(self):
        resource, driver, _ = self.driver(active=False)
        first = driver.stop_output()
        self.assertTrue(first['verified'])
        self.assertEqual(len(resource.queries), 24)
        self.assertEqual(resource.commands, [])
        first['final']['output_enabled'] = True
        second = driver.stop_output()
        self.assertFalse(second['final']['output_enabled'])
        self.assertEqual(len(resource.queries), 48)

    def test_live_or_latched_compliance_is_archived_before_stop_not_cleared(self):
        for word in ('STAT:MEAS:COND', 'STAT:MEAS:EVEN'):
            events = []
            resource, driver, _ = self.driver(event=lambda *args: events.append(args))
            resource.state[word] = '8'
            with self.subTest(word=word), self.assertRaisesRegex(Keithley6221Error, 'compliance'):
                driver.stop_output()
            self.assertEqual(resource.commands, [])
            self.assertTrue(driver.faulted)
            self.assertFalse(driver.ready)
            observed = [p for k, p in events if k == 'excitation_raw_state'][-1]
            key = 'measurement_event' if word.endswith('EVEN') else 'measurement_condition'
            self.assertEqual(observed[key], 8)
            with self.assertRaisesRegex(Keithley6221Error, 'faulted'):
                driver.set_point(1e-6, 200)

    def test_unknown_connection_error_and_nonzero_dc_do_not_pass(self):
        for key, value in (('STAT:OPER:COND', '32'), ('OUTP:INT:TRIP', '0'),
                           ('SOUR:CURR', '1e-9'), ('SOUR:CURR:COMP', '21'),
                           ('OUTP:ISH', 'GUAR')):
            resource, driver, _ = self.driver()
            resource.state[key] = value
            with self.subTest(key=key), self.assertRaises(Keithley6221Error):
                driver.stop_output()
            self.assertEqual(resource.commands, [])
            self.assertTrue(driver.faulted)

    def test_stop_ack_failure_does_not_retry_writes_or_allow_restart(self):
        resource, driver, clock = self.driver()
        original = resource.write
        def ignore_stop(command):
            if command in {'SOUR:WAVE:ABOR', 'OUTP:STAT OFF'}:
                resource.commands.append(command)
            else:
                original(command)
        resource.write = ignore_stop
        with self.assertRaisesRegex(Keithley6221Error, 'OFF not certified'):
            driver.stop_output()
        self.assertEqual(resource.commands, ['SOUR:WAVE:ABOR', 'OUTP:STAT OFF'])
        self.assertLessEqual(clock.now, 2.000001)
        self.assertTrue(driver.faulted)

    def test_audit_or_transport_failure_faults_pause(self):
        for kind in ('excitation_raw_state', 'excitation_output_stopped'):
            resource, driver, _ = self.driver()
            driver.event = lambda event, payload: (_ for _ in ()).throw(OSError('audit failed')) if event == kind else None
            with self.subTest(kind=kind), self.assertRaisesRegex(OSError, 'audit failed'):
                driver.stop_output()
            self.assertTrue(driver.faulted)
            with self.assertRaisesRegex(Keithley6221Error, 'faulted'):
                driver.set_point(1e-6, 200)
        resource, driver, _ = self.driver()
        resource.fail_query = 'OUTP:STAT?'
        with self.assertRaises(OSError):
            driver.stop_output()
        self.assertTrue(driver.faulted)


class ConditionGateStationTests(unittest.TestCase):
    def station(self):
        resource = Fake6221()
        driver = Keithley6221Ac(resource, source_config(compliance_voltage_v=20.0), authorize_writes=True)
        driver.preflight(); driver.configure()
        point = AxisPoint({'excitation_current_rms_a': 1e-6, 'excitation_frequency_hz': 200.0})
        station = HardwareCombinationStation(SimpleNamespace(plan=SimpleNamespace(
            axes=(ScanAxis('excitation', (point,)),))))
        station.excitation = driver
        trace = []
        station.set_event_sink(lambda k, p: trace.append((k, deepcopy(p))))
        station.lockin = SimpleNamespace(
            source_stopped=lambda proof: trace.append(('detector_stopped', proof)),
            prepare_source_change=lambda hz: trace.append(('detector_prepare', hz)),
            source_changed=lambda *args: trace.append(('detector_started', args)),
            qualify=lambda: trace.append(('detector_qualified', None)),
            disturbed=lambda module: trace.append(('detector_disturbed', module)))
        station.cryo = SimpleNamespace(temperature_qualification=None,
            apply=lambda *args: trace.append(('temperature_applied', args)) or {'ready': True},
            qualify=lambda **kwargs: trace.append(('temperature_qualified', kwargs)) or {'ready': True})
        return station, resource, driver, point, trace

    def test_all_axis_changes_precede_source_start_and_end_stops(self):
        station, resource, driver, point, trace = self.station()
        condition = {'condition_id': 'condition-000000'}
        station.begin_condition(condition)
        station.apply('temperature', AxisPoint({'temperature_k': 7.0}))
        station.qualify_axis('temperature', AxisPoint({'temperature_k': 7.0}), {})
        ready = station.apply('excitation', point)
        self.assertTrue(ready['deferred_until_condition_qualification'])
        self.assertEqual(resource.commands.count('SOUR:WAVE:INIT'), 0)
        station.qualify(('temperature', 'excitation', 'lockin'))
        self.assertTrue(driver.readback()['output_enabled'])
        names = [k for k, _ in trace]
        self.assertLess(names.index('temperature_qualified'), names.index('detector_started'))
        station.end_condition(condition)
        self.assertFalse(driver.verify_stopped()['output_enabled'])
        self.assertIsNone(driver.current_point)
        # A later inner-axis condition can reuse its coordinate but not old ON state.
        station.begin_condition({'condition_id': 'condition-000001'})
        station.qualify(('temperature', 'excitation', 'lockin'))
        self.assertEqual(resource.commands.count('SOUR:WAVE:INIT'), 2)

    def test_failed_off_guard_prevents_temperature_or_source_writes(self):
        station, resource, driver, point, trace = self.station()
        driver.set_point(1e-6, 200)
        resource.state['STAT:MEAS:EVEN'] = '8'
        resource.commands.clear()
        with self.assertRaisesRegex(Keithley6221Error, 'compliance'):
            station.begin_condition({'condition_id': 'condition-000001'})
        self.assertEqual(resource.commands, [])
        self.assertNotIn('temperature_applied', [k for k, _ in trace])

    def test_no_external_source_adds_no_lifecycle_actions(self):
        station = HardwareCombinationStation(SimpleNamespace())
        station.set_event_sink(lambda *args: self.fail('nonexternal path emitted source action'))
        station.begin_condition({'condition_id': 'test'})
        station.end_condition({'condition_id': 'test'})


class TemperatureSourceGridTests(unittest.TestCase):
    setUp = fixtures.Combination6221Tests.setUp
    devices = fixtures.Combination6221Tests.devices
    config = fixtures.Combination6221Tests.config
    execute = fixtures.Combination6221Tests.execute
    events = fixtures.Combination6221Tests.events

    def temperature_config(self, *, samples=1):
        example = (Path(__file__).resolve().parents[1] / 'config/hardware.example.toml').read_text(encoding='utf-8')
        sections = []
        for name in ('cryostat', 'magnet', 'temperature_stability', 'temperature_run', 'temperature_scan', 'cleanup'):
            section = re.search(r'(?ms)^\[' + name + r'\]\n.*?(?=^\[|\Z)', example).group()
            if name == 'cryostat':
                section = section.replace('CHANGE_ME_COM_PORT', 'COM5').replace('C:/CHANGE_ME/attoDRYxyz64bit.dll', 'FAKE.dll')
            if name == 'temperature_stability':
                section = section.replace('stable_dwell_s = 30.0', 'stable_dwell_s = 3.0')
            if name == 'temperature_scan':
                section = re.sub(r'(?s)temperature_ranges = \[.*?\]', 'start_k = 2.0\nstop_k = 7.0\nstep_k = 5.0', section, count=1)
            sections.append(section)
        text = self.text.replace('order = ["excitation", "lockin"]', 'order = ["temperature", "excitation", "lockin"]')
        text = text.replace('samples_per_condition = 1', 'samples_per_condition = ' + str(samples))
        self.path.write_text(text + '\n' + '\n'.join(sections), encoding='utf-8')
        dll = FakeAttoDryDll(); dll.temperature_follows_setpoint = True
        original_set = dll.AttoDRY_Interface_setUserTemperature
        def set_temperature(value):
            self.assertIn(self.source.state['OUTP:STAT'], {'0', 'OFF'})
            self.assertFalse(int(self.source.state['STAT:OPER:COND']) & 16)
            return original_set(value)
        dll.AttoDRY_Interface_setUserTemperature = set_temperature
        return dll

    def test_full_grid_source_is_off_at_every_condition_setting_and_dwell(self):
        from attodry_control.cryostat_points import CryostatPointSession
        dll = self.temperature_config(samples=2)
        original = CryostatPointSession.qualify
        def qualify(session, *args, **kwargs):
            self.assertIn(self.source.state['OUTP:STAT'], {'0', 'OFF'})
            self.assertFalse(int(self.source.state['STAT:OPER:COND']) & 16)
            return original(session, *args, **kwargs)
        with patch.object(CryostatPointSession, 'qualify', qualify):
            result = self.execute(authorize_cryostat=True, dll=dll)
        self.assertEqual(result['status'], 'completed', result)
        self.assertTrue(result['cleanup']['clean'], result)
        events = self.events()
        begins = [p for k, p in events if k == 'excitation_condition_off_verified' and p['stage'] == 'before_settings']
        ends = [p for k, p in events if k == 'excitation_condition_off_verified' and p['stage'] == 'after_samples']
        self.assertEqual(len(begins), 8)
        self.assertEqual(len(ends), 8)
        self.assertEqual(self.source.commands.count('SOUR:WAVE:INIT'), 8)
        self.assertEqual(sum(k == 'formal_sample' for k, p in events), 16)
        self.assertEqual(sum(k == 'temperature_qualification_completed' for k, p in events), 2)
        with closing(open_readonly(self.database)) as db:
            self.assertEqual(validate_accepted_records(db, 'current-run', self.config().snapshot)['accepted_conditions'], 8)

    def test_unqualified_temperature_never_starts_waveform(self):
        from attodry_control.cryostat_points import CryostatPointSession
        dll = self.temperature_config()
        with patch.object(CryostatPointSession, 'qualify', side_effect=RuntimeError('temperature pending')):
            result = self.execute(authorize_cryostat=True, dll=dll)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('temperature pending', result['error'])
        self.assertEqual(self.source.commands.count('SOUR:WAVE:INIT'), 0)
        self.assertEqual(sum(k == 'formal_sample' for k, p in self.events()), 0)

    def test_range_provenance_survives_source_restart_with_range_changes_off(self):
        dll = self.temperature_config()
        rules = '''
[[combination_scan.lockin_range_rules]]
name = "small_current"
match = { excitation_current_rms_a = [0.000001] }
lockin_xx_full_scale_v = 1.0
lockin_xy_full_scale_v = 0.010
[[combination_scan.lockin_range_rules]]
name = "large_current"
match = { excitation_current_rms_a = [0.000002] }
lockin_xx_full_scale_v = 1.0
lockin_xy_full_scale_v = 0.100
'''
        self.path.write_text(self.path.read_text(encoding='utf-8') + rules, encoding='utf-8')
        write = self.xy.write
        def write_off(command):
            if command.startswith('SCAL '):
                self.assertIn(self.source.state['OUTP:STAT'], {'0', 'OFF'})
            return write(command)
        self.xy.write = write_off
        result = self.execute(authorize_cryostat=True, dll=dll)
        self.assertEqual(result['status'], 'completed', result)
        starts = [p for k, p in self.events() if k == 'external_excitation_applied_to_detectors']
        self.assertEqual(len(starts), 8)
        self.assertEqual([p['lockin_range_rule']['rule_name'] for p in starts],
                         ['small_current', 'large_current'] * 4)
        self.assertTrue(all(p['lockin_range_rule']['verified'] is True for p in starts))
        with closing(open_readonly(self.database)) as db:
            self.assertEqual(validate_accepted_records(db, 'current-run')['accepted_conditions'], 8)

    def test_end_guard_audit_failure_rejects_raw_sample_and_stops_next_condition(self):
        dll = self.temperature_config()
        original = _Events.event
        def event(events, kind, payload):
            if kind == 'excitation_condition_off_verified' and payload['stage'] == 'after_samples':
                raise OSError('OFF proof audit failed')
            return original(events, kind, payload)
        with patch.object(_Events, 'event', event):
            result = self.execute(authorize_cryostat=True, dll=dll)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('OFF proof audit failed', result['error'])
        self.assertEqual(self.source.commands.count('SOUR:WAVE:INIT'), 1)
        self.assertIn(self.source.state['OUTP:STAT'], {'0', 'OFF'})
        with closing(open_readonly(self.database)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM combination_attempts WHERE status='accepted'").fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM combination_samples').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM combination_attempts').fetchone()[0], 1)

    def test_end_guard_current_compliance_rejects_without_changing_protection(self):
        dll = self.temperature_config()
        original = HardwareCombinationStation.end_condition
        def end(station, condition):
            self.source.state['STAT:MEAS:COND'] = '8'
            self.source.state['STAT:MEAS:EVEN'] = '8'
            return original(station, condition)
        with patch.object(HardwareCombinationStation, 'end_condition', end):
            result = self.execute(authorize_cryostat=True, dll=dll)
        self.assertEqual(result['status'], 'failed')
        self.assertIn('compliance', result['error'])
        self.assertEqual(self.source.commands.count('SOUR:WAVE:INIT'), 1)
        self.assertEqual(self.source.state['SOUR:CURR:COMP'], '1')
        with closing(open_readonly(self.database)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM combination_attempts WHERE status='accepted'").fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM combination_samples').fetchone()[0], 1)

    def test_resume_preserves_prefix_and_requalifies_pending_temperature_with_source_off(self):
        from attodry_control.cryostat_points import CryostatPointSession
        dll = self.temperature_config()
        original = CryostatPointSession.qualify
        def fail_second_temperature(session, *args, **kwargs):
            if session.temperature_target == 7:
                raise RuntimeError('injected temperature interruption')
            return original(session, *args, **kwargs)
        with patch.object(CryostatPointSession, 'qualify', fail_second_temperature):
            first = self.execute(authorize_cryostat=True, dll=dll)
        self.assertEqual(first['status'], 'failed', first)
        with closing(open_readonly(self.database)) as db:
            prefix = [tuple(row) for row in db.execute(
                'SELECT condition_id,attempt_index,sample_index,payload_json FROM combination_samples ORDER BY condition_id')]
        self.assertEqual(len(prefix), 4)
        recovery = build_recovery(self.config(), self.database, 'current-run', acknowledge_failure=True)
        self.assertTrue(recovery['review']['ready'], recovery['review'])
        self.devices()
        resumed_dll = self.temperature_config()
        def qualify_off(session, *args, **kwargs):
            self.assertIn(self.source.state['OUTP:STAT'], {'0', 'OFF'})
            return original(session, *args, **kwargs)
        with patch.object(CryostatPointSession, 'qualify', qualify_off):
            result = self.execute(recovery=recovery, authorize_cryostat=True, dll=resumed_dll)
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(self.source.commands.count('SOUR:WAVE:INIT'), 4)
        with closing(open_readonly(self.database)) as db:
            after = [tuple(row) for row in db.execute(
                'SELECT condition_id,attempt_index,sample_index,payload_json FROM combination_samples ORDER BY condition_id')]
            self.assertEqual(after[:4], prefix)
            self.assertEqual(len(after), 8)
            self.assertEqual(db.execute("SELECT count(*) FROM combination_attempts WHERE status='rejected'").fetchone()[0], 1)
            self.assertEqual(validate_accepted_records(db, 'current-run', self.config().snapshot)['accepted_conditions'], 8)


if __name__ == '__main__':
    unittest.main()
