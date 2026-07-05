"""Classify symbols into entry templates from the latest scan features."""
from __future__ import annotations

import json
from typing import Any


def _row_dict(row: Any) -> dict:
    if isinstance(row, dict):
        return row
    return dict(row)


def _raw_features(row: dict) -> dict:
    raw = row.get("raw_features") or row.get("features") or {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _has(text: str, *terms: str) -> bool:
    text = str(text or "").lower()
    return any(term.lower() in text for term in terms)


def _score_item(score: float, reasons: list[str], points: float, reason: str) -> float:
    if points > 0:
        reasons.append(reason)
    return score + points


def classify_symbol(row: Any, v3_signals: dict | None = None, side: str | None = None) -> dict:
    """Return the best entry profile for the symbol.

    The classifier is intentionally transparent and rule-based. It does not
    decide whether to open; it only chooses which template should evaluate the
    signal.
    """
    row = _row_dict(row)
    raw = _raw_features(row)
    tech = raw.get("technical") or {}
    fut = raw.get("futures") or {}
    depth = raw.get("depth") or {}
    breakout = (v3_signals or {}).get("breakout") or {}

    symbol = str(row.get("symbol") or "").upper()
    score = _num(row.get("composite_score"))
    entry_alpha = _num(row.get("entry_alpha") or raw.get("entry_alpha"))
    rs = _num(row.get("relative_strength"), 50)
    atr_ratio = _num(tech.get("atr_ratio"))
    ret_6h = _num(tech.get("return_6h"))
    ret_24h = _num(tech.get("price_change_24h") if tech.get("price_change_24h") is not None else tech.get("return_24h"))
    volume_change = _num(tech.get("volume_change_pct"))
    oi_change = _num(fut.get("oi_change_pct") if fut.get("oi_change_pct") is not None else fut.get("oi_change"))
    funding = _num(fut.get("funding_rate"))
    depth_ratio = _num(depth.get("depth_ratio"), 1.0)
    support_score = _num(tech.get("support_score"), 50)

    price_position = str(row.get("price_position") or tech.get("price_position") or "")
    trend_state = str(row.get("trend_state") or tech.get("trend_state") or "")
    trend_direction = str(row.get("trend_direction") or "")
    chip_phase = str(row.get("chip_phase") or tech.get("chip_phase") or "")
    volatility = str(row.get("volatility_level") or "")
    text = " ".join([price_position, trend_state, trend_direction, chip_phase, volatility])

    is_low = _has(text, "低位", "偏低", "low")
    is_high = _has(text, "高位", "偏高", "high", "overbought")
    is_up = _has(text, "向上", "上涨", "up")
    is_down = _has(text, "向下", "下跌", "down")
    accumulating = _has(text, "吸筹", "蓄力", "accumulation", "reaccumulation")
    distributing = _has(text, "出货", "派发", "distribution")
    high_vol = _has(text, "高波", "极高", "high volatility") or atr_ratio >= 0.06
    low_vol = _has(text, "低波", "收缩") or (0 < atr_ratio <= 0.025)
    breakout_ok = bool(breakout.get("ok"))
    bid_support = depth_ratio >= 0.9
    ask_pressure = depth_ratio <= 0.8
    pullback_core = (
        (ret_24h <= -0.03 or ret_6h <= -0.015)
        and not is_down
        and (support_score >= 55 or bid_support)
        and funding <= 0.001
    )

    candidates: dict[str, dict] = {
        "breakout": {"score": 0.0, "reasons": []},
        "accumulation": {"score": 0.0, "reasons": []},
        "pullback": {"score": 0.0, "reasons": []},
        "momentum": {"score": 0.0, "reasons": []},
        "short_breakdown": {"score": 0.0, "reasons": []},
        "weak_short": {"score": 0.0, "reasons": []},
    }

    if side == "SHORT" or distributing or is_down:
        c = candidates["short_breakdown"]
        c["score"] = _score_item(c["score"], c["reasons"], 28 if is_down else 0, "趋势偏空")
        c["score"] = _score_item(c["score"], c["reasons"], 22 if distributing else 0, "筹码有出货迹象")
        c["score"] = _score_item(c["score"], c["reasons"], 18 if ret_24h < -0.03 else 0, "24h 跌幅较大")
        c["score"] = _score_item(c["score"], c["reasons"], 14 if ask_pressure else 0, "盘口卖压更强")
        c["score"] = _score_item(c["score"], c["reasons"], 10 if oi_change < 0 else 0, "OI 走弱")

        c = candidates["weak_short"]
        c["score"] = _score_item(c["score"], c["reasons"], 22 if is_down else 0, "持续弱势")
        c["score"] = _score_item(c["score"], c["reasons"], 18 if is_high else 0, "位置偏高")
        c["score"] = _score_item(c["score"], c["reasons"], 16 if rs <= 45 else 0, "相对强度弱")
        c["score"] = _score_item(c["score"], c["reasons"], 12 if ret_6h < 0 and ret_24h < 0 else 0, "短中周期都偏弱")

    c = candidates["momentum"]
    c["score"] = _score_item(c["score"], c["reasons"], 24 if high_vol else 0, "高波动")
    c["score"] = _score_item(c["score"], c["reasons"], 22 if volume_change >= 1.5 else 0, "明显放量")
    c["score"] = _score_item(c["score"], c["reasons"], 18 if rs >= 75 else 0, "RS 较强")
    c["score"] = _score_item(c["score"], c["reasons"], 18 if entry_alpha >= 66 else 0, "Entry Alpha 高")
    c["score"] = _score_item(c["score"], c["reasons"], 12 if breakout_ok else 0, "已突破")

    c = candidates["breakout"]
    c["score"] = _score_item(c["score"], c["reasons"], 30 if breakout_ok else 0, "已突破前高")
    c["score"] = _score_item(c["score"], c["reasons"], 18 if volume_change > 0.5 else 0, "量能恢复")
    c["score"] = _score_item(c["score"], c["reasons"], 18 if is_up else 0, "趋势向上")
    c["score"] = _score_item(c["score"], c["reasons"], 16 if rs >= 65 else 0, "相对强度较强")
    c["score"] = _score_item(c["score"], c["reasons"], 10 if bid_support else 0, "盘口承接不差")

    c = candidates["pullback"]
    c["score"] = _score_item(c["score"], c["reasons"], 30 if pullback_core else 0, "回调承接核心条件")
    c["score"] = _score_item(c["score"], c["reasons"], 14 if ret_24h <= -0.04 else 0, "跌幅释放")
    c["score"] = _score_item(c["score"], c["reasons"], 10 if not is_down and rs >= 55 else 0, "不是明显弱势")
    c["score"] = _score_item(c["score"], c["reasons"], 16 if bid_support or support_score >= 55 else 0, "盘口/支撑有承接")
    c["score"] = _score_item(c["score"], c["reasons"], 8 if funding <= 0.001 else 0, "资金费率不过热")
    c["score"] = _score_item(c["score"], c["reasons"], 8 if is_low else 0, "接近低位/支撑")
    if not pullback_core:
        c["score"] = min(c["score"], 35.0)

    c = candidates["accumulation"]
    c["score"] = _score_item(c["score"], c["reasons"], 24 if is_low else 0, "价格处于低位")
    c["score"] = _score_item(c["score"], c["reasons"], 20 if accumulating else 0, "筹码处于吸筹/蓄力")
    c["score"] = _score_item(c["score"], c["reasons"], 16 if low_vol else 0, "波动收缩")
    c["score"] = _score_item(c["score"], c["reasons"], 14 if rs >= 60 else 0, "相对强度不弱")
    c["score"] = _score_item(c["score"], c["reasons"], 12 if oi_change >= -3 else 0, "OI 未明显恶化")
    c["score"] = _score_item(c["score"], c["reasons"], 10 if score >= 54 and entry_alpha >= 58 else 0, "基础评分达标")

    ranked = sorted(candidates.items(), key=lambda item: item[1]["score"], reverse=True)
    profile, best = ranked[0]
    if best["score"] < 30:
        profile = "accumulation" if is_low else "breakout"
        best = candidates[profile]

    confidence = max(0.35, min(0.95, best["score"] / 90.0))
    return {
        "symbol": symbol,
        "profile": profile,
        "profile_name": profile,
        "confidence": round(confidence, 2),
        "reason": "、".join(best["reasons"][:4]) or "没有明显类型特征，使用默认模板",
        "scores": {key: round(value["score"], 1) for key, value in candidates.items()},
        "features": {
            "rs": round(rs, 2),
            "atr_ratio": round(atr_ratio, 4),
            "return_24h": round(ret_24h, 4),
            "volume_change_pct": round(volume_change, 4),
            "oi_change": round(oi_change, 4),
            "funding_rate": round(funding, 6),
            "breakout_ok": breakout_ok,
            "support_score": round(support_score, 2),
            "pullback_core": pullback_core,
        },
    }
