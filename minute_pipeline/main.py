from __future__ import annotations

import asyncio
import logging
import os
import signal
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    READINESS_RECONCILE_SECONDS,
    REST_CONCURRENCY,
    RETENTION_DAYS,
    SOURCE_ENV,
    SPOT_WS_URL,
    UNIVERSE_REFRESH_SECONDS,
    WS_MAX_STREAMS_PER_CONNECTION,
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

RECOVERY_KLINE_LIMIT = 20


class MinutePipelineAlreadyRunning(RuntimeError):
    pass


def acquire_instance_lock(lock_path=None):
    """Hold a process lock for the lifetime of the minute pipeline.

    Two collectors writing the same SQLite database corrupt runtime cursors
    and create sustained lock contention.  The launcher also removes stale
    processes, but this lock protects manual starts and alternate terminals.
    """
    path = Path(lock_path) if lock_path else (
        Path(__file__).resolve().parents[1]
        / ".runtime"
        / "minute_pipeline.lock"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if handle.read(1) == "":
                handle.write(" ")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError) as exc:
        handle.close()
        raise MinutePipelineAlreadyRunning(
            "another minute_pipeline process already holds the instance lock"
        ) from exc
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def _batch_failure_detail(results) -> str | None:
    for result in results:
        if isinstance(result, BaseException):
            message = str(result).strip() or repr(result)
            return f"{type(result).__name__}: {message}"[:1000]
    return None


