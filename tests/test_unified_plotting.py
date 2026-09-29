"""Offline regressions for selection, channel quality, statistics and widgets."""
import ast
from contextlib import redirect_stdout
import copy
import csv
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from attodry_control.analysis_observations import channel_quality, qualify_observations, repeat_statistics
from attodry_control.unified_plotting import (
    UnifiedPlotDashboard, _curve_groups, _filter_rows, _source_signature, _sample_token,
    discover_plot_sources, load_plot_sources, render_plot, export_plot_bundle,
)

X = 'actual.field_x_t'
Y = 'measured.lockin_xy_h2_amplitude_v'
H1 = 'measured.lockin_xy_h1_amplitude_v'


def observation(condition=0, sample=0, value=1.0, flag=0):
    return {
        'source_path': 'archive.sqlite', 'run_id': 'test', 'condition_id': str(condition),
        'attempt_index': 0, 'sample_index': sample, 'repeat_index': 0, 'accepted': True,
        'axes.magnetic.index': condition, 'axes.magnetic.direction': 'forward',
        'requested.field_x_t': condition, 'requested.field_z_t': 0.,
        'requested.lockin_excitation_v_rms': 1., 'requested.lockin_frequency_hz': 50000.,
        X: condition, Y: value, H1: 2.,
        'status.lockin': {'samples': [
            {'harmonic': 1, 'settings_verified': True, 'selected_roles': ['xy'],
             'lockin_xy': {'lia_status': {'raw': 4}, 'error_status': 0, 'reading': {'harmonic': 1}}},
            {'harmonic': 2, 'settings_verified': True, 'selected_roles': ['xy'],
             'lockin_xy': {'lia_status': {'raw': flag}, 'error_status': 0, 'reading': {'harmonic': 2}}}],
            'transition_probes': [{'lockin_xy': {'lia_status': {'raw': 4}}}]},
    }


