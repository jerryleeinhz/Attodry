from pathlib import Path
import tempfile
import unittest

from attodry_control.scientific_plotting import (
    PUBLICATION_RASTER_DPI,
    export_publication_figure_set,
    ordered_series_style,
    publication_style,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class ScientificPlottingTests(unittest.TestCase):
    def test_ordered_series_style_accepts_distinct_sequential_colormaps(self) -> None:
        try:
            import matplotlib as mpl
        except ImportError as exc:
            self.skipTest(f"matplotlib unavailable: {exc}")

        temperature = ordered_series_style(0, 2, colormap_name="plasma")
        frequency = ordered_series_style(0, 2, colormap_name="viridis")

        self.assertEqual(temperature["color"], mpl.colormaps["plasma"](0.08))
        self.assertEqual(frequency["color"], mpl.colormaps["viridis"](0.08))
        self.assertNotEqual(temperature["color"], frequency["color"])
        self.assertEqual(temperature["marker"], frequency["marker"])
        self.assertEqual(temperature["linestyle"], frequency["linestyle"])

    def test_style_is_scoped_and_export_writes_raster_and_vectors(self) -> None:
        try:
            import matplotlib as mpl
            import matplotlib.pyplot as plt
        except ImportError as exc:
            self.skipTest(f"matplotlib unavailable: {exc}")

        original_font_size = mpl.rcParams["font.size"]
        with publication_style():
            self.assertEqual(mpl.rcParams["savefig.dpi"], PUBLICATION_RASTER_DPI)
            self.assertEqual(mpl.rcParams["pdf.fonttype"], 42)
            self.assertEqual(mpl.rcParams["svg.fonttype"], "none")
            figure, axis = plt.subplots(figsize=(3.0, 2.0), constrained_layout=True)
            axis.plot([1.0, 2.0], [3.0, 4.0])
        self.addCleanup(plt.close, figure)

        self.assertEqual(mpl.rcParams["font.size"], original_font_size)
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT) as directory:
            paths = export_publication_figure_set(
                figure, Path(directory) / "publication_figure"
            )
            self.assertEqual({path.suffix for path in paths}, {".png", ".pdf", ".svg"})
            self.assertTrue(all(path.stat().st_size > 0 for path in paths))


if __name__ == "__main__":
    unittest.main()
