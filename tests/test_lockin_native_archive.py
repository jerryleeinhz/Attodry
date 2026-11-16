"""New mixed captures bind derived settings to complete native raw evidence."""
from contextlib import closing
from copy import deepcopy
from datetime import datetime
import json
import unittest

from attodry_control.combination_store import _validate_lockin_record, open_readonly
from tests.test_lockin_single_role_pair import mixed_fixture


class NativeArchiveProofTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = mixed_fixture()
        cls.addClassCleanup(fixture.doCleanups)
        cls.config = fixture.config()
        result = fixture.execute(cls.config)
        if result['status'] != 'completed':
            raise AssertionError(result)
        with closing(open_readonly(fixture.database)) as db:
            cls.payload = json.loads(db.execute(
                'SELECT payload_json FROM combination_samples ORDER BY condition_id, sample_index LIMIT 1').fetchone()[0])
        cls.reading = next(r for r in cls.payload['reads'] if r['module'] == 'lockin')

    def validate(self, reading):
        return _validate_lockin_record(reading, self.config.snapshot,
            self.config.plan.conditions()[0], datetime.fromisoformat(self.payload['started_at_utc']),
            datetime.fromisoformat(self.payload['finished_at_utc']))

    @staticmethod
    def native(pair, when='after'):
        return pair['settings_' + when]['roles']['lockin_xy']['receiver_invariants']

    @staticmethod
    def reply(native, command, response):
        next(r for r in native['raw'] if r['command'] == command)['response'] = response

    def test_raw_codes_physical_fields_and_compatibility_must_agree(self):
        def raw_scale(pair):
            self.reply(self.native(pair), 'SCAL?', '0')
        mutations = {
            'raw code contradiction': raw_scale,
            'physical contradiction': lambda p: self.native(p).update(sensitivity_full_scale_v=1.0),
            'compatibility after code': lambda p: p['settings_after']['roles']['lockin_xy'].update(time_constant_code=0),
            'boolean compatibility code': lambda p: p['settings_after']['roles']['lockin_xy'].update(sensitivity_code=True),
            'false successful proof': lambda p: p['settings_after']['roles']['lockin_xy'].update(receiver_invariants_error='partial failure'),
            'missing proof': lambda p: p['settings_after']['roles']['lockin_xy'].pop('receiver_invariants'),
        }
        for label, mutate in mutations.items():
            reading = deepcopy(self.reading)
            mutate(reading['status']['samples'][0])
            with self.subTest(label=label), self.assertRaises(ValueError):
                self.validate(reading)

    def test_failed_missing_extra_or_reordered_raw_reply_is_rejected(self):
        mutations = {
            'error': lambda raw: raw[0].update(error='transport failed'),
            'missing': lambda raw: raw.pop(),
            'extra': lambda raw: raw.append(deepcopy(raw[-1])),
            'wrong command': lambda raw: raw[0].update(command='FREQ?'),
            'nontext response': lambda raw: raw[0].update(response=None),
        }
        for label, mutate in mutations.items():
            reading = deepcopy(self.reading)
            mutate(self.native(reading['status']['samples'][0])['raw'])
            with self.subTest(label=label), self.assertRaises(ValueError):
                self.validate(reading)

    def test_coherent_raw_changes_cannot_bypass_archived_receiver_targets(self):
        mutations = (
            ('input_range_v_peak', 0.3, 'IRNG?', '1'),
            ('advanced_filter', True, 'ADVFILT?', '1'),
            ('reference_source', 'internal', 'RSRC?', '0'),
        )
        for field, value, command, response in mutations:
            reading = deepcopy(self.reading)
            pair = reading['status']['samples'][0]
            for when in ('before', 'after'):
                native = self.native(pair, when)
                native[field] = value
                self.reply(native, command, response)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.validate(reading)

    def test_disconnected_source_baseline_and_identity_cannot_drift_between_brackets(self):
        for identity in (False, True):
            reading = deepcopy(self.reading)
            native = self.native(reading['status']['samples'][0])
            if identity:
                value = 'Stanford_Research_Systems,SR865A,other-serial,V2.11'
                native['identity'] = value
                self.reply(native, '*IDN?', value)
            else:
                native['source_offset_v'] = 0.2
                native['source_output_baseline']['source_offset_v'] = 0.2
                self.reply(native, 'SOFF?', '0.2')
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                self.validate(reading)

    def test_preserved_source_baseline_is_observed_not_invented_from_configuration(self):
        reading = deepcopy(self.reading)
        pair = reading['status']['samples'][0]
        for when in ('before', 'after'):
            native = self.native(pair, when)
            native['source_offset_v'] = 0.2
            native['source_output_baseline']['source_offset_v'] = 0.2
            self.reply(native, 'SOFF?', '0.2')
        self.validate(reading)
        self.native(pair)['source_output_baseline']['source_offset_v'] = 0.0
        with self.assertRaises(ValueError):
            self.validate(reading)


if __name__ == '__main__':
    unittest.main()
