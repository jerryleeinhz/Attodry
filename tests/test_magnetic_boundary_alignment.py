"""Offline regression for verified command-scoped magnetic boundaries."""
from contextlib import closing
from dataclasses import replace
import json
import math
from pathlib import Path
import re
import sqlite3
from types import SimpleNamespace
import unittest

from attodry_control.attodry import AttoDryDriver
from attodry_control.config import (ConfigError, load_config, load_magnetic_field_operation_config,
                                    load_temperature_operation_config)
from attodry_control.combination_hardware import HardwareCombinationStation
from attodry_control.combination_scan import AxisPoint, ScanAxis, CombinationPlan
from attodry_control.cryostat_points import CryostatPointSession
from attodry_control.models import VectorField
from attodry_control.safety import FieldReadbackPolicy, MagnetLimits, SafetyViolation, float32_value
from attodry_control.safety import FieldTransitionPolicy
from tests.test_attodry import FakeAttoDryDll
from tests.test_magnetic_field import _MagneticConfigFixture, _state


class CommandScopedBoundaryTests(unittest.TestCase):
    def policy(self, targets=(VectorField(0, 3.6),), **kwargs):
        return FieldReadbackPolicy.from_targets(targets, readback_tolerance_t=.0015,
                                                command_scoped=True, **kwargs)

    def test_single_z_plan_uses_configured_axis_cap_not_universal_three_tesla(self):
        policy = self.policy((VectorField(0, -3.6), VectorField(0, 3.6)))
        for z in (-9., -3.6, 3.6, 9.):
            with self.subTest(z=z):
                actual = VectorField(.001, z)
                self.assertIs(policy.validate_readback(actual, command=VectorField(0, z)), actual)
        with self.assertRaises(SafetyViolation):
            policy.validate_target(VectorField(0, math.nextafter(9., math.inf)))

    def test_nominal_readback_boundary_has_no_half_millitesla_margin(self):
        policy = self.policy((VectorField(3., 0),))
        policy.validate_readback(VectorField(3., 0), command=VectorField(3., 0))
        with self.assertRaises(SafetyViolation):
            policy.validate_readback(VectorField(3.000001, 0), command=VectorField(3., 0))
        self.assertEqual(policy.snapshot()['readback_margin_t'], 0)

    def test_float32_command_above_reduced_cap_is_rejected(self):
        nominal = 2.9
        self.assertGreater(float32_value(nominal), nominal)
        policy = self.policy((VectorField(0, 2.),), limits=MagnetLimits(3., nominal, 3.))
        policy.validate_target(VectorField(0, nominal))
        with self.assertRaises(SafetyViolation):
            policy.validate_target(VectorField(0, float32_value(nominal)))

    def test_reduced_configured_axis_cap_is_never_expanded(self):
        policy = self.policy((VectorField(0, 2.),), limits=MagnetLimits(3., 2.5, 3.))
        for value in (2.500001, 3.6):
            with self.subTest(value=value), self.assertRaises(SafetyViolation):
                policy.validate_readback(VectorField(0, value), command=VectorField(0, 2.))

    def test_dual_command_resultant_uses_exact_three_tesla_limit(self):
        policy = self.policy((VectorField(1., 2.),))
        policy.validate_target(VectorField(1.8, 2.4))
        with self.assertRaises(SafetyViolation):
            policy.validate_target(VectorField(1.8, 2.400001))
        with self.assertRaises(SafetyViolation):
            policy.validate_readback(VectorField(1., 2.829), command=VectorField(1., 2.))

    def test_tiny_commanded_inactive_component_is_not_rounded_to_zero(self):
        with self.assertRaises(SafetyViolation):
            self.policy((VectorField(1e-20, 3.6),))

    def test_inactive_readback_has_separate_hard_residual_guard(self):
        policy = self.policy()
        limit = max(.0015, float32_value(.0015))
        policy.validate_readback(VectorField(limit, 3.6), command=VectorField(0, 3.6))
        with self.assertRaises(SafetyViolation):
            policy.validate_readback(VectorField(math.nextafter(limit, math.inf), 3.6),
                                     command=VectorField(0, 3.6))

    def test_inactive_guard_is_clamped_to_configured_axis_cap(self):
        policy = self.policy(limits=MagnetLimits(.0001, 9., 3.))
        policy.validate_readback(VectorField(.0001, 3.6), command=VectorField(0, 3.6))
        with self.assertRaises(SafetyViolation):
            policy.validate_readback(VectorField(.0001000001, 3.6), command=VectorField(0, 3.6))

    def test_command_context_is_mandatory_not_inferred_from_noisy_actual(self):
        policy = self.policy()
        with self.assertRaisesRegex(SafetyViolation, 'command|context'):
            policy.validate_readback(VectorField(.0001, 3.6))

    def test_mixed_plan_classifies_readback_by_confirmed_command(self):
        policy = self.policy((VectorField(3., 0), VectorField(0, 9.), VectorField(1., 2.)))
        policy.validate_readback(VectorField(.001, 3.6), command=VectorField(0, 3.6))
        with self.assertRaises(SafetyViolation):
            policy.validate_readback(VectorField(.001, 3.6), command=VectorField(1., 2.))
        # Initial zero in a mixed plan does not invent a previous single-Z command.
        with self.assertRaises(SafetyViolation):
            policy.validate_readback(VectorField(.001, 3.6), command=VectorField(0, 0))

    def test_command_snapshot_roundtrip_preserves_exact_policy(self):
        snapshot = self.policy().snapshot()
        restored = FieldReadbackPolicy.from_snapshot(snapshot)
        self.assertEqual(restored.snapshot(), snapshot)
        self.assertTrue(snapshot['command_scoped'])
        self.assertEqual(snapshot['readback_margin_t'], 0)

    def test_archived_legacy_policy_keeps_its_margin_and_snapshot(self):
        legacy = FieldReadbackPolicy.from_targets((VectorField(3., 0),), readback_tolerance_t=.0015)
        snapshot = legacy.snapshot()
        self.assertNotIn('command_scoped', snapshot)
        self.assertEqual(snapshot['readback_margin_t'], .0005)
        restored = FieldReadbackPolicy.from_snapshot(json.loads(json.dumps(snapshot)))
        restored.validate_readback(VectorField(3.0004, 0))
        self.assertEqual(restored.snapshot(), snapshot)
        with self.assertRaises(SafetyViolation):
            restored.validate_readback(VectorField(3.0006, 0))

    def test_previous_nonzero_context_survives_zero_setpoint_without_extra_io(self):
        driver = AttoDryDriver.from_config(load_config('config/hardware.example.toml'),
            dll=FakeAttoDryDll(), connection_authorized=True, writes_authorized=True)
        driver.field_readback_policy = self.policy((VectorField(3., 0), VectorField(0, 9.)))
        command = VectorField(0, 3.6)
        self.assertEqual(driver.field_readback_context(_state(command)), command)
        zeroing = replace(_state(VectorField(.001, 3.5)), field_setpoint=VectorField(0, 0))
        self.assertEqual(driver.field_readback_context(zeroing), command)
        driver.field_readback_policy.validate_readback(zeroing.field,
            command=driver.field_readback_context(zeroing))
        self.assertEqual(driver.dll.events, [])


