import os
import subprocess
import threading
import time
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi import Response
from fastapi.testclient import TestClient

import shared.db as db
from shared.account_status_snapshot import load_account_snapshot, save_account_snapshot


def _legacy_account_snapshot_data():
    return {
        "accounts": [{
            "account_id": 1,
            "account_name": "primary",
            "environment": "testnet",
            "status": "ok",
            "equity": 100,
            "total_pnl": 5,
            "positions": [{
                "symbol": "BTCUSDT",
                "last_system_action": "hold",
                "entry_score": 82.5,
                "entry_reason": "breakout confirmed",
                "roll_status": "waiting_1_5r",
                "last_exit_reason": "partial_profit_protect",
            }],
            "recent_trades": [{"symbol": "OLDUSDT"}],
            "decision_panel": {"recent": []},
            "runtime_diagnostics": {"exchange": "ok"},
            "stats": {"total_closed": 1},
        }],
        "summary": {"equity": 100, "total_pnl": 5},
        "environment_status": "TESTNET LIVE",
        "recent_trades": [{"symbol": "OLDUSDT"}],
        "decision_panel": {"recent": []},
        "runtime_diagnostics": {"exchange": "ok"},
    }


def _assert_slim_snapshot(test_case, payload):
    for lazy_key in ("recent_trades", "decision_panel", "runtime_diagnostics"):
        test_case.assertNotIn(lazy_key, payload)
        test_case.assertNotIn(lazy_key, payload["accounts"][0])
    test_case.assertNotIn("stats", payload["accounts"][0])
    position = payload["accounts"][0]["positions"][0]
    test_case.assertEqual(position["last_system_action"], "hold")
    test_case.assertEqual(position["entry_score"], 82.5)
    test_case.assertEqual(position["entry_reason"], "breakout confirmed")
    test_case.assertEqual(position["roll_status"], "waiting_1_5r")
    test_case.assertEqual(position["last_exit_reason"], "partial_profit_protect")


class AccountStatusSnapshotPersistenceTest(unittest.TestCase):
    def test_runtime_snapshot_path_is_gitignored(self):
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["git", "check-ignore", "-q", ".runtime/trading-account-status.json"],
            cwd=root,
            check=False,
        )

        self.assertEqual(result.returncode, 0)

    def test_snapshot_is_loaded_from_disk_without_exchange_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            expected = {"accounts": [{"account_id": 1}], "summary": {"equity": 10}}

            save_account_snapshot(path, expected)

            self.assertEqual(load_account_snapshot(path), expected)
            self.assertFalse(path.with_suffix(".json.tmp").exists())

    def test_invalid_snapshot_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_text("not json", encoding="utf-8")

            with self.assertLogs("shared.account_status_snapshot", level="WARNING"):
                self.assertIsNone(load_account_snapshot(path))

    def test_invalid_snapshot_encoding_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            path.write_bytes(b"\xff")

            with self.assertLogs("shared.account_status_snapshot", level="WARNING"):
                self.assertIsNone(load_account_snapshot(path))

    def test_concurrent_saves_use_distinct_temporary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            temporary_paths = []
            lock = threading.Lock()
            replace_lock = threading.Lock()
            barrier = threading.Barrier(2)
            real_replace = os.replace

            def synchronized_replace(source, target):
                with lock:
                    temporary_paths.append(Path(source))
                barrier.wait(timeout=2)
                with replace_lock:
                    real_replace(source, target)

            with patch("shared.account_status_snapshot.os.replace", side_effect=synchronized_replace):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [
                        executor.submit(save_account_snapshot, path, {"accounts": [{"account_id": account_id}]})
                        for account_id in (1, 2)
                    ]
                    errors = []
                    for future in futures:
                        try:
                            future.result()
                        except OSError as exc:
                            errors.append(exc)

            self.assertEqual(errors, [])
            self.assertEqual(len(set(temporary_paths)), 2)

    def test_failed_replace_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            with patch("shared.account_status_snapshot.os.replace", side_effect=OSError("disk unavailable")):
                with self.assertRaisesRegex(OSError, "disk unavailable"):
                    save_account_snapshot(path, {"accounts": []})

            self.assertEqual(list(Path(directory).iterdir()), [])


