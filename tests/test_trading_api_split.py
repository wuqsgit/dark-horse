import asyncio
import os
import statistics
import subprocess
import sys
import threading
import time
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import httpx
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
        self.original_runtime_snapshot = dict(main._runtime_status_snapshot)
        self.original_runtime_refresh_task = main._runtime_status_refresh_task
        main._runtime_status_snapshot.clear()
        main._runtime_status_snapshot.update({
            "data": None,
            "time": 0.0,
            "last_error": None,
        })
        main._runtime_status_refresh_task = None
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
                        "2026-08-20 01:00:00", "2026-08-20 02:00:00",
                        100000, 101000, 0.01, 10, 1, 1, "exchange_income", "normal",
                    ),
                    (
                        self.account_id, "BTCUSDT-cycle-2", "BTCUSDT", "LONG",
                        "2026-08-21 01:00:00", "2026-08-21 02:00:00",
                        102000, 101000, 0.01, -10, -1, 1, "exchange_income", "alpha",
                    ),
                    (
                        self.account_id, "ETHUSDT-cycle-1", "ETHUSDT", "SHORT",
                        "2026-08-22 01:00:00", "2026-08-22 02:00:00",
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
        task = self.main._runtime_status_refresh_task
        if task is not None and not task.done():
            task.cancel()
        self.main._runtime_status_snapshot.clear()
        self.main._runtime_status_snapshot.update(self.original_runtime_snapshot)
        self.main._runtime_status_refresh_task = self.original_runtime_refresh_task
        db.DB_PATH = self.original_db_path
        self.temp.cleanup()

    async def _request_slow_route_and_snapshot(self, path):
        transport = httpx.ASGITransport(app=self.main.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            scheduled_at = time.monotonic()
            slow_request = asyncio.create_task(client.get(path))
            snapshot_request = asyncio.create_task(
                client.get("/api/trading/accounts/status")
            )
            snapshot = await snapshot_request
            snapshot_elapsed = time.monotonic() - scheduled_at
            slow_response = await slow_request
        return slow_response, snapshot, snapshot_elapsed

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
            ({"limit": 0}, "invalid_limit"),
            ({"limit": 101}, "invalid_limit"),
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

    def test_history_route_normalizes_iso_filters_to_db_timestamps(self):
        conn = db.get_conn()
        try:
            conn.executemany(
                """INSERT INTO fills
                   (account_id, symbol, side, position_side, quantity, price,
                    trade_id, created_at, strategy_source)
                   VALUES (?, 'SOLUSDT', ?, 'LONG', 1, ?, ?, ?, 'normal')""",
                [
                    (
                        self.account_id,
                        "BUY",
                        100,
                        "sol-open",
                        "2026-08-24 01:00:00",
                    ),
                    (
                        self.account_id,
                        "SELL",
                        110,
                        "sol-close",
                        "2026-08-24 02:00:00",
                    ),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        response = self.client.get(
            f"/api/trading/accounts/{self.account_id}/history",
            params={
                "from": "2026-08-24T02:00:00Z",
                "to": "2026-08-24T02:00:00Z",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [(row["symbol"], row["side"]) for row in response.json()["items"]],
            [("SOLUSDT", "LONG")],
        )

    def test_history_and_decisions_reject_unknown_accounts(self):
        for suffix in ("history", "decisions"):
            with self.subTest(suffix=suffix):
                response = self.client.get(f"/api/trading/accounts/999/{suffix}")
                self.assertEqual(response.status_code, 404)
                detail = response.json()["detail"]
                self.assertIsInstance(detail, dict)
                self.assertEqual(detail["code"], "account_not_found")

    def test_decisions_and_runtime_status_return_local_account_data(self):
        runtime_data = self.main._trading_runtime_status_payload()
        self.main._runtime_status_snapshot.update({
            "data": runtime_data,
            "time": time.time(),
            "last_error": None,
        })
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

    def test_slow_history_computation_does_not_block_snapshot_reads(self):
        helper_threads = []

        def slow_history(*_args, **_kwargs):
            helper_threads.append(threading.get_ident())
            time.sleep(0.4)
            return {
                "items": [],
                "next_cursor": None,
                "stats": {},
                "reconcile_status": "ok",
            }

        event_loop_thread = threading.get_ident()
        with patch(
            "shared.trade_history.fetch_trade_history_summaries",
            side_effect=slow_history,
        ):
            slow, snapshot, elapsed = asyncio.run(
                self._request_slow_route_and_snapshot(
                    f"/api/trading/accounts/{self.account_id}/history"
                )
            )

        self.assertEqual(slow.status_code, 200)
        self.assertEqual(snapshot.status_code, 200)
        self.assertLess(elapsed, 0.25)
        self.assertEqual(len(helper_threads), 1)
        self.assertNotEqual(helper_threads[0], event_loop_thread)

    def test_slow_decision_sqlite_work_does_not_block_snapshot_reads(self):
        event_loop_thread = threading.get_ident()
        connection_threads = {"open": [], "close": []}
        panel_threads = []
        real_get_conn = db.get_conn

        class TrackedConnection:
            def __init__(self, conn):
                self.conn = conn

            def __getattr__(self, name):
                return getattr(self.conn, name)

            def close(self):
                connection_threads["close"].append(threading.get_ident())
                self.conn.close()

        def tracked_get_conn():
            connection_threads["open"].append(threading.get_ident())
            return TrackedConnection(real_get_conn())

        def slow_panel(_conn, _account_id):
            panel_threads.append(threading.get_ident())
            time.sleep(0.4)
            return {"latest_run_id": "slow-local-run", "recent": []}

        with patch("shared.db.get_conn", side_effect=tracked_get_conn), patch.object(
            self.main,
            "_account_decision_panel",
            side_effect=slow_panel,
        ):
            slow, snapshot, elapsed = asyncio.run(
                self._request_slow_route_and_snapshot(
                    f"/api/trading/accounts/{self.account_id}/decisions"
                )
            )

        self.assertEqual(slow.status_code, 200)
        self.assertEqual(snapshot.status_code, 200)
        self.assertLess(elapsed, 0.25)
        self.assertEqual(connection_threads["open"], panel_threads)
        self.assertEqual(connection_threads["close"], panel_threads)
        self.assertNotEqual(panel_threads[0], event_loop_thread)

    def test_slow_runtime_local_reads_do_not_block_or_use_exchange(self):
        from shared.accounts import list_accounts

        configured_accounts = list_accounts()
        event_loop_thread = threading.get_ident()
        runtime_threads = {"accounts": [], "diagnostics": []}

        def slow_list_accounts(*_args, **_kwargs):
            runtime_threads["accounts"].append(threading.get_ident())
            time.sleep(0.4)
            return configured_accounts

        def local_diagnostics(account):
            runtime_threads["diagnostics"].append(threading.get_ident())
            return {"status": "healthy", "account_id": account["id"]}

        with patch(
            "shared.accounts.list_accounts",
            side_effect=slow_list_accounts,
        ), patch(
            "shared.live_diagnostics.build_live_diagnostics",
            side_effect=local_diagnostics,
        ), patch(
            "shared.accounts.account_exchange_config",
            side_effect=AssertionError("runtime must not validate credentials"),
        ), patch(
            "trader.exchange.BinanceFutures",
            side_effect=AssertionError("runtime must not instantiate Binance"),
        ):
            slow, snapshot, elapsed = asyncio.run(
                self._request_slow_route_and_snapshot(
                    "/api/trading/runtime/status"
                )
            )

        self.assertEqual(slow.status_code, 200)
        self.assertEqual(snapshot.status_code, 200)
        self.assertLess(elapsed, 0.25)
        self.assertEqual(runtime_threads["accounts"], runtime_threads["diagnostics"])
        self.assertNotEqual(runtime_threads["accounts"][0], event_loop_thread)

    def test_local_read_routes_remain_responsive_during_exchange_timeout(self):
        child_flag = "DARK_HORSE_TIMEOUT_ISOLATION_CHILD"
        if os.environ.get(child_flag) != "1":
            environment = os.environ.copy()
            environment[child_flag] = "1"
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "unittest",
                    (
                        "tests.test_trading_api_split."
                        "TradingApiReplacementRoutesTest."
                        "test_local_read_routes_remain_responsive_during_exchange_timeout"
                    ),
                    "-q",
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(
                result.returncode,
                0,
                result.stdout + result.stderr,
            )
            return

        entered_margin_call = threading.Event()
        release_margin_call = threading.Event()
        margin_call_threads = []
        replacement_paths = (
            "/api/trading/accounts",
            "/api/trading/accounts/status",
            f"/api/trading/accounts/{self.account_id}/history",
            f"/api/trading/accounts/{self.account_id}/decisions",
            "/api/trading/runtime/status",
        )
        expected_paths = {
            "/api/trading/accounts",
            "/api/trading/accounts/status",
            f"/api/trading/accounts/{self.account_id}/history",
            f"/api/trading/accounts/{self.account_id}/decisions",
            "/api/trading/runtime/status",
        }

        self.assertEqual(set(replacement_paths), expected_paths)

        async def measure_routes(*, warm_up=False):
            measurements = {}
            transport = httpx.ASGITransport(app=self.main.app)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as client:
                for path in replacement_paths:
                    if warm_up:
                        response = await client.get(path)
                        self.assertEqual(response.status_code, 200, path)
                    samples = []
                    for _ in range(3):
                        started = time.perf_counter()
                        response = await client.get(path)
                        samples.append(time.perf_counter() - started)
                        self.assertEqual(response.status_code, 200, path)
                    measurements[path] = samples
            return measurements

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
            baseline_measurements = asyncio.run(measure_routes(warm_up=True))

            with patch(
                "trader.exchange.BinanceFutures.get_margin_balance",
                new=delayed_timeout,
            ), ThreadPoolExecutor(max_workers=1) as executor:
                refresh = executor.submit(self.main._refresh_all_account_statuses_sync)
                self.assertTrue(entered_margin_call.wait(timeout=2))
                background_thread = margin_call_threads[0]

                timeout_measurements = asyncio.run(measure_routes())

                self.timeout_latency_measurements = {}
                for path in replacement_paths:
                    baseline_median = statistics.median(baseline_measurements[path])
                    timeout_median = statistics.median(timeout_measurements[path])
                    self.timeout_latency_measurements[path] = {
                        "baseline_median_ms": baseline_median * 1000,
                        "timeout_median_ms": timeout_median * 1000,
                        "timeout_max_ms": max(timeout_measurements[path]) * 1000,
                    }
                    for elapsed in timeout_measurements[path]:
                        self.assertLess(elapsed, 0.1, path)
                        self.assertLessEqual(
                            elapsed,
                            baseline_median + 0.03,
                            path,
                        )
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


class RuntimeStatusSnapshotEndpointTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import api.main as main

        self.main = main
        self.original_snapshot = dict(getattr(
            main,
            "_runtime_status_snapshot",
            {"data": None, "time": 0.0, "last_error": None},
        ))
        self.original_refresh_task = getattr(
            main,
            "_runtime_status_refresh_task",
            None,
        )
        main._runtime_status_snapshot = {
            "data": None,
            "time": 0.0,
            "last_error": None,
        }
        main._runtime_status_refresh_task = None

    async def asyncTearDown(self):
        task = self.main._runtime_status_refresh_task
        if task is not None and not task.done():
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.main._runtime_status_snapshot = self.original_snapshot
        self.main._runtime_status_refresh_task = self.original_refresh_task

    async def test_stale_snapshot_returns_before_slow_background_refresh(self):
        old_data = {
            "trading_controls": {"normal_trading_enabled": True},
            "accounts": [{"account_id": 1, "runtime_diagnostics": {"status": "healthy"}}],
        }
        new_data = {
            "trading_controls": {"normal_trading_enabled": False},
            "accounts": [{"account_id": 1, "runtime_diagnostics": {"status": "degraded"}}],
        }
        self.main._runtime_status_snapshot.update({
            "data": old_data,
            "time": time.time() - 31,
            "last_error": "previous refresh failed",
        })
        entered = threading.Event()

        def slow_payload():
            entered.set()
            time.sleep(0.15)
            return new_data

        response = Response()
        with patch.object(
            self.main,
            "_trading_runtime_status_payload",
            side_effect=slow_payload,
        ):
            started = time.perf_counter()
            payload = await self.main.get_trading_runtime_status(
                response,
                user="viewer",
            )
            elapsed = time.perf_counter() - started
            refresh_task = self.main._runtime_status_refresh_task
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            await refresh_task

        self.assertLess(elapsed, 0.05)
        self.assertEqual(response.headers["X-Cache"], "STALE")
        self.assertEqual(payload["accounts"], old_data["accounts"])
        self.assertFalse(payload["fresh"])
        self.assertGreaterEqual(payload["age_seconds"], 31)
        self.assertIsNotNone(payload["snapshot_at"])
        self.assertEqual(payload["last_error"], "previous refresh failed")
        self.assertIs(self.main._runtime_status_snapshot["data"], new_data)
        self.assertTrue(self.main._runtime_status_snapshot["time"] > 0)
        self.assertIsNone(self.main._runtime_status_snapshot["last_error"])

    async def test_stale_requests_share_one_background_refresh(self):
        self.main._runtime_status_snapshot.update({
            "data": {"trading_controls": {}, "accounts": []},
            "time": time.time() - 31,
            "last_error": None,
        })
        entered = threading.Event()
        release = threading.Event()
        calls = []

        def blocked_payload():
            calls.append(threading.get_ident())
            entered.set()
            release.wait(timeout=2)
            return {"trading_controls": {}, "accounts": []}

        try:
            with patch.object(
                self.main,
                "_trading_runtime_status_payload",
                side_effect=blocked_payload,
            ):
                await self.main.get_trading_runtime_status(Response(), user="viewer")
                first_task = self.main._runtime_status_refresh_task
                await self.main.get_trading_runtime_status(Response(), user="viewer")
                second_task = self.main._runtime_status_refresh_task
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                self.assertIs(first_task, second_task)
                self.assertEqual(len(calls), 1)
                release.set()
                await first_task
        finally:
            release.set()

    async def test_refresh_failure_preserves_last_good_runtime_snapshot(self):
        old_data = {
            "trading_controls": {"normal_trading_enabled": True},
            "accounts": [{"account_id": 1}],
        }
        self.main._runtime_status_snapshot.update({
            "data": old_data,
            "time": time.time() - 31,
            "last_error": None,
        })

        with self.assertLogs("api", level="ERROR"), patch.object(
            self.main,
            "_trading_runtime_status_payload",
            side_effect=RuntimeError("diagnostics unavailable"),
        ):
            payload = await self.main.get_trading_runtime_status(
                Response(),
                user="viewer",
            )
            refresh_task = self.main._runtime_status_refresh_task
            await refresh_task

        self.assertEqual(payload["accounts"], old_data["accounts"])
        self.assertIs(self.main._runtime_status_snapshot["data"], old_data)
        self.assertEqual(
            self.main._runtime_status_snapshot["last_error"],
            "diagnostics unavailable",
        )

    async def test_runtime_refresh_uses_only_local_account_data(self):
        account = {
            "id": 1,
            "name": "local-only",
            "environment": "testnet",
        }
        worker_threads = []
        event_loop_thread = threading.get_ident()

        def local_accounts(*_args, **_kwargs):
            worker_threads.append(threading.get_ident())
            return [account]

        def local_diagnostics(item):
            worker_threads.append(threading.get_ident())
            return {"status": "healthy", "account_id": item["id"]}

        with patch(
            "shared.accounts.list_accounts",
            side_effect=local_accounts,
        ), patch(
            "shared.live_diagnostics.build_live_diagnostics",
            side_effect=local_diagnostics,
        ), patch.object(
            self.main,
            "_safe_trading_runtime_controls",
            return_value={"normal_trading_enabled": True},
        ), patch(
            "shared.accounts.account_exchange_config",
            side_effect=AssertionError("runtime must not validate credentials"),
        ), patch(
            "trader.exchange.BinanceFutures",
            side_effect=AssertionError("runtime must not instantiate Binance"),
        ):
            payload = await self.main._run_trading_runtime_status_refresh()

        self.assertEqual(payload["accounts"][0]["account_id"], 1)
        self.assertEqual(worker_threads[0], worker_threads[1])
        self.assertNotEqual(worker_threads[0], event_loop_thread)


if __name__ == "__main__":
    unittest.main()
