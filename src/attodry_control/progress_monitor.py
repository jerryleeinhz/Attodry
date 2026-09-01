"""Small, hardware-free helpers for tailing commissioning progress JSONL files.

This module intentionally imports only the Python standard library.  It opens a
progress file for reading and never constructs a DLL, VISA resource, or driver.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


class ProgressFormatError(ValueError):
    """Raised when a complete progress-file line is not a JSON object."""


class JsonlProgressTail:
    """Read appended JSON objects while safely ignoring an unfinished last line."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._offset = 0
        self._pending = ""

    def read_new_events(self) -> tuple[dict[str, Any], ...]:
        """Return complete events appended since the previous call.

        A writer can be interrupted between bytes of one JSONL line.  That line
        is retained internally until its newline arrives; it is never presented
        as a partly decoded measurement.
        """

        try:
            with self.path.open("r", encoding="utf-8", newline="") as stream:
                stream.seek(0, 2)
                size = stream.tell()
                if size < self._offset:
                    # A new run may have replaced/truncated the selected file.
                    self._offset = 0
                    self._pending = ""
                stream.seek(self._offset)
                appended = stream.read()
                self._offset = stream.tell()
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"Progress file does not exist: {self.path}") from exc

        if not appended and not self._pending:
            return ()

        text = self._pending + appended
        lines = text.splitlines(keepends=True)
        self._pending = ""
        if lines and not lines[-1].endswith(("\n", "\r")):
            self._pending = lines.pop()

        events: list[dict[str, Any]] = []
        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            try:
                decoded = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ProgressFormatError(
                    f"Invalid JSONL event in {self.path} at byte offset "
                    f"{self._offset - len(appended)}: {exc.msg}"
                ) from exc
            if not isinstance(decoded, dict):
                raise ProgressFormatError(
                    f"Progress event in {self.path} must be a JSON object."
                )
            events.append(decoded)
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
