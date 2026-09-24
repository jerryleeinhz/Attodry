"""Run offline unittest targets with real hardware imports/DLL loads blocked.

Usage: python tools/run_guarded_tests.py [tests.test_module ...]
No arguments discovers the complete suite. Sets this checkout's src explicitly.
"""
from pathlib import Path
import ctypes
import importlib.abc
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT), str(ROOT / "tests")]


class HardwareBlocked(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split(".")[0] in {"pyvisa", "qcodes", "serial"}:
            raise RuntimeError(f"Offline test attempted real hardware import: {fullname}")
        return None


def blocked_dll(*args, **kwargs):
    raise RuntimeError("Offline test attempted to load a real Windows DLL")


sys.meta_path.insert(0, HardwareBlocked())
ctypes.WinDLL = blocked_dll
if hasattr(ctypes, "windll"):
    ctypes.windll.LoadLibrary = blocked_dll

if __name__ == "__main__":
    suite = (unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])
             if len(sys.argv) > 1 else
             unittest.defaultTestLoader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT)))
    print(f"Offline interpreter: {sys.executable}", flush=True)
    print(f"Source root: {ROOT / 'src'}", flush=True)
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    raise SystemExit(not result.wasSuccessful())
