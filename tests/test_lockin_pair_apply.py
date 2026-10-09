"""Pair apply uses injected instruments only; no live hardware is permitted."""
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
import io
import json
import unittest
from unittest.mock import patch

from attodry_control.lockin_test import build_parser, run
from attodry_control.sr830 import Sr830Error
from tests import test_mixed_lockin_integration as mixed


class PairApplyTests(unittest.TestCase):
    def fixture(self):
        helper = mixed.MixedLockinIntegrationTests('test_apply_toml_native_xy_is_authorized_preserves_source_and_is_idempotent')
        self.addCleanup(helper.doCleanups)
        fixture = helper.apply_fixture()
        config = helper.standalone_config(fixture)
        config = replace(config,
            lockin_xx=replace(config.lockin_xx, frequency_hz=500., source_voltage_v=.004),
            lockin_xy=replace(config.lockin_xy, frequency_hz=500.))
        fixture.config_mock = self.enterContext(patch('attodry_control.lockin_test.load_config', return_value=config))
        self.enterContext(patch('attodry_control.lockin_test.time.sleep'))
        fixture.frequency['hz'] = 10.
        fixture.xx.responses['SLVL?'] = '.008'
        return helper, fixture, config

    def execute(self, helper, fixture, *, error=None):
        arguments = helper.apply_arguments(fixture)
        arguments[arguments.index('--role') + 1] = 'xx, xy'
        output = io.StringIO()
        with redirect_stdout(output):
            if error:
                with self.assertRaisesRegex(Exception, error):
                    run(arguments, resource_manager_factory=lambda: fixture.manager)
            else:
                self.assertEqual(run(arguments, resource_manager_factory=lambda: fixture.manager), 0)
        return json.loads(output.getvalue())

    def test_role_aliases_and_rejections(self):
        parser = build_parser()
        for text, expected in [('xx', 'lockin_xx'), ('xy', 'lockin_xy'),
                ('xx,xy', 'lockin_xx,lockin_xy'), ('xx, xy', 'lockin_xx,lockin_xy'),
                ('lockin_xy,lockin_xx', 'lockin_xx,lockin_xy'), ('both', 'lockin_xx,lockin_xy')]:
            self.assertEqual(parser.parse_args(['apply-toml', '--role', text]).role, expected)
        for text in ('', 'xx,', 'xx,xx', 'xx,other'):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser.parse_args(['apply-toml', '--role', text])

    def test_current_10hz_8mv_prepares_500hz_4mv_and_preserves_xy_source(self):
        helper, fixture, config = self.fixture()
        result = self.execute(helper, fixture)
        self.assertTrue(result['completed'])
        self.assertFalse(result['communication_uncertain'])
        self.assertEqual(fixture.xx.writes[0], 'SLVL 0.004')
        self.assertEqual(fixture.frequency['hz'], 500.)
        self.assertEqual(float(fixture.xx.responses['SLVL?']), .004)
        self.assertTrue(result['actions'])
        self.assertEqual(result['before']['lockin_xx']['frequency_hz'], 10.)
        self.assertEqual(result['after']['lockin_xx']['frequency_hz'], 500.)
        helper.assert_receiver_owns_no_source_writes(fixture, allow_sync_filter_write=True)
        self.assertTrue(fixture.manager.closed and fixture.xx.closed and fixture.xy.closed)

    def test_repeated_matching_apply_does_not_write(self):
        helper, fixture, _ = self.fixture()
        self.execute(helper, fixture)
        fixture.xx.writes.clear()
        fixture.xy.writes.clear()
        result = self.execute(helper, fixture)
        self.assertFalse(result['write_performed'])
        self.assertEqual(fixture.xx.writes + fixture.xy.writes, [])

    def test_h2_input_filter_range_changes_are_prepared_at_minimum(self):
        helper, fixture, config = self.fixture()
        fixture.xy.responses.update({'HARM?': '2', 'ISRC?': '0', 'OFLT?': '12', 'OFSL?': '2'})
        fixture.xx.responses.update({'ISRC?': '0', 'OFLT?': '10', 'OFSL?': '2'})
        fixture.config_mock.return_value = replace(config, lockin_xx=replace(config.lockin_xx, source_voltage_v=.008))
        result = self.execute(helper, fixture)
        self.assertTrue(result['completed'])
        self.assertEqual(float(fixture.xy.responses['HARM?']), 1)
        self.assertEqual(fixture.xx.writes[0], 'SLVL 0.004')
        self.assertEqual(fixture.xx.writes[-1], 'SLVL 0.008')
        self.assertEqual(result['prepared']['lockin_xx']['sine_output_v'], .004)
        self.assertEqual(result['after']['lockin_xx']['sine_output_v'], .008)
        helper.assert_receiver_owns_no_source_writes(fixture, allow_sync_filter_write=True)

    def test_widen_before_low_noise_and_narrow_after_reserve(self):
        from attodry_control.sr830_settings import ReserveMode
        for old_code, target_v, reserve in [(20, 1., ReserveMode.LOW_NOISE), (26, .01, ReserveMode.NORMAL)]:
            with self.subTest(target_v=target_v):
                helper, fixture, config = self.fixture()
                fixture.xx.responses.update({'SENS?': str(old_code), 'RMOD?': '0'})
                fixture.config_mock.return_value = replace(config, lockin_xx=replace(
                    config.lockin_xx, sensitivity_full_scale_v=target_v, reserve_mode=reserve))
                self.execute(helper, fixture)
                sens = next(i for i, cmd in enumerate(fixture.xx.writes) if cmd.startswith('SENS '))
                rmod = next(i for i, cmd in enumerate(fixture.xx.writes) if cmd.startswith('RMOD '))
                self.assertEqual(sens < rmod, target_v == 1.)

    def test_invalid_authorization_and_source_limits_block_before_factory(self):
        helper, fixture, config = self.fixture()
        args = helper.apply_arguments(fixture)
        args[args.index('--role') + 1] = 'both'
        for flag in ('--authorize-writes', '--authorize-status-latch-consumption', '--confirm-xy-sine-disconnected'):
            with self.subTest(flag=flag), self.assertRaises(Exception):
                run([x for x in args if x != flag], resource_manager_factory=lambda: self.fail('opened'))
        fixture.config_mock.return_value = replace(config, lockin_xx=replace(config.lockin_xx, source_voltage_v=6.))
        with self.assertRaisesRegex(ValueError, 'safety range'):
            run(args, resource_manager_factory=lambda: self.fail('opened'))
        self.assertEqual(fixture.manager.opened, [])

    def test_preflight_faults_do_not_write(self):
        for query, value in [('CUROVLDSTAT?', '4'), ('LIAS?', '1'), ('ERRS?', '1')]:
            with self.subTest(query=query):
                helper, fixture, _ = self.fixture()
                fixture.xy.responses[query] = value
                result = self.execute(helper, fixture, error='unknown|overload|error')
                self.assertFalse(result['completed'])
                self.assertTrue(result['manual_verification_required'])
                self.assertEqual(fixture.xx.writes + fixture.xy.writes, [])

    def test_failed_frequency_write_stops_io_without_speculative_cleanup(self):
        helper, fixture, _ = self.fixture()
        fixture.xx.fail_write = 'FREQ 500'
        result = self.execute(helper, fixture, error='VISA write failure')
        self.assertTrue(result['communication_uncertain'])
        self.assertFalse(result['cleanup']['attempted'])
        self.assertEqual(fixture.xx.writes[-1], 'FREQ 500')
        self.assertEqual(result['actions'][-1]['method'], 'set_internal_reference_frequency')
        self.assertTrue(result['manual_verification_required'])
        self.assertFalse(result['completed'])
        self.assertTrue(fixture.manager.closed)

    def test_ignored_frequency_write_retains_actual_and_known_minimum_cleanup(self):
        helper, fixture, _ = self.fixture()
        original = fixture.xx.write
        def write(command):
            if command.startswith('FREQ '):
                fixture.xx.writes.append(command)
            else:
                original(command)
        fixture.xx.write = write
        result = self.execute(helper, fixture, error='requested 500.*actual 10')
        self.assertFalse(result['communication_uncertain'])
        self.assertTrue(result['cleanup']['minimum_output_verified'])
        self.assertTrue(result['manual_verification_required'])
        self.assertFalse(result['completed'])

    def test_real_fault_after_configuration_stays_failed_even_with_minimum_verified(self):
        helper, fixture, _ = self.fixture()
        fixture.xx.responses['ERRS?'] = ['0', '1']
        result = self.execute(helper, fixture, error='instrument error')
        self.assertTrue(result['cleanup']['minimum_output_verified'])
        self.assertFalse(result['completed'])
        self.assertEqual(result['outcome'], 'rejected')
        self.assertTrue(result['manual_verification_required'])

    def test_expected_sr830_change_latches_require_second_clean_window(self):
        for last in ('0', '32'):
            with self.subTest(last=last):
                helper, fixture, _ = self.fixture()
                fixture.xx.responses['OFLT?'] = '10'
                fixture.xx.responses['LIAS?'] = ['0', '48', last, '0']
                result = self.execute(helper, fixture, error=None if last == '0' else 'time-constant')
                self.assertEqual(result['completed'], last == '0')
                self.assertEqual(result['transition']['lockin_xx']['lia_status']['raw'], 48)

    def test_both_sr830_roles_and_reference_repair_keep_xy_source_unchanged(self):
        from attodry_control.sr830_settings import ReserveMode
        from tests.test_sr830 import TrackingVisaResource, responses, FakeResourceManager
        helper, fixture, config = self.fixture()
        xy_config = replace(config.lockin_xy, model='SR830', sr865a=None, reserve_mode=ReserveMode.NORMAL)
        fixture.config_mock.return_value = replace(config, lockin_xy=xy_config)
        fixture.xy = TrackingVisaResource(responses(0), shared_frequency=fixture.frequency, name='xy')
        fixture.manager = FakeResourceManager({'FAKE::XX': fixture.xx, 'FAKE::XY': fixture.xy})
        fixture.xx.responses['FMOD?'] = '0'
        fixture.xy.responses['FMOD?'] = '1'
        fixture.xy.responses['RSLP?'] = '0'
        fixture.xy.responses['SLVL?'] = '.008'
        for resource in (fixture.xx, fixture.xy):
            original = resource.write
            def write(command, resource=resource, original=original):
                original(command)
                if command.startswith(('FMOD ', 'RSLP ')):
                    name, value = command.split()
                    resource.responses[name + '?'] = value
            resource.write = write
        result = self.execute(helper, fixture)
        self.assertTrue(result['completed'])
        self.assertIn('FMOD 1', fixture.xx.writes)
        self.assertIn('FMOD 0', fixture.xy.writes)
        self.assertIn('RSLP 1', fixture.xy.writes)
        self.assertFalse(any(cmd.startswith(('SLVL ', 'FREQ ', 'PHAS ')) for cmd in fixture.xy.writes))
        self.assertEqual(float(fixture.xy.responses['SLVL?']), .008)

    def test_reference_setters_reject_role_inversion_before_io(self):
        from attodry_control.models import LockinRole
        from attodry_control.sr830 import Sr830
        from tests.test_sr830 import FakeVisaResource
        for role, field, code in [(LockinRole.XX, 'reference_mode', 0),
                (LockinRole.XY, 'reference_mode', 1), (LockinRole.XX, 'reference_slope', 1),
                (LockinRole.XY, 'reference_slope', 0)]:
            resource = FakeVisaResource({})
            with self.assertRaises(ValueError):
                Sr830(resource, role).set_fixed_setting(field, code)
            self.assertEqual(resource.queries + resource.writes, [])
