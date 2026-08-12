from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from datetime import timedelta

from minute_pipeline.aggregation import (
    INTERVAL_MINUTES,
    affected_buckets,
    aggregate_minutes,
    format_utc,
    parse_utc,
)
from shared.db import (
    fetch_latest_minute_time,
    get_conn,
    materialize_aggregated_candles,
    upsert_aggregated_candles,
    upsert_candle_gap,
    upsert_minute_candles,
)

logger = logging.getLogger("minute_pipeline.writer")

LEGACY_TABLES = {
    ("spot", "15m"): ("candles_15m", "symbol", False),
    ("spot", "1h"): ("candles_1h", "symbol", False),
    ("spot", "6h"): ("candles_6h", "symbol", False),
    ("spot", "1d"): ("candles_24h", "symbol", False),
    ("futures", "15m"): ("futures_candles_15m", "symbol", True),
    ("futures", "1h"): ("futures_candles_1h", "symbol", True),
    ("futures", "6h"): ("futures_candles_6h", "symbol", True),
    ("futures", "1d"): ("futures_candles_24h", "symbol", True),
    ("alpha", "15m"): ("alpha_candles_15m", "alpha_symbol", True),
    ("alpha", "1h"): ("alpha_candles_1h", "alpha_symbol", True),
    ("alpha", "6h"): ("alpha_candles_6h", "alpha_symbol", True),
    ("alpha", "1d"): ("alpha_candles_24h", "alpha_symbol", True),
}

MINUTE_TABLES = {
    "spot": ("candles_1m", "symbol"),
    "futures": ("futures_candles_1m", "symbol"),
    "alpha": ("alpha_candles_1m", "alpha_symbol"),
}


def _relative_error(left, right) -> float:
    left_value = float(left or 0)
    right_value = float(right or 0)
    scale = max(abs(left_value), abs(right_value), 1e-12)
    return abs(left_value - right_value) / scale


def compare_with_legacy(
    aggregate: dict,
    *,
    connection=None,
) -> tuple[str, dict]:
    mapping = LEGACY_TABLES.get(
        (aggregate["market_kind"], aggregate["interval"])
    )
    if not mapping:
        return "unavailable", {}
    table, symbol_column, scoped_env = mapping
    clauses = [f"{symbol_column}=?", "time=?"]
    params = [aggregate["symbol"], aggregate["time"]]
    if scoped_env:
        clauses.append("source_env=?")
        params.append(aggregate["source_env"])
    conn = connection or get_conn()
    owns_connection = connection is None
    try:
        row = conn.execute(
            f"""SELECT open, high, low, close, volume, quote_vol, trades
                FROM {table}
                WHERE {' AND '.join(clauses)}
                LIMIT 1""",
            params,
        ).fetchone()
    finally:
        if owns_connection:
            conn.close()
    if not row:
        return "missing_reference", {}
    details = {}
    for field in ("open", "high", "low", "close", "volume", "quote_vol"):
        error = _relative_error(aggregate[field], row[field])
        if error > 1e-8:
            details[field] = {
                "aggregate": aggregate[field],
                "reference": row[field],
                "relative_error": error,
            }
    if int(aggregate["trades"]) != int(row["trades"] or 0):
        details["trades"] = {
            "aggregate": aggregate["trades"],
            "reference": row["trades"],
        }
    return ("mismatch", details) if details else ("matched", {})


