import unittest
from datetime import datetime, timezone

from alpha_engine.strategy.market_data import (
    futures_rest_base,
    is_closed_kline,
    oi_change_at_horizon,
    resolve_market_env,
)


class AlphaStrategyMarketDataTest(unittest.TestCase):
    def test_market_environment_is_explicit_and_has_matching_base_url(self):
        self.assertEqual(resolve_market_env("TESTNET"), "testnet")
        self.assertEqual(resolve_market_env("mainnet"), "mainnet")
        self.assertEqual(
            futures_rest_base("testnet"),
            "https://testnet.binancefuture.com",
        )
        self.assertEqual(futures_rest_base("mainnet"), "https://fapi.binance.com")

    def test_unknown_market_environment_is_rejected(self):
        with self.assertRaises(ValueError):
            resolve_market_env("paper")

    def test_kline_is_closed_from_exchange_close_time(self):
        now_ms = 1_800_000
        closed = [0, "1", "2", "0.5", "1.5", "10", 1_799_999]
        open_bar = [0, "1", "2", "0.5", "1.5", "10", 1_800_001]

        self.assertTrue(is_closed_kline(closed, now_ms=now_ms))
        self.assertFalse(is_closed_kline(open_bar, now_ms=now_ms))

    def test_oi_change_uses_timestamps_not_row_offsets(self):
        rows = [
            {"time": "2026-07-28T00:00:00Z", "open_interest": 100},
            {"time": "2026-07-28T00:10:00Z", "open_interest": 110},
            {"time": "2026-07-28T03:50:00Z", "open_interest": 120},
            {"time": "2026-07-28T04:00:00Z", "open_interest": 125},
        ]

        result = oi_change_at_horizon(
            rows,
            hours=4,
            as_of=datetime(2026, 7, 28, 4, 0, tzinfo=timezone.utc),
        )

        self.assertAlmostEqual(result, 0.25)

    def test_oi_change_is_missing_without_a_sample_at_the_horizon(self):
        rows = [
            {"time": "2026-07-28T03:50:00Z", "open_interest": 120},
            {"time": "2026-07-28T04:00:00Z", "open_interest": 125},
        ]

        self.assertIsNone(
            oi_change_at_horizon(
                rows,
                hours=4,
                as_of=datetime(2026, 7, 28, 4, 0, tzinfo=timezone.utc),
            )
        )


if __name__ == "__main__":
    unittest.main()
