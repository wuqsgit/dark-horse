import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import shared.db as db
import shared.policy_loop as policy_loop


class EntryReviewPersistenceTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, "DB_PATH", str(Path(self.temp_dir.name) / "entry.db"))
        self.db_patch.start()
        db.init_db()

    def tearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_entry_snapshot_is_first_write_wins(self):
        snapshot = {
            "position_trade_id": "p-1",
            "symbol": "AAVEUSDT",
            "side": "LONG",
            "entry_time": "2026-07-11T01:00:00Z",
            "entry_price": 100.0,
            "total_score": 72.4,
            "entry_reason_text": "original reason",
        }

        self.assertTrue(db.record_entry_review_snapshot(snapshot))
        self.assertFalse(db.record_entry_review_snapshot({**snapshot, "total_score": 99, "entry_reason_text": "changed"}))

        row = db.fetch_entry_reviews(limit=1)[0]
        self.assertEqual(row["total_score"], 72.4)
        self.assertEqual(row["entry_reason_text"], "original reason")

    def test_missing_historical_indicators_remain_null(self):
        db.record_entry_review_snapshot({
            "position_trade_id": "p-2",
            "symbol": "OLDUSDT",
            "side": "LONG",
            "entry_time": "2026-07-10T01:00:00Z",
            "entry_price": 1.0,
            "snapshot_source": "historical_rebuild",
        })

        row = db.fetch_entry_reviews(limit=1)[0]
        self.assertIsNone(row["trend_score"])
        self.assertIsNone(row["atr_pct"])
        self.assertEqual(row["snapshot_source"], "historical_rebuild")

    def test_review_backfill_reuses_its_database_connection(self):
        conn = db.get_conn()
        try:
            conn.execute(
                """INSERT INTO position_trades(position_trade_id,symbol,side,entry_time,exit_time,entry_price,exit_price,quantity)
                   VALUES('p-3','ETHUSDT','LONG','2026-07-11T01:00:00Z','2026-07-11T02:00:00Z',1700,1710,0.1)"""
            )
            conn.commit()
        finally:
            conn.close()

        result = policy_loop.review_position_trade_entries(limit=10)
        self.assertEqual(result["inserted"], 1)

    def test_stale_current_position_is_not_shown_when_matching_trade_is_closed(self):
        db.upsert_position_history(
            "UBUSDT", "LONG", 10, 0.08, "entry", 80, 0.1, 0.01,
            position_id="stale-open", initial_stop_loss=0.07,
        )
        conn = db.get_conn()
        try:
            entry_time = conn.execute("SELECT entry_time FROM position_history WHERE symbol='UBUSDT'").fetchone()[0]
            conn.execute(
                """INSERT INTO position_trades(position_trade_id,symbol,side,entry_time,exit_time,entry_price,exit_price,quantity)
                   VALUES('closed-one','UBUSDT','LONG',?,datetime(?, '+10 minutes'),0.08,0.081,10)""",
                (entry_time, entry_time),
            )
            conn.commit()
        finally:
            conn.close()

        policy_loop.review_position_trade_entries(limit=10)
        rows = [r for r in db.fetch_entry_reviews(10) if r["symbol"] == "UBUSDT"]
        self.assertEqual([r["position_status"] for r in rows], ["closed"])


if __name__ == "__main__":
    unittest.main()
