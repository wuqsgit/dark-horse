import unittest

from trader.exchange import BinanceFutures
from trader.execution import ExecutionEngine


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
        exchange._hedge_mode = True
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

    def test_one_way_close_omits_directional_position_side(self):
        exchange = object.__new__(BinanceFutures)
        exchange.adjust_quantity = lambda symbol, quantity: quantity
        exchange._hedge_mode = False
        captured = {}
        exchange._request = lambda method, path, signed=False, params=None: captured.update(params) or {}

        exchange.close_position_market(
            "ETHUSDT",
            "SELL",
            0.2,
            position_side="LONG",
        )

        self.assertNotIn("positionSide", captured)
        self.assertTrue(captured["reduceOnly"])

    def test_execution_passes_position_direction_to_market_orders(self):
        class Exchange:
            def __init__(self):
                self.calls = []

            def place_market_order(
                self, symbol, side, quantity, *, client_order_id=None,
                position_side=None,
            ):
                self.calls.append(("open", symbol, side, quantity, position_side))
                return {"orderId": "open-1"}

            def close_position_market(
                self, symbol, side, quantity, *, client_order_id=None,
                position_side=None,
            ):
                self.calls.append(("close", symbol, side, quantity, position_side))
                return {"orderId": "close-1"}

        exchange = Exchange()
        engine = ExecutionEngine(exchange)
        action = {
            "symbol": "AKEUSDT",
            "side": "SELL",
            "quantity": 3621,
            "position_side": "LONG",
        }

        engine._place_market_action_order(action)
        engine._place_market_action_order(action, reduce_only=True)

        self.assertEqual(
            exchange.calls,
            [
                ("open", "AKEUSDT", "SELL", 3621, "LONG"),
                ("close", "AKEUSDT", "SELL", 3621, "LONG"),
            ],
        )

    def test_close_infers_hedge_direction_from_selected_position(self):
        class Exchange:
            def __init__(self):
                self.position_side = None

            def close_position_market(
                self, symbol, side, quantity, *, client_order_id=None,
                position_side=None,
            ):
                self.position_side = position_side
                return {"orderId": "close-1"}

        exchange = Exchange()
        engine = ExecutionEngine(exchange)

        engine._place_market_action_order(
            {"symbol": "AKEUSDT", "side": "SELL", "quantity": 3621},
            reduce_only=True,
            position={"symbol": "AKEUSDT", "positionSide": "LONG", "side": "LONG"},
        )

        self.assertEqual(exchange.position_side, "LONG")


if __name__ == "__main__":
    unittest.main()
