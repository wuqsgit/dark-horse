import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import shared.db as db


class MarketReadinessGateTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(db, "DB_PATH", str(Path(self.temp_dir.name) / "market.db"))
        self.path_patch.start()
        db.init_db()

    def tearDown(self):
        self.path_patch.stop()
        self.temp_dir.cleanup()

    def test_active_symbols_only_include_selected_ready_normal_rows(self):
        db.upsert_market_universe([
            {"pool_type": "normal", "source_symbol": "BTCUSDT", "spot_symbol": "BTCUSDT", "futures_symbol": "BTCUSDT", "selected": True, "data_ready": True},
            {"pool_type": "normal", "source_symbol": "OLDUSDT", "spot_symbol": "OLDUSDT", "futures_symbol": "OLDUSDT", "selected": True, "data_ready": False},
        ])

        self.assertEqual(db.fetch_active_symbols(), ["BTCUSDT"])

    def test_alpha_entry_checks_source_mapping_readiness(self):
        db.upsert_market_universe([
            {"pool_type": "alpha", "source_symbol": "ALPHA_1USDT", "spot_symbol": "ALPHA_1USDT", "futures_symbol": "AKEUSDT", "selected": True, "data_ready": False, "data_error": "spot_15m_age"},
        ])

        ready, error = db.is_market_entry_ready("AKEUSDT", "alpha", "ALPHA_1USDT")

        self.assertFalse(ready)
        self.assertEqual(error, "spot_15m_age")


if __name__ == "__main__":
    unittest.main()
