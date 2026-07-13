import unittest
from unittest.mock import patch

from trader.execution import ExecutionEngine


class ResidualExchange:
    def __init__(self, quantity, mark_price, leverage, min_notional, first_fill_ratio=1.0):
        self.quantity = quantity
        self.mark_price = mark_price
        self.leverage = leverage
        self.min_notional = min_notional
        self.closed_quantity = None
        self.closed_quantities = []
        self.first_fill_ratio = first_fill_ratio

    def get_positions(self):
        return [{
            "symbol": "TINYUSDT", "side": "LONG", "quantity": self.quantity,
            "entry_price": self.mark_price, "mark_price": self.mark_price,
            "unrealized_pnl": 0, "leverage": self.leverage,
        }]

    def get_symbol_info(self, symbol):
        return {
            "step_size": 0.01, "min_qty": 0.01,
            "min_notional": self.min_notional,
        }

    def close_position_market(self, symbol, side, quantity):
        ratio = self.first_fill_ratio if not self.closed_quantities else 1.0
        executed = min(self.quantity, quantity * ratio)
        self.closed_quantity = quantity
        self.closed_quantities.append(executed)
        self.quantity = max(0, self.quantity - executed)
        return {"orderId": "close-1", "executedQty": str(executed), "avgPrice": str(self.mark_price)}


class ResidualPositionCleanupTest(unittest.TestCase):
    def _execute(self, exchange, close_pct):
        engine = ExecutionEngine(exchange)
        engine._record_decision = lambda *args, **kwargs: None
        results = []
        with patch("shared.db.get_position_history", return_value={}), \
             patch("shared.db.record_trade") as record, \
             patch("shared.db.delete_position_history"):
            engine._execute_partial_close({
                "action": "partial_close", "symbol": "TINYUSDT", "side": "SELL",
                "close_pct": close_pct, "reason": "TP1",
            }, results)
        return results, record

    def test_remaining_margin_below_five_usdt_is_fully_closed(self):
        exchange = ResidualExchange(quantity=3, mark_price=3.77, leverage=2, min_notional=0.1)
        results, record = self._execute(exchange, 0.25)

        self.assertEqual(exchange.closed_quantity, 3)
        self.assertEqual(record.call_args.kwargs["qty"], 3)
        self.assertEqual(record.call_args.kwargs["exit_reason"], "residual_position_cleanup")
        self.assertEqual(results[0]["reason"], "residual_position_cleanup")

    def test_remaining_notional_below_exchange_buffer_is_fully_closed(self):
        exchange = ResidualExchange(quantity=10, mark_price=6, leverage=1, min_notional=5)
        self._execute(exchange, 0.9)

        self.assertEqual(exchange.closed_quantity, 10)

    def test_underfilled_cleanup_rechecks_and_closes_exchange_remainder(self):
        exchange = ResidualExchange(
            quantity=3, mark_price=3.77, leverage=2,
            min_notional=0.1, first_fill_ratio=0.5,
        )
        results, record = self._execute(exchange, 0.25)

        self.assertEqual(exchange.quantity, 0)
        self.assertEqual(exchange.closed_quantities, [1.5, 1.5])
        self.assertEqual(record.call_args.kwargs["qty"], 3)
        self.assertEqual(results[0]["status"], "ok")

    def test_current_position_below_five_usdt_margin_plans_full_close(self):
        exchange = ResidualExchange(quantity=2, mark_price=3.77, leverage=2, min_notional=0.1)
        engine = ExecutionEngine(exchange)
        engine._record_decision = lambda *args, **kwargs: None
        position = exchange.get_positions()[0]

        with patch("shared.db.get_position_history", return_value={}):
            actions = engine._build_position_actions([], [position], run_id="run1")

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["action"], "close")
        self.assertEqual(actions[0]["side"], "SELL")
        self.assertEqual(actions[0]["reason"], "residual_position_cleanup")

    def test_current_position_below_notional_buffer_plans_full_close(self):
        exchange = ResidualExchange(quantity=10, mark_price=0.6, leverage=1, min_notional=5)
        engine = ExecutionEngine(exchange)
        engine._record_decision = lambda *args, **kwargs: None
        position = exchange.get_positions()[0]

        with patch("shared.db.get_position_history", return_value={}):
            actions = engine._build_position_actions([], [position], run_id="run1")

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["reason"], "residual_position_cleanup")


if __name__ == "__main__":
    unittest.main()
