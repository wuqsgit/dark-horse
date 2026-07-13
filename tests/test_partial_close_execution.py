import unittest
from unittest.mock import patch

from trader.execution import ExecutionEngine


class PartialCloseExchange:
    def __init__(self, fail=False, response=None, update_position=True):
        self.fail = fail
        self.response = response
        self.update_position = update_position
        self.quantity = 10.0
        self.events = []

    def get_positions(self):
        return [{
            "symbol": "B2USDT",
            "side": "LONG",
            "quantity": self.quantity,
            "entry_price": 100.0,
            "mark_price": 102.0,
            "unrealized_pnl": 20.0,
            "leverage": 2,
        }]

    def close_position_market(self, symbol, side, quantity):
        self.events.append("exchange")
        if self.fail:
            raise RuntimeError("exchange rejected close")
        if self.update_position:
            self.quantity = max(0.0, self.quantity - quantity)
        return self.response or {"orderId": 123, "executedQty": str(quantity), "avgPrice": "102.0"}


class PartialCloseExecutionTest(unittest.TestCase):
    def test_failed_exchange_close_does_not_record_trade(self):
        exchange = PartialCloseExchange(fail=True)
        engine = ExecutionEngine(exchange)
        results = []

        with patch("shared.db.get_position_history", return_value={}), patch("shared.db.record_trade") as record:
            with self.assertRaisesRegex(RuntimeError, "exchange rejected close"):
                engine._execute_partial_close(
                    {
                        "action": "partial_close",
                        "symbol": "B2USDT",
                        "side": "SELL",
                        "close_pct": 0.25,
                        "reason": "alpha_volume_regime_profit_protect regime=suspicious",
                    },
                    results,
                )

        record.assert_not_called()
        self.assertEqual(results, [])

    def test_management_state_is_marked_only_after_successful_partial_close(self):
        engine = ExecutionEngine(PartialCloseExchange())
        engine._execute_partial_close = lambda act, results: False
        marked = []
        engine._mark_partial_close_state = lambda act: marked.append(act["symbol"])

        engine.execute([
            {"action": "partial_close", "symbol": "B2USDT", "side": "SELL", "close_pct": 0.25}
        ])

        self.assertEqual(marked, [])

    def test_zero_ack_execution_fields_fall_back_to_requested_quantity_and_mark_price(self):
        exchange = PartialCloseExchange(
            response={"orderId": 123, "executedQty": "0", "avgPrice": "0"}
        )
        engine = ExecutionEngine(exchange)
        engine._record_decision = lambda *args, **kwargs: None
        results = []

        with patch("shared.db.get_position_history", return_value={}), patch("shared.db.record_trade") as record:
            succeeded = engine._execute_partial_close(
                {
                    "action": "partial_close",
                    "symbol": "B2USDT",
                    "side": "SELL",
                    "close_pct": 0.25,
                    "reason": "alpha_volume_regime_profit_protect regime=suspicious",
                },
                results,
            )

        self.assertTrue(succeeded)
        self.assertEqual(record.call_args.kwargs["qty"], 2.5)
        self.assertEqual(record.call_args.kwargs["exit_price"], 102.0)

    def test_unconfirmed_zero_execution_does_not_record_or_mark_success(self):
        exchange = PartialCloseExchange(
            response={"orderId": 123, "executedQty": "0", "avgPrice": "0"},
            update_position=False,
        )
        engine = ExecutionEngine(exchange)
        results = []

        with patch("shared.db.get_position_history", return_value={}), patch("shared.db.record_trade") as record:
            with self.assertRaisesRegex(RuntimeError, "execution unconfirmed"):
                engine._execute_partial_close(
                    {
                        "action": "partial_close",
                        "symbol": "B2USDT",
                        "side": "SELL",
                        "close_pct": 0.25,
                        "reason": "alpha_volume_regime_profit_protect regime=suspicious",
                    },
                    results,
                )

        record.assert_not_called()
        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