class MagneticAcknowledgementConfigTests(_MagneticConfigFixture, unittest.TestCase):
    def write_unified(self, tolerance=.0015, ack=.0002):
        path = self.write_config(((0., 3.6),), transition_policy='direct')
        text = path.read_text(encoding='utf-8').replace('field_tolerance_t = 0.001',
            f'readback_tolerance_t = {tolerance!r}\nsetpoint_ack_tolerance_t = {ack!r}')
        path.write_text(text, encoding='utf-8')
        return path

    def test_reference_donor_ack_point_two_millitesla_is_accepted(self):
        config = load_magnetic_field_operation_config(self.write_unified(), command_scoped=True)
        self.assertEqual(config.magnet.setpoint_ack_tolerance_t, .0002)
        self.assertEqual(config.magnet.readback_tolerance_t, .0015)

    def test_ack_cannot_exceed_configured_actual_tolerance(self):
        with self.assertRaises(ConfigError):
            load_magnetic_field_operation_config(self.write_unified(ack=.0015001), command_scoped=True)

    def test_default_standalone_parser_retains_original_ack_ceiling(self):
        with self.assertRaises(ConfigError):
            load_magnetic_field_operation_config(self.write_unified())

    def test_configurable_magnetic_tolerance_does_not_change_ambient_parser(self):
        path = self.write_unified(tolerance=.002, ack=.0002)
        config = load_magnetic_field_operation_config(path, command_scoped=True)
        self.assertEqual(config.magnet.readback_tolerance_t, .002)
        with self.assertRaises(ConfigError):
            load_magnetic_field_operation_config(path)

    def test_legacy_tolerance_stays_separate_and_bounded(self):
        path = self.write_config(((0., 2.),), field_tolerance_t=.0011)
        with self.assertRaises(ConfigError):
            load_magnetic_field_operation_config(path)

    def test_invalid_explicit_tolerances_fail_before_connecting(self):
        for tolerance in (0., -1., math.nan, math.inf):
            with self.subTest(tolerance=tolerance), self.assertRaises(ConfigError):
                load_magnetic_field_operation_config(self.write_unified(tolerance=tolerance), command_scoped=True)