class CandleBatchWriter:
    def __init__(self, *, batch_size=1000, flush_seconds=0.25):
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=20000)
        self.batch_size = max(1, int(batch_size))
        self.flush_seconds = max(0.05, float(flush_seconds))
        self._latest_seen: dict[tuple[str, str, str], str | None] = {}
        self.written_count = 0
        self.aggregate_count = 0
        self.mismatch_count = 0

    async def put(self, market_kind: str, row: dict):
        await self.queue.put((market_kind, dict(row)))

    async def run(self, stop: asyncio.Event):
        while not stop.is_set() or not self.queue.empty():
            batch = []
            try:
                batch.append(
                    await asyncio.wait_for(
                        self.queue.get(),
                        timeout=self.flush_seconds,
                    )
                )
            except asyncio.TimeoutError:
                continue
            while len(batch) < self.batch_size:
                try:
                    batch.append(self.queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                await self._write_batch(batch)
            finally:
                for _ in batch:
                    self.queue.task_done()

    async def _write_batch(self, batch):
        grouped = defaultdict(list)
        for market_kind, row in batch:
            grouped[market_kind].append(row)
        for market_kind, rows in grouped.items():
            rows.sort(key=lambda item: (item["symbol"], item["time"]))
            self._detect_gaps(market_kind, rows)
            await asyncio.to_thread(
                upsert_minute_candles,
                market_kind,
                rows,
            )
            self.written_count += len(rows)
        aggregates = await asyncio.to_thread(self._build_aggregates, batch)
        if aggregates:
            await asyncio.to_thread(upsert_aggregated_candles, aggregates)
            await asyncio.to_thread(
                materialize_aggregated_candles,
                aggregates,
            )
            self.aggregate_count += len(aggregates)

    def _detect_gaps(self, market_kind, rows):
        for row in rows:
            env = str(row.get("source_env") or "mainnet").lower()
            symbol = str(row["symbol"])
            key = (market_kind, env, symbol)
            previous = self._latest_seen.get(key)
            if key not in self._latest_seen:
                previous = fetch_latest_minute_time(
                    market_kind,
                    symbol,
                    env,
                )
            current_time = parse_utc(row["time"])
            if previous:
                expected = parse_utc(previous) + timedelta(minutes=1)
                if current_time > expected:
                    upsert_candle_gap(
                        market_kind,
                        symbol,
                        format_utc(expected),
                        format_utc(current_time - timedelta(minutes=1)),
                        source_env=env,
                    )
            if previous is None or current_time > parse_utc(previous):
                self._latest_seen[key] = format_utc(current_time)

    def _build_aggregates(self, batch):
        targets = set()
        for market_kind, row in batch:
            for interval, start in affected_buckets(row["time"]):
                targets.add(
                    (
                        market_kind,
                        str(row.get("source_env") or "mainnet").lower(),
                        str(row["symbol"]),
                        interval,
                        format_utc(start),
                    )
                )
        aggregates = []
        conn = get_conn()
        try:
            for (
                market_kind,
                env,
                symbol,
                interval,
                start_text,
            ) in sorted(targets):
                start = parse_utc(start_text)
                end = start + timedelta(
                    minutes=INTERVAL_MINUTES[interval] - 1
                )
                table, symbol_column = MINUTE_TABLES[market_kind]
                rows = conn.execute(
                    f"""SELECT time, {symbol_column} AS symbol, open, high,
                               low, close, volume, quote_vol, trades,
                               taker_buy_quote_vol, source_env, is_closed,
                               source
                        FROM {table}
                        WHERE {symbol_column}=? AND source_env=?
                          AND time>=? AND time<=? AND is_closed=1
                        ORDER BY time""",
                    (
                        symbol,
                        env,
                        format_utc(start),
                        format_utc(end),
                    ),
                ).fetchall()
                aggregate = aggregate_minutes(
                    rows,
                    market_kind=market_kind,
                    source_env=env,
                    symbol=symbol,
                    interval=interval,
                    start_time=start,
                )
                if not aggregate:
                    continue
                status, details = compare_with_legacy(
                    aggregate,
                    connection=conn,
                )
                aggregate["comparison_status"] = status
                aggregate["comparison_details"] = details
                aggregates.append(aggregate)
                if status == "mismatch":
                    self.mismatch_count += 1
                    if (
                        self.mismatch_count <= 10
                        or self.mismatch_count % 100 == 0
                    ):
                        logger.warning(
                            "aggregate mismatch #%d %s/%s %s %s: %s",
                            self.mismatch_count,
                            market_kind,
                            symbol,
                            interval,
                            start_text,
                            json.dumps(details, ensure_ascii=False),
                        )
        finally:
            conn.close()
        return aggregates
