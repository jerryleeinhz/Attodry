"""Magnetic/CLD/AUX grids validate offline without loosening hardware guards."""
import unittest
import tomllib
from unittest.mock import patch
from attodry_control.combination_hardware import load_hardware_combination
from tests import test_cld_shared_cleanup as cld_helpers
from tests.test_three_lockin import AUX, POLICY
from attodry_control.config import ConfigError, load_magnetic_field_operation_config


class MagneticAuxConfigurationTests(unittest.TestCase):
    write_config = cld_helpers.CldSharedCleanupTests.write_config
    write_document = cld_helpers.CldSharedCleanupTests.write_document

    def load(self):
        with patch("attodry_control.attodry.AttoDryDriver.connect", side_effect=AssertionError("No cryostat connection")), patch("attodry_control.internal_lockin_group.InternalLockinGroupSession.open", side_effect=AssertionError("No lockin connection")), patch("attodry_control.optical_points.OpticalPointSession.open", side_effect=AssertionError("No optical connection")):
            result = load_hardware_combination(self.path)
        self.assertEqual(self.manager.opened, [])
        return result

    def setUp(self):
        cld_helpers.CldSharedCleanupTests.setUp(self)
        self.document["combination_scan"]["samples_per_condition"] = 3
        self.document["lockin_aux"] = tomllib.loads(AUX)["lockin_aux"]
        self.safety = self.directory / "lockin_safety.toml"
        with self.safety.open("a", encoding="utf-8") as handle:
            handle.write(POLICY)
        self.document['combination_scan']['order'] = ['magnetic', 'optical', 'lockin']
        field = self.document['magnetic_field_run']
        field.pop('points', None)
        field.update(axis='z', segments=[dict(min=-3, max=3, points=21),
            dict(min=-3, max=3, points=21, direction='descending')], transition_policy='direct')
        self.document['cld1010']['current_points_a'] = [.02272, .02579, .02640, .02698, .02784]
        self.document['power_feedback'].pop('target_power_w', None)
        self.document['power_feedback'].pop('target_power_ranges', None)
        self.document['power_feedback'].update(target_mapping='paired',
            target_powers_w=[1e-6, 6e-6, 15e-6, 25e-6, 40e-6])
        self.write_document()

    def test_three_lockin_field_round_trip_and_paired_powers(self):
        standalone = load_magnetic_field_operation_config(self.path)
        self.assertEqual(len(standalone.run.points), 42)
        self.assertFalse(hasattr(standalone, 'lockin_aux'))
        config = self.load()
        self.assertEqual(config.snapshot['hardware']['lockin']['active_roles'], ['xx', 'xy', 'aux'])
        self.assertEqual([len(axis.points) for axis in config.plan.axes], [42, 5, 1])
        conditions = list(config.plan.conditions())
        self.assertEqual(len(conditions), 210)
        self.assertEqual([conditions[i]['requested']['field_z_t'] for i in (0, 100, 105, 205)], [-3, 3, 3, -3])
        for i, condition in enumerate(conditions):
            request = condition['requested']
            self.assertEqual(request['field_x_t'], 0)
            self.assertEqual(request['optical_current_a'], self.document['cld1010']['current_points_a'][i % 5])
            self.assertEqual(request['optical_target_power_w'], self.document['power_feedback']['target_powers_w'][i % 5])
        self.assertFalse(config.cryostat.strict_resultant)
        self.assertTrue(config.cryostat.command_scoped_field)
        self.assertEqual(config.snapshot['field_readback_policy']['version'], 'command-scoped-readback-v1')
        self.assertIsNone(config.temperature)

    def test_aux_typos_and_unsafe_output_still_rejected(self):
        self.document['lockin_aux']['sine_output_connected'] = True
        self.write_document()
        with self.assertRaisesRegex(ValueError, 'SINE OUT physical connection'):
            self.load()
        self.document['lockin_auz'] = self.document.pop('lockin_aux')
        self.write_document()
        with self.assertRaisesRegex(ValueError, 'Unknown top-level'):
            self.load()

    def test_configured_axis_boundary_not_relaxed(self):
        self.document['magnet']['hardware_z_max_t'] = 3.0
        self.document['magnetic_field_run']['segments'][0]['min'] = -3.1
        self.write_document()
        with self.assertRaises(ValueError):
            self.load()

    def test_missing_aux_safety_policy_still_rejected(self):
        self.safety.write_text(self.safety.read_text().split('[lockin_aux]')[0])
        with self.assertRaisesRegex(ValueError, 'explicit.*safety'):
            self.load()
