#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from alpha_engine.strategy.market_data import futures_rest_base
from alpha_pipeline.collector import AlphaCollector
from shared.db import (
    get_conn,
    init_db,
    insert_alpha_candles,
    insert_futures,
    insert_futures_candles,
)


INTERVAL_MS = {"15m": 15 * 60_000, "1h": 60 * 60_000}
TABLES = {
    "15m": ("alpha_candles_15m", "futures_candles_15m"),
    "1h": ("alpha_candles_1h", "futures_candles_1h"),
}


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _milliseconds(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _resolve_alpha_symbol(futures_symbol: str, alpha_symbol: str | None):
    if alpha_symbol:
        return alpha_symbol.upper()
    conn = get_conn()
    try:
        row = conn.execute(
            """SELECT alpha_symbol FROM alpha_symbols
               WHERE futures_symbol=?
               ORDER BY datetime(last_seen) DESC LIMIT 1""",
            (futures_symbol.upper(),),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise ValueError(
            f"no Alpha mapping for {futures_symbol}; pass --alpha-symbol"
        )
    return str(row["alpha_symbol"]).upper()


async def _paged_klines(
    client: httpx.AsyncClient,
    *,
    url: str,
    params: dict,
    start_ms: int,
    end_ms: int,
    interval: str,
    limit: int,
    alpha_payload: bool,
) -> list:
    cursor = int(start_ms)
    rows = []
    step = INTERVAL_MS[interval]
    while cursor < end_ms:
        response = await client.get(
            url,
            params={
                **params,
                "interval": interval,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": limit,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if alpha_payload:
            if payload.get("success") is False:
                raise RuntimeError(f"Alpha API error: {payload}")
            page = payload.get("data") or []
        else:
            page = payload
        if not page:
            break
        page = sorted(page, key=lambda row: int(row[0]))
        rows.extend(row for row in page if int(row[0]) < end_ms)
        next_cursor = int(page[-1][0]) + step
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        await asyncio.sleep(0.08)
    unique = {int(row[0]): row for row in rows}
    return [unique[key] for key in sorted(unique)]


async def _open_interest_history(
    client: httpx.AsyncClient,
    base: str,
    symbol: str,
    start_ms: int,
    end_ms: int,
) -> list[dict]:
    result = []
    chunk_ms = int(timedelta(days=29).total_seconds() * 1000)
    chunk_start = start_ms
    while chunk_start < end_ms:
        chunk_end = min(end_ms, chunk_start + chunk_ms)
        cursor = chunk_start
        while cursor < chunk_end:
            response = await client.get(
                base + "/futures/data/openInterestHist",
                params={
                    "symbol": symbol,
                    "period": "5m",
                    "startTime": cursor,
                    "endTime": chunk_end,
                    "limit": 500,
                },
            )
            response.raise_for_status()
            page = response.json()
            if not page:
                break
            result.extend(page)
            next_cursor = int(page[-1]["timestamp"]) + 1
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            await asyncio.sleep(0.08)
        chunk_start = chunk_end + 1
    unique = {int(row["timestamp"]): row for row in result}
    return [unique[key] for key in sorted(unique)]


async def _funding_history(
    client: httpx.AsyncClient,
    base: str,
    symbol: str,
    start_ms: int,
    end_ms: int,
) -> list[dict]:
    cursor = start_ms
    result = []
    while cursor < end_ms:
        response = await client.get(
            base + "/fapi/v1/fundingRate",
            params={
                "symbol": symbol,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": 1000,
            },
        )
        response.raise_for_status()
        page = response.json()
        if not page:
            break
        result.extend(page)
        next_cursor = int(page[-1]["fundingTime"]) + 1
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        await asyncio.sleep(0.08)
    return sorted(result, key=lambda row: int(row["fundingTime"]))


async def backfill(args) -> dict:
    futures_symbol = args.symbol.upper()
    alpha_symbol = _resolve_alpha_symbol(
        futures_symbol,
        args.alpha_symbol,
    )
    end_at = _parse(args.end) if args.end else datetime.now(timezone.utc)
    start_at = (
        _parse(args.start)
        if args.start
        else end_at - timedelta(days=int(args.days))
    )
    start_ms = _milliseconds(start_at)
    end_ms = _milliseconds(end_at)
    futures_base = futures_rest_base(args.market_env)
    collector = AlphaCollector(market_env=args.market_env)
    counts = {}
    try:
        for interval in ("15m", "1h"):
            alpha_rows, futures_rows = await asyncio.gather(
                _paged_klines(
                    collector.client,
                    url=(
                        "https://www.binance.com"
                        "/bapi/defi/v1/public/alpha-trade/klines"
                    ),
                    params={"symbol": alpha_symbol},
                    start_ms=start_ms,
                    end_ms=end_ms,
                    interval=interval,
                    limit=1000,
                    alpha_payload=True,
                ),
                _paged_klines(
                    collector.client,
                    url=futures_base + "/fapi/v1/klines",
                    params={"symbol": futures_symbol},
                    start_ms=start_ms,
                    end_ms=end_ms,
                    interval=interval,
                    limit=1500,
                    alpha_payload=False,
                ),
            )
            alpha_table, futures_table = TABLES[interval]
            alpha_normalized = [
                collector.normalize_kline_row(
                    alpha_symbol,
                    row,
                    market_env="mainnet",
                    now_ms=int(time.time() * 1000),
                )
                for row in alpha_rows
            ]
            futures_normalized = [
                collector.normalize_kline_row(
                    futures_symbol,
                    row,
                    market_env=args.market_env,
                    now_ms=int(time.time() * 1000),
                )
                for row in futures_rows
            ]
            insert_alpha_candles(alpha_table, alpha_normalized)
            insert_futures_candles(futures_table, futures_normalized)
            counts[alpha_table] = len(alpha_normalized)
            counts[futures_table] = len(futures_normalized)

        if args.market_env == "mainnet":
            oi_rows, funding_rows = await asyncio.gather(
                _open_interest_history(
                    collector.client,
                    futures_base,
                    futures_symbol,
                    start_ms,
                    end_ms,
                ),
                _funding_history(
                    collector.client,
                    futures_base,
                    futures_symbol,
                    start_ms,
                    end_ms,
                ),
            )
            funding_index = 0
            latest_funding = None
            derivative_rows = []
            for oi in oi_rows:
                timestamp = int(oi["timestamp"])
                while (
                    funding_index < len(funding_rows)
                    and int(funding_rows[funding_index]["fundingTime"])
                    <= timestamp
                ):
                    latest_funding = funding_rows[funding_index]
                    funding_index += 1
                derivative_rows.append(
                    (
                        datetime.fromtimestamp(
                            timestamp / 1000,
                            tz=timezone.utc,
                        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        futures_symbol,
                        float(oi.get("sumOpenInterest") or 0),
                        float(
                            (latest_funding or {}).get("fundingRate") or 0
                        ),
                        float((latest_funding or {}).get("markPrice") or 0),
                        "mainnet",
                    )
                )
            insert_futures(derivative_rows)
            counts["futures_data"] = len(derivative_rows)
        return {
            "alpha_symbol": alpha_symbol,
            "futures_symbol": futures_symbol,
            "market_env": args.market_env,
            "start": start_at.isoformat(),
            "end": end_at.isoformat(),
            "counts": counts,
        }
    finally:
        await collector.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Backfill Alpha Strategy V2 candles and derivatives.",
    )
    parser.add_argument("symbol", help="Futures symbol, e.g. AKEUSDT")
    parser.add_argument("--alpha-symbol")
    parser.add_argument(
        "--market-env",
        choices=("mainnet", "testnet"),
        default="mainnet",
    )
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--start")
    parser.add_argument("--end")
    args = parser.parse_args()
    if args.days < 1:
        parser.error("--days must be at least 1")
    if args.start and not args.end:
        args.end = datetime.now(timezone.utc).isoformat()
    init_db()
    result = asyncio.run(backfill(args))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
