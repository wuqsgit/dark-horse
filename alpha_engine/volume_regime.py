"""Long-only Alpha volume trend regime helpers."""
from __future__ import annotations


def _num(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def clamp(value, lo=0.0, hi=100.0):
    return max(lo, min(hi, float(value)))


def classify_alpha_volume_regime(raw_features: dict) -> dict:
    raw = raw_features or {}
    returns = raw.get("returns") or {}
    volume = raw.get("volume") or {}
    depth = raw.get("depth") or {}
    risk = raw.get("risk") or {}
    vg = _num(volume.get("volume_growth_6h"), 1.0)
    ret_15m = _num(returns.get("ret_15m"))
    ret_1h = _num(returns.get("ret_1h"))
    ret_6h = _num(returns.get("ret_6h"))
    spread = _num(depth.get("spread_pct"), 99.0)
    bid = _num(depth.get("bid_depth"))
    ask = _num(depth.get("ask_depth"))
    imbalance = _num(depth.get("imbalance"), 1.0)
    pullback = _num(risk.get("pullback_from_high_pct"))
    reasons = []

    sell_pressure = ask > 0 and bid > 0 and ask / max(bid, 1e-9) > 1.35
    if spread > 0.12:
        reasons.append(f"spread {spread:.3f}% > 0.12%")
        return {"regime": "suspicious", "volume_growth_6h": vg, "reasons": reasons, "is_chase_risk": False, "is_tradeable_impulse": False}
    if sell_pressure:
        reasons.append("ask depth pressure too high")
        return {"regime": "suspicious", "volume_growth_6h": vg, "reasons": reasons, "is_chase_risk": False, "is_tradeable_impulse": False}
    if (ret_15m > 8 or ret_1h > 15 or ret_6h > 30) or 8 <= vg < 15:
        if ret_15m > 8:
            reasons.append(f"15m return {ret_15m:.1f}% overheated")
        if ret_1h > 15:
            reasons.append(f"1h return {ret_1h:.1f}% overheated")
        if ret_6h > 30:
            reasons.append(f"6h return {ret_6h:.1f}% overheated")
        if vg >= 8:
            reasons.append(f"alpha volume {vg:.1f}x overheated")
        return {"regime": "overheated", "volume_growth_6h": vg, "reasons": reasons, "is_chase_risk": True, "is_tradeable_impulse": False}
    if vg >= 15:
        reasons.append(f"alpha volume {vg:.1f}x extreme")
        return {"regime": "extreme", "volume_growth_6h": vg, "reasons": reasons, "is_chase_risk": True, "is_tradeable_impulse": False}
    if 3.5 <= vg < 8:
        reasons.append(f"alpha impulse volume {vg:.1f}x")
        return {"regime": "impulse", "volume_growth_6h": vg, "reasons": reasons, "is_chase_risk": False, "is_tradeable_impulse": True}
    if 1.8 <= vg < 3.5:
        reasons.append(f"alpha warmup volume {vg:.1f}x")
        if pullback > 18 or imbalance < 0.75:
            reasons.append("price/orderbook hold not ready")
        return {"regime": "warmup", "volume_growth_6h": vg, "reasons": reasons, "is_chase_risk": False, "is_tradeable_impulse": True}
    reasons.append(f"alpha volume {vg:.1f}x neutral")
    return {"regime": "neutral", "volume_growth_6h": vg, "reasons": reasons, "is_chase_risk": False, "is_tradeable_impulse": False}


def compute_price_hold_score(raw_features: dict) -> float:
    raw = raw_features or {}
    returns = raw.get("returns") or {}
    volume = raw.get("volume") or {}
    depth = raw.get("depth") or {}
    risk = raw.get("risk") or {}
    score = 50.0
    ret_1h = _num(returns.get("ret_1h"))
    ret_6h = _num(returns.get("ret_6h"))
    pullback = _num(risk.get("pullback_from_high_pct"))
    range_24h = _num(risk.get("range_24h_pct"))
    vg = _num(volume.get("volume_growth_6h"), 1.0)
    bid = _num(depth.get("bid_depth"))
    ask = _num(depth.get("ask_depth"))

    if ret_1h > 0 and ret_6h > 0:
        score += 15
    if pullback <= 6:
        score += 10
    elif pullback > 15:
        score -= 15
    if range_24h <= 35:
        score += 10
    else:
        score -= 10
    if bid > 0 and ask > 0 and bid >= ask * 0.85:
        score += 10
    elif ask > 0:
        score -= 10
    if vg > 8:
        score -= 15
    if vg > 15:
        score -= 25
    return round(clamp(score), 2)


def compute_pullback_quality_score(raw_features: dict) -> float:
    raw = raw_features or {}
    returns = raw.get("returns") or {}
    volume = raw.get("volume") or {}
    depth = raw.get("depth") or {}
    risk = raw.get("risk") or {}
    score = 50.0
    ret_1h = _num(returns.get("ret_1h"))
    ret_6h = _num(returns.get("ret_6h"))
    pullback = _num(risk.get("pullback_from_high_pct"))
    vg = _num(volume.get("volume_growth_6h"), 1.0)
    bid = _num(depth.get("bid_depth"))
    ask = _num(depth.get("ask_depth"))
    if ret_6h > 0 and pullback <= 8:
        score += 15
    if 1.8 <= vg <= 6:
        score += 10
    if bid > 0 and ask > 0 and bid >= ask * 0.85:
        score += 10
    if ret_1h < -5:
        score -= 15
    if pullback > 15:
        score -= 20
    return round(clamp(score), 2)


def compute_trend_continuation(raw_features: dict) -> dict:
    raw = raw_features or {}
    regime = classify_alpha_volume_regime(raw)
    futures_sync = raw.get("futures_sync") or {}
    price_hold = compute_price_hold_score(raw)
    pullback_quality = compute_pullback_quality_score(raw)
    depth = raw.get("depth") or {}
    spread = _num(depth.get("spread_pct"), 99.0)
    bid = _num(depth.get("bid_depth"))
    ask = _num(depth.get("ask_depth"))
    vg = _num((raw.get("volume") or {}).get("volume_growth_6h"), 1.0)
    volume_regime_score = {
        "neutral": 35,
        "warmup": 62,
        "impulse": 76,
        "overheated": 35,
        "extreme": 20,
        "suspicious": 20,
    }.get(regime["regime"], 35)
    orderbook_score = 50.0
    if spread <= 0.08:
        orderbook_score += 15
    if bid > 0 and ask > 0 and bid >= ask * 0.85:
        orderbook_score += 15
    if ask > 0 and bid > 0 and ask / max(bid, 1e-9) > 1.35:
        orderbook_score -= 25
    overheat_score = 80.0
    if vg > 8:
        overheat_score -= 35
    if vg > 15:
        overheat_score -= 35
    sync_score = _num(futures_sync.get("sync_score"), 35.0)
    score = (
        volume_regime_score * 0.20
        + price_hold * 0.20
        + sync_score * 0.20
        + pullback_quality * 0.15
        + clamp(orderbook_score) * 0.15
        + clamp(overheat_score) * 0.10
    )
    score = round(clamp(score), 2)
    if score >= 82:
        state = "trend_confirmed"
    elif score >= 72:
        state = "trend_candidate"
    elif score >= 62:
        state = "probe"
    elif score >= 50:
        state = "watch"
    else:
        state = "observe"
    reasons = list(regime.get("reasons") or [])
    reasons.append(f"trend score {score:.1f}")
    return {
        "volume_regime": regime["regime"],
        "volume_regime_score": volume_regime_score,
        "price_hold_score": price_hold,
        "pullback_quality_score": pullback_quality,
        "orderbook_score": round(clamp(orderbook_score), 2),
        "overheat_score": round(clamp(overheat_score), 2),
        "trend_continuation_score": score,
        "trend_state": state,
        "reasons": reasons,
    }
