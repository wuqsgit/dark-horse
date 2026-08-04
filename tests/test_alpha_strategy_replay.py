import unittest
from datetime import datetime, timedelta, timezone

from backtest.alpha_strategy_v2.labels import label_counterfactual_path
from backtest.alpha_strategy_v2.replay import AlphaStrategyReplay


def _candles(count, start, minutes, *, step=0.12):
    rows = []
    for index in range(count):
        open_price = 100 + index * step
        close = open_price + step * 0.6
        rows.append(
            {
                "time": (
                    start + timedelta(minutes=index * minutes)
                ).isoformat().replace("+00:00", "Z"),
                "open": open_price,
                "high": close + 0.10,
                "low": open_price - 0.08,
                "close": close,
                "quote_vol": 1000 + index * 15,
                "trades": 100 + index,
                "taker_buy_quote_vol": (1000 + index * 15) * 0.55,
                "is_closed": 1,
            }
        )
    return rows


class AlphaStrategyReplayTest(unittest.TestCase):
    def test_future_candle_change_does_not_change_earlier_features(self):
        start = datetime(2026, 7, 10, tzinfo=timezone.utc)
        c15 = _candles(72, start, 15)
        c1h = _candles(50, start - timedelta(days=2), 60)
        replay = AlphaStrategyReplay()

        baseline = replay.run(
            alpha_symbol="AKEALPHAUSDT",
            futures_symbol="AKEUSDT",
            market_env="mainnet",
            candles_15m=c15,
            candles_1h=c1h,
            market_context={"btc_ret_1h": 999},
        )
        changed = [dict(row) for row in c15]
        changed[-1] = {
            **changed[-1],
            "open": 5000,
            "high": 7000,
            "low": 4000,
            "close": 6500,
            "quote_vol": 999_999_999,
        }
        replayed = replay.run(
            alpha_symbol="AKEALPHAUSDT",
            futures_symbol="AKEUSDT",
            market_env="mainnet",
            candles_15m=changed,
            candles_1h=c1h,
            market_context={"btc_ret_1h": 999},
        )

        self.assertGreater(len(baseline["rows"]), 1)
        earlier = baseline["rows"][0]
        same_time = next(
            row for row in replayed["rows"]
            if row["candle_close_time"] == earlier["candle_close_time"]
        )
        self.assertEqual(earlier["snapshot_id"], same_time["snapshot_id"])
        self.assertIsNone(earlier["features"]["btc_ret_1h"])

    def test_same_bar_target_and_stop_uses_adverse_priority(self):
        start = datetime(2026, 7, 10, tzinfo=timezone.utc)
        candles = [
            {
                "time": start.isoformat().replace("+00:00", "Z"),
                "open": 100,
                "high": 104,
                "low": 97,
                "close": 103,
            }
        ] + [
            {
                "time": (
                    start + timedelta(minutes=15 * index)
                ).isoformat().replace("+00:00", "Z"),
                "open": 101,
                "high": 102,
                "low": 100,
                "close": 101,
            }
            for index in range(1, 32)
        ]

        label = label_counterfactual_path(
            stage="setup",
            entry_price=100,
            invalidation_price=98,
            breakout_level=101,
            candles=candles,
        )

        self.assertEqual(label["first_event"], "minus_1r")
        self.assertEqual(label["followthrough"], 0)
        self.assertIsNotNone(label["mfe_8h_r"])
        self.assertIsNotNone(label["mae_30m_r"])


if __name__ == "__main__":
    unittest.main()
