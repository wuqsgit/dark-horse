import asyncio
import json
import logging
from datetime import datetime, timezone

import httpx

from shared.db import (
    insert_alpha_candles,
    insert_alpha_orderbook_snapshot,
    upsert_alpha_symbols,
)

logger = logging.getLogger("alpha_pipeline")

BASE = "https://www.binance.com"
TOKEN_LIST = "/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list"
EXCHANGE_INFO = "/bapi/defi/v1/public/alpha-trade/get-exchange-info"
KLINES = "/bapi/defi/v1/public/alpha-trade/klines"
TICKER = "/bapi/defi/v1/public/alpha-trade/ticker"
FULL_DEPTH = "/bapi/defi/v1/public/alpha-trade/fullDepth"


def _f(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _utc(ms):
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class AlphaCollector:
    def __init__(self):
        self.client = httpx.AsyncClient(timeout=15, headers={"User-Agent": "Mozilla/5.0"})

    async def close(self):
        await self.client.aclose()

    async def _get_data(self, path, params=None):
        resp = await self.client.get(BASE + path, params=params)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("code") not in (None, "000000") or payload.get("success") is False:
            raise RuntimeError(f"Alpha API error {path}: {payload}")
        return payload.get("data")

    async def get_token_list(self):
        data = await self._get_data(TOKEN_LIST)
        return data or []

    async def get_exchange_symbols(self):
        data = await self._get_data(EXCHANGE_INFO)
        symbols = data.get("symbols") if isinstance(data, dict) else []
        return symbols or []

    async def get_futures_symbols(self):
        try:
            resp = await self.client.get("https://fapi.binance.com/fapi/v1/exchangeInfo")
            resp.raise_for_status()
            return {s["symbol"] for s in resp.json().get("symbols", []) if s.get("status") == "TRADING"}
        except Exception as exc:
            logger.warning("futures exchangeInfo failed: %s", exc)
            return set()

    async def refresh_universe(self, limit=200):
        tokens, exchange_symbols, futures_symbols = await asyncio.gather(
            self.get_token_list(),
            self.get_exchange_symbols(),
            self.get_futures_symbols(),
        )
        trade_by_symbol = {
            s.get("symbol"): s
            for s in exchange_symbols
            if s.get("status") == "TRADING" and str(s.get("symbol", "")).endswith("USDT")
        }

        rows = []
        universe = []
        for token in tokens:
            base_asset = str(token.get("symbol") or "").upper()
            if not base_asset:
                continue
            alpha_id = str(token.get("alphaId") or "").upper()
            alpha_trade_symbol = f"{alpha_id}USDT" if alpha_id else None
            info = trade_by_symbol.get(alpha_trade_symbol)
            tradeability = "alpha_tradeable" if info else "alpha_only"
            status = (info or {}).get("status") or ("TRADING" if alpha_trade_symbol else "WATCH")

            futures_symbol = f"{base_asset}USDT" if f"{base_asset}USDT" in futures_symbols else None
            if futures_symbol:
                tradeability = "alpha_futures_mapped"

            volume_24h = _f(token.get("volume24h"))
            if volume_24h <= 0:
                tradeability = "inactive"

            rows.append((
                alpha_trade_symbol,
                base_asset,
                token.get("tokenId"),
                token.get("name"),
                status,
                alpha_trade_symbol,
                futures_symbol,
                tradeability,
                _f(token.get("price")),
                _f(token.get("percentChange24h")),
                volume_24h,
                _f(token.get("liquidity")),
                _f(token.get("marketCap")),
                json.dumps(token, ensure_ascii=False),
            ))
            if tradeability != "inactive":
                universe.append({
                    "alpha_symbol": alpha_trade_symbol,
                    "base_asset": base_asset,
                    "futures_symbol": futures_symbol,
                    "tradeability": tradeability,
                    "volume_24h": volume_24h,
                })

        rows.sort(key=lambda r: float(r[10] or 0), reverse=True)
        if limit:
            keep = {r[0] for r in rows[:limit]}
            rows = [r for r in rows if r[0] in keep]
            universe = [u for u in universe if u["alpha_symbol"] in keep]
        if rows:
            upsert_alpha_symbols(rows)
        logger.info("alpha universe refreshed: %s symbols", len(rows))
        return universe

    async def collect_market_data(self, universe, top_n=80):
        selected = sorted(universe, key=lambda x: -float(x.get("volume_24h") or 0))[:top_n]
        rows_by_table = {
            "alpha_candles_1h": [],
            "alpha_candles_15m": [],
            "alpha_candles_6h": [],
            "alpha_candles_24h": [],
        }
        depth_rows = []
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        semaphore = asyncio.Semaphore(4)

        interval_map = {
            "alpha_candles_15m": ("15m", 48),
            "alpha_candles_1h": ("1h", 72),
            "alpha_candles_6h": ("6h", 56),
            "alpha_candles_24h": ("1d", 35),
        }

        async def fetch_one(item):
            symbol = item["alpha_symbol"]
            async with semaphore:
                try:
                    for table, (interval, limit) in interval_map.items():
                        data = await self._get_data(KLINES, {"symbol": symbol, "interval": interval, "limit": limit})
                        for o in data or []:
                            rows_by_table[table].append((
                                _utc(o[0]),
                                symbol,
                                _f(o[1]), _f(o[2]), _f(o[3]), _f(o[4]),
                                _f(o[5]), _f(o[7]), _i(o[8]),
                            ))

                    try:
                        depth = await self._get_data(FULL_DEPTH, {"symbol": symbol, "limit": 20})
                        bids = depth.get("bids") or []
                        asks = depth.get("asks") or []
                        bid_depth = sum(_f(q) for _, q in bids[:20])
                        ask_depth = sum(_f(q) for _, q in asks[:20])
                        top_bid = _f(bids[0][0]) if bids else 0
                        top_ask = _f(asks[0][0]) if asks else 0
                        spread_pct = ((top_ask - top_bid) / top_bid * 100) if top_bid > 0 and top_ask > 0 else 0
                        spread_pct = max(0.0, spread_pct)
                        depth_rows.append((
                            now_utc,
                            symbol,
                            bid_depth,
                            ask_depth,
                            bid_depth / ask_depth if ask_depth > 0 else 0,
                            spread_pct,
                            _f(bids[0][1]) if bids else 0,
                            _f(asks[0][1]) if asks else 0,
                        ))
                    except Exception as depth_exc:
                        logger.debug("alpha depth failed %s: %s", symbol, depth_exc)
                except Exception as exc:
                    logger.warning("alpha market fetch failed %s: %s", symbol, exc)

        for i in range(0, len(selected), 10):
            await asyncio.gather(*(fetch_one(item) for item in selected[i:i + 10]))
            await asyncio.sleep(0.4)

        for table, rows in rows_by_table.items():
            if rows:
                insert_alpha_candles(table, rows)
        if depth_rows:
            insert_alpha_orderbook_snapshot(depth_rows)
        logger.info(
            "alpha market data: %s 1h, %s 15m, %s depth",
            len(rows_by_table["alpha_candles_1h"]),
            len(rows_by_table["alpha_candles_15m"]),
            len(depth_rows),
        )

    async def collect_all(self, universe_limit=200, market_top_n=80):
        universe = await self.refresh_universe(limit=universe_limit)
        await self.collect_market_data(universe, top_n=market_top_n)
        return universe
