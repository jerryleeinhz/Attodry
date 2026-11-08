"""Offline export naming and setup compatibility."""
from contextlib import redirect_stdout
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from attodry_control.unified_plotting import UnifiedPlotDashboard, export_plot_bundle, _export_name

class ExportNameTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_validation(self):
        for name in ("", "120K_gate_scan", "低温 栅压", "sample.1"):
            self.assertEqual(_export_name(name), name)
        for name in ("../file", "a/b", "a\\b", "a:b", "a*", "a?", 'a"', "a<", "a>", "a|", "a\n", "a.", " a", "a ", "CON", "aux.txt", "LPT1", "x"*121, None):
            with self.subTest(name=name), self.assertRaises(ValueError):
                _export_name(name)

    def test_files_optional_tables_and_manifest(self):
        def fake_export(fig, destination):
            path = Path(destination)
            path.write_text("synthetic figure", encoding="utf-8")
            return path
        for name, optional in (("", False), ("120K_gate_scan", True), ("低温 栅压.1", True)):
            items = [dict(figure=None, spec={}, data=dict(rows=[{"run_id": "run", "x": 1}],
                     summary_rows=[{"x": 1, "y": 2, "n": 1}], report={"source_paths": []},
                     pixel_cells=[{"x": 1, "y": 2, "z": 3}] if optional else [],
                     audit_rows=[{"run_id": "run"}] if optional else [])) for _ in range(2)]
            with patch("attodry_control.unified_plotting.save_publication_figure", side_effect=fake_export):
                dest = export_plot_bundle(self.root, items, export_name=name)
            prefix = name + "_" if name else ""
            manifest = json.loads((dest / (prefix + "plot_manifest.json")).read_text(encoding="utf-8"))
            self.assertEqual(manifest["export_name"], name)
            self.assertEqual(len(manifest["data_files"]), 4 if optional else 2)
            for value in manifest["data_files"].values():
                self.assertTrue((dest / value).is_file())
                self.assertTrue(value.startswith(prefix))
            for plot in manifest["plots"]:
                self.assertIn(plot["plot_id"], ("plot_01", "plot_02"))
                self.assertEqual(len(plot["figure_files"]), 3)
                self.assertTrue(all((dest / f).is_file() and f.startswith(prefix) for f in plot["figure_files"]))
            with (dest / (prefix + "selected_samples.csv")).open(encoding="utf-8") as f:
                self.assertEqual([r["plot_id"] for r in csv.DictReader(f)], ["plot_01", "plot_02"])
        self.assertEqual(len(list(self.root.iterdir())), 3)

    def test_invalid_name_creates_no_directory(self):
        with self.assertRaises(ValueError):
            export_plot_bundle(self.root / "new", [{}], export_name="../bad")
        self.assertFalse((self.root / "new").exists())

    def test_setup_and_legacy_default(self):
        with redirect_stdout(io.StringIO()):
            d = UnifiedPlotDashboard(self.root)
            d.add_plot("curve")
            d.add_plot("xy_z")
            for card, name in zip(d.cards, ("低温_gate", "map")):
                card["export_name"].value = name
            d.configuration_path.value = str(self.root / "setup.json")
            d._save_setup()
            d._load_setup()
            self.assertEqual([c["export_name"].value for c in d.cards], ["低温_gate", "map"])
            payload = d._configuration()
            self.assertNotIn("export_name", payload)
            for spec in payload["plots"]:
                del spec["export_name"]
            for legacy_name in (None, "old_prefix"):
                if legacy_name is not None:
                    payload["export_name"] = legacy_name
                (self.root / "legacy.json").write_text(json.dumps(payload), encoding="utf-8")
                d.configuration_path.value = str(self.root / "legacy.json")
                d._load_setup()
                self.assertEqual([c["export_name"].value for c in d.cards], [legacy_name or ""] * 2)

    def test_dashboard_exports_only_clicked_card(self):
        from attodry_control.unified_plotting import render_plot
        import matplotlib.pyplot as plt
        with redirect_stdout(io.StringIO()):
            d = UnifiedPlotDashboard(self.root)
            d.output_directory.value = str(self.root / "exports")
            for mode, run in (("curve", "run_a"), ("xy_z", "run_b")):
                d.add_plot(mode)
                fig, data = render_plot([{"csv.x": 1., "csv.y": 2., "run_id": run}],
                                       {"x": "csv.x", "y": "csv.y"})
                self.addCleanup(plt.close, fig)
                d.cards[-1]["rendered"] = dict(figure=fig, data=data,
                    spec={"source_signatures": {}, "run_ids": [run]})
            first, second = d.cards
            original = first["rendered"]
            first["export_name"].value = "gate"
            self.assertIs(first["rendered"], original)
            first["export_button"].click()
            dest, = (self.root / "exports").iterdir()
            manifest = json.loads((dest / "gate_plot_manifest.json").read_text())
            self.assertEqual(manifest["figure_count"], 1)
            self.assertEqual(manifest["plots"][0]["spec"]["run_ids"], ["run_a"])
            self.assertEqual(len(list(dest.glob("*.png"))), 1)
            with (dest / "gate_selected_samples.csv").open() as f:
                self.assertEqual({r["run_id"] for r in csv.DictReader(f)}, {"run_a"})
            self.assertEqual(second["export_status"].value, "")
            d._invalidate_card(second)
            with patch("attodry_control.unified_plotting.export_plot_bundle", return_value=self.root) as export:
                first["export_button"].click()
                self.assertEqual(export.call_args.args[1], [original])
                self.assertEqual(export.call_args.kwargs["export_name"], "gate")
                export.reset_mock()
                second["export_button"].click()
                export.assert_not_called()
            self.assertIn("Render this plot", second["export_status"].value)
            first["title"].value = "changed plot"
            self.assertIsNone(first["rendered"])
            with patch("attodry_control.unified_plotting.export_plot_bundle") as export:
                first["export_button"].click()
                export.assert_not_called()

    def test_real_dotted_name_exports_all_formats(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from attodry_control.unified_plotting import render_plot
        fig, data = render_plot([{"csv.x": 1., "csv.y": 2.}], {"x": "csv.x", "y": "csv.y"})
        self.addCleanup(lambda: plt.close(fig))
        dest = export_plot_bundle(self.root, [dict(figure=fig, data=data, spec={})], export_name="试验.1")
        self.assertTrue(all((dest / ("试验.1_plot_01." + ext)).is_file() for ext in ("png", "pdf", "svg")))
