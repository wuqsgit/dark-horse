import unittest

from alpha_pipeline.collector import AlphaCollector


class AlphaDualMarketCollectorTest(unittest.TestCase):
    def test_mapped_futures_tables_never_use_normal_spot_tables(self):
        self.assertEqual(AlphaCollector.futures_table_for_interval("15m"), "futures_candles_15m")
        self.assertEqual(AlphaCollector.futures_table_for_interval("1h"), "futures_candles_1h")
        self.assertNotEqual(AlphaCollector.futures_table_for_interval("1h"), "candles_1h")


if __name__ == "__main__":
    unittest.main()
