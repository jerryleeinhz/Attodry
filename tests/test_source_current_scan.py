from dataclasses import replace
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import tempfile
import unittest
from pathlib import Path

from optical_helpers import ROOT, devices
from test_nkt import FakeApi
from attodry_control.nkt_sdk import NktSdk
from attodry_control.optical_config import FeedbackConfig, WindowConfig
from attodry_control.optical_scan import OpticalScan, accepted_points
from attodry_control.optical_test import main as scan_main


def source_scanner(*, targets=(5e-6, 22.5e-6, 40e-6), wavelengths=(633, 633, 633),
                   provider=None, use_pem=True):
    clock, nkt, scan, backend, pem, meter = devices()
    points = tuple(replace(nkt.points[0], source_level_pct=2, wavelength_nm=w, nd_pct=None)
                   for w in wavelengths)
    nkt = replace(nkt, points=points, dwell_s=0.04)
    meter.config = replace(meter.config, max_power_w=50e-6)
    feedback = FeedbackConfig(None, None, 50e-6, 5, 0.04, None, None, None, None,
                              WindowConfig(0.04, 3, 0.01, 0.2e-6),
                              target_powers_w=targets, target_tolerance_fraction=0.10,
                              actuator='source_current', source_current_min_pct=0.1,
                              source_current_max_pct=8, source_current_step_pct=1,
                              minimum_signal_power_w=0.1e-6)
    scan = replace(scan, mode='power_stabilized', feedback=feedback, use_pem=use_pem,
                   peak_retardance_waves=0.25 if use_pem else None)
    backend.filter['nd_pct'] = 25
    backend.source['pulse_picker_ratio'] = 3
    meter.resource.power_provider = lambda: (provider(backend) if provider else
                                            50e-6 * (backend.source['level_pct'] / 8) ** 2)
    return OpticalScan(nkt, scan, backend, pem=pem if use_pem else None, meter=meter,
                       clock=clock, sleep=clock.sleep)


