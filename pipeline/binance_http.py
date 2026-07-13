"""Binance collector — direct HTTP, no ccxt dependency"""
import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from shared.db import (
    insert_candles_1h, insert_candles_15m, insert_candles_6h, insert_candles_24h,
    insert_futures_candles, insert_futures, upsert_symbol, insert_orderbook_snapshot,
    replace_market_universe, fetch_tracked_position_symbols, purge_old_kline_data, RETENTION_DAYS,
)
from shared.market_universe import build_normal_universe
from pipeline.candle_health import refresh_universe_readiness, retry_async

logger = logging.getLogger("binance")


class BinanceHTTPCollector:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=15, headers={"User-Agent": "Mozilla/5.0"})

    @staticmethod
    def kline_request(market, symbol, interval):
        if market == "spot":
            url = "https://api.binance.com/api/v3/klines"
        elif market == "futures":
            url = "https://fapi.binance.com/fapi/v1/klines"
        else:
            raise ValueError(f"unsupported market: {market}")
        return url, {"symbol": symbol, "interval": interval, "limit": 48}

    async def get_normal_universe(self, limit=150):
        """Get the liquid intersection of Binance spot and USDT perpetuals."""
        try:
            urls = (
                "https://api.binance.com/api/v3/exchangeInfo",
                "https://fapi.binance.com/fapi/v1/exchangeInfo",
                "https://api.binance.com/api/v3/ticker/24hr",
                "https://fapi.binance.com/fapi/v1/ticker/24hr",
            )
            responses = await asyncio.gather(*(self.client.get(url) for url in urls))
            for response in responses:
                response.raise_for_status()
            spot_info, futures_info, spot_tickers, futures_tickers = (response.json() for response in responses)
            spot_volumes = {row.get("symbol"): row.get("quoteVolume") for row in spot_tickers}
            futures_volumes = {row.get("symbol"): row.get("quoteVolume") for row in futures_tickers}
            spot = {
                row["symbol"]: {"status": row.get("status"), "quote_volume": spot_volumes.get(row["symbol"], 0)}
                for row in spot_info.get("symbols", []) if str(row.get("symbol", "")).endswith("USDT")
            }
            futures = {
                row["symbol"]: {
                    "status": row.get("status"), "contract_type": row.get("contractType"),
                    "quote_volume": futures_volumes.get(row["symbol"], 0),
                }
                for row in futures_info.get("symbols", []) if str(row.get("symbol", "")).endswith("USDT")
            }
            selected = build_normal_universe(spot, futures, limit=limit)
            selected_symbols = {row["source_symbol"] for row in selected}
            for symbol in sorted(fetch_tracked_position_symbols() - selected_symbols):
                if symbol not in spot or symbol not in futures:
                    continue
                forced = build_normal_universe({symbol: spot[symbol]}, {symbol: futures[symbol]}, limit=1)
                if forced:
                    forced[0].update(selected=False, forced_position=True, universe_rank=None, selection_reason="open_position")
                    selected.extend(forced)
            replace_market_universe("normal", selected)
            logger.info("Normal dual-market universe: %s selected, %s forced", sum(r["selected"] for r in selected), sum(r["forced_position"] for r in selected))
            return selected

        except Exception as e:
            logger.error(f"get_normal_universe: {e}")
            return []

    async def get_top_pairs(self, limit=150):
        return [row["source_symbol"] for row in await self.get_normal_universe(limit)]

    async def collect_all(self, symbols: list):
        if not symbols:
            return

        if isinstance(symbols[0], dict):
            symbols = [row["futures_symbol"] for row in symbols if row.get("selected") or row.get("forced_position")]

        now_utc = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        now_ms = int(time.time() * 1000)
        rows_1h = []
        rows_15m = []
        rows_6h = []
        rows_24h = []
        futures_rows = {"futures_candles_1h": [], "futures_candles_15m": [], "futures_candles_6h": [], "futures_candles_24h": []}
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
                    interval_tables = (("15m", rows_15m, "futures_candles_15m"), ("1h", rows_1h, "futures_candles_1h"), ("6h", rows_6h, "futures_candles_6h"), ("1d", rows_24h, "futures_candles_24h"))
                    for interval, spot_rows, futures_table in interval_tables:
                        for market, target in (("spot", spot_rows), ("futures", futures_rows[futures_table])):
                            url, params = self.kline_request(market, pair, interval)

                            async def request(url=url, params=params):
                                response = await self.client.get(url, params=params)
                                response.raise_for_status()
                                return response.json()

                            for o in await retry_async(request, retries=2):
                                target.append((
                                    datetime.fromtimestamp(o[0] / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                                    pair, float(o[1]), float(o[2]), float(o[3]), float(o[4]),
                                    float(o[5]), float(o[7]), int(o[8]),
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
        for table, rows in futures_rows.items():
            insert_futures_candles(table, rows)
        if rows_fut:
            insert_futures(rows_fut)

        purge_old_kline_data(days=RETENTION_DAYS)
        refresh_universe_readiness("normal")

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
