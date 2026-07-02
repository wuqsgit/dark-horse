"""DB connection for engine - shares same schema"""
import os
import asyncpg
from tenacity import retry, stop_after_attempt, wait_fixed

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", 5432)),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", "alphadog123"),
    "database": os.getenv("DB_NAME", "alphadog"),
}

_pool = None


@retry(stop=stop_after_attempt(10), wait=wait_fixed(3))
async def get_pool():
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(**DB_CONFIG, min_size=2, max_size=4)
    return _pool


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def fetch_klines_1h(symbols: list, hours=72):
    """拉取最近 N 小时的 1h K线"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT time, symbol, open, high, low, close, volume, quote_vol
               FROM candles_1h
               WHERE symbol = ANY($1::text[]) AND time > NOW() - INTERVAL '72 hours'
               ORDER BY symbol, time""",
            symbols,
        )
    return rows


async def fetch_klines_15m(symbols: list, hours=12):
    """拉取最近 N 小时的 15m K线"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT time, symbol, open, high, low, close, volume, quote_vol
               FROM candles_15m
               WHERE symbol = ANY($1::text[]) AND time > NOW() - INTERVAL '12 hours'
               ORDER BY symbol, time""",
            symbols,
        )
    return rows


async def fetch_futures(symbols: list, hours=72):
    """拉取合约数据"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT time, symbol, open_interest, funding_rate, mark_price
               FROM futures_data
               WHERE symbol = ANY($1::text[]) AND time > NOW() - INTERVAL '72 hours'
               ORDER BY symbol, time""",
            symbols,
        )
    return rows


async def fetch_onchain(symbols: list, hours=72):
    """拉取链上数据"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT time, symbol, chain, cex_net_flow_usd, cex_net_flow_14d_usd,
                      cex_net_outflow_ratio
               FROM onchain_flows
               WHERE time > NOW() - INTERVAL '72 hours'
               ORDER BY time""",
        )
    return rows


async def fetch_active_symbols():
    """获取活跃币种列表"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT symbol FROM symbols WHERE is_active = TRUE")
        return [r["symbol"] for r in rows]


async def save_scores(rows: list):
    """保存评分结果"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(
            """INSERT INTO alpha_scores (time, symbol, composite_score, composite_summary,
               risk_label, chip_phase, trend_state, trend_direction, volatility_level,
               price_position, relative_strength, market_price, raw_features, scan_id)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14)
               ON CONFLICT (time, symbol) DO UPDATE SET
               composite_score=EXCLUDED.composite_score,
               composite_summary=EXCLUDED.composite_summary,
               risk_label=EXCLUDED.risk_label,
               chip_phase=EXCLUDED.chip_phase,
               trend_state=EXCLUDED.trend_state,
               raw_features=EXCLUDED.raw_features""",
            rows,
        )


async def fetch_historical_scores(hours_back=720):
    """拉取历史评分用于回测"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT time, symbol, composite_score, composite_summary, market_price
               FROM alpha_scores
               WHERE time > NOW() - INTERVAL '720 hours'
               ORDER BY symbol, time""",
        )
    return rows


async def fetch_price_history(symbols: list, hours_back=720):
    """拉取历史价格用于回测收益计算"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT DISTINCT ON (symbol, time_bucket)
                  time_bucket, symbol, close
               FROM (
                 SELECT date_trunc('1h', time) as time_bucket, symbol,
                        LAST(close, time) as close
                 FROM candles_1h
                 WHERE symbol = ANY($1::text[])
                   AND time > NOW() - INTERVAL '720 hours'
                 GROUP BY time_bucket, symbol
               ) sub
               ORDER BY symbol, time_bucket""",
            symbols,
        )
    return rows


async def save_backtest_results(rows: list):
    """保存回测结果"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(
            """INSERT INTO backtest_results (symbol, grade, grade_score, grade_time,
               price_at_grade, return_6h, return_12h, return_24h, return_48h,
               max_drawdown, win_12h, win_24h)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)""",
            rows,
        )
