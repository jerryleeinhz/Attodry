"""OS-owned, nonblocking station lease shared by real combination/diagnostic CLIs."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import os


@contextmanager
def station_hardware_lease(station_config):
    """A leftover file is harmless; the OS releases the lock on process exit.

    This serializes cooperating CLIs using the same station config, even with
    a database override. It does
    not establish ownership of instruments operated by other applications.
    """
    path = Path(station_config).resolve().with_suffix(".hardware-owner.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as stream:
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("Another combination/diagnostic process owns this station") from exc
        try:
            yield path
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
