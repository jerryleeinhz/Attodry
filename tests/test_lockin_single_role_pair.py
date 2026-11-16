"""One physical mixed-harmonic pair, with unchanged raw/analysis protection."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime
import json
import unittest
from unittest.mock import patch

from attodry_control import lockin_test as daily
from attodry_control.combination_store import (
    CombinationStore, _validate_lockin_record, open_readonly, validate_accepted_records,
)
from attodry_control.combination_resume import build_recovery
from attodry_control.commissioning_6221_analysis import load_6221_sweep
from attodry_control.lockin_capture_contract import SINGLE_ROLE_PAIR_POLICY
from tests import test_combination_6221 as fixtures
from tests import test_combination_hardware as sr830_fixtures
from tests.test_lockin_role_harmonics import select_roles
from tests.test_lockin_harmonics import AUTO
from tests.test_lockin_settling import Clock


def mixed_fixture():
    fixture = fixtures.Combination6221Tests()
    fixture.setUp()
    fixture.text = (fixture.text.replace('xx_harmonics = [1, 2]', 'xx_harmonics = [1]')
        .replace('xy_harmonics = [1, 2]', 'xy_harmonics = [2]')
        .replace('frequency_points_hz = [17.777, 200.0]', 'frequency_points_hz = [25.0]'))
    fixture.path.write_text(fixture.text, encoding='utf-8')
    return fixture


class SingleRolePairArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = mixed_fixture()
        cls.addClassCleanup(cls.fixture.doCleanups)
        cls.config = cls.fixture.config()
        with patch.object(daily, '_read_sweep_pair', wraps=daily._read_sweep_pair) as capture:
            cls.result = cls.fixture.execute(cls.config)
            cls.pair_reads = capture.call_count
        if cls.result['status'] != 'completed':
            raise AssertionError(cls.result)
        cls.formal = [p for name, p in cls.fixture.events() if name == 'lockin_formal_pair']
        with closing(open_readonly(cls.fixture.database)) as db:
            cls.payload = json.loads(db.execute(
                'SELECT payload_json FROM combination_samples ORDER BY condition_id, sample_index LIMIT 1').fetchone()[0])
        cls.reading = next(r for r in cls.payload['reads'] if r['module'] == 'lockin')

    def validate(self, reading=None, snapshot=None):
        return _validate_lockin_record(reading or self.reading, snapshot or self.config.snapshot,
            self.config.plan.conditions()[0], datetime.fromisoformat(self.payload['started_at_utc']),
            datetime.fromisoformat(self.payload['finished_at_utc']))

    def test_one_real_pair_per_condition_both_native_roles(self):
        self.assertEqual(self.pair_reads, 2)
        self.assertEqual(len(self.formal), 2)
        self.assertEqual(self.config.snapshot['hardware']['lockin']['capture_policy'], SINGLE_ROLE_PAIR_POLICY)
        for pair in self.formal:
            self.assertEqual(pair['capture_layout'], SINGLE_ROLE_PAIR_POLICY)
            self.assertIsNone(pair['harmonic'])
            self.assertEqual(pair['selected_roles'], ['xx', 'xy'])
            self.assertEqual(pair['harmonics_by_role'], {'lockin_xx': 1, 'lockin_xy': 2})
            for role, harmonic in (('xx', 1), ('xy', 2)):
                raw = pair['lockin_' + role]
                self.assertEqual(raw['reading']['harmonic'], harmonic)
                self.assertIn('captured_at_utc', raw)
                self.assertEqual(raw['error_status'], 0)
            self.assertTrue(pair['settings_verified'])
        self.assertTrue(self.result['cleanup']['clean'])

    def test_archive_and_notebook_preserve_selected_rows_and_current_basis(self):
        with closing(open_readonly(self.fixture.database)) as db:
            self.assertEqual(validate_accepted_records(db, 'current-run')['accepted_conditions'], 2)
        snapshot = load_6221_sweep(self.fixture.database, 'current-run')
        rows = [r for group in snapshot.groups.values() for r in group]
        self.assertEqual(len(rows), 4)
        self.assertEqual({(r.role, r.harmonic) for r in rows}, {('xx', 1), ('xy', 2)})
        self.assertEqual(snapshot.metadata['current_basis'], '6221_rms_setting_readback')
        self.assertFalse(snapshot.metadata['current_is_measured'])

    def test_corrupt_mixed_pair_contract_never_passes_archive_review(self):
        self.validate()
        mutations = {
            'unknown layout': lambda pair: pair.update(capture_layout='guessed_v2'),
            'fictional common harmonic': lambda pair: pair.update(harmonic=1),
            'missing selected role': lambda pair: pair.update(selected_roles=['xx']),
            'duplicate selected role': lambda pair: pair.update(selected_roles=['xx', 'xx', 'xy']),
            'wrong role map': lambda pair: pair['harmonics_by_role'].update(lockin_xy=1),
            'bool harmonic': lambda pair: pair['harmonics_by_role'].update(lockin_xx=True),
            'wrong raw harmonic': lambda pair: pair['lockin_xy']['reading'].update(harmonic=1),
            'unverified': lambda pair: pair.update(settings_verified=False),
        }
        for label, mutate in mutations.items():
            reading = deepcopy(self.reading)
            mutate(reading['status']['samples'][0])
            with self.subTest(label=label), self.assertRaises((ValueError, KeyError)):
                self.validate(reading)
        reading = deepcopy(self.reading)
        reading['status']['samples'].append(deepcopy(reading['status']['samples'][0]))
        with self.assertRaises(ValueError):
            self.validate(reading)

    def test_plan_binding_and_fixed_singleton_requirement(self):
        self.validate()
        mutations = (
            lambda lockin: lockin.pop('capture_policy'),
            lambda lockin: lockin.update(capture_policy='unknown'),
            lambda lockin: lockin['harmonics_by_role'].update(xx=[1, 2]),
            lambda lockin: lockin['lockin_xy'].update(sensitivity_mode='bounded_auto'),
        )
        for mutate in mutations:
            snapshot = deepcopy(self.config.snapshot)
            mutate(snapshot['hardware']['lockin'])
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                self.validate(snapshot=snapshot)


class SingleRolePairExecutionTests(unittest.TestCase):
    def test_whole_condition_recovery_keeps_mixed_pair_prefix(self):
        fixture = mixed_fixture()
        self.addCleanup(fixture.doCleanups)
        config = fixture.config()
        original = CombinationStore.event
        failed = False
        def event(store, run_id, kind, payload):
            nonlocal failed
            original(store, run_id, kind, payload)
            if kind == 'condition_started' and payload['condition_id'] == 'condition-000001' and not failed:
                failed = True
                raise RuntimeError('injected second condition interruption')
        with patch.object(CombinationStore, 'event', event):
            first = fixture.execute(config)
        self.assertEqual(first['status'], 'failed')
        first_pair = next(p for kind, p in fixture.events() if kind == 'lockin_formal_pair')
        fixture.devices()
        review = build_recovery(config, fixture.database, 'current-run', acknowledge_failure=True,
                                manual_verification=True)
        self.assertEqual(review['next_condition']['condition_id'], 'condition-000001')
        result = fixture.execute(config, recovery=review)
        self.assertEqual(result['status'], 'completed', result)
        pairs = [p for kind, p in fixture.events() if kind == 'lockin_formal_pair']
        self.assertEqual(len(pairs), 2)
        self.assertEqual(pairs[0], first_pair)
        self.assertTrue(all(p['capture_layout'] == SINGLE_ROLE_PAIR_POLICY for p in pairs))
        with closing(open_readonly(fixture.database)) as db:
            self.assertEqual(validate_accepted_records(db, 'current-run')['accepted_conditions'], 2)

    def test_legacy_split_pairs_remain_readable_without_new_plan_policy(self):
        fixture = mixed_fixture()
        self.addCleanup(fixture.doCleanups)
        config = fixture.config()
        compact = fixture.execute(config)
        self.assertEqual(compact['status'], 'completed', compact)
        compact_queries = len(fixture.xx.queries) + len(fixture.xy.queries)
        compact_writes = (list(fixture.xx.writes), list(fixture.xy.writes), list(fixture.source.commands))
        compact_sleeps = list(fixture.clock.sleeps)
        fixture.devices()
        fixture.database = fixture.directory / 'legacy.sqlite'
        config.snapshot['hardware']['lockin'].pop('capture_policy')
        original = daily._capture_sweep_point
        def legacy(*args, **kwargs):
            kwargs['single_role_pair'] = False
            return original(*args, **kwargs)
        with patch.object(daily, '_capture_sweep_point', legacy):
            result = fixture.execute(config)
        self.assertEqual(result['status'], 'completed', result)
        pairs = [p for kind, p in fixture.events() if kind == 'lockin_formal_pair']
        self.assertEqual(len(pairs), 4)
        self.assertTrue(all('capture_layout' not in p for p in pairs))
        self.assertLess(compact_queries, len(fixture.xx.queries) + len(fixture.xy.queries))
        self.assertEqual(compact_writes, (fixture.xx.writes, fixture.xy.writes, fixture.source.commands))
        self.assertEqual(compact_sleeps, fixture.clock.sleeps)
        with closing(open_readonly(fixture.database)) as db:
            self.assertEqual(validate_accepted_records(db, 'current-run')['accepted_conditions'], 2)
        self.assertEqual(load_6221_sweep(fixture.database, 'current-run').metadata['selected_channel_rows'], 4)

    def test_second_detector_failure_retains_completed_xx_and_closes_source(self):
        fixture = mixed_fixture()
        self.addCleanup(fixture.doCleanups)
        original = daily._read_sweep_pair
        def fail_xy(xx, xy, *args, **kwargs):
            with patch.object(xy, 'read_harmonic_sample', side_effect=RuntimeError('injected XY transport failure')):
                return original(xx, xy, *args, **kwargs)
        with patch.object(daily, '_read_sweep_pair', fail_xy):
            result = fixture.execute()
        self.assertEqual(result['status'], 'failed')
        self.assertIn('injected XY transport failure', result['error'])
        records = [p for kind, p in fixture.events() if kind == 'lockin_point_samples']
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['samples'], [])
        partial = records[0]['partial_sample_reads'][0]
        self.assertEqual(partial['lockin_xx']['reading']['harmonic'], 1)
        self.assertNotIn('lockin_xy', partial)
        self.assertEqual(fixture.source.state['OUTP:STAT'], 'OFF')
        self.assertTrue(fixture.xx.closed and fixture.xy.closed and fixture.source.closed)

    def test_internal_sr830_mixed_and_unchanged_fallback_shapes(self):
        for xx_h, xy_h, extra, mixed, count in (
                ('1', '2', '', True, 1),
                ('1', '1', '', False, 1),
                ('1, 2', '2', '', False, 2),
                ('1', '2', AUTO, False, 2)):
            with self.subTest(xx=xx_h, xy=xy_h, auto=bool(extra)):
                fixture = sr830_fixtures.ElectricalCombinationTests()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                source = select_roles(fixture.base, xx_h, xy_h) + extra
                fixture.write_config(('lockin',), source=source)
                config = fixtures.load_hardware_combination(fixture.path)
                clock = Clock()
                with CombinationStore(fixture.database) as store:
                    result = fixtures.run_hardware_combination(config, store, 'mixed-internal',
                        authorize_hardware=True, confirm_xy_sine_disconnected=True,
                        manager_factory=lambda: fixture.manager, monotonic=clock.monotonic, sleep=clock.sleep)
                self.assertEqual(result['status'], 'completed', result)
                pairs = [p for kind, p in fixture.events() if kind == 'lockin_formal_pair']
                self.assertEqual(len(pairs), 2 * count)
                self.assertTrue(all((p.get('capture_layout') == SINGLE_ROLE_PAIR_POLICY) == mixed for p in pairs))
                with closing(open_readonly(fixture.database)) as db:
                    self.assertEqual(validate_accepted_records(db, 'mixed-internal')['accepted_conditions'], 2)
                if mixed:
                    self.assertTrue(all(p['lockin_xx']['reading']['harmonic'] == 1
                        and p['lockin_xy']['reading']['harmonic'] == 2 for p in pairs))
