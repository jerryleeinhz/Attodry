"""Small, hardware-free helpers for tailing commissioning progress JSONL files.

This module intentionally imports only the Python standard library.  It opens a
progress file for reading and never constructs a DLL, VISA resource, or driver.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable


class ProgressFormatError(ValueError):
    """Raised when a complete progress-file line is not a JSON object."""


class JsonlProgressTail:
    """Read complete appended UTF-8 events without consuming malformed lines."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._offset = 0
        self._pending = b""
        self._identity: tuple[int, int] | None = None
        self._prefix = b""
        self._end_anchor = b""
        self.generation = 0

    def read_new_events(self) -> tuple[dict[str, Any], ...]:
        """Return a file snapshot's new complete events.

        Decode only newline-terminated records, so an incomplete UTF-8 character
        is deferred together with its line. Parsing is transactional: a malformed
        complete record remains an error on subsequent refreshes. Replacement or
        truncation starts a new generation for downstream snapshot caches.
        """

        try:
            with self.path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                identity = (stat.st_dev, stat.st_ino)
                size = stat.st_size
                reset = (
                    (self._identity is not None and identity != self._identity)
                    or size < self._offset
                )
                if not reset and self._offset:
                    stream.seek(0)
                    prefix_matches = stream.read(len(self._prefix)) == self._prefix
                    stream.seek(self._offset - len(self._end_anchor))
                    end_matches = stream.read(len(self._end_anchor)) == self._end_anchor
                    reset = not (prefix_matches and end_matches)
                if reset:
                    self._offset = 0
                    self._pending = b""
                    self._prefix = b""
                    self._end_anchor = b""
                    self._identity = identity
                    self.generation += 1
                stream.seek(self._offset)
                appended = stream.read(size - self._offset)
                if len(appended) != size - self._offset:
                    raise ProgressFormatError(
                        f"Progress file changed while reading {self.path}; refresh again."
                    )
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"Progress file does not exist: {self.path}") from exc

        data = self._pending + appended
        lines = data.split(b"\n")
        pending = lines.pop()
        events: list[dict[str, Any]] = []
        line_offset = self._offset - len(self._pending)
        for raw_line in lines:
            line = raw_line.strip()
            if line:
                try:
                    decoded = json.loads(line.decode("utf-8-sig"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ProgressFormatError(
                        f"Invalid JSONL event in {self.path} at byte offset "
                        f"{line_offset}: {exc}"
                    ) from exc
                if not isinstance(decoded, dict):
                    raise ProgressFormatError(
                        f"Progress event in {self.path} at byte offset "
                        f"{line_offset} must be a JSON object."
                    )
                events.append(decoded)
            line_offset += len(raw_line) + 1

        # Advance only after every complete line in this snapshot has parsed.
        self._prefix = (self._prefix + appended)[:512]
        self._end_anchor = (self._end_anchor + appended)[-512:]
        self._offset += len(appended)
        self._pending = pending
        self._identity = identity
        return tuple(events)


def resolve_progress_path(
    *,
    progress: Path | None,
    directory: Path | None,
    patterns: Iterable[str],
) -> Path:
    """Resolve one explicit file or the newest matching file under a directory."""

    if (progress is None) == (directory is None):
        raise ValueError("Provide exactly one of --progress or --directory.")
    if progress is not None:
        resolved = progress.resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"Progress file does not exist: {resolved}")
        return resolved

    assert directory is not None
    resolved_directory = directory.resolve()
    if not resolved_directory.is_dir():
        raise NotADirectoryError(f"Progress directory does not exist: {resolved_directory}")
    candidates = {
        path.resolve()
        for pattern in patterns
        for path in resolved_directory.rglob(pattern)
        if path.is_file()
    }
    if not candidates:
        rendered_patterns = ", ".join(patterns)
        raise FileNotFoundError(
            f"No matching progress JSONL under {resolved_directory} ({rendered_patterns})."
        )
    return max(candidates, key=lambda path: (path.stat().st_mtime_ns, str(path)))
