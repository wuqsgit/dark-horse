from __future__ import annotations

import asyncio
import logging
import signal
from datetime import datetime, timedelta, timezone

from minute_pipeline.collectors import (
    BinanceKlineStream,
    RestKlineClient,
)
from minute_pipeline.config import (
    ALPHA_CONCURRENCY,
    ALPHA_OFFSET_SECONDS,
    BOOTSTRAP_MINUTES,
    ENABLED,
    FUTURES_WS_URL,
    FUTURES_FALLBACK_AFTER_SECONDS,
    GAP_REPAIR_INTERVAL_SECONDS,
    MODE,
    REST_CONCURRENCY,
    RETENTION_DAYS,
    SOURCE_ENV,
    SPOT_WS_URL,
    UNIVERSE_REFRESH_SECONDS,
    WS_RECONNECT_SECONDS,
)
from minute_pipeline.aggregation import format_utc, parse_utc
from minute_pipeline.aggregation import INTERVAL_MINUTES, bucket_start
from minute_pipeline.universe import MinuteUniverse, load_minute_universe
from minute_pipeline.writer import CandleBatchWriter
from pipeline.candle_health import refresh_universe_readiness
from shared.db import (
    fetch_candle_gaps,
    fetch_candle_sync_status,
    fetch_latest_minute_time,
    fetch_minute_candles,
    init_db,
    materialize_stored_aggregates,
    materialize_aggregated_candles,
    purge_minute_candle_data,
    reset_stale_candle_repairs,
    update_candle_gap,
    upsert_aggregated_candles,
    upsert_minute_candles,
    upsert_candle_sync_runtime,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("minute_pipeline")
logging.getLogger("httpx").setLevel(logging.WARNING)


class MinutePipeline:
    def __init__(self):
        self.stop = asyncio.Event()
        self.writer_stop = asyncio.Event()
        self.writer = CandleBatchWriter()
        self.rest = RestKlineClient()
        self.universe = MinuteUniverse(spot=(), futures=(), alpha=())
        self.bootstrap_semaphore = asyncio.Semaphore(REST_CONCURRENCY)
        self.alpha_semaphore = asyncio.Semaphore(ALPHA_CONCURRENCY)
        self.gap_semaphore = asyncio.Semaphore(8)

    def symbols(self, market_kind: str):
        return getattr(self.universe, market_kind)

    async def run(self):
        self.universe = await asyncio.to_thread(load_minute_universe)
        logger.info(
            "minute pipeline mode=%s universe spot=%d futures=%d alpha=%d",
            MODE,
            len(self.universe.spot),
            len(self.universe.futures),
            len(self.universe.alpha),
        )
        tasks = [
            asyncio.create_task(self.writer.run(self.writer_stop)),
            asyncio.create_task(self._bootstrap()),
            asyncio.create_task(self._refresh_universe()),
            asyncio.create_task(self._alpha_poll_loop()),
            asyncio.create_task(self._futures_fallback_loop()),
            asyncio.create_task(self._gap_repair_loop()),
            asyncio.create_task(self._readiness_loop()),
            asyncio.create_task(self._maintenance_loop()),
            asyncio.create_task(
                BinanceKlineStream(
                    collector_id="spot_ws_1m",
                    market_kind="spot",
                    ws_url=SPOT_WS_URL,
                    symbols_provider=lambda: self.symbols("spot"),
                    writer=self.writer,
                    reconnect_seconds=WS_RECONNECT_SECONDS,
                ).run(self.stop)
            ),
            asyncio.create_task(
                BinanceKlineStream(
                    collector_id="futures_ws_1m",
                    market_kind="futures",
                    ws_url=FUTURES_WS_URL,
                    symbols_provider=lambda: self.symbols("futures"),
                    writer=self.writer,
                    reconnect_seconds=WS_RECONNECT_SECONDS,
                ).run(self.stop)
            ),
        ]
        try:
            await self.stop.wait()
        finally:
            for task in tasks[1:]:
                task.cancel()
            await asyncio.gather(*tasks[1:], return_exceptions=True)
            await self.writer.queue.join()
            self.writer_stop.set()
            await tasks[0]
            await self.rest.close()

    async def _refresh_universe(self):
        while not self.stop.is_set():
            await self._sleep(UNIVERSE_REFRESH_SECONDS)
            if self.stop.is_set():
                return
            updated = await asyncio.to_thread(load_minute_universe)
            if updated != self.universe:
                logger.info(
                    "minute universe updated spot=%d futures=%d alpha=%d",
                    len(updated.spot),
                    len(updated.futures),
                    len(updated.alpha),
                )
                self.universe = updated

    async def _bootstrap(self):
        if BOOTSTRAP_MINUTES <= 0:
            return
        end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
        start = end - timedelta(minutes=BOOTSTRAP_MINUTES)
        jobs = [
            self._bootstrap_symbol(kind, symbol, start, end)
            for kind in ("spot", "futures", "alpha")
            for symbol in self.symbols(kind)
        ]
        if jobs:
            await asyncio.gather(*jobs)
            await self.writer.queue.join()
        for market_kind in ("spot", "futures", "alpha"):
            await asyncio.to_thread(
                upsert_candle_sync_runtime,
                f"{market_kind}_bootstrap_1m",
                market_kind,
                status="completed",
                connection_state="rest",
                queue_depth=self.writer.queue.qsize(),
                metrics={
                    "symbols_total": len(self.symbols(market_kind)),
                },
            )
        logger.info("minute bootstrap completed: %d symbol feeds", len(jobs))

    async def _bootstrap_symbol(
        self,
        market_kind,
        symbol,
        start,
        end,
    ):
        semaphore = (
            self.alpha_semaphore
            if market_kind == "alpha"
            else self.bootstrap_semaphore
        )
        collector_id = f"{market_kind}_bootstrap_1m"
        try:
            latest = await asyncio.to_thread(
                fetch_latest_minute_time,
                market_kind,
                symbol,
                SOURCE_ENV,
            )
            fetch_start = start
            if latest:
                fetch_start = max(
                    fetch_start,
                    parse_utc(latest) + timedelta(minutes=1),
                )
            if fetch_start >= end:
                return
            async with semaphore:
                rows = await self.rest.fetch(
                    market_kind,
                    symbol,
                    start_time=fetch_start,
                    end_time=end,
                    limit=BOOTSTRAP_MINUTES + 2,
                )
            for row in rows:
                await self.writer.put(market_kind, row)
            await asyncio.to_thread(
                upsert_candle_sync_runtime,
                collector_id,
                market_kind,
                status="running",
                connection_state="rest",
                last_event_at=format_utc(datetime.now(timezone.utc)),
                last_closed_time=rows[-1]["time"] if rows else None,
                queue_depth=self.writer.queue.qsize(),
                metrics={"symbols_total": len(self.symbols(market_kind))},
            )
        except Exception as exc:
            logger.warning(
                "minute bootstrap failed %s/%s: %s",
                market_kind,
                symbol,
                exc,
            )

    async def _alpha_poll_loop(self):
        while not self.stop.is_set():
            now = datetime.now(timezone.utc)
            next_minute = now.replace(
                second=ALPHA_OFFSET_SECONDS,
                microsecond=0,
            )
            if next_minute <= now:
                next_minute += timedelta(minutes=1)
            await self._sleep((next_minute - now).total_seconds())
            if self.stop.is_set():
                return
            symbols = self.symbols("alpha")
            results = await asyncio.gather(
                *(self._poll_alpha_symbol(symbol) for symbol in symbols),
                return_exceptions=True,
            )
            failures = sum(
                isinstance(result, Exception) for result in results
            )
            latest = max(
                (
                    result
                    for result in results
                    if isinstance(result, str)
                ),
                default=None,
            )
            await asyncio.to_thread(
                upsert_candle_sync_runtime,
                "alpha_rest_1m",
                "alpha",
                status="degraded" if failures else "running",
                connection_state="polling",
                last_event_at=format_utc(datetime.now(timezone.utc)),
                last_closed_time=latest,
                queue_depth=self.writer.queue.qsize(),
                error_count=failures,
                last_error=(
                    f"{failures} symbol requests failed"
                    if failures
                    else None
                ),
                metrics={
                    "symbols": len(symbols),
                    "failed_symbols": failures,
                },
            )

    async def _poll_alpha_symbol(self, symbol):
        async with self.alpha_semaphore:
            rows = await self.rest.fetch("alpha", symbol, limit=3)
        for row in rows:
            await self.writer.put("alpha", row)
        return rows[-1]["time"] if rows else None

    async def _futures_fallback_loop(self):
        while not self.stop.is_set():
            now = datetime.now(timezone.utc)
            next_minute = now.replace(second=4, microsecond=0)
            if next_minute <= now:
                next_minute += timedelta(minutes=1)
            await self._sleep((next_minute - now).total_seconds())
            if self.stop.is_set():
                return
            status = await asyncio.to_thread(fetch_candle_sync_status)
            websocket = next(
                (
                    row
                    for row in status.get("runtime") or []
                    if row.get("collector_id") == "futures_ws_1m"
                ),
                {},
            )
            last_event = websocket.get("last_event_at")
            event_age = float("inf")
            if last_event:
                event_age = (
                    datetime.now(timezone.utc) - parse_utc(last_event)
                ).total_seconds()
            if event_age <= FUTURES_FALLBACK_AFTER_SECONDS:
                continue
            results = await asyncio.gather(
                *(
                    self._poll_futures_symbol(symbol)
                    for symbol in self.symbols("futures")
                ),
                return_exceptions=True,
            )
            failures = sum(
                isinstance(result, Exception) for result in results
            )
            latest = max(
                (
                    result
                    for result in results
                    if isinstance(result, str)
                ),
                default=None,
            )
            await asyncio.to_thread(
                upsert_candle_sync_runtime,
                "futures_rest_fallback_1m",
                "futures",
                status="degraded" if failures else "running",
                connection_state="rest_fallback",
                last_event_at=format_utc(datetime.now(timezone.utc)),
                last_closed_time=latest,
                queue_depth=self.writer.queue.qsize(),
                error_count=failures,
                last_error=(
                    f"{failures} symbol requests failed"
                    if failures
                    else None
                ),
                metrics={
                    "symbols": len(self.symbols("futures")),
                    "failed_symbols": failures,
                    "websocket_event_age_seconds": (
                        round(event_age, 1) if last_event else None
                    ),
                },
            )

    async def _poll_futures_symbol(self, symbol):
        async with self.bootstrap_semaphore:
            rows = await self.rest.fetch("futures", symbol, limit=3)
        for row in rows:
            await self.writer.put("futures", row)
        return rows[-1]["time"] if rows else None

    async def _gap_repair_loop(self):
        await self._sleep(10)
        while not self.stop.is_set():
            gaps = await asyncio.to_thread(fetch_candle_gaps, "pending", 50)
            if gaps:
                await asyncio.gather(
                    *(self._repair_gap(gap) for gap in gaps)
                )
            await self._sleep(GAP_REPAIR_INTERVAL_SECONDS)

    async def _readiness_loop(self):
        first_pass = True
        while not self.stop.is_set():
            try:
                await self._reconcile_latest_aggregates(
                    tuple(INTERVAL_MINUTES)
                    if first_pass
                    else ("15m", "1h")
                )
            except Exception as exc:
                logger.warning("latest aggregate reconciliation failed: %s", exc)
            first_pass = False
            for pool_type in ("normal", "alpha"):
                try:
                    await asyncio.to_thread(
                        refresh_universe_readiness,
                        pool_type,
                        futures_source_env=SOURCE_ENV,
                    )
                except Exception as exc:
                    logger.warning(
                        "market readiness refresh failed %s: %s",
                        pool_type,
                        exc,
                    )
            await self._sleep(60)

    async def _reconcile_latest_aggregates(self, intervals):
        now = datetime.now(timezone.utc)
        batch = []
        for market_kind in ("spot", "futures", "alpha"):
            for symbol in self.symbols(market_kind):
                for interval in intervals:
                    close_minute = (
                        bucket_start(now, interval)
                        - timedelta(minutes=1)
                    )
                    batch.append(
                        (
                            market_kind,
                            {
                                "symbol": symbol,
                                "time": format_utc(close_minute),
                                "source_env": SOURCE_ENV,
                            },
                        )
                    )
        aggregates = await asyncio.to_thread(
            self.writer._build_aggregates,
            batch,
        )
        if not aggregates:
            return
        await asyncio.to_thread(upsert_aggregated_candles, aggregates)
        await asyncio.to_thread(
            materialize_aggregated_candles,
            aggregates,
        )
        logger.info(
            "reconciled %d latest complete aggregates",
            len(aggregates),
        )

    async def _repair_gap(self, gap):
        async with self.gap_semaphore:
            gap_id = gap["id"]
            try:
                await asyncio.to_thread(
                    update_candle_gap,
                    gap_id,
                    "repairing",
                )
                start = parse_utc(gap["start_time"])
                end = parse_utc(gap["end_time"])
                expected = int((end - start).total_seconds() // 60) + 1
                cursor = start
                repaired_rows = []
                while cursor <= end:
                    chunk_end = min(
                        end,
                        cursor + timedelta(minutes=999),
                    )
                    rows = await self.rest.fetch(
                        gap["market_kind"],
                        gap["symbol"],
                        start_time=cursor,
                        end_time=chunk_end + timedelta(seconds=59),
                        limit=1000,
                    )
                    repaired_rows.extend(
                        row
                        for row in rows
                        if cursor <= parse_utc(row["time"]) <= chunk_end
                    )
                    cursor = chunk_end + timedelta(minutes=1)
                await asyncio.to_thread(
                    upsert_minute_candles,
                    gap["market_kind"],
                    repaired_rows,
                )
                aggregates = await asyncio.to_thread(
                    self.writer._build_aggregates,
                    [
                        (gap["market_kind"], row)
                        for row in repaired_rows
                    ],
                )
                if aggregates:
                    await asyncio.to_thread(
                        upsert_aggregated_candles,
                        aggregates,
                    )
                    await asyncio.to_thread(
                        materialize_aggregated_candles,
                        aggregates,
                    )
                stored = await asyncio.to_thread(
                    fetch_minute_candles,
                    gap["market_kind"],
                    gap["symbol"],
                    gap["start_time"],
                    gap["end_time"],
                    gap["source_env"],
                )
                if len(stored) != expected:
                    raise RuntimeError(
                        f"gap still incomplete: {len(stored)}/{expected}"
                    )
                await asyncio.to_thread(
                    update_candle_gap,
                    gap_id,
                    "resolved",
                )
            except Exception as exc:
                await asyncio.to_thread(
                    update_candle_gap,
                    gap_id,
                    "pending",
                    str(exc)[:1000],
                )
                logger.warning("gap repair failed id=%s: %s", gap_id, exc)

    async def _maintenance_loop(self):
        while not self.stop.is_set():
            await self._sleep(3600)
            if self.stop.is_set():
                return
            deleted = await asyncio.to_thread(
                purge_minute_candle_data,
                RETENTION_DAYS,
            )
            logger.info("minute retention cleanup: %s", deleted)

    async def _sleep(self, seconds):
        try:
            await asyncio.wait_for(self.stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass


async def async_main():
    init_db()
    if not ENABLED:
        logger.info("minute pipeline disabled")
        return
    published = await asyncio.to_thread(materialize_stored_aggregates)
    reset_repairs = await asyncio.to_thread(reset_stale_candle_repairs)
    logger.info("published %d stored unified aggregates", published)
    if reset_repairs:
        logger.info("reset %d interrupted gap repairs", reset_repairs)
    pipeline = MinutePipeline()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, pipeline.stop.set)
        except NotImplementedError:
            pass
    await pipeline.run()


if __name__ == "__main__":
    asyncio.run(async_main())
