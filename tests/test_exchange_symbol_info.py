import unittest
from unittest.mock import patch

from trader.exchange import BinanceFutures


class Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "symbols": [{
                "symbol": "BTCUSDT",
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "1000"},
                    {"filterType": "MIN_NOTIONAL", "notional": "5"},
                ],
            }]
        }


class ExchangeSymbolInfoTest(unittest.TestCase):
    def test_reads_min_notional_and_tick_size(self):
        exchange = BinanceFutures.__new__(BinanceFutures)
        exchange.base_rest = "https://example.test"
        exchange.api_key = "key"
        with patch("trader.exchange.httpx.get", return_value=Response()):
            info = exchange.get_symbol_info("BTCUSDT")

        self.assertEqual(info["min_notional"], 5.0)
        self.assertEqual(info["tick_size"], 0.1)


if __name__ == "__main__":
    unittest.main()
