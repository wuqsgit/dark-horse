from datetime import datetime, timedelta, timezone
import unittest

from shared.market_universe import (
    CandleState,
    assess_dual_market_readiness,
    build_alpha_universe,
    build_normal_universe,
)


class MarketUniverseTest(unittest.TestCase):
    def test_normal_pool_requires_both_markets_and_ranks_by_weaker_volume(self):
        spot = {
        "AAAUSDT": {"status": "TRADING", "quote_volume": 1_000_000},
        "BBBUSDT": {"status": "TRADING", "quote_volume": 800_000},
        "SPOTONLYUSDT": {"status": "TRADING", "quote_volume": 9_000_000},
    }
        futures = {
        "AAAUSDT": {"status": "TRADING", "contract_type": "PERPETUAL", "quote_volume": 500_000},
        "BBBUSDT": {"status": "TRADING", "contract_type": "PERPETUAL", "quote_volume": 900_000},
        "FUTONLYUSDT": {"status": "TRADING", "contract_type": "PERPETUAL", "quote_volume": 9_000_000},
    }

        rows = build_normal_universe(spot, futures, limit=2)

        self.assertEqual([row["futures_symbol"] for row in rows], ["BBBUSDT", "AAAUSDT"])
        self.assertEqual(rows[0]["effective_quote_volume_24h"], 800_000)
        self.assertTrue(all(row["selected"] for row in rows))


    def test_alpha_pool_requires_mapping_and_futures_volume_floor(self):
        alpha = [
        {"alpha_symbol": "ALPHA_1USDT", "futures_symbol": "AAAUSDT", "volume_24h": 900_000},
        {"alpha_symbol": "ALPHA_2USDT", "futures_symbol": "THINUSDT", "volume_24h": 2_000_000},
        {"alpha_symbol": "ALPHA_3USDT", "futures_symbol": None, "volume_24h": 3_000_000},
    ]
        futures = {
        "AAAUSDT": {"status": "TRADING", "contract_type": "PERPETUAL", "quote_volume": 500_000},
        "THINUSDT": {"status": "TRADING", "contract_type": "PERPETUAL", "quote_volume": 99_999},
    }

        rows = build_alpha_universe(alpha, futures, limit=80, futures_volume_floor=100_000)

        self.assertEqual([row["source_symbol"] for row in rows], ["ALPHA_1USDT"])


    def test_readiness_rejects_stale_or_short_market(self):
        now = datetime(2026, 7, 10, 4, 0, tzinfo=timezone.utc)
        spot = CandleState(now - timedelta(minutes=5), now - timedelta(minutes=30), 40, 60)
        futures = CandleState(now - timedelta(minutes=5), now - timedelta(minutes=30), 40, 47)

        result = assess_dual_market_readiness(now, spot, futures)

        self.assertFalse(result.ready)
        self.assertEqual(result.error, "futures_1h_count")


    def test_readiness_accepts_complete_fresh_markets(self):
        now = datetime(2026, 7, 10, 4, 0, tzinfo=timezone.utc)
        state = CandleState(now - timedelta(minutes=5), now - timedelta(minutes=30), 40, 60)

        result = assess_dual_market_readiness(now, state, state)

        self.assertTrue(result.ready)
        self.assertIsNone(result.error)


if __name__ == "__main__":
    unittest.main()
