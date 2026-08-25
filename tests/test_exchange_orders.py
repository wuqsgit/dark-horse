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
        self.assertTrue(captured["reduceOnly"])

    def test_hedge_position_close_sends_position_side_without_reduce_only(self):
        exchange = object.__new__(BinanceFutures)
        exchange.adjust_quantity = lambda symbol, quantity: quantity
        captured = {}
        exchange._request = lambda method, path, signed=False, params=None: captured.update(params) or {}

        exchange.close_position_market(
            "ETHUSDT",
            "BUY",
            0.2,
            position_side="SHORT",
        )

        self.assertEqual(captured["positionSide"], "SHORT")
        self.assertNotIn("reduceOnly", captured)
        self.assertEqual(captured["newOrderRespType"], "RESULT")


if __name__ == "__main__":
    unittest.main()
