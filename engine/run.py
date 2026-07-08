"""AlphaDog Scoring Engine 鈥?main runner (SQLite)"""
import asyncio
import json
import logging
import sys, os
from datetime import datetime, timezone

import pandas as pd
from apscheduler.schedulers.asyncio import AsyncIOScheduler

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from shared.db import (
    fetch_klines_1h, fetch_klines_15m, fetch_klines_6h, fetch_klines_24h, fetch_futures, fetch_onchain,
    fetch_active_symbols, fetch_historical_scores, fetch_price_history,
    insert_scores,
    label_signal_outcomes,
    get_conn, init_db, close_conn
)
from engine.scoring import ScoringEngine
from shared.policy_loop import label_decision_outcomes, generate_and_activate_policies, policy_guard

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("engine")


def rows_to_df(rows, cols):
    if not rows:
        return pd.DataFrame()
    data = [{k: r[k] for k in cols} for r in rows]
    return pd.DataFrame(data)


async def run_scoring():
    engine = ScoringEngine()
    try:
        symbols = fetch_active_symbols()
        if not symbols:
            logger.warning("No symbols")
            return
        logger.info(f"Scoring {len(symbols)} symbols")

        k1h = fetch_klines_1h(symbols)
        k15m = fetch_klines_15m(symbols)
        k6h = fetch_klines_6h(symbols)
        k24h = fetch_klines_24h(symbols)
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
            return

        results = engine.score_all(df_1h, df_15m, df_6h, df_24h, df_fut, df_onc)
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

    except Exception as e:
        logger.error(f"Scoring error: {e}", exc_info=True)


async def run_signal_labeling():
    try:
        count = label_signal_outcomes(max_rows=2000)
        if count:
            logger.info(f"[signal-outcomes] labeled/updated {count} decisions")
        loop_count = label_decision_outcomes(limit=2500)
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


async def main():
    logger.info("AlphaDog Engine starting...")
    init_db()  # 纭繚鎵€鏈夎〃瀛樺湪

    sched = AsyncIOScheduler()
    sched.add_job(run_scoring, "interval", minutes=5, id="scoring",
                  replace_existing=True, next_run_time=datetime.now(tz=timezone.utc))
    sched.add_job(run_signal_labeling, "interval", minutes=5, id="signal_labeling",
                  replace_existing=True, next_run_time=datetime.now(tz=timezone.utc))
    sched.add_job(run_policy_guard, "interval", minutes=15, id="policy_guard",
                  replace_existing=True, next_run_time=datetime.now(tz=timezone.utc).replace(second=20))
    sched.add_job(run_policy_autotune, "interval", hours=1, id="policy_autotune",
                  replace_existing=True, next_run_time=datetime.now(tz=timezone.utc).replace(minute=10, second=0))
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
