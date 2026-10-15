"""Source selection without hardware access or global environment changes."""
import importlib.machinery
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "tools/run_combination.py"


class CheckoutLauncherTests(unittest.TestCase):
    def test_foreign_pythonpath_cannot_override_checkout_help(self):
        with tempfile.TemporaryDirectory() as directory:
            foreign = Path(directory) / "attodry_control"
            foreign.mkdir()
            (foreign / "__init__.py").write_text("raise AssertionError('foreign package imported')\n", encoding="utf8")
            env = dict(os.environ, PYTHONPATH=directory)
            result = subprocess.run([sys.executable, "-s", "-B", str(LAUNCHER), "--help"],
                                    cwd=directory, env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("describe-hardware", result.stdout)

    def test_existing_cli_receives_all_arguments_and_child_source_is_explicit(self):
        args = [str(LAUNCHER), "run", "--config", "fake.toml", "--authorize-optical", "--confirm-optical-route"]
        observed = []
        def main():
            observed.append((list(sys.argv), os.environ["PYTHONPATH"].split(os.pathsep)[0]))
        with patch.object(sys, "path", list(sys.path)), patch.object(sys, "argv", args), \
                patch.dict(os.environ), patch("attodry_control.combination_cli.main", main):
            runpy.run_path(str(LAUNCHER), run_name="__main__")
        self.assertEqual(observed[0][0], args)
        self.assertEqual(Path(observed[0][1]), ROOT / "src")

    def test_already_loaded_foreign_package_fails_before_cli_dispatch(self):
        foreign = ModuleType("attodry_control")
        foreign.__spec__ = importlib.machinery.ModuleSpec("attodry_control", None, origin="foreign/__init__.py")
        with patch.object(sys, "path", list(sys.path)), patch.dict(os.environ), \
                patch.dict(sys.modules, {"attodry_control": foreign}), \
                self.assertRaisesRegex(SystemExit, "already loaded"):
            runpy.run_path(str(LAUNCHER), run_name="__main__")
