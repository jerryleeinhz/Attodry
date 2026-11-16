"""Analog output scaling is decision-only; other native guards/quality remain."""
from copy import deepcopy
from dataclasses import asdict, replace
import unittest

from attodry_control.electrical_lockin_backend import electrical_status
from attodry_control.lockin_overload import (
    OUTPUT_OVERLOAD_POLICY, blocking_output_overload, nonblocking_native_analog_output,
)
from attodry_control.models import LockinRole
from attodry_control.sr830 import decode_lia_status, Sr830Error
from attodry_control.sr865a import Sr865a, Sr865aError
from tests.test_sr865a import FakeResource
from tests import test_external_lockin_points as external_fixture


def native_status(*, current=0, latch=2, errors=0, standard=0):
    resource = FakeResource()
    resource.responses.update({'CUROVLDSTAT?': str(current), 'LIAS?': str(latch),
                               'ERRS?': str(errors), '*ESR?': str(standard)})
    native = Sr865a(resource, LockinRole.XY).read_status(
        consume_status_latches=True, current_status_supported=True)
    return electrical_status(native)


class AnalogOutputPolicyTests(unittest.TestCase):
    def test_all_current_latched_analog_combinations_are_record_only_with_raw_quality(self):
        for current in range(4):
            for latch in range(4):
                if not current | latch:
                    continue
                with self.subTest(current=current, latch=latch):
                    status = native_status(current=current, latch=latch)
                    for view in (status, asdict(status)):
                        self.assertTrue(nonblocking_native_analog_output(view))
                        self.assertFalse(blocking_output_overload(view))
                    self.assertTrue(status.output_overload)
                    self.assertTrue(status.any_overload)
                    self.assertFalse(status.native_status.valid)
                    self.assertEqual(status.output_overload_policy, OUTPUT_OVERLOAD_POLICY)
                    self.assertEqual(status.native_status.current_status_raw, current)
                    self.assertEqual(status.native_status.lia_status_raw, latch)
                    self.assertEqual(status.native_status.raw[0].response, str(current))
                    self.assertEqual(status.native_status.raw[1].response, str(latch))

    def test_other_channels_current_faults_and_nonoverload_faults_never_exempt(self):
        cases = [dict(current=256), dict(current=258), dict(latch=256),
                 dict(latch=1537), dict(latch=258), dict(latch=18), dict(latch=10),
                 dict(latch=34), dict(latch=6), dict(current=8), dict(current=16),
                 dict(current=4), dict(errors=1), dict(standard=64), dict(standard=128)]
        for case in cases:
            with self.subTest(case=case):
                status = native_status(**case)
                self.assertFalse(nonblocking_native_analog_output(status))
                self.assertTrue(blocking_output_overload(status))

    def test_partial_forged_or_unknown_proof_does_not_gain_exception(self):
        original = asdict(native_status())
        mutations = (
            lambda s: s.pop('output_overload_policy'),
            lambda s: s.update(output_overload_policy='unknown'),
            lambda s: s.update(status_known=1),
            lambda s: s.update(raw=True),
            lambda s: s.update(unknown_status_bits=False),
            lambda s: s.update(output_overload=False),
            lambda s: s['native_status'].update(current_status_raw=False),
            lambda s: s['native_status'].update(output_scale_overload=0),
            lambda s: s['native_status'].update(locked=None),
            lambda s: s['native_status']['raw'].pop(),
            lambda s: s['native_status']['raw'][0].update(response='2'),
            lambda s: s['native_status']['raw'][1].update(response='3'),
            lambda s: s['native_status']['raw'][2].update(error='failed'),
            lambda s: s['native_status']['raw'][2].pop('error'),
            lambda s: s['native_status']['raw'][3].update(command='LIAS?'),
        )
        for mutate in mutations:
            candidate = deepcopy(original)
            # dataclasses retains tuple; archive JSON uses a list.
            candidate['native_status']['raw'] = list(candidate['native_status']['raw'])
            mutate(candidate)
            with self.subTest(mutate=mutate):
                self.assertFalse(nonblocking_native_analog_output(candidate))
                self.assertTrue(blocking_output_overload(candidate))

    def test_sr830_and_unflagged_native_status_do_not_change(self):
        self.assertFalse(nonblocking_native_analog_output(decode_lia_status(4)))
        self.assertTrue(blocking_output_overload(decode_lia_status(4)))
        self.assertFalse(blocking_output_overload(decode_lia_status(0)))
        clean = native_status(latch=0)
        self.assertFalse(nonblocking_native_analog_output(clean))
        self.assertFalse(blocking_output_overload(clean))

    def test_native_frequency_counter_gate_retains_original_wait_and_invalid_scaling(self):
        traces = []
        for current, latch in ((0, 0), (0, 1), (0, 2), (0, 3), (1, 0), (2, 0), (3, 3)):
            resource, waits = FakeResource(), []
            resource.responses.update({'HARM?': '2', 'FREQEXT?': '25',
                'FREQDET?': ['50.1', '50'], 'LIAS?': str(latch), 'CUROVLDSTAT?': str(current)})
            driver = Sr865a(resource, LockinRole.XY, sleep=waits.append)
            sample = driver.read_sample(consume_status_latches=True, current_status_supported=True)
            traces.append((resource.events, resource.writes, waits))
            if current | latch:
                self.assertFalse(sample.frequency_checks[0].status.valid)
                self.assertFalse(sample.status.valid)
                self.assertEqual(sample.frequency_checks[0].status.lia_status_raw, latch)
        for trace in traces[1:]:
            self.assertEqual(traces[0], trace)
        self.assertEqual(traces[1][2], [1.0])
        self.assertEqual(traces[1][1], [])

    def test_current_data_channel_blocks_native_counter_recheck_before_wait(self):
        resource, waits = FakeResource(), []
        resource.responses.update({'HARM?': '2', 'FREQEXT?': '25',
            'FREQDET?': ['50.1', '50'], 'LIAS?': '2', 'CUROVLDSTAT?': '256'})
        driver = Sr865a(resource, LockinRole.XY, sleep=waits.append)
        with self.assertRaisesRegex(Sr865aError, 'unsafe/unknown status'):
            driver.read_sample(consume_status_latches=True, current_status_supported=True)
        self.assertEqual(waits, [])
        self.assertNotIn(('query', 'SNAP? X,Y'), resource.events)