class MinutePipeline:
    def __init__(self):
        self.stop = asyncio.Event()
        self.writer_stop = asyncio.Event()
        self.bootstrap_complete = asyncio.Event()
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
            asyncio.create_task(self._bootstrap_with_signal()),
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
                    max_streams_per_connection=WS_MAX_STREAMS_PER_CONNECTION,
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
                    max_streams_per_connection=WS_MAX_STREAMS_PER_CONNECTION,
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
        for market_kind in ("spot", "futures", "alpha"):
            await asyncio.to_thread(
                upsert_candle_sync_runtime,
                f"{market_kind}_bootstrap_1m",
                market_kind,
                status="running",
                connection_state="rest",
                queue_depth=self.writer.queue.qsize(),
                metrics={
                    "symbols_total": len(self.symbols(market_kind)),
                },
            )
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
        # The periodic readiness loop starts concurrently with bootstrap and
        # can finish against the pre-bootstrap candle set. Publish readiness
        # again only after every bootstrapped candle has reached storage.
        for pool_type in ("normal", "alpha"):
            try:
                await asyncio.to_thread(
                    refresh_universe_readiness,
                    pool_type,
                    futures_source_env=SOURCE_ENV,
                )
            except Exception as exc:
                logger.warning(
                    "post-bootstrap readiness refresh failed %s: %s",
                    pool_type,
                    exc,
                )
        logger.info("minute bootstrap completed: %d symbol feeds", len(jobs))

    async def _bootstrap_with_signal(self):
        try:
            await self._bootstrap()
        finally:
            self.bootstrap_complete.set()

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
        try:
            # Skip a complete local window, but never rely on MAX(time) alone:
            # one current WebSocket event can otherwise hide an older gap.
            stored = await asyncio.to_thread(
                fetch_minute_candles,
                market_kind,
                symbol,
                format_utc(start),
                format_utc(end),
                SOURCE_ENV,
            )
            expected_latest = end - timedelta(minutes=1)
            replay_times = {
                format_utc(bucket_start(end, interval) - timedelta(minutes=1))
                for interval in ("15m", "1h")
            }
            replay_rows = [
                dict(row) for row in stored
                if str(row["time"]) in replay_times
            ]
            fetch_start = start
            if (
                len(stored) >= BOOTSTRAP_MINUTES
                and parse_utc(stored[-1]["time"]) >= expected_latest
            ):
                # A previous process can be terminated after its minute
                # transaction commits but before aggregate materialization.
                # Replay one persisted tail row to make that repair cheap and
                # deterministic without requeueing the full window.
                for row in replay_rows:
                    await self.writer.put(market_kind, row)
                return
            if len(stored) >= BOOTSTRAP_MINUTES:
                fetch_start = max(
                    start,
                    parse_utc(stored[-1]["time"]) + timedelta(minutes=1),
                )
            async with semaphore:
                rows = await self.rest.fetch(
                    market_kind,
                    symbol,
                    start_time=fetch_start,
                    end_time=end,
                    limit=BOOTSTRAP_MINUTES + 2,
                )
            stored_times = {str(row["time"]) for row in stored}
            queued_times = set()
            for row in rows:
                # REST bootstrap windows substantially overlap persisted data
                # after a normal restart. Re-enqueuing those rows used to fill
                # the FIFO and delay fresh WebSocket candles by many minutes.
                if str(row["time"]) not in stored_times:
                    await self.writer.put(market_kind, row)
                    queued_times.add(str(row["time"]))
            for row in replay_rows:
                if str(row["time"]) not in queued_times:
                    await self.writer.put(market_kind, row)
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
            recovered_after_reset = False
            if symbols and failures == len(symbols):
                first_error = _batch_failure_detail(results)
                logger.warning(
                    "all alpha REST requests failed; resetting transport: %s",
                    first_error,
                )
                await self.rest.reset("alpha")
                results = await asyncio.gather(
                    *(
                        self._poll_alpha_symbol(
                            symbol,
                            limit=RECOVERY_KLINE_LIMIT,
                        )
                        for symbol in symbols
                    ),
                    return_exceptions=True,
                )
                failures = sum(
                    isinstance(result, Exception) for result in results
                )
                recovered_after_reset = failures < len(symbols)
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
                    f"{failures} symbol requests failed; "
                    f"{_batch_failure_detail(results)}"
                    if failures
                    else None
                ),
                metrics={
                    "symbols": len(symbols),
                    "failed_symbols": failures,
                    "transport_reset": True
                    if recovered_after_reset
                    else False,
                },
            )

    async def _poll_alpha_symbol(self, symbol, *, limit=3):
        async with self.alpha_semaphore:
            rows = await self.rest.fetch("alpha", symbol, limit=limit)
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
            recovered_after_reset = False
            if self.symbols("futures") and failures == len(
                self.symbols("futures")
            ):
                first_error = _batch_failure_detail(results)
                logger.warning(
                    "all futures REST requests failed; resetting transport: %s",
                    first_error,
                )
                await self.rest.reset("futures")
                results = await asyncio.gather(
                    *(
                        self._poll_futures_symbol(
                            symbol,
                            limit=RECOVERY_KLINE_LIMIT,
                        )
                        for symbol in self.symbols("futures")
                    ),
                    return_exceptions=True,
                )
                failures = sum(
                    isinstance(result, Exception) for result in results
                )
                recovered_after_reset = failures < len(
                    self.symbols("futures")
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
                    f"{failures} symbol requests failed; "
                    f"{_batch_failure_detail(results)}"
                    if failures
                    else None
                ),
                metrics={
                    "symbols": len(self.symbols("futures")),
                    "failed_symbols": failures,
                    "transport_reset": True
                    if recovered_after_reset
                    else False,
                    "websocket_event_age_seconds": (
                        round(event_age, 1) if last_event else None
                    ),
                },
            )

    async def _poll_futures_symbol(self, symbol, *, limit=3):
        async with self.bootstrap_semaphore:
            rows = await self.rest.fetch("futures", symbol, limit=limit)
        for row in rows:
            await self.writer.put("futures", row)
        return rows[-1]["time"] if rows else None

    async def _gap_repair_loop(self):
        await self._wait_for_bootstrap()
        while not self.stop.is_set():
            gaps = await asyncio.to_thread(fetch_candle_gaps, "pending", 50)
            if gaps:
                await asyncio.gather(
                    *(self._repair_gap(gap) for gap in gaps)
                )
            await self._sleep(GAP_REPAIR_INTERVAL_SECONDS)

    async def _readiness_loop(self):
        await self._wait_for_bootstrap()
        # Bootstrap writes every affected interval and then publishes a fresh
        # readiness snapshot.  A simultaneous full-market reconciliation used
        # to compete with those writes and was the largest startup lock source.
        last_reconcile = asyncio.get_running_loop().time()
        while not self.stop.is_set():
            now = asyncio.get_running_loop().time()
            should_reconcile = (
                now - last_reconcile >= READINESS_RECONCILE_SECONDS
            )
            if should_reconcile:
                try:
                    await self._reconcile_latest_aggregates(
                        ("15m", "1h")
                    )
                    last_reconcile = asyncio.get_running_loop().time()
                except Exception as exc:
                    logger.warning(
                        "latest aggregate reconciliation failed: %s",
                        exc,
                    )
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

    async def _wait_for_bootstrap(self):
        while not self.stop.is_set() and not self.bootstrap_complete.is_set():
            try:
                await asyncio.wait_for(
                    self.bootstrap_complete.wait(),
                    timeout=5,
                )
            except asyncio.TimeoutError:
                pass

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
    instance_lock = acquire_instance_lock()
    try:
        init_db()
        if not ENABLED:
            logger.info("minute pipeline disabled")
            return
        # A full replay can touch hundreds of thousands of rows in the 6+ GB
        # SQLite database.  It is migration/backfill hygiene, not a reason to
        # keep live collection offline.  Bootstrap and reconciliation repair
        # the active window even when this best-effort replay is skipped.
        try:
            published = await asyncio.to_thread(
                materialize_stored_aggregates,
                12,
            )
            logger.info("published %d recent stored unified aggregates", published)
        except Exception as exc:
            logger.warning(
                "recent aggregate replay skipped; live bootstrap will repair it: %s",
                exc,
            )
        try:
            reset_repairs = await asyncio.to_thread(reset_stale_candle_repairs)
            if reset_repairs:
                logger.info("reset %d interrupted gap repairs", reset_repairs)
        except Exception as exc:
            logger.warning("stale gap reset skipped: %s", exc)
        pipeline = MinutePipeline()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, pipeline.stop.set)
            except NotImplementedError:
                pass
        await pipeline.run()
    finally:
        instance_lock.close()


if __name__ == "__main__":
    asyncio.run(async_main())
