"""Use this checkout's src package for python -m commands from its root."""
from importlib.util import spec_from_file_location as _source_spec
from pathlib import Path as _Path
import sys as _sys

_package = _Path(__file__).resolve().parent.parent / "src" / "attodry_control"
__path__ = [str(_package)]
__file__ = str(_package / "__init__.py")
__spec__ = _source_spec(__name__, __file__, submodule_search_locations=__path__)
__loader__ = __spec__.loader
__loader__.exec_module(_sys.modules[__name__])
