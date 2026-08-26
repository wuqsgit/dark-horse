"""AlphaDog Scoring Engine 鈥?main runner (SQLite)"""
import asyncio
import json
import logging
import sys, os
from datetime import datetime, timedelta, timezone

import pandas as pd
from apscheduler.schedulers.asyncio import AsyncIOScheduler

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared.db import (
    fetch_klines_1h, fetch_klines_15m, fetch_klines_6h, fetch_klines_24h, fetch_futures, fetch_onchain,
    fetch_active_symbols, fetch_historical_scores, fetch_price_history, fetch_spot_klines_1h,
    insert_scores,
    label_signal_outcomes,
    get_conn, init_db, close_conn, cleanup_old_operational_data, RETENTION_DAYS,
    upsert_service_runtime_status,
)
from engine.scoring import ScoringEngine
from shared.policy_loop import label_decision_outcomes, generate_and_activate_policies, policy_guard

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("engine")

EMPTY_SYMBOL_DEGRADE_AFTER = 2
_consecutive_empty_symbol_runs = 0


def _register_symbol_count(count: int) -> int:
    """Return consecutive empty scans, resetting after any healthy universe."""
    global _consecutive_empty_symbol_runs
    if int(count) > 0:
        _consecutive_empty_symbol_runs = 0
    else:
        _consecutive_empty_symbol_runs += 1
    return _consecutive_empty_symbol_runs


