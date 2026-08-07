from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
import websockets

from minute_pipeline.aggregation import format_utc, parse_utc
from minute_pipeline.config import (
    ALPHA_REST_URL,
    FUTURES_REST_URL,
    SOURCE_ENV,
    SPOT_REST_URL,
)
from shared.db import upsert_candle_sync_runtime

logger = logging.getLogger("minute_pipeline.collectors")

ALPHA_KLINES_PATH = "/bapi/defi/v1/public/alpha-trade/klines"


def normalize_rest_kline(symbol: str, row, source: str = "rest") -> dict:
    return {
        "time": format_utc(
            datetime.fromtimestamp(int(row[0]) / 1000, tz=timezone.utc)
        ),
        "symbol": str(symbol).upper(),
        "open": float(row[1]),
        "high": float(row[2]),
        "low": float(row[3]),
        "close": float(row[4]),
        "volume": float(row[5] or 0),
        "quote_vol": float(row[7] or 0),
        "trades": int(float(row[8] or 0)),
        "taker_buy_quote_vol": float(row[10] or 0)
        if len(row) > 10
        else 0.0,
        "source_env": SOURCE_ENV,
        "is_closed": True,
        "source": source,
    }


def normalize_ws_message(message) -> dict | None:
    if isinstance(message, (bytes, bytearray)):
        message = message.decode("utf-8")
    payload = json.loads(message)
    data = payload.get("data", payload)
    if data.get("e") != "kline":
        return None
    kline = data.get("k") or {}
    if not kline.get("x"):
        return None
    return {
        "time": format_utc(
            datetime.fromtimestamp(int(kline["t"]) / 1000, tz=timezone.utc)
        ),
        "symbol": str(data.get("s") or kline.get("s") or "").upper(),
        "open": float(kline["o"]),
        "high": float(kline["h"]),
        "low": float(kline["l"]),
        "close": float(kline["c"]),
        "volume": float(kline.get("v") or 0),
        "quote_vol": float(kline.get("q") or 0),
        "trades": int(kline.get("n") or 0),
        "taker_buy_quote_vol": float(kline.get("Q") or 0),
        "source_env": SOURCE_ENV,
        "is_closed": True,
        "source": "websocket",
    }


class RestKlineClient:
    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(15, pool=5),
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
                keepalive_expiry=30,
            ),
            headers={"User-Agent": "Mozilla/5.0"},
        )

    async def close(self):
        await self.client.aclose()

    async def fetch(
        self,
        market_kind: str,
        symbol: str,
        *,
        start_time=None,
        end_time=None,
        limit=1000,
    ) -> list[dict]:
        params = {
            "symbol": str(symbol).upper(),
            "interval": "1m",
            "limit": min(1000, max(1, int(limit))),
        }
        if start_time is not None:
            params["startTime"] = int(parse_utc(start_time).timestamp() * 1000)
        if end_time is not None:
            params["endTime"] = int(parse_utc(end_time).timestamp() * 1000)

        if market_kind == "spot":
            url = SPOT_REST_URL + "/api/v3/klines"
        elif market_kind == "futures":
            url = FUTURES_REST_URL + "/fapi/v1/klines"
        elif market_kind == "alpha":
            url = ALPHA_REST_URL + ALPHA_KLINES_PATH
        else:
            raise ValueError(f"unsupported market kind: {market_kind}")

        response = await self.client.get(url, params=params)
        response.raise_for_status()
        payload = response.json()
        if market_kind == "alpha":
            if (
                payload.get("code") not in (None, "000000")
                or payload.get("success") is False
            ):
                raise RuntimeError(f"Alpha kline API error: {payload}")
            payload = payload.get("data") or []
        rows = [
            normalize_rest_kline(symbol, row, source="backfill")
            for row in payload or []
        ]
        now = datetime.now(timezone.utc)
        return [
            row
            for row in rows
            if parse_utc(row["time"]) + timedelta(minutes=1) <= now
        ]


