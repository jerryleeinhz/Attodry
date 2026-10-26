"""Per-role coupling through real orchestration and fake VISA transports only."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import itertools
import unittest

from attodry_control.combination_hardware import load_hardware_combination
from attodry_control.internal_lockin_group import InternalLockinGroupSession
from attodry_control.lockin_backend import LockinBackendError, create_lockin_backend
from attodry_control.optical_background_analysis import subtract_dark
from attodry_control.photonics_lockin_config import load_photonics_lockin_config
from attodry_control.sr830_settings import (
    ExternalReferenceEdge, InputCoupling, InputMode, ReferenceSource,
    ShieldGrounding, map_sr830_settings,
)
from tests import test_combination_hardware as combination_fixtures, test_config as config_fixtures
from tests.optical_helpers import Clock
from tests.photonics_lockin_helpers import (
    FakeManager, FakeResource, fixture_document, safety_document,
)
from tests.test_optical_power_reference import change_setting, make_rows
from tests.test_three_lockin import AUX, POLICY


class CouplingConfigTests(unittest.TestCase):
    def test_sr830_both_couplings_map_to_native_codes(self):
        for coupling, code in (("ac", 0), ("dc", 1)):
            with self.subTest(coupling=coupling):
                result = map_sr830_settings(
                    reference_source=ReferenceSource.EXTERNAL_TTL,
                    external_reference_edge=ExternalReferenceEdge.RISING,
                    input_mode=InputMode.A_MINUS_B,
                    shield_grounding=ShieldGrounding.FLOAT,
                    input_coupling=InputCoupling(coupling), time_constant_s=.3,
                    filter_slope_db_oct=24, sensitivity_full_scale_v=.001)
                self.assertEqual(result.input_coupling, code)

    def test_dual_sr830_config_accepts_each_independent_combination(self):
        fixture = config_fixtures.ConfigurationTests()
        for xx, xy in itertools.product(("ac", "dc"), repeat=2):
            text = fixture.simulation_text()
            before, after = text.split('[lockin_xy]', 1)
            before = before.replace('input_coupling = "ac"', f'input_coupling = "{xx}"')
            after = after.replace('input_coupling = "ac"', f'input_coupling = "{xy}"', 1)
            with self.subTest(xx=xx, xy=xy):
                cfg = fixture.load_text(before + '[lockin_xy]' + after)
                self.assertEqual(cfg.lockin_xx.input_coupling.value, xx)
                self.assertEqual(cfg.lockin_xy.input_coupling.value, xy)

    def test_mixed_model_config_validates_mode_without_resource_creation(self):
        for xx, xy in itertools.product(("ac", "dc"), repeat=2):
            document = fixture_document()
            document['lockin_xx']['input_coupling'] = xx
            document['lockin_xy']['input_coupling'] = xy
            with self.subTest(xx=xx, xy=xy):
                cfg = load_photonics_lockin_config('fake.toml', document=document,
                    safety_document=safety_document())
                self.assertEqual((cfg.lockin_xx.input_coupling, cfg.lockin_xy.input_coupling), (xx, xy))
        for role in ('lockin_xx', 'lockin_xy'):
            for bad in ('', 'DC', 'invalid', 0, 1, True, False, None):
                document = fixture_document()
                document[role]['input_coupling'] = bad
                with self.subTest(role=role, bad=bad), self.assertRaisesRegex(ValueError, 'input_coupling'):
                    load_photonics_lockin_config('fake.toml', document=document,
                        safety_document=safety_document())


class CouplingBackendTests(unittest.TestCase):
    def make(self, model):
        cfg = load_photonics_lockin_config('fake.toml', document=fixture_document(),
            safety_document=safety_document())
        settings = cfg.lockin_xy if model == 'SR830' else cfg.lockin_xx
        resource = FakeResource(model, 'lockin_' + settings.role.value)
        backend = create_lockin_backend(model=model, role=settings.role, resource=resource)
        return backend, resource, settings

    def test_both_modes_read_before_write_verify_and_skip_unchanged(self):
        for model, coupling in itertools.product(('SR830', 'SR865A'), ('ac', 'dc')):
            with self.subTest(model=model, coupling=coupling):
                backend, resource, settings = self.make(model)
                code = 0 if coupling == 'ac' else 1
                resource.responses['ICPL?'] = str(1-code)
                settings = replace(settings, input_coupling=coupling)
                backend.configure_fixed_measurement(settings, authorize_writes=True)
                self.assertEqual([v for v in resource.writes if v.startswith('ICPL ')], [f'ICPL {code}'])
                self.assertEqual(resource.queries.count('ICPL?'), 2)
                backend.configure_fixed_measurement(settings, authorize_writes=True)
                self.assertEqual([v for v in resource.writes if v.startswith('ICPL ')], [f'ICPL {code}'])
                self.assertEqual(resource.queries.count('ICPL?'), 3)
                self.assertFalse(any(v.startswith(('SLVL ', 'SOFF ', 'FREQ ')) for v in resource.writes))

    def test_invalid_mode_and_missing_write_authorization_have_zero_io(self):
        for model in ('SR830', 'SR865A'):
            for coupling in ('invalid', '', 0, True, None):
                backend, resource, settings = self.make(model)
                with self.subTest(model=model, coupling=coupling), self.assertRaises(ValueError):
                    backend.configure_fixed_measurement(replace(settings, input_coupling=coupling), authorize_writes=True)
                self.assertEqual(resource.queries + resource.writes, [])
            backend, resource, settings = self.make(model)
            with self.assertRaises(LockinBackendError):
                backend.configure_fixed_measurement(replace(settings, input_coupling='dc'))
            self.assertEqual(resource.queries + resource.writes, [])

    def test_unknown_initial_mode_is_rejected_before_coupling_write(self):
        for model, reply in itertools.product(('SR830', 'SR865A'), ('2', 'garbled')):
            backend, resource, settings = self.make(model)
            resource.responses['ICPL?'] = reply
            with self.subTest(model=model, reply=reply), self.assertRaises(LockinBackendError):
                backend.configure_fixed_measurement(replace(settings, input_coupling='dc'), authorize_writes=True)
            self.assertFalse(any(v.startswith('ICPL ') for v in resource.writes))
            self.assertEqual(resource.queries.count('ICPL?'), 1)

    def test_mismatch_timeout_and_uncertain_write_are_not_replayed(self):
        for model, fault in itertools.product(('SR830', 'SR865A'), ('mismatch', 'read_timeout', 'write_timeout')):
            backend, resource, settings = self.make(model)
            def on_write(device, command):
                if command == 'ICPL 1' and fault == 'write_timeout':
                    raise TimeoutError('uncertain ICPL write')
            def on_query(device, command):
                if command == 'ICPL?' and 'ICPL 1' in device.writes:
                    device.responses[command] = '0' if fault == 'mismatch' else TimeoutError('ICPL read timeout')
            resource.on_write, resource.on_query = on_write, on_query
            with self.subTest(model=model, fault=fault), self.assertRaises((LockinBackendError, TimeoutError)):
                backend.configure_fixed_measurement(replace(settings, input_coupling='dc'), authorize_writes=True)
            self.assertEqual([v for v in resource.writes if v.startswith('ICPL ')], ['ICPL 1'])
            self.assertTrue(any(v.command == 'ICPL 1' for v in backend.audit))


class CouplingSessionTests(unittest.TestCase):
    setUp = combination_fixtures.ElectricalCombinationTests.setUp
    write_config = combination_fixtures.ElectricalCombinationTests.write_config

    def make(self, couplings=('dc', 'ac', 'dc'), *, overload_policy='abort'):
        xx, xy, aux = couplings
        source = self.base.replace('excitation_xy_harmonics = [1, 2, 3]', 'excitation_xy_harmonics = [1]')
        source = source.replace('# overload_policy = "abort"', f'overload_policy = "{overload_policy}"')
        before, after = source.split('[lockin_xy]', 1)
        before = before.replace('input_coupling = "ac"', f'input_coupling = "{xx}"')
        after = after.replace('input_coupling = "ac"', f'input_coupling = "{xy}"', 1)
        extra = AUX.replace('input_coupling = "ac"', f'input_coupling = "{aux}"')
        self.write_config(order=('lockin',), source=before+'[lockin_xy]'+after, extra=extra)
        safety = self.directory / 'lockin_safety.toml'
        if '[lockin_aux]' not in safety.read_text(encoding='utf-8'):
            with safety.open('a', encoding='utf-8') as handle:
                handle.write(POLICY)
        cfg = load_hardware_combination(self.path)
        manager = FakeManager({role: {'model': 'SR830', 'address': 'FAKE::'+role[-2:].upper()}
            for role in ('lockin_xx', 'lockin_xy')})
        manager.resources['FAKE::AUX'] = FakeResource('SR865A', 'lockin_aux')
        for address, resource in manager.resources.items():
            for key in ('FREQ?', 'FREQEXT?', 'FREQDET?', 'FREQINT?'):
                resource.responses[key] = str(cfg.lockin.lockin_xx.frequency_hz)
            if address == 'FAKE::XX':
                resource.responses['FMOD?'] = '1'
        clock = Clock()
        events = []
        session = InternalLockinGroupSession(self.path, cfg.lockin,
            lambda kind, payload: events.append((kind, deepcopy(payload))),
            manager_factory=lambda:manager, monotonic=clock, sleep=clock.sleep)
        self.addCleanup(session.close)
        return session, manager, clock, events

    def test_all_ac_dc_and_mixed_three_role_sessions_archive_and_cleanup(self):
        for modes in itertools.product(('ac', 'dc'), repeat=3):
            with self.subTest(modes=modes):
                session, manager, clock, events = self.make(modes)
                self.assertEqual(manager.opened, [])
                session.open(); session.configure()
                self.assertEqual(clock(), 0)
                session.set_point(.004); session.qualify()
                self.assertAlmostEqual(clock(), session.config.settle_s)
                before = {key:list(value.writes) for key, value in manager.resources.items()}
                reading = session.sample_point()
                self.assertTrue(reading['valid_for_analysis'])
                self.assertAlmostEqual(reading['measurements']['lockin_aux_h1_x_v'], .0001)
                settings = reading['status']['samples'][0]['settings_before']
                for role, coupling in zip(('xx', 'xy', 'aux'), modes):
                    value = settings[role]['native_settings']['input_coupling']
                    self.assertEqual(value, coupling if role == 'aux' else int(coupling == 'dc'))
                self.assertEqual({key:value.writes for key,value in manager.resources.items()}, before)
                self.assertTrue(session.cleanup()['verified'])
                for role, coupling in zip(('XX', 'XY', 'AUX'), modes):
                    writes = [v for v in manager.resources['FAKE::'+role].writes if v.startswith('ICPL ')]
                    self.assertEqual(writes, ['ICPL 1'] if coupling == 'dc' else [])
                session.close()

    def test_mode_drift_at_preflight_or_formal_window_fails_without_repair(self):
        for role, during_sample in itertools.product(('XX','XY','AUX'), (False, True)):
            with self.subTest(role=role, during_sample=during_sample):
                session, manager, _, _ = self.make(('dc','dc','dc'))
                session.open(); session.configure(); session.set_point(.004); session.qualify()
                resource = manager.resources['FAKE::'+role]
                if during_sample:
                    def on_query(device, command):
                        if command.startswith('SNAP?'):
                            device.responses['ICPL?'] = '0'
                    resource.on_query = on_query
                else:
                    resource.responses['ICPL?'] = '0'
                with self.assertRaises((ValueError, RuntimeError)):
                    session.sample_point()
                self.assertTrue(session.failed)
                self.assertEqual([v for v in resource.writes if v.startswith('ICPL ')], ['ICPL 1'])
                session.close()

    def test_record_continue_cannot_accept_or_repair_coupling_drift(self):
        session, manager, _, events = self.make(('dc','dc','dc'), overload_policy='record_continue')
        self.assertEqual(session.config.overload_policy, 'record_continue')
        session.open(); session.configure(); session.set_point(.004); session.qualify()
        session.sample_point()
        accepted = sum(kind == 'lockin_formal_pair' for kind, _ in events)
        writes = {name: list(resource.writes) for name, resource in manager.resources.items()}
        manager.resources['FAKE::XY'].responses['ICPL?'] = '0'
        with self.assertRaisesRegex(ValueError, 'model-specific settings'):
            session.sample_point()
        self.assertTrue(session.failed)
        self.assertEqual(sum(kind == 'lockin_formal_pair' for kind, _ in events), accepted)
        self.assertEqual({name: resource.writes for name, resource in manager.resources.items()}, writes)


class CouplingBackgroundTests(unittest.TestCase):
    def test_independent_dark_rejects_other_coupling_per_role_and_preserves_raw(self):
        for schema, role in itertools.product(('legacy','photonics'), ('xx','xy','aux')):
            with self.subTest(schema=schema, role=role):
                dark = make_rows(powers=(1e-6,), repeats=1, schema=schema)[0]
                dark['run_id'] = 'dark'
                dark['status.optical']['readback']['nkt']['source']['getLdOutputState'] = False
                light = deepcopy(dark)
                light['run_id'] = 'light'
                light['finished_at_utc'] = (datetime.fromisoformat(dark['finished_at_utc'])+timedelta(seconds=1)).isoformat()
                light['status.optical']['readback']['nkt']['source']['getLdOutputState'] = True
                matched, = subtract_dark([dark, light], mode='separate', dark_run_id='dark', phase_confirmed=True)
                self.assertTrue(matched['status.lockin']['background']['valid_for_analysis'])
                change_setting(light, 'input_coupling', 'dc' if role == 'aux' else 0, role)
                original = deepcopy([dark,light])
                mismatch, = subtract_dark([dark, light], mode='separate', dark_run_id='dark', phase_confirmed=True)
                assessment = mismatch['status.lockin']['background']['channels']
                self.assertFalse(assessment[role+':h1']['difference_emitted'])
                self.assertEqual(assessment[role+':h1']['dark_other_context_count'], 1)
                self.assertNotIn('measured.lockin_'+role+'_h1_delta_x_v', mismatch)
                for other in {'xx','xy','aux'}-{role}:
                    self.assertTrue(assessment[other+':h1']['difference_emitted'])
                self.assertEqual([dark,light], original)


if __name__ == '__main__':
    unittest.main()
