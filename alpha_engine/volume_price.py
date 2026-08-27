"""Strict long-only Alpha explosive-move precursor signal."""
from __future__ import annotations

from alpha_engine.square_sentiment import evaluate_square_reversal


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
    event_type=None,
    initial_position_factor=None,
    max_total_position_factor=None,
):
    result = {
        "state": state,
        "action": action,
        "allow_long": bool(allow_long),
        "allow_short": bool(allow_short),
        "max_position_factor": max(0.0, min(2.0, _num(max_position_factor))),
        "cooldown_minutes": int(cooldown_minutes or 0),
        "reasons": list(reasons or []),
        "metrics": dict(metrics or {}),
    }
    if event_type:
        result["event_type"] = str(event_type)
    if initial_position_factor is not None:
        result["initial_position_factor"] = _num(initial_position_factor)
    if max_total_position_factor is not None:
        result["max_total_position_factor"] = _num(max_total_position_factor)
    return result


def evaluate_alpha_volume_price(raw_features, market_price=0, alpha_score=0):
    """Evaluate the precursor conditions for an explosive Alpha entry.

    The closed-candle breakout and next-bar hold are checked by Trader immediately
    before planning the order. Volume alone only creates a watch state here.
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
    sync_score = _num(futures_sync.get("sync_score"))
    oi_change_4h = _num(futures_sync.get("oi_change_4h"))
    oi_change_24h = _num(futures_sync.get("oi_change_24h"))
    oi_24h_available = (
        "oi_change_24h" in futures_sync
        and futures_sync.get("oi_change_24h") is not None
    )
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
        "sync_score": round(sync_score, 2),
        "alpha_score": round(_num(alpha_score), 2),
        "oi_change_4h": round(oi_change_4h, 6),
        "oi_change_24h": round(oi_change_24h, 6),
        "oi_24h_available": oi_24h_available,
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

    square_reversal = evaluate_square_reversal(
        raw,
        alpha_score=alpha_score,
    )
    metrics["square_reversal"] = square_reversal.get("metrics") or {}

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

    if square_reversal.get("candidate"):
        return _state(
            "alpha_square_sentiment_reversal",
            "normal_review_probe",
            allow_long=True,
            max_position_factor=min(
                0.5,
                spread_position_factor,
            ),
            reasons=square_reversal.get("reasons"),
            metrics=metrics,
        )

    oi_expanded = (
        oi_change_4h >= 0.01 and oi_change_24h > 0
        if oi_24h_available
        else oi_change_4h >= 0.02
    )
    conditions = {
        "alpha_score_80": _num(alpha_score) >= 80.0,
        "futures_available": bool(futures_sync.get("available")),
        "alpha_volume_impulse": alpha_volume_growth_6h >= 3.5,
        "futures_volume_expanded": futures_volume_growth_6h >= 1.5,
        "markets_synchronized": sync_score >= 75.0,
        "price_strong_15m": ret_15m >= 0.5,
        "price_strong_1h": ret_1h >= 2.0,
        "price_strong_6h": ret_6h > 0,
        "oi_expanded": oi_expanded,
    }
    metrics["entry_conditions"] = conditions
    if all(conditions.values()):
        reasons = [
            f"alpha score {_num(alpha_score):.1f} >= 80",
            f"alpha volume {alpha_volume_growth_6h:.1f}x >= 3.5x",
            f"futures volume {futures_volume_growth_6h:.1f}x >= 1.5x",
            f"sync score {sync_score:.1f} >= 75",
            f"price strengthened: 15m {ret_15m:.2f}%, 1h {ret_1h:.2f}%, 6h {ret_6h:.2f}%",
            f"OI expanded: 4h {oi_change_4h:.2%}, 24h {oi_change_24h:.2%}",
            "waiting for breakout bar and next closed 15m hold",
            f"position adjusted by spread to {spread_position_factor:.2f}x",
        ]
        return _state(
            "explosive_breakout_pending",
            "normal_review",
            allow_long=True,
            max_position_factor=spread_position_factor,
            reasons=reasons,
            metrics=metrics,
            event_type="explosive_breakout",
            initial_position_factor=1.0,
            max_total_position_factor=2.0,
        )

    missing = [name for name, passed in conditions.items() if not passed]
    volume_watch = all(
        conditions[name]
        for name in (
            "alpha_score_80",
            "futures_available",
            "alpha_volume_impulse",
            "futures_volume_expanded",
        )
    )
    return _state(
        "explosive_volume_watch" if volume_watch else "alpha_entry_conditions_missing",
        "observe",
        reasons=[
            "strict explosive entry missing: " + ", ".join(missing),
            f"alpha volume {alpha_volume_growth_6h:.1f}x, futures volume {futures_volume_growth_6h:.1f}x",
            f"15m {ret_15m:.2f}%, 1h {ret_1h:.2f}%, 6h {ret_6h:.2f}%",
            f"OI 4h {oi_change_4h:.2%}, OI 24h {oi_change_24h:.2%}, sync {sync_score:.1f}",
        ],
        metrics=metrics,
    )
