"""OS-owned, nonblocking station lease shared by real combination/diagnostic CLIs."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import os
import hashlib
import tempfile


@contextmanager
def controller_hardware_lease(serial_number, *, directory=None):
    """Cross-config ANC350 ownership; vendor connection ownership is also required.

    Spaces/case are aliases only for this resource key. Device discovery still
    compares the exact configured serial. Never unlink a live lock file.
    """
    if not isinstance(serial_number, str) or not serial_number.strip():
        raise ValueError("Controller lease requires an explicit serial number")
    identity = serial_number.replace(" ", "").upper()
    root = Path(directory) if directory is not None else Path(tempfile.gettempdir()) / "attodry-controller-leases"
    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    with station_hardware_lease(root / ("anc350-" + key + ".toml")) as path:
        yield path


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
