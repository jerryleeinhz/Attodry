import ast
import json
from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = PROJECT_ROOT / "notebooks" / "transport_analysis.ipynb"


class NotebookTests(unittest.TestCase):
    def test_notebook_is_clean_syntax_valid_and_hardware_free(self) -> None:
        document = json.loads(NOTEBOOK.read_text(encoding="utf-8"))
        code = "\n\n".join(
            "".join(cell.get("source", ()))
            for cell in document["cells"]
            if cell["cell_type"] == "code"
        )
        ast.parse(code)
        forbidden_imports = (
            "attodry_control.attodry",
            "attodry_control.sr830",
            "attodry_control.gates",
            "MultiPyVu",
            "ppms_control",
        )
        self.assertFalse(any(name in code for name in forbidden_imports))
        for cell in document["cells"]:
            if cell["cell_type"] == "code":
                self.assertIsNone(cell["execution_count"])
                self.assertEqual(cell["outputs"], [])


if __name__ == "__main__":
    unittest.main()
