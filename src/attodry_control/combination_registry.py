"""Tiny atomic project-local run index and file-only monitor selection."""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import tomllib

from .combination_store import open_readonly

DEFAULT_RUN_CONFIG = "config/hardware.local.toml"


def index_path(config_path):
    directory = Path(config_path).resolve().parent
    root = next((parent.parent for parent in (directory, *directory.parents)
                 if parent.name.lower() == "config"), directory)
    return root / "run_data" / "latest-combination-run.json"


def run_table(connection):
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    kinds = [("photo_runs", "created_utc"), ("combination_runs", "created_at_utc")]
    found = [pair for pair in kinds if pair[0] in tables]
    if len(found) != 1:
        raise ValueError("Database must contain exactly one supported combination run schema")
    return found[0]


def register_run(config_path, store, run_id):
    connection = store.db if getattr(store, "kind", None) == "dc_photoresponse" else store.connection
    table, created = run_table(connection)
    row = connection.execute(
        f"SELECT {created} FROM {table} WHERE run_id=?", (run_id,)
    ).fetchone()
    if row is None:
        raise ValueError("Cannot register an uncommitted run")
    record = {"database": str(store.path.resolve()), "run_id": run_id,
              "created_at_utc": row[0], "config": str(Path(config_path).resolve()),
              "pid": os.getpid(), "hostname": socket.gethostname()}
    destination = index_path(config_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                dir=destination.parent, prefix=".latest-combination-", suffix=".tmp",
                delete=False) as file:
            temporary = Path(file.name)
            json.dump(record, file, ensure_ascii=False, allow_nan=False)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return record


def _latest_id(database):
    with closing(open_readonly(database)) as connection:
        table, created = run_table(connection)
        row = connection.execute(f"SELECT run_id FROM {table} "
            f"ORDER BY {created} DESC, rowid DESC LIMIT 1").fetchone()
    if row is None:
        raise ValueError(f"No combination runs recorded in {database}")
    return row[0]


def resolve_monitor(config_path, database=None, run_id=None):
    """Select once: explicit DB, valid registered run, then TOML DB latest row.

    No instrument imports, hardware-config validation, file-tree search or mtime
    guesses. The saved PID is audit metadata, never proof of process liveness.
    """
    if database is not None:
        selected = Path(database).resolve()
        return selected, run_id or _latest_id(selected)
    try:
        record = json.loads(index_path(config_path).read_text(encoding="utf-8"))
        selected = Path(record["database"])
        saved_id = record["run_id"]
        if not selected.is_absolute() or not isinstance(saved_id, str) or not saved_id:
            raise ValueError("Invalid run index")
        with closing(open_readonly(selected)) as connection:
            table, created = run_table(connection)
            row = connection.execute(
                f"SELECT {created} FROM {table} WHERE run_id=?", (saved_id,)
            ).fetchone()
        if row is None or row[0] != record["created_at_utc"]:
            raise ValueError("Run index does not match a registered run")
        if run_id is None or run_id == saved_id:
            return selected, saved_id
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        pass
    config_path = Path(config_path).resolve()
    with config_path.open("rb") as file:
        document = tomllib.load(file)
    value = document.get("combination_scan", {}).get("database_path")
    if value is None:
        value = document.get("photoresponse_storage", document.get("project", {})).get("database_path")
    if value is None and document.get("combination_scan", {}).get("output_directory"):
        value = str(Path(document["combination_scan"]["output_directory"]) / "audit.sqlite")
    if not isinstance(value, str) or not value.strip() or "CHANGE_ME" in value:
        raise ValueError("No registered run: set photoresponse_storage.database_path or provide monitor --database")
    selected = Path(value)
    if not selected.is_absolute():
        selected = config_path.parent / selected
    selected = selected.resolve()
    return selected, run_id or _latest_id(selected)
