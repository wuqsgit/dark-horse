"""Direction-specific scoring helpers shared by scoring and trading."""
from __future__ import annotations

from typing import Any, Mapping


def _num(value: Any, fallback: float = 0.0) -> float:
    try:
        return float(value if value is not None else fallback)
    except (TypeError, ValueError):
        return fallback


def compute_short_entry_alpha(
    technical: Mapping[str, Any] | None,
    futures: Mapping[str, Any] | None,
    depth: Mapping[str, Any] | None,
    phase: Mapping[str, Any] | None,
    relative_strength: float = 50.0,
) -> float:
    """Score whether the current snapshot offers a practical short entry."""
    tech = technical or {}
    fut = futures or {}
    book = depth or {}
    market_phase = str((phase or {}).get("phase") or "neutral").lower()
    trend = str(tech.get("trend_direction") or "")
    position = str(tech.get("price_position") or "")
    chip_phase = str(tech.get("chip_phase") or "")
    ret_6h = _num(tech.get("return_6h"))
    ret_24h = _num(
        tech.get("return_24h")
        if tech.get("return_24h") is not None
        else tech.get("price_change_24h")
    )
    rsi = _num(tech.get("rsi_14"), 50)
    volume_change = _num(tech.get("volume_change_pct"))
    funding = _num(fut.get("funding_rate"))
    oi_change = _num(
        fut.get("oi_change_pct")
        if fut.get("oi_change_pct") is not None
        else fut.get("oi_change")
    )
    depth_ratio = _num(book.get("depth_ratio"), 1.0)
    rs = _num(relative_strength, 50)

    score = 50.0
    if trend == "向下":
        score += 18
    elif trend == "向上":
        score -= 16

    if market_phase == "distribution" or chip_phase in {"疑似出货", "筹码松动"}:
        score += 12
    elif market_phase in {"breakout", "accumulation"}:
        score -= 8

    if "高位" in position or "偏高" in position:
        score += 8
    elif "低位" in position or "偏低" in position:
        score -= 8

    if rs <= 25:
        score += 15
    elif rs <= 40:
        score += 10
    elif rs <= 55:
        score += 4
    elif rs >= 75:
        score -= 12
    elif rs >= 65:
        score -= 6

    if ret_6h < 0 and ret_24h < 0:
        score += 10
    elif ret_6h > 0 and ret_24h > 0:
        score -= 10

    if funding > 0.001:
        score += 8
    elif funding < -0.003:
        score -= 18
    elif funding < -0.001:
        score -= 8

    if ret_6h < 0 and oi_change > 0:
        score += 8
    elif ret_6h < 0 and oi_change < -0.03:
        score -= 5

    if depth_ratio <= 0.85:
        score += 8
    elif depth_ratio >= 1.25:
        score -= 8

    if volume_change >= 1.5:
        score += 7
    elif volume_change <= 0:
        score -= 3

    # Do not reward a technically correct short when the move is already exhausted.
    if rsi < 25:
        score -= 12
    if ret_6h <= -0.08:
        score -= 10

    return round(max(5.0, min(95.0, score)), 1)
