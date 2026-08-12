import unittest
from datetime import datetime, timedelta, timezone

from minute_pipeline.aggregation import (
    aggregate_minutes,
    bucket_start,
)
from minute_pipeline.collectors import normalize_ws_message


def minute_row(index, start=None):
    start = start or datetime(2026, 8, 7, 0, 0, tzinfo=timezone.utc)
    price = 100 + index
    return {
        "time": (start + timedelta(minutes=index)).isoformat().replace(
            "+00:00",
            "Z",
        ),
        "symbol": "BTCUSDT",
        "open": price,
        "high": price + 2,
        "low": price - 1,
        "close": price + 1,
        "volume": 10,
        "quote_vol": 1000,
        "trades": 5,
        "taker_buy_quote_vol": 600,
        "is_closed": True,
    }


class MinuteAggregationTest(unittest.TestCase):
    def test_aggregates_complete_15m_bucket(self):
        rows = [minute_row(index) for index in range(15)]
        result = aggregate_minutes(
            rows,
            market_kind="spot",
            source_env="mainnet",
            symbol="BTCUSDT",
            interval="15m",
            start_time=rows[0]["time"],
        )

        self.assertEqual(result["open"], 100)
        self.assertEqual(result["high"], 116)
        self.assertEqual(result["low"], 99)
        self.assertEqual(result["close"], 115)
        self.assertEqual(result["volume"], 150)
        self.assertEqual(result["trades"], 75)
        self.assertTrue(result["is_complete"])

    def test_rejects_bucket_with_missing_minute(self):
        rows = [minute_row(index) for index in range(15) if index != 7]
        result = aggregate_minutes(
            rows,
            market_kind="spot",
            source_env="mainnet",
            symbol="BTCUSDT",
            interval="15m",
            start_time=rows[0]["time"],
        )
        self.assertIsNone(result)

    def test_four_hour_and_daily_buckets_use_utc(self):
        value = datetime(2026, 8, 7, 5, 23, tzinfo=timezone.utc)
        self.assertEqual(
            bucket_start(value, "4h"),
            datetime(2026, 8, 7, 4, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(
            bucket_start(value, "1d"),
            datetime(2026, 8, 7, 0, 0, tzinfo=timezone.utc),
        )

    def test_websocket_parser_only_accepts_closed_kline(self):
        payload = {
            "e": "kline",
            "s": "ETHUSDT",
            "k": {
                "t": 1786060800000,
                "o": "100",
                "h": "105",
                "l": "99",
                "c": "104",
                "v": "12",
                "q": "1220",
                "n": 20,
                "Q": "700",
                "x": True,
            },
        }
        row = normalize_ws_message(__import__("json").dumps(payload))
        self.assertEqual(row["symbol"], "ETHUSDT")
        self.assertEqual(row["close"], 104)
        payload["k"]["x"] = False
        self.assertIsNone(
            normalize_ws_message(__import__("json").dumps(payload))
        )


if __name__ == "__main__":
    unittest.main()
