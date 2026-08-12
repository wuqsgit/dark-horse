import asyncio
import logging
import os
import signal
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from alpha_pipeline.collector import AlphaCollector
from alpha_pipeline.square_collector import BinanceSquareCollector
from shared.db import (
    init_db,
    purge_old_kline_data,
    RETENTION_DAYS,
    upsert_service_runtime_status,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("alpha_pipeline")
logging.getLogger("httpx").setLevel(logging.WARNING)

ALPHA_UNIVERSE_LIMIT = int(os.getenv("ALPHA_UNIVERSE_LIMIT", "200"))
ALPHA_MARKET_TOP_N = int(os.getenv("ALPHA_MARKET_TOP_N", "80"))
ALPHA_COLLECT_INTERVAL_MIN = int(os.getenv("ALPHA_COLLECT_INTERVAL_MIN", "10"))
ALPHA_DERIVATIVES_INTERVAL_MIN = int(
    os.getenv("ALPHA_DERIVATIVES_INTERVAL_MIN", "5")
)
ALPHA_FAST_INTERVAL_SECONDS = int(
    os.getenv("ALPHA_FAST_INTERVAL_SECONDS", "60")
)
ALPHA_SQUARE_INTERVAL_MIN = int(
    os.getenv("ALPHA_SQUARE_INTERVAL_MIN", "5")
)


async def collect_alpha(collector):
    lock = getattr(collector, "job_lock", None)
    if lock is not None and lock.locked():
        logger.info("Alpha full collect skipped: another collector job is active")
        return
    if lock is not None:
        await lock.acquire()
    logger.info("=== Alpha collect ===")
    try:
        await collector.reset_client()
        purge_old_kline_data(days=RETENTION_DAYS)
        universe = await collector.collect_all(
            universe_limit=ALPHA_UNIVERSE_LIMIT,
            market_top_n=ALPHA_MARKET_TOP_N,
        )
        if universe:
            upsert_service_runtime_status(
                "alpha_pipeline",
                status="ok",
                details={"universe_count": len(universe), "market_env": collector.market_env},
            )
        else:
            upsert_service_runtime_status(
                "alpha_pipeline",
                status="degraded",
                error_code="alpha_universe_empty",
                last_error="Alpha 币种池为空，无法生成 Alpha 扫描数据。",
                details={"market_env": collector.market_env},
            )
    except Exception as exc:
        logger.error("Alpha collect failed: %s", exc, exc_info=True)
        upsert_service_runtime_status(
            "alpha_pipeline",
            status="error",
            error_code="alpha_market_collection_failed",
            last_error=f"{type(exc).__name__}: {exc}",
            details={"market_env": collector.market_env},
        )
    finally:
        if lock is not None and lock.locked():
            lock.release()
    logger.info("=== Alpha collect done ===")


async def collect_strategy_fast(collector):
    lock = getattr(collector, "job_lock", None)
    if lock is not None and lock.locked():
        return
    if lock is not None:
        await lock.acquire()
    try:
        result = await collector.collect_strategy_fast_data()
        if not result.get("skipped") or result.get("depth"):
            logger.info("Alpha strategy fast feed: %s", result)
    except Exception as exc:
        logger.error("Alpha strategy fast feed failed: %s", exc, exc_info=True)
    finally:
        if lock is not None and lock.locked():
            lock.release()


async def collect_derivatives(collector):
    lock = getattr(collector, "job_lock", None)
    if lock is not None and lock.locked():
        return
    if lock is not None:
        await lock.acquire()
    try:
        count = await collector.collect_derivatives()
        logger.info("Alpha derivatives refreshed: %s symbols", count)
    except Exception as exc:
        logger.error("Alpha derivatives refresh failed: %s", exc, exc_info=True)
    finally:
        if lock is not None and lock.locked():
            lock.release()


async def collect_square(square_collector):
    if not square_collector.enabled:
        return
    try:
        result = await square_collector.collect_once(
            limit=ALPHA_UNIVERSE_LIMIT,
        )
        logger.info(
            "Alpha Square feed: symbols=%s posts=%s snapshots=%s errors=%s",
            result.get("symbols"),
            result.get("posts"),
            result.get("snapshots"),
            len(result.get("errors") or []),
        )
    except Exception as exc:
        logger.error("Alpha Square feed failed: %s", exc, exc_info=True)


async def run_once():
    init_db()
    collector = AlphaCollector()
    square_collector = BinanceSquareCollector()
    collector.job_lock = asyncio.Lock()
    try:
        await collect_alpha(collector)
        await collect_square(square_collector)
    finally:
        await collector.close()
        await square_collector.close()


async def main():
    init_db()
    upsert_service_runtime_status(
        "alpha_pipeline",
        status="starting",
        details={"message": "Alpha 行情采集服务已启动，正在执行首轮采集。"},
    )
    collector = AlphaCollector()
    square_collector = BinanceSquareCollector()
    collector.job_lock = asyncio.Lock()
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        collect_alpha,
        "interval",
        minutes=ALPHA_COLLECT_INTERVAL_MIN,
        args=[collector],
        id="alpha_collect",
        replace_existing=True,
        next_run_time=datetime.now(tz=timezone.utc),
        max_instances=1,
        coalesce=True,
    )
    if square_collector.enabled:
        scheduler.add_job(
            collect_square,
            "interval",
            minutes=ALPHA_SQUARE_INTERVAL_MIN,
            args=[square_collector],
            id="alpha_square",
            replace_existing=True,
            next_run_time=datetime.now(tz=timezone.utc) + timedelta(seconds=30),
            max_instances=1,
            coalesce=True,
        )
    scheduler.add_job(
        collect_strategy_fast,
        "interval",
        seconds=ALPHA_FAST_INTERVAL_SECONDS,
        args=[collector],
        id="alpha_strategy_fast",
        replace_existing=True,
        next_run_time=datetime.now(tz=timezone.utc) + timedelta(seconds=120),
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        collect_derivatives,
        "interval",
        minutes=ALPHA_DERIVATIVES_INTERVAL_MIN,
        args=[collector],
        id="alpha_derivatives",
        replace_existing=True,
        next_run_time=datetime.now(tz=timezone.utc) + timedelta(seconds=60),
        max_instances=1,
        coalesce=True,
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
    await square_collector.close()
    logger.info("Alpha pipeline stopped")


if __name__ == "__main__":
    if os.getenv("RUN_ONCE") == "1":
        asyncio.run(run_once())
    else:
        asyncio.run(main())
