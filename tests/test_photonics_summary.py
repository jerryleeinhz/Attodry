"""Quality summaries use formal normalized status and retain legacy semantics."""
import unittest

from attodry_control.lockin_overload import overload_summary
from attodry_control.unified_plotting import _unresolved_dimensions, _known_label


class PhotonicsSummaryTests(unittest.TestCase):
    def test_only_formal_pairs_count_without_native_status_bit_interpretation(self):
        status = {"schema_version": "photonics-lockin-v1", "samples": [
            {"selected_harmonics": {"xx": 1, "xy": 2}, "samples": {
                "xx": {"status": {"input_overload": False, "output_scale_overload": False,
                    "native_status": {"lia_status_raw": 128}}},
                "xy": {"status": {"input_overload": True, "output_scale_overload": False}}}},
            {"selected_harmonics": {"xx": 3}, "samples": {
                "xx": {"status": {"input_overload": False, "output_scale_overload": True}},
                "xy": {"status": {"input_overload": False, "output_scale_overload": False}}}}],
            "transition_probes": [{"samples": [{"input_overload": True}]}]}
        result = overload_summary([{"status": status}])
        self.assertEqual(result["formal_pairs"], 2)
        self.assertEqual(result["overloaded_pairs_by_role"], {"lockin_xx": 1, "lockin_xy": 1})
        self.assertEqual(result["continued_overload_pairs"], 0)
        self.assertEqual(result["data_quality"], "overload_recorded")

    def test_power_plot_covers_optical_axis_but_keeps_wavelength_explicit(self):
        rows = [{"requested.optical_target_power_w": power,
                 "requested.optical_wavelength_nm": wavelength,
                 "axes.optical.index": index, "measured.optical_power_w": power}
                for index, (power, wavelength) in enumerate(((1e-6, 600), (2e-6, 600), (1e-6, 700)))]
        missing = _unresolved_dimensions(rows, plot_keys=("measured.optical_power_w",),
                                         filter_keys=(), group_keys=())
        self.assertEqual(missing, [("requested.optical_wavelength_nm", 2)])
        self.assertEqual(_known_label("optical_power_w"), ("Optical power", "W"))


if __name__ == "__main__":
    unittest.main()
