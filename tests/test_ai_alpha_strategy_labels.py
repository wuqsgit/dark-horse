import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ai_service.alpha_labels import AlphaStrategyLabeler
from ai_service.storage import AIStore


class AIAlphaStrategyLabelerTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.market_db = root / "market.db"
        self.store = AIStore(root / "ai.db")
        conn = sqlite3.connect(self.market_db)
        conn.execute(
            """CREATE TABLE futures_candles_15m (
                 time TEXT, symbol TEXT, open REAL, high REAL, low REAL,
                 close REAL, source_env TEXT, is_closed INTEGER
               )"""
        )
        start = datetime(2026, 7, 20, tzinfo=timezone.utc)
        rows = []
        for index in range(32):
            price = 100 + index * 0.2
            rows.append(
                (
                    (start + timedelta(minutes=15 * index))
                    .isoformat()
                    .replace("+00:00", "Z"),
                    "AKEUSDT",
                    price,
                    price + 0.8,
                    price - 0.2,
                    price + 0.5,
                    "mainnet",
                    1,
            )
        )
        conn.execute(
            """CREATE TABLE orders (
                 account_id INTEGER, signal_event_id TEXT, alpha_stage TEXT,
                 setup_id TEXT, symbol TEXT, position_id TEXT,
                 exchange_order_id TEXT, quantity REAL, price REAL,
                 ai_model_versions_json TEXT, created_at TEXT,
                 order_type TEXT
               )"""
        )
        conn.execute(
            """CREATE TABLE alpha_signal_events (
                 event_id TEXT, invalidation_price REAL
               )"""
        )
        conn.execute(
            """CREATE TABLE trades (
                 account_id INTEGER, position_id TEXT, pnl REAL,
                 exit_reason TEXT, created_at TEXT
               )"""
        )
        conn.executemany(
            "INSERT INTO futures_candles_15m VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
        conn.close()
        self.start = start

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_labels_pending_counterfactual_sample(self):
        self.store.add_alpha_strategy_sample(
            {
                "request_id": "ake-label-1",
                "market_env": "mainnet",
                "model_key": "alpha_setup_v1_mainnet",
                "futures_symbol": "AKEUSDT",
                "alpha_symbol": "AKEALPHAUSDT",
                "stage": "setup",
                "setup_type": "accumulation",
                "candle_close_time": self.start.isoformat().replace(
                    "+00:00",
                    "Z",
                ),
                "feature_schema_version": 3,
                "features": {
                    "current_price": 100,
                    "base_low_2h": 98,
                    "breakout_level": 101,
                },
                "feature_quality": {"status": "ready"},
            }
        )
        labeler = AlphaStrategyLabeler(
            self.store,
            self.market_db,
            now_fn=lambda: self.start + timedelta(hours=12),
        )

        result = labeler.label_pending(market_env="mainnet")

        self.assertEqual(result["labeled"], 1)
        samples = self.store.labeled_alpha_strategy_samples(
            model_key="alpha_setup_v1_mainnet",
            target="setup_success",
        )
        self.assertEqual(len(samples), 1)
        self.assertIn("mfe_8h_r", samples[0]["labels"])

    def test_syncs_real_execution_outcome_for_calibration(self):
        conn = sqlite3.connect(self.market_db)
        conn.execute(
            "INSERT INTO alpha_signal_events VALUES ('event-1', 0.95)"
        )
        conn.execute(
            """INSERT INTO orders VALUES
               (7,'event-1','PROBE_LONG','setup-1','AKEUSDT','position-1',
                '9001',100,1.0,'{"trigger":"v1"}',
                '2026-07-20T00:00:00Z','MARKET')"""
        )
        conn.execute(
            """INSERT INTO trades VALUES
               (7,'position-1',8.0,'take_profit','2026-07-21T00:00:00Z')"""
        )
        conn.commit()
        conn.close()
        labeler = AlphaStrategyLabeler(self.store, self.market_db)

        result = labeler.sync_execution_outcomes()
        summary = self.store.alpha_strategy_execution_summary()

        self.assertEqual(result, {"synced": 1, "status": "ok"})
        self.assertEqual(summary["closed"], 1)
        self.assertGreater(summary["mean_realized_r"], 0)


if __name__ == "__main__":
    unittest.main()
