"""TimescaleDB async connection manager"""
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
        _pool = await asyncpg.create_pool(**DB_CONFIG, min_size=2, max_size=8)
    return _pool


async def close_pool():
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def insert_candles_1h(rows: list):
    """批量插入1h K线"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(
            """INSERT INTO candles_1h (time, symbol, open, high, low, close, volume, quote_vol, trades)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
               ON CONFLICT (time, symbol) DO UPDATE SET
               open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
               close=EXCLUDED.close, volume=EXCLUDED.volume, quote_vol=EXCLUDED.quote_vol,
               trades=EXCLUDED.trades""",
            rows,
        )


async def insert_candles_15m(rows: list):
    """批量插入15m K线"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(
            """INSERT INTO candles_15m (time, symbol, open, high, low, close, volume, quote_vol, trades)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
               ON CONFLICT (time, symbol) DO UPDATE SET
               open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
               close=EXCLUDED.close, volume=EXCLUDED.volume, quote_vol=EXCLUDED.quote_vol,
               trades=EXCLUDED.trades""",
            rows,
        )


async def insert_futures(rows: list):
    """批量插入合约数据"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(
            """INSERT INTO futures_data (time, symbol, open_interest, funding_rate, mark_price)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (time, symbol) DO UPDATE SET
               open_interest=EXCLUDED.open_interest, funding_rate=EXCLUDED.funding_rate,
               mark_price=EXCLUDED.mark_price""",
            rows,
        )


async def insert_onchain(rows: list):
    """批量插入链上数据"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(
            """INSERT INTO onchain_flows (time, symbol, chain, cex_inflow_usd, cex_outflow_usd,
               cex_net_flow_usd, cex_net_flow_14d_usd, cex_net_outflow_ratio, window_hours)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
               ON CONFLICT (time, symbol) DO UPDATE SET
               cex_inflow_usd=EXCLUDED.cex_inflow_usd,
               cex_outflow_usd=EXCLUDED.cex_outflow_usd,
               cex_net_flow_usd=EXCLUDED.cex_net_flow_usd,
               cex_net_flow_14d_usd=EXCLUDED.cex_net_flow_14d_usd,
               cex_net_outflow_ratio=EXCLUDED.cex_net_outflow_ratio""",
            rows,
        )


async def insert_scores(rows: list):
    """批量插入评分结果"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(
            """INSERT INTO alpha_scores (time, symbol, composite_score, composite_summary,
               risk_label, chip_phase, trend_state, trend_direction, volatility_level,
               price_position, relative_strength, market_price, raw_features, scan_id)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
               ON CONFLICT (time, symbol) DO UPDATE SET
               composite_score=EXCLUDED.composite_score,
               composite_summary=EXCLUDED.composite_summary,
               risk_label=EXCLUDED.risk_label,
               chip_phase=EXCLUDED.chip_phase,
               trend_state=EXCLUDED.trend_state,
               raw_features=EXCLUDED.raw_features""",
            rows,
        )


async def insert_backtest(rows: list):
    """批量插入回测结果"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(
            """INSERT INTO backtest_results (symbol, grade, grade_score, grade_time,
               price_at_grade, return_6h, return_12h, return_24h, return_48h,
               max_drawdown, win_12h, win_24h)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)""",
            rows,
        )


async def get_symbols():
    """获取活跃交易对列表"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT symbol FROM symbols WHERE is_active = TRUE")
        return [r["symbol"] for r in rows]


async def upsert_symbol(symbol: str):
    """更新交易对"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO symbols (symbol) VALUES ($1)
               ON CONFLICT (symbol) DO UPDATE SET last_seen = NOW()""",
            symbol,
        )
