"""Lightweight per-symbol market phase classification."""

from __future__ import annotations

from typing import Any, Mapping


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _sync_confirmed(futures: Mapping[str, Any], alpha: Mapping[str, Any]) -> bool:
    dual = (alpha or {}).get("dual_market_volume") or {}
    if dual.get("synchronized") or dual.get("sync_confirmed"):
        return True
    oi = _num((futures or {}).get("oi_change_pct") or (futures or {}).get("oi_change"))
    futures_growth = _num(dual.get("futures_volume_ratio_6h"), 1.0)
    return oi >= 0 and futures_growth >= 1.2


def detect_market_phase(
    symbol: str,
    technical: Mapping[str, Any] | None,
    futures: Mapping[str, Any] | None = None,
    alpha: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    tech = technical or {}
    fut = futures or {}
    alpha_ctx = alpha or {}

    price = _num(tech.get("current_price") or tech.get("price") or tech.get("market_price"))
    ema20 = _num(tech.get("ema20"))
    ema_ratio = _num(tech.get("ema20_50_ratio"), 1.0)
    ema_slope = _num(tech.get("ema20_slope"))
    trend_score = _num(tech.get("trend_score"), 50.0)
    sync_ok = _sync_confirmed(fut, alpha_ctx)
    dual = alpha_ctx.get("dual_market_volume") or {}
    alpha_volume = _num(
        dual.get("alpha_spot_volume_ratio_6h")
        or dual.get("alpha_volume_growth_6h")
        or (alpha_ctx.get("volume") or {}).get("alpha_volume_growth_6h"),
        1.0,
    )
    futures_volume = _num(dual.get("futures_volume_ratio_6h"), 1.0)

    if price <= 0 or ema20 <= 0:
        return {
            "phase": "uncertain",
            "confidence": 20,
            "direction": None,
            "position_style": "skip",
            "allow_roll": False,
            "exit_style": "hold",
            "reason": f"{symbol} data insufficient",
        }

    if price < ema20 and ema_slope < 0 and (trend_score < 50 or ema_ratio < 0.998):
        return {
            "phase": "breakdown_risk",
            "confidence": 75,
            "direction": "long_risk",
            "position_style": "avoid",
            "allow_roll": False,
            "exit_style": "tighten",
            "reason": "price below EMA20 with negative slope",
        }

    if (
        alpha_volume >= 1.8
        and not sync_ok
        and trend_score >= 60
        and ema_slope > 0
        and price >= ema20 * 0.995
    ):
        return {
            "phase": "breakout_pending",
            "confidence": 65,
            "direction": "long",
            "position_style": "probe",
            "allow_roll": False,
            "exit_style": "observe",
            "reason": "spot alpha volume expanded but futures confirmation is missing",
        }

    if 0.996 <= ema_ratio <= 1.004 and abs(ema_slope) <= 0.15 and 45 <= trend_score <= 65:
        return {
            "phase": "range",
            "confidence": 70,
            "direction": "neutral",
            "position_style": "range",
            "allow_roll": False,
            "exit_style": "partial_profit",
            "reason": "EMA20/EMA50 are flat and close together",
        }

    if (
        ema_ratio >= 1.004
        and ema_slope > 0
        and price >= ema20
        and trend_score >= 65
        and (sync_ok or futures_volume >= 1.2)
    ):
        return {
            "phase": "trend_up",
            "confidence": 80,
            "direction": "long",
            "position_style": "trend",
            "allow_roll": True,
            "exit_style": "trail",
            "reason": "EMA trend is up and market confirmation is healthy",
        }

    return {
        "phase": "uncertain",
        "confidence": 40,
        "direction": None,
        "position_style": "reduced",
        "allow_roll": False,
        "exit_style": "hold",
        "reason": "mixed or weak market structure",
    }
