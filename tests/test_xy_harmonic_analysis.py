import json
import os
from pathlib import Path
import tempfile
import unittest

from attodry_control.xy_harmonic_analysis import (
    aggregate_xy_harmonics,
    discover_xy_harmonic_records,
    load_xy_harmonic_samples,
    plot_xy_harmonics,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class XYHarmonicAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.paths: list[Path] = []
        self.addCleanup(self._cleanup_files)

    def test_loader_discards_xx_and_preserves_harmonic_order(self) -> None:
        path = self._write_json("accepted.json", self._record(completed=True))

        rows = load_xy_harmonic_samples(path)

        self.assertEqual([row.harmonic for row in rows], [1, 2, 3])
        self.assertEqual([item.harmonic for item in aggregate_xy_harmonics(rows)], [1, 2, 3])
        self.assertTrue(all("clean" in row.statuses for row in rows))

    def test_rejected_record_requires_audit_and_status_filters(self) -> None:
        path = self._write_json(
            "rejected.json", self._record(completed=False, harmonic_two_locked=False)
        )

        with self.assertRaisesRegex(ValueError, "include_rejected=True"):
            load_xy_harmonic_samples(path)
        unlocked = load_xy_harmonic_samples(
            path, include_rejected=True, sample_statuses={"unlocked"}
        )

        self.assertEqual([row.harmonic for row in unlocked], [2])

    def test_discovery_filters_completed_harmonic_records(self) -> None:
        completed = self._write_json("completed.json", self._record(completed=True))
        rejected = self._write_json("rejected.json", self._record(completed=False))

        self.assertIn(completed, discover_xy_harmonic_records(PROJECT_ROOT))
        self.assertNotIn(rejected, discover_xy_harmonic_records(PROJECT_ROOT))
        self.assertIn(
            rejected,
            discover_xy_harmonic_records(PROJECT_ROOT, record_statuses={"rejected"}),
        )

    def test_plot_is_xy_only_and_marks_each_harmonic(self) -> None:
        try:
            import matplotlib.pyplot as plt
        except ImportError:
            self.skipTest("matplotlib is not installed")
        path = self._write_json("accepted.json", self._record(completed=True))
        rows = load_xy_harmonic_samples(path)

        figure = plot_xy_harmonics(rows)
        self.addCleanup(plt.close, figure)

        axis = figure.axes[0]
        self.assertEqual(axis.get_title(), "SR830 XY-only harmonic response")
        self.assertEqual([text.get_text() for text in axis.texts], ["h1", "h2", "h3"])
        _, labels = axis.get_legend_handles_labels()
        self.assertEqual(labels, ["XY"])

    def test_notebook_is_valid_and_xy_only(self) -> None:
        path = PROJECT_ROOT / "notebooks" / "sr830_xy_harmonics.ipynb"
        notebook = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(notebook["nbformat"], 4)
        source = "\n".join(
            "".join(cell["source"])
            for cell in notebook["cells"]
            if cell["cell_type"] == "code"
        )
        self.assertIn("load_xy_harmonic_samples", source)
        self.assertIn("plot_xy_harmonics", source)
        self.assertIn("h{row.harmonic}", source)
        for index, cell in enumerate(notebook["cells"]):
            if cell["cell_type"] == "code":
                compile("".join(cell["source"]), f"xy-notebook-cell-{index}", "exec")

    def _write_json(self, name: str, payload: dict[str, object]) -> Path:
        descriptor, raw_path = tempfile.mkstemp(
            prefix="xy_harmonic_", suffix=f"_{name}", dir=PROJECT_ROOT
        )
        os.close(descriptor)
        path = Path(raw_path)
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.paths.append(path)
        return path

    def _cleanup_files(self) -> None:
        for path in self.paths:
            if path.exists():
                path.unlink()

    @staticmethod
    def _record(*, completed: bool, harmonic_two_locked: bool = True) -> dict[str, object]:
        readings = []
        for harmonic in (1, 2, 3):
            readings.extend(
                [
                    {
                        "role": "xx",
                        "harmonic": harmonic,
                        "x_v": float(harmonic),
                        "y_v": 0.0,
                        "amplitude_v": float(harmonic),
                        "phase_deg": 0.0,
                        "frequency_hz": 17.777,
                        "locked": True,
                        "overload": False,
                    },
                    {
                        "role": "xy",
                        "harmonic": harmonic,
                        "x_v": float(harmonic) / 10,
                        "y_v": 0.0,
                        "amplitude_v": float(harmonic) / 10,
                        "phase_deg": 0.0,
                        "frequency_hz": 17.777,
                        "locked": harmonic != 2 or harmonic_two_locked,
                        "overload": False,
                    },
                ]
            )
        return {"completed": completed, "readings": readings}


if __name__ == "__main__":
    unittest.main()
