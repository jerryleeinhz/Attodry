"""Policy exceptions retain raw faults without bypassing other protections."""
import json
import re
import unittest

from attodry_control.config import ConfigError
from attodry_control.commissioning_analysis import load_sweep_samples
from attodry_control.combination_analysis import load_combination_rows
from attodry_control.lockin_overload import OverloadPolicy
from tests import test_lockin_harmonics as fixtures
from tests.test_lockin_role_harmonics import select_roles


def configure(text, mode="record_continue", xx="1", xy="2"):
    return select_roles(text, xx, xy).replace(
        "[lockin_sweep]", f'[lockin_sweep]\noverload_policy = "{mode}"')


def inject(xx, xy, role="xx", raw=1, *, transition=False, always=False):
    resource = xx if role == "xx" else xy
    original = resource.query
    def query(command):
        active = (always or (xy.responses["HARM?"].strip() == "2" if transition
                             else float(xx.responses["SLVL?"]) > .004))
        if command == "LIAS?" and active:
            return str(raw)
        return original(command)
    resource.query = query


class ConfigTests(unittest.TestCase):
    simulation_text = fixtures.HarmonicConfigTests.simulation_text
    load_text = fixtures.HarmonicConfigTests.load_text

    def test_default_and_explicit_modes(self):
        self.assertEqual(self.load_text(self.simulation_text()).lockin_sweep.overload_policy, "abort")
        for mode in ("abort", "continue_unselected", "record_continue"):
            self.assertEqual(self.load_text(configure(self.simulation_text(), mode)).lockin_sweep.overload_policy, mode)
        for mode in ("ignore", "", "True"):
            with self.assertRaises(ConfigError):
                self.load_text(configure(self.simulation_text(), mode))

    def test_unknown_faults_are_never_swallowed(self):
        policy = OverloadPolicy("record_continue")
        hard = ["lockin_xx instrument error is overload", "unknown overload",
                "lockin_xy reference is unlocked", "lockin_xx frequency range changed unexpectedly"]
        self.assertEqual(policy.partition(hard)[0], hard)

    def test_temperature_summary_and_file_monitor_warn_on_completion(self):
        from attodry_control.lockin_test import _annotate_sweep_quality
        from attodry_control.lockin_progress_monitor import LockinProgressView
        sample = {"sampling_policy": "independent_roles", "selected_roles": ["xy"],
                  "lockin_xx": {"lia_status": {}},
                  "lockin_xy": {"lia_status": {"input_or_reserve_overload": True}},
                  "continued_overload_problems": ["lockin_xy input/reserve overload"]}
        summary = {"completed": True, "outcome": "completed", "temperature_conditions": [
            {"excitation": {"points": [{"samples": [sample]}]}}]}
        _annotate_sweep_quality(summary)
        self.assertEqual(summary["overload_summary"]["continued_overload_pairs"], 1)
        view = LockinProgressView()
        view.update({"event": "scan_finished", **summary})
        self.assertIn("overload points", view.format())


