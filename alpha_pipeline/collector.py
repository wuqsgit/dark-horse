import asyncio
import json
import logging
import os
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
    fetch_market_universe,
    fetch_tracked_alpha_positions,
    futures_candles_current,
    get_conn,
    RETENTION_DAYS,
)
from shared.market_universe import build_alpha_universe
from pipeline.candle_health import refresh_universe_readiness, retry_async
from alpha_engine.strategy.market_data import (
    futures_rest_base,
    is_closed_kline,
    resolve_market_env,
)

logger = logging.getLogger("alpha_pipeline")

BASE = "https://www.binance.com"
TOKEN_LIST = "/bapi/defi/v1/public/wallet-direct/buw/wallet/cex/alpha/all/token/list"
EXCHANGE_INFO = "/bapi/defi/v1/public/alpha-trade/get-exchange-info"
KLINES = "/bapi/defi/v1/public/alpha-trade/klines"
TICKER = "/bapi/defi/v1/public/alpha-trade/ticker"
FULL_DEPTH = "/bapi/defi/v1/public/alpha-trade/fullDepth"

# The strategy readiness gate requires this much closed history. The unified
# minute pipeline normally maintains current bars, while the Alpha collector
# fills this initial history after a fresh install or market-env migration.
READINESS_HISTORY = {
    "alpha_candles_15m": 32,
    "alpha_candles_1h": 50,
    "futures_candles_15m": 32,
    "futures_candles_1h": 50,
}


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
    def __init__(self, market_env=None):
        self.market_env = resolve_market_env(market_env)
        self.futures_base = futures_rest_base(self.market_env)
        self.unified_candles = os.getenv(
            "UNIFIED_CANDLE_PIPELINE_ENABLED",
            "true",
        ).strip().lower() in {"1", "true", "yes", "on"}
        self.client = self._new_client()

    @staticmethod
    def _new_client():
        return httpx.AsyncClient(
            timeout=httpx.Timeout(15, pool=5),
            limits=httpx.Limits(
                max_connections=20,
                max_keepalive_connections=10,
                keepalive_expiry=30,
            ),
            headers={"User-Agent": "Mozilla/5.0"},
        )

    async def reset_client(self):
        previous = self.client
        self.client = self._new_client()
        await previous.aclose()

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
                self.client.get(self.futures_base + "/fapi/v1/exchangeInfo"),
                self.client.get(self.futures_base + "/fapi/v1/ticker/24hr"),
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

    @staticmethod
    def normalize_kline_row(symbol, row, *, market_env, now_ms=None):
        """Convert a Binance-style kline to the extended candle schema."""
        return (
            _utc(row[0]),
            symbol,
            _f(row[1]), _f(row[2]), _f(row[3]), _f(row[4]),
            _f(row[5]), _f(row[7]), _i(row[8]),
            _f(row[10]) if len(row) > 10 else None,
            resolve_market_env(market_env),
            1 if is_closed_kline(row, now_ms=now_ms) else 0,
        )

    @staticmethod
    def candle_history_counts(table, symbols, *, source_env="mainnet"):
        symbol_columns = {
            "alpha_candles_15m": "alpha_symbol",
            "alpha_candles_1h": "alpha_symbol",
            "futures_candles_15m": "symbol",
            "futures_candles_1h": "symbol",
        }
        symbol_column = symbol_columns.get(table)
        normalized = sorted({str(symbol) for symbol in symbols if symbol})
        if not symbol_column or not normalized:
            return {}
        placeholders = ",".join("?" for _ in normalized)
        conn = get_conn()
        try:
            rows = conn.execute(
                f"""SELECT {symbol_column} AS symbol, COUNT(*) AS count
                    FROM {table}
                    WHERE source_env=? AND is_closed=1
                      AND {symbol_column} IN ({placeholders})
                    GROUP BY {symbol_column}""",
                (resolve_market_env(source_env), *normalized),
            ).fetchall()
            return {
                str(row["symbol"]): int(row["count"] or 0)
                for row in rows
            }
        finally:
            conn.close()

    async def refresh_universe(self, limit=200):
        results = await asyncio.gather(
            self.get_token_list(),
            self.get_exchange_symbols(),
            self.get_futures_symbols(),
            return_exceptions=True,
        )
        failures = [result for result in results if isinstance(result, Exception)]
        if failures:
            raise failures[0]
        tokens, exchange_symbols, futures_symbols = results
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
            "alpha_candles_15m": ("15m", 192),
            "alpha_candles_1h": ("1h", 120),
            "alpha_candles_6h": ("6h", 60),
            "alpha_candles_24h": ("1d", 60),
        }
        alpha_symbols = [item["source_symbol"] for item in selected]
        history_counts = {
            table: self.candle_history_counts(
                table,
                alpha_symbols,
                source_env="mainnet",
            )
            for table in READINESS_HISTORY
            if table.startswith("alpha_")
        }

        async def fetch_one(item):
            symbol = item["source_symbol"]
            async with semaphore:
                try:
                    for table, (interval, limit) in interval_map.items():
                        minimum = READINESS_HISTORY.get(table)
                        if self.unified_candles and (
                            minimum is None
                            or history_counts.get(table, {}).get(symbol, 0)
                            >= minimum
                        ):
                            continue
                        async def request():
                            return await self._get_data(KLINES, {"symbol": symbol, "interval": interval, "limit": limit})
                        data = await retry_async(request, retries=2)
                        for o in data or []:
                            rows_by_table[table].append(
                                self.normalize_kline_row(
                                    symbol,
                                    o,
                                    market_env="mainnet",
                                )
                            )

                    try:
                        depth = await self._get_data(FULL_DEPTH, {"symbol": symbol, "limit": 20})
                        bids = depth.get("bids") or []
                        asks = depth.get("asks") or []
                        bid_depth = sum(_f(price) * _f(qty) for price, qty in bids[:20])
                        ask_depth = sum(_f(price) * _f(qty) for price, qty in asks[:20])
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
        await self.collect_mapped_futures_data(
            selected,
            include_candles=not self.unified_candles,
        )
        try:
            refresh_universe_readiness(
                "alpha",
                futures_source_env=self.market_env,
            )
        except Exception as exc:
            # Market/depth/futures collection has already succeeded.  A busy
            # readiness metadata write must not mark the entire Alpha feed as
            # failed; the minute pipeline and next pass will republish it.
            logger.warning("alpha readiness publication deferred: %s", exc)

    @staticmethod
    def futures_table_for_interval(interval):
        suffix = "24h" if interval == "1d" else interval
        table = f"futures_candles_{suffix}"
        if table not in {"futures_candles_15m", "futures_candles_1h", "futures_candles_6h", "futures_candles_24h"}:
            raise ValueError(f"unsupported futures interval: {interval}")
        return table

    async def collect_mapped_futures_data(
        self,
        selected,
        *,
        include_candles=True,
    ):
        mapped_futures_symbols = {
            item.get("futures_symbol")
            for item in selected
            if item.get("futures_symbol")
        }
        # BTC is a required market-context feature even when it is not part of
        # the selected Alpha universe.
        futures_symbols = sorted(mapped_futures_symbols | {"BTCUSDT"})
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
            # Refresh at the bar interval, before the looser readiness age
            # expires. This headroom prevents a long full collection from
            # crossing the readiness boundary after a symbol was skipped.
            "futures_candles_15m": ("15m", 192, 15),
            "futures_candles_1h": ("1h", 120, 60),
            "futures_candles_6h": ("6h", 60, 360),
            "futures_candles_24h": ("1d", 60, 1440),
        }
        history_counts = {
            table: self.candle_history_counts(
                table,
                futures_symbols,
                source_env=self.market_env,
            )
            for table in READINESS_HISTORY
            if table.startswith("futures_")
        }

        try:
            premium_resp = await self.client.get(
                self.futures_base + "/fapi/v1/premiumIndex"
            )
            premium_resp.raise_for_status()
            premium_map = {p.get("symbol"): p for p in premium_resp.json()}
        except Exception as exc:
            logger.warning("mapped futures premiumIndex failed: %s", exc)
            premium_map = {}

        async def fetch_one(symbol):
            async with semaphore:
                try:
                    for table, (
                        interval,
                        limit,
                        max_age_minutes,
                    ) in interval_map.items():
                        minimum = READINESS_HISTORY.get(table)
                        history_missing = bool(
                            minimum is not None
                            and history_counts.get(table, {}).get(symbol, 0)
                            < minimum
                        )
                        if not include_candles and not history_missing:
                            continue
                        if not history_missing and futures_candles_current(
                            symbol,
                            max_age_minutes=max_age_minutes,
                            source_env=self.market_env,
                            table=table,
                            closed_only=True,
                        ):
                            continue
                        async def request():
                            response = await self.client.get(
                                self.futures_base + "/fapi/v1/klines",
                                params={"symbol": symbol, "interval": interval, "limit": limit},
                            )
                            response.raise_for_status()
                            return response.json()
                        for o in await retry_async(request, retries=2):
                            rows_by_table[table].append(
                                self.normalize_kline_row(
                                    symbol,
                                    o,
                                    market_env=self.market_env,
                                )
                            )

                    prem = premium_map.get(symbol) or {}
                    funding = _f(prem.get("lastFundingRate"))
                    mark_price = _f(prem.get("markPrice"))
                    oi = 0.0
                    try:
                        oi_resp = await self.client.get(
                            self.futures_base + "/fapi/v1/openInterest",
                            params={"symbol": symbol},
                        )
                        if oi_resp.status_code == 200:
                            oi = _f(oi_resp.json().get("openInterest"))
                    except Exception as exc:
                        logger.debug("mapped futures openInterest failed %s: %s", symbol, exc)
                    rows_fut.append(
                        (
                            now_utc,
                            symbol,
                            oi,
                            funding,
                            mark_price,
                            self.market_env,
                        )
                    )
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

    @staticmethod
    def _selected_market_rows():
        return [
            dict(row)
            for row in fetch_market_universe(
                "alpha",
                selected_only=True,
            )
        ]

    @staticmethod
    def _watched_alpha_symbols() -> set[str]:
        conn = get_conn()
        try:
            rows = conn.execute(
                """SELECT alpha_symbol FROM alpha_signal_states
                   WHERE alpha_symbol IS NOT NULL
                     AND state NOT IN ('IDLE','FAILED','EXPIRED','COOLDOWN')"""
            ).fetchall()
            return {
                str(row["alpha_symbol"]).upper()
                for row in rows
                if row["alpha_symbol"]
            }
        finally:
            conn.close()

    async def collect_strategy_depth(self) -> int:
        """Refresh watched-state orderbooks every minute."""
        selected = self._selected_market_rows()
        watched = self._watched_alpha_symbols()
        targets = [
            row for row in selected
            if str(row.get("source_symbol") or "").upper() in watched
        ]
        if not targets:
            return 0
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        depth_rows = []
        semaphore = asyncio.Semaphore(4)

        async def fetch_one(item):
            symbol = item["source_symbol"]
            async with semaphore:
                try:
                    depth = await self._get_data(
                        FULL_DEPTH,
                        {"symbol": symbol, "limit": 20},
                    )
                    bids = depth.get("bids") or []
                    asks = depth.get("asks") or []
                    bid_depth = sum(
                        _f(price) * _f(qty)
                        for price, qty in bids[:20]
                    )
                    ask_depth = sum(
                        _f(price) * _f(qty)
                        for price, qty in asks[:20]
                    )
                    top_bid = _f(bids[0][0]) if bids else 0
                    top_ask = _f(asks[0][0]) if asks else 0
                    spread_pct = (
                        (top_ask - top_bid) / top_bid * 100
                        if top_bid > 0 and top_ask > 0
                        else 0
                    )
                    depth_rows.append(
                        (
                            now_utc,
                            symbol,
                            bid_depth,
                            ask_depth,
                            bid_depth / ask_depth if ask_depth > 0 else 0,
                            max(0.0, spread_pct),
                            _f(bids[0][1]) if bids else 0,
                            _f(asks[0][1]) if asks else 0,
                        )
                    )
                except Exception as exc:
                    logger.debug(
                        "watched alpha depth failed %s: %s",
                        symbol,
                        exc,
                    )

        await asyncio.gather(*(fetch_one(item) for item in targets))
        if depth_rows:
            insert_alpha_orderbook_snapshot(depth_rows)
        return len(depth_rows)

    async def collect_closed_15m(self, *, force=False) -> dict:
        """Check for new closed 15m Alpha/Futures bars near each boundary."""
        if self.unified_candles:
            refresh_universe_readiness(
                "alpha",
                futures_source_env=self.market_env,
            )
            return {
                "alpha": 0,
                "futures": 0,
                "skipped": True,
                "source": "unified_1m",
            }
        now = datetime.now(timezone.utc)
        selected = self._selected_market_rows()
        if not selected:
            return {"alpha": 0, "futures": 0, "skipped": False}
        if (
            not force
            and now.minute % 15 > 2
            and futures_candles_current(
                "BTCUSDT",
                max_age_minutes=30,
                source_env=self.market_env,
                table="futures_candles_15m",
                closed_only=True,
            )
        ):
            return {"alpha": 0, "futures": 0, "skipped": True}
        alpha_rows = []
        futures_rows = []
        semaphore = asyncio.Semaphore(4)

        async def fetch_one(item):
            alpha_symbol = item["source_symbol"]
            futures_symbol = item["futures_symbol"]
            async with semaphore:
                try:
                    alpha_data, futures_response = await asyncio.gather(
                        self._get_data(
                            KLINES,
                            {
                                "symbol": alpha_symbol,
                                "interval": "15m",
                                "limit": 8,
                            },
                        ),
                        self.client.get(
                            self.futures_base + "/fapi/v1/klines",
                            params={
                                "symbol": futures_symbol,
                                "interval": "15m",
                                "limit": 8,
                            },
                        ),
                    )
                    futures_response.raise_for_status()
                    alpha_rows.extend(
                        self.normalize_kline_row(
                            alpha_symbol,
                            row,
                            market_env="mainnet",
                        )
                        for row in alpha_data or []
                    )
                    futures_rows.extend(
                        self.normalize_kline_row(
                            futures_symbol,
                            row,
                            market_env=self.market_env,
                        )
                        for row in futures_response.json()
                    )
                except Exception as exc:
                    logger.warning(
                        "closed 15m refresh failed %s/%s: %s",
                        alpha_symbol,
                        futures_symbol,
                        exc,
                    )

        for index in range(0, len(selected), 10):
            await asyncio.gather(
                *(fetch_one(item) for item in selected[index:index + 10])
            )
            await asyncio.sleep(0.2)
        selected_futures = {
            str(item.get("futures_symbol") or "").upper()
            for item in selected
        }
        if "BTCUSDT" not in selected_futures:
            try:
                btc_response = await self.client.get(
                    self.futures_base + "/fapi/v1/klines",
                    params={"symbol": "BTCUSDT", "interval": "15m", "limit": 8},
                )
                btc_response.raise_for_status()
                futures_rows.extend(
                    self.normalize_kline_row(
                        "BTCUSDT",
                        row,
                        market_env=self.market_env,
                    )
                    for row in btc_response.json()
                )
            except Exception as exc:
                logger.warning("closed 15m BTC context refresh failed: %s", exc)
        if alpha_rows:
            insert_alpha_candles("alpha_candles_15m", alpha_rows)
        if futures_rows:
            insert_futures_candles("futures_candles_15m", futures_rows)
        refresh_universe_readiness(
            "alpha",
            futures_source_env=self.market_env,
        )
        return {
            "alpha": len(alpha_rows),
            "futures": len(futures_rows),
            "skipped": False,
        }

    async def collect_strategy_fast_data(self) -> dict:
        """One-minute strategy feed: watched depth plus closed-bar checks."""
        depth_count, candles = await asyncio.gather(
            self.collect_strategy_depth(),
            self.collect_closed_15m(),
        )
        return {"depth": depth_count, **candles}

    async def collect_derivatives(self) -> int:
        """Refresh OI, funding and mark price without waiting for candle fetch."""
        selected = self._selected_market_rows()
        if not selected:
            return 0
        await self.collect_mapped_futures_data(
            selected,
            include_candles=False,
        )
        return len(selected)

    async def collect_all(self, universe_limit=200, market_top_n=80):
        try:
            universe = await self.refresh_universe(limit=universe_limit)
        except Exception as exc:
            logger.warning(
                "Alpha universe refresh failed; using persisted universe: %s",
                exc,
            )
            await self.reset_client()
            universe = [
                {
                    "alpha_symbol": row["source_symbol"],
                    "futures_symbol": row["futures_symbol"],
                    "volume_24h": row["spot_quote_volume_24h"],
                    "futures_quote_volume_24h": (
                        row["futures_quote_volume_24h"]
                    ),
                }
                for row in fetch_market_universe(
                    "alpha",
                    selected_only=True,
                )
                if row["source_symbol"] and row["futures_symbol"]
            ]
            if not universe:
                raise
        await self.collect_market_data(universe, top_n=market_top_n)
        return universe
