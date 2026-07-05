import json
import math
import uuid
from datetime import datetime, timezone

from alpha_engine.profiles import (
    classify_alpha_profile,
    weighted_alpha_score,
)
from alpha_engine.volume_regime import clamp, compute_trend_continuation
from alpha_engine.volume_price import evaluate_alpha_volume_price
from shared.db import (
    fetch_active_alpha_symbols,
    fetch_alpha_candles,
    fetch_alpha_orderbook_depth,
    fetch_futures,
    fetch_klines_1h,
    insert_alpha_scan_scores,
)


def clamp(value, lo=0.0, hi=100.0):
    return max(lo, min(hi, float(value)))


def pct_change(start, end):
    if not start:
        return 0.0
    return (end - start) / start * 100


def grade(score):
    if score >= 80:
        return "S"
    if score >= 70:
        return "A"
    if score >= 60:
        return "B"
    if score >= 50:
        return "C"
    return "D"


class AlphaScoringEngine:
    def __init__(self):
        self.scan_id = "alpha-" + uuid.uuid4().hex[:12]

    def _series(self, rows):
        return sorted(rows, key=lambda r: r["time"])

    def _compute_futures_sync(self, futures_symbol, futures_1h, futures_rows):
        if not futures_symbol:
            return {
                "available": False,
                "futures_volume_growth_6h": 1.0,
                "oi_change_4h": 0.0,
                "oi_change_24h": 0.0,
                "funding_rate": 0.0,
                "mark_price": 0.0,
                "sync_score": 20.0,
                "reason": "missing futures_symbol",
            }
        c1h = self._series(futures_1h)
        frows = self._series(futures_rows)
        quote_vol_6h = sum(float(r["quote_vol"] or 0) for r in c1h[-6:]) if c1h else 0
        quote_vol_prev_6h = sum(float(r["quote_vol"] or 0) for r in c1h[-12:-6]) if len(c1h) >= 12 else 0
        vol_growth = quote_vol_6h / quote_vol_prev_6h if quote_vol_prev_6h > 0 else 1.0
        latest = frows[-1] if frows else None
        oi_now = float(latest["open_interest"] or 0) if latest else 0.0

        def oi_change(hours_back):
            if not frows or oi_now <= 0:
                return 0.0
            idx = max(0, len(frows) - hours_back - 1)
            old = float(frows[idx]["open_interest"] or 0)
            return (oi_now - old) / old if old > 0 else 0.0

        oi_4h = oi_change(4)
        oi_24h = oi_change(24)
        funding = float(latest["funding_rate"] or 0) if latest else 0.0
        mark_price = float(latest["mark_price"] or 0) if latest else 0.0
        score = 50.0
        if vol_growth >= 1.3:
            score += 15
        if vol_growth >= 2.0:
            score += 10
        if oi_4h >= 0.03:
            score += 15
        if oi_24h >= 0.06:
            score += 10
        if abs(funding) > 0.0012:
            score -= 15
        if vol_growth < 1.0 and oi_4h <= 0:
            score -= 20
        return {
            "available": bool(c1h or frows),
            "futures_volume_growth_6h": round(vol_growth, 4),
            "futures_quote_vol_6h": round(quote_vol_6h, 2),
            "futures_quote_vol_prev_6h": round(quote_vol_prev_6h, 2),
            "oi_change_4h": round(oi_4h, 6),
            "oi_change_24h": round(oi_24h, 6),
            "funding_rate": round(funding, 8),
            "mark_price": mark_price,
            "sync_score": round(clamp(score), 2),
        }

    def _score_symbol(self, symbol_row, candles_1h, candles_15m, candles_24h, depth_rows, futures_1h=None, futures_rows=None):
        c1h = self._series(candles_1h)
        c15m = self._series(candles_15m)
        c24h = self._series(candles_24h)
        latest = c1h[-1] if c1h else None
        price = float(latest["close"] if latest else symbol_row["price"] or 0)
        volume_24h = float(symbol_row["volume_24h"] or 0)
        liquidity = float(symbol_row["liquidity"] or 0)
        pct_24h = float(symbol_row["percent_change_24h"] or 0)

        ret_1h = pct_change(c1h[-2]["close"], c1h[-1]["close"]) if len(c1h) >= 2 else 0
        ret_6h = pct_change(c1h[-7]["close"], c1h[-1]["close"]) if len(c1h) >= 7 else 0
        ret_15m = pct_change(c15m[-2]["close"], c15m[-1]["close"]) if len(c15m) >= 2 else 0
        quote_vol_6h = sum(float(r["quote_vol"] or 0) for r in c1h[-6:]) if c1h else 0
        quote_vol_prev_6h = sum(float(r["quote_vol"] or 0) for r in c1h[-12:-6]) if len(c1h) >= 12 else 0
        volume_growth = quote_vol_6h / quote_vol_prev_6h if quote_vol_prev_6h > 0 else 1.0

        high_24 = max((float(r["high"] or 0) for r in c1h[-24:]), default=price)
        low_24 = min((float(r["low"] or 0) for r in c1h[-24:]), default=price)
        range_24 = (high_24 - low_24) / price * 100 if price else 0
        dist_from_high = (high_24 - price) / price * 100 if price else 0
        pullback_from_high = max(0, dist_from_high)

        latest_depth = depth_rows[0] if depth_rows else None
        spread_pct = float(latest_depth["spread_pct"] or 0) if latest_depth else 99
        imbalance = float(latest_depth["imbalance_ratio"] or 1) if latest_depth else 1
        bid_depth = float(latest_depth["bid_depth"] or 0) if latest_depth else 0
        ask_depth = float(latest_depth["ask_depth"] or 0) if latest_depth else 0

        discovery_score = clamp(
            35
            + min(25, math.log10(max(volume_24h, 1)) * 5)
            + min(20, max(volume_growth - 1, 0) * 15)
            + (10 if pullback_from_high > 2 else 0)
            + (10 if bool(symbol_row["futures_symbol"]) else 0)
        )

        momentum_score = clamp(
            50
            + ret_1h * 3
            + ret_6h * 1.5
            + ret_15m * 2
            - max(0, pct_24h - 40) * 1.2
            - max(0, -ret_1h) * 2
        )

        liquidity_score = clamp(
            20
            + min(35, math.log10(max(volume_24h, 1)) * 6)
            + min(20, math.log10(max(liquidity, 1)) * 4)
            + min(15, math.log10(max(bid_depth + ask_depth, 1)) * 4)
            - max(0, spread_pct - 0.3) * 35
        )

        risk_score = clamp(
            85
            - max(0, abs(pct_24h) - 25) * 1.0
            - max(0, range_24 - 35) * 0.8
            - max(0, spread_pct - 0.5) * 45
            - (10 if volume_24h < 100_000 else 0)
            - (10 if bid_depth <= 0 or ask_depth <= 0 else 0)
        )

        tradeability_map = {
            "alpha_futures_mapped": 95,
            "alpha_tradeable": 65,
            "alpha_only": 35,
            "inactive": 0,
        }
        tradeability_score = tradeability_map.get(symbol_row["tradeability"], 20)

        raw = {
            "returns": {"ret_15m": ret_15m, "ret_1h": ret_1h, "ret_6h": ret_6h, "pct_24h": pct_24h},
            "volume": {
                "volume_24h": volume_24h,
                "liquidity": liquidity,
                "volume_growth_6h": volume_growth,
                "alpha_volume_growth_6h": volume_growth,
                "alpha_quote_vol_6h": quote_vol_6h,
                "alpha_quote_vol_prev_6h": quote_vol_prev_6h,
            },
            "depth": {"spread_pct": spread_pct, "imbalance": imbalance, "bid_depth": bid_depth, "ask_depth": ask_depth},
            "risk": {"range_24h_pct": range_24, "pullback_from_high_pct": pullback_from_high},
            "tradeability": symbol_row["tradeability"],
            "futures_symbol": symbol_row["futures_symbol"],
        }
        raw["futures_sync"] = self._compute_futures_sync(
            symbol_row["futures_symbol"],
            futures_1h or [],
            futures_rows or [],
        )
        raw["alpha_trend"] = compute_trend_continuation(raw)
        raw["volume_price"] = evaluate_alpha_volume_price(raw, price)
        base_scores = {
            "discovery_score": discovery_score,
            "momentum_score": momentum_score,
            "liquidity_score": liquidity_score,
            "risk_score": risk_score,
            "tradeability_score": tradeability_score,
        }
        alpha_profile = classify_alpha_profile(base_scores, raw)
        # Alpha score is now only a discovery priority. It must not be treated
        # as an entry score; live entry is re-scored by the normal engine.
        alpha_score = clamp(weighted_alpha_score(base_scores, alpha_profile))
        discovery_only_thresholds = {
            "open_gate": "normal_trading_engine",
            "alpha_discovery_bonus_cap": 5,
            "entry_decision": "disabled",
        }

        return {
            "alpha_symbol": symbol_row["alpha_symbol"],
            "base_asset": symbol_row["base_asset"],
            "futures_symbol": symbol_row["futures_symbol"],
            "alpha_score": round(alpha_score, 2),
            "discovery_score": round(discovery_score, 2),
            "momentum_score": round(momentum_score, 2),
            "liquidity_score": round(liquidity_score, 2),
            "risk_score": round(risk_score, 2),
            "tradeability_score": round(tradeability_score, 2),
            "grade": grade(alpha_score),
            "decision": "DISCOVERY_ONLY",
            "market_price": price,
            "raw_features": raw,
            "alpha_profile": alpha_profile,
            "entry_level": "normal_gate",
            "suggested_position_pct": 0,
            "block_reasons": ["alpha_discovery_only_use_normal_entry_gate"],
            "profile_thresholds": discovery_only_thresholds,
        }

    def score_all(self, limit=200):
        symbols = fetch_active_alpha_symbols(limit=limit)
        alpha_symbols = [r["alpha_symbol"] for r in symbols]
        if not alpha_symbols:
            return []

        candles_1h = fetch_alpha_candles("alpha_candles_1h", alpha_symbols, hours=96)
        candles_15m = fetch_alpha_candles("alpha_candles_15m", alpha_symbols, hours=18)
        candles_24h = fetch_alpha_candles("alpha_candles_24h", alpha_symbols, days=40)
        futures_symbols = sorted({r["futures_symbol"] for r in symbols if r["futures_symbol"]})
        futures_1h = fetch_klines_1h(futures_symbols, hours=96) if futures_symbols else []
        futures_rows = fetch_futures(futures_symbols, hours=72) if futures_symbols else []
        by_1h = {}
        by_15m = {}
        by_24h = {}
        by_futures_1h = {}
        by_futures = {}
        for row in candles_1h:
            by_1h.setdefault(row["alpha_symbol"], []).append(row)
        for row in candles_15m:
            by_15m.setdefault(row["alpha_symbol"], []).append(row)
        for row in candles_24h:
            by_24h.setdefault(row["alpha_symbol"], []).append(row)
        for row in futures_1h:
            by_futures_1h.setdefault(row["symbol"], []).append(row)
        for row in futures_rows:
            by_futures.setdefault(row["symbol"], []).append(row)

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        results = []
        insert_rows = []
        for symbol_row in symbols:
            symbol = symbol_row["alpha_symbol"]
            if not by_1h.get(symbol):
                continue
            result = self._score_symbol(
                symbol_row,
                by_1h.get(symbol, []),
                by_15m.get(symbol, []),
                by_24h.get(symbol, []),
                fetch_alpha_orderbook_depth(symbol, hours=6),
                by_futures_1h.get(symbol_row["futures_symbol"], []),
                by_futures.get(symbol_row["futures_symbol"], []),
            )
            results.append(result)
            insert_rows.append((
                now,
                self.scan_id,
                result["alpha_symbol"],
                result["base_asset"],
                result["futures_symbol"],
                result["alpha_score"],
                result["discovery_score"],
                result["momentum_score"],
                result["liquidity_score"],
                result["risk_score"],
                result["tradeability_score"],
                result["grade"],
                result["decision"],
                result["market_price"],
                json.dumps(result["raw_features"], ensure_ascii=False),
                result["alpha_profile"],
                result["entry_level"],
                result["suggested_position_pct"],
                json.dumps(result["block_reasons"], ensure_ascii=False),
                json.dumps(result["profile_thresholds"], ensure_ascii=False),
            ))
        if insert_rows:
            insert_alpha_scan_scores(insert_rows)
        return sorted(results, key=lambda r: -r["alpha_score"])
