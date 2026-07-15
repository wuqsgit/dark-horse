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


def _spread_position_factor(spread_pct, soft_spread_pct, hard_spread_pct):
    """Convert alpha-side spread into a sizing multiplier instead of a hard gate."""
    spread = max(0.0, _num(spread_pct, 99.0))
    if spread <= soft_spread_pct:
        return 1.0
    if spread <= hard_spread_pct:
        span = max(hard_spread_pct - soft_spread_pct, 1e-9)
        return 1.0 - ((spread - soft_spread_pct) / span) * 0.35
    if spread <= 1.0:
        span = max(1.0 - hard_spread_pct, 1e-9)
        return 0.65 - ((spread - hard_spread_pct) / span) * 0.30
    return max(0.10, 0.35 / max(spread, 1.0))


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

    soft_spread_pct = 0.12
    hard_spread_pct = 0.35
    spread_degraded = spread_pct > soft_spread_pct
    spread_position_factor = _spread_position_factor(spread_pct, soft_spread_pct, hard_spread_pct)
    metrics["spread_degraded"] = spread_degraded
    metrics["soft_spread_pct"] = soft_spread_pct
    metrics["hard_spread_pct"] = hard_spread_pct
    metrics["spread_position_factor"] = round(spread_position_factor, 4)

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

    pre_breakout_volume_sync = (
        alpha_volume_growth_6h >= 2.0
        and futures_volume_growth_6h >= 1.5
        and futures_sync_score >= 65
        and 60 <= trend_score < 68
        and -3 <= ret_15m <= 3
        and -5 <= ret_1h <= 5
        and -8 <= ret_6h <= 8
    )
    metrics["pre_breakout_volume_sync"] = pre_breakout_volume_sync
    if pre_breakout_volume_sync:
        base_position_factor = 2.0
        position_factor = base_position_factor * spread_position_factor
        metrics["base_position_factor"] = base_position_factor
        reasons = [
            f"pre-breakout volume sync: alpha volume {alpha_volume_growth_6h:.1f}x, futures volume {futures_volume_growth_6h:.1f}x",
            f"futures sync {futures_sync_score:.0f}, trend {trend_score:.1f}",
            f"double normal position adjusted by spread to {position_factor:.2f}x",
        ]
        if spread_degraded:
            reasons.append(f"alpha spread {spread_pct:.3f}% sizes position factor {spread_position_factor:.2f}")
        return _state(
            "alpha_pre_breakout_volume_sync",
            "normal_review",
            allow_long=True,
            max_position_factor=position_factor,
            reasons=reasons,
            metrics=metrics,
        )

    # Opening an Alpha long requires both markets and futures positioning to
    # confirm the move. Discovery score must never compensate for weak OI.
    min_trend_score = 72.0
    min_alpha_volume = 1.8
    min_futures_volume = 1.5
    if trend_score < min_trend_score:
        return _state(
            "alpha_entry_confirmation_missing",
            "observe",
            reasons=[f"trend score {trend_score:.1f} < {min_trend_score:.0f}"],
            metrics=metrics,
        )
    if alpha_volume_growth_6h < min_alpha_volume or futures_volume_growth_6h < min_futures_volume:
        return _state(
            "alpha_entry_confirmation_missing",
            "observe",
            reasons=[
                f"dual volume not confirmed: alpha {alpha_volume_growth_6h:.1f}x/{min_alpha_volume:.1f}x, "
                f"futures {futures_volume_growth_6h:.1f}x/{min_futures_volume:.1f}x"
            ],
            metrics=metrics,
        )

    oi_confirmed = oi_change_4h >= 0 and oi_change_24h >= -0.01
    oi_waiver = (
        -0.005 <= oi_change_4h < 0
        and alpha_volume_growth_6h >= 3.0
        and futures_volume_growth_6h >= 2.0
    )
    metrics["oi_confirmed"] = oi_confirmed
    metrics["oi_volume_waiver"] = oi_waiver
    if not oi_confirmed and not oi_waiver:
        return _state(
            "alpha_oi_not_confirmed",
            "observe",
            reasons=[
                f"OI not confirmed: 4h {oi_change_4h:.2%}, 24h {oi_change_24h:.2%}; "
                f"waiver needs OI4h >= -0.50%, alpha volume 3.0x and futures volume 2.0x"
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
        and futures_volume_growth_6h >= min_futures_volume
        and (oi_confirmed or oi_waiver)
        and abs(funding_rate) <= 0.0012
    )
    futures_probe_ok = (
        bool(futures_sync.get("available"))
        and futures_volume_growth_6h >= min_futures_volume
        and (oi_confirmed or oi_waiver)
        and abs(funding_rate) <= 0.0015
    )
    degraded_reasons = []
    if spread_degraded:
        degraded_reasons.append(f"alpha spread {spread_pct:.3f}% sizes position factor {spread_position_factor:.2f}")
    if futures_probe_ok and not futures_ok:
        degraded_reasons.append(
            f"early futures sync: futures volume {futures_volume_growth_6h:.1f}x, OI4h {oi_change_4h:.2%}, OI24h {oi_change_24h:.2%}"
        )
    if trend_state in {"probe", "trend_candidate", "trend_confirmed"} and not futures_probe_ok:
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
        base_factor = 0.22 if not futures_ok else 0.35
        factor = base_factor * spread_position_factor
        return _state(
            "alpha_trend_confirmed" if futures_ok else "alpha_trend_confirmed_probe",
            "normal_review" if futures_ok else "normal_review_probe",
            allow_long=True,
            max_position_factor=factor,
            reasons=(alpha_trend.get("reasons") or []) + ["long-only alpha trend confirmed"] + degraded_reasons,
            metrics=metrics,
        )
    if trend_state == "trend_candidate":
        base_factor = 0.16 if not futures_ok else 0.25
        factor = base_factor * spread_position_factor
        return _state(
            "alpha_trend_candidate" if futures_ok else "alpha_trend_candidate_probe",
            "normal_review" if futures_ok else "normal_review_probe",
            allow_long=True,
            max_position_factor=factor,
            reasons=(alpha_trend.get("reasons") or []) + ["long-only alpha trend candidate"] + degraded_reasons,
            metrics=metrics,
        )
    if trend_state == "probe":
        factor = 0.12 * spread_position_factor
        return _state(
            "alpha_trend_probe",
            "normal_review_probe",
            allow_long=True,
            max_position_factor=factor,
            reasons=(alpha_trend.get("reasons") or []) + ["long-only alpha probe"] + degraded_reasons,
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
        and futures_probe_ok
    ):
        base_factor = 0.12 if not futures_ok else 0.20
        factor = base_factor * spread_position_factor
        return _state(
            "breakout_pullback" if futures_ok else "breakout_pullback_probe",
            "normal_review" if futures_ok else "normal_review_probe",
            allow_long=True,
            max_position_factor=factor,
            reasons=[f"breakout pullback: alpha volume {alpha_volume_growth_6h:.1f}x"] + (["futures sync confirmed"] if futures_ok else degraded_reasons),
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
        and futures_probe_ok
    ):
        base_factor = 0.08 if not futures_ok else 0.12
        factor = base_factor * spread_position_factor
        return _state(
            "accumulation_volume" if futures_ok else "accumulation_volume_probe",
            "normal_review_probe",
            allow_long=True,
            max_position_factor=factor,
            reasons=[f"warm accumulation: alpha volume {alpha_volume_growth_6h:.1f}x"] + (["futures sync confirmed"] if futures_ok else degraded_reasons),
            metrics=metrics,
        )

    return _state(
        "neutral",
        "observe",
        reasons=["alpha volume/price/futures sync has no long edge"],
        metrics=metrics,
    )
