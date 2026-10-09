"""Read-only run selection and explicit-refresh loading regressions."""
from contextlib import closing, redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import attodry_control.combination_analysis as analysis
import attodry_control.unified_plotting as plotting
from attodry_control.combination_scan import CombinationPlan, run_simulated_combination
from attodry_control.combination_store import CombinationStore
from tests.test_combination import axes


class PlotRunLoadingTests(unittest.TestCase):
    def setUp(self):
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        self.addCleanup(lambda: plt.close('all'))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.path = self.directory / 'runs.sqlite'
        plan = CombinationPlan((axes()['smu'], axes()['lockin']))
        with CombinationStore(self.path) as store:
            for run in ('first', 'second'):
                run_simulated_combination(plan, store, run)

    def dashboard(self):
        with redirect_stdout(io.StringIO()):
            d = plotting.UnifiedPlotDashboard(self.directory)
            d.add_plot('curve', {'source_paths': [str(self.path)]})
        return d, d.cards[0]

    def choose(self, card, run):
        card['runs'].value = tuple(value for _, value in card['runs'].options
                                  if json.loads(value)['run_id'] == run)

    def test_selecting_database_lists_runs_without_loading_samples(self):
        with patch.object(plotting, 'load_plot_sources', wraps=plotting.load_plot_sources) as load:
            d, card = self.dashboard()
            self.assertEqual(load.call_count, 0)
            self.assertEqual(len(card['runs'].options), 2)
            self.assertEqual(d._selected_rows(card), ())
            self.assertEqual(card['x'].options, ())

    def test_selected_run_only_and_cache_stays_until_manual_reload(self):
        d, card = self.dashboard()
        with redirect_stdout(io.StringIO()), patch.object(
                plotting, 'load_plot_sources', wraps=plotting.load_plot_sources) as load:
            self.choose(card, 'first')
            self.assertEqual(load.call_count, 0)
            d._load_selected_runs(card)
            rows = d._selected_rows(card)
            self.assertEqual({r['run_id'] for r in rows}, {'first'})
            self.assertEqual(len(rows), 6)
            with patch.object(plotting, '_source_signature', return_value=('changed WAL',)):
                d._update_card_options(card)
                self.assertEqual(d._selected_rows(card), rows)
                self.assertEqual(load.call_count, 1)
            d._load_selected_runs(card)
            self.assertEqual(load.call_count, 2)

    def test_cleanup_parsed_once_and_unselected_payload_never_decoded(self):
        with closing(sqlite3.connect(self.path)) as db:
            cleanup = json.dumps({'clean': True, 'transcript': 'x' * 100000})
            db.execute('UPDATE combination_runs SET cleanup_json=? WHERE run_id=?', (cleanup, 'first'))
            db.execute("UPDATE combination_samples SET payload_json='invalid JSON' WHERE run_id='second'")
            db.commit()
        original = analysis.json.loads
        with patch.object(analysis.json, 'loads', wraps=original) as decode:
            rows = analysis.load_combination_rows(self.path, run_id='first')
        self.assertEqual(len(rows), 6)
        self.assertEqual(sum(c.args[0] == cleanup for c in decode.call_args_list), 1)

    def test_run_catalog_does_not_parse_sample_or_cleanup_json(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE combination_runs SET cleanup_json='invalid JSON'")
            db.execute("UPDATE combination_samples SET payload_json='invalid JSON'")
            db.commit()
        runs = analysis.list_combination_runs(self.path)
        self.assertEqual({r['run_id'] for r in runs}, {'first', 'second'})

    def test_setup_restores_explicit_run_selection_and_snapshot(self):
        d, card = self.dashboard()
        with redirect_stdout(io.StringIO()):
            self.choose(card, 'second')
            d._load_selected_runs(card)
            card['x'].value = 'measured.smu_bias_voltage_v'
            card['y'].value = 'measured.smu_bias_current_a'
            d.configuration_path.value = str(self.directory / 'setup.json')
            d._save_setup()
            d._load_setup()
        loaded = d.cards[0]
        self.assertEqual({r['run_id'] for r in d._selected_rows(loaded)}, {'second'})
        spec = d._card_spec(loaded)
        self.assertEqual(spec['run_ids'][str(self.path)], ['second'])
        self.assertEqual(spec['data_snapshots'][0]['row_count'], 6)

    def test_active_run_requires_audit_and_refresh_to_get_new_samples(self):
        with closing(sqlite3.connect(self.path)) as db:
            db.execute("UPDATE combination_runs SET status='active',cleanup_json=NULL WHERE run_id='first'")
            db.commit()
        d, card = self.dashboard()
        with redirect_stdout(io.StringIO()):
            self.choose(card, 'first')
            d._load_selected_runs(card)
            self.assertEqual(d._selected_rows(card), ())
            d.include_audit.value = True
            self.assertEqual(d._selected_rows(card), ())
            d._load_selected_runs(card)
        self.assertEqual(len(d._selected_rows(card)), 6)
        self.assertTrue(all(r['accepted'] is False for r in d._selected_rows(card)))

    def test_failed_refresh_clears_old_data_and_restores_load_button(self):
        d, card = self.dashboard()
        with redirect_stdout(io.StringIO()):
            self.choose(card, 'first')
            d._load_selected_runs(card)
            with patch.object(plotting, 'load_plot_sources', side_effect=ValueError('broken read')):
                d._load_selected_runs(card)
        self.assertEqual(d._selected_rows(card), ())
        self.assertFalse(card['load_runs'].disabled)
        self.assertIn('Load failed: broken read', card['load_status'].value)

    def test_sqlite_export_uses_loaded_snapshot_even_after_file_changes(self):
        d, card = self.dashboard()
        with redirect_stdout(io.StringIO()):
            self.choose(card, 'first')
            d._load_selected_runs(card)
            card['x'].value = 'measured.smu_bias_voltage_v'
            card['y'].value = 'measured.smu_bias_current_a'
            card['group'].value = 'requested.lockin_excitation_v_rms'
            d._render_card(card)
            self.assertIsNotNone(card['rendered'])
            with patch.object(plotting, '_source_signature', return_value=('new WAL',)), patch.object(
                    plotting, 'export_plot_bundle', return_value=self.directory / 'export') as export:
                d._export()
            export.assert_called_once()
            self.assertEqual(card['rendered']['spec']['data_snapshots'][0]['row_count'], 6)

    def test_missing_saved_run_keeps_current_card(self):
        d, card = self.dashboard()
        setup = {'schema': 'unified-plot-setup-v1', 'data_directory': str(self.directory),
                 'plots': [{'mode': 'curve', 'source_paths': [str(self.path)],
                            'run_ids': {str(self.path): ['missing']}}]}
        path = self.directory / 'missing.json'
        path.write_text(json.dumps(setup), encoding='utf-8')
        d.configuration_path.value = str(path)
        with redirect_stdout(io.StringIO()):
            d._load_setup()
        self.assertIs(d.cards[0], card)
        self.assertIn('run IDs are missing', d.status.value)


if __name__ == '__main__':
    unittest.main()
