import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import shared.db as db


class AlphaOrderMetadataTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "orders.db")
        self.db_patch = patch.object(db, "DB_PATH", self.db_path)
        self.db_patch.start()
        db.init_db()
        self.account_token = db.set_account_context(3)

    def tearDown(self):
        db.reset_account_context(self.account_token)
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_order_persists_signal_identity_and_rejects_duplicate_client_id(self):
        order_id = db.insert_order(
            "AKEUSDT",
            "BUY",
            "MARKET",
            10,
            1.0,
            client_order_id="DH-A2-3-event-P",
            exchange_order_id="9001",
            signal_event_id="event-1",
            setup_id="setup-1",
            alpha_stage="PROBE_LONG",
            ai_model_versions={"trigger": "v1"},
        )
        conn = db.get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM orders WHERE id=?",
                (order_id,),
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(row["account_id"], 3)
        self.assertEqual(row["signal_event_id"], "event-1")
        self.assertEqual(row["exchange_order_id"], "9001")
        self.assertIn('"trigger": "v1"', row["ai_model_versions_json"])
        with self.assertRaises(sqlite3.IntegrityError):
            db.insert_order(
                "AKEUSDT",
                "BUY",
                "MARKET",
                10,
                1.0,
                client_order_id="DH-A2-3-event-P",
            )


if __name__ == "__main__":
    unittest.main()