class MagneticRecoveryBoundaryTests(unittest.TestCase):
    def station(self, *, actual=VectorField(0, 0), setpoint=None, scoped=True,
                targets=(VectorField(0, -3.6), VectorField(0, 3.6)), limits=MagnetLimits()):
        axes = (ScanAxis('magnetic', tuple(AxisPoint({'field_x_t': p.bx_t, 'field_z_t': p.bz_t})
                                          for p in targets)),)
        plan = CombinationPlan(axes, magnet_limits=limits, readback_tolerance_t=.0015,
                               command_scoped_field=scoped)
        config = SimpleNamespace(plan=plan, temperature=None,
            magnetic=SimpleNamespace(run=SimpleNamespace(max_step_t=2., transition_policy=FieldTransitionPolicy.DIRECT)),
            cryostat=SimpleNamespace(magnet=SimpleNamespace(limits=limits)))
        driver = AttoDryDriver.from_config(load_config('config/hardware.example.toml'),
            dll=FakeAttoDryDll(), connection_authorized=True, writes_authorized=True)
        driver.field_readback_policy = plan.magnetic_readback_policy()
        driver.last_confirmed_state = replace(_state(actual), field_setpoint=setpoint or actual)
        station = HardwareCombinationStation(config, recovery={'next_condition': plan.conditions()[0]})
        station.cryo = SimpleNamespace(driver=driver)
        station.set_event_sink(lambda *args: None)
        return station

    def test_new_pure_z_recovery_can_retain_three_point_six_tesla_path(self):
        station = self.station(actual=VectorField(0, 3.6))
        station._validate_recovery_environment()
        self.assertEqual(station.cryo.driver.dll.events, [])

    def test_new_pure_z_recovery_accepts_bounded_inactive_readback(self):
        station = self.station(actual=VectorField(.001, 3.6), setpoint=VectorField(0, 3.6))
        station._validate_recovery_environment()
        self.assertEqual(station.cryo.driver.dll.events, [])

    def test_new_recovery_rejects_inactive_axis_fault_before_any_io(self):
        station = self.station(actual=VectorField(.002, 3.6), setpoint=VectorField(0, 3.6))
        with self.assertRaises(SafetyViolation):
            station._validate_recovery_environment()
        self.assertEqual(station.cryo.driver.dll.events, [])

    def test_new_recovery_rejects_dual_readback_above_three_tesla(self):
        station = self.station(actual=VectorField(1., 2.829), setpoint=VectorField(1., 2.),
                               targets=(VectorField(1., 2.),))
        with self.assertRaises(SafetyViolation):
            station._validate_recovery_environment()

    def test_new_recovery_does_not_expand_a_lower_configured_z_ceiling(self):
        station = self.station(actual=VectorField(0, 2.6), setpoint=VectorField(0, 2.),
                               targets=(VectorField(0, 2.),), limits=MagnetLimits(3., 2.5, 3.))
        with self.assertRaises(SafetyViolation):
            station._validate_recovery_environment()

    def test_unknown_initial_state_cannot_establish_safe_recovery(self):
        station = self.station()
        station.cryo.driver.last_confirmed_state = None
        with self.assertRaisesRegex(ValueError, 'complete|state'):
            station._validate_recovery_environment()

    def test_legacy_recovery_still_rejects_path_above_three_tesla(self):
        station = self.station(scoped=False)
        self.assertNotIn('command_scoped_field', station.config.plan.snapshot())
        with self.assertRaises(SafetyViolation):
            station._validate_recovery_environment()

    def test_legacy_recovery_still_rejects_present_field_above_three_tesla(self):
        station = self.station(actual=VectorField(0, 3.6), scoped=False)
        with self.assertRaisesRegex(ValueError, '3 T'):
            station._validate_recovery_environment()

    def test_new_cleanup_does_not_expand_configured_scan_envelope(self):
        limits = MagnetLimits(3., 2.5, 3.)
        config = SimpleNamespace(magnet=SimpleNamespace(limits=limits, readback_tolerance_t=.0015),
                                 command_scoped_field=True)
        magnetic = SimpleNamespace(run=SimpleNamespace(points=(VectorField(0, 2.),)))
        session = CryostatPointSession(config, None, magnetic, lambda *args: None)
        session.cleaning = True
        original = session.field_policy.snapshot()
        with session._recovery('zero'):
            self.assertEqual(session.field_readback_limits, limits)
            self.assertEqual(session.active_field_policy.snapshot(), original)
            with self.assertRaises(SafetyViolation):
                session.active_field_policy.validate_readback(VectorField(0, 2.500001),
                                                               command=VectorField(0, 2.))

    def test_historical_cleanup_keeps_original_factory_recovery_envelope(self):
        limits = MagnetLimits(3., 2.5, 3.)
        config = SimpleNamespace(magnet=SimpleNamespace(limits=limits, readback_tolerance_t=.0015))
        magnetic = SimpleNamespace(run=SimpleNamespace(points=(VectorField(0, 2.),)))
        session = CryostatPointSession(config, None, magnetic, lambda *args: None)
        session.cleaning = True
        with session._recovery('zero'):
            self.assertEqual(session.field_readback_limits.hardware_z_max_t, 9.)
            self.assertEqual(session.active_field_policy.snapshot()['readback_margin_t'], .0005)


