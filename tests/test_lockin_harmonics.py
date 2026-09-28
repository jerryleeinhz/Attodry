"""No real instruments: exercise per-harmonic configuration and acquisition."""
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import re
from pathlib import Path
import unittest
from unittest.mock import patch

from attodry_control.config import ConfigError, load_config
from attodry_control.lockin_test import run
from attodry_control.sr830 import Sr830Error
from tests import test_sr830 as fixtures
from tests import test_config as config_tests
from tests import test_combination_hardware as combination


FIXED = '''
[lockin_xy.harmonic_settings.h1]
sensitivity_full_scale_v = 0.100
reserve_mode = "low_noise"
[lockin_xy.harmonic_settings.h2]
sensitivity_mode = "fixed"
sensitivity_full_scale_v = 0.002
reserve_mode = "normal"
'''


class HarmonicConfigTests(unittest.TestCase):
    simulation_text = config_tests.ConfigurationTests.simulation_text
    load_text = config_tests.ConfigurationTests.load_text

    def test_optional_inheritance_and_strict_overrides(self):
        original = self.load_text(self.simulation_text())
        self.assertEqual(original.lockin_xy.harmonic_settings, ())
        parsed = self.load_text(self.simulation_text() + FIXED)
        self.assertEqual([s.sensitivity_full_scale_v for s in parsed.lockin_xy.harmonic_settings], [.1, .002])
        self.assertEqual(parsed.lockin_xy.harmonic_settings[0].sensitivity_mode, 'fixed')
        for invalid in ('[lockin_xy.harmonic_settings.h4]\nreserve_mode="normal"',
                        '[lockin_xy.harmonic_settings.h2]',
                        '[lockin_xy.harmonic_settings.h2]\nsensitivity_full_scale_v=0.005',
                        '[lockin_xy.harmonic_settings.h2]\nreserve_mode="typo"',
                        '[lockin_xy.harmonic_settings.h2]\nfrequency_hz=100',
                        '[lockin_xy.harmonic_settings.h2]\nsensitivity_full_scale_v=nan'):
            with self.subTest(invalid=invalid), self.assertRaises(ConfigError):
                self.load_text(self.simulation_text() + '\n' + invalid)


