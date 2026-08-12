import os
import tempfile
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import shared.db as db
from shared.live_diagnostics import build_live_diagnostics


class LiveDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        self.original_db_path = db.DB_PATH
        self.temp = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.temp.name, "diagnostics.db")
        db.init_db()
        db.set_trading_runtime_control("normal_trading_enabled", True)
        db.set_trading_runtime_control("alpha_trading_enabled", True)
        self.now = datetime(2026, 8, 4, 16, 0, tzinfo=timezone.utc)
        self.account = {
            "id": 7,
            "normal_trading_enabled": True,
            "alpha_trading_enabled": True,
            "auto_trading_enabled": True,
        }

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        self.temp.cleanup()

    def _seed_fresh_data_and_services(self):
        timestamp = "2026-08-04T15:58:00Z"
        conn = db.get_conn()
        try:
            conn.execute(
                "INSERT INTO alpha_scores(time, symbol, scan_id) VALUES (?, 'BTCUSDT', 'normal-1')",
                (timestamp,),
            )
            conn.execute(
                """INSERT INTO alpha_scan_scores
                   (time, scan_id, alpha_symbol, futures_symbol)
                   VALUES (?, 'alpha-1', 'ALPHA_1USDT', 'BTCUSDT')""",
                (timestamp,),
            )
            conn.commit()
        finally:
            conn.close()
        for service, account_id in (
            ("pipeline", 0),
            ("engine", 0),
            ("alpha_pipeline", 0),
            ("alpha_engine", 0),
            ("trader", 7),
        ):
            db.upsert_service_runtime_status(
                service,
                account_id=account_id,
                status="ok",
                heartbeat_at=timestamp,
                details={"checked": True},
            )

    def test_runtime_status_round_trip(self):
        db.upsert_service_runtime_status(
            "trader",
            account_id=7,
            status="error",
            error_code="order_execution_failed",
            last_error="exchange rejected order",
            details={"symbol": "BTCUSDT"},
            heartbeat_at="2026-08-04T15:59:00Z",
        )

        row = db.fetch_service_runtime_status("trader", 7)[0]

        self.assertEqual(row["status"], "error")
        self.assertEqual(row["error_code"], "order_execution_failed")
        self.assertEqual(row["details"]["symbol"], "BTCUSDT")

    def test_healthy_runtime_can_open_positions(self):
        self._seed_fresh_data_and_services()
        with patch.dict(
            os.environ,
            {"ALPHA_STRATEGY_V2_ENABLED": "true", "ALPHA_STRATEGY_V2_MODE": "testnet_live"},
        ):
            result = build_live_diagnostics(self.account, now=self.now)

        self.assertEqual(result["status"], "healthy")
        self.assertTrue(result["can_open_new_positions"])
        self.assertEqual(result["issues"], [])

    def test_exchange_and_service_errors_are_visible_blockers(self):
        self._seed_fresh_data_and_services()
        db.upsert_service_runtime_status(
            "trader",
            account_id=7,
            status="error",
            error_code="trading_loop_failed",
            last_error="ConnectTimeout: Binance unavailable",
            heartbeat_at="2026-08-04T15:59:00Z",
        )
        with patch.dict(
            os.environ,
            {"ALPHA_STRATEGY_V2_ENABLED": "true", "ALPHA_STRATEGY_V2_MODE": "testnet_live"},
        ):
            result = build_live_diagnostics(
                self.account,
                exchange_error="invalid API key",
                now=self.now,
            )

        codes = {issue["code"] for issue in result["issues"]}
        self.assertEqual(result["status"], "blocked")
        self.assertFalse(result["can_open_new_positions"])
        self.assertIn("exchange_connection_failed", codes)
        self.assertIn("trading_loop_failed", codes)


if __name__ == "__main__":
    unittest.main()
