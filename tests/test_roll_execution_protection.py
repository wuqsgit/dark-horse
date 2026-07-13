import unittest
from unittest.mock import patch

from trader.execution import ExecutionEngine


class ProtectedRollExchange:
    def __init__(self, fail_stop=False):
        self.fail_stop = fail_stop
        self.quantity = 10.0
        self.entry_price = 100.0
        self.events = []

    def set_leverage(self, symbol, leverage):
        self.events.append(("leverage", leverage))

    def place_market_order(self, symbol, side, quantity, reduce_only=False):
        self.events.append(("add", quantity, reduce_only))
        self.entry_price = (self.entry_price * self.quantity + 110.0 * quantity) / (self.quantity + quantity)
        self.quantity += quantity
        return {"orderId": "add-1", "executedQty": str(quantity), "avgPrice": "110"}

    def get_positions(self):
        if self.quantity <= 0:
            return []
        return [{
            "symbol": "BTCUSDT", "side": "LONG", "quantity": self.quantity,
            "entry_price": self.entry_price, "mark_price": 110.0, "leverage": 3,
            "unrealized_pnl": 0,
        }]

    def place_stop_order(self, symbol, side, quantity, stop_price):
        self.events.append(("stop", quantity, stop_price))
        if self.fail_stop:
            raise RuntimeError("stop rejected")
        return {"orderId": "stop-1"}

    def cancel_other_protective_stops(self, symbol, keep_order_id):
        self.events.append(("cancel_old_stops", keep_order_id))

    def close_position_market(self, symbol, side, quantity):
        self.events.append(("unwind", quantity))
        self.quantity -= quantity
        return {"orderId": "unwind-1", "executedQty": str(quantity)}


def action():
    return {
        "action": "roll_add", "symbol": "BTCUSDT", "side": "BUY",
        "position_side": "LONG", "quantity": 2.5, "entry_price": 110.0,
        "roll_layer": 1, "position_id": "p1", "strategy_source": "normal",
        "reason": "roll_add_once", "risk_before": {}, "risk_after": {},
    }


class RollExecutionProtectionTest(unittest.TestCase):
    def _patch_db(self):
        return patch.multiple(
            "shared.db",
            insert_order=unittest.mock.DEFAULT,
            update_position_management=unittest.mock.DEFAULT,
            record_position_roll_event=unittest.mock.DEFAULT,
        )

    def test_roll_keeps_leverage_and_protects_full_refreshed_position(self):
        exchange = ProtectedRollExchange()
        engine = ExecutionEngine(exchange)
        engine._record_decision = lambda *args, **kwargs: None
        results = []

        with self._patch_db() as mocked:
            engine._execute_roll_add(action(), results)

        self.assertFalse(any(event[0] == "leverage" for event in exchange.events))
        stop = next(event for event in exchange.events if event[0] == "stop")
        self.assertEqual(stop[1], 12.5)
        self.assertAlmostEqual(stop[2], exchange.entry_price * 1.0015)
        cancel = next(event for event in exchange.events if event[0] == "cancel_old_stops")
        self.assertEqual(cancel[1], "stop-1")
        updates = mocked["update_position_management"].call_args.kwargs
        self.assertEqual(updates["roll_layer"], 1)
        self.assertEqual(updates["quantity"], 12.5)
        self.assertEqual(updates["roll_price"], 110.0)
        self.assertAlmostEqual(updates["protected_stop"], stop[2])
        self.assertEqual(results[0]["status"], "ok")

    def test_failed_protection_unwinds_only_confirmed_add_and_does_not_mark_roll(self):
        exchange = ProtectedRollExchange(fail_stop=True)
        engine = ExecutionEngine(exchange)
        engine._record_decision = lambda *args, **kwargs: None
        results = []

        with self._patch_db() as mocked:
            with self.assertRaisesRegex(RuntimeError, "roll protection failed"):
                engine._execute_roll_add(action(), results)

        unwind = next(event for event in exchange.events if event[0] == "unwind")
        self.assertEqual(unwind[1], 2.5)
        mocked["update_position_management"].assert_called_once_with(
            "BTCUSDT", roll_enabled=0, roll_block_reason="roll_protection_failed"
        )
        mocked["record_position_roll_event"].assert_not_called()
        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