class BinanceKlineStream:
    def __init__(
        self,
        *,
        collector_id: str,
        market_kind: str,
        ws_url: str,
        symbols_provider,
        writer,
        reconnect_seconds: int,
    ):
        self.collector_id = collector_id
        self.market_kind = market_kind
        self.ws_url = ws_url
        self.symbols_provider = symbols_provider
        self.writer = writer
        self.reconnect_seconds = reconnect_seconds
        self.error_count = 0
        self.reconnect_count = 0
        self.event_count = 0
        self._last_runtime_write = 0.0
        self._last_event_monotonic = None

    async def _runtime(self, *, force=False, **fields):
        now = asyncio.get_running_loop().time()
        if not force and now - self._last_runtime_write < 5:
            return
        self._last_runtime_write = now
        await asyncio.to_thread(
            upsert_candle_sync_runtime,
            self.collector_id,
            self.market_kind,
            source_env=SOURCE_ENV,
            queue_depth=self.writer.queue.qsize(),
            error_count=self.error_count,
            reconnect_count=self.reconnect_count,
            metrics={"closed_events": self.event_count},
            **fields,
        )

    async def run(self, stop: asyncio.Event):
        backoff = 1
        while not stop.is_set():
            symbols = tuple(self.symbols_provider())
            if not symbols:
                await self._runtime(
                    force=True,
                    status="idle",
                    connection_state="waiting_for_universe",
                )
                await _wait_or_stop(stop, 30)
                continue
            try:
                await self._consume(symbols, stop)
                backoff = 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error_count += 1
                self.reconnect_count += 1
                await self._runtime(
                    force=True,
                    status="degraded",
                    connection_state="disconnected",
                    last_error=str(exc)[:1000],
                )
                logger.warning(
                    "%s stream disconnected: %s",
                    self.collector_id,
                    exc,
                )
                await _wait_or_stop(stop, backoff)
                backoff = min(30, backoff * 2)
        await self._runtime(
            force=True,
            status="stopped",
            connection_state="stopped",
        )

    async def _consume(self, symbols, stop):
        async with websockets.connect(
            self.ws_url,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            max_queue=2048,
        ) as websocket:
            for index in range(0, len(symbols), 100):
                params = [
                    f"{symbol.lower()}@kline_1m"
                    for symbol in symbols[index : index + 100]
                ]
                await websocket.send(
                    json.dumps(
                        {
                            "method": "SUBSCRIBE",
                            "params": params,
                            "id": index // 100 + 1,
                        }
                    )
                )
                await asyncio.sleep(0.25)
            await self._runtime(
                force=True,
                status="running",
                connection_state="connected",
                last_error=None,
            )
            deadline = (
                asyncio.get_running_loop().time() + self.reconnect_seconds
            )
            connected_at = asyncio.get_running_loop().time()
            while not stop.is_set():
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    self.reconnect_count += 1
                    return
                try:
                    message = await asyncio.wait_for(
                        websocket.recv(),
                        timeout=min(30, remaining),
                    )
                except asyncio.TimeoutError:
                    now = asyncio.get_running_loop().time()
                    silence = now - (
                        self._last_event_monotonic or connected_at
                    )
                    await self._runtime(
                        status="degraded" if silence > 90 else "running",
                        connection_state=(
                            "connected_silent"
                            if silence > 90
                            else "connected"
                        ),
                        last_error=(
                            f"no market event for {silence:.0f}s"
                            if silence > 90
                            else None
                        ),
                    )
                    continue
                row = normalize_ws_message(message)
                if not row or not row["symbol"]:
                    continue
                self.event_count += 1
                self._last_event_monotonic = (
                    asyncio.get_running_loop().time()
                )
                await self.writer.put(self.market_kind, row)
                closed_at = parse_utc(row["time"]) + timedelta(minutes=1)
                lag = max(
                    0,
                    (
                        datetime.now(timezone.utc) - closed_at
                    ).total_seconds(),
                )
                await self._runtime(
                    status="running",
                    connection_state="connected",
                    last_event_at=format_utc(datetime.now(timezone.utc)),
                    last_closed_time=row["time"],
                    lag_seconds=round(lag, 3),
                    last_error=None,
                )


async def _wait_or_stop(stop: asyncio.Event, seconds: float):
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
