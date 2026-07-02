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
