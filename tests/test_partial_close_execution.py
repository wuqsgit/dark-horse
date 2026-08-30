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

    def place_stop_order(self, symbol, side, quantity, stop_price):
        self.events.append(("new_stop", quantity, stop_price))
        return {"algoId": "new-stop-1"}

    def cancel_other_protective_stops(self, symbol, keep_order_id):
        self.events.append(("cancel_old", keep_order_id))


class PartialCloseExecutionTest(unittest.TestCase):
    def test_hedge_partial_close_selects_and_closes_only_requested_side(self):
        class HedgeExchange:
            def __init__(self):
                self.closed = None
                self.positions = [
                    {
                        "symbol": "AKEUSDT", "positionSide": "LONG", "side": "LONG",
                        "quantity": 10.0, "entry_price": 0.009, "mark_price": 0.01,
                        "unrealized_pnl": 1.0, "leverage": 2,
                    },
                    {
                        "symbol": "AKEUSDT", "positionSide": "SHORT", "side": "SHORT",
                        "quantity": 4.0, "entry_price": 0.011, "mark_price": 0.01,
                        "unrealized_pnl": 0.4, "leverage": 2,
                    },
                ]

            def get_positions(self):
                return list(self.positions)

            def get_symbol_info(self, symbol):
                return {"min_notional": 5.0, "min_qty": 1.0}

            def close_position_market(
                self, symbol, side, quantity, *, position_side=None,
            ):
                self.closed = (symbol, side, quantity, position_side)
                self.positions = [
                    position for position in self.positions
                    if position["positionSide"] != position_side
                ]
                return {"orderId": 123, "executedQty": str(quantity), "avgPrice": "0.01"}

        exchange = HedgeExchange()
        engine = ExecutionEngine(exchange)
        engine._record_decision = lambda *args, **kwargs: None
        action = {
            "action": "partial_close",
            "symbol": "AKEUSDT",
            "side": "BUY",
            "position_side": "SHORT",
            "close_pct": 1.0,
            "reason": "close short only",
        }

        with patch("shared.db.get_position_history", return_value={}), \
             patch("shared.db.record_trade"):
            succeeded = engine._execute_partial_close(action, [])

        self.assertTrue(succeeded)
        self.assertEqual(exchange.closed, ("AKEUSDT", "BUY", 4.0, "SHORT"))

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

    def test_successful_alpha_partial_close_adds_realized_profit_to_protection_budget(self):
        exchange = PartialCloseExchange()
        engine = ExecutionEngine(exchange)
        engine._record_decision = lambda *args, **kwargs: None
        results = []
        history = {
            "strategy_source": "alpha",
            "protected_profit": 5.0,
            "current_stop_loss": 95.0,
        }

        with patch("shared.db.get_position_history", return_value=history), \
             patch("shared.db.record_trade"), \
             patch("shared.db.update_position_management") as update:
            succeeded = engine._execute_partial_close(
                {
                    "action": "partial_close",
                    "symbol": "B2USDT",
                    "side": "SELL",
                    "strategy_source": "alpha",
                    "close_pct": 0.25,
                    "reason": "alpha_profit_lock_stage1 peak_roi=10.0%",
                },
                results,
            )

        self.assertTrue(succeeded)
        protection_updates = [
            call.kwargs
            for call in update.call_args_list
            if "protected_stop" in call.kwargs
        ]
        self.assertAlmostEqual(
            next(
                call.kwargs["protected_profit"]
                for call in update.call_args_list
                if "protected_profit" in call.kwargs
            ),
            10.0,
        )
        self.assertEqual(len(protection_updates), 1)
        self.assertEqual(protection_updates[0]["quantity"], 7.5)
        self.assertAlmostEqual(protection_updates[0]["protected_stop"], 100.15)
        self.assertEqual(
            exchange.events[-2:],
            [("new_stop", 7.5, 100.15), ("cancel_old", "new-stop-1")],
        )

    def test_normal_tp1_moves_remaining_exchange_stop_to_break_even(self):
        exchange = PartialCloseExchange()
        engine = ExecutionEngine(exchange)
        engine._record_decision = lambda *args, **kwargs: None
        history = {
            "strategy_source": "normal",
            "current_stop_loss": 95.0,
            "initial_stop_loss": 95.0,
        }

        with patch("shared.db.get_position_history", return_value=history), \
             patch("shared.db.record_trade"), \
             patch("shared.db.update_position_management") as update:
            succeeded = engine._execute_partial_close(
                {
                    "action": "partial_close",
                    "symbol": "B2USDT",
                    "side": "SELL",
                    "strategy_source": "normal",
                    "close_pct": 0.25,
                    "reason": "TP1 r>=1",
                },
                [],
            )

        self.assertTrue(succeeded)
        protection_updates = [
            call.kwargs for call in update.call_args_list
            if "protected_stop" in call.kwargs
        ]
        self.assertEqual(len(protection_updates), 1)
        self.assertAlmostEqual(protection_updates[0]["protected_stop"], 100.15)
        self.assertEqual(
            exchange.events[-2:],
            [("new_stop", 7.5, 100.15), ("cancel_old", "new-stop-1")],
        )

    def test_explosive_grace_keeps_exchange_stop_at_initial_structure(self):
        exchange = PartialCloseExchange()
        engine = ExecutionEngine(exchange)
        engine._record_decision = lambda *args, **kwargs: None
        history = {
            "strategy_source": "alpha",
            "entry_reason": "explosive_breakout alpha_volume_price",
            "initial_quantity": 10.0,
            "initial_stop_loss": 95.0,
            "current_stop_loss": 95.0,
        }

        with patch("shared.db.get_position_history", return_value=history), \
             patch("shared.db.record_trade"), \
             patch("shared.db.update_position_management") as update:
            succeeded = engine._execute_partial_close(
                {
                    "action": "partial_close",
                    "symbol": "B2USDT",
                    "side": "SELL",
                    "strategy_source": "alpha",
                    "close_pct": 0.20,
                    "min_remaining_fraction": 0.40,
                    "initial_quantity": 10.0,
                    "explosive_runner_grace": True,
                    "reason": "alpha_profit_lock_stage1 peak_roi=10.2%",
                },
                [],
            )

        self.assertTrue(succeeded)
        protection = next(
            call.kwargs for call in update.call_args_list
            if "protected_stop" in call.kwargs
        )
        self.assertAlmostEqual(protection["protected_stop"], 95.0)
        self.assertEqual(exchange.events[-2:], [("new_stop", 8.0, 95.0), ("cancel_old", "new-stop-1")])

    def test_explosive_partial_close_never_consumes_forty_percent_runner(self):
        exchange = PartialCloseExchange()
        exchange.quantity = 5.0
        engine = ExecutionEngine(exchange)
        engine._record_decision = lambda *args, **kwargs: None

        with patch("shared.db.get_position_history", return_value={}), \
             patch("shared.db.record_trade"), \
             patch("shared.db.update_position_management"):
            succeeded = engine._execute_partial_close(
                {
                    "action": "partial_close",
                    "symbol": "B2USDT",
                    "side": "SELL",
                    "close_pct": 0.50,
                    "min_remaining_fraction": 0.40,
                    "initial_quantity": 10.0,
                    "reason": "explosive runner protect",
                },
                [],
            )

        self.assertTrue(succeeded)
        self.assertAlmostEqual(exchange.quantity, 4.0)


if __name__ == "__main__":
    unittest.main()
