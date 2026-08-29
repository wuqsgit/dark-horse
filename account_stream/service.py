from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from account_stream.binance import BinanceUserStreamClient
from shared.accounts import account_exchange_config
from shared.live_account_store import (
    replace_live_account_snapshot,
    update_account_stream_state,
)
from trader.exchange import BinanceFutures


logger = logging.getLogger("account_stream")
RECONCILE_EVENTS = {
    "ACCOUNT_UPDATE",
    "ORDER_TRADE_UPDATE",
    "MARGIN_CALL",
    "ACCOUNT_CONFIG_UPDATE",
}


def _event_time(value) -> str | None:
    if value in (None, ""):
        return None
    try:
        timestamp = float(value) / 1000.0
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace(
        "+00:00", "Z"
    )


class AccountSynchronizer:
    def __init__(
        self,
        account: dict,
        *,
        exchange=None,
        stream=None,
        persist=replace_live_account_snapshot,
        update_state=update_account_stream_state,
        reconcile_interval: float = 10.0,
        reconnect_seconds: float = 2.0,
    ):
        self.account = dict(account)
        self.account_id = int(account["id"])
        config = None
        if exchange is None or stream is None:
            config = account_exchange_config(account, require_credentials=True)
        self.exchange = exchange or BinanceFutures(
            config=config,
            account_id=self.account_id,
            account_name=account.get("name"),
        )
        self.stream = stream or BinanceUserStreamClient(
            api_key=config["api_key"],
            testnet=bool(config.get("testnet")),
        )
        self.persist = persist
        self.update_state = update_state
        self.reconcile_interval = max(0.01, float(reconcile_interval))
        self.reconnect_seconds = max(0.01, float(reconnect_seconds))
        self._refresh = asyncio.Event()
        self._event_time = None

    async def _set_state(self, **fields) -> None:
        await asyncio.to_thread(self.update_state, self.account_id, **fields)

    async def reconcile(self, source: str, exchange_event_time=None) -> None:
        snapshot = await asyncio.to_thread(
            self.exchange.get_live_account_snapshot
        )
        await asyncio.to_thread(
            self.persist,
            self.account_id,
            snapshot.get("balance") or {},
            snapshot.get("positions") or [],
            snapshot.get("orders") or [],
            source=source,
            exchange_event_time=exchange_event_time,
        )

    async def _consume_events(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await self._set_state(status="ok", ws_connected=1)
                async for payload in self.stream.events(stop):
                    event_type = str(payload.get("e") or "")
                    if event_type in RECONCILE_EVENTS:
                        self._event_time = _event_time(payload.get("E"))
                        self._refresh.set()
                if stop.is_set():
                    return
                raise RuntimeError("Binance user stream ended")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                logger.warning(
                    "Account %s user stream disconnected: %s",
                    self.account_id,
                    exc,
                )
                await self._set_state(
                    status="degraded",
                    ws_connected=0,
                    last_error_at=now,
                    last_error=f"{type(exc).__name__}: {exc}",
                )
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.reconnect_seconds)
                except asyncio.TimeoutError:
                    pass

    async def run(self, stop: asyncio.Event) -> None:
        stream_task = None
        try:
            try:
                await self.reconcile("startup_http")
            except Exception as exc:
                now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                await self._set_state(
                    status="error",
                    ws_connected=0,
                    last_error_at=now,
                    last_error=f"{type(exc).__name__}: {exc}",
                )
            stream_task = asyncio.create_task(self._consume_events(stop))
            while not stop.is_set():
                try:
                    await asyncio.wait_for(
                        self._refresh.wait(),
                        timeout=self.reconcile_interval,
                    )
                    source = "ws_event"
                    event_time = self._event_time
                    self._refresh.clear()
                    self._event_time = None
                except asyncio.TimeoutError:
                    source = "periodic_http"
                    event_time = None
                if stop.is_set():
                    break
                try:
                    await self.reconcile(source, event_time)
                except Exception as exc:
                    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                    await self._set_state(
                        status="degraded",
                        last_error_at=now,
                        last_error=f"{type(exc).__name__}: {exc}",
                    )
        finally:
            if stream_task is not None:
                stream_task.cancel()
                await asyncio.gather(stream_task, return_exceptions=True)
            await self.stream.close()
            await asyncio.to_thread(self.exchange.close)
