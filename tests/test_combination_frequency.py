"""Real coordinator and SR830 transcripts with fake resources only."""
from contextlib import closing, redirect_stdout
import io
import json
import os
import re
import unittest
from unittest.mock import patch

from attodry_control.combination_cli import run as cli
from attodry_control.combination_hardware import load_hardware_combination, run_hardware_combination
from attodry_control.combination_launch import resolve_launch, launch_summary
from attodry_control.combination_store import open_readonly
from attodry_control.combination_analysis import load_combination_rows, select_series
from tests import test_combination_hardware as fixture


class CombinationFrequencyTests(unittest.TestCase):
    # Reuse the established hardware-isolated fixture, without inheriting its tests.
    setUp = fixture.ElectricalCombinationTests.setUp
    write_config = fixture.ElectricalCombinationTests.write_config
    factory = fixture.ElectricalCombinationTests.factory
    execute = fixture.ElectricalCombinationTests.execute
    events = fixture.ElectricalCombinationTests.events

    def configure(self, mode="frequency_excitation", frequencies=(17.777, 100),
                  order=("smu", "lockin"), extra="", source=None):
        source = re.sub(r"(?ms)^frequency_ranges = \[.*?^\]",
            "frequency_points_hz = " + json.dumps(frequencies), source or self.base)
        self.write_config(order, f'lockin_mode = "{mode}"\n' + extra, source)

    def test_grid_order_and_indices_preserved(self):
        self.configure()
        config = load_hardware_combination(self.path)
        conditions = config.plan.conditions()
        self.assertEqual(len(conditions), 12)
        self.assertEqual([(c["requested"]["lockin_frequency_hz"],
                           c["requested"]["lockin_excitation_v_rms"]) for c in conditions[:4]],
                         [(17.777, .004), (17.777, .008), (100, .004), (100, .008)])
        self.assertEqual(conditions[3]["axes"]["lockin"]["grid"]["frequency_index"], 1)
        self.assertEqual(conditions[3]["axes"]["lockin"]["grid"]["excitation_index"], 1)

    def test_outer_reset_has_minimum_bridge_and_frequency_cleanup(self):
        self.configure()
        result = self.execute()
        self.assertEqual(result["status"], "completed", result)
        freq = [w for w in self.xx.writes if w.startswith("FREQ ")]
        self.assertEqual(freq, ["FREQ 100", "FREQ 17.777", "FREQ 100",
                                "FREQ 17.777", "FREQ 100", "FREQ 17.777"])
        for i, command in enumerate(self.xx.writes):
            if command.startswith("FREQ "):
                self.assertEqual(next(w for w in reversed(self.xx.writes[:i])
                                      if w.startswith("SLVL ")), "SLVL 0.004")
        rows = load_combination_rows(self.database)
        self.assertEqual(len(rows), 12)
        self.assertEqual({r["actual.lockin_frequency_hz"] for r in rows}, {17.777, 100})
        self.assertEqual(self.frequency["hz"], 17.777)
        self.assertEqual(self.xx.responses["HARM?"].strip(), "1")
        self.assertTrue(result["cleanup"]["clean"])

    def test_frequency_only_uses_its_fixed_amplitude_and_own_harmonics(self):
        source = self.base.replace("frequency_source_voltage_v_rms = 0.004",
                                   "frequency_source_voltage_v_rms = 0.012")
        source = source.replace("frequency_xx_harmonics = [1, 2, 3]", "frequency_xx_harmonics = []")
        source = source.replace("frequency_xy_harmonics = [1, 2, 3]", "frequency_xy_harmonics = [2]")
        self.configure("frequency", source=source, order=("lockin",))
        result = self.execute()
        self.assertEqual(result["status"], "completed", result)
        rows = load_combination_rows(self.database)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["actual.lockin_excitation_v_rms"] == .012 for r in rows))
        self.assertTrue(all("measured.lockin_xy_h2_x_v" in r for r in rows))
        self.assertFalse(any("measured.lockin_xx_h1_x_v" in r for r in rows))
        self.assertFalse(any(w.startswith("HARM 2") for w in self.xx.writes))

    def test_frequency_failure_keeps_partial_records_and_restores_baseline(self):
        self.configure(order=("lockin",))
        self.xx.fail_write = "FREQ 100"
        result = self.execute()
        self.assertEqual(result["status"], "failed")
        self.assertIn("injected VISA write failure", result["error"])
        self.assertEqual(len(load_combination_rows(self.database, audit=True)), 2)
        self.assertEqual(self.frequency["hz"], 17.777)
        self.assertEqual(float(self.xx.responses["SLVL?"]), .004)
        self.assertTrue(any(k == "lockin_point_transition" and p["frequency_write_attempted"]
                            for k, p in self.events()))

    def test_bad_frequency_readback_never_restores_high_amplitude(self):
        self.configure(order=("lockin",))
        self.xx.frequency_transform = lambda f: 101 if f == 100 else f
        result = self.execute()
        self.assertEqual(result["status"], "failed")
        i = self.xx.writes.index("FREQ 100")
        self.assertNotIn("SLVL 0.008", self.xx.writes[i:])
        self.assertEqual(self.frequency["hz"], 17.777)

    def test_partial_unsupported_harmonics_are_explicit_and_detector_parks(self):
        source = self.base.replace("combined_xx_harmonics = [1]", "combined_xx_harmonics = []")
        source = source.replace("combined_xy_harmonics = [1, 2, 3]", "combined_xy_harmonics = [1, 2]")
        self.configure(frequencies=(30000, 60000), source=source, order=("lockin",))
        result = self.execute()
        self.assertEqual(result["status"], "completed", result)
        rows = load_combination_rows(self.database)
        high = [r for r in rows if r["actual.lockin_frequency_hz"] == 60000]
        self.assertEqual(len(high), 2)
        self.assertTrue(all("measured.lockin_xy_h1_x_v" in r for r in high))
        self.assertFalse(any("measured.lockin_xy_h2_x_v" in r for r in high))
        transitions = [p for k, p in self.events() if k == "lockin_point_qualification"
                       and p["target_frequency_hz"] == 60000]
        self.assertTrue(all(p["skipped_harmonics"][0]["harmonic"] == 2 for p in transitions))
        commands = [(p["role"], p["method"], p["arguments"]) for k, p in self.events()
                    if k == "lockin_command_attempt"]
        i = commands.index(("lockin_xx", "set_internal_reference_frequency", [60000]))
        self.assertEqual(next(p for p in reversed(commands[:i]) if p[0] == "lockin_xy"
                              and p[1] == "set_harmonic"), ("lockin_xy", "set_harmonic", [1]))

    def test_all_harmonics_unsupported_rejects_before_io(self):
        source = self.base.replace("combined_xx_harmonics = [1]", "combined_xx_harmonics = []")
        source = source.replace("combined_xy_harmonics = [1, 2, 3]", "combined_xy_harmonics = [2]")
        self.configure(frequencies=(30000, 60000), source=source)
        with self.assertRaisesRegex(ValueError, "No requested harmonic"):
            load_hardware_combination(self.path)
        self.assertEqual(self.manager.opened, [])
        self.assertFalse(self.database.exists())

    def test_strict_unsupported_policy_rejects_grid(self):
        self.configure(frequencies=(30000, 60000), source=self.base.replace(
            "skip_unsupported_harmonics = true", "skip_unsupported_harmonics = false"))
        with self.assertRaisesRegex(ValueError, "harmonic detection frequency"):
            load_hardware_combination(self.path)

    def test_excessive_grid_rejects_before_materialization_or_io(self):
        source = re.sub(r"(?ms)^frequency_ranges = \[.*?^\]", '''frequency_ranges = [
  { min = 17.777, max = 100, scale = "linear", points = 1001 },
]''', self.base)
        source = source.replace("excitation_points_v_rms = [0.004, 0.008]", '''excitation_ranges = [
  { min = 0.004, max = 0.008, scale = "linear", points = 101 },
]''')
        self.write_config(("lockin",), 'lockin_mode = "frequency_excitation"\n', source)
        with self.assertRaisesRegex(ValueError, "exceeds 100000 conditions"):
            load_hardware_combination(self.path)
        self.assertEqual(self.manager.opened, [])
        self.assertFalse(self.database.exists())

    def test_frequency_and_excitation_segment_override_precedence(self):
        source = re.sub(r"(?ms)^frequency_ranges = \[.*?^\]", '''frequency_ranges = [
  { min = 17.777, max = 100, scale = "linear", points = 2, xx_full_scale_v = 0.02 },
]''', self.base)
        source = source.replace("excitation_points_v_rms = [0.004, 0.008]", '''excitation_ranges = [
  { min = 0.004, max = 0.008, scale = "linear", points = 2, xx_full_scale_v = 0.05 },
]''')
        self.write_config(("lockin",), 'lockin_mode = "frequency_excitation"\n', source)
        result = self.execute()
        self.assertEqual(result["status"], "completed", result)
        records = [p for k, p in self.events() if k == "lockin_point_qualification"]
        self.assertTrue(all(p["range_spec"]["xx_full_scale_v"] == .05 for p in records))
        self.assertTrue(all(p["frequency_segment"] == 0 and p["excitation_segment"] == 0
                            for p in records))

    def test_frequency_groups_do_not_pool_across_frequency_or_outer_points(self):
        self.configure()
        self.assertEqual(self.execute()["status"], "completed")
        rows = load_combination_rows(self.database)
        groups = select_series(rows, x="measured.smu_bias_voltage_v",
            y="measured.lockin_xy_h2_x_v",
            group_by=("requested.lockin_frequency_hz", "requested.lockin_excitation_v_rms"))
        self.assertEqual(len(groups), 4)
        self.assertEqual([len(g.rows) for g in groups], [3, 3, 3, 3])

    def test_unified_frequency_curve_and_f_u_map_use_archived_coordinates(self):
        from attodry_control.unified_plotting import render_plot
        import matplotlib.pyplot as plt
        self.configure()
        self.assertEqual(self.execute()["status"], "completed")
        rows = load_combination_rows(self.database)
        y = "measured.lockin_xy_h2_amplitude_v"
        filters = {"requested.smu_bias_v": [0.0]}
        spec = {"mode": "curve", "x": "actual.lockin_frequency_hz", "y": y,
                "group_by": "requested.lockin_excitation_v_rms", "filters": filters,
                "statistics": "raw", "x_scale": "log"}
        figure, data = render_plot(rows, spec)
        self.addCleanup(plt.close, figure)
        self.assertEqual(data["report"]["displayed_point_count"], 4)
        self.assertEqual({r["actual.lockin_frequency_hz"] for r in data["rows"]}, {17.777, 100})
        with self.assertRaisesRegex(ValueError, "Unresolved varying"):
            render_plot(rows, {**spec, "group_by": None})
        map_spec = {"mode": "xy_z", "x": "actual.lockin_frequency_hz",
                    "y": "actual.lockin_excitation_v_rms", "z": y, "filters": filters}
        figure, data = render_plot(rows, map_spec)
        self.addCleanup(plt.close, figure)
        self.assertEqual(data["report"]["plotted_row_count"], 4)

    def test_invalid_mode_and_unknown_keys_rejected(self):
        for extra in ('lockin_mode = "typo"\n', 'lockin_modes = "frequency"\n', 'run_id = ""\n'):
            self.write_config(extra=extra)
            with self.assertRaises(ValueError):
                load_hardware_combination(self.path)

    def test_launch_defaults_and_cli_override(self):
        self.configure(extra='run_id = "manual-id"\n')
        source = self.path.read_text(encoding="utf-8").replace('../run_data/combination/scan.sqlite', 'data/scan.sqlite')
        self.path.write_text(source, encoding="utf-8")
        config = load_hardware_combination(self.path)
        database, run_id = resolve_launch(config)
        self.assertEqual(database, self.directory / "data/scan.sqlite")
        self.assertEqual(run_id, "manual-id")
        self.assertEqual(resolve_launch(config, self.database, "override"), (self.database, "override"))
        self.assertRegex(resolve_launch(config, self.database, "auto")[1], r"^\d{8}T\d{12}Z_")
        summary = launch_summary(config, database, run_id)
        self.assertEqual(summary["total_conditions"], 12)
        self.assertEqual(summary["lockin"]["internal_order"], ["frequency", "excitation"])
        self.assertEqual(summary["cleanup"]["lockin"],
                         "4mV_h1_restore_baseline_frequency_ranges_reserve")

    def test_cancel_or_eof_creates_no_database_and_no_connections(self):
        self.configure()
        for response in ("", "no", EOFError()):
            mock = {"side_effect": response} if isinstance(response, Exception) else {"return_value": response}
            with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", **mock), \
                    redirect_stdout(io.StringIO()), patch("attodry_control.combination_hardware.run_hardware_combination") as runner:
                self.assertEqual(cli(["run", "--config", str(self.path), "--database", str(self.database)]), 0)
                runner.assert_not_called()
            self.assertFalse(self.database.exists())

    def test_noninteractive_missing_authorization_no_database(self):
        self.configure()
        with patch("sys.stdin.isatty", return_value=False), redirect_stdout(io.StringIO()), \
                self.assertRaisesRegex(ValueError, "confirm-xy-sine-disconnected"):
            cli(["run", "--config", str(self.path), "--database", str(self.database), "--authorize-combination"])
        self.assertFalse(self.database.exists())

    def test_interactive_and_explicit_launch_pass_resolved_audit_to_runner(self):
        self.configure(extra='run_id = "from-toml"\n')
        self.path.write_text(self.path.read_text(encoding="utf-8").replace('../run_data/combination/scan.sqlite', str(self.database).replace('\\', '/')), encoding="utf-8")
        for explicit in (False, True):
            flags = ["--authorize-combination", "--confirm-xy-sine-disconnected"] if explicit else []
            with patch("sys.stdin.isatty", return_value=not explicit), patch("builtins.input", return_value="RUN") as prompt, \
                    redirect_stdout(io.StringIO()), patch("attodry_control.combination_hardware.run_hardware_combination",
                    return_value={"status": "completed"}) as runner:
                self.assertEqual(cli(["run", "--config", str(self.path)] + flags), 0)
                config, _, run_id = runner.call_args.args
                self.assertEqual(run_id, "from-toml")
                self.assertEqual(config.snapshot["launch"]["authorization_method"],
                                 "explicit_flags" if explicit else "interactive_RUN")
                self.assertEqual(prompt.called, not explicit)

    def test_duplicate_run_fails_before_confirmation_or_hardware(self):
        self.configure()
        self.execute("duplicate")
        with patch("builtins.input") as prompt, patch("attodry_control.combination_hardware.run_hardware_combination") as runner, \
                self.assertRaisesRegex(ValueError, "already exists"):
            cli(["run", "--config", str(self.path), "--database", str(self.database), "--run-id", "duplicate"])
        prompt.assert_not_called()
        runner.assert_not_called()

    def test_config_change_during_confirmation_refuses_launch(self):
        self.configure()
        def change(_):
            self.path.write_text(self.path.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")
            return "RUN"
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", side_effect=change), \
                redirect_stdout(io.StringIO()), self.assertRaisesRegex(ValueError, "changed during"):
            cli(["run", "--config", str(self.path), "--database", str(self.database)])
        self.assertFalse(self.database.exists())

    def test_plain_run_reads_default_toml_and_persists_confirmation(self):
        self.configure(order=("lockin",), extra='run_id = "short-command"\n')
        directory = self.directory / "config"
        directory.mkdir()
        (directory / "hardware.local.toml").write_text(self.path.read_text(encoding="utf-8").replace(
            "../run_data/combination/scan.sqlite", "../scan.sqlite"), encoding="utf-8")
        (directory / "lockin_safety.toml").write_bytes((self.directory / "lockin_safety.toml").read_bytes())
        previous = os.getcwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(self.directory)
        def execute(config, store, run_id, **kwargs):
            return run_hardware_combination(config, store, run_id,
                manager_factory=lambda: self.manager, **kwargs)
        with patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="RUN"), \
                redirect_stdout(io.StringIO()), patch("attodry_control.combination_hardware.run_hardware_combination", side_effect=execute):
            self.assertEqual(cli(["run"]), 0)
        with closing(open_readonly(self.database)) as connection:
            row = connection.execute("SELECT plan_json,status FROM combination_runs WHERE run_id='short-command'").fetchone()
        self.assertEqual(row["status"], "completed")
        launch = json.loads(row["plan_json"])["launch"]
        self.assertEqual(launch["authorization_method"], "interactive_RUN")
        self.assertEqual(launch["summary"]["total_conditions"], 4)
