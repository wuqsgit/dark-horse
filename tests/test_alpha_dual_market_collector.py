import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from alpha_pipeline.collector import AlphaCollector
import shared.db as db


class AlphaDualMarketCollectorTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(
            db,
            "DB_PATH",
            str(Path(self.temp_dir.name) / "market.db"),
        )
        self.db_patch.start()
        db.init_db()

    def tearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_mapped_futures_tables_never_use_normal_spot_tables(self):
        self.assertEqual(AlphaCollector.futures_table_for_interval("15m"), "futures_candles_15m")
        self.assertEqual(AlphaCollector.futures_table_for_interval("1h"), "futures_candles_1h")
        self.assertNotEqual(AlphaCollector.futures_table_for_interval("1h"), "candles_1h")

    def test_collector_rejects_testnet_market_data(self):
        with self.assertRaisesRegex(ValueError, "market environment"):
            AlphaCollector(market_env="testnet")

    def test_extended_kline_row_keeps_taker_volume_env_and_closed_state(self):
        kline = [
            1_000, "1.0", "1.2", "0.9", "1.1", "10",
            1_999, "1000", 20, "5", "580", "0",
        ]

        row = AlphaCollector.normalize_kline_row(
            "AKEUSDT",
            kline,
            market_env="mainnet",
            now_ms=2_000,
        )

        self.assertEqual(row[0], "1970-01-01T00:00:01Z")
        self.assertEqual(row[1], "AKEUSDT")
        self.assertEqual(row[9], 580.0)
        self.assertEqual(row[10], "mainnet")
        self.assertEqual(row[11], 1)

    def test_history_counts_are_environment_and_symbol_scoped(self):
        rows = [
            (
                f"2026-08-12T{i:02d}:00:00Z",
                "AKEUSDT",
                1, 1, 1, 1, 1, 1, 1, 1,
                "mainnet",
                1,
            )
            for i in range(3)
        ]
        rows.append(
            (
                "2026-08-12T04:00:00Z",
                "AKEUSDT",
                1, 1, 1, 1, 1, 1, 1, 1,
                "testnet",
                1,
            )
        )
        db.insert_futures_candles("futures_candles_1h", rows)

        counts = AlphaCollector.candle_history_counts(
            "futures_candles_1h",
            ["AKEUSDT", "MISSINGUSDT"],
            source_env="mainnet",
        )

        self.assertEqual(counts, {"AKEUSDT": 3})

    def test_collect_all_uses_persisted_universe_when_refresh_fails(self):
        collector = AlphaCollector()
        collector.refresh_universe = AsyncMock(
            side_effect=RuntimeError("token list unavailable"),
        )
        collector.reset_client = AsyncMock()
        collector.collect_market_data = AsyncMock()
        persisted = {
            "source_symbol": "ALPHA_331USDT",
            "futures_symbol": "AKEUSDT",
            "spot_quote_volume_24h": 1_000_000,
            "futures_quote_volume_24h": 2_000_000,
        }

        try:
            with patch(
                "alpha_pipeline.collector.fetch_market_universe",
                return_value=[persisted],
            ):
                result = asyncio.run(collector.collect_all())
        finally:
            asyncio.run(collector.close())

        self.assertEqual(result[0]["alpha_symbol"], "ALPHA_331USDT")
        self.assertEqual(result[0]["futures_symbol"], "AKEUSDT")
        collector.reset_client.assert_awaited_once()
        collector.collect_market_data.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
