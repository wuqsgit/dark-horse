import unittest

from pipeline.binance_http import BinanceHTTPCollector


class BinanceDualMarketCollectorTest(unittest.TestCase):
    def test_kline_endpoints_are_market_specific_and_warm_ema50(self):
        spot_url, spot_params = BinanceHTTPCollector.kline_request("spot", "BTCUSDT", "1h")
        futures_url, futures_params = BinanceHTTPCollector.kline_request("futures", "BTCUSDT", "1h")

        self.assertEqual(spot_url, "https://api.binance.com/api/v3/klines")
        self.assertEqual(futures_url, "https://fapi.binance.com/fapi/v1/klines")
        self.assertEqual(spot_params["limit"], 72)
        self.assertEqual(futures_params["limit"], 72)

    def test_non_hourly_intervals_keep_the_default_window(self):
        _, params = BinanceHTTPCollector.kline_request("spot", "BTCUSDT", "15m")

        self.assertEqual(params["limit"], 48)

    def test_configured_data_endpoints_are_used(self):
        spot_url, _ = BinanceHTTPCollector.kline_request(
            "spot",
            "BTCUSDT",
            "1h",
            spot_base_url="https://data-api.binance.vision/",
        )
        futures_url, _ = BinanceHTTPCollector.kline_request(
            "futures",
            "BTCUSDT",
            "1h",
            futures_base_url="https://testnet.binancefuture.com/",
        )

        self.assertEqual(
            spot_url,
            "https://data-api.binance.vision/api/v3/klines",
        )
        self.assertEqual(
            futures_url,
            "https://testnet.binancefuture.com/fapi/v1/klines",
        )

    def test_invalid_futures_data_environment_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "mainnet or testnet"):
            BinanceHTTPCollector(futures_source_env="paper")


if __name__ == "__main__":
    unittest.main()
