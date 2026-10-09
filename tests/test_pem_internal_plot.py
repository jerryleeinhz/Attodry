import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from attodry_control.pem_internal_plot import plot_diagnostic


def result_record():
    import numpy as np
    from attodry_control.pem_internal_analysis import summarize_capture

    time = np.arange(2048) / 1024
    captures = []
    for illumination, amplitude in (("dark", 0.02e-6), ("light", 2e-6)):
        capture = {"x_v": (amplitude * np.cos(2 * np.pi * 54 * time)).tolist(),
                   "y_v": (amplitude * np.sin(2 * np.pi * 54 * time)).tolist(),
                   "t_s": time.tolist(), "actual_rate_hz": 1024.0, "completed": True}
        analysis = summarize_capture({**capture, "sample_rate_hz": 1024.0, "validity": illumination == "dark"},
                                     internal_frequency_hz=50000, harmonic=2, time_constant_s=0.001,
                                     pem_frequency_hz=50027)
        captures.append({"capture_id": illumination, "illumination": illumination,
                         "optical_point_index": 0, "internal_frequency_hz": 50000,
                         "time_constant_s": 0.001, "harmonic": 2, "pem_frequency_hz": 50027,
                         "capture": capture, "analysis": analysis,
                         "exclusion_reasons": [] if illumination == "dark" else ["input_overload"]})
    return {"schema_version": 1, "mode": "pem_internal_diagnostic", "status": "completed",
            "cleanup": {"verified": True}, "captures": captures,
            "frequency_observations": [{"optical_point_index": 0,
                                        "samples": [{"elapsed_s": index, "frequency_hz": 50027.0,
                                                     "stable": False, "at_utc": "2026-10-06T00:00:00Z"}
                                                    for index in range(3)]}]}


class PemInternalPlotTests(unittest.TestCase):
    def test_synthetic_render_preserves_raw_invalidity_counts_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / "result.json"
            content = json.dumps(result_record())
            source.write_text(content, encoding="utf-8")
            paths = plot_diagnostic(directory)
            self.assertEqual(len(paths), 5)
            self.assertTrue(all(path.exists() and path.stat().st_size > 0 for path in paths))
            manifest = json.loads(paths[-1].read_text(encoding="utf-8"))
            self.assertIn("input_overload", manifest["figures"][0]["captures"][1]["exclusion_reasons"])
            self.assertEqual(manifest["figures"][1]["counts"], [3])
            self.assertIn("STABLE=false", manifest["figures"][1]["stability_note"])
            self.assertFalse(manifest["formal_hall_eligible"])
            again = plot_diagnostic(directory)
            self.assertNotEqual(paths[0].parent, again[0].parent)
            self.assertEqual(source.read_text(encoding="utf-8"), content)

    def test_missing_arrays_stay_explicit_and_missing_dependencies_are_clear(self):
        result = result_record()
        result["captures"][1]["capture"]["x_v"] = None
        result.update(status="failed", cleanup={"verified": False})
        result["frequency_observations"] = [{"optical_point_index": 0, "samples": []}]
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "result.json").write_text(json.dumps(result), encoding="utf-8")
            paths = plot_diagnostic(directory)
            manifest = json.loads(paths[-1].read_text(encoding="utf-8"))
            self.assertIn("missing_or_invalid_xy_arrays", manifest["figures"][0]["captures"][1]["exclusion_reasons"])
            self.assertIn("run_terminal_failed", manifest["figures"][0]["captures"][0]["exclusion_reasons"])
            self.assertIn("final_cleanup_not_verified", manifest["figures"][0]["captures"][0]["exclusion_reasons"])
            with patch("builtins.__import__", side_effect=ImportError("analysis unavailable")):
                with self.assertRaisesRegex(ValueError, "requires numpy and matplotlib"):
                    plot_diagnostic(directory)


if __name__ == "__main__":
    unittest.main()
