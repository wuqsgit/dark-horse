import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import shared.db as db


def iso_z(value):
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


class DataRetentionTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(db, "DB_PATH", str(Path(self.temp_dir.name) / "retention.db"))
        self.path_patch.start()
        db.init_db()

    def tearDown(self):
        self.path_patch.stop()
        self.temp_dir.cleanup()

    def test_cleanup_deletes_only_operational_rows_older_than_five_days(self):
        now = datetime(2026, 7, 10, 6, 0, tzinfo=timezone.utc)
        conn = db.get_conn()
        conn.executemany(
            """INSERT INTO candles_1h
               (time, symbol, open, high, low, close, volume, quote_vol, trades)
               VALUES (?, 'BTCUSDT', 1, 1, 1, 1, 1, 1, 1)""",
            [(iso_z(now - timedelta(days=6)),), (iso_z(now - timedelta(days=4)),)],
        )
        conn.execute(
            """INSERT INTO strategy_decisions
               (decision_id, time, symbol, decision_stage)
               VALUES ('old-decision', ?, 'BTCUSDT', 'scan')""",
            (iso_z(now - timedelta(days=6)),),
        )
        conn.execute(
            """INSERT INTO trades
               (symbol, side, pnl, created_at) VALUES ('BTCUSDT', 'LONG', 1, ?)""",
            (iso_z(now - timedelta(days=10)),),
        )
        conn.commit()
        conn.close()

        deleted = db.cleanup_old_operational_data(retention_days=5, now=now, batch_size=1)

        conn = db.get_conn()
        try:
            candle_times = [row[0] for row in conn.execute("SELECT time FROM candles_1h")]
            trade_count = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
            decision_count = conn.execute("SELECT COUNT(*) FROM strategy_decisions").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(candle_times, [iso_z(now - timedelta(days=4))])
        self.assertEqual(decision_count, 0)
        self.assertEqual(trade_count, 1)
        self.assertEqual(deleted["candles_1h"], 1)
        self.assertEqual(deleted["strategy_decisions"], 1)


if __name__ == "__main__":
    unittest.main()
