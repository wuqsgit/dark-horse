"""Candidate selection for live trading.

The selector deliberately avoids a tiny hand-picked universe.  It ranks the
latest scored symbols by tradability, freshness and opportunity quality, then
applies soft category diversification.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from shared.db import get_conn
from trader.config import TRADING_CONFIG
from trader.symbol_risk import get_symbol_risk

logger = logging.getLogger("selection")

DEFAULT_CATEGORY = "discovery"
PROFILE_PATH = Path(__file__).resolve().parent.parent / "strategies" / "token_profiles.json"


def _as_dict(row: Any) -> dict:
    if isinstance(row, dict):
        return row
    return dict(row)


def _base_symbol(symbol: str) -> str:
    sym = (symbol or "").upper()
    for suffix in ("USDT", "BUSD", "USD"):
        if sym.endswith(suffix):
            return sym[: -len(suffix)]
    return sym


def _raw_features(row: dict) -> dict:
    raw = row.get("raw_features") or row.get("features") or {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return {}


class CandidateSelector:
    """Rank symbols with dynamic discovery and category diversification."""

    CATEGORY_LIMITS = {
        "bluechip": 1,
        "fundamental": 2,
        "narrative": 2,
        "meme": 1,
        DEFAULT_CATEGORY: 2,
    }

    CATEGORY_ALIASES = {
        "钃濈": "bluechip",
        "蓝筹": "bluechip",
        "鍩烘湰闈?": "fundamental",
        "基本面": "fundamental",
        "鍙欎簨/搴勮偂": "narrative",
        "叙事/庄股": "narrative",
        "Meme/瓒呴珮椋庨櫓": "meme",
        "Meme": "meme",
    }

    def __init__(self):
        self.blacklist = self._load_blacklist()
        self.token_map = self._load_token_map()

    def _normalize_category(self, category: Any) -> str:
        raw = str(category or "")
        if raw in self.CATEGORY_ALIASES:
            return self.CATEGORY_ALIASES[raw]
        lowered = raw.lower()
        if "meme" in lowered:
            return "meme"
        if any(x in raw for x in ("蓝筹", "钃", "BTC", "ETH")):
            return "bluechip"
        if any(x in raw for x in ("基本", "鍩", "DeFi", "AI")):
            return "fundamental"
        if any(x in raw for x in ("叙事", "庄", "鍙", "GameFi")):
            return "narrative"
        return DEFAULT_CATEGORY

    def _load_token_map(self) -> dict[str, str]:
        if not PROFILE_PATH.exists():
            return {}
        try:
            with PROFILE_PATH.open(encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception as exc:
            logger.warning("[Selection] token profile load failed: %s", exc)
            return {}

        mapped = {}
        for symbol, category in (cfg.get("token_map") or {}).items():
            mapped[symbol.upper()] = self._normalize_category(category)
        return mapped

    def _load_blacklist(self) -> set[str]:
        conn = get_conn()
        try:
            rows = conn.execute(
                """
                SELECT symbol FROM trade_cooldown
                WHERE cooldown_until > datetime('now', 'utc')
                """
            ).fetchall()
            return {r["symbol"].upper() for r in rows}
        finally:
            conn.close()

    def _get_category(self, symbol: str) -> str:
        return self.token_map.get(_base_symbol(symbol), DEFAULT_CATEGORY)

    def _get_historical_win_rate(self, symbol: str) -> float:
        conn = get_conn()
        try:
            row = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins
                FROM trades
                WHERE symbol = ? AND created_at > datetime('now', '-30 days')
                """,
                (symbol,),
            ).fetchone()
            if row and row["total"] and row["total"] >= 5:
                return float(row["wins"] or 0) / float(row["total"]) * 100
            return 50.0
        finally:
            conn.close()

    def _get_historical_performance(self, symbol: str) -> dict:
        conn = get_conn()
        try:
            row = conn.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) AS wins,
                       SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) AS gross_win,
                       ABS(SUM(CASE WHEN pnl < 0 THEN pnl ELSE 0 END)) AS gross_loss,
                       SUM(pnl) AS total_pnl
                FROM trades
                WHERE symbol = ? AND created_at > datetime('now', '-7 days')
                  AND source IN ('system', 'income_auto')
                """,
                (symbol,),
            ).fetchone()
            total = int(row["total"] or 0) if row else 0
            wins = int(row["wins"] or 0) if row else 0
            gross_win = float(row["gross_win"] or 0) if row else 0.0
            gross_loss = float(row["gross_loss"] or 0) if row else 0.0
            total_pnl = float(row["total_pnl"] or 0) if row else 0.0
            win_rate = wins / total * 100 if total else 50.0
            profit_factor = gross_win / gross_loss if gross_loss > 0 else (3.0 if gross_win > 0 else 1.0)
            expectancy = total_pnl / total if total else 0.0
            return {
                "total": total,
                "win_rate": win_rate,
                "total_pnl": total_pnl,
                "profit_factor": profit_factor,
                "expectancy": expectancy,
            }
        finally:
            conn.close()

    def _liquidity_score(self, symbol: str) -> float:
        conn = get_conn()
        try:
            row = conn.execute(
                """
                SELECT AVG(quote_vol) AS avg_quote_vol
                FROM (
                    SELECT quote_vol
                    FROM candles_1h
                    WHERE symbol = ?
                    ORDER BY time DESC
                    LIMIT 24
                )
                """,
                (symbol,),
            ).fetchone()
            quote_vol = float(row["avg_quote_vol"] or 0) if row else 0.0
        finally:
            conn.close()

        if quote_vol >= 50_000_000:
            return 100.0
        if quote_vol >= 10_000_000:
            return 85.0
        if quote_vol >= 3_000_000:
            return 70.0
        if quote_vol >= 1_000_000:
            return 55.0
        if quote_vol >= 300_000:
            return 35.0
        return 0.0

    def _opportunity_score(self, row: dict) -> float:
        score = float(row.get("composite_score") or 0)
        strength = float(row.get("relative_strength") or 50)
        liquidity = self._liquidity_score(row["symbol"])
        raw = _raw_features(row)
        hist = raw.get("historical_performance") or self._get_historical_performance(row["symbol"])
        entry_alpha = float(row.get("entry_alpha") or raw.get("entry_alpha") or score)
        symbol_risk = get_symbol_risk(row["symbol"])
        risk_rank_factor = float(symbol_risk.get("max_position_factor") or 0.35)

        category = self._get_category(row["symbol"])
        discovery_bonus = 4.0 if category == DEFAULT_CATEGORY else 0.0
        if category == "meme":
            discovery_bonus -= 3.0

        history_adjust = 0.0
        if int(hist.get("total") or 0) >= 5:
            expectancy = float(hist.get("expectancy") or 0)
            total_pnl = float(hist.get("total_pnl") or 0)
            profit_factor = float(hist.get("profit_factor") or 1)
            if expectancy > 0 and total_pnl > 0 and profit_factor > 1.15:
                history_adjust += min(8.0, 2.0 + (profit_factor - 1.0) * 4.0)
            if expectancy <= 0 or total_pnl < 0 or profit_factor < 1.0:
                history_adjust -= min(24.0, 8.0 + abs(expectancy) * 5.0)

        vol = str(row.get("volatility_level") or "")
        pos = str(row.get("price_position") or "")
        risk_penalty = 0.0
        if "极高" in vol or "鏋侀珮" in vol:
            risk_penalty += 10.0
        elif "偏高" in vol or "鍋忛珮" in vol:
            risk_penalty += 4.0
        if "overbought" in pos.lower():
            risk_penalty += 12.0

        return round(
            score * 0.35
            + entry_alpha * 0.25
            + strength * 0.20
            + liquidity * 0.15
            + min(6.0, risk_rank_factor * 6.0)
            + history_adjust
            + discovery_bonus
            - risk_penalty,
            3,
        )

    def select_candidates(
        self,
        scored_symbols: list,
        current_positions: list,
        max_positions: int = 3,
    ) -> list:
        pos_symbols = {p["symbol"].upper() for p in current_positions}
        available = []
        for raw in scored_symbols:
            row = _as_dict(raw).copy()
            symbol = row.get("symbol", "").upper()
            if not symbol or symbol in pos_symbols or symbol in self.blacklist:
                continue
            row["selection_category"] = self._get_category(symbol)
            row["selection_score"] = self._opportunity_score(row)
            available.append(row)

        available.sort(key=lambda x: x["selection_score"], reverse=True)

        selected = []
        category_counts: dict[str, int] = {}
        for row in available:
            category = row["selection_category"]
            limit = self.CATEGORY_LIMITS.get(category, self.CATEGORY_LIMITS[DEFAULT_CATEGORY])
            if category_counts.get(category, 0) >= limit:
                continue
            selected.append(row)
            category_counts[category] = category_counts.get(category, 0) + 1
            if len(selected) >= max_positions:
                break

        if len(selected) < max_positions:
            selected_symbols = {r["symbol"] for r in selected}
            for row in available:
                if row["symbol"] in selected_symbols:
                    continue
                selected.append(row)
                if len(selected) >= max_positions:
                    break

        logger.info(
            "[Selection] %s candidates: %s",
            len(selected),
            [(s["symbol"], s["selection_category"], s["selection_score"]) for s in selected],
        )
        return selected[:max_positions]

    def can_open(self, symbol: str) -> tuple[bool, str]:
        symbol = symbol.upper()
        if symbol in self.blacklist:
            return False, "symbol is in cooldown"

        conn = get_conn()
        try:
            row = conn.execute(
                """
                SELECT created_at FROM trades
                WHERE symbol = ?
                ORDER BY created_at DESC LIMIT 1
                """,
                (symbol,),
            ).fetchone()
            if not row:
                return True, ""

            raw_time = str(row["created_at"]).replace("Z", "+00:00")
            last_trade = datetime.fromisoformat(raw_time)
            if last_trade.tzinfo is None:
                last_trade = last_trade.replace(tzinfo=timezone.utc)
            hours_since = (datetime.now(timezone.utc) - last_trade).total_seconds() / 3600
            if hours_since < 4:
                return False, f"recently traded {hours_since:.1f}h ago"
            return True, ""
        finally:
            conn.close()


def _num(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value if value is not None else fallback)
    except Exception:
        return fallback


class BluechipTrendSelector:
    """Independent trend lane for BTC/ETH/SOL style steady moves."""

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or (TRADING_CONFIG.get("bluechip_trend") or {})
        self.last_evaluations: list[dict] = []

    def _symbols(self) -> set[str]:
        return {str(x).upper() for x in self.cfg.get("symbols", [])}

    def _score(self, row: dict, raw: dict) -> tuple[float, dict]:
        tech = raw.get("technical") or {}
        fut = raw.get("futures") or {}
        depth = raw.get("depth") or {}
        score = _num(row.get("composite_score"))
        entry_alpha = _num(row.get("entry_alpha"))
        rs = _num(row.get("relative_strength"), 50)
        ret_24h = _num(tech.get("price_change_24h") if tech.get("price_change_24h") is not None else tech.get("return_24h"))
        ret_6h = _num(tech.get("return_6h"))
        ema_slope = _num(tech.get("ema20_slope"))
        ema_ratio = _num(tech.get("ema20_50_ratio"), 1.0)
        volume_change = _num(tech.get("volume_change_pct"))
        oi_change = _num(fut.get("oi_change_pct"))
        support_score = _num(tech.get("support_score"), 50)
        absorption_score = _num(tech.get("absorption_score"), 50)
        depth_score = _num(depth.get("depth_ratio_score"), 50)
        big_order_score = _num(depth.get("big_order_score"), 50)
        rsi = _num(tech.get("rsi_14"), 50)
        position_value = _num(tech.get("price_position_value"), 0.5)
        trend_score = _num(tech.get("trend_score"), 50)

        trend_component = min(100.0, max(0.0, 50 + ret_24h * 500 + max(0, ema_ratio - 1) * 1800 + max(0, ema_slope) * 6))
        contract_component = min(100.0, max(0.0, 50 + oi_change * 500 + (10 if _num(fut.get("oi_score"), 50) >= 65 else 0)))
        volume_component = min(100.0, max(0.0, 50 + volume_change * 45))
        support_component = max(support_score, absorption_score)
        depth_component = depth_score * 0.7 + big_order_score * 0.3
        overheat_penalty = 0.0
        if rsi > 72:
            overheat_penalty += min(18.0, (rsi - 72) * 1.4)
        if position_value > 0.88:
            overheat_penalty += min(14.0, (position_value - 0.88) * 80)

        bluechip_score = (
            trend_component * 0.25
            + contract_component * 0.20
            + volume_component * 0.20
            + support_component * 0.15
            + depth_component * 0.10
            + min(100.0, score + entry_alpha * 0.35 + rs * 0.20) * 0.10
            - overheat_penalty
        )
        metrics = {
            "bluechip_trend_score": round(bluechip_score, 2),
            "trend_component": round(trend_component, 2),
            "contract_component": round(contract_component, 2),
            "volume_component": round(volume_component, 2),
            "support_component": round(support_component, 2),
            "depth_component": round(depth_component, 2),
            "overheat_penalty": round(overheat_penalty, 2),
            "score": score,
            "entry_alpha": entry_alpha,
            "relative_strength": rs,
            "ret_24h": ret_24h,
            "ret_6h": ret_6h,
            "ema20_slope": ema_slope,
            "ema20_50_ratio": ema_ratio,
            "volume_change_pct": volume_change,
            "oi_change_pct": oi_change,
            "support_score": support_score,
            "absorption_score": absorption_score,
            "depth_ratio_score": depth_score,
            "big_order_score": big_order_score,
            "rsi_14": rsi,
            "price_position_value": position_value,
            "trend_score": trend_score,
        }
        return round(bluechip_score, 3), metrics

    def _evaluate(self, row: dict, current_symbols: set[str], bluechip_open: int) -> dict:
        raw = _raw_features(row)
        symbol = str(row.get("symbol") or "").upper()
        score, metrics = self._score(row, raw)
        cfg = self.cfg
        reasons = []
        if not cfg.get("enabled", True):
            reasons.append("bluechip trend lane disabled")
        if symbol not in self._symbols():
            reasons.append("not bluechip symbol")
        if symbol in current_symbols:
            reasons.append("already in position")
        if bluechip_open >= int(cfg.get("max_positions", 1)):
            reasons.append("bluechip trend slot already used")
        if metrics["score"] < float(cfg.get("min_score", 55)):
            reasons.append(f"score {metrics['score']:.1f} < {float(cfg.get('min_score', 55)):.1f}")
        if metrics["entry_alpha"] < float(cfg.get("min_entry_alpha", 45)):
            reasons.append(f"entry_alpha {metrics['entry_alpha']:.1f} < {float(cfg.get('min_entry_alpha', 45)):.1f}")
        if metrics["relative_strength"] < float(cfg.get("min_relative_strength", 50)):
            reasons.append(f"relative_strength {metrics['relative_strength']:.1f} < {float(cfg.get('min_relative_strength', 50)):.1f}")
        if metrics["ret_24h"] < float(cfg.get("min_return_24h", 0.025)):
            reasons.append(f"return_24h {metrics['ret_24h']:.2%} < {float(cfg.get('min_return_24h', 0.025)):.2%}")
        if metrics["ema20_50_ratio"] < float(cfg.get("min_ema20_50_ratio", 1.004)) or metrics["ema20_slope"] <= 0:
            reasons.append("EMA trend not confirmed")
        if metrics["support_score"] < float(cfg.get("min_support_score", 55)) and metrics["absorption_score"] < float(cfg.get("min_support_score", 55)):
            reasons.append("support/absorption not confirmed")
        if metrics["depth_ratio_score"] < float(cfg.get("min_depth_score", 35)):
            reasons.append(f"depth_score {metrics['depth_ratio_score']:.1f} too weak")
        if metrics["big_order_score"] < float(cfg.get("min_big_order_score", 40)):
            reasons.append(f"big_order_score {metrics['big_order_score']:.1f} too weak")
        if metrics["rsi_14"] > float(cfg.get("max_rsi", 82)):
            reasons.append(f"rsi {metrics['rsi_14']:.1f} > {float(cfg.get('max_rsi', 82)):.1f}")
        if metrics["price_position_value"] > float(cfg.get("max_price_position_value", 0.95)):
            reasons.append(f"price_position_value {metrics['price_position_value']:.2f} too high")
        funding = _num((raw.get("futures") or {}).get("funding_rate"))
        if abs(funding) > float(cfg.get("max_funding_rate", 0.001)):
            reasons.append(f"funding_rate {funding:.5f} too high")

        confirmed = (
            metrics["score"] >= float(cfg.get("confirmed_score", 60))
            and metrics["entry_alpha"] >= float(cfg.get("confirmed_entry_alpha", 50))
            and metrics["relative_strength"] >= float(cfg.get("confirmed_relative_strength", 58))
            and metrics["trend_score"] >= float(cfg.get("confirmed_trend_score", 68))
        )
        mode = "trend_confirmed" if confirmed else "probe"
        return {
            **row,
            "side": "LONG",
            "bluechip_trend_score": score,
            "bluechip_metrics": metrics,
            "bluechip_entry_mode": mode,
            "bluechip_size_factor": float(cfg.get("confirmed_size_factor" if confirmed else "probe_size_factor", 0.25)),
            "bluechip_reject_reason": "; ".join(reasons),
            "selection_category": "bluechip_trend",
            "selection_score": score,
        }

    def select_candidates(self, scored_symbols: list, current_positions: list, max_positions: int = 1) -> list[dict]:
        if not self.cfg.get("enabled", False) or max_positions <= 0:
            self.last_evaluations = []
            return []
        current_symbols = {str(p.get("symbol") or "").upper() for p in current_positions}
        bluechip_symbols = self._symbols()
        bluechip_open = sum(1 for sym in current_symbols if sym in bluechip_symbols)
        evaluations = []
        for raw in scored_symbols:
            row = _as_dict(raw).copy()
            if str(row.get("symbol") or "").upper() not in bluechip_symbols:
                continue
            evaluations.append(self._evaluate(row, current_symbols, bluechip_open))
        evaluations.sort(key=lambda x: x["bluechip_trend_score"], reverse=True)
        self.last_evaluations = evaluations
        selected = [x for x in evaluations if not x.get("bluechip_reject_reason")]
        return selected[: min(max_positions, int(self.cfg.get("max_positions", 1)))]


def get_candidate_rankings(scored_symbols: list) -> list:
    selector = CandidateSelector()
    rankings = []
    for raw in scored_symbols[:50]:
        row = _as_dict(raw)
        symbol = row["symbol"]
        hist = _raw_features(row).get("historical_performance") or selector._get_historical_performance(symbol)
        rankings.append(
            {
                "symbol": symbol,
                "category": selector._get_category(symbol),
                "score": row.get("composite_score", 0),
                "selection_score": selector._opportunity_score(row),
                "win_rate": hist.get("win_rate", 50),
                "expectancy": hist.get("expectancy", 0),
                "profit_factor": hist.get("profit_factor", 1),
                "blacklisted": symbol.upper() in selector.blacklist,
            }
        )
    return rankings
