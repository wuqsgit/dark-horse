"""Alpha long-only volume trend gate.

Alpha spot volume growth and futures volume growth are calculated separately.
The gate may use futures volume, open interest, and funding as confirmation,
but it never opens Alpha shorts.
"""


def _num(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _pct(value):
    return _num(value, 0.0)


def _state(
    state,
    action,
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
        "allow_short": False,
        "max_position_factor": float(max_position_factor or 0),
        "cooldown_minutes": int(cooldown_minutes or 0),
        "reasons": reasons or [],
        "metrics": metrics or {},
    }


def evaluate_alpha_volume_price(raw_features, market_price=0):
    """Return a long-only Alpha volume-price gate result."""
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
    alpha_volume_growth_6h = _num(volume.get("alpha_volume_growth_6h", volume.get("volume_growth_6h")), 1.0)
    futures_volume_growth_6h = _num(futures_sync.get("futures_volume_growth_6h"), 1.0)
    oi_change_4h = _num(futures_sync.get("oi_change_4h"))
    oi_change_24h = _num(futures_sync.get("oi_change_24h"))
    funding_rate = _num(futures_sync.get("funding_rate"))
    futures_sync_score = _num(futures_sync.get("sync_score"), 20.0)
    spread_pct = _num(depth.get("spread_pct"), 99.0)
    imbalance = _num(depth.get("imbalance"), 1.0)
    bid_depth = _num(depth.get("bid_depth"), 0.0)
    ask_depth = _num(depth.get("ask_depth"), 0.0)
    range_24h_pct = _pct(risk.get("range_24h_pct"))
    pullback_from_high_pct = _pct(risk.get("pullback_from_high_pct"))
    trend_score = _num(alpha_trend.get("trend_continuation_score"), 0.0)
    trend_state = str(alpha_trend.get("trend_state") or "observe")
    volume_regime = str(alpha_trend.get("volume_regime") or "neutral")

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
        "futures_sync_score": round(futures_sync_score, 2),
        "trend_score": round(trend_score, 2),
        "trend_state": trend_state,
        "volume_regime": volume_regime,
        "spread_pct": round(spread_pct, 6),
        "imbalance": round(imbalance, 4),
        "bid_depth": round(bid_depth, 4),
        "ask_depth": round(ask_depth, 4),
        "range_24h_pct": round(range_24h_pct, 4),
        "pullback_from_high_pct": round(pullback_from_high_pct, 4),
        "market_price": _num(market_price, 0.0),
    }

    if not raw or not returns or not volume:
        return _state("insufficient_data", "observe", reasons=["alpha volume data insufficient"], metrics=metrics)

    if spread_pct > 0.12:
        return _state("wide_spread", "observe", reasons=[f"alpha spread {spread_pct:.3f}% > 0.12%"], metrics=metrics)

    if (
        volume_regime in {"overheated", "extreme", "suspicious"}
        or ret_15m > 8
        or ret_1h > 15
        or ret_6h > 30
        or alpha_volume_growth_6h > 8
    ):
        reasons = list(alpha_trend.get("reasons") or [])
        if ret_15m > 8:
            reasons.append(f"15m return {ret_15m:.1f}% overheated")
        if ret_1h > 15:
            reasons.append(f"1h return {ret_1h:.1f}% overheated")
        if ret_6h > 30:
            reasons.append(f"6h return {ret_6h:.1f}% overheated")
        if alpha_volume_growth_6h > 8:
            reasons.append(f"alpha volume {alpha_volume_growth_6h:.1f}x overheated")
        return _state("overheated_chase", "cooldown", cooldown_minutes=60, reasons=reasons, metrics=metrics)

    if (ret_1h < -5 or ret_6h < -10) and alpha_volume_growth_6h > 1.5:
        return _state(
            "breakdown_volume_long_only",
            "observe",
            reasons=[
                f"breakdown volume: 1h {ret_1h:.1f}%, 6h {ret_6h:.1f}%, alpha volume {alpha_volume_growth_6h:.1f}x",
                "alpha is long-only; no short execution",
            ],
            metrics=metrics,
        )

    sell_pressure = ask_depth > 0 and bid_depth > 0 and ask_depth / max(bid_depth, 1e-9) > 1.35
    near_high = pullback_from_high_pct < 2
    if near_high and alpha_volume_growth_6h > 2 and ret_1h < 2 and (sell_pressure or imbalance < 0.85):
        return _state(
            "distribution_risk_long_only",
            "observe",
            reasons=[
                f"near high distribution risk: pullback {pullback_from_high_pct:.1f}%, 1h {ret_1h:.1f}%",
                "alpha is long-only; distribution risk is observe",
            ],
            metrics=metrics,
        )

    futures_ok = (
        bool(futures_sync.get("available"))
        and futures_volume_growth_6h >= 1.3
        and (oi_change_4h >= 0.03 or oi_change_24h >= 0.06)
        and abs(funding_rate) <= 0.0012
    )
    if trend_state in {"probe", "trend_candidate", "trend_confirmed"} and not futures_ok:
        return _state(
            "alpha_trend_watch_no_futures_sync",
            "observe",
            reasons=[
                f"alpha volume {alpha_volume_growth_6h:.1f}x but futures sync not confirmed",
                f"futures volume {futures_volume_growth_6h:.1f}x, OI4h {oi_change_4h:.2%}, OI24h {oi_change_24h:.2%}",
            ],
            metrics=metrics,
        )

    if trend_state == "trend_confirmed":
        return _state(
            "alpha_trend_confirmed",
            "normal_review",
            allow_long=True,
            max_position_factor=0.35,
            reasons=(alpha_trend.get("reasons") or []) + ["long-only alpha trend confirmed with futures sync"],
            metrics=metrics,
        )
    if trend_state == "trend_candidate":
        return _state(
            "alpha_trend_candidate",
            "normal_review",
            allow_long=True,
            max_position_factor=0.25,
            reasons=(alpha_trend.get("reasons") or []) + ["long-only alpha trend candidate with futures sync"],
            metrics=metrics,
        )
    if trend_state == "probe":
        return _state(
            "alpha_trend_probe",
            "normal_review_probe",
            allow_long=True,
            max_position_factor=0.12,
            reasons=(alpha_trend.get("reasons") or []) + ["long-only alpha probe with futures sync"],
            metrics=metrics,
        )

    if (
        6 <= ret_6h <= 18
        and 1.8 <= alpha_volume_growth_6h <= 4.5
        and 2 <= pullback_from_high_pct <= 6
        and -2 <= ret_15m <= 2.5
        and -2 <= ret_1h <= 5
        and range_24h_pct < 32
        and 1.0 <= imbalance <= 4.0
        and futures_ok
    ):
        return _state(
            "breakout_pullback",
            "normal_review",
            allow_long=True,
            max_position_factor=0.20,
            reasons=[f"breakout pullback: alpha volume {alpha_volume_growth_6h:.1f}x", "futures sync confirmed"],
            metrics=metrics,
        )

    if (
        1.8 <= alpha_volume_growth_6h <= 3.2
        and -1 <= ret_15m <= 3.5
        and -2 <= ret_1h <= 6
        and 2 <= ret_6h <= 14
        and 4 <= pullback_from_high_pct <= 12
        and range_24h_pct < 28
        and 0.8 <= imbalance <= 3.5
        and futures_ok
    ):
        return _state(
            "accumulation_volume",
            "normal_review_probe",
            allow_long=True,
            max_position_factor=0.12,
            reasons=[f"warm accumulation: alpha volume {alpha_volume_growth_6h:.1f}x", "futures sync confirmed"],
            metrics=metrics,
        )

    return _state(
        "neutral",
        "observe",
        reasons=["alpha volume/price/futures sync has no long edge"],
        metrics=metrics,
    )
