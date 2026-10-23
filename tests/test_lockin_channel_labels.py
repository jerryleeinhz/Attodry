"""Archived sample wiring labels; fake acquisition and read-only plots only."""
from copy import deepcopy
from contextlib import redirect_stdout
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
import warnings

from attodry_control.combination_hardware import load_hardware_combination
from attodry_control.combination_analysis import load_combination_rows, load_combination_dark_rows
from attodry_control.combination_store import CombinationStore
from attodry_control.photonics_lockin_config import load_photonics_lockin_config
from attodry_control.unified_plotting import (UnifiedPlotDashboard, column_label,
    numeric_columns, render_plot, export_plot_bundle)
from tests import test_combination_hardware as dual
from tests import test_three_lockin as triple
from tests import test_dark_drift_plotting as dark
from tests.photonics_lockin_helpers import fixture_document, safety_document
from tests.test_unified_plotting import observation, X, Y


class ConfigLabels(unittest.TestCase):
    setUp = dual.ElectricalCombinationTests.setUp
    write_config = dual.ElectricalCombinationTests.write_config
    execute = dual.ElectricalCombinationTests.execute
    factory = dual.ElectricalCombinationTests.factory

    def labelled(self, value='"端口1-2 / b轴"'):
        self.write_config(order=("lockin",), source=self.base.replace(
            "[lockin_xx]\n", "[lockin_xx]\nchannel_label = " + value + "\n", 1).replace(
            "[lockin_xy]\n", '[lockin_xy]\nchannel_label = "端口3-4"\n', 1))

    def test_dual_label_snapshot_samples_and_historical_reload(self):
        self.labelled()
        cfg = load_hardware_combination(self.path)
        self.assertEqual(cfg.snapshot['hardware']['lockin']['lockin_xx']['channel_label'], '端口1-2 / b轴')
        result = self.execute()
        self.assertEqual(result['status'], 'completed', result)
        self.labelled('"different wiring now"')
        rows = load_combination_rows(self.database, run_id='test')
        self.assertTrue(rows)
        self.assertEqual(rows[0]['status.lockin']['channel_labels']['xx'], '端口1-2 / b轴')
        self.assertEqual(rows[0]['channel_label.xx'], '端口1-2 / b轴')
        self.assertEqual(rows[0]['channel_label.xy'], '端口3-4')

    def test_optional_blank_and_invalid_labels(self):
        self.write_config(order=("lockin",))
        self.assertEqual(load_hardware_combination(self.path).lockin.lockin_xx.channel_label, '')
        self.labelled('"   "')
        self.assertEqual(load_hardware_combination(self.path).lockin.lockin_xx.channel_label, '')
        for value in ('42', 'true', '["name"]', '"line\\nnew"'):
            self.labelled(value)
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'channel_label'):
                load_hardware_combination(self.path)

    def test_photonics_pair_uses_the_same_optional_labels(self):
        doc = fixture_document()
        doc['lockin_xx']['channel_label'] = '  contact A-B  '
        cfg = load_photonics_lockin_config('fake.toml', document=doc, safety_document=safety_document())
        self.assertEqual(cfg.lockin_xx.channel_label, 'contact A-B')
        self.assertEqual(cfg.lockin_xy.channel_label, '')


class ThreeLabels(unittest.TestCase):
    setUp = triple.ThreeLockinTests.setUp
    write_config = triple.ThreeLockinTests.write_config
    config = triple.ThreeLockinTests.config
    make = triple.ThreeLockinTests.make

    def test_three_role_labels_add_no_instrument_commands_or_waits(self):
        records = []
        original = self.base
        for labelled in (False, True):
            self.base = original if not labelled else original.replace(
                '[lockin_xx]\n', '[lockin_xx]\nchannel_label = "b axis"\n', 1).replace(
                '[lockin_xy]\n', '[lockin_xy]\nchannel_label = "45 axis"\n', 1)
            session, manager, _ = self.make()
            reading = session.sample_point()
            records.append(({name: (list(r.queries), list(r.writes)) for name, r in manager.resources.items()},
                            self.clock.value, reading['measurements']))
            if labelled:
                self.assertEqual(reading['status']['channel_labels'],
                                 {'xx': 'b axis', 'xy': '45 axis', 'aux': 'Third sample voltage'})
            session.close()
        self.assertEqual(records[0], records[1])


