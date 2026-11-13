"""Filter stacking shares curve identity with dimension validation and export."""
import copy
import math
import unittest

from attodry_control.unified_plotting import _effective_group_keys, _sample_token, render_plot
from tests.test_excitation_analysis import CURRENT, FREQUENCY, SIGNAL, observation


class StackedFilterBackendTests(unittest.TestCase):
    def setUp(self):
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        self.addCleanup(plt.close, "all")

    def rows(self, frequencies=(150., 200., 250., 300.), currents=(1e-7, 2e-7, 3e-7)):
        return [observation(index, current, frequency) for index, (frequency, current) in enumerate(
            (frequency, current) for frequency in frequencies for current in currents)]

    def spec(self, rows, **changes):
        return dict(x=CURRENT, y=SIGNAL, curve_style="line+points",
                    filters={FREQUENCY: sorted({row[FREQUENCY] for row in rows})},
                    stack_filters=[FREQUENCY], **changes)

    def test_screenshot_four_frequencies_render_as_four_target_labelled_curves(self):
        rows = self.rows()
        original = copy.deepcopy(rows)
        figure, data = render_plot(rows, self.spec(rows))
        self.assertEqual(len(figure.axes[0].lines), 4)
        self.assertEqual([len(line.get_xdata()) for line in figure.axes[0].lines], [3] * 4)
        self.assertEqual(data["report"]["group_keys"], ["requested.excitation_frequency_hz"])
        self.assertEqual({point["group"]["requested.excitation_frequency_hz"]
                          for point in data["summary_rows"]}, {150., 200., 250., 300.})
        self.assertTrue(all("target Excitation frequency" in label
                            for label in data["report"]["legend_labels"]))
        self.assertEqual(rows, original)

    def test_filter_without_stacking_keeps_previous_unresolved_dimension_guard(self):
        rows = self.rows()
        spec = self.spec(rows)
        spec["stack_filters"] = []
        with self.assertRaisesRegex(ValueError, "Unresolved varying"):
            render_plot(rows, spec)
        spec["group_by"] = FREQUENCY
        self.assertEqual(_effective_group_keys(rows, spec), (FREQUENCY,))
        figure, _ = render_plot(rows, spec)
        self.assertEqual(len(figure.axes[0].lines), 4)

    def test_readback_noise_does_not_fragment_requested_frequency_curves(self):
        rows = self.rows(frequencies=(100., 200.))
        for index, row in enumerate(rows):
            row[FREQUENCY] += index * .001
        figure, data = render_plot(rows, self.spec(rows))
        self.assertEqual(len(figure.axes[0].lines), 2)
        self.assertEqual({row[FREQUENCY] for row in data["rows"]}, {row[FREQUENCY] for row in rows})
        spec = self.spec(rows, group_by=FREQUENCY)
        self.assertEqual(_effective_group_keys(rows, spec), ("requested.excitation_frequency_hz",))

    def test_distinct_targets_with_identical_readbacks_remain_separate(self):
        rows = self.rows(frequencies=(100., 100.000001))
        for row in rows:
            row[FREQUENCY] = 100.
        figure, data = render_plot(rows, self.spec(rows))
        self.assertEqual(len(figure.axes[0].lines), 2)
        self.assertEqual({point["group"]["requested.excitation_frequency_hz"]
                          for point in data["summary_rows"]}, {100., 100.000001})
        self.assertEqual(len(set(data["report"]["legend_labels"])), 2)

    def test_multiple_stacked_filters_use_only_recorded_condition_combinations(self):
        rows = []
        for visit, (temperature, frequency) in enumerate(((20., 100.), (20., 200.), (40., 200.))):
            for current in (1e-7, 2e-7):
                rows.append(observation(len(rows), current, frequency, temperature,
                                        temperature_index=visit))
        spec = self.spec(rows)
        spec["filters"]["actual.temperature_k"] = [20.001, 40.001]
        spec["stack_filters"].append("actual.temperature_k")
        figure, data = render_plot(rows, spec)
        self.assertEqual(len(figure.axes[0].lines), 3)
        self.assertEqual(data["report"]["group_keys"],
                         ["requested.excitation_frequency_hz", "requested.temperature_k"])
        self.assertEqual(len(data["summary_rows"]), 6)

    def test_manual_exclusion_removes_only_exact_observation_and_preserves_curve_gap(self):
        rows = self.rows(frequencies=(100., 200.))
        spec = self.spec(rows, excluded_sample_ids=[_sample_token(rows[1])])
        figure, data = render_plot(rows, spec)
        self.assertEqual(data["report"]["plotted_row_count"], 5)
        self.assertEqual(data["report"]["manual_excluded_sample_ids"], [_sample_token(rows[1])])
        self.assertEqual(sum(math.isnan(float(value)) for line in figure.axes[0].lines
                             for value in line.get_ydata()), 1)
        self.assertEqual(sum(point["n"] for point in data["summary_rows"]
                             if point["group"]["requested.excitation_frequency_hz"] == 200.), 3)

    def test_repeats_and_runs_keep_their_existing_statistics_and_identity(self):
        rows = self.rows(frequencies=(100., 200.), currents=(1e-7, 2e-7))
        rows += [{**row, "sample_index": 1, SIGNAL: row[SIGNAL] * 2} for row in rows]
        _, data = render_plot(rows, self.spec(rows, statistics="mean_sd"))
        self.assertEqual(len(data["summary_rows"]), 4)
        self.assertTrue(all(point["n"] == 2 for point in data["summary_rows"]))
        other = [{**row, "run_id": "other-run"} for row in rows]
        _, combined = render_plot(rows + other, self.spec(rows, statistics="mean_sd"))
        self.assertEqual(len(combined["report"]["legend_labels"]), 4)
        self.assertEqual(len(combined["summary_rows"]), 8)

    def test_incomplete_archived_target_is_not_replaced_or_silently_mixed(self):
        rows = self.rows(frequencies=(100., 200.))
        rows[0]["requested.excitation_frequency_hz"] = None
        with self.assertRaisesRegex(ValueError, "Incomplete archived target"):
            render_plot(rows, self.spec(rows))

    def test_unsupported_or_unbound_stacked_filter_rejects(self):
        rows = self.rows()
        for mode in ("xy_z", "heatmap"):
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "curve and four-channel"):
                render_plot(rows, self.spec(rows, mode=mode, z=SIGNAL))
        for stacked in ("actual.excitation_frequency_hz", ["requested.temperature_k"], [None]):
            spec = self.spec(rows)
            spec["stack_filters"] = stacked
            with self.subTest(stacked=stacked), self.assertRaises(ValueError):
                render_plot(rows, spec)


if __name__ == "__main__":
    unittest.main()
