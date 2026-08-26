import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import Response

from shared.account_status_snapshot import load_account_snapshot, save_account_snapshot


class AccountStatusSnapshotPersistenceTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
