"""Run the combination CLI from this checkout, independent of editable installs.

Usage: python -s tools/run_combination.py describe-hardware --config config/photonics.local.toml
All commands and hardware authorization flags are handled by the existing CLI.
"""
from pathlib import Path
import importlib.util
import os
import sys


def main():
    source = Path(__file__).resolve().parents[1] / "src"
    sys.path.insert(0, str(source))
    os.environ["PYTHONPATH"] = os.pathsep.join(
        [str(source), *filter(None, os.environ.get("PYTHONPATH", "").split(os.pathsep))])
    spec = importlib.util.find_spec("attodry_control")
    if spec is None or spec.origin is None or Path(spec.origin).resolve() != source / "attodry_control/__init__.py":
        raise SystemExit("Another attodry_control checkout is already loaded; start a fresh Python process.")
    from attodry_control.combination_cli import main as combination_main
    combination_main()


if __name__ == "__main__":
    main()