class HarmonicSweepTests(unittest.TestCase):
    setUp = fixtures.Sr830Tests.setUp
    _hardware_config = fixtures.Sr830Tests._hardware_config

    def execute(self, command='sweep-frequency', *, extra=FIXED, mutate=None, config_edit=None):
        path = self._hardware_config()
        source = path.read_text(encoding='utf-8') + extra
        if command == 'sweep-frequency-excitation':
            source = re.sub(r'(?m)^frequency_ranges = \[.*?^\]', 'frequency_points_hz = [17.777, 1000]', source, flags=re.S)
            source = re.sub(r'(?m)^excitation_ranges = \[.*?^\]', 'excitation_points_v_rms = [0.004, 0.008]', source, flags=re.S)
        path.write_text(source if config_edit is None else config_edit(source), encoding='utf-8')
        shared = {'hz': 17.777}
        xx = fixtures.TrackingVisaResource(fixtures.responses(1), shared_frequency=shared, name='xx')
        xy = fixtures.TrackingVisaResource(fixtures.responses(0), shared_frequency=shared, name='xy')
        events = []
        xx.events = xy.events = events
        manager = fixtures.FakeResourceManager({'XX': xx, 'XY': xy})
        if mutate:
            mutate(xx, xy)
        options = {'sweep-frequency': ['--points-hz', '17.777,1000'],
                   'sweep-excitation': ['--points-v', '0.004,0.008'],
                   'sweep-frequency-excitation': []}
        out, err = io.StringIO(), io.StringIO()
        with patch('attodry_control.lockin_test.time.sleep'), redirect_stdout(out), redirect_stderr(err):
            try:
                code = run([command, '--config', str(path), '--xx-address', 'XX', '--xy-address', 'XY',
                            '--samples-per-point', '1', *options[command]], resource_manager_factory=lambda: manager)
            except (KeyboardInterrupt, OSError, Sr830Error):
                code = 1
        self.assertTrue(out.getvalue().strip(), err.getvalue())
        return code, json.loads(out.getvalue()), xx, xy, path

    def test_all_sweeps_settings_metadata_and_baseline_cleanup(self):
        for command in ('sweep-frequency', 'sweep-excitation', 'sweep-frequency-excitation'):
            with self.subTest(command=command):
                code, result, xx, xy, path = self.execute(command)
                self.assertEqual(code, 0, result)
                self.assertTrue(result['cleanup']['verified'])
                formal = [s for p in result['points'] for s in p['samples']]
                self.assertTrue(formal)
                for sample in formal:
                    settings = sample['settings_before']['roles']['lockin_xy']
                    expected = {1: (.1, 'low_noise'), 2: (.002, 'normal'), 3: (.001, 'normal')}
                    self.assertEqual((settings['sensitivity_full_scale_v'], settings['reserve_mode']),
                                     expected[sample['harmonic']])
                    self.assertTrue(sample['settings_verified'])
                    self.assertEqual(settings, sample['settings_after']['roles']['lockin_xy'])
                self.assertEqual(xy.responses['SENS?'].strip(), '17')
                self.assertEqual(xy.responses['HARM?'].strip(), '1')
                self.assertEqual(xy.responses['RMOD?'].strip(), '1')
                self.assertEqual(float(xx.responses['SLVL?']), .004)
                # Reference filename is authoritative (not guessed folder layout).
                directory = path.parent / f'run_data/lockin_commissioning_{path.stem}'
                profile = json.loads((directory / result['measurement_profile_ref']['path']).read_text(encoding='utf-8'))
                self.assertIn('harmonic_settings', json.dumps(profile))

    def test_widen_before_harmonic_then_narrow_after_transition(self):
        code, result, xx, xy, _ = self.execute()
        self.assertEqual(code, 0, result)
        writes = xy.writes
        h2 = writes.index('HARM 2')
        narrow = writes.index('SENS 18')
        h3 = writes.index('HARM 3')
        self.assertLess(writes.index('SENS 23'), h2)
        self.assertLess(h2, narrow)
        self.assertLess(narrow, writes.index('SENS 23', narrow))
        self.assertLess(writes.index('SENS 23', narrow), h3)
        self.assertNotIn('AGAN', writes)

    def test_failure_and_ctrl_c_keep_attempt_and_restore(self):
        for error in (OSError('injected write'), KeyboardInterrupt()):
            def mutate(xx, xy):
                original = xy.write
                failed = False
                def write(command):
                    nonlocal failed
                    if command == 'SENS 18' and not failed:
                        failed = True
                        raise error
                    return original(command)
                xy.write = write
            with self.subTest(error=type(error).__name__):
                code, result, xx, xy, _ = self.execute(mutate=mutate)
                self.assertNotEqual(code, 0)
                self.assertTrue(result['cleanup']['verified'], result)
                self.assertEqual(xy.responses['HARM?'].strip(), '1')
                self.assertEqual(xy.responses['SENS?'].strip(), '17')
                events = result['points'][0]['harmonic_settings_transitions']
                self.assertTrue(any('error' in e for e in events))
                self.assertFalse(any(s['harmonic'] == 2 for s in result['points'][0]['samples']))

    def test_partial_harmonic_write_cleanup(self):
        def mutate(xx, xy):
            original = xy.write
            def write(command):
                if command == 'HARM 2':
                    raise OSError('partial harmonic write')
                return original(command)
            xy.write = write
        code, result, xx, xy, _ = self.execute(mutate=mutate)
        self.assertNotEqual(code, 0)
        self.assertTrue(result['cleanup']['verified'], result)
        self.assertEqual(xx.responses['HARM?'].strip(), '1')
        self.assertEqual(xy.responses['HARM?'].strip(), '1')

    def test_readback_mismatch_rejects_before_h2_formal(self):
        def mutate(xx, xy):
            original = xy.query
            failed = False
            def query(command):
                nonlocal failed
                if command == 'SENS?' and xy.responses['SENS?'].strip() == '18' and not failed:
                    failed = True
                    return '17'
                return original(command)
            xy.query = query
        code, result, _, _, _ = self.execute(mutate=mutate)
        self.assertNotEqual(code, 0)
        self.assertTrue(result['cleanup']['verified'], result)
        self.assertFalse(any(s['harmonic'] == 2 for s in result['points'][0]['samples']))

    def test_persistent_companion_overload_rejected(self):
        def mutate(xx, xy):
            original = xx.query
            def query(command):
                if command == 'LIAS?' and xx.responses['HARM?'].strip() == '2':
                    return '1'
                return original(command)
            xx.query = query
        code, result, _, _, _ = self.execute(mutate=mutate)
        self.assertNotEqual(code, 0)
        self.assertFalse(any(s['harmonic'] == 2 for s in result['points'][0]['samples']))

    def test_other_role_autorange_remains_independent(self):
        def edit(source):
            start, end = source.index('[lockin_xx]'), source.index('[lockin_xy]')
            base = source[start:end].replace('sensitivity_mode = "fixed"', '''sensitivity_mode = "bounded_auto"
autorange_min_full_scale_v = 0.010
autorange_max_full_scale_v = 0.020
autorange_target_occupancy = 0.85
autorange_stable_samples = 2''').replace('sensitivity_full_scale_v = 0.020', 'sensitivity_full_scale_v = 0.010')
            return source[:start] + base + source[end:]
        def mutate(xx, xy):
            xx.responses['SENS?'] = '21'
        code, result, _, _, _ = self.execute(config_edit=edit, mutate=mutate)
        self.assertEqual(code, 0, result)

    def test_segment_conflict_rejected_before_open(self):
        path = self._hardware_config()
        source = path.read_text(encoding='utf-8').replace('min = 0.004, max = 0.400,', 'min = 0.004, max = 0.400, xy_full_scale_v = 0.002,') + FIXED
        path.write_text(source, encoding='utf-8')
        factory = unittest.mock.Mock()
        with self.assertRaises(ConfigError):
            load_config(path, safety_path=Path('config/lockin_safety.toml'))
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            with self.assertRaises(ConfigError):
                run(['sweep-excitation', '--config', str(path)], resource_manager_factory=factory)
        factory.assert_not_called()


class HarmonicCombinationTests(unittest.TestCase):
    setUp = combination.ElectricalCombinationTests.setUp
    write_config = combination.ElectricalCombinationTests.write_config
    factory = combination.ElectricalCombinationTests.factory
    execute = combination.ElectricalCombinationTests.execute
    events = combination.ElectricalCombinationTests.events

    def test_combination_reuses_harmonic_settings(self):
        self.write_config(extra=FIXED)
        result = self.execute()
        self.assertEqual(result['status'], 'completed', result)
        formal = [data for name, data in self.events() if name == 'lockin_formal_pair']
        self.assertTrue(formal)
        self.assertTrue(all(p['settings_verified'] for p in formal))
        self.assertEqual({p['settings_before']['roles']['lockin_xy']['sensitivity_full_scale_v'] for p in formal}, {.1, .002, .01})


if __name__ == '__main__':
    unittest.main()
