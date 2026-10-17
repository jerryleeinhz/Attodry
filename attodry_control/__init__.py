"""Use this checkout's src package for `python -m` from the project root.

Installed distributions still use setuptools' src layout. No hardware imports,
environment changes or duplicated public API live in this bootstrap.
"""
from pathlib import Path as _Path

_source = _Path(__file__).resolve().parents[1] / "src" / "attodry_control"
__path__ = [str(_source)]
__file__ = str(_source / "__init__.py")
exec(compile(_Path(__file__).read_bytes(), __file__, "exec"), globals())