class Magnetic6221BoundaryIntegrationTests(unittest.TestCase):
    from tests.test_combination_6221 import Combination6221Tests as _Fixture
    setUp = _Fixture.setUp
    devices = _Fixture.devices
    config = _Fixture.config
    execute = _Fixture.execute
    events = _Fixture.events

    def magnetic_config(self, points=None):
        example = (Path(__file__).resolve().parents[1] / 'config/hardware.example.toml').read_text(encoding='utf-8')
        sections = []
        for name in ('cryostat', 'magnet', 'cleanup'):
            section = re.search(r'(?ms)^\[' + name + r'\]\n.*?(?=^\[|\Z)', example).group()
            if name == 'cryostat':
                section = section.replace('CHANGE_ME_COM_PORT', 'COM_FAKE').replace('C:/CHANGE_ME/attoDRYxyz64bit.dll', 'FAKE.dll')
            elif name == 'magnet':
                section = section.replace('stable_dwell_s = 10.0', 'stable_dwell_s = 2.0')
                section = section.replace('setpoint_ack_tolerance_t = 0.0001', 'setpoint_ack_tolerance_t = 0.0002')
            elif name == 'cleanup':
                section = section.replace('normal_end_field_policy = "hold"', 'normal_end_field_policy = "zero"')
            sections.append(section)
        text = (self.text.replace('order = ["excitation", "lockin"]', 'order = ["magnetic", "excitation", "lockin"]')
                .replace('current_rms_points_a = [0.000001, 0.000002]', 'current_rms_points_a = [0.000001]')
                .replace('frequency_points_hz = [17.777, 200.0]', 'frequency_points_hz = [200.0]')
                .replace('compliance_voltage_v = 1.00', 'compliance_voltage_v = 20.0'))
        requested = points or ((0., -3.6), (0., 3.6), (0., -3.6))
        rendered = ','.join(f'{{bx_t={x!r},bz_t={z!r}}}' for x, z in requested)
        text += '\n' + '\n'.join(sections) + f'''
[magnetic_field_run]
points = [{rendered}]
transition_policy = "direct"
max_step_t = 2.0
run_name = "fake_magnetic_6221"
note = "Offline fake only"
output_directory = "field-audit"
'''
        self.path.write_text(text, encoding='utf-8')
        dll = FakeAttoDryDll()
        for method in ('AttoDRY_Interface_setUserMagneticFieldX', 'AttoDRY_Interface_setUserMagneticFieldZ'):
            original = getattr(dll, method)
            def setter(value, original=original):
                self.assertIn(self.source.state['OUTP:STAT'], {'0', 'OFF'})
                self.assertFalse(int(self.source.state['STAT:OPER:COND']) & 16)
                return original(value)
            setattr(dll, method, setter)
        return dll

    def test_three_point_six_tesla_combination_keeps_source_off_during_motion_and_zeros(self):
        from attodry_control.combination_store import open_readonly, validate_accepted_records
        dll = self.magnetic_config()
        config = self.config()
        self.assertEqual(config.snapshot['field_readback_policy']['readback_margin_t'], 0)
        self.assertTrue(config.snapshot['field_readback_policy']['command_scoped'])
        result = self.execute(config, authorize_cryostat=True, dll=dll)
        self.assertEqual(result['status'], 'completed', result)
        self.assertTrue(result['cleanup']['clean'], result)
        self.assertEqual(self.source.commands.count('SOUR:WAVE:INIT'), 3)
        self.assertEqual(self.source.state['SOUR:CURR:COMP'], '20')
        self.assertEqual(self.source.state['OUTP:STAT'], 'OFF')
        self.assertEqual((dll.bx_t, dll.bz_t), (0., 0.))
        with closing(open_readonly(self.database)) as db:
            self.assertEqual(validate_accepted_records(db, 'current-run', config.snapshot)['accepted_conditions'], 3)

    def quantized_setpoint(self, dll, offset):
        original = dll.AttoDRY_Interface_setUserMagneticFieldZ
        def set_z(value):
            result = original(value)
            if value.value != 0:
                dll.setpoint_z_t = float32_value(value.value + math.copysign(offset, value.value))
                dll.bz_t = dll.setpoint_z_t
            return result
        dll.AttoDRY_Interface_setUserMagneticFieldZ = set_z

    def test_confirmed_field_context_within_ack_is_preserved_not_replaced_by_target(self):
        from attodry_control.combination_store import open_readonly, validate_accepted_records
        dll = self.magnetic_config(((0., 3.6),))
        self.quantized_setpoint(dll, .00004)
        config = self.config()
        result = self.execute(config, authorize_cryostat=True, dll=dll)
        self.assertEqual(result['status'], 'completed', result)
        self.assertTrue(result['cleanup']['clean'], result)
        with closing(open_readonly(self.database)) as db:
            self.assertEqual(validate_accepted_records(db, 'current-run', config.snapshot)['accepted_conditions'], 1)
            sample = json.loads(db.execute('SELECT payload_json FROM combination_samples').fetchone()[0])
        window = next(r['status'] for r in sample['reads'] if r['module'] == 'magnetic')
        contexts = [r['field_readback_assessment']['command_context'] for r in window['samples']]
        for raw, context in zip(window['samples'], contexts):
            self.assertEqual(context, raw['state']['field_setpoint'])
            self.assertGreater(context['bz_t'], float32_value(3.6))
            self.assertLess(context['bz_t'] - float32_value(3.6), .0001)
        self.assertEqual(window['field_readback_assessment']['command_context'], contexts[-1])
        self.assertEqual(self.source.state['OUTP:STAT'], 'OFF')

    def test_setpoint_readback_beyond_ack_still_rejects_before_source_start(self):
        from attodry_control.combination_store import open_readonly, validate_accepted_records
        dll = self.magnetic_config(((0., 3.6),))
        self.quantized_setpoint(dll, .0003)
        config = self.config()
        result = self.execute(config, authorize_cryostat=True, dll=dll)
        self.assertEqual(result['status'], 'failed', result)
        self.assertRegex(result['error'], '(?i)acknowledg|setpoint')
        self.assertEqual(self.source.commands.count('SOUR:WAVE:INIT'), 0)
        with closing(open_readonly(self.database)) as db:
            self.assertEqual(validate_accepted_records(db, 'current-run', config.snapshot)['accepted_conditions'], 0)

    def test_temperature_plus_magnetic_uses_scoped_ack_but_temperature_alone_does_not(self):
        dll = self.magnetic_config()
        example = (Path(__file__).resolve().parents[1] / 'config/hardware.example.toml').read_text(encoding='utf-8')
        tables = [re.search(r'(?ms)^\[' + name + r'\]\n.*?(?=^\[|\Z)', example).group()
                  for name in ('temperature_stability', 'temperature_run', 'temperature_scan')]
        text = self.path.read_text(encoding='utf-8').replace(
            'order = ["magnetic", "excitation", "lockin"]',
            'order = ["temperature", "magnetic", "excitation", "lockin"]')
        text += '\n' + '\n'.join(tables)
        self.path.write_text(text, encoding='utf-8')
        config = self.config()
        self.assertTrue(config.plan.command_scoped_field)
        self.assertEqual(config.magnetic.magnet.setpoint_ack_tolerance_t, .0002)
        self.assertEqual(config.temperature.magnet.setpoint_ack_tolerance_t, .0002)
        # The generic temperature loader never inherits magnetic permission just
        # because another table in the file happens to contain a magnetic plan.
        with self.assertRaisesRegex(ConfigError, 'setpoint_ack|acknowledgement'):
            load_temperature_operation_config(self.path)
        self.path.write_text(text.replace(
            'order = ["temperature", "magnetic", "excitation", "lockin"]',
            'order = ["temperature", "excitation", "lockin"]'), encoding='utf-8')
        with self.assertRaisesRegex(ConfigError, 'setpoint_ack|acknowledgement'):
            self.config()
        self.assertEqual(dll.events, [])
        self.assertEqual(self.manager.opened, [])

    def test_mixed_commands_and_zero_formal_window_keep_traceable_context(self):
        from attodry_control.combination_store import open_readonly, validate_accepted_records
        dll = self.magnetic_config(((1., 0.), (0., 3.6), (0., 0.)))
        config = self.config()
        result = self.execute(config, authorize_cryostat=True, dll=dll)
        self.assertEqual(result['status'], 'completed', result)
        self.assertTrue(result['cleanup']['clean'], result)
        with closing(open_readonly(self.database)) as db:
            self.assertEqual(validate_accepted_records(db, 'current-run', config.snapshot)['accepted_conditions'], 3)
            sample = json.loads(db.execute('SELECT payload_json FROM combination_samples '
                'WHERE condition_id=?', ('condition-000002',)).fetchone()[0])
        window = next(r['status'] for r in sample['reads'] if r['module'] == 'magnetic')
        contexts = [r['field_readback_assessment']['command_context'] for r in window['samples']]
        self.assertTrue(all(c == contexts[0] for c in contexts))
        # Protected zeroing can issue an intermediate nonzero Z waypoint. The
        # context must come from that real confirmed command, not an old target.
        nonzero = [payload['state']['field_setpoint'] for kind, payload in self.events()
                   if kind == 'cryostat_state' and any(payload['state']['field_setpoint'].values())]
        self.assertEqual(contexts[0], nonzero[-1])
        self.assertEqual(contexts[0]['bx_t'], 0.)
        self.assertGreater(contexts[0]['bz_t'], 0.)
        self.assertEqual(window['field_readback_assessment']['command_context'], contexts[0])
        self.assertEqual(self.source.state['OUTP:STAT'], 'OFF')

    def test_zero_context_cannot_be_forged_or_accepted_without_original_trace(self):
        from attodry_control.combination_store import validate_accepted_records
        dll = self.magnetic_config(((1., 0.), (0., 3.6), (0., 0.)))
        config = self.config()
        result = self.execute(config, authorize_cryostat=True, dll=dll)
        self.assertEqual(result['status'], 'completed', result)
        with closing(sqlite3.connect(self.database)) as db:
            original = db.execute('SELECT payload_json FROM combination_samples WHERE condition_id=?',
                                  ('condition-000002',)).fetchone()[0]
            policy = config.plan.magnetic_readback_policy()
            forged = json.loads(original)
            window = next(r['status'] for r in forged['reads'] if r['module'] == 'magnetic')
            for raw in window['samples']:
                raw['field_readback_assessment'] = policy.assessment(VectorField(**raw['state']['field']),
                                                                    command=VectorField(1., 0.))
            mean = window['field_mean']
            window['field_readback_assessment'] = policy.assessment(
                VectorField(mean['field_x_t'], mean['field_z_t']), command=VectorField(1., 0.))
            db.execute('UPDATE combination_samples SET payload_json=? WHERE condition_id=?',
                       (json.dumps(forged), 'condition-000002'))
            with self.assertRaises(ValueError):
                validate_accepted_records(db, 'current-run', config.snapshot)
            db.rollback()
            db.execute("DELETE FROM combination_events WHERE event_type='cryostat_state'")
            with self.assertRaises(ValueError):
                validate_accepted_records(db, 'current-run', config.snapshot)
            db.rollback()
            self.assertEqual(validate_accepted_records(db, 'current-run', config.snapshot)['accepted_conditions'], 3)

    def test_zero_first_mixed_scan_starts_with_conservative_context(self):
        from attodry_control.combination_store import open_readonly, validate_accepted_records
        dll = self.magnetic_config(((0., 0.), (1., 0.), (0., 3.6)))
        config = self.config()
        result = self.execute(config, authorize_cryostat=True, dll=dll)
        self.assertEqual(result['status'], 'completed', result)
        with closing(open_readonly(self.database)) as db:
            self.assertEqual(validate_accepted_records(db, 'current-run', config.snapshot)['accepted_conditions'], 3)
            sample = json.loads(db.execute('SELECT payload_json FROM combination_samples '
                'WHERE condition_id=?', ('condition-000000',)).fetchone()[0])
        window = next(r['status'] for r in sample['reads'] if r['module'] == 'magnetic')
        self.assertTrue(all(r['field_readback_assessment']['command_context'] == {'bx_t': 0., 'bz_t': 0.}
                            for r in window['samples']))
        self.assertTrue(all(r['field_readback_assessment']['mode'] == 'vector' for r in window['samples']))


if __name__ == '__main__':
    unittest.main()
