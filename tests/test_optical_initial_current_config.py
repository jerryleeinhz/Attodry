"""Offline contracts for explicit optical-current starting strategies."""
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict, replace
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

from attodry_control.nkt_config import NktError, load_nkt_config
from attodry_control.optical_config import load_scan_config


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "config/optical_source_current_simulation.toml"


class InitialCurrentConfigTests(unittest.TestCase):
    def content(self, mode=None, levels=None, *, rows=1, scalar=False, mapping="cartesian"):
        text = EXAMPLE.read_text(encoding="utf-8")
        points = ",\n".join(
            "  {source_level_pct = 8.0, wavelength_nm = " + str(633 + 2 * index) +
            ", bandwidth_nm = 10.0}" for index in range(rows))
        text = re.sub(r"points = \[.*?\n\]", "points = [\n" + points + "\n]", text,
                      count=1, flags=re.DOTALL)
        options = f'target_mapping = "{mapping}"\n'
        if mode is not None:
            options += f'initial_current_mode = "{mode}"\n'
        if levels is not None:
            options += "initial_source_levels_pct = " + levels + "\n"
        text = text.replace("[power_feedback]\n", "[power_feedback]\n" + options)
        if scalar:
            text = text.replace("target_powers_w = [0.000005, 0.0000225, 0.00004]",
                                "target_power_w = 0.000005")
        return text

    def load(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fake.toml"
            path.write_text(text, encoding="utf-8")
            nkt = load_nkt_config(path)
            scan, pem, pm = load_scan_config(path, nkt)
            return nkt, scan, pem, pm

    def feedback(self):
        return self.load(self.content())[1].feedback

    def test_omitted_options_preserve_point_defaults_and_scalar_resolution(self):
        feedback = self.feedback()
        self.assertEqual(feedback.initial_current_mode, "point")
        self.assertIsNone(feedback.initial_source_levels_pct)
        self.assertEqual([feedback.initial_source_level_pct(index, 3, 8) for index in range(3)],
                         [8, 8, 8])
        scalar = replace(feedback, target_powers_w=None, target_power_w=5e-6,
                         target_mapping="paired")
        scalar.validate()
        self.assertEqual(scalar.for_point(2, 3).initial_current_mode, "point")
        self.assertEqual(scalar.for_point(2, 3).target_power_w, 5e-6)

    def test_per_target_load_is_immutable_and_resolves_every_column(self):
        nkt, scan, _, _ = self.load(self.content("per_target", "[10, 20.1, 30.2]"))
        feedback = scan.feedback
        self.assertEqual(feedback.initial_source_levels_pct, (10, 20.1, 30.2))
        self.assertEqual([feedback.initial_source_level_pct(index, 3, 8) for index in range(3)],
                         [10, 20.1, 30.2])
        for index, target in enumerate(feedback.target_powers_w):
            resolved = feedback.for_point(index, 3)
            self.assertEqual(resolved.target_power_w, target)
            self.assertEqual(resolved.initial_current_mode, "point")
            self.assertIsNone(resolved.initial_source_levels_pct)
            resolved.validate()
        self.assertEqual(feedback.initial_current_mode, "per_target")
        self.assertEqual(feedback.initial_source_levels_pct, (10, 20.1, 30.2))
        self.assertEqual(len(nkt.points), 1)
        serialized = json.loads(json.dumps(asdict(scan), allow_nan=False))
        self.assertEqual(serialized["feedback"]["initial_source_levels_pct"], [10, 20.1, 30.2])

    def test_grid_has_exact_row_major_optical_row_then_target_order(self):
        _, scan, _, _ = self.load(self.content("grid", "[[10, 20, 30], [11, 21, 31]]", rows=2))
        feedback = scan.feedback
        self.assertEqual(feedback.initial_source_levels_pct, ((10, 20, 30), (11, 21, 31)))
        self.assertEqual([feedback.initial_source_level_pct(index, 6, 8) for index in range(6)],
                         [10, 20, 30, 11, 21, 31])
        self.assertEqual([feedback.for_point(index, 6).target_power_w for index in range(6)],
                         list(feedback.target_powers_w) * 2)
        self.assertIsInstance(feedback.initial_source_levels_pct[0], tuple)

    def test_previous_returns_row_default_without_implicit_runtime_history(self):
        _, scan, _, _ = self.load(self.content("previous", rows=2))
        feedback = scan.feedback
        self.assertEqual(feedback.initial_current_mode, "previous")
        self.assertIsNone(feedback.initial_source_levels_pct)
        self.assertEqual(feedback.initial_source_level_pct(1, 6, 9), 9)
        self.assertEqual(feedback.initial_source_level_pct(4, 6, 12), 12)
        self.assertEqual(feedback.for_point(4, 6).initial_current_mode, "point")

    def test_scalar_targets_support_one_column_per_target_and_grid(self):
        _, scan, _, _ = self.load(self.content("per_target", "[10.1]", scalar=True))
        self.assertEqual(scan.feedback.initial_source_level_pct(0, 1, 8), 10.1)
        _, scan, _, _ = self.load(self.content("grid", "[[10], [11]]", rows=2, scalar=True))
        self.assertEqual(scan.feedback.initial_source_level_pct(1, 2, 8), 11)

    def test_arrays_for_point_previous_and_noncartesian_modes_reject(self):
        for mode in ("point", "previous"):
            with self.subTest(mode=mode), self.assertRaises(NktError):
                self.load(self.content(mode, "[10, 20, 30]"))
        for mode, levels in (("previous", None), ("per_target", "[10, 20, 30]"),
                             ("grid", "[[10, 20, 30]]")):
            with self.subTest(mode=mode), self.assertRaises(NktError):
                self.load(self.content(mode, levels, mapping="paired", rows=3))
        for mode in ("unknown", "", "PER_TARGET"):
            with self.subTest(mode=mode), self.assertRaises(NktError):
                self.load(self.content(mode))

    def test_flat_and_grid_dimensions_are_never_broadcast_or_filled(self):
        invalid = (
            self.content("per_target"),
            self.content("per_target", "[]"),
            self.content("per_target", "[10]"),
            self.content("per_target", "[10, 20, 30]", rows=2),
            self.content("per_target", "[[10, 20, 30]]"),
            self.content("grid"),
            self.content("grid", "[]"),
            self.content("grid", "[10, 20, 30]"),
            self.content("grid", "[[10, 20, 30]]", rows=2),
            self.content("grid", "[[10, 20], [11, 21, 31]]", rows=2),
            self.content("grid", "[[10, 20, 30], [11, 21, 31], [12, 22, 32]]", rows=2),
        )
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(NktError):
                self.load(text)

    def test_direct_constructor_rejects_mutable_ragged_and_wrong_typed_arrays(self):
        base = self.feedback()
        bad = (
            ("per_target", [10, 20, 30]),
            ("per_target", (10, 20)),
            ("per_target", ((10, 20, 30),)),
            ("grid", ([10, 20, 30],)),
            ("grid", ((10, 20),)),
            ("grid", (10, 20, 30)),
        )
        for mode, values in bad:
            with self.subTest(mode=mode, values=values), self.assertRaises(NktError):
                replace(base, initial_current_mode=mode, initial_source_levels_pct=values).validate()
        for mode in (True, None, 1):
            with self.subTest(mode=mode), self.assertRaises(NktError):
                replace(base, initial_current_mode=mode).validate()

    def test_every_entry_must_be_finite_nonboolean_quantized_and_within_bounds(self):
        base = self.feedback()
        for value in (True, False, float("nan"), float("inf"), -float("inf"),
                      "20", None, 7.9, 40.1, 20.05):
            # Preflight must check later rows/columns before any hardware access.
            candidate = replace(base, initial_current_mode="grid",
                                initial_source_levels_pct=((10, 20, 30), (11, 21, value)))
            with self.subTest(value=value), self.assertRaises(NktError):
                candidate.validate_initial_currents(6)
        for raw in ('10', '"10,20,30"', 'true', '[10, nan, 30]', '[10, inf, 30]',
                    '[10, true, 30]', '[10, 20.05, 30]', '[7.9, 20, 30]', '[10, 20, 40.1]'):
            with self.subTest(raw=raw), self.assertRaises(NktError):
                self.load(self.content("per_target", raw))
        for values in ((8, 20.1, 40), (8.0, 8.1, 39.9)):
            replace(base, initial_current_mode="per_target", initial_source_levels_pct=values).validate()

    def test_nkt_limit_is_checked_independently_for_all_explicit_starts(self):
        feedback = replace(self.feedback(), initial_current_mode="grid",
                           initial_source_levels_pct=((10, 20, 30), (11, 21, 31)))
        with self.assertRaises(NktError):
            feedback.validate_initial_currents(6, 30)
        feedback.validate_initial_currents(6, 31)
        with self.assertRaises(NktError):
            self.load(self.content("grid", "[[10, 20, 30]]").replace(
                "source_current_max_pct = 40.0", "source_current_max_pct = 41.0"))

    def test_index_and_expanded_counts_are_strict_and_complete(self):
        feedback = replace(self.feedback(), initial_current_mode="grid",
                           initial_source_levels_pct=((10, 20, 30), (11, 21, 31)))
        for index, count in ((-1, 6), (6, 6), (True, 6), (0.5, 6), (0, True),
                             (0, "6"), (0, 0), (0, 5), (0, 9), (0, 10001)):
            with self.subTest(index=index, count=count), self.assertRaises(NktError):
                feedback.initial_source_level_pct(index, count, 8)
        previous = replace(self.feedback(), initial_current_mode="previous")
        for value in (True, float("nan"), 7.9, 40.1, 20.05):
            with self.subTest(value=value), self.assertRaises(NktError):
                previous.initial_source_level_pct(0, 3, value)

    def test_nondefault_modes_require_source_current_actuator(self):
        base = self.feedback()
        nd = replace(base, actuator="varia_nd", source_current_min_pct=None,
                     source_current_max_pct=None, source_current_step_pct=None,
                     minimum_signal_power_w=None, nd_min_pct=0, nd_max_pct=80,
                     nd_step_pct=1, increasing_nd_increases_power=False)
        nd.validate()
        for mode, values in (("previous", None), ("per_target", (10, 20, 30)),
                             ("grid", ((10, 20, 30),))):
            with self.subTest(mode=mode), self.assertRaises(NktError):
                replace(nd, initial_current_mode=mode, initial_source_levels_pct=values).validate()

    def test_large_grid_resolution_does_not_repeat_complete_preflight(self):
        feedback = replace(self.feedback(), target_power_w=5e-6, target_powers_w=None,
                           initial_current_mode="grid", initial_source_levels_pct=((10,),) * 10000)
        feedback.validate()
        feedback.validate_initial_currents(10000, 40)
        with patch.object(type(feedback), "validate_initial_currents",
                          side_effect=AssertionError("whole-grid preflight repeated")):
            for index in (0, 5000, 9999):
                self.assertEqual(feedback.initial_source_level_pct(index, 10000, 8), 10)
                self.assertEqual(feedback.for_point(index, 10000).target_power_w, 5e-6)

    def test_direct_resolver_reports_malformed_selected_row_as_configuration_error(self):
        base = self.feedback()
        for levels in ((10, 20), ((10, 20),), ((10, 20, True),)):
            feedback = replace(base, initial_current_mode="grid", initial_source_levels_pct=levels)
            with self.subTest(levels=levels), self.assertRaises(NktError):
                feedback.initial_source_level_pct(2, 3, 8)

    def test_standalone_rejects_each_strategy_before_resource_or_output_creation(self):
        from attodry_control.optical_cli import scan_main
        for mode, levels in (("previous", None), ("per_target", "[10, 20, 30]"),
                             ("grid", "[[10, 20, 30]]")):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "fake.toml"
                path.write_text(self.content(mode, levels), encoding="utf-8")
                with patch("attodry_control.optical_cli.SimulatedNkt") as backend, \
                     patch("attodry_control.optical_cli._device") as device, \
                     patch("attodry_control.optical_cli._paths") as output, \
                     redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
                    self.assertEqual(scan_main(["simulate", "--config", str(path)]), 2)
                backend.assert_not_called()
                device.assert_not_called()
                output.assert_not_called()
                self.assertIn("standalone optical scan", errors.getvalue())
        # The strategy check remains explicit even after normalizing mapping in a direct API object.
        _, scan, _, _ = self.load(self.content("previous"))
        with self.assertRaisesRegex(NktError, "initial_current_mode"):
            replace(scan, feedback=replace(scan.feedback, target_mapping="paired")).require_standalone_scan()


if __name__ == "__main__":
    unittest.main()
