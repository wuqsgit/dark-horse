"""Binance data collector — SQLite backend"""
import asyncio
import time
import logging
from datetime import datetime, timezone

import ccxt.async_support as ccxt
import httpx

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.db import (
    insert_candles_1h, insert_candles_15m, insert_futures, upsert_symbol
)

logger = logging.getLogger("binance")
MIN_VOLUME_USD = 50_000


class BinanceCollector:
    def __init__(self):
        self.exchange = ccxt.binance({"enableRateLimit": True})
        self.http = httpx.AsyncClient(timeout=15)

    async def get_top_pairs(self, limit=150):
        try:
            tickers = await self.exchange.fetch_tickers()
            usdt_pairs = []
            for sym, t in tickers.items():
                if "/USDT" not in sym:
                    continue
                vol = t.get("quoteVolume", 0) or 0
                if vol >= MIN_VOLUME_USD:
                    usdt_pairs.append((sym, vol))
            usdt_pairs.sort(key=lambda x: -x[1])
            top = [p[0] for p in usdt_pairs[:limit]]
            logger.info(f"Top {len(top)} pairs, top vol: ${usdt_pairs[0][1]:,.0f}" if usdt_pairs else "No pairs")
            return top
        except Exception as e:
            logger.error(f"get_top_pairs: {e}")
            return []

    async def collect_all(self, symbols: list):
        now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows_1h = []
        rows_15m = []
        rows_fut = []

        # Fetch all futures data first
        try:
            url = "https://fapi.binance.com/fapi/v1/ticker/24hr"
            resp = await self.http.get(url)
            all_tickers = {}
            if resp.status_code == 200:
                all_tickers = {t["symbol"]: t for t in resp.json()}

            url2 = "https://fapi.binance.com/fapi/v1/premiumIndex"
            resp2 = await self.http.get(url2)
            premium = {}
            if resp2.status_code == 200:
                for p in resp2.json():
                    premium[p["symbol"]] = p
        except Exception as e:
            logger.error(f"Futures fetch error: {e}")
            all_tickers = {}
            premium = {}

        now_ms = int(time.time() * 1000)

        for sym in symbols:
            pair = sym.replace("/", "")
            try:
                # 1h klines (last 48)
                ohlcv = await self.exchange.fetch_ohlcv(sym, "1h", since=now_ms - 48 * 3600 * 1000, limit=48)
                for o in ohlcv:
                    rows_1h.append((
                        datetime.fromtimestamp(o[0] / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        pair,
                        float(o[1]), float(o[2]), float(o[3]), float(o[4]),
                        float(o[5]), float(o[5]) * float(o[4]),
                        int(o[6]) if len(o) > 6 else 0,
                    ))

                # 15m klines (last 12h)
                ohlcv_15 = await self.exchange.fetch_ohlcv(sym, "15m", since=now_ms - 12 * 3600 * 1000, limit=48)
                for o in ohlcv_15:
                    rows_15m.append((
                        datetime.fromtimestamp(o[0] / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        pair,
                        float(o[1]), float(o[2]), float(o[3]), float(o[4]),
                        float(o[5]), float(o[5]) * float(o[4]),
                        int(o[6]) if len(o) > 6 else 0,
                    ))

                # Futures data from cached API
                ticker = all_tickers.get(pair)
                prem = premium.get(pair, {})
                oi = float(ticker.get("openInterest", 0)) if ticker else 0
                mark = float(ticker.get("lastPrice", 0)) if ticker else 0
                fr = float(prem.get("lastFundingRate", 0)) if prem else 0
                rows_fut.append((now, pair, oi, fr, mark))

                await upsert_symbol(pair)
                await asyncio.sleep(0.03)

            except Exception as e:
                logger.warning(f"Failed {sym}: {e}")
                continue

        if rows_1h:
            insert_candles_1h(rows_1h)
        if rows_15m:
            insert_candles_15m(rows_15m)
        if rows_fut:
            insert_futures(rows_fut)

        logger.info(f"Inserted {len(rows_1h)} 1h + {len(rows_15m)} 15m candles, {len(rows_fut)} futures")

    async def close(self):
        await self.exchange.close()
        await self.http.aclose()
