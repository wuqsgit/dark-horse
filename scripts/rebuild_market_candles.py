"""One-shot backup and rebuild for the separated spot/futures candle stores."""
import argparse
import asyncio
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from alpha_pipeline.collector import AlphaCollector
from pipeline.binance_http import BinanceHTTPCollector
from shared.db import DB_PATH, RETENTION_DAYS, fetch_market_data_health, get_conn, init_db, purge_old_kline_data


def backup_database():
    source_path = Path(DB_PATH).resolve()
    backup_dir = source_path.parent / ".runtime_logs"
    backup_dir.mkdir(exist_ok=True)
    target_path = backup_dir / f"alphadog_before_market_rebuild_{datetime.now():%Y%m%d_%H%M%S}.db"
    with sqlite3.connect(source_path) as source, sqlite3.connect(target_path) as target:
        source.backup(target)
    return target_path


def clear_mixed_candles():
    conn = get_conn()
    try:
        for table in (
            "candles_15m", "candles_1h", "candles_6h", "candles_24h",
            "futures_candles_15m", "futures_candles_1h", "futures_candles_6h", "futures_candles_24h",
        ):
            conn.execute(f"DELETE FROM {table}")
        conn.execute("DELETE FROM market_universe")
        conn.commit()
    finally:
        conn.close()


async def rebuild():
    normal = BinanceHTTPCollector()
    alpha = AlphaCollector()
    try:
        universe = await normal.get_normal_universe(limit=150)
        await normal.collect_all(universe)
        await alpha.collect_all(universe_limit=200, market_top_n=80)
    finally:
        await normal.close()
        await alpha.close()
    purge_old_kline_data(days=RETENTION_DAYS)
    return fetch_market_data_health()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-backup", action="store_true")
    args = parser.parse_args()
    init_db()
    if not args.skip_backup:
        print(f"backup={backup_database()}")
    clear_mixed_candles()
    health = asyncio.run(rebuild())
    print(health)
    if any(pool["selected"] == 0 or pool["unready"] for pool in health.values()):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
