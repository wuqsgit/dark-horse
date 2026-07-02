"""Alpha volume-price state gate.

Alpha discovery is noisy. This module decides whether a hot Alpha token has a
reasonable volume-price structure before it is allowed into the normal trading
entry gate.
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
        "allow_short": bool(allow_short),
        "max_position_factor": float(max_position_factor or 0),
        "cooldown_minutes": int(cooldown_minutes or 0),
        "reasons": reasons or [],
        "metrics": metrics or {},
    }


def evaluate_alpha_volume_price(raw_features, market_price=0):
    """Return an Alpha volume-price gate result.

    Input values from alpha_engine are in percent units for returns and spread.
    The result intentionally favors waiting over chasing.
    """
    raw = raw_features or {}
    returns = raw.get("returns") or {}
    volume = raw.get("volume") or {}
    depth = raw.get("depth") or {}
    risk = raw.get("risk") or {}

    ret_15m = _pct(returns.get("ret_15m"))
    ret_1h = _pct(returns.get("ret_1h"))
    ret_6h = _pct(returns.get("ret_6h"))
    pct_24h = _pct(returns.get("pct_24h"))
    volume_growth_6h = _num(volume.get("volume_growth_6h"), 1.0)
    spread_pct = _num(depth.get("spread_pct"), 99.0)
    imbalance = _num(depth.get("imbalance"), 1.0)
    bid_depth = _num(depth.get("bid_depth"), 0.0)
    ask_depth = _num(depth.get("ask_depth"), 0.0)
    range_24h_pct = _pct(risk.get("range_24h_pct"))
    pullback_from_high_pct = _pct(risk.get("pullback_from_high_pct"))

    metrics = {
        "ret_15m": round(ret_15m, 4),
        "ret_1h": round(ret_1h, 4),
        "ret_6h": round(ret_6h, 4),
        "pct_24h": round(pct_24h, 4),
        "volume_growth_6h": round(volume_growth_6h, 4),
        "spread_pct": round(spread_pct, 6),
        "imbalance": round(imbalance, 4),
        "bid_depth": round(bid_depth, 4),
        "ask_depth": round(ask_depth, 4),
        "range_24h_pct": round(range_24h_pct, 4),
        "pullback_from_high_pct": round(pullback_from_high_pct, 4),
        "market_price": _num(market_price, 0.0),
    }

    if not raw or not returns or not volume:
        return _state(
            "insufficient_data",
            "observe",
            reasons=["量价数据不足，Alpha 只观察，不进入实盘执行"],
            metrics=metrics,
        )

    if spread_pct > 0.12:
        return _state(
            "wide_spread",
            "observe",
            reasons=[f"盘口价差 {spread_pct:.3f}% > 0.12%，Alpha 不进实盘执行"],
            metrics=metrics,
        )

    if ret_15m > 8 or ret_1h > 15 or ret_6h > 30 or volume_growth_6h > 8:
        reasons = []
        if ret_15m > 8:
            reasons.append(f"15m 涨幅 {ret_15m:.1f}% 过热")
        if ret_1h > 15:
            reasons.append(f"1h 涨幅 {ret_1h:.1f}% 过热")
        if ret_6h > 30:
            reasons.append(f"6h 涨幅 {ret_6h:.1f}% 过热")
        if volume_growth_6h > 8:
            reasons.append(f"6h 成交放大 {volume_growth_6h:.1f}x 过热")
        return _state(
            "overheated_chase",
            "cooldown",
            cooldown_minutes=60,
            reasons=reasons,
            metrics=metrics,
        )

    if (ret_1h < -5 or ret_6h < -10) and volume_growth_6h > 1.5:
        return _state(
            "breakdown_volume",
            "short_review_only",
            allow_short=True,
            max_position_factor=0.25,
            reasons=[
                f"放量下跌：1h {ret_1h:.1f}%，6h {ret_6h:.1f}%，成交 {volume_growth_6h:.1f}x",
                "禁止做多，只允许 Alpha 做空执行",
            ],
            metrics=metrics,
        )

    near_high = pullback_from_high_pct < 2
    sell_pressure = ask_depth > 0 and bid_depth > 0 and (ask_depth / max(bid_depth, 1e-9)) > 1.25
    if near_high and volume_growth_6h > 2 and ret_1h < 2 and (sell_pressure or imbalance < 0.85):
        return _state(
            "distribution_risk",
            "short_review_only",
            allow_short=True,
            max_position_factor=0.20,
            reasons=[
                f"高位放量滞涨：距高点 {pullback_from_high_pct:.1f}%，1h 涨幅 {ret_1h:.1f}%",
                "盘口卖压偏重，禁止做多",
            ],
            metrics=metrics,
        )

    if (
        6 <= ret_6h <= 18
        and 1.8 <= volume_growth_6h <= 4.5
        and 2 <= pullback_from_high_pct <= 6
        and -2 <= ret_15m <= 2.5
        and -2 <= ret_1h <= 5
        and range_24h_pct < 32
        and 1.0 <= imbalance <= 4.0
    ):
        return _state(
            "breakout_pullback",
            "normal_review",
            allow_long=True,
            max_position_factor=0.40,
            reasons=[
                f"放量突破后回踩：6h 涨幅 {ret_6h:.1f}%，成交 {volume_growth_6h:.1f}x",
                f"距 24h 高点 {pullback_from_high_pct:.1f}%，未继续暴拉",
            ],
            metrics=metrics,
        )

    if (
        1.8 <= volume_growth_6h <= 3.2
        and -1 <= ret_15m <= 3.5
        and -2 <= ret_1h <= 6
        and 2 <= ret_6h <= 14
        and 4 <= pullback_from_high_pct <= 12
        and range_24h_pct < 28
        and 0.8 <= imbalance <= 3.5
    ):
        return _state(
            "accumulation_volume",
            "normal_review_probe",
            allow_long=True,
            max_position_factor=0.25,
            reasons=[
                f"低位温和放量：6h 成交 {volume_growth_6h:.1f}x",
                f"价格距 24h 高点 {pullback_from_high_pct:.1f}%，暂未追高",
            ],
            metrics=metrics,
        )

    return _state(
        "neutral",
        "observe",
        reasons=["量价结构没有明显优势，只观察，不进入实盘执行"],
        metrics=metrics,
    )
