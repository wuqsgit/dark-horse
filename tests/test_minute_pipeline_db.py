import asyncio
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import shared.db as db
from minute_pipeline.config import BOOTSTRAP_MINUTES
from minute_pipeline.main import MinutePipeline
from minute_pipeline.writer import CandleBatchWriter


def candle(index, start):
    price = 100 + index
    return {
        "time": (start + timedelta(minutes=index)).isoformat().replace(
            "+00:00",
            "Z",
        ),
        "symbol": "BTCUSDT",
        "open": price,
        "high": price + 1,
        "low": price - 1,
        "close": price + 0.5,
        "volume": 1,
        "quote_vol": 100,
        "trades": 2,
        "taker_buy_quote_vol": 60,
        "source_env": "mainnet",
        "is_closed": True,
        "source": "test",
    }


class MinutePipelineDatabaseTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "minute.db")
        self.db_patch = patch.object(db, "DB_PATH", self.db_path)
        self.db_patch.start()
        db.init_db()

    async def asyncTearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    async def test_writer_persists_and_aggregates_complete_bucket(self):
        start = datetime(2026, 8, 7, 0, 0, tzinfo=timezone.utc)
        writer = CandleBatchWriter()
        rows = [candle(index, start) for index in range(15)]

        await writer._write_batch([("spot", row) for row in rows])

        conn = sqlite3.connect(self.db_path)
        try:
            minute_count = conn.execute(
                "SELECT COUNT(*) FROM candles_1m"
            ).fetchone()[0]
            aggregate = conn.execute(
                """SELECT minute_count, is_complete, comparison_status
                   FROM aggregated_candles
                   WHERE market_kind='spot' AND interval='15m'"""
            ).fetchone()
            published = conn.execute(
                """SELECT open, high, low, close, volume, trades
                   FROM candles_15m
                   WHERE symbol='BTCUSDT'"""
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(minute_count, 15)
        self.assertEqual(aggregate, (15, 1, "missing_reference"))
        self.assertEqual(published, (100.0, 115.0, 99.0, 114.5, 15.0, 30))

    async def test_writer_publishes_extended_futures_candle(self):
        start = datetime(2026, 8, 7, 0, 0, tzinfo=timezone.utc)
        writer = CandleBatchWriter()
        rows = [candle(index, start) for index in range(15)]

        await writer._write_batch([("futures", row) for row in rows])

        conn = sqlite3.connect(self.db_path)
        try:
            published = conn.execute(
                """SELECT source_env, is_closed, taker_buy_quote_vol
                   FROM futures_candles_15m
                   WHERE symbol='BTCUSDT'"""
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(published, ("mainnet", 1, 900.0))

    async def test_writer_records_detected_gap(self):
        start = datetime(2026, 8, 7, 0, 0, tzinfo=timezone.utc)
        writer = CandleBatchWriter()

        await writer._write_batch(
            [
                ("futures", candle(0, start)),
                ("futures", candle(2, start)),
            ]
        )

        gaps = db.fetch_candle_gaps("pending")
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["symbol"], "BTCUSDT")
        self.assertEqual(gaps[0]["start_time"], "2026-08-07T00:01:00Z")
        self.assertEqual(gaps[0]["end_time"], "2026-08-07T00:01:00Z")

    async def test_writer_retries_locked_database_without_losing_batch(self):
        writer = CandleBatchWriter(
            retry_base_seconds=0.001,
            retry_max_seconds=0.001,
        )
        batch = [("spot", {"symbol": "BTCUSDT", "time": "now"})]

        with patch.object(
            writer,
            "_write_batch",
            AsyncMock(
                side_effect=[
                    sqlite3.OperationalError("database is locked"),
                    None,
                ]
            ),
        ) as write:
            await writer._write_batch_resilient(batch)

        self.assertEqual(write.await_count, 2)
        self.assertEqual(writer.retry_count, 1)
        self.assertIsNone(writer.last_error)

    async def test_resilient_loop_restarts_after_database_lock(self):
        pipeline = object.__new__(MinutePipeline)
        pipeline.stop = asyncio.Event()
        calls = 0

        async def operation():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise sqlite3.OperationalError("database is locked")
            pipeline.stop.set()

        with patch.object(pipeline, "_sleep", AsyncMock()):
            await pipeline._run_resilient("test-loop", operation)

        self.assertEqual(calls, 2)

    async def test_recent_replay_excludes_old_aggregates(self):
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        recent_start = (now - timedelta(hours=1)).replace(
            minute=((now - timedelta(hours=1)).minute // 15) * 15
        )
        old_start = (now - timedelta(days=2)).replace(
            minute=((now - timedelta(days=2)).minute // 15) * 15
        )
        recent = [candle(index, recent_start) for index in range(15)]
        old = [candle(index, old_start) for index in range(15)]
        writer = CandleBatchWriter()
        await writer._write_batch([("spot", row) for row in recent + old])

        conn = db.get_conn()
        try:
            conn.execute("DELETE FROM candles_15m")
            conn.commit()
        finally:
            conn.close()

        published = db.materialize_stored_aggregates(since_hours=12)
        conn = db.get_conn()
        try:
            rows = conn.execute(
                "SELECT time FROM candles_15m ORDER BY time"
            ).fetchall()
        finally:
            conn.close()

        self.assertEqual(published, 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["time"], recent[0]["time"])

    async def test_bootstrap_requeues_only_current_stored_candle_for_aggregation(self):
        start = datetime(2026, 8, 7, 0, 0, tzinfo=timezone.utc)
        end = start + timedelta(minutes=180)
        current = candle(179, start)
        db.upsert_minute_candles("spot", [current])

        pipeline = object.__new__(MinutePipeline)
        pipeline.universe = SimpleNamespace(
            spot=("BTCUSDT",),
            futures=(),
            alpha=(),
        )
        pipeline.bootstrap_semaphore = asyncio.Semaphore(1)
        pipeline.alpha_semaphore = asyncio.Semaphore(1)
        pipeline.rest = SimpleNamespace(fetch=AsyncMock(return_value=[current]))
        pipeline.writer = SimpleNamespace(
            put=AsyncMock(),
            queue=SimpleNamespace(qsize=lambda: 0),
        )

        await pipeline._bootstrap_symbol("spot", "BTCUSDT", start, end)

        pipeline.rest.fetch.assert_awaited_once_with(
            "spot",
            "BTCUSDT",
            start_time=start,
            end_time=end,
            limit=BOOTSTRAP_MINUTES + 2,
        )
        pipeline.writer.put.assert_awaited_once_with("spot", current)

    async def test_bootstrap_skips_complete_local_window(self):
        end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        start = end - timedelta(minutes=BOOTSTRAP_MINUTES)
        rows = [candle(index, start) for index in range(BOOTSTRAP_MINUTES)]
        db.upsert_minute_candles("spot", rows)

        pipeline = object.__new__(MinutePipeline)
        pipeline.bootstrap_semaphore = asyncio.Semaphore(1)
        pipeline.alpha_semaphore = asyncio.Semaphore(1)
        pipeline.rest = SimpleNamespace(fetch=AsyncMock())
        pipeline.writer = SimpleNamespace(put=AsyncMock())

        await pipeline._bootstrap_symbol("spot", "BTCUSDT", start, end)

        pipeline.rest.fetch.assert_not_awaited()
        pipeline.writer.put.assert_awaited()
        replayed_times = {
            call.args[1]["time"] for call in pipeline.writer.put.await_args_list
        }
        self.assertLessEqual(len(replayed_times), 2)
        self.assertTrue(replayed_times)

    async def test_bootstrap_only_fetches_tail_of_complete_stale_window(self):
        end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        start = end - timedelta(minutes=BOOTSTRAP_MINUTES)
        rows = [candle(index, start) for index in range(BOOTSTRAP_MINUTES)]
        db.upsert_minute_candles("spot", rows)
        latest = rows[-1]
        recovered = candle(BOOTSTRAP_MINUTES, start)

        pipeline = object.__new__(MinutePipeline)
        pipeline.bootstrap_semaphore = asyncio.Semaphore(1)
        pipeline.alpha_semaphore = asyncio.Semaphore(1)
        pipeline.rest = SimpleNamespace(fetch=AsyncMock(return_value=[recovered]))
        pipeline.writer = SimpleNamespace(put=AsyncMock())

        await pipeline._bootstrap_symbol(
            "spot",
            "BTCUSDT",
            start,
            end + timedelta(minutes=1),
        )

        pipeline.rest.fetch.assert_awaited_once_with(
            "spot",
            "BTCUSDT",
            start_time=datetime.fromisoformat(
                latest["time"].replace("Z", "+00:00")
            ) + timedelta(minutes=1),
            end_time=end + timedelta(minutes=1),
            limit=BOOTSTRAP_MINUTES + 2,
        )
        calls = [call.args for call in pipeline.writer.put.await_args_list]
        self.assertIn(("spot", recovered), calls)


if __name__ == "__main__":
    unittest.main()
