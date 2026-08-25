import os
import tempfile
import unittest
from unittest.mock import patch

import shared.db as db
from shared.accounts import get_account, prepare_account_deletion, save_account
from trader.account_lifecycle import (
    AccountDeletionError,
    close_positions_and_delete_account,
)
from trader.execution import ExecutionEngine


class DeletionExchange:
    def __init__(self, positions=None, regular_orders=None, algo_orders=None, stubborn=False):
        self.positions = list(positions or [])
        self.regular_orders = list(regular_orders or [])
        self.algo_orders = list(algo_orders or [])
        self.stubborn = stubborn
        self.closed_orders = []
        self.cancelled_regular = []
        self.cancelled_algo = []
        self.closed = False

    def get_positions(self):
        return [dict(position) for position in self.positions]

    def get_open_orders(self):
        return [dict(order) for order in self.regular_orders]

    def get_open_algo_orders(self):
        return [dict(order) for order in self.algo_orders]

    def cancel_all_open_orders(self, symbol):
        self.cancelled_regular.append(symbol)
        self.regular_orders = [
            order for order in self.regular_orders if order["symbol"] != symbol
        ]
        return {"code": 200}

    def cancel_all_algo_orders(self, symbol):
        self.cancelled_algo.append(symbol)
        before = len(self.algo_orders)
        self.algo_orders = [
            order for order in self.algo_orders if order["symbol"] != symbol
        ]
        return before - len(self.algo_orders)

    def close_position_market(
        self,
        symbol,
        side,
        quantity,
        client_order_id=None,
        position_side=None,
    ):
        if self.stubborn:
            raise RuntimeError("simulated close rejection")
        self.closed_orders.append(
            (symbol, side, quantity, position_side, client_order_id)
        )
        self.positions = [
            position
            for position in self.positions
            if not (
                position["symbol"] == symbol
                and str(position.get("positionSide") or "BOTH")
                == str(position_side or "BOTH")
            )
        ]
        return {"orderId": f"close-{len(self.closed_orders)}", "executedQty": str(quantity)}

    def close(self):
        self.closed = True


class AccountDeletionTest(unittest.TestCase):
    def setUp(self):
        self.original_db_path = db.DB_PATH
        self.temp = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.temp.name, "accounts.db")
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        self.temp.cleanup()

    def _account(self, name="deletable"):
        return save_account(
            {
                "name": name,
                "environment": "testnet",
                "api_key": "test-api-key",
                "api_secret": "test-api-secret",
                "auto_trading_enabled": True,
            }
        )

    def test_delete_flattens_long_and_short_then_cancels_orders(self):
        account = self._account()
        exchange = DeletionExchange(
            positions=[
                {
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "positionSide": "LONG",
                    "quantity": 0.01,
                },
                {
                    "symbol": "ETHUSDT",
                    "side": "SHORT",
                    "positionSide": "SHORT",
                    "quantity": 0.2,
                },
            ],
            regular_orders=[{"symbol": "BTCUSDT", "orderId": 1}],
            algo_orders=[
                {"symbol": "BTCUSDT", "algoId": 11},
                {"symbol": "ETHUSDT", "algoId": 12},
            ],
        )

        with patch("trader.runner.fetch_and_store_income"):
            result = close_positions_and_delete_account(
                account["id"],
                exchange_factory=lambda **kwargs: exchange,
                retry_delay_seconds=0,
            )

        self.assertEqual(result["closed"], 2)
        self.assertEqual(
            [(row[0], row[1], row[3]) for row in exchange.closed_orders],
            [
                ("BTCUSDT", "SELL", "LONG"),
                ("ETHUSDT", "BUY", "SHORT"),
            ],
        )
        self.assertFalse(exchange.positions)
        self.assertFalse(exchange.regular_orders)
        self.assertFalse(exchange.algo_orders)
        self.assertTrue(exchange.closed)
        self.assertIsNone(get_account(account["id"]))

    def test_failed_flatten_keeps_account_with_entries_disabled(self):
        account = self._account("stubborn")
        exchange = DeletionExchange(
            positions=[
                {
                    "symbol": "BTCUSDT",
                    "side": "LONG",
                    "positionSide": "BOTH",
                    "quantity": 0.01,
                }
            ],
            algo_orders=[{"symbol": "BTCUSDT", "algoId": 11}],
            stubborn=True,
        )

        with self.assertRaisesRegex(AccountDeletionError, "账户未删除"):
            close_positions_and_delete_account(
                account["id"],
                exchange_factory=lambda **kwargs: exchange,
                close_attempts=2,
                retry_delay_seconds=0,
            )

        retained = get_account(account["id"])
        self.assertIsNotNone(retained)
        self.assertFalse(retained["auto_trading_enabled"])
        self.assertFalse(retained["normal_trading_enabled"])
        self.assertFalse(retained["alpha_trading_enabled"])
        self.assertTrue(exchange.algo_orders, "protective algo order must remain on close failure")

    def test_account_without_credentials_and_local_positions_is_preserved(self):
        account = save_account({"name": "missing-credentials", "environment": "testnet"})
        conn = db.get_conn()
        try:
            conn.execute(
                """INSERT INTO account_position_history
                   (account_id, symbol, side, quantity, entry_price)
                   VALUES (?, 'BTCUSDT', 'LONG', 0.01, 100000)""",
                (account["id"],),
            )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaisesRegex(AccountDeletionError, "缺少 Binance API 凭据"):
            close_positions_and_delete_account(account["id"])

        self.assertIsNotNone(get_account(account["id"]))

    def test_empty_account_without_credentials_can_be_deleted(self):
        account = save_account({"name": "empty-account", "environment": "testnet"})

        result = close_positions_and_delete_account(account["id"])

        self.assertEqual(result["closed"], 0)
        self.assertFalse(result["exchange_verified"])
        self.assertIsNone(get_account(account["id"]))

    def test_execution_rechecks_account_switch_before_opening(self):
        account = self._account("race-gate")
        prepare_account_deletion(account["id"])

        class Exchange:
            account_id = account["id"]

        engine = ExecutionEngine(Exchange())
        engine._record_decision = lambda *args, **kwargs: None
        results = engine.execute(
            [
                {
                    "action": "open",
                    "symbol": "BTCUSDT",
                    "side": "BUY",
                    "strategy_source": "normal",
                }
            ]
        )

        self.assertEqual(results[0]["status"], "blocked")
        self.assertEqual(results[0]["error"], "account_entry_disabled")


if __name__ == "__main__":
    unittest.main()
