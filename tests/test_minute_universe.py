import unittest
from unittest.mock import patch

from minute_pipeline.universe import load_minute_universe


class MinuteUniverseTest(unittest.TestCase):
    def test_stale_coin_margined_position_is_not_sent_to_usdt_collector(self):
        normal = [{
            "source_symbol": "BTCUSDT",
            "spot_symbol": "BTCUSDT",
            "futures_symbol": "BTCUSDT",
            "selected": 1,
            "forced_position": 0,
        }]

        with patch(
            "minute_pipeline.universe.fetch_market_universe",
            side_effect=[normal, []],
        ), patch(
            "minute_pipeline.universe.fetch_tracked_position_symbols",
            return_value={"ETHUSDT", "WLDUSD_PERP"},
        ):
            universe = load_minute_universe()

        self.assertEqual(universe.spot, ("BTCUSDT",))
        self.assertEqual(universe.futures, ("BTCUSDT", "ETHUSDT"))
        self.assertNotIn("WLDUSD_PERP", universe.futures)


if __name__ == "__main__":
    unittest.main()
