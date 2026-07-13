"""Small reliability helpers shared by normal and Alpha candle collectors."""
import asyncio
from datetime import datetime, timezone

from shared.db import get_conn, fetch_market_universe, update_market_readiness
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


def _candle_state(conn, table_15m, table_1h, symbol, symbol_column="symbol"):
    row_15m = conn.execute(
        f"SELECT MAX(time) latest, COUNT(*) count FROM {table_15m} WHERE {symbol_column} = ?",
        (symbol,),
    ).fetchone()
    row_1h = conn.execute(
        f"SELECT MAX(time) latest, COUNT(*) count FROM {table_1h} WHERE {symbol_column} = ?",
        (symbol,),
    ).fetchone()
    return CandleState(
        _parse_time(row_15m["latest"]),
        _parse_time(row_1h["latest"]),
        int(row_15m["count"] or 0),
        int(row_1h["count"] or 0),
    )


def refresh_universe_readiness(pool_type, now=None):
    now = now or datetime.now(timezone.utc)
    rows = fetch_market_universe(pool_type)
    conn = get_conn()
    results = {}
    try:
        for row in rows:
            if pool_type == "alpha":
                spot = _candle_state(
                    conn, "alpha_candles_15m", "alpha_candles_1h",
                    row["source_symbol"], "alpha_symbol",
                )
            else:
                spot = _candle_state(
                    conn, "candles_15m", "candles_1h", row["spot_symbol"],
                )
            futures = _candle_state(
                conn, "futures_candles_15m", "futures_candles_1h", row["futures_symbol"],
            )
            result = assess_dual_market_readiness(now, spot, futures)
            results[row["source_symbol"]] = result
    finally:
        conn.close()
    for source_symbol, result in results.items():
        update_market_readiness(pool_type, source_symbol, result.ready, result.error)
    return results
