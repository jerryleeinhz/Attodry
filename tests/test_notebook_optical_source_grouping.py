"""Notebook-only optical grouping policy, with hardware access blocked by runner."""
from contextlib import contextmanager, redirect_stdout
import copy
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import attodry_control.unified_plotting as plotting
from tests.test_unified_plotting import observation, Y


ROOT = Path(__file__).resolve().parents[1]
NOTEBOOKS = ('combination_analysis.ipynb', 'unified_plotting.ipynb')
SOURCE = 'requested.optical_source_level_pct'
POWER = 'measured.optical_power_w'


def optical_rows():
    rows = []
    for i in range(2):
        row = observation(i, value=(i + 1) * 1e-6)
        for key in list(row):
            if 'field_' in key or key.startswith('axes.magnetic.'):
                row.pop(key)
        row.update({SOURCE: 29. + 20 * i, 'actual.optical_source_level_pct': 28.9 + 20 * i,
                    'requested.optical_target_power_w': (i + 1) * 10e-6,
                    'requested.optical_wavelength_nm': 532.,
                    'requested.optical_bandwidth_nm': 88., 'requested.gate_bottom_v': 0.,
                    'actual.gate_bottom_v': 0., 'axes.optical.index': i,
                    POWER: (i + 1) * 9.5e-6})
        rows.append(row)
    return rows


@contextmanager
def notebook_policy(name):
    document = json.loads((ROOT / 'notebooks' / name).read_text(encoding='utf-8'))
    with patch.object(plotting, '_condition_columns', plotting._condition_columns):
        scope = {}
        for cell in document['cells']:
            if cell['cell_type'] == 'code':
                code = ''.join(cell['source'])
                compile(code, name, 'exec')
                if cell['id'] == 'analysis-imports':
                    exec(code, scope)
                    exec(code, scope)  # Re-running a cell must not recurse.
        yield scope


class NotebookOpticalGroupingTests(unittest.TestCase):
    def setUp(self):
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        self.addCleanup(lambda: plt.close('all'))

    def test_both_notebooks_render_power_curves_maps_and_channels(self):
        rows = optical_rows()
        before = copy.deepcopy(rows)
        for name in NOTEBOOKS:
            with self.subTest(notebook=name), notebook_policy(name):
                for spec in (dict(x=POWER, y=Y),
                             dict(mode='xy_z', x=POWER, y=Y, z='actual.optical_source_level_pct'),
                             dict(mode='field', x=POWER, statistics='mean_sd')):
                    figure, data = plotting.render_plot(rows, spec)
                    self.assertGreater(data['report']['plotted_row_count'], 0)
                    if spec.get('mode', 'curve') == 'curve':
                        self.assertEqual(len(figure.axes[0].lines), 1)
        self.assertEqual(rows, before)

    def test_other_varying_conditions_still_require_resolution(self):
        for name in NOTEBOOKS:
            with self.subTest(notebook=name), notebook_policy(name):
                for key, value in (('requested.optical_wavelength_nm', 620.),
                                   ('requested.optical_bandwidth_nm', 10.),
                                   ('requested.gate_bottom_v', 6.),
                                   ('requested.lockin_frequency_hz', 51000.)):
                    rows = optical_rows()
                    rows[1][key] = value
                    with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'Unresolved'):
                        plotting.render_plot(rows, dict(x=POWER, y=Y))
                unresolved = plotting._unresolved_dimensions(
                    optical_rows(), plot_keys=('actual.gate_bottom_v', Y),
                    filter_keys=(), group_keys=())
                self.assertIn(('requested.optical_target_power_w', 2), unresolved)

    def test_source_metadata_is_available_for_optional_grouping_and_export(self):
        rows = optical_rows()
        with notebook_policy(NOTEBOOKS[0]):
            self.assertIn(SOURCE, {key for _, key in plotting.numeric_columns(rows)})
            self.assertIn(SOURCE, {key for _, key in plotting.scalar_columns(rows)})
            figure, data = plotting.render_plot(rows, dict(x=POWER, y=Y, group_by=SOURCE))
            self.assertEqual(len(figure.axes[0].lines), 2)
            with tempfile.TemporaryDirectory() as directory:
                destination = plotting.export_plot_bundle(directory, [
                    {'figure': figure, 'data': data, 'spec': dict(x=POWER, y=Y, group_by=SOURCE)}])
                with (destination / 'selected_samples.csv').open(encoding='utf-8-sig', newline='') as stream:
                    exported = list(csv.DictReader(stream))
                self.assertEqual({float(row[SOURCE]) for row in exported}, {29., 49.})

    def test_quality_exclusions_and_condition_statistics_are_preserved(self):
        rows = optical_rows()
        rows[1]['status.lockin']['samples'][1]['lockin_xy']['lia_status']['raw'] = 8
        with notebook_policy(NOTEBOOKS[0]):
            _, data = plotting.render_plot(rows, dict(x=POWER, y=Y, statistics='mean_sd'))
            self.assertEqual(data['report']['excluded_quality_count'], 1)
            self.assertEqual(len(data['rows']), 1)
            _, audit = plotting.render_plot(rows, dict(
                x=POWER, y=Y, statistics='mean_sd', quality_policy='include'))
            self.assertEqual(len(audit['rows']), 2)
            self.assertEqual(len(audit['summary_rows']), 2)
            self.assertEqual([point['n'] for point in audit['summary_rows']], [1, 1])

    def test_notebook_widget_renders_without_source_group_selection(self):
        with notebook_policy(NOTEBOOKS[0]) as scope, tempfile.TemporaryDirectory() as directory:
            with redirect_stdout(io.StringIO()):
                dashboard = scope['UnifiedPlotDashboard'](directory)
                dashboard.add_plot('curve')
                card = dashboard.cards[0]
                with patch.object(dashboard, '_selected_rows', return_value=tuple(optical_rows())):
                    dashboard._update_card_options(card)
                    card['x'].value, card['y'].value = POWER, Y
                    self.assertEqual(card['group'].value, '')
                    dashboard._render_card(card)
                    self.assertIsNotNone(card['rendered'])
                    self.assertEqual(card['rendered']['data']['report']['plotted_row_count'], 2)


if __name__ == '__main__':
    unittest.main()
