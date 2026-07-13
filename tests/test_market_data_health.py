import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import shared.db as db


class MarketDataHealthTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(db, "DB_PATH", str(Path(self.temp_dir.name) / "market.db"))
        self.path_patch.start()
        db.init_db()

    def tearDown(self):
        self.path_patch.stop()
        self.temp_dir.cleanup()

    def test_health_summarizes_selected_ready_and_unready_by_pool(self):
        db.upsert_market_universe([
            {"pool_type": "normal", "source_symbol": "BTCUSDT", "spot_symbol": "BTCUSDT", "futures_symbol": "BTCUSDT", "selected": True, "data_ready": True},
            {"pool_type": "normal", "source_symbol": "ETHUSDT", "spot_symbol": "ETHUSDT", "futures_symbol": "ETHUSDT", "selected": True, "data_ready": False},
            {"pool_type": "alpha", "source_symbol": "ALPHA_1USDT", "spot_symbol": "ALPHA_1USDT", "futures_symbol": "AKEUSDT", "selected": True, "data_ready": True},
        ])

        health = db.fetch_market_data_health()

        self.assertEqual(health["normal"]["selected"], 2)
        self.assertEqual(health["normal"]["ready"], 1)
        self.assertEqual(health["normal"]["unready"], 1)
        self.assertEqual(health["alpha"]["limit"], 80)


if __name__ == "__main__":
    unittest.main()
