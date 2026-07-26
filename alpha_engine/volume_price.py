"""Simple long-only Alpha volume-price entry signal."""
from __future__ import annotations


def _num(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _pct(value):
    return _num(value)


def _spread_position_factor(spread_pct, soft_spread_pct, hard_spread_pct):
    spread = max(0.0, _num(spread_pct, 99.0))
    if spread <= soft_spread_pct:
        return 1.0
    span = max(hard_spread_pct - soft_spread_pct, 1e-9)
    return max(0.50, 1.0 - ((spread - soft_spread_pct) / span) * 0.50)


def _state(
    state,
    action,
    *,
    allow_long=False,
    allow_short=False,
    max_position_factor=0.0,
    cooldown_minutes=0,
    reasons=None,
    metrics=None,
):
    return {
        "state": state,
        "action": action,
        "allow_long": bool(allow_long),
        "allow_short": bool(allow_short),
        "max_position_factor": max(0.0, min(2.0, _num(max_position_factor))),
        "cooldown_minutes": int(cooldown_minutes or 0),
        "reasons": list(reasons or []),
        "metrics": dict(metrics or {}),
    }


def evaluate_alpha_volume_price(raw_features, market_price=0):
    """Evaluate one simple Alpha impulse signal.

    Discovery score and symbol eligibility are checked by Trader. This gate only
    needs dual-market volume, basic price confirmation, and four hard risks.
    """
    raw = raw_features or {}
    returns = raw.get("returns") or {}
    volume = raw.get("volume") or {}
    depth = raw.get("depth") or {}
    risk = raw.get("risk") or {}
    futures_sync = raw.get("futures_sync") or {}
    alpha_trend = raw.get("alpha_trend") or {}

    ret_15m = _pct(returns.get("ret_15m"))
    ret_1h = _pct(returns.get("ret_1h"))
    ret_6h = _pct(returns.get("ret_6h"))
    pct_24h = _pct(returns.get("pct_24h"))
    alpha_volume_growth_6h = _num(
        volume.get("alpha_volume_growth_6h", volume.get("volume_growth_6h")),
        1.0,
    )
    futures_volume_growth_6h = _num(
        futures_sync.get("futures_volume_growth_6h"),
        1.0,
    )
    oi_change_4h = _num(futures_sync.get("oi_change_4h"))
    oi_change_24h = _num(futures_sync.get("oi_change_24h"))
    funding_rate = _num(futures_sync.get("funding_rate"))
    spread_pct = _num(depth.get("spread_pct"), 99.0)
    imbalance = _num(depth.get("imbalance"), 1.0)
    bid_depth = _num(depth.get("bid_depth"))
    ask_depth = _num(depth.get("ask_depth"))
    trend_score = _num(alpha_trend.get("trend_continuation_score"))
    trend_state = str(alpha_trend.get("trend_state") or "")
    volume_regime = str(alpha_trend.get("volume_regime") or "neutral")

    soft_spread_pct = 0.12
    hard_spread_pct = 0.35
    spread_degraded = spread_pct > soft_spread_pct
    spread_position_factor = _spread_position_factor(
        spread_pct,
        soft_spread_pct,
        hard_spread_pct,
    )
    metrics = {
        "ret_15m": round(ret_15m, 4),
        "ret_1h": round(ret_1h, 4),
        "ret_6h": round(ret_6h, 4),
        "pct_24h": round(pct_24h, 4),
        "volume_growth_6h": round(alpha_volume_growth_6h, 4),
        "alpha_volume_growth_6h": round(alpha_volume_growth_6h, 4),
        "futures_volume_growth_6h": round(futures_volume_growth_6h, 4),
        "oi_change_4h": round(oi_change_4h, 6),
        "oi_change_24h": round(oi_change_24h, 6),
        "funding_rate": round(funding_rate, 8),
        "trend_score": round(trend_score, 2),
        "trend_state": trend_state,
        "volume_regime": volume_regime,
        "spread_pct": round(spread_pct, 6),
        "imbalance": round(imbalance, 4),
        "bid_depth": round(bid_depth, 4),
        "ask_depth": round(ask_depth, 4),
        "range_24h_pct": round(_pct(risk.get("range_24h_pct")), 4),
        "pullback_from_high_pct": round(_pct(risk.get("pullback_from_high_pct")), 4),
        "market_price": _num(market_price),
        "spread_degraded": spread_degraded,
        "soft_spread_pct": soft_spread_pct,
        "hard_spread_pct": hard_spread_pct,
        "spread_position_factor": round(spread_position_factor, 4),
    }

    if not raw or not returns or not volume:
        return _state(
            "insufficient_data",
            "observe",
            reasons=["alpha volume data insufficient"],
            metrics=metrics,
        )

    if spread_pct >= hard_spread_pct:
        return _state(
            "spread_too_wide",
            "observe",
            reasons=[f"alpha spread {spread_pct:.3f}% >= hard limit {hard_spread_pct:.2f}%"],
            metrics=metrics,
        )

    if ret_15m > 8 or ret_1h > 15 or ret_6h > 30:
        reasons = []
        if ret_15m > 8:
            reasons.append(f"15m return {ret_15m:.1f}% overheated")
        if ret_1h > 15:
            reasons.append(f"1h return {ret_1h:.1f}% overheated")
        if ret_6h > 30:
            reasons.append(f"6h return {ret_6h:.1f}% overheated")
        return _state(
            "overheated_chase",
            "cooldown",
            cooldown_minutes=60,
            reasons=reasons,
            metrics=metrics,
        )

    if oi_change_24h <= -0.05:
        return _state(
            "alpha_oi_collapse",
            "observe",
            reasons=[f"OI 24h {oi_change_24h:.2%} <= -5.00% hard limit for alpha long"],
            metrics=metrics,
        )

    conditions = {
        "futures_available": bool(futures_sync.get("available")),
        "alpha_volume_impulse": alpha_volume_growth_6h >= 3.5,
        "futures_volume_expanded": futures_volume_growth_6h >= 1.5,
        "price_confirmed_15m": ret_15m >= -1.0,
        "price_confirmed_1h": ret_1h >= -2.0,
    }
    metrics["entry_conditions"] = conditions
    if all(conditions.values()):
        reasons = [
            f"alpha volume {alpha_volume_growth_6h:.1f}x >= 3.5x",
            f"futures volume {futures_volume_growth_6h:.1f}x >= 1.5x",
            f"price confirmed: 15m {ret_15m:.2f}%, 1h {ret_1h:.2f}%",
            f"position adjusted by spread to {spread_position_factor:.2f}x",
        ]
        return _state(
            "alpha_volume_impulse_entry",
            "normal_review",
            allow_long=True,
            max_position_factor=spread_position_factor,
            reasons=reasons,
            metrics=metrics,
        )

    missing = [name for name, passed in conditions.items() if not passed]
    return _state(
        "alpha_entry_conditions_missing",
        "observe",
        reasons=[
            "simple alpha entry missing: " + ", ".join(missing),
            f"alpha volume {alpha_volume_growth_6h:.1f}x, futures volume {futures_volume_growth_6h:.1f}x",
            f"15m {ret_15m:.2f}%, 1h {ret_1h:.2f}%",
        ],
        metrics=metrics,
    )
