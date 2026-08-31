import unittest
from unittest.mock import patch

from trader.execution import (
    ExecutionEngine,
    _alpha_profile_is_blocked,
    _evaluate_explosive_loss_guard,
)


class ExplosiveExchange:
    account_id = None

    def __init__(self, fail_stop=False, occupied=False):
        self.fail_stop = fail_stop
        self.occupied = occupied
        self.closed = []

    def get_positions(self):
        if self.occupied:
            return [{"symbol": "JCTUSDT", "side": "LONG", "quantity": 1}]
        return []

    def set_leverage(self, symbol, leverage):
        return None

    def place_market_order(self, symbol, side, quantity, client_order_id=None):
        return {"orderId": "open-1", "executedQty": str(quantity)}

    def place_stop_order(self, symbol, side, quantity, stop_price):
        if self.fail_stop:
            raise RuntimeError("stop rejected")
        return {"orderId": "stop-1"}

    def cancel_other_protective_stops(self, symbol, keep_order_id):
        return None

    def close_position_market(self, symbol, side, quantity, client_order_id=None):
        self.closed.append((symbol, side, quantity))
        return {"orderId": "flatten-1"}


class TransientExplosiveExchange(ExplosiveExchange):
    def __init__(self, accepted_before_timeout=False):
        super().__init__()
        self.accepted_before_timeout = accepted_before_timeout
        self.open_attempts = 0
        self.lookup_ids = []

    def place_market_order(self, symbol, side, quantity, client_order_id=None):
        self.open_attempts += 1
        if self.open_attempts == 1:
            raise TimeoutError("order submit timeout")
        return {"orderId": "open-retry", "executedQty": str(quantity)}

    def get_order_by_client_id(self, symbol, client_order_id):
        self.lookup_ids.append(client_order_id)
        if self.accepted_before_timeout:
            return {"orderId": "open-recovered", "executedQty": "1813"}
        return None


def explosive_action():
    return {
        "action": "open",
        "symbol": "BTRUSDT",
        "side": "BUY",
        "position_side": "LONG",
        "quantity": 1813.0,
        "entry_price": 0.03655,
        "stop_loss": 0.033626,
        "leverage": 2,
        "reason": "explosive breakout",
        "event_type": "explosive_breakout",
        "setup_id": "BTRUSDT:LONG:2026-08-26T02:30:00Z",
        "run_id": "run-btr",
        "scan_id": "scan-btr",
        "strategy_source": "alpha",
    }


class ExplosiveExecutionContractTest(unittest.TestCase):
    def test_high_risk_watch_remains_hard_blocked_for_explosive_entry(self):
        self.assertTrue(
            _alpha_profile_is_blocked(
                "high_risk_watch",
                {"high_risk_watch"},
            )
        )

    def test_two_recent_explosive_losses_pause_new_entries(self):
        allowed, reason = _evaluate_explosive_loss_guard(
            [
                {"net_pnl": -3.0},
                {"net_pnl": -2.0},
                {"net_pnl": 4.0},
            ],
            balance=707.0,
        )

        self.assertFalse(allowed)
        self.assertIn("consecutive_losses=2", reason)

    def test_two_percent_explosive_loss_pauses_new_entries(self):
        allowed, reason = _evaluate_explosive_loss_guard(
            [{"net_pnl": -15.0}],
            balance=707.0,
        )

        self.assertFalse(allowed)
        self.assertIn("loss_pct", reason)

    def _db_patches(self):
        return patch.multiple(
            "shared.db",
            insert_order=unittest.mock.DEFAULT,
            new_position_id=unittest.mock.DEFAULT,
            upsert_position_history=unittest.mock.DEFAULT,
            record_entry_review_snapshot=unittest.mock.DEFAULT,
            is_market_entry_ready=unittest.mock.DEFAULT,
        )

    def _execute(self, exchange):
        engine = ExecutionEngine(exchange)
        decisions = []
        engine._record_decision = lambda symbol, **kwargs: decisions.append(kwargs)
        with self._db_patches() as mocked, patch(
            "trader.execution.record_profit"
        ):
            mocked["new_position_id"].return_value = "position-btr"
            mocked["is_market_entry_ready"].return_value = (True, None)
            results = engine.execute([explosive_action()])
        return results, decisions

    def test_successful_explosive_open_has_one_opened_terminal_result(self):
        results, decisions = self._execute(ExplosiveExchange())

        terminals = [d for d in decisions if d.get("decision_stage") == "execution"]
        self.assertEqual([d["decision_result"] for d in terminals], ["opened"])
        self.assertEqual(results[0]["status"], "ok")

    def test_failed_protective_stop_has_only_error_terminal_result(self):
        exchange = ExplosiveExchange(fail_stop=True)
        results, decisions = self._execute(exchange)

        terminals = [d for d in decisions if d.get("decision_stage") == "execution"]
        self.assertEqual([d["decision_result"] for d in terminals], ["error"])
        self.assertEqual(results[0]["status"], "error")
        self.assertEqual(len(exchange.closed), 1)

    def test_occupied_narrative_category_does_not_block_explosive_order(self):
        results, decisions = self._execute(ExplosiveExchange(occupied=True))

        self.assertEqual(results[0]["status"], "ok")
        self.assertNotIn("blocked", [d.get("decision_result") for d in decisions])

    def test_transient_submit_failure_retries_with_idempotent_client_id(self):
        exchange = TransientExplosiveExchange()
        action = explosive_action()
        action["client_order_id"] = "DH-EXP-test-retry"
        engine = ExecutionEngine(exchange)

        with patch("trader.execution.time.sleep"):
            order = engine._place_market_action_order(action)

        self.assertEqual(order["orderId"], "open-retry")
        self.assertEqual(exchange.open_attempts, 2)
        self.assertEqual(exchange.lookup_ids, ["DH-EXP-test-retry"])

    def test_timeout_after_exchange_acceptance_recovers_without_duplicate_submit(self):
        exchange = TransientExplosiveExchange(accepted_before_timeout=True)
        action = explosive_action()
        action["client_order_id"] = "DH-EXP-test-recovered"
        engine = ExecutionEngine(exchange)

        order = engine._place_market_action_order(action)

        self.assertEqual(order["orderId"], "open-recovered")
        self.assertEqual(exchange.open_attempts, 1)


if __name__ == "__main__":
    unittest.main()
