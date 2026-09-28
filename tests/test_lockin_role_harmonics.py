"""Regression: role selection controls HARM writes, not only plot filtering."""
import re
import json
import unittest

from tests import test_lockin_harmonics as fixtures
from attodry_control.lockin_progress_monitor import LockinProgressView
from attodry_control.commissioning_analysis import load_sweep_samples

FIXED, AUTO = fixtures.FIXED, fixtures.AUTO


def select_roles(text, xx='1', xy='2'):
    for scan in ('frequency', 'excitation', 'combined'):
        for role, values in (('xx', xx), ('xy', xy)):
            text = re.sub(rf'(?m)^{scan}_{role}_harmonics = \[.*?\]',
                          f'{scan}_{role}_harmonics = [{values}]', text)
    return text


class RoleHarmonicTests(unittest.TestCase):
    setUp = fixtures.HarmonicSweepTests.setUp
    _hardware_config = fixtures.HarmonicSweepTests._hardware_config
    execute = fixtures.HarmonicSweepTests.execute

    def test_single_harmonic_roles_stay_independent_all_sweeps(self):
        for command in ('sweep-frequency', 'sweep-excitation', 'sweep-frequency-excitation'):
            for extra in ('', FIXED, AUTO):
                with self.subTest(command=command, extra=extra):
                    code, result, xx, xy, _ = self.execute(command, extra=extra, config_edit=select_roles)
                    self.assertEqual(code, 0, result.get('error'))
                    self.assertNotIn('HARM 2', xx.writes)
                    self.assertNotIn('HARM 3', xx.writes)
                    self.assertEqual(xy.writes.count('HARM 2'), 1)
                    # The only return to h1 belongs to minimum-output cleanup.
                    self.assertEqual(xy.writes.count('HARM 1'), 1)
                    for p in result['points']:
                        for s in p['samples']:
                            self.assertEqual(s['lockin_xx']['reading']['harmonic'], 1)
                            self.assertEqual(s['lockin_xy']['reading']['harmonic'], 2)
                            self.assertEqual(s['selected_roles'], ['xx'] if s['harmonic'] == 1 else ['xy'])
                    self.assertTrue(result['cleanup']['verified'])

    def test_only_selected_instrument_cycles_multiple_harmonics(self):
        code, result, xx, xy, _ = self.execute(config_edit=lambda s: select_roles(s, '1, 3', '2'))
        self.assertEqual(code, 0, result.get('error'))
        self.assertNotIn('HARM 2', xx.writes)
        self.assertIn('HARM 3', xx.writes)
        self.assertEqual(xy.writes.count('HARM 2'), 1)
        self.assertNotIn('HARM 3', xy.writes)
        for p in result['points']:
            for s in p['samples']:
                self.assertEqual(s['lockin_xy']['reading']['harmonic'], 2)
                self.assertEqual(s['settings_before']['roles']['lockin_xy']['sensitivity_full_scale_v'], .002)

    def test_unselected_xx_harmonic_is_not_used_even_for_probes(self):
        def mutate(xx, xy):
            original = xx.write
            def write(command):
                if command in ('HARM 2', 'HARM 3'):
                    raise AssertionError('Unselected XX harmonic was requested')
                return original(command)
            xx.write = write
        code, result, xx, xy, _ = self.execute('sweep-excitation', extra=AUTO,
            config_edit=lambda s: select_roles(s, '', '2'), mutate=mutate)
        self.assertEqual(code, 0, result.get('error'))
        self.assertTrue(all(s['selected_roles'] == ['xy'] for p in result['points'] for s in p['samples']))

    def test_mixed_harmonics_survive_segment_range_changes(self):
        def edit(source):
            return select_roles(source).replace(
                'min = 0.004, max = 0.400,',
                'min = 0.004, max = 0.400, xx_full_scale_v = 0.010,')
        code, result, xx, xy, _ = self.execute('sweep-excitation', extra='', config_edit=edit)
        self.assertEqual(code, 0, result.get('error'))
        self.assertNotIn('HARM 2', xx.writes)
        self.assertEqual(xy.writes.count('HARM 2'), 1)

    def test_high_frequency_parks_only_incompatible_role_before_freq(self):
        def mutate(xx, xy):
            original = xx.write
            def write(command):
                if command.startswith('FREQ '):
                    f = float(command.split()[1])
                    for resource in (xx, xy):
                        self.assertLessEqual(f * int(resource.responses['HARM?']), 102000)
                return original(command)
            xx.write = write
        code, result, xx, xy, _ = self.execute(config_edit=select_roles,
            points_hz='17.777,50000,60000', mutate=mutate)
        self.assertEqual(code, 0, result.get('error'))
        self.assertNotIn('HARM 2', xx.writes)
        self.assertEqual([s['harmonic'] for s in result['points'][-1]['samples']], [1])
        self.assertTrue(result['points'][-1]['skipped_harmonics'])

    def test_overload_at_actual_harmonic_remains_visible_and_rejects(self):
        for role, raw, label in (('xx', 1, 'INPUT/RESERVE OVERLOAD'),
                                 ('xy', 1, 'INPUT/RESERVE OVERLOAD'),
                                 ('xy', 2, 'FILTER OVERLOAD')):
            def mutate(xx, xy):
                resource = xx if role == 'xx' else xy
                original = resource.query
                def query(command):
                    if command == 'LIAS?' and float(xx.responses['SLVL?']) > .004:
                        return str(raw)
                    return original(command)
                resource.query = query
            with self.subTest(role=role, raw=raw):
                code, result, xx, xy, _ = self.execute('sweep-excitation', extra='',
                    config_edit=select_roles, mutate=mutate)
                self.assertNotEqual(code, 0)
                self.assertTrue(result['cleanup']['verified'])
                sample = result['points'][-1]['samples'][-1]
                self.assertEqual(sample['lockin_' + role]['lia_status']['raw'], raw)
                view = LockinProgressView()
                view.update({'event': 'lockin_formal_sample', 'sample': sample})
                self.assertIn(label, view.format())
                self.assertIn('h1' if role == 'xx' else 'h2', view.format())
                self.assertNotIn('HARM 2', xx.writes)

    def test_h1_output_overload_is_reported_without_inventing_xy_h1(self):
        def mutate(xx, xy):
            original = xx.query
            def query(command):
                if command == 'LIAS?' and float(xx.responses['SLVL?']) > .004:
                    return '4'
                return original(command)
            xx.query = query
        code, result, xx, xy, _ = self.execute('sweep-excitation', extra='',
            config_edit=select_roles, mutate=mutate)
        self.assertEqual(code, 0, result.get('error'))  # Existing output-only policy.
        sample = result['points'][-1]['samples'][-1]
        view = LockinProgressView()
        view.update({'event': 'lockin_formal_sample', 'sample': sample})
        self.assertIn('Vxx (diagnostic) h1', view.format())
        self.assertIn('OUTPUT OVERLOAD', view.format())
        self.assertNotIn('Vxy h1', view.format())

    def test_small_h2_with_whole_input_overload_still_rejects_auto(self):
        def mutate(xx, xy):
            original = xy.query
            def query(command):
                if command == 'LIAS?' and float(xx.responses['SLVL?']) > .004:
                    return '1'
                return original(command)
            xy.query = query
        code, result, xx, xy, _ = self.execute('sweep-excitation', extra=AUTO,
            config_edit=select_roles, mutate=mutate)
        self.assertNotEqual(code, 0)
        self.assertIn('input/reserve overload', result['error'])
        self.assertTrue(result['cleanup']['verified'])
        self.assertEqual(xy.writes.count('HARM 2'), 1)
        self.assertNotIn('pre_abort_h1_diagnostics', json.dumps(result['points']))

    def test_fixed_input_trip_records_h1_before_minimum_output_cleanup(self):
        def mutate(xx, xy):
            original = xy.query
            def query(command):
                if command == 'LIAS?' and float(xx.responses['SLVL?']) > .004:
                    return '1'
                return original(command)
            xy.query = query
        code, result, xx, xy, _ = self.execute('sweep-excitation', extra=FIXED,
            config_edit=select_roles, mutate=mutate)
        self.assertNotEqual(code, 0)
        point = result['points'][-1]
        audit = point['pre_abort_h1_diagnostics'][0]
        self.assertEqual(audit['role'], 'lockin_xy')
        self.assertEqual(audit['original_sample']['reading']['harmonic'], 2)
        self.assertEqual(audit['h1_sample']['reading']['harmonic'], 1)
        self.assertEqual(audit['source_readback_v_rms'], .008)
        self.assertEqual(audit['sensitivity_full_scale_v'], .002)
        self.assertEqual(audit['sensitivity_code'], audit['sensitivity_after'])
        self.assertEqual(audit['reserve_code'], audit['reserve_after'])
        self.assertFalse(audit['valid_for_analysis'])
        self.assertTrue(audit['completed'])
        h1 = xx.events.index(('xy', 'write', 'HARM 1'))
        minimum = next(i for i in range(h1 + 1, len(xx.events))
                       if xx.events[i] == ('xx', 'write', 'SLVL 0.004'))
        self.assertLess(h1, minimum)
        self.assertFalse(any(s['harmonic'] == 2 for s in point['samples']))
        self.assertTrue(result['cleanup']['verified'])

    def test_failed_abort_diagnostic_does_not_mask_trip_or_block_cleanup(self):
        def mutate(xx, xy):
            query, write = xy.query, xy.write
            def read(command):
                if command == 'LIAS?' and float(xx.responses['SLVL?']) > .004:
                    return '1'
                return query(command)
            def set_value(command):
                if command == 'HARM 1' and float(xx.responses['SLVL?']) > .004:
                    raise OSError('abort diagnosis write failed')
                return write(command)
            xy.query, xy.write = read, set_value
        code, result, xx, xy, _ = self.execute('sweep-excitation', extra=FIXED,
            config_edit=select_roles, mutate=mutate)
        self.assertNotEqual(code, 0)
        self.assertIn('input/reserve overload', result['error'])
        audit = result['points'][-1]['pre_abort_h1_diagnostics'][0]
        self.assertIn('diagnosis write failed', audit['error'])
        self.assertFalse(audit['completed'])
        self.assertTrue(result['cleanup']['verified'])
        self.assertEqual(float(xx.responses['SLVL?']), .004)

    def test_cleared_input_latch_does_not_trigger_h1_abort_diagnosis(self):
        def mutate(xx, xy):
            original = xy.query
            triggered = False
            def query(command):
                nonlocal triggered
                if command == 'LIAS?' and float(xx.responses['SLVL?']) > .004 and not triggered:
                    triggered = True
                    return '1'
                return original(command)
            xy.query = query
        code, result, xx, xy, _ = self.execute('sweep-excitation', extra='',
            config_edit=select_roles, mutate=mutate)
        self.assertEqual(code, 0, result.get('error'))
        self.assertNotIn('pre_abort_h1_diagnostics', json.dumps(result))
        self.assertEqual(xy.writes.count('HARM 2'), 1)
        self.assertIn('status_recheck', result['points'][-1]['samples'][0])

    def test_transition_input_trip_also_records_abort_diagnosis(self):
        def mutate(xx, xy):
            original = xy.query
            def query(command):
                if command == 'LIAS?' and xy.responses['HARM?'].strip() == '2':
                    return '1'
                return original(command)
            xy.query = query
        code, result, xx, xy, _ = self.execute('sweep-excitation', extra=FIXED,
            config_edit=select_roles, mutate=mutate)
        self.assertNotEqual(code, 0)
        self.assertFalse(result['points'])
        self.assertIn('pre_abort_h1_diagnostics', json.dumps(result['sensitivity_setup']))
        self.assertTrue(result['cleanup']['verified'])

    def test_partial_read_preserves_completed_role(self):
        def mutate(xx, xy):
            original = xy.query
            def query(command):
                if command == 'SNAP? 1,2,3,4,9' and float(xx.responses['SLVL?']) > .004:
                    raise OSError('injected second role read failure')
                return original(command)
            xy.query = query
        code, result, xx, xy, _ = self.execute('sweep-excitation', extra='',
            config_edit=select_roles, mutate=mutate)
        self.assertNotEqual(code, 0)
        partial = result['points'][-1]['partial_sample_reads'][0]
        self.assertEqual(partial['lockin_xx']['reading']['harmonic'], 1)
        self.assertIn('read failure', partial['error'])
        self.assertTrue(result['cleanup']['verified'])

    def test_new_raw_record_loads_only_selected_roles_and_harmonics(self):
        code, result, _, _, path = self.execute('sweep-excitation', config_edit=select_roles)
        self.assertEqual(code, 0, result.get('error'))
        directory = path.parent / f'run_data/lockin_commissioning_{path.stem}'
        source = next(directory.glob('*_excitation_completed.json'))
        rows = load_sweep_samples(source)
        self.assertEqual({(r.role, r.harmonic) for r in rows}, {('xx', 1), ('xy', 2)})


class RoleHarmonicCombinationTests(unittest.TestCase):
    setUp = fixtures.HarmonicCombinationTests.setUp
    write_config = fixtures.HarmonicCombinationTests.write_config
    factory = fixtures.HarmonicCombinationTests.factory
    execute = fixtures.HarmonicCombinationTests.execute
    events = fixtures.HarmonicCombinationTests.events

    def test_mixed_roles_keep_harmonics_across_combination_conditions(self):
        self.write_config(source=select_roles(self.base), extra=AUTO)
        result = self.execute()
        self.assertEqual(result['status'], 'completed', result)
        self.assertNotIn('HARM 2', self.xx.writes)
        self.assertEqual(self.xy.writes.count('HARM 2'), 1)
        formal = [p for kind, p in self.events() if kind == 'lockin_formal_pair']
        self.assertTrue(formal)
        self.assertTrue(all(p['lockin_xy']['reading']['harmonic'] == 2 for p in formal))


if __name__ == '__main__':
    unittest.main()
