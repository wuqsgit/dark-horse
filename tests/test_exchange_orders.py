import unittest

from trader.exchange import BinanceFutures


class ExchangeOrderTest(unittest.TestCase):
    def test_reduce_only_market_order_requests_result_response(self):
        exchange = object.__new__(BinanceFutures)
        exchange.adjust_quantity = lambda symbol, quantity: quantity
        captured = {}
        exchange._request = lambda method, path, signed=False, params=None: captured.update(params) or {}

        exchange.place_market_order("B2USDT", "SELL", 2.5, reduce_only=True)

        self.assertEqual(captured["newOrderRespType"], "RESULT")


if __name__ == "__main__":
    unittest.main()