class AnalogOutputLiveTimingTests(unittest.TestCase):
    fixture = external_fixture.ExternalLockinTests.fixture
    start = external_fixture.ExternalLockinTests.start

    def test_repeated_analog_scaling_never_adds_wait_query_or_requalification(self):
        traces = []
        cases = [('abort', 0, 0)] + [(policy, current, latch)
            for policy in ('abort', 'record_continue', 'continue_unselected')
            for current in range(4) for latch in range(4) if current | latch]
        for policy, current, latch in cases:
            f, config, session, clock, events, _ = self.fixture(harmonics=(1,))
            config.lockin_acquisition = replace(config.lockin_acquisition, overload_policy=policy)
            self.start(f, session)
            f.xy.responses.update({'LIAS?': str(latch), 'CUROVLDSTAT?': str(current)})
            before = (len(f.xx.queries), len(f.xy.queries), len(f.xx.writes), len(f.xy.writes))
            session.qualify()
            reading = session.sample_point()
            self.assertTrue(reading['clean'])
            self.assertEqual(reading['status']['samples'][0]['valid_for_analysis_by_role']['lockin_xy'], not bool(current | latch))
            self.assertTrue(reading['status']['samples'][0]['valid_for_analysis_by_role']['lockin_xx'])
            self.assertFalse(any(k == 'external_output_latch_requalification' for k, _ in events))
            traces.append((f.xx.queries[before[0]:], f.xy.queries[before[1]:],
                           f.xx.writes[before[2]:], f.xy.writes[before[3]:], list(clock.waits)))
            # Pending-counter proof checks consume only this already captured status.
            evidence = session._status()
            session._verify_frequency_pending_status(evidence)
            session.source_stopped({'verified': True, 'output_off_confirmed': True})
            self.assertTrue(session.cleanup()['verified'])
        for trace in traces[1:]:
            self.assertEqual(trace, traces[0])
        self.assertEqual(traces[0][-1], [4.5])

    def test_current_data_channel_still_stops_qualification_and_formal_sampling(self):
        for stage in ('qualification', 'formal'):
            f, _, session, _, events, _ = self.fixture(harmonics=(1,))
            self.start(f, session)
            if stage == 'formal':
                session.qualify()
            f.xy.responses.update({'LIAS?': '2', 'CUROVLDSTAT?': '256'})
            with self.subTest(stage=stage), self.assertRaises(Sr830Error):
                (session.qualify if stage == 'qualification' else session.sample_point)()

    def test_current_analog_scaling_does_not_stop_initial_baseline(self):
        for current, latch in ((1, 0), (2, 0), (3, 3)):
            with self.subTest(current=current, latch=latch):
                f, _, session, _, _, _ = self.fixture(harmonics=(1,))
                f.xy.responses.update({'LIAS?': str(latch), 'CUROVLDSTAT?': str(current)})
                self.start(f, session)
                session.qualify()
                reading = session.sample_point()
                self.assertTrue(reading['clean'])
                self.assertFalse(reading['valid_for_analysis'])


if __name__ == '__main__':
    unittest.main()
