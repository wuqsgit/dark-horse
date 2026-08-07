import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import shared.db as db
from pipeline.candle_health import refresh_universe_readiness


class AlphaFuturesEnvironmentTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "market.db")
        self.db_patch = patch.object(db, "DB_PATH", self.db_path)
        self.db_patch.start()
        db.init_db()

    def tearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_open_interest_reads_are_scoped_to_market_environment(self):
        now = datetime.now(timezone.utc)
        db.insert_futures(
            [
                (
                    (now - timedelta(seconds=2)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "AKEUSDT",
                    100,
                    0.0001,
                    1.0,
                    "mainnet",
                ),
                (
                    (now - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "AKEUSDT",
                    25,
                    0.0,
                    0.9,
                    "testnet",
                ),
            ]
        )

        mainnet = db.fetch_futures(
            ["AKEUSDT"],
            source_env="mainnet",
        )
        testnet = db.fetch_futures(
            ["AKEUSDT"],
            source_env="testnet",
        )

        self.assertEqual([row["open_interest"] for row in mainnet], [100])
        self.assertEqual([row["open_interest"] for row in testnet], [25])
        self.assertEqual(mainnet[0]["source_env"], "mainnet")
        self.assertEqual(testnet[0]["source_env"], "testnet")

    def test_legacy_open_interest_rows_default_to_mainnet(self):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        db.insert_futures([(now, "BANKUSDT", 50, 0.0, 0.5)])

        mainnet = db.fetch_futures(
            ["BANKUSDT"],
            source_env="mainnet",
        )
        testnet = db.fetch_futures(
            ["BANKUSDT"],
            source_env="testnet",
        )

        self.assertEqual(len(mainnet), 1)
        self.assertEqual(testnet, [])

    @staticmethod
    def _candle_rows(symbol, latest, count, step, market_env):
        return [
            (
                (latest - step * index).strftime("%Y-%m-%dT%H:%M:%SZ"),
                symbol,
                1.0,
                1.1,
                0.9,
                1.0,
                100.0,
                100.0,
                10,
                50.0,
                market_env,
                1,
            )
            for index in range(count)
        ]

    @staticmethod
    def _aggregates(market_kind, symbol, interval, latest, count, step, market_env):
        expected_count = int(step.total_seconds() // 60)
        return [
            {
                "market_kind": market_kind,
                "source_env": market_env,
                "symbol": symbol,
                "interval": interval,
                "time": (latest - step * index).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "open": 1.0,
                "high": 1.1,
                "low": 0.9,
                "close": 1.0,
                "volume": 100.0,
                "quote_vol": 100.0,
                "trades": 10,
                "taker_buy_quote_vol": 50.0,
                "minute_count": expected_count,
                "expected_count": expected_count,
                "is_complete": True,
            }
            for index in range(count)
        ]

    def test_candle_freshness_is_interval_and_environment_scoped(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        db.insert_futures_candles(
            "futures_candles_15m",
            self._candle_rows(
                "AKEUSDT",
                now,
                1,
                timedelta(minutes=15),
                "testnet",
            )
            + self._candle_rows(
                "AKEUSDT",
                now,
                1,
                timedelta(minutes=15),
                "mainnet",
            ),
        )
        db.insert_futures_candles(
            "futures_candles_1h",
            self._candle_rows(
                "AKEUSDT",
                now,
                1,
                timedelta(hours=1),
                "mainnet",
            )
            + self._candle_rows(
                "AKEUSDT",
                now - timedelta(hours=2),
                1,
                timedelta(hours=1),
                "testnet",
            ),
        )

        self.assertTrue(
            db.futures_candles_current(
                "AKEUSDT",
                source_env="testnet",
                table="futures_candles_15m",
            )
        )
        self.assertTrue(
            db.futures_candles_current(
                "AKEUSDT",
                max_age_minutes=75,
                source_env="mainnet",
                table="futures_candles_1h",
            )
        )
        self.assertFalse(
            db.futures_candles_current(
                "AKEUSDT",
                max_age_minutes=75,
                source_env="testnet",
                table="futures_candles_1h",
            )
        )
        mainnet = db.fetch_futures_candles(
            "futures_candles_15m",
            ["AKEUSDT"],
            source_env="mainnet",
        )
        testnet = db.fetch_futures_candles(
            "futures_candles_15m",
            ["AKEUSDT"],
            source_env="testnet",
        )
        self.assertEqual(len(mainnet), 1)
        self.assertEqual(len(testnet), 1)

    def test_closed_candle_freshness_ignores_recent_open_candle(self):
        now = datetime.now(timezone.utc).replace(microsecond=0)
        rows = self._candle_rows(
            "AKEUSDT",
            now - timedelta(hours=1),
            1,
            timedelta(minutes=15),
            "mainnet",
        )
        open_row = list(
            self._candle_rows(
                "AKEUSDT",
                now,
                1,
                timedelta(minutes=15),
                "mainnet",
            )[0]
        )
        open_row[-1] = 0
        db.insert_futures_candles(
            "futures_candles_15m",
            [*rows, tuple(open_row)],
        )

        self.assertTrue(
            db.futures_candles_current(
                "AKEUSDT",
                max_age_minutes=20,
                source_env="mainnet",
                table="futures_candles_15m",
            )
        )
        self.assertFalse(
            db.futures_candles_current(
                "AKEUSDT",
                max_age_minutes=20,
                source_env="mainnet",
                table="futures_candles_15m",
                closed_only=True,
            )
        )

    def test_alpha_readiness_uses_requested_futures_environment(self):
        now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        latest_15m = now.replace(
            minute=(now.minute // 15) * 15
        ) - timedelta(minutes=15)
        latest_1h = now.replace(minute=0) - timedelta(hours=1)
        db.upsert_market_universe(
            [
                {
                    "pool_type": "alpha",
                    "source_symbol": "ALPHA_331USDT",
                    "spot_symbol": "ALPHA_331USDT",
                    "futures_symbol": "AKEUSDT",
                    "selected": True,
                    "data_ready": False,
                }
            ]
        )
        db.insert_alpha_candles(
            "alpha_candles_15m",
            self._candle_rows(
                "ALPHA_331USDT",
                now,
                32,
                timedelta(minutes=15),
                "mainnet",
            ),
        )
        db.insert_alpha_candles(
            "alpha_candles_1h",
            self._candle_rows(
                "ALPHA_331USDT",
                now,
                50,
                timedelta(hours=1),
                "mainnet",
            ),
        )
        for market_env, latest in (
            ("mainnet", now),
            ("testnet", now - timedelta(hours=2)),
        ):
            db.insert_futures_candles(
                "futures_candles_15m",
                self._candle_rows(
                    "AKEUSDT",
                    latest,
                    32,
                    timedelta(minutes=15),
                    market_env,
                ),
            )
            db.insert_futures_candles(
                "futures_candles_1h",
                self._candle_rows(
                    "AKEUSDT",
                    latest,
                    50,
                    timedelta(hours=1),
                    market_env,
                ),
            )

        db.upsert_aggregated_candles(
            self._aggregates(
                "alpha", "ALPHA_331USDT", "15m",
                latest_15m, 32, timedelta(minutes=15), "mainnet",
            )
            + self._aggregates(
                "alpha", "ALPHA_331USDT", "1h",
                latest_1h, 50, timedelta(hours=1), "mainnet",
            )
            + self._aggregates(
                "futures", "AKEUSDT", "15m",
                latest_15m, 32, timedelta(minutes=15), "mainnet",
            )
            + self._aggregates(
                "futures", "AKEUSDT", "1h",
                latest_1h, 50, timedelta(hours=1), "mainnet",
            )
            + self._aggregates(
                "futures", "AKEUSDT", "15m",
                latest_15m - timedelta(hours=2), 32,
                timedelta(minutes=15), "testnet",
            )
            + self._aggregates(
                "futures", "AKEUSDT", "1h",
                latest_1h - timedelta(hours=2), 50,
                timedelta(hours=1), "testnet",
            )
        )

        mainnet = refresh_universe_readiness(
            "alpha",
            now=now,
            futures_source_env="mainnet",
        )
        testnet = refresh_universe_readiness(
            "alpha",
            now=now,
            futures_source_env="testnet",
        )

        self.assertTrue(mainnet["ALPHA_331USDT"].ready)
        self.assertFalse(testnet["ALPHA_331USDT"].ready)
        self.assertIn(
            "futures_1h_age",
            testnet["ALPHA_331USDT"].error,
        )

    def test_init_db_migrates_legacy_futures_primary_keys(self):
        conn = db.get_conn()
        try:
            for table in (
                "futures_candles_15m",
                "futures_candles_1h",
                "futures_candles_6h",
                "futures_candles_24h",
            ):
                conn.execute(f"DROP TABLE {table}")
                conn.execute(
                    f"""CREATE TABLE {table} (
                        time TEXT,
                        symbol TEXT,
                        open REAL,
                        high REAL,
                        low REAL,
                        close REAL,
                        volume REAL,
                        quote_vol REAL,
                        trades INTEGER,
                        taker_buy_quote_vol REAL,
                        source_env TEXT NOT NULL DEFAULT 'mainnet',
                        is_closed INTEGER NOT NULL DEFAULT 1,
                        PRIMARY KEY (time, symbol)
                    )"""
                )
            conn.execute("DROP TABLE futures_data")
            conn.execute(
                """CREATE TABLE futures_data (
                    time TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    open_interest REAL,
                    funding_rate REAL,
                    mark_price REAL,
                    source_env TEXT NOT NULL DEFAULT 'mainnet',
                    UNIQUE(time, symbol)
                )"""
            )
            conn.execute(
                """INSERT INTO futures_candles_1h
                   VALUES (
                     '2026-07-29T12:00:00Z','AKEUSDT',
                     1,1,1,1,1,1,1,1,'mainnet',1
                   )"""
            )
            conn.commit()
        finally:
            conn.close()

        db.init_db()
        conn = db.get_conn()
        try:
            pk = [
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_info(futures_candles_1h)"
                ).fetchall()
                if row["pk"]
            ]
            preserved = conn.execute(
                "SELECT COUNT(*) total FROM futures_candles_1h"
            ).fetchone()["total"]
        finally:
            conn.close()

        self.assertEqual(pk, ["time", "symbol", "source_env"])
        self.assertEqual(preserved, 1)


if __name__ == "__main__":
    unittest.main()