class ObservationTests(unittest.TestCase):
    def test_channel_specific_formal_status(self):
        row = observation()
        before = copy.deepcopy(row)
        self.assertEqual(channel_quality(row, H1), ('flagged', ('output_overload',)))
        self.assertEqual(channel_quality(row, Y), ('clear', ()))
        self.assertEqual(len(qualify_observations([row], [Y])[0]), 1)
        self.assertEqual(len(qualify_observations([row], [H1])[0]), 0)
        self.assertEqual(row, before)

    def test_unknown_is_not_assumed_clear_or_dropped(self):
        row = observation()
        row.pop('status.lockin')
        kept, report = qualify_observations([row], [Y])
        self.assertEqual(len(kept), 1)
        self.assertEqual(report['unknown_status_count'], 1)

    def test_overload_types_errors_unlock_and_settings(self):
        for bit, reason in [(1, 'input_or_reserve_overload'), (2, 'filter_overload'),
                            (4, 'output_overload'), (8, 'reference_unlocked')]:
            with self.subTest(bit=bit):
                self.assertIn(reason, channel_quality(observation(flag=bit), Y)[1])
        row = observation()
        sample = row['status.lockin']['samples'][1]
        sample['settings_verified'] = False
        sample['lockin_xy']['error_status'] = 16
        self.assertEqual(set(channel_quality(row, Y)[1]), {'settings_unverified', 'instrument_error'})

    def test_clipping_and_audit_provenance(self):
        row = observation(value=.022)
        row['status.lockin']['samples'][1]['settings_after'] = {'roles': {'lockin_xy': {'sensitivity_full_scale_v': .020}}}
        kept, report = qualify_observations([row], [Y], 'include')
        self.assertEqual(len(kept), 1)
        self.assertEqual(report['flagged_row_count'], 1)
        self.assertEqual(report['excluded_quality_count'], 0)
        self.assertEqual(report['flagged_samples'][0]['reasons'][Y], ['exceeds_full_scale'])

    def test_missing_selected_channel_not_companion(self):
        row = observation()
        row['status.lockin']['samples'][1]['selected_roles'] = ['xx']
        self.assertIn('not_selected_channel', channel_quality(row, Y)[1])

    def test_legacy_long_row_status(self):
        row = {'role': 'xy', 'harmonic': 2, 'lia_status_raw': 4}
        self.assertEqual(channel_quality(row, Y)[0], 'flagged')
        self.assertEqual(channel_quality(row, H1)[0], 'unknown')

    def test_mean_sd_sem_and_exact_identities(self):
        rows = [observation(sample=i, value=v) for i, v in enumerate((1., 2., 3.))]
        point, = repeat_statistics(rows, x=X, y=Y)
        self.assertEqual((point['y'], point['n'], point['sd']), (2., 3, 1.))
        self.assertAlmostEqual(point['sem'], 1 / math.sqrt(3))
        self.assertEqual(len(point['sample_ids']), 3)
        self.assertAlmostEqual(repeat_statistics(rows, x=X, y=Y, mode='mean_sem')[0]['error'], point['sem'])

    def test_do_not_pool_runs_directions_revisits_or_attempts(self):
        base = observation()
        rows = [base]
        for key, value in [('run_id', 'other'), ('source_path', 'other.sqlite'),
                           ('axes.magnetic.direction', 'reverse'), ('condition_id', 'revisit'),
                           ('attempt_index', 1), ('repeat_index', 1)]:
            rows.append({**base, key: value})
        self.assertEqual(len(repeat_statistics(rows, x=X, y=Y)), 7)
        self.assertEqual(len(_curve_groups(rows[:4], None, separate_samples=False)), 4)

    def test_missing_n1_and_gap_are_not_zero(self):
        points = repeat_statistics([observation(value=2.), observation(condition=1, value=None)], x=X, y=Y)
        self.assertIsNone(points[0]['error'])
        self.assertIsNone(points[1]['y'])
        self.assertEqual(points[1]['n'], 0)

    def test_circular_phase_wrap_and_undefined_antipodes(self):
        phase = Y.replace('amplitude_v', 'phase_deg')
        rows = [{**observation(sample=i), phase: v} for i, v in enumerate((179., -179.))]
        p, = repeat_statistics(rows, x=X, y=phase)
        self.assertAlmostEqual(abs(p['y']), 180.)
        self.assertAlmostEqual(p['sd'], math.sqrt(2))
        rows[0][phase], rows[1][phase] = 0., 180.
        self.assertIsNone(repeat_statistics(rows, x=X, y=phase)[0]['y'])

    def test_unidentified_csv_cannot_invent_repeats(self):
        with self.assertRaisesRegex(ValueError, 'condition_id'):
            repeat_statistics([{'x': 1., 'y': 2.}], x='x', y='y')