class SweepTests(unittest.TestCase):
    setUp = fixtures.HarmonicSweepTests.setUp
    _hardware_config = fixtures.HarmonicSweepTests._hardware_config
    execute = fixtures.HarmonicSweepTests.execute

    def test_record_continue_all_sweeps_and_filter_flags(self):
        for command in ("sweep-excitation", "sweep-frequency", "sweep-frequency-excitation"):
            for role, raw in (("xx", 1), ("xy", 2)):
                with self.subTest(command=command, role=role):
                    code, result, xx, xy, _ = self.execute(command, extra="", config_edit=lambda s: re.sub(r"(?m)^frequency_source_voltage_v_rms = .*",
                            "frequency_source_voltage_v_rms = 0.008", configure(s)),
                        mutate=lambda x, y: inject(x, y, role, raw))
                    self.assertEqual(code, 0, result.get("error"))
                    self.assertTrue(result["cleanup"]["verified"])
                    self.assertEqual(result["data_quality"], "overload_recorded")
                    self.assertIn("overload points", result["completion_message"])
                    formal = [s for p in result["points"] for s in p["samples"]]
                    bad = [s for s in formal if s["lockin_" + role]["lia_status"]["raw"] == raw]
                    self.assertTrue(bad)
                    self.assertTrue(all(s["blocking_problems"] == [] for s in bad))
                    self.assertTrue(all(s["valid_for_analysis_by_role"]["lockin_" + role] is False for s in bad))
                    self.assertNotIn("pre_abort_h1_diagnostics", json.dumps(result))

    def test_unselected_xx_continues_but_selected_xx_does_not(self):
        for selected in ("", "1"):
            code, result, _, _, _ = self.execute("sweep-excitation", extra="",
                config_edit=lambda s: configure(s, "continue_unselected", selected), mutate=inject)
            self.assertEqual(code == 0, selected == "", result)
            self.assertTrue(result["cleanup"]["verified"])

    def test_unselected_mode_does_not_ignore_selected_xy(self):
        code, result, _, _, _ = self.execute("sweep-excitation", extra="",
            config_edit=lambda s: configure(s, "continue_unselected", ""),
            mutate=lambda x, y: inject(x, y, "xy"))
        self.assertNotEqual(code, 0)
        self.assertIn("input/reserve overload", result["error"])

    def test_mixed_overload_and_unlock_still_aborts(self):
        code, result, _, _, _ = self.execute("sweep-excitation", extra="", config_edit=configure,
            mutate=lambda x, y: inject(x, y, "xy", 9))
        self.assertNotEqual(code, 0)
        self.assertIn("unlocked", result["error"])
        self.assertTrue(result["cleanup"]["verified"])

    def test_overload_transition_continues_without_abort_h1_switch(self):
        code, result, xx, xy, _ = self.execute("sweep-excitation", config_edit=configure,
            mutate=lambda x, y: inject(x, y, "xy", transition=True))
        self.assertEqual(code, 0, result.get("error"))
        self.assertEqual(xy.writes.count("HARM 2"), 1)
        self.assertEqual(xy.writes.count("HARM 1"), 1)  # Cleanup only.
        self.assertTrue(result["cleanup"]["verified"])

    def test_segment_range_transition_obeys_policy(self):
        def edit(s):
            return configure(s).replace('min = 0.004, max = 0.400,',
                'min = 0.004, max = 0.400, xx_full_scale_v = 0.010,')
        code, result, _, _, _ = self.execute("sweep-excitation", extra="", config_edit=edit, mutate=inject)
        self.assertEqual(code, 0, result.get("error"))

    def test_auto_never_changes_gain_from_saturated_selected_probe(self):
        code, result, _, _, _ = self.execute("sweep-excitation", extra=fixtures.AUTO,
            config_edit=configure, mutate=lambda x, y: inject(x, y, "xy"))
        self.assertEqual(code, 0, result.get("error"))
        auto = result["points"][-1]["harmonic_autorange"][0]["autorange"]
        self.assertIn("diagnostic_skip", auto)
        self.assertEqual(auto["decisions"], [])

    def test_default_analysis_retains_clean_xy_excludes_overloaded_xx(self):
        code, result, _, _, path = self.execute("sweep-excitation", extra="", config_edit=configure, mutate=inject)
        self.assertEqual(code, 0)
        directory = path.parent / f"run_data/lockin_commissioning_{path.stem}"
        source = next(directory.glob("*_excitation_completed.json"))
        all_rows = load_sweep_samples(source)
        clean = load_sweep_samples(source, sample_statuses=("clean",))
        self.assertTrue(any(r.role == "xx" and "overload" in r.statuses for r in all_rows))
        self.assertFalse(any(r.role == "xx" and r.source_v_rms > .004 for r in clean))
        self.assertTrue(any(r.role == "xy" and r.source_v_rms > .004 for r in clean))

    def test_preflight_overload_remains_strict_no_writes(self):
        code, result, xx, xy, _ = self.execute("sweep-excitation", extra="", config_edit=configure,
            mutate=lambda x, y: inject(x, y, always=True))
        self.assertNotEqual(code, 0)
        self.assertFalse(xx.writes or xy.writes)

    def test_cleanup_fault_remains_failure(self):
        def mutate(xx, xy):
            original = xy.query
            active = False
            def query(command):
                nonlocal active
                active |= float(xx.responses["SLVL?"]) > .004
                if command == "LIAS?" and active:
                    return "1"
                return original(command)
            xy.query = query
        code, result, _, _, _ = self.execute("sweep-excitation", extra="", config_edit=configure, mutate=mutate)
        self.assertNotEqual(code, 0)
        self.assertFalse(result["cleanup"]["verified"])
        self.assertEqual(result["outcome"], "rejected")


class CombinationTests(unittest.TestCase):
    setUp = fixtures.HarmonicCombinationTests.setUp
    write_config = fixtures.HarmonicCombinationTests.write_config
    factory = fixtures.HarmonicCombinationTests.factory
    execute = fixtures.HarmonicCombinationTests.execute
    events = fixtures.HarmonicCombinationTests.events

    def test_invalid_selected_read_is_recorded_and_default_analysis_excludes_it(self):
        self.write_config(source=configure(self.base), extra="")
        inject(self.xx, self.xy, "xy")
        result = self.execute()
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["cleanup"]["clean"])
        self.assertIn("overload points", result["completion_message"])
        raw = load_combination_rows(self.database, audit=True)
        clean = load_combination_rows(self.database)
        self.assertEqual(len(raw), 6)
        self.assertEqual(len(clean), 3)
        self.assertTrue(all(r["actual.lockin_excitation_v_rms"] == .004 for r in clean))
        self.assertTrue(any(not r["clean"] for r in raw))

    def test_unselected_xx_fault_keeps_selected_xy_usable(self):
        self.write_config(source=configure(self.base, "continue_unselected", ""), extra="")
        inject(self.xx, self.xy)
        result = self.execute()
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(len(load_combination_rows(self.database)), 6)

    def test_other_module_fault_is_not_excused(self):
        from attodry_control.lockin_overload import reading_allows_continuation
        self.assertFalse(reading_allows_continuation({"module": "smu", "clean": False,
            "problems": ["compliance"], "overload_continuation": True}))


if __name__ == "__main__":
    unittest.main()
