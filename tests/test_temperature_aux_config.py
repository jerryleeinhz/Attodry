"""Shared temperature/optical/AUX profiles parse without instrument I/O."""
from copy import deepcopy
import tomllib
import unittest
from unittest.mock import patch

from attodry_control.combination_hardware import load_hardware_combination
from attodry_control.config import ConfigError, load_temperature_operation_config
from tests import test_cld_shared_cleanup as cld_helpers
from tests.test_three_lockin import AUX, POLICY


class TemperatureAuxConfigurationTests(unittest.TestCase):
    write_config = cld_helpers.CldSharedCleanupTests.write_config
    write_document = cld_helpers.CldSharedCleanupTests.write_document

    def setUp(self):
        cld_helpers.CldSharedCleanupTests.setUp(self)
        self.document['combination_scan'].update(
            order=['temperature', 'optical', 'lockin'], samples_per_condition=3)
        self.document['temperature_scan']['temperature_ranges'] = [
            dict(min=1.7, max=251.7, scale='linear', step=2.5)]
        self.document['cld1010']['current_points_a'] = [0.02835]
        feedback = self.document['power_feedback']
        for key in ('target_powers_w', 'target_power_ranges'):
            feedback.pop(key, None)
        feedback['target_power_w'] = 30e-6
        self.document['lockin_aux'] = tomllib.loads(AUX)['lockin_aux']
        self.safety = self.directory / 'lockin_safety.toml'
        with self.safety.open('a', encoding='utf-8') as handle:
            handle.write(POLICY)
        self.write_document()

    def load(self):
        # Parsing may prepare an offline grid but cannot enter any hardware stage.
        with patch('attodry_control.attodry.AttoDryDriver.connect',
                   side_effect=AssertionError('No cryostat connection')), \
             patch('attodry_control.internal_lockin_group.InternalLockinGroupSession.open',
                   side_effect=AssertionError('No AUX lockin connection')), \
             patch('attodry_control.lockin_points.LockinPointSession.open',
                   side_effect=AssertionError('No lockin connection')), \
             patch('attodry_control.optical_points.OpticalPointSession.open',
                   side_effect=AssertionError('No optical connection')):
            result = load_hardware_combination(self.path)
        self.assertEqual(self.manager.opened, [])
        return result

    def test_temperature_subloader_accepts_aux_but_only_parses_temperature(self):
        config = load_temperature_operation_config(self.path)
        self.assertEqual(len(config.temperature_scan.points_k), 101)
        self.assertFalse(hasattr(config, 'lockin_aux'))

    def test_complete_temperature_optical_three_lockin_grid(self):
        config = self.load()
        self.assertEqual([axis.module for axis in config.plan.axes],
                         ['temperature', 'optical', 'lockin'])
        points = config.plan.conditions()
        self.assertEqual(len(points), 101)
        self.assertEqual(points[0]['requested']['temperature_k'], 1.7)
        self.assertEqual(points[-1]['requested']['temperature_k'], 251.7)
        for before, after in zip(points, points[1:]):
            self.assertAlmostEqual(after['requested']['temperature_k'] -
                                   before['requested']['temperature_k'], 2.5)
        self.assertEqual({point['requested']['optical_target_power_w'] for point in points}, {30e-6})
        self.assertEqual({point['requested']['optical_current_a'] for point in points}, {0.02835})
        self.assertEqual(config.plan.samples_per_condition, 3)
        self.assertEqual(config.snapshot['hardware']['lockin']['active_roles'], ['xx', 'xy', 'aux'])
        self.assertEqual(config.lockin.lockin_aux.channel_label, 'Third sample voltage')
        self.assertTrue(config.cryostat.strict_resultant)
        self.assertEqual(config.cryostat.magnet.limits.hardware_z_max_t, 3.0)
        self.assertEqual(config.cryostat.magnet.limits.experiment_vector_max_t, 3.0)
        self.assertIsNone(config.rotator)
        self.assertIsNone(config.smu)
        self.assertIsNone(config.magnetic)

    def test_missing_or_disabled_aux_keeps_dual_temperature_path(self):
        self.safety.write_text(self.safety.read_text(encoding='utf-8').split('[lockin_aux]')[0], encoding='utf-8')
        for auxiliary in (None, {'enabled': False}):
            with self.subTest(auxiliary=auxiliary):
                self.document.pop('lockin_aux', None)
                if auxiliary is not None:
                    self.document['lockin_aux'] = auxiliary
                self.write_document()
                config = self.load()
                self.assertIsNone(config.lockin.lockin_aux)
                self.assertEqual(len(config.plan.conditions()), 101)

    def test_misspelled_top_level_table_stays_rejected(self):
        self.document['lockin_auz'] = self.document.pop('lockin_aux')
        self.write_document()
        with self.assertRaisesRegex(ConfigError, 'unknown field.*lockin_auz'):
            load_temperature_operation_config(self.path)

    def test_temperature_fields_still_validate_with_aux_present(self):
        for key, value, error in (
            ('stable_range_k', -0.05, 'stable_range'),
            ('stable_rnage_k', 0.05, 'unknown field.*stable_rnage'),
        ):
            with self.subTest(key=key):
                self.document['temperature_stability'][key] = value
                self.write_document()
                with self.assertRaisesRegex(ConfigError, error):
                    self.load()
                self.document['temperature_stability'].pop(key)
                self.document['temperature_stability']['stable_range_k'] = 0.05

    def test_active_aux_still_validates_its_own_fields(self):
        original = deepcopy(self.document['lockin_aux'])
        for key, value, error in (
            ('model', 'SR830', 'requires SR865A'),
            ('sine_output_connected', True, 'SINE OUT physical connection'),
            ('channel_lable', 'typo', 'unsupported fields.*channel_lable'),
        ):
            with self.subTest(key=key):
                self.document['lockin_aux'] = {**deepcopy(original), key: value}
                self.write_document()
                with self.assertRaisesRegex(ValueError, error):
                    self.load()

    def test_active_aux_still_requires_explicit_safety_policy(self):
        self.safety.write_text(self.safety.read_text(encoding='utf-8').split('[lockin_aux]')[0], encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'explicit.*safety'):
            self.load()

    def test_optical_only_profile_retains_no_temperature_session(self):
        self.document['combination_scan']['order'] = ['optical', 'lockin']
        self.write_document()
        config = self.load()
        self.assertIsNone(config.temperature)
        self.assertIsNone(config.cryostat)
        self.assertEqual(len(config.plan.conditions()), 1)
        self.assertIsNotNone(config.lockin.lockin_aux)
