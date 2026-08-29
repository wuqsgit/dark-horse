import os
import tempfile
import unittest

import shared.db as db


class LiveAccountStoreTest(unittest.TestCase):
    def setUp(self):
        self.original_db_path = db.DB_PATH
        self.temp = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.temp.name, "live-account.db")
        db.init_db()

        connection = db.get_conn()
        try:
            connection.executemany(
                """INSERT INTO trading_accounts
                   (id, name, environment, initial_capital, enabled)
                   VALUES (?, ?, 'testnet', 1000, 1)""",
                ((1, "primary"), (2, "secondary")),
            )
            connection.commit()
        finally:
            connection.close()

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        self.temp.cleanup()

    @staticmethod
    def balance(wallet=1000, equity=1012):
        return {
            "asset": "USDT",
            "wallet_balance": wallet,
            "equity": equity,
            "available_balance": 800,
            "unrealized_pnl": equity - wallet,
            "total_maint_margin": 3,
            "total_initial_margin": 200,
        }

    @staticmethod
    def position(symbol="BTCUSDT", quantity=0.1):
        return {
            "symbol": symbol,
            "position_side": "BOTH",
            "side": "LONG",
            "quantity": quantity,
            "entry_price": 100000,
            "mark_price": 101000,
            "unrealized_pnl": 100,
            "leverage": 2,
            "margin": 5000,
            "initial_margin": 5000,
            "maint_margin": 100,
            "position_initial_margin": 5000,
            "open_order_initial_margin": 0,
            "isolated_margin": 0,
            "notional": 10100,
            "margin_asset": "USDT",
            "margin_type": "cross",
            "liquidation_price": 50000,
            "break_even_price": 100010,
            "risk_api_version": "v3",
        }

    @staticmethod
    def order(order_id="987", symbol="BTCUSDT"):
        return {
            "exchange_order_id": order_id,
            "client_order_id": "DH-live-987",
            "symbol": symbol,
            "side": "SELL",
            "position_side": "BOTH",
            "order_type": "STOP_MARKET",
            "status": "NEW",
            "quantity": 0.1,
            "executed_quantity": 0,
            "price": 0,
            "stop_price": 95000,
            "reduce_only": True,
            "close_position": False,
            "time_in_force": "GTC",
        }

    def test_full_snapshot_round_trip_preserves_separate_current_state(self):
        from shared.live_account_store import (
            fetch_live_account_snapshot,
            replace_live_account_snapshot,
        )

        replace_live_account_snapshot(
            1,
            self.balance(),
            [self.position()],
            [self.order()],
            source="http_reconcile",
            exchange_event_time="2026-08-29T08:00:00Z",
        )

        snapshot = fetch_live_account_snapshot(1)
        self.assertEqual(snapshot["balance"]["wallet_balance"], 1000)
        self.assertEqual(snapshot["positions"][0]["symbol"], "BTCUSDT")
        self.assertEqual(snapshot["orders"][0]["exchange_order_id"], "987")
        self.assertEqual(snapshot["state"]["status"], "ok")
        self.assertEqual(snapshot["state"]["source"], "http_reconcile")
        self.assertTrue(snapshot["state"]["snapshot_version"])

    def test_completed_reconcile_removes_positions_and_orders_missing_from_exchange(self):
        from shared.live_account_store import (
            fetch_live_account_snapshot,
            replace_live_account_snapshot,
        )

        replace_live_account_snapshot(
            1,
            self.balance(),
            [self.position()],
            [self.order()],
            source="http_reconcile",
        )
        replace_live_account_snapshot(
            1,
            self.balance(wallet=990, equity=990),
            [],
            [],
            source="http_reconcile",
        )

        snapshot = fetch_live_account_snapshot(1)
        self.assertEqual(snapshot["positions"], [])
        self.assertEqual(snapshot["orders"], [])
        self.assertEqual(snapshot["balance"]["wallet_balance"], 990)

    def test_replacing_one_account_does_not_change_another_account(self):
        from shared.live_account_store import (
            fetch_live_account_snapshot,
            replace_live_account_snapshot,
        )

        replace_live_account_snapshot(
            1,
            self.balance(wallet=1000),
            [self.position("BTCUSDT")],
            [self.order("1", "BTCUSDT")],
            source="http_reconcile",
        )
        replace_live_account_snapshot(
            2,
            self.balance(wallet=500),
            [self.position("ETHUSDT", quantity=1.5)],
            [self.order("2", "ETHUSDT")],
            source="http_reconcile",
        )
        replace_live_account_snapshot(
            1,
            self.balance(wallet=1010),
            [],
            [],
            source="http_reconcile",
        )

        first = fetch_live_account_snapshot(1)
        second = fetch_live_account_snapshot(2)
        self.assertEqual(first["positions"], [])
        self.assertEqual(second["positions"][0]["symbol"], "ETHUSDT")
        self.assertEqual(second["orders"][0]["exchange_order_id"], "2")
        self.assertEqual(second["balance"]["wallet_balance"], 500)


if __name__ == "__main__":
    unittest.main()