class PlotTests(unittest.TestCase):
    def setUp(self):
        import matplotlib
        matplotlib.use('Agg')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        import matplotlib.pyplot as plt
        self.addCleanup(lambda: plt.close('all'))

    def test_excluded_samples_form_line_gaps(self):
        rows = [observation(i, value=i + 1., flag=4 if i == 1 else 0) for i in range(3)]
        figure, data = render_plot(rows, dict(x=X, y=Y, curve_style='line'))
        ydata = figure.axes[0].lines[0].get_ydata()
        self.assertTrue(math.isnan(ydata[1]))
        self.assertEqual(data['report']['excluded_quality_count'], 1)
        self.assertEqual(len(data['rows']), 2)

    def test_empty_filter_means_no_rows(self):
        self.assertEqual(_filter_rows([observation()], {'run_id': []}), ())
        with self.assertRaisesRegex(ValueError, 'No data'):
            render_plot([observation()], dict(x=X, y=Y, filters={'run_id': []}))

    def test_unresolved_excitation_requires_filter_or_group(self):
        rows = [observation(1), {**observation(2), 'requested.lockin_excitation_v_rms': 2.}]
        with self.assertRaisesRegex(ValueError, 'Unresolved'):
            render_plot(rows, dict(x=X, y=Y))
        _, data = render_plot(rows, dict(x=X, y=Y, group_by='requested.lockin_excitation_v_rms'))
        self.assertEqual(len(data['rows']), 2)

    def test_log_errors_cannot_be_silently_clipped(self):
        rows = [observation(1, i, v) for i, v in enumerate((.01, 10., .01))]
        with self.assertRaisesRegex(ValueError, 'crossing zero'):
            render_plot(rows, dict(x=X, y=Y, statistics='mean_sd', y_scale='log'))

    def test_field_quality_separates_h1_h2(self):
        rows = [observation(1, i, i + 1.) for i in range(3)]
        _, data = render_plot(rows, dict(mode='field', x=X, statistics='mean_sd'))
        self.assertEqual(data['report']['channels'][H1]['excluded_quality_count'], 3)
        self.assertEqual(data['report']['channels'][Y]['displayed_point_count'], 1)
        self.assertEqual(next(p for p in data['summary_rows'] if p['channel'] == Y)['n'], 3)

    def test_manual_exclusion_identity_retained(self):
        rows = [observation(1, i, i + 1.) for i in range(3)]
        spec = dict(x=X, y=Y, statistics='mean_sd', excluded_sample_ids=[_sample_token(rows[0])])
        _, data = render_plot(rows, spec)
        self.assertEqual(data['summary_rows'][0]['n'], 2)
        self.assertEqual(data['report']['manual_excluded_sample_ids'], spec['excluded_sample_ids'])

    def test_manual_exclusion_also_breaks_lines(self):
        rows = [observation(i) for i in range(3)]
        fig, _ = render_plot(rows, dict(x=X, y=Y, curve_style='line', excluded_sample_ids=[_sample_token(rows[1])]))
        self.assertTrue(math.isnan(fig.axes[0].lines[0].get_ydata()[1]))

    def test_generic_csv_extra_signal_is_not_unresolved_condition(self):
        path = self.directory / 'signals.csv'
        path.write_text('U,V,W\n1,2,3\n2,4,6\n', encoding='utf-8')
        sources = discover_plot_sources(self.directory)
        self.assertEqual(len(sources), 1)
        rows = load_plot_sources(sources)
        _, data = render_plot(rows, dict(x='csv.U', y='csv.V'))
        self.assertEqual(len(data['rows']), 2)

    def test_map_retains_overlaps_and_signed_color_limits(self):
        rows = [observation(1, i, v) for i, v in enumerate((-1., 1.))]
        _, data = render_plot(rows, dict(mode='xy_z', x=X, y='requested.field_z_t', z=Y))
        self.assertEqual(data['report']['duplicate_xy_observation_count'], 1)
        self.assertEqual(data['report']['color_normalization']['vcenter'], 0)

    def test_audit_and_simulation_visible(self):
        rows = [{**observation(1), 'accepted': False, 'simulated': True}]
        fig, _ = render_plot(rows, dict(x=X, y=H1, quality_policy='include'))
        title = fig.axes[0].get_title()
        self.assertIn('SIMULATED', title)
        self.assertIn('AUDIT: rejected', title)
        self.assertIn('flagged samples included', title)

    def test_export_exact_rows_statistics_and_exclusion_manifest(self):
        rows = [observation(1, i, i + 1., flag=4 if i == 2 else 0) for i in range(3)]
        spec = dict(x=X, y=Y, statistics='mean_sd')
        fig, data = render_plot(rows, spec)
        destination = export_plot_bundle(self.directory, [{'figure': fig, 'data': data, 'spec': spec}])
        manifest = json.loads((destination / 'plot_manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['selected_sample_rows'], 2)
        self.assertEqual(manifest['plots'][0]['report']['excluded_quality_count'], 1)
        with (destination / 'plotted_statistics.csv').open(encoding='utf-8') as stream:
            summary = list(csv.DictReader(stream))
        self.assertEqual(summary[0]['n'], '2')
        self.assertEqual(len(json.loads(summary[0]['sample_ids'])), 2)
        self.assertTrue(all((destination / ('plot_01.' + ext)).is_file() for ext in ('png', 'pdf', 'svg')))

    def test_wal_changes_source_identity(self):
        source = self.directory / 'data.sqlite'
        source.write_bytes(b'original')
        first = _source_signature(source)
        Path(str(source) + '-wal').write_bytes(b'new committed data')
        self.assertNotEqual(_source_signature(source), first)

    def dashboard(self):
        source = self.directory / 'data.csv'
        source.write_text('U,V,W\n1,2,3\n2,4,6\n', encoding='utf-8')
        dashboard = UnifiedPlotDashboard(self.directory)
        dashboard.add_plot('curve', {'source_paths': [str(source)], 'x': 'csv.U', 'y': 'csv.V', 'filters': {'csv.W': [3.]}})
        return dashboard, dashboard.cards[0], source

    def test_widget_axes_filters_and_invalidation(self):
        with redirect_stdout(io.StringIO()):
            d, card, _ = self.dashboard()
            self.assertEqual((card['x'].value, card['y'].value), ('csv.U', 'csv.V'))
            self.assertEqual(card['filters'][0]['choices'].value, (3.,))
            d._render_card(card)
            self.assertIsNotNone(card['rendered'])
            self.assertTrue(card['image'].value)
            card['filters'][0]['choices'].value = ()
            self.assertEqual(d._card_spec(card)['filters']['csv.W'], [])
            self.assertIsNone(card['rendered'])
            self.assertFalse(card['image'].value)

    def test_saved_setup_restores_axes_empty_filter_and_audit(self):
        with redirect_stdout(io.StringIO()):
            d, card, _ = self.dashboard()
            card['filters'][0]['choices'].value = ()
            d.include_audit.value = True
            card['quality'].value = 'include'
            d.configuration_path.value = str(self.directory / 'setup.json')
            d._save_setup()
            card['y'].value = 'csv.W'
            d._load_setup()
            loaded = d.cards[0]
            self.assertEqual(loaded['y'].value, 'csv.V')
            self.assertEqual(loaded['filters'][0]['choices'].value, ())
            self.assertEqual(loaded['quality'].value, 'include')
            self.assertTrue(d.include_audit.value)

    def test_stale_export_refused_and_missing_setup_preserves_cards(self):
        with redirect_stdout(io.StringIO()):
            d, card, source = self.dashboard()
            d._render_card(card)
            source.write_text('U,V,W\n1,3,3\n2,8,6\n3,9,9\n', encoding='utf-8')
            d._export()
            self.assertIn('changed after rendering', d.status.value)
            self.assertIsNone(card['rendered'])
            setup = d._configuration()
            setup['plots'][0]['source_paths'] = ['does-not-exist.csv']
            path = self.directory / 'missing.json'
            path.write_text(json.dumps(setup), encoding='utf-8')
            d.configuration_path.value = str(path)
            d._load_setup()
            self.assertIn('missing', d.status.value)
            self.assertIs(d.cards[0], card)

    def test_failed_refresh_cannot_keep_old_sources_or_figures(self):
        with redirect_stdout(io.StringIO()):
            d, card, _ = self.dashboard()
            d._render_card(card)
            d.directory.value = str(self.directory / 'missing-directory')
            d._refresh()
            self.assertEqual(d.catalog, ())
            self.assertEqual(card['sources'].value, ())
            self.assertIsNone(card['rendered'])
            self.assertFalse(card['image'].value)

    def test_modules_do_not_import_instrument_drivers(self):
        import attodry_control.unified_plotting as module
        forbidden = {'sr830', 'attodry', 'pyvisa', 'qcodes', 'keithley2400'}
        for name in ('unified_plotting.py', 'analysis_observations.py'):
            tree = ast.parse(Path(module.__file__).with_name(name).read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertNotIn((node.module or '').split('.')[-1], forbidden)
                elif isinstance(node, ast.Import):
                    self.assertFalse({a.name.split('.')[0] for a in node.names} & forbidden)


if __name__ == '__main__':
    unittest.main()
