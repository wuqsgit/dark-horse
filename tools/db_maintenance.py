"""SQLite maintenance helpers for alphadog.db."""
from __future__ import annotations

import argparse
import os
import sqlite3


TIME_COLUMNS = ("time", "timestamp", "created_at", "run_time", "entry_time", "exit_time", "date")


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def tables(conn: sqlite3.Connection) -> list[str]:
    return [
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def inspect(path: str, include_dbstat: bool = False) -> None:
    conn = connect(path)
    print(f"db_path={path}", flush=True)
    print(f"db_bytes={os.path.getsize(path)}", flush=True)
    print(f"page_count={conn.execute('PRAGMA page_count').fetchone()[0]}", flush=True)
    print(f"freelist_count={conn.execute('PRAGMA freelist_count').fetchone()[0]}", flush=True)
    print(f"page_size={conn.execute('PRAGMA page_size').fetchone()[0]}", flush=True)
    print(flush=True)

    for table in tables(conn):
        count = conn.execute(f'SELECT COUNT(*) AS c FROM "{table}"').fetchone()["c"]
        cols = [r["name"] for r in conn.execute(f'PRAGMA table_info("{table}")')]
        time_col = next((c for c in TIME_COLUMNS if c in cols), None)
        suffix = ""
        if time_col:
            row = conn.execute(
                f'SELECT MIN("{time_col}") AS mn, MAX("{time_col}") AS mx FROM "{table}"'
            ).fetchone()
            suffix = f" {time_col}=[{row['mn']}..{row['mx']}]"
        print(f"{table}: rows={count}{suffix}", flush=True)

    if not include_dbstat:
        conn.close()
        return

    print(flush=True)
    print("dbstat_top:", flush=True)
    try:
        rows = conn.execute(
            "SELECT name, SUM(pgsize) AS bytes, COUNT(*) AS pages "
            "FROM dbstat GROUP BY name ORDER BY bytes DESC LIMIT 40"
        ).fetchall()
        for row in rows:
            print(f"{row['name']}: bytes={row['bytes']} pages={row['pages']}", flush=True)
    except sqlite3.DatabaseError as exc:
        print(f"dbstat unavailable: {exc}", flush=True)
    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="alphadog.db")
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--dbstat", action="store_true")
    args = parser.parse_args()

    if args.inspect:
        inspect(args.db, include_dbstat=args.dbstat)


if __name__ == "__main__":
    main()
