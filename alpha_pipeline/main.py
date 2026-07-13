import asyncio
import logging
import os
import signal
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from alpha_pipeline.collector import AlphaCollector
from shared.db import init_db, purge_old_kline_data, RETENTION_DAYS

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("alpha_pipeline")

ALPHA_UNIVERSE_LIMIT = int(os.getenv("ALPHA_UNIVERSE_LIMIT", "200"))
ALPHA_MARKET_TOP_N = int(os.getenv("ALPHA_MARKET_TOP_N", "80"))
ALPHA_COLLECT_INTERVAL_MIN = int(os.getenv("ALPHA_COLLECT_INTERVAL_MIN", "10"))


async def collect_alpha(collector):
    logger.info("=== Alpha collect ===")
    try:
        purge_old_kline_data(days=RETENTION_DAYS)
        await collector.collect_all(
            universe_limit=ALPHA_UNIVERSE_LIMIT,
            market_top_n=ALPHA_MARKET_TOP_N,
        )
    except Exception as exc:
        logger.error("Alpha collect failed: %s", exc, exc_info=True)
    logger.info("=== Alpha collect done ===")


async def run_once():
    init_db()
    collector = AlphaCollector()
    try:
        await collect_alpha(collector)
    finally:
        await collector.close()


async def main():
    init_db()
    collector = AlphaCollector()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        collect_alpha,
        "interval",
        minutes=ALPHA_COLLECT_INTERVAL_MIN,
        args=[collector],
        id="alpha_collect",
        replace_existing=True,
        next_run_time=datetime.now(tz=timezone.utc),
    )
    scheduler.start()
    logger.info("Alpha pipeline scheduler started")

    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_event_loop().add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass

    try:
        await stop.wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    scheduler.shutdown()
    await collector.close()
    logger.info("Alpha pipeline stopped")


if __name__ == "__main__":
    if os.getenv("RUN_ONCE") == "1":
        asyncio.run(run_once())
    else:
        asyncio.run(main())
