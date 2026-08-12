"""Wide scenario projection for high-volatility Alpha strategy states."""
from __future__ import annotations

from typing import Any, Mapping


def _num(value: Any) -> float | None:
    try:
        if value is None:
            return None
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _pct(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _round_price(value: float) -> float:
    if value >= 1:
        return round(value, 4)
    if value >= 0.01:
        return round(value, 6)
    return round(value, 8)


def _level(label: str, price: float, role: str) -> dict[str, Any]:
    return {"label": label, "price": _round_price(price), "role": role}


def build_strategy_projection(
    features: Mapping[str, Any],
    *,
    setup_type: str | None = None,
) -> dict[str, Any]:
    """Build a wider, conditional playbook from the current market features.

    This is intentionally separate from entry approval. It expands the scenario
    map for volatile Alpha symbols so the monitor does not anchor on only the
    nearest breakout level.
    """
    current = _num(features.get("current_price"))
    if current is None:
        return {"status": "insufficient_data", "reason": "missing_current_price"}

    base_low = _num(features.get("base_low_2h"))
    base_high = _num(features.get("base_high_2h"))
    breakout = _num(features.get("breakout_level")) or base_high or current

    range_24h_pct = abs(_pct(features.get("range_24h_pct")))
    range_6h_pct = abs(_pct(features.get("range_6h_pct")))
    range_2h_pct = abs(_pct(features.get("range_2h_pct")))
    atr_15m_pct = abs(_pct(features.get("atr_15m_pct")))
    ret_1h = _pct(features.get("ret_1h"))
    ret_4h = _pct(features.get("ret_4h"))
    funding = _pct(features.get("funding_rate"))
    oi_change_1h = _pct(features.get("oi_change_1h"))
    taker_buy_ratio = _pct(features.get("taker_buy_quote_ratio"))
    depth_imbalance = _pct(features.get("depth_imbalance"))

    heat_score = 0
    reasons: list[str] = []
    if range_24h_pct >= 10 or range_6h_pct >= 6:
        heat_score += 2
        reasons.append("wide_intraday_range")
    if ret_1h >= 2 or ret_4h >= 4:
        heat_score += 1
        reasons.append("fresh_momentum")
    if taker_buy_ratio >= 0.52:
        heat_score += 1
        reasons.append("taker_buying_active")
    if funding >= 0.0002:
        heat_score += 1
        reasons.append("perp_premium_expanding")
    if depth_imbalance >= 0.15:
        heat_score += 1
        reasons.append("bid_depth_heavier")
    if oi_change_1h < 0 and ret_1h > 0:
        heat_score += 1
        reasons.append("short_covering_signature")

    imagination_pct = max(
        8.0,
        range_2h_pct * 2.0,
        range_6h_pct * 1.6,
        range_24h_pct * 1.15,
        atr_15m_pct * 12.0,
    )
    if heat_score >= 4:
        imagination_pct = max(imagination_pct, 24.0)
    elif heat_score >= 2:
        imagination_pct = max(imagination_pct, 16.0)
    if str(setup_type or "").lower() == "sentiment_reversal":
        imagination_pct = max(imagination_pct, 18.0)
    imagination_pct = min(imagination_pct, 60.0)

    acceptance = max(breakout, current * 1.018)
    first_extension = max(
        acceptance * 1.025,
        current * (1 + max(4.0, imagination_pct * 0.30) / 100),
    )
    squeeze_target = max(
        first_extension * 1.035,
        current * (1 + max(8.0, imagination_pct * 0.62) / 100),
    )
    extreme_target = max(
        squeeze_target * 1.045,
        current * (1 + imagination_pct / 100),
    )

    supports = []
    if base_high:
        supports.append(_level("retest_acceptance", base_high, "support"))
    supports.append(_level("momentum_line", current * 0.985, "support"))
    if base_low:
        supports.append(_level("structure_low", base_low, "support"))
    invalidation = (base_low * 0.99) if base_low else current * 0.94

    return {
        "status": "ready",
        "profile": "wide_alpha_squeeze" if heat_score >= 3 else "balanced_alpha_path",
        "wide_bias": heat_score >= 2,
        "imagination_pct": round(imagination_pct, 2),
        "heat_score": heat_score,
        "reasons": reasons,
        "targets": [
            _level("acceptance", acceptance, "trigger"),
            _level("first_extension", first_extension, "target"),
            _level("squeeze_target", squeeze_target, "target"),
            _level("extreme_wick", extreme_target, "stretch"),
        ],
        "supports": supports,
        "failure": {
            "below": _round_price(invalidation),
            "reason": "structure fails below the 2h base low or momentum line",
        },
        "tempo": [
            {
                "window": "0-30m",
                "condition": "hold above acceptance with taker buy ratio not fading",
            },
            {
                "window": "30-90m",
                "condition": "push through first_extension before funding cools",
            },
            {
                "window": "2-6h",
                "condition": "squeeze_target/extreme_wick only if shorts keep covering",
            },
        ],
    }
