import asyncio
import logging
import os
import signal
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from alpha_engine.scoring import AlphaScoringEngine
from shared.db import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("alpha_engine")

ALPHA_SCORE_INTERVAL_MIN = int(os.getenv("ALPHA_SCORE_INTERVAL_MIN", "5"))
ALPHA_SCORE_LIMIT = int(os.getenv("ALPHA_SCORE_LIMIT", "200"))


async def score_alpha():
    try:
        engine = AlphaScoringEngine()
        rows = engine.score_all(limit=ALPHA_SCORE_LIMIT)
        logger.info("Alpha scored %s symbols", len(rows))
    except Exception as exc:
        logger.error("Alpha scoring failed: %s", exc, exc_info=True)


async def run_once():
    init_db()
    await score_alpha()


async def main():
    init_db()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        score_alpha,
        "interval",
        minutes=ALPHA_SCORE_INTERVAL_MIN,
        id="alpha_score",
        replace_existing=True,
        next_run_time=datetime.now(tz=timezone.utc),
    )
    scheduler.start()
    logger.info("Alpha engine scheduler started")

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
    logger.info("Alpha engine stopped")


if __name__ == "__main__":
    if os.getenv("RUN_ONCE") == "1":
        asyncio.run(run_once())
    else:
        asyncio.run(main())
