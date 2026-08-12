import asyncio
import logging
import os
import signal
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from alpha_engine.scoring import AlphaScoringEngine
from alpha_engine.strategy.ai_client import AlphaStrategyAIClient
from alpha_engine.strategy.state_machine import (
    AlphaStrategyStateMachine,
    StateMachineConfig,
)
from alpha_engine.strategy.worker import AlphaStrategyWorker
from shared.db import init_db, purge_old_kline_data, upsert_service_runtime_status
from trader.config import TRADING_CONFIG

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("alpha_engine")

ALPHA_SCORE_INTERVAL_MIN = int(os.getenv("ALPHA_SCORE_INTERVAL_MIN", "5"))
ALPHA_SCORE_LIMIT = int(os.getenv("ALPHA_SCORE_LIMIT", "200"))
ALPHA_STRATEGY_CFG = TRADING_CONFIG.get("alpha_strategy_v2") or {}


async def score_alpha():
    try:
        purge_old_kline_data(days=90)
        engine = AlphaScoringEngine()
        rows = engine.score_all(limit=ALPHA_SCORE_LIMIT)
        logger.info("Alpha scored %s symbols", len(rows))
        if rows:
            upsert_service_runtime_status(
                "alpha_engine",
                status="ok",
                details={"scored_count": len(rows), "strategy_mode": ALPHA_STRATEGY_CFG.get("mode")},
            )
        else:
            upsert_service_runtime_status(
                "alpha_engine",
                status="degraded",
                error_code="alpha_scores_empty",
                last_error="Alpha 策略没有生成评分结果。",
                details={"strategy_mode": ALPHA_STRATEGY_CFG.get("mode")},
            )
    except Exception as exc:
        logger.error("Alpha scoring failed: %s", exc, exc_info=True)
        upsert_service_runtime_status(
            "alpha_engine",
            status="error",
            error_code="alpha_scoring_failed",
            last_error=f"{type(exc).__name__}: {exc}",
        )


def build_strategy_worker():
    cfg = ALPHA_STRATEGY_CFG
    machine = AlphaStrategyStateMachine(
        StateMachineConfig(
            setup_watch_threshold=float(cfg.get("setup_watch_threshold", 0.55)),
            setup_arm_threshold=float(cfg.get("setup_arm_threshold", 0.62)),
            trigger_followthrough_threshold=float(
                cfg.get("trigger_followthrough_threshold", 0.65)
            ),
            trigger_fakeout_max=float(cfg.get("trigger_fakeout_max", 0.35)),
            acceptance_followthrough_threshold=float(
                cfg.get("acceptance_followthrough_threshold", 0.70)
            ),
            acceptance_fakeout_max=float(
                cfg.get("acceptance_fakeout_max", 0.25)
            ),
            watch_ttl_hours=float(cfg.get("watch_ttl_hours", 12)),
            armed_ttl_hours=float(cfg.get("armed_ttl_hours", 4)),
            acceptance_ttl_bars=int(cfg.get("acceptance_ttl_bars", 2)),
            wait_retest_ttl_hours=float(cfg.get("wait_retest_ttl_hours", 4)),
        )
    )
    ai_client = AlphaStrategyAIClient(
        timeout_seconds=float(cfg.get("ai_timeout_ms", 300)) / 1000.0,
    )
    return AlphaStrategyWorker(
        ai_evaluate=ai_client.evaluate,
        machine=machine,
        mode=cfg.get("mode", "shadow"),
        market_env=cfg.get("market_env", "testnet"),
    )


async def process_alpha_strategy(worker):
    try:
        result = await asyncio.to_thread(
            worker.run_once,
            limit=ALPHA_SCORE_LIMIT,
        )
        logger.info(
            "Alpha Strategy V2 processed=%s applied=%s skipped=%s errors=%s",
            result["processed"],
            result["applied"],
            result["skipped"],
            len(result["errors"]),
        )
        if result["errors"]:
            upsert_service_runtime_status(
                "alpha_engine",
                status="error",
                error_code="alpha_strategy_worker_errors",
                last_error=str(result["errors"][0])[:2000],
                details={
                    "processed": result["processed"],
                    "applied": result["applied"],
                    "skipped": result["skipped"],
                    "error_count": len(result["errors"]),
                },
            )
        else:
            upsert_service_runtime_status(
                "alpha_engine",
                status="ok",
                details={
                    "processed": result["processed"],
                    "applied": result["applied"],
                    "skipped": result["skipped"],
                    "strategy_mode": ALPHA_STRATEGY_CFG.get("mode"),
                },
            )
    except Exception as exc:
        logger.error("Alpha Strategy V2 failed: %s", exc, exc_info=True)
        upsert_service_runtime_status(
            "alpha_engine",
            status="error",
            error_code="alpha_strategy_worker_failed",
            last_error=f"{type(exc).__name__}: {exc}",
        )


async def run_once():
    init_db()
    await score_alpha()
    if (
        ALPHA_STRATEGY_CFG.get("enabled", False)
        and ALPHA_STRATEGY_CFG.get("mode", "shadow") != "off"
    ):
        await process_alpha_strategy(build_strategy_worker())


async def main():
    init_db()
    scheduler = AsyncIOScheduler()
    startup_time = datetime.now(tz=timezone.utc)
    scheduler.add_job(
        score_alpha,
        "interval",
        minutes=ALPHA_SCORE_INTERVAL_MIN,
        id="alpha_score",
        replace_existing=True,
        next_run_time=startup_time,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
    )
    if (
        ALPHA_STRATEGY_CFG.get("enabled", False)
        and ALPHA_STRATEGY_CFG.get("mode", "shadow") != "off"
    ):
        strategy_worker = build_strategy_worker()
        scheduler.add_job(
            process_alpha_strategy,
            "interval",
            seconds=int(ALPHA_STRATEGY_CFG.get("worker_interval_seconds", 60)),
            args=[strategy_worker],
            id="alpha_strategy_v2",
            replace_existing=True,
            # Keep the one-minute worker off the same second as the heavier score job.
            next_run_time=startup_time + timedelta(seconds=5),
            max_instances=1,
            coalesce=True,
            misfire_grace_time=30,
        )
    scheduler.start()
    logger.info(
        "Alpha engine scheduler started (strategy_v2=%s mode=%s)",
        bool(ALPHA_STRATEGY_CFG.get("enabled", False)),
        ALPHA_STRATEGY_CFG.get("mode", "shadow"),
    )

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
