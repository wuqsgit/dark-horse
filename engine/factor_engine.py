"""Layered factor scoring with explainable contributions."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCORING_PROFILE_PATH = os.path.join(ROOT_DIR, "configs", "scoring_profiles.json")
ENTRY_PROFILE_PATH = os.path.join(ROOT_DIR, "configs", "entry_profiles.json")

_CACHE = {"scoring_mtime": None, "scoring": None, "entry_mtime": None, "entry": None}


@dataclass
class FactorScore:
    name: str
    score: float | None
    weight: float
    reason: str = ""
    status: str = "ok"
    raw_value: Any = None


def _load_json(path: str, default: dict) -> dict:
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_scoring_profiles() -> dict:
    mtime = os.path.getmtime(SCORING_PROFILE_PATH) if os.path.exists(SCORING_PROFILE_PATH) else None
    if _CACHE["scoring_mtime"] == mtime and _CACHE["scoring"] is not None:
        return _CACHE["scoring"]
    data = _load_json(SCORING_PROFILE_PATH, {"default_profile": "breakout", "profiles": {}})
    _CACHE["scoring_mtime"] = mtime
    _CACHE["scoring"] = data
    return data


def load_entry_profiles() -> dict:
    mtime = os.path.getmtime(ENTRY_PROFILE_PATH) if os.path.exists(ENTRY_PROFILE_PATH) else None
    if _CACHE["entry_mtime"] == mtime and _CACHE["entry"] is not None:
        return _CACHE["entry"]
    data = _load_json(ENTRY_PROFILE_PATH, {"default_template": "breakout", "templates": {}, "symbols": {}})
    _CACHE["entry_mtime"] = mtime
    _CACHE["entry"] = data
    return data


def get_symbol_profile(symbol: str) -> str:
    data = load_entry_profiles()
    symbol_cfg = data.get("symbols", {}).get(str(symbol).upper(), {})
    return symbol_cfg.get("template") or data.get("default_template") or "breakout"


def _num(value: Any, default: float | None = None) -> float | None:
    if value is None:
        return default
    try:
        return float(value)
    except Exception:
        return default


def _clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, float(value)))


def _score_from_pct(value: float | None, scale: float = 0.05, midpoint: float = 50.0) -> float | None:
    if value is None:
        return None
    return _clamp(midpoint + (float(value) / scale) * 25.0)


def _risk_from_score(score: float | None) -> float | None:
    if score is None:
        return None
    return _clamp(100.0 - float(score))


def _factor_values(symbol: str, tech: dict, fut: dict, onchain: dict, depth: dict, historical: dict) -> dict[str, FactorScore]:
    volume_change = _num(tech.get("volume_change_pct"))
    funding = _num(fut.get("funding_rate"), 0.0)
    oi_change = _num(fut.get("oi_change_pct") if fut.get("oi_change_pct") is not None else fut.get("oi_change"))
    expectancy = _num(historical.get("expectancy"))
    profit_factor = _num(historical.get("profit_factor"))
    total = int(_num(historical.get("total"), 0) or 0)
    atr_ratio = _num(tech.get("atr_ratio"))
    price_position_value = _num(tech.get("price_position_value"))
    depth_ratio_score = _num(depth.get("depth_ratio_score"))
    big_order_score = _num(depth.get("big_order_score"))
    spread_score = _num(depth.get("spread_score"))
    robot = bool(depth.get("robot_signature"))
    relative_strength = _num(tech.get("relative_strength"))
    if relative_strength is None:
        relative_strength = _num(tech.get("market_strength_score"))

    if total >= 5 and expectancy is not None:
        hist_score = _clamp(50 + expectancy * 12 + ((profit_factor or 1.0) - 1.0) * 20)
    elif total >= 5 and profit_factor is not None:
        hist_score = _clamp(50 + (profit_factor - 1.0) * 25)
    else:
        hist_score = None

    high_pos_risk = _clamp((price_position_value or 0.5) * 100) if price_position_value is not None else None
    volatility_risk = _clamp(((atr_ratio or 0.02) / 0.08) * 100) if atr_ratio is not None else None
    funding_risk = _clamp(max(0.0, (funding or 0.0) - 0.0002) / 0.001 * 100) if funding is not None else None
    funding_contrarian = _clamp(55 + max(0.0, -float(funding or 0.0)) / 0.001 * 25) if funding is not None else None
    negative_expectancy_risk = _clamp(50 - (hist_score - 50) if hist_score is not None else 50) if total >= 5 else None
    oi_confirmation = _score_from_pct(oi_change, scale=0.06) if oi_change is not None else None
    oi_divergence_risk = _clamp(55 - (oi_change or 0.0) * 350) if oi_change is not None else None

    breakout_proxy = _clamp(
        (_num(tech.get("trend_score"), 50) or 50) * 0.45
        + (_num(tech.get("vol_quality_score"), 50) or 50) * 0.25
        + (_score_from_pct(volume_change, scale=0.8) or 50) * 0.30
    )
    rr_proxy = _clamp(
        (_num(tech.get("support_score"), 50) or 50) * 0.5
        + (100 - (high_pos_risk or 50)) * 0.3
        + (_num(tech.get("atr_normalized_score"), 50) or 50) * 0.2
    )
    spread_score = spread_score if spread_score is not None else 50.0
    spread_risk = 100.0 - spread_score
    liquidity_score = _score_from_pct(volume_change, scale=1.0) if volume_change is not None else None

    values = {
        "chip_score": FactorScore("chip_score", _num(tech.get("chip_score")), 0, "筹码结构"),
        "support_score": FactorScore("support_score", _num(tech.get("support_score")), 0, "支撑质量"),
        "absorption_score": FactorScore("absorption_score", _num(tech.get("absorption_score"), _num(tech.get("abs_score"))), 0, "承接/吸筹"),
        "price_position_score": FactorScore("price_position_score", _num(tech.get("position_score")), 0, "价格位置"),
        "relative_strength": FactorScore("relative_strength", relative_strength, 0, "横截面强度"),
        "historical_expectancy_score": FactorScore("historical_expectancy_score", hist_score, 0, "历史期望"),
        "volume_growth_score": FactorScore("volume_growth_score", _score_from_pct(volume_change, scale=0.8), 0, "成交量变化", raw_value=volume_change),
        "trend_score": FactorScore("trend_score", _num(tech.get("trend_score")), 0, "趋势方向"),
        "oi_confirmation_score": FactorScore("oi_confirmation_score", oi_confirmation, 0, "OI 配合", raw_value=oi_change),
        "liquidity_score": FactorScore("liquidity_score", liquidity_score, 0, "流动性/活跃度"),
        "volatility_compression_score": FactorScore("volatility_compression_score", _num(tech.get("vol_quality_score")), 0, "波动收敛"),
        "rr_proxy_score": FactorScore("rr_proxy_score", rr_proxy, 0, "结构 R:R 代理"),
        "breakout_proxy_score": FactorScore("breakout_proxy_score", breakout_proxy, 0, "突破代理"),
        "orderbook_depth_score": FactorScore("orderbook_depth_score", depth_ratio_score, 0, "盘口深度"),
        "depth_ratio_score": FactorScore("depth_ratio_score", depth_ratio_score, 0, "买卖盘深度"),
        "big_order_score": FactorScore("big_order_score", big_order_score, 0, "大单支持"),
        "spread_score": FactorScore("spread_score", spread_score, 0, "价差"),
        "robot_penalty_score": FactorScore("robot_penalty_score", 0.0 if robot else 100.0, 0, "机器盘口惩罚"),
        "funding_contrarian_score": FactorScore("funding_contrarian_score", funding_contrarian, 0, "资金费率反向机会"),
        "high_position_risk": FactorScore("high_position_risk", high_pos_risk, 0, "高位风险"),
        "funding_overheat_risk": FactorScore("funding_overheat_risk", funding_risk, 0, "资金费率过热"),
        "volatility_risk": FactorScore("volatility_risk", volatility_risk, 0, "波动风险"),
        "negative_expectancy_risk": FactorScore("negative_expectancy_risk", negative_expectancy_risk, 0, "负期望风险"),
        "spread_risk": FactorScore("spread_risk", spread_risk, 0, "价差风险"),
        "oi_divergence_risk": FactorScore("oi_divergence_risk", oi_divergence_risk, 0, "OI 背离风险"),
    }
    for item in values.values():
        if item.score is None:
            item.status = "missing"
            item.reason = f"{item.reason}缺失"
        else:
            item.score = round(_clamp(item.score), 1)
    return values


def weighted_score(factors: list[FactorScore], neutral_when_empty: float = 50.0) -> dict:
    ok = [f for f in factors if f.status == "ok" and f.score is not None and f.weight > 0]
    missing = [f.name for f in factors if f.status != "ok" or f.score is None]
    total_weight = sum(f.weight for f in ok)
    if total_weight <= 0:
        return {"score": neutral_when_empty, "available_weight": 0.0, "missing_factors": missing, "contributors": []}
    contributors = []
    score = 0.0
    for f in ok:
        normalized_weight = f.weight / total_weight
        contribution = f.score * normalized_weight
        score += contribution
        contributors.append({
            "name": f.name,
            "score": round(f.score, 1),
            "weight": round(normalized_weight, 4),
            "contribution": round(contribution, 2),
            "reason": f.reason,
            "raw_value": f.raw_value,
        })
    contributors.sort(key=lambda x: abs(x["contribution"] - 50 * x["weight"]), reverse=True)
    return {
        "score": round(score, 1),
        "available_weight": round(total_weight, 4),
        "missing_factors": missing,
        "contributors": contributors,
    }


def compute_score_layers(symbol: str, tech: dict, fut: dict, onchain: dict, depth: dict, historical: dict | None = None) -> dict:
    profile_key = get_symbol_profile(symbol)
    profiles = load_scoring_profiles()
    profile = profiles.get("profiles", {}).get(profile_key) or profiles.get("profiles", {}).get(profiles.get("default_profile")) or {}
    values = _factor_values(symbol, tech, fut, onchain, depth, historical or {})
    layers = {}
    for layer in ("opportunity", "entry", "risk", "execution"):
        weights = profile.get(layer, {})
        factors = []
        for name, weight in weights.items():
            base = values.get(name) or FactorScore(name, None, 0, "未知因子", "missing")
            factors.append(FactorScore(base.name, base.score, float(weight or 0), base.reason, base.status, base.raw_value))
        layers[layer] = weighted_score(factors)
    display_weights = profiles.get("display_weights") or {"opportunity": 0.35, "entry": 0.3, "risk_inverse": 0.2, "execution": 0.15}
    display = (
        layers.get("opportunity", {}).get("score", 50) * float(display_weights.get("opportunity", 0.35))
        + layers.get("entry", {}).get("score", 50) * float(display_weights.get("entry", 0.3))
        + (100 - layers.get("risk", {}).get("score", 50)) * float(display_weights.get("risk_inverse", 0.2))
        + layers.get("execution", {}).get("score", 50) * float(display_weights.get("execution", 0.15))
    )
    return {
        "profile": profile_key,
        "version": profiles.get("version"),
        "display_score": round(_clamp(display), 1),
        "thresholds": profile.get("thresholds", {}),
        "layers": layers,
    }