class TradingApiReplacementRoutesTest(unittest.TestCase):
    def setUp(self):
        import api.main as main
        from shared.accounts import ensure_default_account, save_account

        self.main = main
        self.original_db_path = db.DB_PATH
        self.temp = tempfile.TemporaryDirectory()
        db.DB_PATH = os.path.join(self.temp.name, "trading-api.db")
        db.init_db()
        self.account_id = ensure_default_account()
        save_account(
            {
                "name": "route-test",
                "environment": "testnet",
                "api_key": "test-api-key",
                "api_secret": "test-api-secret",
                "normal_trading_enabled": True,
                "alpha_trading_enabled": False,
                "auto_trading_enabled": True,
            },
            account_id=self.account_id,
        )
        conn = db.get_conn()
        try:
            conn.executemany(
                """INSERT INTO position_trades
                   (account_id, position_trade_id, symbol, side, entry_time, exit_time,
                    entry_price, exit_price, quantity, net_pnl, pnl_pct, income_count,
                    source, strategy_source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (
                        self.account_id, "BTCUSDT-cycle-1", "BTCUSDT", "LONG",
                        "2026-08-20T01:00:00Z", "2026-08-20T02:00:00Z",
                        100000, 101000, 0.01, 10, 1, 1, "exchange_income", "normal",
                    ),
                    (
                        self.account_id, "BTCUSDT-cycle-2", "BTCUSDT", "LONG",
                        "2026-08-21T01:00:00Z", "2026-08-21T02:00:00Z",
                        102000, 101000, 0.01, -10, -1, 1, "exchange_income", "alpha",
                    ),
                    (
                        self.account_id, "ETHUSDT-cycle-1", "ETHUSDT", "SHORT",
                        "2026-08-22T01:00:00Z", "2026-08-22T02:00:00Z",
                        4000, 3900, 0.1, 10, 2.5, 1, "exchange_income", "normal",
                    ),
                ],
            )
            conn.execute(
                """INSERT INTO strategy_decisions
                   (account_id, run_id, time, symbol, side, decision_stage,
                    decision_result, filter_reason, composite_score)
                   VALUES (?, 'route-test-run', '2026-08-22T03:00:00Z',
                           'BTCUSDT', 'LONG', 'safety_filter', 'filtered',
                           'score too low', 42)""",
                (self.account_id,),
            )
            conn.commit()
        finally:
            conn.close()
        self.main._response_cache.clear()
        self.client = TestClient(main.app)

    def tearDown(self):
        self.client.close()
        db.DB_PATH = self.original_db_path
        self.temp.cleanup()

    def test_legacy_trading_routes_are_removed(self):
        with patch(
            "shared.accounts.account_exchange_config",
            side_effect=ValueError("credentials unavailable"),
        ):
            for path in (
                "/api/trading/status",
                "/api/trading/statu",
                "/api/trading/stats",
            ):
                with self.subTest(path=path):
                    self.assertEqual(self.client.get(path).status_code, 404)

    def test_history_route_returns_one_row_per_symbol_and_direction(self):
        response = self.client.get(
            f"/api/trading/accounts/{self.account_id}/history?limit=20"
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        keys = [(row["symbol"], row["side"]) for row in payload["items"]]
        self.assertEqual(len(keys), len(set(keys)))
        self.assertEqual(keys, [("ETHUSDT", "SHORT"), ("BTCUSDT", "LONG")])
        self.assertEqual(payload["stats"]["total_cycles"], 3)
        self.assertEqual(payload["reconcile_status"], "incomplete")

    def test_history_route_maps_filters_and_validation_errors(self):
        response = self.client.get(
            f"/api/trading/accounts/{self.account_id}/history",
            params={
                "symbol": "BTCUSDT",
                "direction": "LONG",
                "source": "alpha",
                "from": "2026-08-21T00:00:00Z",
                "to": "2026-08-21T23:59:59Z",
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["items"]), 1)
        self.assertEqual(payload["items"][0]["symbol"], "BTCUSDT")
        self.assertEqual(payload["items"][0]["side"], "LONG")
        self.assertEqual(payload["items"][0]["position_count"], 1)
        self.assertEqual(payload["stats"]["total_cycles"], 1)

        cases = (
            ({"cursor": "not-a-cursor"}, "invalid_history_cursor"),
            ({"direction": "SIDEWAYS"}, "invalid_direction"),
            ({"from": "not-a-date"}, "invalid_date"),
            (
                {"from": "2026-08-22T00:00:00Z", "to": "2026-08-21T00:00:00Z"},
                "invalid_date",
            ),
        )
        for params, code in cases:
            with self.subTest(params=params):
                invalid = self.client.get(
                    f"/api/trading/accounts/{self.account_id}/history",
                    params=params,
                )
                self.assertEqual(invalid.status_code, 400)
                self.assertEqual(invalid.json()["detail"]["code"], code)

    def test_history_and_decisions_reject_unknown_accounts(self):
        for suffix in ("history", "decisions"):
            with self.subTest(suffix=suffix):
                response = self.client.get(f"/api/trading/accounts/999/{suffix}")
                self.assertEqual(response.status_code, 404)
                detail = response.json()["detail"]
                self.assertIsInstance(detail, dict)
                self.assertEqual(detail["code"], "account_not_found")

    def test_decisions_and_runtime_status_return_local_account_data(self):
        decisions = self.client.get(
            f"/api/trading/accounts/{self.account_id}/decisions"
        )
        runtime = self.client.get("/api/trading/runtime/status")

        self.assertEqual(decisions.status_code, 200)
        self.assertEqual(decisions.json()["latest_run_id"], "route-test-run")
        self.assertEqual(decisions.json()["recent"][0]["symbol"], "BTCUSDT")
        self.assertEqual(runtime.status_code, 200)
        runtime_payload = runtime.json()
        self.assertIn("trading_controls", runtime_payload)
        self.assertEqual(len(runtime_payload["accounts"]), 1)
        account = runtime_payload["accounts"][0]
        self.assertEqual(account["account_id"], self.account_id)
        self.assertEqual(account["account_name"], "route-test")
        self.assertIn("runtime_diagnostics", account)
        self.assertNotIn("api_key", account)
        self.assertNotIn("api_secret", account)

    def test_local_read_routes_remain_responsive_during_exchange_timeout(self):
        entered_margin_call = threading.Event()
        release_margin_call = threading.Event()
        margin_call_threads = []

        def delayed_timeout(_exchange):
            margin_call_threads.append(threading.get_ident())
            entered_margin_call.set()
            release_margin_call.wait(timeout=3)
            raise TimeoutError("exchange timed out")

        original_snapshot = dict(self.main._account_status_snapshot)
        self.main._account_status_snapshot.update({
            "data": {"accounts": [], "summary": {}},
            "time": time.time(),
            "last_error": None,
        })
        try:
            with patch(
                "trader.exchange.BinanceFutures.get_margin_balance",
                new=delayed_timeout,
            ), ThreadPoolExecutor(max_workers=1) as executor:
                refresh = executor.submit(self.main._refresh_all_account_statuses_sync)
                self.assertTrue(entered_margin_call.wait(timeout=2))
                background_thread = margin_call_threads[0]

                started = time.monotonic()
                responses = [
                    self.client.get("/api/trading/accounts/status"),
                    self.client.get(
                        f"/api/trading/accounts/{self.account_id}/history"
                    ),
                    self.client.get(
                        f"/api/trading/accounts/{self.account_id}/decisions"
                    ),
                    self.client.get("/api/trading/runtime/status"),
                ]
                elapsed = time.monotonic() - started

                self.assertTrue(all(response.status_code == 200 for response in responses))
                self.assertLess(elapsed, 1.0)
                self.assertEqual(margin_call_threads, [background_thread])
                release_margin_call.set()
                with self.assertRaisesRegex(RuntimeError, "refresh failed"):
                    refresh.result(timeout=2)
        finally:
            release_margin_call.set()
            self.main._account_status_snapshot.clear()
            self.main._account_status_snapshot.update(original_snapshot)


class AccountStatusSnapshotEndpointTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import api.main as main

        self.main = main
        self.original_snapshot = dict(main._account_status_snapshot)
        main._account_status_snapshot.clear()
        main._account_status_snapshot.update({
            "data": {
                "accounts": [{"account_id": 1, "positions": []}],
                "summary": {"equity": 100},
            },
            "time": time.time() - 60,
            "last_error": "exchange unavailable",
        })

    async def asyncTearDown(self):
        self.main._account_status_snapshot.clear()
        self.main._account_status_snapshot.update(self.original_snapshot)

    async def test_snapshot_http_read_does_not_start_a_refresh(self):
        response = Response()
        with patch.object(self.main, "_ensure_trading_account_status_refresh") as refresh:
            payload = await self.main.get_all_trading_account_status(response, user="viewer")

        refresh.assert_not_called()
        self.assertEqual(payload["accounts"][0]["account_id"], 1)
        self.assertEqual(payload["summary"]["equity"], 100)
        self.assertIn("snapshot_at", payload)
        self.assertGreaterEqual(payload["age_seconds"], 60)
        self.assertFalse(payload["fresh"])
        self.assertEqual(payload["last_error"], "exchange unavailable")

    async def test_failed_refresh_keeps_last_successful_snapshot(self):
        old_data = self.main._account_status_snapshot["data"]
        with self.assertLogs("api", level="ERROR"), patch.object(
            self.main,
            "_refresh_all_account_statuses_sync",
            side_effect=RuntimeError("exchange unavailable"),
        ), patch.object(self.main, "save_account_snapshot") as save:
            payload = await self.main._run_trading_account_status_refresh()

        self.assertIs(payload, old_data)
        self.assertIs(self.main._account_status_snapshot["data"], old_data)
        self.assertEqual(self.main._account_status_snapshot["last_error"], "exchange unavailable")
        save.assert_not_called()

    async def test_loaded_snapshot_is_sanitized_before_it_is_served(self):
        original_path = self.main._ACCOUNT_STATUS_SNAPSHOT_PATH
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            save_account_snapshot(path, {
                "data": _legacy_account_snapshot_data(),
                "snapshot_at": time.time(),
            })
            self.main._ACCOUNT_STATUS_SNAPSHOT_PATH = str(path)
            self.main._account_status_snapshot.update({"data": None, "time": 0.0, "last_error": None})
            try:
                self.main._load_persisted_trading_account_status_snapshot()
                payload, _ = await self.main._get_trading_account_status_snapshot()
            finally:
                self.main._ACCOUNT_STATUS_SNAPSHOT_PATH = original_path

        _assert_slim_snapshot(self, payload)

    async def test_refresh_sanitizes_snapshot_before_persisting_it(self):
        legacy_data = _legacy_account_snapshot_data()
        with patch.object(
            self.main,
            "_refresh_all_account_statuses_sync",
            return_value=legacy_data,
        ), patch.object(self.main, "save_account_snapshot") as save:
            payload = await self.main._run_trading_account_status_refresh()

        saved_data = save.call_args.args[1]["data"]
        _assert_slim_snapshot(self, payload)
        _assert_slim_snapshot(self, saved_data)


if __name__ == "__main__":
    unittest.main()
