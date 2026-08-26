import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

import shared.db as db


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
    def test_init_sql_includes_all_live_database_schema_objects(self):
        source = sqlite3.connect(DATABASE, timeout=10)
        initialized = sqlite3.connect(":memory:")
        try:
            initialized.executescript(INIT_SQL.read_text(encoding="utf-8"))
            initialized_objects = schema_objects(initialized)
            for object_key, definition in schema_objects(source).items():
                self.assertIn(object_key, initialized_objects)
                self.assertEqual(initialized_objects[object_key], definition)
        finally:
            initialized.close()
            source.close()

    def test_runtime_and_init_sql_create_history_reader_indexes(self):
        original_db_path = db.DB_PATH
        with tempfile.TemporaryDirectory() as temp_dir:
            db.DB_PATH = str(Path(temp_dir) / "runtime.db")
            try:
                db.init_db()
                runtime = sqlite3.connect(db.DB_PATH)
                initialized = sqlite3.connect(":memory:")
                try:
                    initialized.executescript(INIT_SQL.read_text(encoding="utf-8"))
                    runtime_objects = schema_objects(runtime)
                    initialized_objects = schema_objects(initialized)
                    for name in (
                        "idx_fills_account_symbol_time",
                        "idx_income_account_symbol_time",
                    ):
                        object_key = ("index", name)
                        self.assertIn(object_key, runtime_objects)
                        self.assertIn(object_key, initialized_objects)
                        self.assertEqual(
                            initialized_objects[object_key], runtime_objects[object_key]
                        )
                finally:
                    initialized.close()
                    runtime.close()
            finally:
                db.DB_PATH = original_db_path


if __name__ == "__main__":
    unittest.main()
