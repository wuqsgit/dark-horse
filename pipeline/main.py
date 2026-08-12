"""AlphaDog Data Pipeline — main entry (HTTP collector)"""
import asyncio
import logging
import os
import signal
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline.binance_http import BinanceHTTPCollector
from pipeline.dune_collector import DuneCollector
from shared.db import init_db, insert_symbol_snapshot, upsert_service_runtime_status

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("pipeline")
logging.getLogger("httpx").setLevel(logging.WARNING)

TOP_SYMBOLS = int(os.getenv("TOP_SYMBOLS", "150"))


async def collect_binance(bc):
    logger.info("=== Binance ===")
    try:
        universe = await bc.get_normal_universe(limit=TOP_SYMBOLS)
        if not universe:
            upsert_service_runtime_status(
                "pipeline",
                status="degraded",
                error_code="normal_universe_empty",
                last_error="普通行情币种池为空，无法生成扫描数据。",
                details={"endpoint": bc.futures_base_url},
            )
            return
        symbols = [row["source_symbol"] for row in universe]
        if universe:
            await bc.collect_all(universe)
            # 🆕 V4.0: 采集深度数据（只采前20个高成交量币种）
            try:
                await bc.collect_depth(symbols, top_n=20)
            except Exception as depth_e:
                logger.warning(f"[depth] collect_depth failed: {depth_e}")
            # 🆕 写入 symbol_snapshots（每日快照，用于回测幸存者偏差修复）
            try:
                import httpx
                resp = await httpx.AsyncClient(timeout=10).get(
                    f"{bc.futures_base_url}/fapi/v1/ticker/24hr"
                )
                if resp.status_code == 200:
                    data = resp.json()
                    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    snap_rows = []
                    for t in data:
                        sym = t.get("symbol", "")
                        if not sym.endswith("USDT"):
                            continue
                        qv = float(t.get("quoteVolume", 0) or 0)
                        pc = float(t.get("priceChangePercent", 0) or 0)
                        active = 1 if qv > 100_000 else 0
                        snap_rows.append((today, sym, "TRADING", qv, pc, active))
                    if snap_rows:
                        insert_symbol_snapshot(snap_rows)
                        logger.info(f"[symbol-snapshot] Wrote {len(snap_rows)} records for {today}")
            except Exception as snap_e:
                logger.warning(f"[symbol-snapshot] Failed: {snap_e}")
        upsert_service_runtime_status(
            "pipeline",
            status="ok",
            details={"universe_count": len(universe), "endpoint": bc.futures_base_url},
        )
    except Exception as e:
        logger.error(f"Binance: {e}", exc_info=True)
        upsert_service_runtime_status(
            "pipeline",
            status="error",
            error_code="normal_market_collection_failed",
            last_error=f"{type(e).__name__}: {e}",
        )
    logger.info("=== Binance done ===")


async def collect_dune(dc):
    logger.info("=== Dune ===")
    try:
        await dc.collect_flows()
    except Exception as e:
        logger.error(f"Dune: {e}")
    logger.info("=== Dune done ===")


async def main():
    logger.info("Pipeline starting...")
    from shared.db import init_db
    init_db()
    upsert_service_runtime_status(
        "pipeline",
        status="starting",
        details={"message": "普通行情采集服务已启动，正在执行首轮采集。"},
    )
    bc = BinanceHTTPCollector()
    dc = DuneCollector()

    sched = AsyncIOScheduler()
    sched.add_job(collect_binance, "interval", minutes=10, args=[bc],
                  id="bc", replace_existing=True,
                  next_run_time=datetime.now(tz=timezone.utc))
    sched.add_job(collect_dune, "interval", minutes=30, args=[dc],
                  id="dc", replace_existing=True,
                  next_run_time=datetime.now(tz=timezone.utc))
    sched.start()
    logger.info("Scheduler started")

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

    sched.shutdown()
    await bc.close()
    await dc.close()
    logger.info("Pipeline stopped")


if __name__ == "__main__":
    asyncio.run(main())
