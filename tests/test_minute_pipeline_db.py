import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import shared.db as db
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


if __name__ == "__main__":
    unittest.main()
