from __future__ import annotations

import asyncio
import json
import os

import httpx
import websockets


class BinanceUserStreamClient:
    def __init__(
        self,
        *,
        api_key: str,
        testnet: bool,
        http_client=None,
        keepalive_seconds: float | None = None,
    ):
        self.api_key = str(api_key or "")
        self.testnet = bool(testnet)
        self.rest_base = (
            "https://testnet.binancefuture.com"
            if self.testnet
            else "https://fapi.binance.com"
        )
        self.ws_base = (
            "wss://stream.binancefuture.com/ws"
            if self.testnet
            else "wss://fstream.binance.com/ws"
        )
        self.keepalive_seconds = float(
            keepalive_seconds
            if keepalive_seconds is not None
            else os.getenv("ACCOUNT_STREAM_KEEPALIVE_SECONDS", "2700")
        )
        self._owns_http = http_client is None
        self.http = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0),
            headers={"X-MBX-APIKEY": self.api_key},
        )

    @property
    def listen_key_endpoint(self) -> str:
        return self.rest_base + "/fapi/v1/listenKey"

    async def create_listen_key(self) -> str:
        response = await self.http.post(self.listen_key_endpoint)
        response.raise_for_status()
        listen_key = str(response.json().get("listenKey") or "")
        if not listen_key:
            raise RuntimeError("Binance did not return a futures listen key")
        return listen_key

    async def keepalive(self, listen_key: str) -> None:
        response = await self.http.put(self.listen_key_endpoint)
        response.raise_for_status()

    def websocket_url(self, listen_key: str) -> str:
        return f"{self.ws_base}/{listen_key}"

    async def _keepalive_loop(self, listen_key: str, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.keepalive_seconds)
                return
            except asyncio.TimeoutError:
                await self.keepalive(listen_key)

    async def events(self, stop: asyncio.Event):
        listen_key = await self.create_listen_key()
        keepalive_task = asyncio.create_task(
            self._keepalive_loop(listen_key, stop)
        )
        try:
            async with websockets.connect(
                self.websocket_url(listen_key),
                ping_interval=20,
                ping_timeout=20,
                close_timeout=5,
                max_queue=1000,
            ) as websocket:
                while not stop.is_set():
                    receive = asyncio.create_task(websocket.recv())
                    stopped = asyncio.create_task(stop.wait())
                    done, pending = await asyncio.wait(
                        {receive, stopped},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                    if stopped in done and stopped.result():
                        return
                    raw = receive.result()
                    payload = json.loads(raw)
                    if payload.get("e") == "listenKeyExpired":
                        raise RuntimeError("Binance futures listen key expired")
                    yield payload
        finally:
            keepalive_task.cancel()
            await asyncio.gather(keepalive_task, return_exceptions=True)

    async def close(self) -> None:
        if self._owns_http:
            await self.http.aclose()
