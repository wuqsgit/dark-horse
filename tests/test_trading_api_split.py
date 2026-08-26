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
