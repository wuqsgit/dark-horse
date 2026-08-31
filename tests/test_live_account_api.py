import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import shared.db as db
from shared.live_account_store import replace_live_account_snapshot


class LiveAccountApiReadPathTest(unittest.TestCase):
    def setUp(self):
        import api.main as main

        self.main = main
        self.original_db_path = db.DB_PATH
        self.temp = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.temp.name, "live-api.db")
        db.init_db()
        connection = db.get_conn()
        try:
            connection.execute(
                """INSERT INTO trading_accounts
                   (id, name, environment, initial_capital, max_positions,
                    normal_trading_enabled, alpha_trading_enabled,
                    auto_trading_enabled, enabled, is_default)
                   VALUES (1, 'primary', 'testnet', 700, 5, 1, 1, 1, 1, 1)"""
            )
            connection.commit()
        finally:
            connection.close()
        replace_live_account_snapshot(
            1,
            {
                "asset": "USDT",
                "wallet_balance": 707,
                "equity": 717,
                "available_balance": 600,
                "unrealized_pnl": 10,
                "total_maint_margin": 2,
                "total_initial_margin": 106,
            },
            [{
                "symbol": "ETHUSDT",
                "position_side": "BOTH",
                "side": "LONG",
                "quantity": 0.1,
                "entry_price": 1788,
                "mark_price": 1888,
                "unrealized_pnl": 10,
                "leverage": 2,
                "margin": 89.4,
                "initial_margin": 89.4,
                "maint_margin": 2,
                "position_initial_margin": 89.4,
                "open_order_initial_margin": 0,
                "isolated_margin": 0,
                "notional": 188.8,
                "margin_asset": "USDT",
                "margin_type": "cross",
                "liquidation_price": 900,
                "break_even_price": 1789,
                "risk_api_version": "v3",
            }],
            [],
            source="http_reconcile",
            exchange_event_time="2026-08-29T09:00:00Z",
        )
        self.client = TestClient(self.main.app)

    def tearDown(self):
        self.client.close()
        db.DB_PATH = self.original_db_path
        self.temp.cleanup()

    def test_account_trading_switch_does_not_require_admin_token(self):
        response = self.client.post(
            "/api/trading/accounts/1/controls",
            json={"mode": "normal", "enabled": False},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["ok"])
        connection = db.get_conn()
        try:
            account = connection.execute(
                "SELECT normal_trading_enabled, alpha_trading_enabled, name "
                "FROM trading_accounts WHERE id=1"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(account["normal_trading_enabled"], 0)
        self.assertEqual(account["alpha_trading_enabled"], 1)
        self.assertEqual(account["name"], "primary")

    def test_account_trading_switch_rejects_non_switch_fields(self):
        response = self.client.post(
            "/api/trading/accounts/1/controls",
            json={"mode": "api_key", "enabled": False, "api_key": "replace-me"},
        )

        self.assertEqual(response.status_code, 400)

    def test_status_payload_reads_current_tables_without_exchange_client(self):
        with patch(
            "trader.exchange.BinanceFutures",
            side_effect=AssertionError("status API must not access Binance"),
        ):
            payload = self.main._refresh_all_account_statuses_sync()

        account = payload["accounts"][0]
        self.assertEqual(account["wallet_balance"], 707)
        self.assertEqual(account["equity"], 717)
        self.assertEqual(account["positions"][0]["symbol"], "ETHUSDT")
        self.assertEqual(account["positions"][0]["pnl_pct"], 11.19)
        self.assertEqual(payload["summary"]["position_count"], 1)

    def test_status_sql_never_groups_full_strategy_decision_history(self):
        statements = []
        real_get_conn = db.get_conn

        def traced_connection():
            connection = real_get_conn()
            connection.set_trace_callback(statements.append)
            return connection

        with patch("shared.db.get_conn", side_effect=traced_connection), patch(
            "shared.live_account_store.get_conn",
            side_effect=traced_connection,
        ):
            self.main._refresh_all_account_statuses_sync()

        normalized = " ".join(statements).lower()
        self.assertNotIn("group by symbol", normalized)
        self.assertNotIn("join ( select symbol, max(id)", normalized)

    def test_stale_stream_state_serves_last_snapshot_as_degraded(self):
        connection = db.get_conn()
        try:
            connection.execute(
                """UPDATE account_stream_state
                   SET status='degraded', ws_connected=0,
                       last_error='temporary disconnect',
                       last_success_at='2026-08-29T00:00:00Z',
                       updated_at='2026-08-29T00:00:00Z' WHERE account_id=1"""
            )
            connection.commit()
        finally:
            connection.close()

        payload = self.main._refresh_all_account_statuses_sync()

        account = payload["accounts"][0]
        self.assertEqual(account["status"], "degraded")
        self.assertTrue(account["stale"])
        self.assertEqual(account["error"], "temporary disconnect")
        self.assertEqual(account["positions"][0]["symbol"], "ETHUSDT")


if __name__ == "__main__":
    unittest.main()