def next_hourly_run(now, minute=10):
    """Return the next UTC hourly slot without ever scheduling in the past."""
    candidate = now.replace(minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(hours=1)
    return candidate


def rows_to_df(rows, cols):
    if not rows:
        return pd.DataFrame()
    data = [{k: r[k] for k in cols} for r in rows]
    return pd.DataFrame(data)


def _volume_growth(rows):
    ordered = sorted(rows, key=lambda row: row["time"])
    current = sum(float(row["quote_vol"] or 0) for row in ordered[-6:])
    previous = sum(float(row["quote_vol"] or 0) for row in ordered[-12:-6])
    return current / previous if previous > 0 else 1.0


async def run_scoring():
    engine = ScoringEngine()
    try:
        symbols = fetch_active_symbols()
        empty_runs = _register_symbol_count(len(symbols))
        if not symbols:
            if empty_runs < EMPTY_SYMBOL_DEGRADE_AFTER:
                logger.warning(
                    "No symbols (transient %s/%s); preserving last healthy status",
                    empty_runs,
                    EMPTY_SYMBOL_DEGRADE_AFTER,
                )
                return
            logger.warning("No symbols for %s consecutive scoring runs", empty_runs)
            upsert_service_runtime_status(
                "engine",
                status="degraded",
                error_code="normal_symbols_empty",
                last_error="普通策略没有可评分币种。",
                details={"consecutive_empty_runs": empty_runs},
            )
            return
        logger.info(f"Scoring {len(symbols)} symbols")

        k1h = fetch_klines_1h(symbols)
        k15m = fetch_klines_15m(symbols)
        k6h = fetch_klines_6h(symbols)
        k24h = fetch_klines_24h(symbols)
        spot_1h = fetch_spot_klines_1h(symbols, hours=72)
        fut = fetch_futures(symbols)
        onc = fetch_onchain(symbols)

        df_1h = rows_to_df(k1h, ["time","symbol","open","high","low","close","volume","quote_vol"])
        df_15m = rows_to_df(k15m, ["time","symbol","open","high","low","close","volume","quote_vol"])
        df_6h = rows_to_df(k6h, ["time","symbol","open","high","low","close","volume","quote_vol"])
        df_24h = rows_to_df(k24h, ["time","symbol","open","high","low","close","volume","quote_vol"])
        df_fut = rows_to_df(fut, ["time","symbol","open_interest","funding_rate","mark_price"])
        df_onc = rows_to_df(onc, ["time","symbol","chain","cex_net_flow_usd","cex_net_flow_14d_usd","cex_net_outflow_ratio"])

        logger.info(f"Data: 1h={len(df_1h)} 15m={len(df_15m)} 6h={len(df_6h)} 24h={len(df_24h)} fut={len(df_fut)} onc={len(df_onc)}")

        if df_1h.empty:
            logger.warning("No data yet")
            upsert_service_runtime_status(
                "engine",
                status="degraded",
                error_code="normal_candles_empty",
                last_error="普通策略缺少 1 小时 K 线。",
            )
            return

        results = engine.score_all(df_1h, df_15m, df_6h, df_24h, df_fut, df_onc)
        spot_by_symbol = {}
        futures_by_symbol = {}
        for row in spot_1h:
            spot_by_symbol.setdefault(row["symbol"], []).append(row)
        for row in k1h:
            futures_by_symbol.setdefault(row["symbol"], []).append(row)
        for result in results:
            spot_ratio = _volume_growth(spot_by_symbol.get(result["symbol"], []))
            futures_ratio = _volume_growth(futures_by_symbol.get(result["symbol"], []))
            result["raw_features"]["dual_market_volume"] = {
                "spot_volume_ratio_6h": round(spot_ratio, 4),
                "futures_volume_ratio_6h": round(futures_ratio, 4),
                "volume_sync_score": round(min(spot_ratio, futures_ratio), 4),
                "synchronized": spot_ratio >= 1.3 and futures_ratio >= 1.3,
            }
        logger.info(f"Scored {len(results)}")

        if results:
            import json
            db_rows = [
                (
                    r["time"].strftime("%Y-%m-%dT%H:%M:%SZ"),
                    r["symbol"], r["composite_score"], r["composite_summary"],
                    r["risk_label"], r["chip_phase"], r["trend_state"],
                    r["trend_direction"], r["volatility_level"],
                    r["price_position"], r["relative_strength"],
                    r["market_price"], json.dumps(r["raw_features"], ensure_ascii=False),
                    r["scan_id"],
                    r.get("entry_alpha", 0),  # V3.0
                    r.get("hold_alpha", 0),    # V3.0
                )
                for r in results
            ]
            insert_scores(db_rows)

            top = sorted(results, key=lambda x: -x["composite_score"])[:5]
            for t in top:
                logger.info(f"  #{t['composite_score']:.1f} {t['symbol']} ({t['composite_summary']}) - {t['chip_phase']}")
        upsert_service_runtime_status(
            "engine",
            status="ok",
            details={"symbol_count": len(symbols), "scored_count": len(results)},
        )

    except Exception as e:
        logger.error(f"Scoring error: {e}", exc_info=True)
        upsert_service_runtime_status(
            "engine",
            status="error",
            error_code="normal_scoring_failed",
            last_error=f"{type(e).__name__}: {e}",
        )


async def run_signal_labeling():
    try:
        count = label_signal_outcomes(max_rows=500)
        if count:
            logger.info(f"[signal-outcomes] labeled/updated {count} decisions")
        loop_count = label_decision_outcomes(limit=500)
        if loop_count:
            logger.info(f"[policy-loop] labeled/updated {loop_count} decision outcomes")
    except Exception as e:
        logger.warning(f"[signal-outcomes] failed: {e}")


async def run_policy_autotune():
    try:
        result = generate_and_activate_policies()
        logger.info(
            "[policy-loop] review=%s created=%s activated=%s",
            result.get("review"),
            result.get("created"),
            len(result.get("activated") or []),
        )
    except Exception as e:
        logger.warning(f"[policy-loop] autotune failed: {e}", exc_info=True)


async def run_policy_guard():
    try:
        result = policy_guard()
        if result.get("rolled_back"):
            logger.warning(f"[policy-loop] rolled back {result.get('rolled_back')} policies")
    except Exception as e:
        logger.warning(f"[policy-loop] guard failed: {e}", exc_info=True)


async def run_data_retention():
    try:
        deleted = await asyncio.to_thread(cleanup_old_operational_data, RETENTION_DAYS)
        total = sum(deleted.values())
        logger.info("[retention] kept=%s days deleted=%s tables=%s", RETENTION_DAYS, total, deleted)
    except Exception as e:
        logger.error("[retention] cleanup failed: %s", e, exc_info=True)


def register_retention_job(scheduler):
    scheduler.add_job(
        run_data_retention,
        trigger="date",
        # Let candle bootstrap and the first scoring pass finish before a
        # multi-table delete acquires SQLite's single writer lock.
        run_date=datetime.now(tz=timezone.utc) + timedelta(minutes=20),
        id="startup_data_retention",
        replace_existing=True,
    )
    scheduler.add_job(
        run_data_retention,
        trigger="cron",
        hour=3,
        minute=30,
        timezone="Asia/Shanghai",
        id="daily_data_retention",
        replace_existing=True,
    )


def register_startup_scoring_retry(scheduler, startup_time):
    """Retry after collectors have published their startup universe."""
    scheduler.add_job(
        run_scoring,
        trigger="date",
        run_date=startup_time + timedelta(seconds=45),
        id="startup_scoring_retry",
        replace_existing=True,
        misfire_grace_time=60,
    )


async def main():
    logger.info("AlphaDog Engine starting...")
    init_db()

    sched = AsyncIOScheduler()
    startup_time = datetime.now(tz=timezone.utc)
    common_job_options = {
        "replace_existing": True,
        "max_instances": 1,
        "coalesce": True,
        "misfire_grace_time": 120,
    }
    sched.add_job(
        run_scoring,
        "interval",
        minutes=5,
        id="scoring",
        next_run_time=startup_time,
        **common_job_options,
    )
    sched.add_job(
        run_signal_labeling,
        "interval",
        minutes=30,
        id="signal_labeling",
        next_run_time=startup_time + timedelta(seconds=15),
        **common_job_options,
    )
    sched.add_job(
        run_policy_guard,
        "interval",
        minutes=15,
        id="policy_guard",
        next_run_time=startup_time + timedelta(seconds=30),
        **common_job_options,
    )
    sched.add_job(
        run_policy_autotune,
        "interval",
        hours=1,
        id="policy_autotune",
        next_run_time=next_hourly_run(startup_time),
        **common_job_options,
    )
    register_retention_job(sched)
    register_startup_scoring_retry(sched, startup_time)
    logger.info("Legacy backtest scheduler removed; policy loop is the only review/autotune path")
    sched.start()
    logger.info("Engine scheduler started")

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass

    sched.shutdown()
    logger.info("Engine stopped")


if __name__ == "__main__":
    asyncio.run(main())
