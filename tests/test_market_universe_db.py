import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import shared.db as db


class MarketUniverseDbTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "market.db")
        self.db_path_patch = patch.object(db, "DB_PATH", self.db_path)
        self.db_path_patch.start()

    def tearDown(self):
        self.db_path_patch.stop()
        self.temp_dir.cleanup()

    def test_market_universe_and_futures_tables_are_created(self):
        db.init_db()

        conn = sqlite3.connect(db.DB_PATH)
        try:
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
        self.assertIn("market_universe", tables)
        self.assertIn("futures_candles_15m", tables)
        self.assertIn("futures_candles_1h", tables)

    def test_ready_only_query_excludes_forced_unready_symbols(self):
        db.init_db()
        db.upsert_market_universe([
        {
            "pool_type": "normal", "source_symbol": "FORCEDUSDT", "spot_symbol": "FORCEDUSDT",
            "futures_symbol": "FORCEDUSDT", "selected": False, "forced_position": True,
            "data_ready": False, "selection_reason": "open_position",
        },
        {
            "pool_type": "normal", "source_symbol": "BTCUSDT", "spot_symbol": "BTCUSDT",
            "futures_symbol": "BTCUSDT", "selected": True, "forced_position": False,
            "data_ready": True, "selection_reason": "top150",
        },
        ])

        rows = db.fetch_market_universe("normal", selected_only=True, ready_only=True)

        self.assertEqual([row["source_symbol"] for row in rows], ["BTCUSDT"])

    def test_metadata_refresh_preserves_previous_readiness_until_collection_finishes(self):
        db.init_db()
        row = {
            "pool_type": "normal", "source_symbol": "BTCUSDT", "spot_symbol": "BTCUSDT",
            "futures_symbol": "BTCUSDT", "selected": True, "data_ready": True,
        }
        db.upsert_market_universe([row])
        refreshed = dict(row, data_ready=False, data_error="not_checked", spot_quote_volume_24h=123)

        db.upsert_market_universe([refreshed])

        stored = db.fetch_market_universe("normal")[0]
        self.assertEqual(stored["data_ready"], 1)
        self.assertIsNone(stored["data_error"])


if __name__ == "__main__":
    unittest.main()
