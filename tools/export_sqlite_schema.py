"""Export the current SQLite schema as a deterministic SQL snapshot."""
import sqlite3
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    database = root / "alphadog.db"
    target = root / "db" / "init.sql"
    conn = sqlite3.connect(database)
    try:
        rows = conn.execute(
            """SELECT type, name, sql FROM sqlite_master
               WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%'
               ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END, name"""
        ).fetchall()
    finally:
        conn.close()
    statements = ["-- Auto-generated SQLite schema snapshot for DarkHorse.", "-- Generated from alphadog.db sqlite_master.", ""]
    statements.extend(f"{sql.rstrip(';')};\n" for _, _, sql in rows)
    target.write_text("\n".join(statements), encoding="utf-8")
    print(f"wrote {target} ({len(rows)} objects)")


if __name__ == "__main__":
    main()
