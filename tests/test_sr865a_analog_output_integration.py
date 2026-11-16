"""Real fake scans retain analog-output quality without stopping later groups."""
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import datetime
import json
import unittest
from unittest.mock import patch

from attodry_control import lockin_test as daily
from attodry_control.combination_resume import build_recovery
from attodry_control.combination_store import (
    CombinationStore, _validate_lockin_record, open_readonly, validate_accepted_records,
)
from attodry_control.commissioning_6221_analysis import load_6221_sweep
from tests import test_combination_6221 as fixtures


POLICY = 'sr865a-analog-output-record-only-v1'


def three_group_fixture():
    fixture = fixtures.Combination6221Tests()
    fixture.setUp()
    fixture.text = (fixture.text.replace('xx_harmonics = [1, 2]', 'xx_harmonics = [1]')
        .replace('xy_harmonics = [1, 2]', 'xy_harmonics = [2]')
        .replace('frequency_points_hz = [17.777, 200.0]', 'frequency_points_hz = [25.0]')
        .replace('samples_per_condition = 1', 'samples_per_condition = 3'))
    fixture.path.write_text(fixture.text, encoding='utf-8')
    return fixture


@contextmanager
def formal_status(fixture, *, lia=2, current=0, errors=0, standard=0):
    """Change real fake replies only during each condition's second raw pair."""
    original = daily._read_sweep_pair
    count = [0]
    def capture(*args, **kwargs):
        count[0] += 1
        if count[0] % 3 != 2:
            return original(*args, **kwargs)
        with patch.dict(fixture.xy.responses, {'LIAS?': str(lia),
                'CUROVLDSTAT?': str(current), 'ERRS?': str(errors), '*ESR?': str(standard)}):
            return original(*args, **kwargs)
    with patch.object(daily, '_read_sweep_pair', capture):
        yield count


def source_and_wait_trace(fixture):
    return (list(fixture.source.queries), list(fixture.source.commands),
            list(fixture.xx.writes), list(fixture.xy.writes), list(fixture.clock.sleeps))


def sample_payloads(fixture):
    with closing(open_readonly(fixture.database)) as db:
        return [json.loads(row[0]) for row in db.execute(
            'SELECT payload_json FROM combination_samples ORDER BY condition_id, attempt_index, sample_index')]


class AnalogOutputArchiveIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.normal = three_group_fixture()
        cls.addClassCleanup(cls.normal.doCleanups)
        cls.normal_result = cls.normal.execute()
        if cls.normal_result['status'] != 'completed':
            raise AssertionError(cls.normal_result)
        cls.fixture = three_group_fixture()
        cls.addClassCleanup(cls.fixture.doCleanups)
        cls.config = cls.fixture.config()
        with formal_status(cls.fixture) as count:
            cls.result = cls.fixture.execute(cls.config)
        cls.pair_count = count[0]
        if cls.result['status'] != 'completed':
            raise AssertionError(cls.result)
        cls.payloads = sample_payloads(cls.fixture)

    def validate(self, payload, snapshot=None):
        reading = next(r for r in payload['reads'] if r['module'] == 'lockin')
        condition = self.config.plan.conditions()[0]
        _validate_lockin_record(reading, snapshot or self.config.snapshot, condition,
            datetime.fromisoformat(payload['started_at_utc']),
            datetime.fromisoformat(payload['finished_at_utc']))

    def test_second_group_latch_keeps_all_groups_raw_and_independent_quality(self):
        self.assertEqual(self.pair_count, 6)
        self.assertEqual(len(self.payloads), 6)
        self.assertTrue(self.result['cleanup']['clean'])
        self.assertEqual(self.fixture.source.state['OUTP:STAT'], 'OFF')
        self.assertEqual(self.config.snapshot['hardware']['lockin']['output_overload_policy'], POLICY)
        for index, payload in enumerate(self.payloads):
            pair = next(r for r in payload['reads'] if r['module'] == 'lockin')['status']['samples'][0]
            flags = pair['lockin_xy']['lia_status']
            latch = index % 3 == 1
            self.assertEqual(flags['raw'], 2 if latch else 0)
            self.assertEqual(flags['output_overload_policy'], POLICY)
            self.assertEqual(flags['native_status']['current_status_raw'], 0)
            self.assertEqual(flags['native_status']['lia_status_raw'], flags['raw'])
            self.assertEqual(flags['output_overload'], latch)
            self.assertEqual(pair['valid_for_analysis_by_role']['lockin_xy'], not latch)
            self.assertTrue(pair['valid_for_analysis_by_role']['lockin_xx'])
            self.assertEqual(pair['blocking_problems'], [])
            self.assertTrue(pair['settings_verified'])
            replies = {r['command']: r['response'] for r in flags['native_status']['raw']}
            self.assertEqual(replies, {'CUROVLDSTAT?': '0', 'LIAS?': str(flags['raw']),
                                       'ERRS?': '0', '*ESR?': '0'})
        with closing(open_readonly(self.fixture.database)) as db:
            self.assertEqual(validate_accepted_records(db, 'current-run')['accepted_conditions'], 2)

    def test_analysis_excludes_only_latched_xy_rows_and_retains_audit(self):
        snapshot = load_6221_sweep(self.fixture.database, 'current-run')
        rows = [row for group in snapshot.groups.values() for row in group]
        self.assertEqual(len(rows), 10)
        self.assertEqual(sum(row.role == 'xx' for row in rows), 6)
        self.assertEqual(sum(row.role == 'xy' for row in rows), 4)
        self.assertEqual(len(snapshot.metadata['quality_exclusions']), 2)
        self.assertEqual({r['role'] for r in snapshot.metadata['quality_exclusions']}, {'xy'})
        audit = load_6221_sweep(self.fixture.database, 'current-run', include_problem=True)
        audit_rows = [row for group in audit.groups.values() for row in group]
        self.assertEqual(len(audit_rows), 12)
        self.assertEqual(sum('problem' in row.statuses for row in audit_rows), 2)

    def test_record_only_latch_adds_no_source_commands_detector_writes_or_wait(self):
        self.assertEqual(source_and_wait_trace(self.fixture), source_and_wait_trace(self.normal))
        self.assertEqual(self.fixture.xx.queries, self.normal.xx.queries)
        self.assertEqual(self.fixture.xy.queries, self.normal.xy.queries)
        self.assertEqual(self.fixture.source.commands.count('SOUR:WAVE:INIT'), 2)

    def test_latched_archive_requires_matching_plan_and_status_policy(self):
        payload = self.payloads[1]
        self.validate(payload)
        for marker in (None, 'unsupported-policy'):
            with self.subTest(plan_policy=marker):
                snapshot = deepcopy(self.config.snapshot)
                if marker is None:
                    snapshot['hardware']['lockin'].pop('output_overload_policy')
                else:
                    snapshot['hardware']['lockin']['output_overload_policy'] = marker
                with self.assertRaises(ValueError):
                    self.validate(payload, snapshot)
            with self.subTest(status_policy=marker):
                changed = deepcopy(payload)
                flags = next(r for r in changed['reads'] if r['module'] == 'lockin')['status']['samples'][0]['lockin_xy']['lia_status']
                if marker is None:
                    flags.pop('output_overload_policy')
                else:
                    flags['output_overload_policy'] = marker
                with self.assertRaises(ValueError):
                    self.validate(changed)

    def test_legacy_clean_archive_is_readable_but_cannot_relabel_latched_data(self):
        snapshot = deepcopy(self.config.snapshot)
        snapshot['hardware']['lockin'].pop('output_overload_policy')
        for index, succeeds in ((0, True), (1, False)):
            payload = deepcopy(self.payloads[index])
            pair = next(r for r in payload['reads'] if r['module'] == 'lockin')['status']['samples'][0]
            pair['lockin_xy']['lia_status'].pop('output_overload_policy')
            if succeeds:
                self.validate(payload, snapshot)
            else:
                with self.assertRaises(ValueError):
                    self.validate(payload, snapshot)

    def test_current_and_latched_analog_channels_continue_without_new_io_or_wait(self):
        for lia, current in ((0, 1), (0, 2), (0, 3), (1, 0), (3, 0), (1, 2), (3, 3)):
            with self.subTest(latched=lia, current=current):
                fixture = three_group_fixture()
                self.addCleanup(fixture.doCleanups)
                with formal_status(fixture, lia=lia, current=current) as count:
                    result = fixture.execute()
                self.assertEqual(result['status'], 'completed', result)
                self.assertEqual(count[0], 6)
                self.assertTrue(result['cleanup']['clean'])
                for index, payload in enumerate(sample_payloads(fixture)):
                    pair = next(r for r in payload['reads'] if r['module'] == 'lockin')['status']['samples'][0]
                    flags = pair['lockin_xy']['lia_status']
                    injected = index % 3 == 1
                    self.assertEqual(flags['raw'], lia if injected else 0)
                    self.assertEqual(flags['native_status']['current_status_raw'], current if injected else 0)
                    self.assertEqual(flags['native_status']['output_scale_overload'], bool(current) and injected)
                    self.assertEqual(flags['native_status']['output_scale_overload_latched'], bool(lia) and injected)
                    replies = {r['command']: r['response'] for r in flags['native_status']['raw']}
                    self.assertEqual(replies, {'CUROVLDSTAT?': str(current if injected else 0),
                        'LIAS?': str(lia if injected else 0), 'ERRS?': '0', '*ESR?': '0'})
                    self.assertEqual(flags['output_overload'], injected)
                    self.assertEqual(pair['valid_for_analysis_by_role']['lockin_xy'], not injected)
                    self.assertTrue(pair['valid_for_analysis_by_role']['lockin_xx'])
                    self.assertEqual(pair['blocking_problems'], [])
                with closing(open_readonly(fixture.database)) as db:
                    self.assertEqual(validate_accepted_records(db, 'current-run')['accepted_conditions'], 2)
                snapshot = load_6221_sweep(fixture.database, 'current-run')
                self.assertEqual(snapshot.metadata['selected_channel_rows'], 10)
                self.assertEqual({row['role'] for row in snapshot.metadata['quality_exclusions']}, {'xy'})
                self.assertEqual(source_and_wait_trace(fixture), source_and_wait_trace(self.normal))
                self.assertEqual(fixture.xx.queries, self.normal.xx.queries)
                self.assertEqual(fixture.xy.queries, self.normal.xy.queries)

    def test_persistent_analog_only_flags_do_not_block_setup_groups_or_cleanup(self):
        fixture = three_group_fixture()
        self.addCleanup(fixture.doCleanups)
        fixture.xy.responses.update({'LIAS?': '3', 'CUROVLDSTAT?': '3'})
        result = fixture.execute()
        self.assertEqual(result['status'], 'completed', result)
        self.assertTrue(result['cleanup']['clean'])
        self.assertEqual(len(sample_payloads(fixture)), 6)
        with closing(open_readonly(fixture.database)) as db:
            self.assertEqual(validate_accepted_records(db, 'current-run')['accepted_conditions'], 2)
        snapshot = load_6221_sweep(fixture.database, 'current-run')
        self.assertEqual(snapshot.metadata['selected_channel_rows'], 6)
        self.assertEqual({row.role for rows in snapshot.groups.values() for row in rows}, {'xx'})
        self.assertEqual(load_6221_sweep(fixture.database, 'current-run', include_problem=True)
                         .metadata['selected_channel_rows'], 12)
        self.assertEqual(source_and_wait_trace(fixture), source_and_wait_trace(self.normal))
        self.assertEqual(fixture.xx.queries, self.normal.xx.queries)
        self.assertEqual(fixture.xy.queries, self.normal.xy.queries)

    def test_current_analog_archive_rejects_stripped_policy_and_raw_proof_tampering(self):
        fixture = three_group_fixture()
        self.addCleanup(fixture.doCleanups)
        with formal_status(fixture, lia=0, current=2):
            result = fixture.execute()
        self.assertEqual(result['status'], 'completed', result)
        payload = sample_payloads(fixture)[1]
        self.validate(payload)
        mutations = {
            'clear aggregate': lambda flags: flags.update(output_overload=False),
            'current digital output': lambda flags: flags['native_status'].update(current_status_raw=256),
            'bool current word': lambda flags: flags['native_status'].update(current_status_raw=True),
            'missing native reply': lambda flags: flags['native_status']['raw'].pop(),
            'wrong current raw reply': lambda flags: flags['native_status']['raw'][0].update(response='0'),
            'failed current query': lambda flags: flags['native_status']['raw'][0].update(error='transport failure'),
            'missing policy': lambda flags: flags.pop('output_overload_policy'),
        }
        for label, mutation in mutations.items():
            with self.subTest(tamper=label):
                changed = deepcopy(payload)
                flags = next(r for r in changed['reads'] if r['module'] == 'lockin')['status']['samples'][0]['lockin_xy']['lia_status']
                mutation(flags)
                with self.assertRaises(ValueError):
                    self.validate(changed)
        changed = deepcopy(payload)
        flags = next(r for r in changed['reads'] if r['module'] == 'lockin')['status']['samples'][0]['lockin_xy']['lia_status']
        flags.pop('output_overload_policy')
        snapshot = deepcopy(self.config.snapshot)
        snapshot['hardware']['lockin'].pop('output_overload_policy')
        with self.assertRaises(ValueError):
            self.validate(changed, snapshot)


