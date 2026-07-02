"""Binance collector — direct HTTP, no ccxt dependency"""
import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.db import (
    insert_candles_1h, insert_candles_15m, insert_candles_6h, insert_candles_24h, insert_futures, upsert_symbol, insert_orderbook_snapshot
)

logger = logging.getLogger("binance")


class BinanceHTTPCollector:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=15, headers={"User-Agent": "Mozilla/5.0"})

    async def get_top_pairs(self, limit=150):
        """Get top USDT pairs by 24h quote volume from Binance futures"""
        try:
            resp = await self.client.get("https://fapi.binance.com/fapi/v1/ticker/24hr")
            if resp.status_code != 200:
                logger.error(f"ticker/24hr: {resp.status_code}")
                return []

            data = resp.json()
            pairs = []
            for t in data:
                sym = t.get("symbol", "")
                if sym.endswith("USDT"):
                    vol = float(t.get("quoteVolume", 0) or 0)
                    trades = float(t.get("count", 0) or 0)
                    change_abs = abs(float(t.get("priceChangePercent", 0) or 0))
                    if vol > 100_000:
                        pairs.append({
                            "symbol": sym,
                            "quote_volume": vol,
                            "trades": trades,
                            "abs_change": change_abs,
                        })

            by_volume = sorted(pairs, key=lambda x: -x["quote_volume"])[:limit]
            by_change = sorted(
                [p for p in pairs if p["quote_volume"] >= 500_000],
                key=lambda x: -x["abs_change"],
            )[: max(20, limit // 4)]
            by_activity = sorted(
                [p for p in pairs if p["quote_volume"] >= 300_000],
                key=lambda x: -x["trades"],
            )[: max(20, limit // 4)]

            merged = {}
            for bucket in (by_volume, by_change, by_activity):
                for p in bucket:
                    merged[p["symbol"]] = p

            ranked = sorted(
                merged.values(),
                key=lambda x: (
                    -x["quote_volume"],
                    -x["abs_change"],
                    -x["trades"],
                ),
            )
            top = [p["symbol"] for p in ranked[: max(limit, len(ranked))]]
            logger.info(
                "Top %s pairs from volume/change/activity pools (top volume: $%s)",
                len(top),
                f"{by_volume[0]['quote_volume']:,.0f}" if by_volume else "0",
            )
            return top

        except Exception as e:
            logger.error(f"get_top_pairs: {e}")
            return []

    async def collect_all(self, symbols: list):
        if not symbols:
            return

        now_utc = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        now_ms = int(time.time() * 1000)
        rows_1h = []
        rows_15m = []
        rows_6h = []
        rows_24h = []
        rows_fut = []

        # Pre-fetch futures data
        try:
            resp = await self.client.get("https://fapi.binance.com/fapi/v1/premiumIndex")
            premium_map = {}
            if resp.status_code == 200:
                for p in resp.json():
                    premium_map[p["symbol"]] = p
        except Exception as e:
            logger.warning(f"premiumIndex error: {e}")
            premium_map = {}

        semaphore = asyncio.Semaphore(5)  # Max 5 concurrent requests

        async def fetch_one(sym):
            async with semaphore:
                try:
                    pair = sym.replace("/", "")

                    # 1h klines (last 48)
                    url = f"https://api.binance.com/api/v3/klines?symbol={pair}&interval=1h&limit=48"
                    resp = await self.client.get(url)
                    if resp.status_code == 200:
                        for o in resp.json():
                            rows_1h.append((
                                datetime.fromtimestamp(o[0] / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                pair,
                                float(o[1]), float(o[2]), float(o[3]), float(o[4]),
                                float(o[5]), float(o[7]),
                                int(o[8]),
                            ))

                    # 15m klines (last 12h)
                    url15 = f"https://api.binance.com/api/v3/klines?symbol={pair}&interval=15m&limit=48"
                    resp15 = await self.client.get(url15)
                    if resp15.status_code == 200:
                        for o in resp15.json():
                            rows_15m.append((
                                datetime.fromtimestamp(o[0] / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                pair,
                                float(o[1]), float(o[2]), float(o[3]), float(o[4]),
                                float(o[5]), float(o[7]),
                                int(o[8]),
                            ))

                    # 6h klines (last 12 days)
                    url6h = f"https://api.binance.com/api/v3/klines?symbol={pair}&interval=6h&limit=48"
                    resp6h = await self.client.get(url6h)
                    if resp6h.status_code == 200:
                        for o in resp6h.json():
                            rows_6h.append((
                                datetime.fromtimestamp(o[0] / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                pair,
                                float(o[1]), float(o[2]), float(o[3]), float(o[4]),
                                float(o[5]), float(o[7]),
                                int(o[8]),
                            ))

                    # 24h klines (last 30 days)
                    url24h = f"https://api.binance.com/api/v3/klines?symbol={pair}&interval=1d&limit=30"
                    resp24h = await self.client.get(url24h)
                    if resp24h.status_code == 200:
                        for o in resp24h.json():
                            rows_24h.append((
                                datetime.fromtimestamp(o[0] / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                pair,
                                float(o[1]), float(o[2]), float(o[3]), float(o[4]),
                                float(o[5]), float(o[7]),
                                int(o[8]),
                            ))

                    # Futures data
                    prem = premium_map.get(pair, {})
                    fr = float(prem.get("lastFundingRate", 0) or 0) if prem else 0
                    mark = float(prem.get("markPrice", 0) or 0) if prem else 0

                    oi_url = f"https://fapi.binance.com/fapi/v1/openInterest?symbol={pair}"
                    oi_resp = await self.client.get(oi_url)
                    oi = 0
                    if oi_resp.status_code == 200:
                        oi = float(oi_resp.json().get("openInterest", 0) or 0)

                    rows_fut.append((now_utc, pair, oi, fr, mark))
                    upsert_symbol(pair)

                except Exception as e:
                    logger.warning(f"Failed {sym}: {e}")

        # Process in batches
        batch_size = 10
        for i in range(0, len(symbols), batch_size):
            batch = symbols[i:i+batch_size]
            await asyncio.gather(*[fetch_one(s) for s in batch])
            await asyncio.sleep(0.5)  # rate limit buffer

        if rows_1h:
            insert_candles_1h(rows_1h)
        if rows_15m:
            insert_candles_15m(rows_15m)
        if rows_6h:
            insert_candles_6h(rows_6h)
        if rows_24h:
            insert_candles_24h(rows_24h)
        if rows_fut:
            insert_futures(rows_fut)

        logger.info(f"Done: {len(rows_1h)} 1h + {len(rows_15m)} 15m + {len(rows_6h)} 6h + {len(rows_24h)} 24h + {len(rows_fut)} futures")

    async def collect_depth(self, symbols: list, top_n: int = 20):
        """采集深度数据（只采评分前top_n的币，减少API消耗）
        
        Args:
            symbols: 币种列表（按成交量排序取前top_n）
            top_n: 最多采多少个币的深度
        """
        if not symbols:
            return
        
        # 只取前top_n（成交量最大的，API限制考虑）
        symbols = symbols[:top_n]
        now_utc = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = []
        semaphore = asyncio.Semaphore(3)
        
        async def fetch_depth(sym):
            async with semaphore:
                try:
                    pair = sym.replace("/", "")
                    url = f"https://api.binance.com/api/v3/depth?symbol={pair}&limit=100"
                    resp = await self.client.get(url, timeout=10)
                    if resp.status_code != 200:
                        return
                    
                    data = resp.json()
                    bids = data.get("bids", [])
                    asks = data.get("asks", [])
                    
                    # 计算买卖盘总量（前20档）
                    bid_depth = sum([float(q) for p, q in bids[:20]])
                    ask_depth = sum([float(q) for p, q in asks[:20]])
                    imbalance = bid_depth / ask_depth if ask_depth > 0 else 0
                    
                    # 卖一/买一量
                    top_bid_qty = float(bids[0][1]) if bids else 0
                    top_ask_qty = float(asks[0][1]) if asks else 0
                    
                    rows.append((now_utc, pair, bid_depth, ask_depth, imbalance, top_bid_qty, top_ask_qty))
                except Exception as e:
                    logger.warning(f"Depth fetch failed {sym}: {e}")
        
        await asyncio.gather(*[fetch_depth(s) for s in symbols])
        if rows:
            insert_orderbook_snapshot(rows)
            logger.info(f"Depth done: {len(rows)} symbols")

    async def close(self):
        await self.client.aclose()