class SourceCurrentScanTests(unittest.TestCase):
    def recovery_scanner(self, **kwargs):
        s = source_scanner(**kwargs)
        s.meter.config = replace(s.meter.config, max_power_w=500e-6)
        scan = replace(s.config, feedback=replace(s.config.feedback, max_power_w=500e-6,
                                                 reduce_above_power_w=50e-6))
        return OpticalScan(s.nkt_config, scan, s.nkt.backend, pem=s.pem, meter=s.meter,
                           clock=s.clock, sleep=s.sleep)

    def test_soft_overshoot_reduces_before_stability_and_completes(self):
        s = self.recovery_scanner(targets=(40e-6,), wavelengths=(633,),
                                  provider=lambda b: 20e-6 * b.source['level_pct'] ** 2)
        result = s.run()
        self.assertTrue(result['completed'], result['error'])
        row = result['points'][0]
        self.assertEqual(row['power_samples'][0]['power_w'], 80e-6)
        self.assertTrue(row['power_samples'][0]['above_reduce_threshold'])
        first = row['source_current_iterations'][0]
        self.assertEqual((first['from_source_current_pct'], first['to_source_current_pct']), (2, 1))
        self.assertFalse(first['window']['power_window_stable'])
        self.assertEqual(first['window']['count'], 1)
        self.assertFalse(s.meter.poisoned)
        self.assertLessEqual(abs(row['feedback_result']['mean_power_w'] - 40e-6), 4e-6)
        self.assertTrue(result['cleanup']['nkt']['off_confirmed'])
        commands = s.nkt.backend.commands
        for i, command in enumerate(commands):
            if command[0] == 'configure':
                self.assertEqual([c for c in commands[:i] if c[0] == 'emission'][-1], ('emission', False))

    def test_formal_soft_overshoot_reuses_sample_and_restarts_dwell(self):
        s = self.recovery_scanner(targets=(40e-6,), wavelengths=(633,))
        drifted = [False]
        def power():
            row = s.record['points'][-1]
            if 'feedback_result' in row:
                drifted[0] = True
            return 50e-6 * (s.nkt.backend.source['level_pct'] / 8) ** 2 * (4 if drifted[0] else 1)
        s.meter.resource.power_provider = power
        result = s.run()
        self.assertTrue(result['completed'], result['error'])
        row = result['points'][0]
        overshoots = [sample for sample in row['power_samples'] if sample['above_reduce_threshold']]
        self.assertTrue(overshoots)
        self.assertEqual(overshoots[0]['phase'], 'rejected_formal_drift')
        self.assertFalse(overshoots[0]['accepted'])
        self.assertEqual(len({sample['sequence'] for sample in row['power_samples']}), len(row['power_samples']))
        reductions = [change for change in row['source_current_iterations']
                      if change['window']['above_reduce_threshold']]
        self.assertEqual(len(reductions), len(overshoots))
        self.assertTrue(all(change['window']['count'] == 1 and
                            change['to_source_current_pct'] < change['from_source_current_pct']
                            for change in reductions))
        self.assertTrue(row['feedback_result']['target_in_tolerance'])

    def test_hard_overshoot_after_completed_point_rejects_all_without_reduction(self):
        s = self.recovery_scanner(targets=(40e-6, 40e-6), wavelengths=(633, 638))
        def power():
            row = s.record['points'][-1]
            if len(s.record['points']) == 2 and 'feedback_result' in row:
                return 501e-6
            return 50e-6 * (s.nkt.backend.source['level_pct'] / 8) ** 2
        s.meter.resource.power_provider = power
        result = s.run()
        self.assertFalse(result['completed'])
        self.assertEqual(len(result['points']), 2)
        self.assertTrue(all(not row['accepted'] for row in result['points']))
        self.assertEqual(accepted_points(result), [])
        self.assertTrue(s.meter.poisoned)
        self.assertTrue(result['cleanup']['nkt']['off_confirmed'])
        # No revision is issued for the hard-limit sample.
        self.assertFalse(any(change['window'].get('above_reduce_threshold')
                             for change in result['points'][1]['source_current_iterations']))

    def test_power_grid_converges_nonlinearly_without_changing_nd_pp_or_band(self):
        s = source_scanner()
        result = s.run()
        self.assertTrue(result['completed'], result['error'])
        self.assertEqual(len(accepted_points(result)), 3)
        self.assertEqual(result['feedback_fixed_settings'], {'nd_pct': 25, 'pulse_picker_ratio': 3})
        for row, target in zip(result['points'], (5e-6, 22.5e-6, 40e-6)):
            self.assertEqual(row['feedback_actuator'], 'source_current')
            self.assertEqual(row['nd_iterations'], [])
            self.assertTrue(row['source_current_iterations'])
            self.assertTrue(row['feedback_result']['target_in_tolerance'])
            self.assertLessEqual(abs(row['measured_power_w'] - target), target * 0.10)
            self.assertEqual(row['readback']['nkt']['filter']['nd_pct'], 25)
            self.assertEqual(row['readback']['nkt']['source']['pulse_picker_ratio'], 3)
            self.assertEqual(row['readback']['nkt']['filter']['lower_edge_nm'], 628)
            self.assertEqual(row['readback']['nkt']['filter']['upper_edge_nm'], 638)
            self.assertEqual(row['pem_preparation']['requested_amplitude_nm'], 158.25)
            for change in row['source_current_iterations']:
                self.assertLessEqual(abs(change['to_source_current_pct'] - change['from_source_current_pct']), 1)
        commands = s.nkt.backend.commands
        for i, command in enumerate(commands):
            if command[0] == 'configure':
                self.assertEqual([c for c in commands[:i] if c[0] == 'emission'][-1], ('emission', False))
                self.assertIsNone(command[1]['nd_pct'])
                self.assertIsNone(command[1]['pulse_picker_ratio'])
        self.assertTrue(result['cleanup']['nkt']['off_confirmed'])

    def test_wavelength_grid_reacquires_fixed_power_and_quarter_wave(self):
        s = source_scanner(targets=(40e-6,) * 3, wavelengths=(633, 638, 643),
                           provider=lambda b: 50e-6 * (b.source['level_pct'] / 8) ** 2 *
                           (1 + (b.filter['lower_edge_nm'] + 5 - 633) / 50))
        result = s.run()
        self.assertTrue(result['completed'], result['error'])
        self.assertEqual([r['center_setpoint_nm'] for r in result['points']], [633, 638, 643])
        self.assertEqual([r['pem_preparation']['requested_amplitude_nm'] for r in result['points']],
                         [158.25, 159.5, 160.75])
        self.assertTrue(all(abs(r['measured_power_w'] - 40e-6) <= 4e-6 for r in result['points']))

    def test_first_invalid_or_dark_sample_never_increases_current(self):
        for power in (-22e-9, float('nan'), 50.1e-6, 0, 22e-9):
            with self.subTest(power=power):
                s = source_scanner(provider=lambda b: power)
                result = s.run()
                self.assertFalse(result['completed'])
                self.assertEqual(result['points'][0]['source_current_iterations'], [])
                self.assertEqual(sum(c[0] == 'configure' for c in s.nkt.backend.commands), 1)
                self.assertEqual(sum(c == ('emission', True) for c in s.nkt.backend.commands), 1)
                self.assertTrue(result['cleanup']['nkt']['off_confirmed'])
                self.assertTrue(s.meter.closed and s.pem.closed)
                self.assertEqual(accepted_points(result), [])
                if power in (0, 22e-9):
                    self.assertEqual(len(result['points'][0]['power_samples']), 1)
                    self.assertEqual(result['points'][0]['power_samples'][0]['power_w'], power)

    def test_fixed_nd_or_pp_drift_rejects_without_restoring_or_retuning(self):
        for field in ('nd', 'pp'):
            s = source_scanner()
            def drift(b):
                if field == 'nd':
                    b.filter['nd_pct'] = 25.1
                else:
                    b.source['pulse_picker_ratio'] = 4
                return 3e-6
            s.meter.resource.power_provider = lambda: drift(s.nkt.backend)
            result = s.run()
            self.assertIn('unchanged preflight ND and pulse picker', result['error'])
            self.assertEqual(result['points'][0]['source_current_iterations'], [])
            self.assertEqual(sum(c[0] == 'configure' for c in s.nkt.backend.commands), 1)
            self.assertTrue(result['cleanup']['nkt']['off_confirmed'])

    def test_boundary_and_failed_current_write_preserve_rejection(self):
        for failure in ('boundary', 'write', 'interrupt'):
            s = source_scanner(provider=lambda b: 1e-6)
            if failure != 'boundary':
                original = s.nkt.backend.configure
                count = [0]
                def configure(point):
                    count[0] += 1
                    if count[0] == 2:
                        if failure == 'interrupt':
                            raise KeyboardInterrupt()
                        raise OSError('current write failed')
                    original(point)
                s.nkt.backend.configure = configure
            result = s.run()
            self.assertFalse(result['completed'])
            self.assertTrue(result['cleanup']['nkt']['off_confirmed'])
            self.assertEqual(accepted_points(result), [])
            self.assertLessEqual(s.clock(), 5)
            if failure == 'boundary':
                self.assertIn('unreachable', result['error'])
            elif failure == 'interrupt':
                self.assertEqual(result['outcome'], 'interrupted')
            else:
                self.assertIn('current write failed', result['error'])

    def test_total_deadline_is_not_reset_by_current_revisions(self):
        s = source_scanner(targets=(40e-6,), wavelengths=(633,))
        scan = replace(s.config, feedback=replace(s.config.feedback, timeout_s=0.11))
        result = OpticalScan(s.nkt_config, scan, s.nkt.backend, pem=s.pem, meter=s.meter,
                             clock=s.clock, sleep=s.sleep).run()
        self.assertFalse(result['completed'])
        self.assertIn('timeout', result['error'].lower())
        self.assertTrue(result['points'][0]['source_current_iterations'])
        self.assertLessEqual(s.clock(), 0.12)
        self.assertTrue(result['cleanup']['nkt']['off_confirmed'])

    def test_formal_drift_retunes_current_and_preserves_rejected_samples(self):
        s = source_scanner(targets=(22.5e-6,), wavelengths=(633,))
        drifted = [False]
        def power(b):
            row = s.record['points'][-1]
            if any(sample['phase'] == 'formal' for sample in row['power_samples']):
                drifted[0] = True
            return 50e-6 * (b.source['level_pct'] / 8) ** 2 * (1.2 if drifted[0] else 1)
        s.meter.resource.power_provider = lambda: power(s.nkt.backend)
        result = s.run()
        self.assertTrue(result['completed'], result['error'])
        row = result['points'][0]
        rejected = [sample for sample in row['power_samples'] if sample['phase'] == 'rejected_formal_drift']
        self.assertTrue(rejected)
        self.assertTrue(all(not sample['accepted'] for sample in rejected))
        self.assertTrue(row['feedback_result']['target_in_tolerance'])
        self.assertEqual(row['readback']['nkt']['filter']['nd_pct'], 25)

    def test_fake_sdk_uses_current_register_with_unverified_nd_untouched(self):
        s = source_scanner(targets=(5e-6,), wavelengths=(633,), use_pem=False)
        nkt = replace(s.nkt_config, backend='nkt_sdk', port='FAKE_CURRENT_PORT', dll_path='unused.dll',
                      source_serial='SOURCE01', varia_serial='VARIA001')
        api = FakeApi()
        api.data[2, 0x32] = (250).to_bytes(2, 'little')
        backend = NktSdk(nkt, api=api, authorize_writes=True).open(authorize_connection=True)
        s.meter.resource.power_provider = lambda: 50e-6 * (int.from_bytes(api.data[1, 0x38], 'little') / 80) ** 2
        result = OpticalScan(nkt, s.config, backend, meter=s.meter, clock=s.clock, sleep=s.sleep).run(
            authorize_writes=True, confirm_manual_route=True)
        self.assertTrue(result['completed'], result['error'])
        writes = [c for c in api.calls if c[0] == 'write']
        self.assertTrue(any(c[1:3] == (1, 0x38) for c in writes))
        self.assertFalse(any(c[1:3] in ((2, 0x32), (1, 0x34)) for c in writes))
        self.assertFalse(nkt.varia_nd_control_verified)

    def test_simulation_cli_produces_all_three_accepted_power_targets(self):
        example = ROOT / 'config/optical_source_current_simulation.toml'
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'source.toml'
            path.write_text(example.read_text().replace('../run_data/optical_source_current_simulation', 'data'))
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
                status = scan_main(['simulate', '--config', str(path)])
            self.assertEqual(status, 0, errors.getvalue())
            record = json.loads(next((Path(td) / 'data').glob('*.json')).read_text())
            self.assertEqual(len(accepted_points(record)), 3)
            self.assertTrue(record['simulated'])
            self.assertTrue(all(r['feedback_actuator'] == 'source_current' for r in record['points']))


if __name__ == '__main__':
    unittest.main()