class DarkLabels(unittest.TestCase):
    setUp = dark.DarkDriftTests.setUp

    def test_dark_labels_from_archived_run_not_todays_toml(self):
        plan = {'hardware': {'lockin': {'lockin_xx': {'channel_label': '端口1-2'},
                                      'lockin_xy': {'channel_label': '端口3-4'}}}}
        with CombinationStore(self.database) as store, store.connection:
            store.connection.execute("UPDATE combination_runs SET plan_json=? WHERE run_id='selected'", (json.dumps(plan),))
        before = self.database.read_bytes()
        rows = load_combination_dark_rows(self.database, run_id='selected')
        self.assertEqual(rows[0]['channel_label.xy'], '端口3-4')
        self.assertEqual(self.database.read_bytes(), before)
        self.assertIn('XY/端口3-4', dict((v, k) for k, v in numeric_columns(rows))[dark.DARK])
        self.assertNotIn('channel_label.xy', load_combination_dark_rows(self.database, run_id='other')[0])

    def test_widget_dark_label_survives_setup_reload_and_export(self):
        plan = {'hardware': {'lockin': {'lockin_xy': {'channel_label': 'contact 3-4'}}}}
        with CombinationStore(self.database) as store, store.connection:
            store.connection.execute("UPDATE combination_runs SET plan_json=? WHERE run_id='selected'", (json.dumps(plan),))
        before = self.database.read_bytes()
        with redirect_stdout(io.StringIO()):
            dashboard = UnifiedPlotDashboard(self.directory)
            dashboard.add_plot('curve', {'source_paths': [str(self.database)],
                'run_ids': {str(self.database): ['selected']}, 'x': 'elapsed_s', 'y': dark.DARK})
            dashboard.configuration_path.value = str(self.directory / 'labels-setup.json')
            dashboard._save_setup()
            dashboard._load_setup()
            card = dashboard.cards[0]
            self.assertEqual(card['y'].value, dark.DARK)
            self.assertIn('XY/contact 3-4', dict((value, label) for label, value in card['y'].options)[dark.DARK])
            dashboard._render_card(card)
            self.assertIsNotNone(card['rendered'])
            destination = export_plot_bundle(self.directory / 'export', [card['rendered']])
        with (destination / 'selected_samples.csv').open(encoding='utf-8') as stream:
            rows = list(csv.DictReader(stream))
        self.assertTrue(rows)
        self.assertTrue(all(row['channel_label.xy'] == 'contact 3-4' for row in rows))
        self.assertEqual(self.database.read_bytes(), before)


class PlotLabels(unittest.TestCase):
    def setUp(self):
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        self.addCleanup(lambda: plt.close('all'))

    def labelled_row(self, label='端口3-4', run='test'):
        row = observation()
        row['run_id'] = run
        row['status.lockin']['channel_labels'] = {'xy': label}
        return row

    def test_options_axes_dark_delta_and_keys(self):
        row = self.labelled_row()
        for key in (Y, 'dark.lockin_xy_h2_x_v', 'measured.lockin_xy_h2_delta_x_v'):
            row[key] = 1.
            self.assertIn('XY/端口3-4', column_label(key, [row]))
        self.assertEqual({key for _, key in numeric_columns([row])},
                         {key for _, key in numeric_columns([observation()])}
                         | {'dark.lockin_xy_h2_x_v', 'measured.lockin_xy_h2_delta_x_v'})
        fig, _ = render_plot([row], dict(x=X, y=Y))
        self.assertIn('XY/端口3-4', fig.axes[0].get_ylabel())
        self.assertEqual(column_label(Y), 'measured Lock-in XY h2 R (V RMS)')

    def test_multi_run_labels_and_missing_labels_are_not_misassigned(self):
        rows = [self.labelled_row('A', 'one'), self.labelled_row('B', 'two'),
                {**observation(), 'run_id': 'legacy'}]
        before = deepcopy(rows)
        label = column_label(Y, rows)
        self.assertIn('XY/A', label)
        self.assertIn('XY/B', label)
        fig, _ = render_plot(rows, dict(x=X, y=Y, statistics='mean_sd'))
        legends = fig.axes[0].get_legend_handles_labels()[1]
        self.assertTrue(any('one' in v and 'XY/A' in v for v in legends), legends)
        self.assertTrue(any('two' in v and 'XY/B' in v for v in legends), legends)
        self.assertTrue(any('legacy' in v and 'XY/A' not in v and 'XY/B' not in v for v in legends), legends)
        self.assertEqual(rows, before)

    def test_three_panel_labels_and_chinese_export_fonts(self):
        import matplotlib as mpl
        from matplotlib import font_manager
        row = self.labelled_row()
        row.pop('status.lockin')  # Unknown remains unknown; labels are only metadata.
        row.update({'channel_label.xx': '端口1-2', 'channel_label.xy': '端口3-4',
                    'channel_label.aux': 'a axis'})
        for role in ('xx', 'xy', 'aux'):
            row[f'measured.lockin_{role}_h1_amplitude_v'] = 1.
        fonts_before = list(mpl.rcParams['font.family'])
        fig, data = render_plot([row], dict(x=X, mode='three_channels'))
        for axis, name in zip(fig.axes, ('XX/端口1-2', 'XY/端口3-4', 'AUX/a axis')):
            self.assertIn(name, axis.get_title())
        self.assertEqual(mpl.rcParams['font.family'], fonts_before)
        available = {font.name for font in font_manager.fontManager.ttflist}
        with tempfile.TemporaryDirectory() as directory, warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            fig.savefig(Path(directory) / 'labels.png')
            self.assertTrue((Path(directory) / 'labels.png').is_file())
        if available & {'Microsoft YaHei', 'SimHei', 'Noto Sans CJK SC', 'PingFang SC'}:
            self.assertFalse([str(w.message) for w in caught if 'Glyph' in str(w.message)])
        self.assertEqual(data['report']['plotted_row_count'], 3)


if __name__ == '__main__':
    unittest.main()
