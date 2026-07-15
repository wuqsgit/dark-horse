import os
import sqlite3
import tempfile
import unittest

from ai_service.storage import AIStore
from shared.strategy_insights import generate_strategy_insights


def add_ai_sample(store, *, symbol, label, futures_volume, trend_score, spread=0.0008):
    hour = (len(symbol) + label) % 23
    sample_id, _ = store.add_sample({
        "model_key": "alpha",
        "symbol": symbol,
        "side": "LONG",
        "template": "alpha_trend_confirmed",
        "category": "alpha",
        "observed_at": f"2026-07-14T{hour:02d}:05:00Z",
        "entry_price": 1.0,
        "stop_pct": 0.05,
        "features": {
            "futures_volume_growth_6h": futures_volume,
            "trend_score": trend_score,
            "spread_pct": spread,
            "pullback_from_high_pct": 0.02,
        },
    })
    store.set_sample_label(
        sample_id,
        label=label,
        first_event="plus_1r" if label else "minus_1r",
        mfe_r=1.4 if label else 0.2,
        mae_r=-0.2 if label else -1.1,
    )


class StrategyInsightsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.main_db = os.path.join(self.tmp.name, "main.db")
        self.ai_db = os.path.join(self.tmp.name, "ai.db")
        self.ai_store = AIStore(self.ai_db)
        conn = sqlite3.connect(self.main_db)
        conn.executescript(
            """
            CREATE TABLE trade_entry_reviews (
                position_trade_id TEXT PRIMARY KEY,
                symbol TEXT,
                strategy_source TEXT,
                category TEXT,
                entry_template TEXT,
                review_label TEXT,
                net_pnl REAL,
                max_favorable_return REAL,
                max_adverse_return REAL
            );
            CREATE TABLE trade_exit_reviews (
                position_trade_id TEXT PRIMARY KEY,
                symbol TEXT,
                strategy_source TEXT,
                category TEXT,
                review_label TEXT,
                net_pnl REAL,
                max_favorable_return REAL,
                max_adverse_return REAL
            );
            """
        )
        conn.executemany(
            """INSERT INTO trade_entry_reviews
               VALUES (?, ?, 'alpha', 'alpha', 'alpha_trend_probe', ?, ?, ?, ?)""",
            [
                ("p1", "AKEUSDT", "bad_condition", -4.0, 0.01, -0.08),
                ("p2", "UBUSDT", "bad_condition", -5.0, 0.01, -0.09),
                ("p3", "RIVERUSDT", "reasonable", 2.0, 0.05, -0.02),
                ("p4", "BSBUSDT", "bad_condition", -3.0, 0.02, -0.06),
                ("p5", "TAUSDT", "reasonable", 1.5, 0.04, -0.02),
            ],
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_generates_actionable_ai_and_trade_insights(self):
        for idx in range(4):
            add_ai_sample(self.ai_store, symbol=f"WIN{idx}USDT", label=1, futures_volume=4.0 + idx, trend_score=82 + idx)
        for idx in range(3):
            add_ai_sample(self.ai_store, symbol=f"LOSS{idx}USDT", label=0, futures_volume=1.0, trend_score=62 + idx)

        result = generate_strategy_insights(self.main_db, self.ai_db, min_ai_samples=5, min_trade_samples=5)

        self.assertGreaterEqual(len(result["insights"]), 2)
        ai_item = next(item for item in result["insights"] if item["source"] == "ai_candidates")
        self.assertEqual(ai_item["category"], "alpha")
        self.assertIn("futures_volume_growth_6h", ai_item["key_metrics"])
        self.assertIn("trend_score", ai_item["key_metrics"])
        self.assertIn("合约同步放量", ai_item["recommendation"])

        trade_item = next(item for item in result["insights"] if item["source"] == "real_trades")
        self.assertTrue(trade_item["priority"])
        self.assertIn("representative_cases", trade_item)
        self.assertIn("代表样本动作", trade_item["recommendation"])

    def test_trade_insights_include_symbol_specific_actions(self):
        conn = sqlite3.connect(self.main_db)
        conn.execute("DELETE FROM trade_entry_reviews")
        conn.executemany(
            """INSERT INTO trade_entry_reviews
               VALUES (?, ?, 'normal', 'discovery', NULL, ?, ?, ?, ?)""",
            [
                ("bsb-loss", "BSBUSDT", "pending", -40.0, 0.128, -0.145),
                ("jct-loss", "JCTUSDT", "pending", -60.0, 0.02, -0.12),
                ("jct-loss-2", "JCTUSDT", "pending", -70.0, 0.02, -0.12),
                ("b2-loss", "B2USDT", "pending", -20.0, 0.03, -0.08),
                ("ok1", "OK1USDT", "reasonable", 2.0, 0.04, -0.01),
                ("ok2", "OK2USDT", "reasonable", 1.5, 0.03, -0.01),
            ],
        )
        conn.commit()
        conn.close()

        result = generate_strategy_insights(self.main_db, self.ai_db, min_trade_samples=5)

        item = next(
            insight for insight in result["insights"]
            if insight["source"] == "real_trades"
            and insight["strategy_source"] == "normal"
            and insight["category"] == "discovery"
        )
        self.assertIn("discovery", item["recommendation"])
        self.assertIn("representative_cases", item)
        case_symbols = [case["symbol"] for case in item["representative_cases"]]
        self.assertEqual(len(case_symbols), len(set(case_symbols)))
        bsb_case = next(case for case in item["representative_cases"] if case["symbol"] == "BSBUSDT")
        self.assertIn("BSBUSDT", bsb_case["action"])
        self.assertIn("MFE", bsb_case["diagnosis"])
        self.assertIn("保护止损", bsb_case["action"])


if __name__ == "__main__":
    unittest.main()
