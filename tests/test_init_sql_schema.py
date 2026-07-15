import re
import sqlite3
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DATABASE = ROOT / "alphadog.db"
INIT_SQL = ROOT / "db" / "init.sql"


def schema_objects(connection):
    rows = connection.execute(
        """SELECT type, name, sql
           FROM sqlite_master
           WHERE sql IS NOT NULL
             AND name NOT LIKE 'sqlite_%'
           ORDER BY type, name"""
    ).fetchall()
    return {
        (object_type, name): re.sub(r"\s+", " ", sql.strip().rstrip(";"))
        for object_type, name, sql in rows
    }


class InitSqlSchemaTest(unittest.TestCase):
    def test_init_sql_matches_all_live_database_schema_objects(self):
        source = sqlite3.connect(DATABASE, timeout=10)
        initialized = sqlite3.connect(":memory:")
        try:
            initialized.executescript(INIT_SQL.read_text(encoding="utf-8"))
            self.assertEqual(schema_objects(initialized), schema_objects(source))
        finally:
            initialized.close()
            source.close()


if __name__ == "__main__":
    unittest.main()
