import unittest
from unittest.mock import patch

from trader.execution import ExecutionEngine


class ReconciliationExchange:
    def __init__(self):
        self.events = []
        self.orders = [
            {
                "algoId": "old-eth-1",
                "orderType": "STOP_MARKET",
                "symbol": "ETHUSDT",
                "side": "SELL",
                "quantity": "0.63",
                "triggerPrice": "1848.66",
            },
            {
                "algoId": "old-eth-2",
                "orderType": "STOP_MARKET",
                "symbol": "ETHUSDT",
                "side": "SELL",
                "quantity": "0.669",
                "triggerPrice": "1836.42",
            },
            {
                "algoId": "orphan-link",
                "orderType": "STOP_MARKET",
                "symbol": "LINKUSDT",
                "side": "SELL",
                "quantity": "10",
                "triggerPrice": "8.0",
            },
        ]

    def get_open_protective_stops(self, symbol=None):
        return [
            order
            for order in self.orders
            if symbol is None or order["symbol"] == symbol
        ]

    def cancel_other_protective_stops(self, symbol, keep_order_id=None):
        cancelled = [
            order
            for order in self.orders
            if order["symbol"] == symbol
            and str(order["algoId"]) != str(keep_order_id)
        ]
        self.events.append(("cancel", symbol, keep_order_id, len(cancelled)))
        self.orders = [order for order in self.orders if order not in cancelled]
        return len(cancelled)

    def place_stop_order(self, symbol, side, quantity, stop_price):
        self.events.append(("place", symbol, side, quantity, stop_price))
        order = {
            "algoId": "new-eth",
            "orderType": "STOP_MARKET",
            "symbol": symbol,
            "side": side,
            "quantity": str(quantity),
            "triggerPrice": str(stop_price),
        }
        self.orders.append(order)
        return order


class ProtectiveStopReconciliationTest(unittest.TestCase):
    def test_hedge_reconciliation_keeps_separate_long_and_short_stops(self):
        class HedgeExchange:
            def __init__(self):
                self.cancelled = []
                self.orders = [
                    {
                        "algoId": "long-stop", "orderType": "STOP_MARKET",
                        "symbol": "AKEUSDT", "positionSide": "LONG",
                        "side": "SELL", "quantity": "1", "triggerPrice": "90",
                    },
                    {
                        "algoId": "short-stop", "orderType": "STOP_MARKET",
                        "symbol": "AKEUSDT", "positionSide": "SHORT",
                        "side": "BUY", "quantity": "1", "triggerPrice": "110",
                    },
                ]

            def get_open_protective_stops(self, symbol=None):
                return list(self.orders)

            def cancel_other_protective_stops(
                self, symbol, keep_order_id=None, position_side=None,
            ):
                self.cancelled.append((symbol, keep_order_id, position_side))
                return 0

            def place_stop_order(
                self, symbol, side, quantity, stop_price, *, position_side=None,
            ):
                raise AssertionError("matching hedge stops must be reused")

        exchange = HedgeExchange()
        engine = ExecutionEngine(exchange)
        positions = [
            {
                "symbol": "AKEUSDT", "positionSide": "LONG", "side": "LONG",
                "quantity": 1, "entry_price": 100, "mark_price": 100,
            },
            {
                "symbol": "AKEUSDT", "positionSide": "SHORT", "side": "SHORT",
                "quantity": 1, "entry_price": 100, "mark_price": 100,
            },
        ]

        with patch("shared.db.get_position_history", return_value={}), \
             patch("shared.db.update_position_management"):
            result = engine.reconcile_exchange_protective_stops(positions)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            exchange.cancelled,
            [
                ("AKEUSDT", "long-stop", "LONG"),
                ("AKEUSDT", "short-stop", "SHORT"),
            ],
        )

    def test_repairs_active_quantity_and_removes_orphans(self):
        exchange = ReconciliationExchange()
        engine = ExecutionEngine(exchange)
        positions = [{
            "symbol": "ETHUSDT",
            "side": "LONG",
            "quantity": 0.266,
            "entry_price": 1886.88,
            "mark_price": 1894.44,
        }]
        history = {
            "tp1_hit": 1,
            "current_stop_loss": 1880.73,
        }

        with patch("shared.db.get_position_history", return_value=history), \
             patch("shared.db.update_position_management") as update:
            result = engine.reconcile_exchange_protective_stops(positions)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["orphaned"], 1)
        self.assertEqual(result["repaired"], 1)
        place = next(event for event in exchange.events if event[0] == "place")
        self.assertEqual(place[3], 0.266)
        self.assertAlmostEqual(place[4], 1886.88 * 1.0015)
        self.assertEqual(
            [order["symbol"] for order in exchange.orders],
            ["ETHUSDT"],
        )
        self.assertEqual(exchange.orders[0]["quantity"], "0.266")
        self.assertAlmostEqual(update.call_args.kwargs["protected_stop"], place[4])


if __name__ == "__main__":
    unittest.main()
