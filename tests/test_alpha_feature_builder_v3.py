import unittest
from datetime import datetime, timedelta, timezone

from alpha_engine.strategy.feature_builder import (
    FEATURE_SCHEMA_VERSION,
    build_alpha_feature_snapshot,
)


def _rows(count, *, start, interval_minutes, start_price=100.0, price_step=0.4):
    rows = []
    for index in range(count):
        opened = start + timedelta(minutes=interval_minutes * index)
        open_price = start_price + price_step * index
        close = open_price + price_step * 0.7
        rows.append(
            {
                "time": opened.isoformat().replace("+00:00", "Z"),
                "open": open_price,
                "high": close + 0.3,
                "low": open_price - 0.2,
                "close": close,
                "quote_vol": 1000 + index * 30,
                "trades": 100 + index,
                "taker_buy_quote_vol": (1000 + index * 30) * 0.58,
                "is_closed": 1,
            }
        )
    return rows


class AlphaFeatureBuilderV3Test(unittest.TestCase):
    def test_builds_deterministic_closed_candle_snapshot(self):
        start = datetime(2026, 7, 27, tzinfo=timezone.utc)
        rows_15m = _rows(40, start=start, interval_minutes=15)
        rows_1h = _rows(30, start=start - timedelta(hours=20), interval_minutes=60)
        cutoff = start + timedelta(hours=10)

        first = build_alpha_feature_snapshot(
            alpha_symbol="AKEALPHAUSDT",
            futures_symbol="AKEUSDT",
            market_env="mainnet",
            cutoff_time=cutoff,
            candles_15m=rows_15m,
            candles_1h=rows_1h,
        )
        second = build_alpha_feature_snapshot(
            alpha_symbol="AKEALPHAUSDT",
            futures_symbol="AKEUSDT",
            market_env="mainnet",
            cutoff_time=cutoff,
            candles_15m=rows_15m,
            candles_1h=rows_1h,
        )

        self.assertEqual(first.snapshot_id, second.snapshot_id)
        self.assertEqual(first.feature_schema_version, FEATURE_SCHEMA_VERSION)
        self.assertEqual(first.market_env, "mainnet")
        self.assertEqual(first.features["higher_lows_8x15m"], 7.0)
        self.assertGreater(first.features["ema20_slope_1h"], 0)
        self.assertGreater(first.features["taker_buy_quote_ratio"], 0.5)
        self.assertEqual(first.quality["status"], "ready")

    def test_excludes_open_and_future_candles(self):
        start = datetime(2026, 7, 27, tzinfo=timezone.utc)
        rows_15m = _rows(40, start=start, interval_minutes=15)
        rows_1h = _rows(30, start=start - timedelta(hours=20), interval_minutes=60)
        rows_15m[-1]["close"] = 9999
        rows_15m[-1]["is_closed"] = 0
        cutoff = start + timedelta(hours=10)

        snapshot = build_alpha_feature_snapshot(
            alpha_symbol="AKEALPHAUSDT",
            futures_symbol="AKEUSDT",
            market_env="mainnet",
            cutoff_time=cutoff,
            candles_15m=rows_15m,
            candles_1h=rows_1h,
        )

        self.assertLess(snapshot.features["current_price"], 200)

    def test_missing_optional_feature_stays_missing_instead_of_zero(self):
        start = datetime(2026, 7, 27, tzinfo=timezone.utc)
        rows_15m = _rows(40, start=start, interval_minutes=15)
        rows_1h = _rows(30, start=start - timedelta(hours=20), interval_minutes=60)
        for row in rows_15m:
            row.pop("taker_buy_quote_vol")

        snapshot = build_alpha_feature_snapshot(
            alpha_symbol="AKEALPHAUSDT",
            futures_symbol="AKEUSDT",
            market_env="mainnet",
            cutoff_time=start + timedelta(hours=10),
            candles_15m=rows_15m,
            candles_1h=rows_1h,
        )

        self.assertIsNone(snapshot.features["taker_buy_quote_ratio"])
        self.assertIn("taker_buy_quote_ratio", snapshot.quality["missing_features"])

    def test_includes_square_sentiment_features(self):
        start = datetime(2026, 7, 27, tzinfo=timezone.utc)
        snapshot = build_alpha_feature_snapshot(
            alpha_symbol="AKEALPHAUSDT",
            futures_symbol="AKEUSDT",
            market_env="mainnet",
            cutoff_time=start + timedelta(hours=10),
            candles_15m=_rows(40, start=start, interval_minutes=15),
            candles_1h=_rows(
                30,
                start=start - timedelta(hours=20),
                interval_minutes=60,
            ),
            square_sentiment={
                "bearish_ratio": 0.85,
                "baseline_bearish_ratio_24h": 0.40,
                "effective_post_count": 24,
                "unique_authors": 20,
                "top3_author_share": 0.15,
                "substantive_risk_count": 0,
                "age_minutes": 4,
            },
            alpha_discovery_score=86,
        )

        self.assertEqual(snapshot.feature_schema_version, 4)
        self.assertEqual(snapshot.features["square_sentiment_available"], 1)
        self.assertAlmostEqual(
            snapshot.features["square_bearish_shift_24h"],
            0.45,
        )
        self.assertEqual(snapshot.features["alpha_discovery_score"], 86)


if __name__ == "__main__":
    unittest.main()