class AnalogOutputFailureAndRecoveryTests(unittest.TestCase):
    def test_recovery_keeps_accepted_latched_prefix_and_resumes_whole_condition(self):
        fixture = three_group_fixture()
        self.addCleanup(fixture.doCleanups)
        config = fixture.config()
        original = CombinationStore.event
        def event(store, run_id, kind, payload):
            original(store, run_id, kind, payload)
            if kind == 'condition_started' and payload['condition_id'] == 'condition-000001':
                raise RuntimeError('injected failure after accepted CH2-latched condition')
        with formal_status(fixture), patch.object(CombinationStore, 'event', event):
            first = fixture.execute(config)
        self.assertEqual(first['status'], 'failed')
        prefix = sample_payloads(fixture)
        self.assertEqual(len(prefix), 3)
        fixture.devices()
        review = build_recovery(config, fixture.database, 'current-run',
                                acknowledge_failure=True, manual_verification=True)
        self.assertEqual(review['next_condition']['condition_id'], 'condition-000001')
        with formal_status(fixture) as count:
            result = fixture.execute(config, recovery=review)
        self.assertEqual(result['status'], 'completed', result)
        self.assertEqual(count[0], 3)
        self.assertEqual(sample_payloads(fixture)[:3], prefix)
        with closing(open_readonly(fixture.database)) as db:
            self.assertEqual(validate_accepted_records(db, 'current-run')['accepted_conditions'], 2)
        self.assertEqual(load_6221_sweep(fixture.database, 'current-run').metadata['selected_channel_rows'], 10)

    def test_digital_output_and_nonoverload_faults_keep_blocking(self):
        cases = (
            ('data-channel latch', {'lia': 256}),
            ('data-channel current', {'lia': 2, 'current': 256}),
            ('analog plus digital output', {'lia': 259, 'current': 3}),
            ('CH2 plus input', {'lia': 18}),
            ('analog plus current input', {'lia': 3, 'current': 17}),
            ('CH2 plus filter', {'lia': 34}),
            ('reference unlock', {'lia': 2, 'current': 8}),
            ('unknown latch', {'lia': 6}),
            ('instrument error', {'lia': 2, 'errors': 1}),
        )
        for name, words in cases:
            with self.subTest(fault=name):
                fixture = three_group_fixture()
                self.addCleanup(fixture.doCleanups)
                with formal_status(fixture, **words) as count:
                    result = fixture.execute()
                self.assertEqual(result['status'], 'failed', result)
                self.assertEqual(count[0], 2)
                self.assertEqual(fixture.source.commands.count('SOUR:WAVE:INIT'), 1)
                self.assertEqual(fixture.source.state['OUTP:STAT'], 'OFF')
                self.assertTrue(fixture.source.closed and fixture.xx.closed and fixture.xy.closed)
                formal = [p for kind, p in fixture.events() if kind == 'lockin_formal_pair']
                self.assertEqual(len(formal), 2)
                self.assertEqual(formal[-1]['lockin_xy']['lia_status']['raw'], words.get('lia', 2))
                self.assertTrue(formal[-1]['blocking_problems'])
