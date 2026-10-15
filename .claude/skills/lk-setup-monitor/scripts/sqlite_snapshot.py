"""Read-only progress snapshot of a run database.

Opens the database with ``mode=ro`` in a single read transaction, so it never
writes, checkpoints, or blocks the acquisition writer. Knows the photoresponse
tables (``photo_*``) and the legacy transport tables (``runs``/``conditions``).

Usage: python -s -B sqlite_snapshot.py DATABASE [--rows N]
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path


def short(value, limit=160):
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + "..."


def columns(db, table):
    return [row[1] for row in db.execute(f'PRAGMA table_info("{table}")')]


def tail(db, table, n, prefer):
    cols = columns(db, table)
    shown = [c for c in prefer if c in cols] or cols[:6]
    order = "rowid"
    rows = db.execute(
        f'SELECT {", ".join(shown)} FROM "{table}" ORDER BY {order} DESC LIMIT ?', (n,)
    ).fetchall()
    print(f"\n-- last {len(rows)} of {table} ({', '.join(shown)})")
    for row in reversed(rows):
        print("  " + " | ".join(short(v) for v in row))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("--rows", type=int, default=5)
    args = parser.parse_args()
    if not args.database.is_file():
        sys.exit(f"missing database: {args.database}")

    db = sqlite3.connect(f"{args.database.resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    db.execute("BEGIN")  # one consistent snapshot across all queries
    tables = [r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    print(f"== {args.database}")
    for table in tables:
        print(f"{table}: {db.execute(f'SELECT COUNT(*) FROM \"{table}\"').fetchone()[0]} rows")

    if "photo_conditions" in tables and "accepted" in columns(db, "photo_conditions"):
        accepted, total = db.execute(
            "SELECT COALESCE(SUM(accepted), 0), COUNT(*) FROM photo_conditions").fetchone()
        print(f"\nphoto_conditions accepted: {accepted}/{total}")

    for table, prefer in (
        ("photo_runs", ["run_id", "series_id", "attempt_index", "created_utc", "outcome"]),
        ("photo_events", ["event_id", "run_id", "event_json"]),
        ("photo_conditions", ["run_id", "condition_id", "attempt_index", "accepted"]),
        ("runs", ["run_id", "created_at_utc", "status"]),
        ("events", ["event_id", "created_at_utc", "level", "event_type", "condition_id", "message"]),
        ("conditions", ["run_id", "sequence_index", "temperature_k", "bx_t", "bz_t", "gate_top_v", "gate_bottom_v"]),
    ):
        if table in tables:
            tail(db, table, args.rows, prefer)
    db.rollback()
    db.close()


if __name__ == "__main__":
    main()
