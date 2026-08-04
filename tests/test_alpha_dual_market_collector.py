import asyncio
import unittest

from alpha_pipeline.collector import AlphaCollector


class AlphaDualMarketCollectorTest(unittest.TestCase):
    def test_mapped_futures_tables_never_use_normal_spot_tables(self):
        self.assertEqual(AlphaCollector.futures_table_for_interval("15m"), "futures_candles_15m")
        self.assertEqual(AlphaCollector.futures_table_for_interval("1h"), "futures_candles_1h")
        self.assertNotEqual(AlphaCollector.futures_table_for_interval("1h"), "candles_1h")

    def test_collector_uses_explicit_testnet_market_data_base(self):
        collector = AlphaCollector(market_env="testnet")
        try:
            self.assertEqual(collector.market_env, "testnet")
            self.assertEqual(
                collector.futures_base,
                "https://testnet.binancefuture.com",
            )
        finally:
            asyncio.run(collector.close())

    def test_extended_kline_row_keeps_taker_volume_env_and_closed_state(self):
        kline = [
            1_000, "1.0", "1.2", "0.9", "1.1", "10",
            1_999, "1000", 20, "5", "580", "0",
        ]

        row = AlphaCollector.normalize_kline_row(
            "AKEUSDT",
            kline,
            market_env="testnet",
            now_ms=2_000,
        )

        self.assertEqual(row[0], "1970-01-01T00:00:01Z")
        self.assertEqual(row[1], "AKEUSDT")
        self.assertEqual(row[9], 580.0)
        self.assertEqual(row[10], "testnet")
        self.assertEqual(row[11], 1)


if __name__ == "__main__":
    unittest.main()
