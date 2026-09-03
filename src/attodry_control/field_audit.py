from __future__ import annotations

import json
import os
from pathlib import Path
import time
from typing import Callable, Mapping
from uuid import uuid4


SCHEMA_VERSION = 1


class JsonlEventWriter:
    """Append canonical, durable events to one magnetic-field audit stream."""

    def __init__(
        self,
        path: str | Path,
        *,
        create: bool,
        run_id: str | None = None,
        wall_time: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self.run_id = run_id or uuid4().hex
        self._wall_time = wall_time
        self._closed = False
        self._failed: BaseException | None = None
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.open("x", encoding="utf-8").close()
            self._next_event_index = 0
        else:
            events, trailing = read_jsonl_events(self.path)
            if trailing:
                raise ValueError(
                    "Cannot append to a JSONL audit stream with an incomplete "
                    "trailing record."
                )
            self._next_event_index = len(events)
            if events:
                existing_run_id = events[0].get("run_id")
                if not isinstance(existing_run_id, str) or not existing_run_id:
                    raise ValueError("Existing JSONL audit stream has no run_id.")
                if run_id is not None and run_id != existing_run_id:
                    raise ValueError("run_id does not match the existing audit stream.")
                self.run_id = existing_run_id

    def __enter__(self) -> JsonlEventWriter:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._closed = True

    def append(self, payload: Mapping[str, object]) -> dict[str, object]:
        if self._closed:
            raise ValueError("Cannot append to a closed JSONL audit writer.")
        if self._failed is not None:
            raise OSError(
                "Cannot append after a previous JSONL audit write had an "
                "uncertain outcome."
            ) from self._failed
        event = payload.get("event")
        if not isinstance(event, str) or not event.strip():
            raise ValueError("Every JSONL audit record requires a non-empty event.")
        record = dict(payload)
        record.update(
            {
                "schema_version": SCHEMA_VERSION,
                "run_id": self.run_id,
                "event_index": self._next_event_index,
                "captured_unix_s": self._wall_time(),
            }
        )
        serialized = json.dumps(
            record,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        start_offset: int | None = None
        try:
            with self.path.open("ab") as stream:
                start_offset = stream.tell()
                stream.write((serialized + "\n").encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException as exc:
            # Bytes may already have reached the file.  Never reuse an index or
            # append behind a potentially torn record after that uncertainty.
            # Best-effort rollback also prevents a complete but unconfirmed
            # terminal record from being mistaken for a durable completion by
            # the file-only monitor.
            self._failed = exc
            if start_offset is not None:
                try:
                    with self.path.open("r+b") as stream:
                        stream.truncate(start_offset)
                        stream.flush()
                        os.fsync(stream.fileno())
                except BaseException as rollback_error:
                    exc.add_note(
                        "Could not confirm rollback of the uncertain JSONL "
                        f"append: {rollback_error}"
                    )
            raise
        self._next_event_index += 1
        return record


def read_jsonl_events(path: str | Path) -> tuple[list[dict[str, object]], bool]:
    """Read complete JSON objects, tolerating only a torn final record."""

    audit_path = Path(path)
    try:
        raw = audit_path.read_bytes()
    except OSError as exc:
        raise OSError(f"Could not read magnetic-field audit {audit_path}: {exc}") from exc
    if not raw:
        return [], False

    has_final_newline = raw.endswith(b"\n")
    lines = raw.splitlines()
    events: list[dict[str, object]] = []
    for index, line in enumerate(lines):
        final_unterminated_line = index == len(lines) - 1 and not has_final_newline
        if final_unterminated_line:
            # Even syntactically valid JSON is not a committed JSONL record
            # until its terminating newline exists.
            return events, True
        try:
            decoded = line.decode("utf-8")
            value = json.loads(decoded)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"Invalid complete JSONL record on line {index + 1} of "
                f"{audit_path}: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise ValueError(
                f"Invalid complete JSONL record on line {index + 1} of "
                f"{audit_path}: record is not a JSON object"
            )
        events.append(value)
    return events, False
