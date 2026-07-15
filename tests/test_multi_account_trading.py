import asyncio
import os
import tempfile
import time
import unittest
from unittest.mock import patch

import shared.db as db


class MultiAccountTradingTest(unittest.TestCase):
    def setUp(self):
        self.original_db_path = db.DB_PATH
        self.temp = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.temp.name, "accounts.db")
        db.init_db()

    def tearDown(self):
        db.DB_PATH = self.original_db_path
        self.temp.cleanup()

    def test_income_rebuild_is_isolated_by_account(self):
        for account_id, income in ((1, 1.25), (2, -0.75)):
            token = db.set_account_context(account_id)
            try:
                db.upsert_exchange_income({
                    "symbol": "BTCUSDT", "incomeType": "REALIZED_PNL", "income": income,
                    "asset": "USDT", "time": 1783821600000, "tradeId": f"trade-{account_id}",
                })
                db.rebuild_position_trades_from_income()
            finally:
                db.reset_account_context(token)

        conn = db.get_conn()
        try:
            rows = conn.execute(
                "SELECT account_id, ROUND(SUM(net_pnl), 2) pnl FROM position_trades GROUP BY account_id ORDER BY account_id"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual([(row["account_id"], row["pnl"]) for row in rows], [(1, 1.25), (2, -0.75)])

    def test_account_context_restores_previous_account(self):
        self.assertEqual(db.current_account_id(), 1)
        token = db.set_account_context(4)
        self.assertEqual(db.current_account_id(), 4)
        db.reset_account_context(token)
        self.assertEqual(db.current_account_id(), 1)

    def test_delete_account_is_blocked_when_position_exists(self):
        from shared.accounts import delete_account, save_account

        account = save_account({"name": "hold-account", "environment": "testnet"})
        conn = db.get_conn()
        try:
            conn.execute(
                "INSERT INTO account_position_history (account_id, symbol, side, quantity, entry_price) VALUES (?, ?, ?, ?, ?)",
                (account["id"], "BTCUSDT", "LONG", 0.01, 100000),
            )
            conn.commit()
        finally:
            conn.close()

        with self.assertRaisesRegex(ValueError, "账户还有持仓"):
            delete_account(account["id"])

    def test_update_account_does_not_create_new_account(self):
        from shared.accounts import ensure_default_account, list_accounts, save_account

        account_id = ensure_default_account()
        save_account({"name": "renamed-default", "environment": "testnet"}, account_id=account_id)

        accounts = list_accounts()
        self.assertEqual(len(accounts), 1)
        self.assertEqual(accounts[0]["name"], "renamed-default")

    def test_list_accounts_does_not_reinitialize_db_when_account_exists(self):
        from shared.accounts import ensure_default_account, list_accounts

        ensure_default_account()

        with patch("shared.accounts.init_db", side_effect=AssertionError("init_db should not run on hot account reads")):
            accounts = list_accounts()

        self.assertEqual(len(accounts), 1)

    def test_account_status_endpoint_uses_snapshot_ttl_instead_of_fast_cache_path(self):
        from api.main import _FAST_CACHE_PATHS, _cache_ttl_for_path

        self.assertNotIn("/api/trading/accounts/status", _FAST_CACHE_PATHS)
        self.assertEqual(_cache_ttl_for_path("/api/trading/accounts/status"), 30)
        self.assertEqual(_cache_ttl_for_path("/api/trading/status"), 10)

    def test_recent_trades_are_position_level_groups_with_score_and_pct(self):
        conn = db.get_conn()
        try:
            conn.executemany(
                """INSERT INTO position_trades
                   (account_id, position_trade_id, symbol, side, entry_time, exit_time,
                    entry_price, exit_price, quantity, net_pnl, pnl_pct, income_count,
                    grade_at_entry, score_at_entry, source)
                   VALUES (1, ?, 'ETHUSDT', 'LONG', '2026-07-12 01:00:00', ?, 3000, ?, ?, ?, ?, 1, 'A', 82.5, 'exchange_income')""",
                [
                    ("ETHUSDT-POS-1-a", "2026-07-12 02:00:00", 3060, 0.10, 4.0, 4.0),
                    ("ETHUSDT-POS-1-b", "2026-07-12 02:05:00", 3090, 0.05, 3.0, 6.0),
                    ("ETHUSDT-POS-1-c", "2026-07-12 02:10:00", 3120, 0.05, 5.0, 10.0),
                ],
            )
            conn.execute(
                "INSERT INTO positions_history(account_id, time, symbol, side, quantity, entry_price, leverage) VALUES(1, '2026-07-12 01:30:00', 'ETHUSDT', 'LONG', 0.20, 3000, 3)"
            )
            conn.commit()
        finally:
            conn.close()

        rows = db.fetch_position_trade_groups(limit=20, account_id=1)
        eth_rows = [row for row in rows if row["symbol"] == "ETHUSDT"]

        self.assertEqual(len(eth_rows), 1)
        row = eth_rows[0]
        self.assertEqual(row["close_count"], 3)
        self.assertAlmostEqual(row["pnl"], 12.0)
        self.assertAlmostEqual(row["qty"], 0.2)
        self.assertAlmostEqual(row["pnl_pct"], 6.0)
        self.assertEqual(row["grade_at_entry"], "A")
        self.assertAlmostEqual(row["score_at_entry"], 82.5)

    def test_recent_trades_backfill_score_from_legacy_trades(self):
        conn = db.get_conn()
        try:
            conn.execute(
                """INSERT INTO position_trades
                   (account_id, position_trade_id, symbol, side, entry_time, exit_time,
                    entry_price, exit_price, quantity, net_pnl, income_count, source)
                   VALUES (1, 'ETHUSDT-legacy', 'ETHUSDT', 'LONG', '2026-07-12 01:00:00',
                           '2026-07-12 02:00:00', 3000, 3090, 0.1, 9.0, 1, 'exchange_income')"""
            )
            conn.execute(
                """INSERT INTO trades
                   (account_id, symbol, side, quantity, entry_price, exit_price, pnl,
                    entry_time, exit_time, grade_at_entry, score_at_entry, source)
                   VALUES (1, 'ETHUSDT', 'LONG', 0.1, 3000, 3090, 9.0,
                           '2026-07-12 01:00:00', '2026-07-12 02:00:00', 'S1', 91.0, 'system')"""
            )
            conn.commit()
        finally:
            conn.close()

        row = [r for r in db.fetch_position_trade_groups(limit=20, account_id=1) if r["symbol"] == "ETHUSDT"][0]

        self.assertEqual(row["grade_at_entry"], "S1")
        self.assertAlmostEqual(row["score_at_entry"], 91.0)

    def test_account_status_enriches_cross_positions_with_account_margin_ratio_and_holding_time(self):
        from api.main import _account_status_payload

        conn = db.get_conn()
        try:
            conn.execute(
                """INSERT INTO account_position_history
                   (account_id, symbol, side, quantity, entry_price, entry_time,
                    entry_score, stop_model, initial_stop_loss, stop_pct,
                    current_stop_loss, highest_price, lowest_price, r_multiple,
                    tp1_hit, tp2_hit, trailing_enabled, roll_layer, roll_enabled,
                    roll_block_reason, last_exit_reason, strategy_source, alpha_symbol)
                   VALUES (1, 'ETHUSDT', 'LONG', 0.1, 2000, '2026-07-12 00:00:00',
                           82.5, 'atr_clamped', 1900, 0.05, 1900, 2120, 1980, 1.2,
                           1, 0, 1, 0, 0, 'waiting_1_5r', 'partial_profit_protect',
                           'alpha', 'ALPHA_1USDT')"""
            )
            conn.executemany(
                """INSERT INTO strategy_decisions
                   (account_id, run_id, time, symbol, side, decision_stage,
                    decision_result, filter_reason, composite_score)
                   VALUES (?, ?, ?, ?, 'LONG', 'position_management', 'hold', ?, 70)""",
                [
                    (1, "account-1-run", "2026-07-12 03:00:00", "ETHUSDT", "account one hold"),
                    (2, "account-2-run", "2026-07-12 03:01:00", "BTCUSDT", "account two hold"),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        class FakeExchange:
            def __init__(self, **kwargs):
                pass

            def get_margin_balance(self):
                return {
                    "totalWalletBalance": 5000,
                    "totalMarginBalance": 5100,
                    "totalMaintMargin": 102,
                    "totalUnrealizedProfit": 100,
                    "availableBalance": 4500,
                }

            def get_positions(self):
                return [
                    {
                        "symbol": "ETHUSDT", "side": "LONG", "quantity": 0.1,
                        "entry_price": 2000, "mark_price": 2100,
                        "unrealized_pnl": 10, "leverage": 5, "margin": 40,
                        "maint_margin": 2, "margin_type": "cross",
                    },
                    {
                        "symbol": "BTCUSDT", "side": "LONG", "quantity": 0.01,
                        "entry_price": 50000, "mark_price": 51000,
                        "unrealized_pnl": 10, "leverage": 5, "margin": 100,
                        "maint_margin": 5, "margin_type": "cross",
                    },
                ]

            def close(self):
                pass

        account = {
            "id": 1, "name": "test", "environment": "testnet",
            "initial_capital": 5000, "max_positions": 5,
            "normal_trading_enabled": 1, "alpha_trading_enabled": 1,
            "auto_trading_enabled": 1,
        }
        alpha_context = {
            "alpha_score": 88.0,
            "volume_price_state": "momentum_continuation",
            "volume_price_action": "normal_review",
            "volume_price_reasons_json": '["dual market volume confirmed"]',
        }
        with patch("trader.exchange.BinanceFutures", FakeExchange), \
             patch("shared.db.fetch_position_trade_groups", return_value=[]), \
             patch("shared.db.fetch_latest_alpha_position_context", return_value=alpha_context):
            payload = _account_status_payload(account)

        self.assertEqual(payload["status"], "ok")
        eth, btc = payload["positions"]
        self.assertEqual(eth["margin_ratio"], 2.0)
        self.assertEqual(btc["margin_ratio"], 2.0)
        self.assertEqual(eth["pnl_pct"], 25.0)
        self.assertEqual(btc["pnl_pct"], 10.0)
        self.assertEqual(eth["entry_time"], "2026-07-12 00:00:00")
        self.assertNotEqual(eth["holding_time"], "-")
        self.assertEqual(eth["entry_score"], 82.5)
        self.assertEqual(eth["stop_model"], "atr_clamped")
        self.assertEqual(eth["initial_stop_loss"], 1900)
        self.assertEqual(eth["current_stop_loss"], 1900)
        self.assertEqual(eth["highest_price"], 2120)
        self.assertEqual(eth["lowest_price"], 1980)
        self.assertEqual(eth["r_multiple"], 1.2)
        self.assertTrue(eth["tp1_hit"])
        self.assertTrue(eth["trailing_enabled"])
        self.assertEqual(eth["roll_status"], "waiting_1_5r")
        self.assertEqual(eth["last_exit_reason"], "partial_profit_protect")
        self.assertEqual(eth["invested"], 200.0)
        self.assertEqual(eth["alpha_current_score"], 88.0)
        self.assertEqual(eth["alpha_volume_price_state"], "momentum_continuation")
        self.assertEqual(eth["alpha_volume_price_action"], "normal_review")
        self.assertEqual(eth["alpha_volume_price_reason"], "dual market volume confirmed")
        self.assertEqual(eth["max_floating_pnl"], 12.0)
        self.assertEqual(eth["last_system_action"], "account one hold")
        self.assertEqual(btc["roll_status"], "state_incomplete")
        self.assertEqual(payload["decision_panel"]["latest_run_id"], "account-1-run")
        self.assertTrue(all(row["symbol"] != "BTCUSDT" for row in payload["decision_panel"]["recent"]))


class AccountStatusSnapshotTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import api.main as main

        self.main = main
        self.original_snapshot = dict(main._account_status_snapshot)
        self.original_refresh_task = main._account_status_refresh_task
        main._account_status_snapshot.update({"data": None, "time": 0.0})
        main._account_status_refresh_task = None

    async def asyncTearDown(self):
        task = self.main._account_status_refresh_task
        if task and not task.done():
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.main._account_status_snapshot.clear()
        self.main._account_status_snapshot.update(self.original_snapshot)
        self.main._account_status_refresh_task = self.original_refresh_task

    async def test_stale_snapshot_returns_immediately_and_refreshes_in_background(self):
        self.main._account_status_snapshot.update({
            "data": {"accounts": [{"account_id": 1}], "summary": {}},
            "time": time.time() - 31,
        })
        refresh_started = asyncio.Event()
        release_refresh = asyncio.Event()

        async def slow_refresh():
            refresh_started.set()
            await release_refresh.wait()
            return {"accounts": [{"account_id": 2}], "summary": {}}

        with patch.object(self.main, "_build_all_trading_account_status", side_effect=slow_refresh):
            payload, cache_status = await asyncio.wait_for(
                self.main._get_trading_account_status_snapshot(),
                timeout=0.1,
            )
            await asyncio.wait_for(refresh_started.wait(), timeout=0.1)

            self.assertEqual(cache_status, "STALE")
            self.assertEqual(payload["accounts"][0]["account_id"], 1)
            self.assertTrue(payload["stale"])
            self.assertGreaterEqual(payload["data_age_seconds"], 30)

            release_refresh.set()
            await self.main._account_status_refresh_task
            self.assertEqual(self.main._account_status_snapshot["data"]["accounts"][0]["account_id"], 2)

    async def test_concurrent_cold_requests_share_one_refresh(self):
        refresh_started = asyncio.Event()
        release_refresh = asyncio.Event()
        refresh_count = 0

        async def slow_refresh():
            nonlocal refresh_count
            refresh_count += 1
            refresh_started.set()
            await release_refresh.wait()
            return {"accounts": [{"account_id": 1}], "summary": {}}

        with patch.object(self.main, "_build_all_trading_account_status", side_effect=slow_refresh):
            first = asyncio.create_task(self.main._get_trading_account_status_snapshot())
            second = asyncio.create_task(self.main._get_trading_account_status_snapshot())
            await asyncio.wait_for(refresh_started.wait(), timeout=0.1)
            self.assertEqual(refresh_count, 1)

            release_refresh.set()
            results = await asyncio.gather(first, second)

        self.assertEqual(refresh_count, 1)
        self.assertEqual([row[1] for row in results], ["MISS", "MISS"])
        self.assertTrue(all(row[0]["accounts"][0]["account_id"] == 1 for row in results))


if __name__ == "__main__":
    unittest.main()
