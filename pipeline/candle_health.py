"""Small reliability helpers shared by normal and Alpha candle collectors."""
import asyncio
from datetime import datetime, timezone

from shared.db import (
    fetch_market_universe,
    get_conn,
    update_market_readiness_batch,
)
from shared.market_universe import CandleState, assess_dual_market_readiness


async def retry_async(operation, retries=2, delay=0.2):
    for attempt in range(retries + 1):
        try:
            return await operation()
        except Exception:
            if attempt >= retries:
                raise
            if delay:
                await asyncio.sleep(delay)


def _parse_time(value):
    if not value:
        return None
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _candle_state(conn, market_kind, symbol, source_env="mainnet"):
    legacy = {
        "spot": ("candles_15m", "candles_1h", "symbol", False),
        "futures": (
            "futures_candles_15m",
            "futures_candles_1h",
            "symbol",
            True,
        ),
        "alpha": (
            "alpha_candles_15m",
            "alpha_candles_1h",
            "alpha_symbol",
            True,
        ),
    }[market_kind]

    def interval_state(interval, legacy_table):
        latest = conn.execute(
            """SELECT MAX(time) latest, COUNT(*) count
               FROM aggregated_candles
               WHERE market_kind=? AND source_env=? AND symbol=?
                 AND interval=? AND is_complete=1""",
            (market_kind, source_env, symbol, interval),
        ).fetchone()
        symbol_column = legacy[2]
        env_clause = " AND source_env=?" if legacy[3] else ""
        params = (symbol, source_env) if legacy[3] else (symbol,)
        count = conn.execute(
            f"""SELECT COUNT(*) count FROM {legacy_table}
                WHERE {symbol_column}=?{env_clause}""",
            params,
        ).fetchone()
        return latest["latest"], count["count"]

    latest_15m, count_15m = interval_state("15m", legacy[0])
    latest_1h, count_1h = interval_state("1h", legacy[1])
    return CandleState(
        _parse_time(latest_15m),
        _parse_time(latest_1h),
        int(count_15m or 0),
        int(count_1h or 0),
    )


def refresh_universe_readiness(pool_type, now=None, futures_source_env=None):
    now = now or datetime.now(timezone.utc)
    rows = fetch_market_universe(pool_type)
    conn = get_conn()
    results = {}
    try:
        for row in rows:
            if pool_type == "alpha":
                spot = _candle_state(conn, "alpha", row["source_symbol"])
            else:
                spot = _candle_state(conn, "spot", row["spot_symbol"])
            futures = _candle_state(
                conn,
                "futures",
                row["futures_symbol"],
                futures_source_env or "mainnet",
            )
            result = assess_dual_market_readiness(now, spot, futures)
            results[row["source_symbol"]] = result
    finally:
        conn.close()
    update_market_readiness_batch(pool_type, results)
    return results
