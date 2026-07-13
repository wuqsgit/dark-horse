import asyncio
import json
import logging
from datetime import datetime, timezone

import httpx

from shared.db import (
    insert_futures_candles,
    insert_futures,
    insert_alpha_candles,
    insert_alpha_orderbook_snapshot,
    purge_old_kline_data,
    upsert_alpha_symbols,
    replace_market_universe,
    fetch_tracked_alpha_positions,
    futures_candles_current,
    RETENTION_DAYS,
)
from shared.market_universe import build_alpha_universe
from pipeline.candle_health import refresh_universe_readiness, retry_async

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
            info_resp, ticker_resp = await asyncio.gather(
                self.client.get("https://fapi.binance.com/fapi/v1/exchangeInfo"),
                self.client.get("https://fapi.binance.com/fapi/v1/ticker/24hr"),
            )
            info_resp.raise_for_status()
            ticker_resp.raise_for_status()
            volumes = {row.get("symbol"): _f(row.get("quoteVolume")) for row in ticker_resp.json()}
            return {
                row["symbol"]: {
                    "status": row.get("status"),
                    "contract_type": row.get("contractType"),
                    "quote_volume": volumes.get(row["symbol"], 0),
                }
                for row in info_resp.json().get("symbols", [])
                if row.get("status") == "TRADING"
            }
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
            if info and futures_symbol:
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
            if info and futures_symbol and tradeability != "inactive":
                universe.append({
                    "alpha_symbol": alpha_trade_symbol,
                    "base_asset": base_asset,
                    "futures_symbol": futures_symbol,
                    "tradeability": tradeability,
                    "volume_24h": volume_24h,
                    "futures_quote_volume_24h": _f((futures_symbols.get(futures_symbol) or {}).get("quote_volume")),
                })

        rows.sort(key=lambda r: float(r[10] or 0), reverse=True)
        universe.sort(key=lambda r: float(r.get("volume_24h") or 0), reverse=True)
        if limit:
            rows = rows[:limit]
            universe = universe[:limit]
        if rows:
            upsert_alpha_symbols(rows)
        logger.info("alpha universe refreshed: %s symbols", len(rows))
        return universe

    async def collect_market_data(self, universe, top_n=80):
        futures_markets = {
            item["futures_symbol"]: {
                "status": "TRADING", "contract_type": "PERPETUAL",
                "quote_volume": item.get("futures_quote_volume_24h", 0),
            }
            for item in universe if item.get("futures_symbol")
        }
        selected = build_alpha_universe(universe, futures_markets, limit=top_n, futures_volume_floor=100_000)
        selected_sources = {row["source_symbol"] for row in selected}
        by_source = {item["alpha_symbol"]: item for item in universe}
        for position in fetch_tracked_alpha_positions():
            source = position.get("alpha_symbol")
            if not source or source in selected_sources or source not in by_source:
                continue
            item = by_source[source]
            forced = build_alpha_universe([item], futures_markets, limit=1, futures_volume_floor=0)
            if forced:
                forced[0].update(selected=False, forced_position=True, universe_rank=None, selection_reason="open_position")
                selected.extend(forced)
        replace_market_universe("alpha", selected)
        if not selected:
            logger.info("alpha market data skipped: no futures-mapped alpha symbols")
            return
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
            "alpha_candles_1h": ("1h", 48),
            "alpha_candles_6h": ("6h", 48),
            "alpha_candles_24h": ("1d", 48),
        }

        async def fetch_one(item):
            symbol = item["source_symbol"]
            async with semaphore:
                try:
                    for table, (interval, limit) in interval_map.items():
                        async def request():
                            return await self._get_data(KLINES, {"symbol": symbol, "interval": interval, "limit": limit})
                        data = await retry_async(request, retries=2)
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
        purge_old_kline_data(days=RETENTION_DAYS)
        logger.info(
            "alpha market data: %s 1h, %s 15m, %s depth",
            len(rows_by_table["alpha_candles_1h"]),
            len(rows_by_table["alpha_candles_15m"]),
            len(depth_rows),
        )
        await self.collect_mapped_futures_data(selected)
        refresh_universe_readiness("alpha")

    @staticmethod
    def futures_table_for_interval(interval):
        suffix = "24h" if interval == "1d" else interval
        table = f"futures_candles_{suffix}"
        if table not in {"futures_candles_15m", "futures_candles_1h", "futures_candles_6h", "futures_candles_24h"}:
            raise ValueError(f"unsupported futures interval: {interval}")
        return table

    async def collect_mapped_futures_data(self, selected):
        futures_symbols = sorted({
            item.get("futures_symbol")
            for item in selected
            if item.get("futures_symbol")
        })
        if not futures_symbols:
            return

        rows_by_table = {
            "futures_candles_1h": [],
            "futures_candles_15m": [],
            "futures_candles_6h": [],
            "futures_candles_24h": [],
        }
        rows_fut = []
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        semaphore = asyncio.Semaphore(4)

        interval_map = {
            "futures_candles_15m": ("15m", 48),
            "futures_candles_1h": ("1h", 48),
            "futures_candles_6h": ("6h", 48),
            "futures_candles_24h": ("1d", 48),
        }

        try:
            premium_resp = await self.client.get("https://fapi.binance.com/fapi/v1/premiumIndex")
            premium_resp.raise_for_status()
            premium_map = {p.get("symbol"): p for p in premium_resp.json()}
        except Exception as exc:
            logger.warning("mapped futures premiumIndex failed: %s", exc)
            premium_map = {}

        async def fetch_one(symbol):
            async with semaphore:
                try:
                    if futures_candles_current(symbol):
                        return
                    for table, (interval, limit) in interval_map.items():
                        async def request():
                            response = await self.client.get(
                                "https://fapi.binance.com/fapi/v1/klines",
                                params={"symbol": symbol, "interval": interval, "limit": limit},
                            )
                            response.raise_for_status()
                            return response.json()
                        for o in await retry_async(request, retries=2):
                            rows_by_table[table].append((
                                _utc(o[0]),
                                symbol,
                                _f(o[1]), _f(o[2]), _f(o[3]), _f(o[4]),
                                _f(o[5]), _f(o[7]), _i(o[8]),
                            ))

                    prem = premium_map.get(symbol) or {}
                    funding = _f(prem.get("lastFundingRate"))
                    mark_price = _f(prem.get("markPrice"))
                    oi = 0.0
                    try:
                        oi_resp = await self.client.get(
                            "https://fapi.binance.com/fapi/v1/openInterest",
                            params={"symbol": symbol},
                        )
                        if oi_resp.status_code == 200:
                            oi = _f(oi_resp.json().get("openInterest"))
                    except Exception as exc:
                        logger.debug("mapped futures openInterest failed %s: %s", symbol, exc)
                    rows_fut.append((now_utc, symbol, oi, funding, mark_price))
                except Exception as exc:
                    logger.warning("mapped futures fetch failed %s: %s", symbol, exc)

        for i in range(0, len(futures_symbols), 10):
            await asyncio.gather(*(fetch_one(symbol) for symbol in futures_symbols[i:i + 10]))
            await asyncio.sleep(0.4)

        for table, rows in rows_by_table.items():
            insert_futures_candles(table, rows)
        if rows_fut:
            insert_futures(rows_fut)

        logger.info(
            "mapped futures data: %s symbols, %s 1h, %s 15m, %s futures",
            len(futures_symbols),
            len(rows_by_table["futures_candles_1h"]),
            len(rows_by_table["futures_candles_15m"]),
            len(rows_fut),
        )

    async def collect_all(self, universe_limit=200, market_top_n=80):
        universe = await self.refresh_universe(limit=universe_limit)
        await self.collect_market_data(universe, top_n=market_top_n)
        return universe
