"""Risk and direction helpers for live execution."""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from typing import Any

from trader.config import HARD_FILTERS, TRADING_CONFIG


def _ensure_dict(row: Any) -> dict:
    if isinstance(row, dict):
        return row
    return dict(row)


def _norm_text(value: Any) -> str:
    return str(value or "").lower()


def _raw_features(row: dict) -> dict:
    raw = row.get("raw_features") or row.get("features") or {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return {}


def _age_minutes(value: Any) -> float | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    for candidate in (text, text.replace(" ", "T")):
        try:
            dt = datetime.fromisoformat(candidate)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return max(0.0, (datetime.now(timezone.utc) - dt.astimezone(timezone.utc)).total_seconds() / 60)
        except Exception:
            pass
    return None


def _symbol_class(symbol: str, fallback: str = "narrative") -> str:
    try:
        from trader.symbol_risk import get_symbol_risk

        return str((get_symbol_risk(symbol) or {}).get("class") or fallback)
    except Exception:
        return fallback


def _position_sizing_config(symbol: str, category: str | None = None) -> tuple[str, dict]:
    sizing = TRADING_CONFIG.get("position_sizing") or {}
    class_key = category or _symbol_class(symbol)
    result = dict(sizing.get(class_key) or sizing.get("narrative") or {})
    symbol_caps = (TRADING_CONFIG.get("dynamic_leverage") or {}).get("symbol_caps") or {}
    symbol_cap = symbol_caps.get(str(symbol or "").upper())
    if symbol_cap is not None:
        result["leverage_max"] = int(symbol_cap)
    return class_key, result


def _leverage_stop_pct(atr_pct: float) -> float:
    cfg = TRADING_CONFIG.get("dynamic_leverage") or {}
    multiplier = float(cfg.get("atr_stop_multiplier", 2.0))
    min_stop = float(cfg.get("min_stop_pct", 0.025))
    max_stop = float(cfg.get("max_stop_pct", 0.10))
    return min(max_stop, max(min_stop, max(0.0, float(atr_pct or 0)) * multiplier))


def _dynamic_leverage(atr_pct: float, sizing: dict) -> int:
    cfg = TRADING_CONFIG.get("dynamic_leverage") or {}
    stop_pct = _leverage_stop_pct(atr_pct)
    target_margin_loss = float(cfg.get("target_margin_loss_pct", 0.20))
    raw_leverage = math.floor(target_margin_loss / stop_pct) if stop_pct > 0 else 1
    min_leverage = int(cfg.get("min_leverage", 2))
    global_max = int(cfg.get("max_leverage", TRADING_CONFIG.get("leverage_max", 8)))
    class_or_symbol_cap = int(sizing.get("leverage_max", global_max))
    leverage_cap = max(1, min(global_max, class_or_symbol_cap))
    return max(1, min(leverage_cap, max(min_leverage, raw_leverage)))


def calculate_position(
    exchange,
    symbol: str,
    price: float,
    balance: float,
    score: float | None = None,
    category: str | None = None,
    entry_mode: str | None = None,
    size_multiplier: float = 1.0,
) -> dict:
    cfg = TRADING_CONFIG
    try:
        atr = float(exchange.get_atr(symbol))
    except Exception:
        atr = price * 0.02
    if atr <= 0:
        atr = price * 0.02
    atr_pct = atr / price if price > 0 else 0.02
    class_key, sizing = _position_sizing_config(symbol, category)
    leverage = _dynamic_leverage(atr_pct, sizing)
    leverage_stop_pct = _leverage_stop_pct(atr_pct)
    hard_stop_pct = float(sizing.get("hard_stop_pct", cfg.get("hard_stop_pct", 0.05)))
    min_stop_pct = float(sizing.get("min_stop_pct", sizing.get("min_effective_stop_pct", 0.003)))
    atr_multiplier = float(sizing.get("atr_stop_multiplier", 2.5))
    raw_stop_pct = atr_pct * atr_multiplier
    stop_pct = min(max(raw_stop_pct, min_stop_pct), hard_stop_pct)
    stop_distance = price * stop_pct

    mode = str(entry_mode or "confirmed").lower()
    if mode in {"probe", "normal_review_probe", "trend_probe"}:
        margin_pct = float(sizing.get("probe_margin_pct", cfg.get("position_size_pct", 0.20)))
    elif mode in {"strong", "trend_confirmed", "confirmed_strong"}:
        margin_pct = float(sizing.get("strong_margin_pct", sizing.get("confirmed_margin_pct", cfg.get("position_size_pct", 0.20))))
    else:
        margin_pct = float(sizing.get("confirmed_margin_pct", cfg.get("position_size_pct", 0.20)))

    score_adj = 1.0 if score is None else min(1.15, max(0.85, float(score) / 80.0))
    margin_pct *= score_adj * max(0.1, min(1.5, float(size_multiplier or 1.0)))
    max_margin_pct = float(sizing.get("max_margin_pct", margin_pct))
    margin_pct = min(margin_pct, max_margin_pct)
    target_margin = balance * margin_pct
    target_notional = target_margin * leverage

    risk_budget = balance * float(sizing.get("risk_per_trade_pct", cfg.get("risk_per_trade_pct", 0.015)))
    risk_notional = risk_budget / stop_pct
    capped_notional = min(target_notional, risk_notional)

    min_margin_pct = float(sizing.get("min_effective_margin_pct", 0))
    min_stop_pct = float(sizing.get("min_effective_stop_pct", 0))
    min_notional = balance * min_margin_pct * leverage
    if min_notional > 0 and stop_pct <= min_stop_pct:
        position_value = max(capped_notional, min(target_notional, min_notional))
    else:
        position_value = capped_notional

    max_notional = balance * max_margin_pct * leverage
    position_value = min(position_value, max_notional)
    margin = position_value / leverage if leverage else 0
    quantity = position_value / price if price > 0 else 0.0
    return {
        "quantity": round(quantity, 3),
        "stop_loss": round(stop_distance, 8),
        "take_profit": round(stop_distance * 2, 8),
        "tp1_distance": round(stop_distance, 8),
        "tp2_distance": round(stop_distance * 2, 8),
        "stop_model": "atr_clamped",
        "stop_pct": round(stop_pct, 6),
        "raw_stop_pct": round(raw_stop_pct, 6),
        "min_stop_pct": round(min_stop_pct, 6),
        "hard_stop_pct": round(hard_stop_pct, 6),
        "atr_stop_multiplier": atr_multiplier,
        "trailing_atr_multiplier": float(sizing.get("trailing_atr_multiplier", cfg.get("trailing_stop_atr_multiplier", 1.5))),
        "atr_value": atr,
        "atr_pct": round(atr_pct, 6),
        "leverage_stop_pct": round(leverage_stop_pct, 6),
        "leverage": leverage,
        "margin": margin,
        "target_margin": target_margin,
        "target_margin_pct": margin_pct,
        "target_notional": target_notional,
        "risk_notional": risk_notional,
        "position_value": position_value,
        "sizing_class": class_key,
        "entry_mode": mode,
        "risk_budget": risk_budget,
    }


def calc_tp_levels(entry_price: float, side: str, atr_value: float) -> dict:
    cfg = TRADING_CONFIG
    stop_pct = float(atr_value or 0)
    if stop_pct > 0.5:
        stop_pct = stop_pct / entry_price if entry_price > 0 else float(cfg.get("tp1_target_pct", 0.05))
    if stop_pct <= 0:
        stop_pct = float(cfg.get("tp1_target_pct", 0.05))
    tp1_pct = stop_pct
    tp2_pct = stop_pct * 2
    if side == "LONG":
        tp1 = entry_price * (1 + tp1_pct)
        tp2 = entry_price * (1 + tp2_pct)
    else:
        tp1 = entry_price * (1 - tp1_pct)
        tp2 = entry_price * (1 - tp2_pct)
    return {
        "tp1_price": round(tp1, 8),
        "tp2_price": round(tp2, 8),
        "tp1_qty_pct": float(cfg.get("tp1_pct", 0.50)),
        "tp2_qty_pct": float(cfg.get("tp2_pct", 0.50)),
        "trail_trigger_atr": float(cfg.get("trailing_stop_atr_multiplier", 1.5)),
    }


def calc_trailing_stop(current_price, highest_price, atr_value, trail_trigger_atr=1.5):
    if highest_price <= 0 or atr_value <= 0:
        return False
    return (highest_price - current_price) >= atr_value * trail_trigger_atr


def can_open_new_position(positions: list, max_positions: int | None = None) -> bool:
    return len(positions) < (max_positions or TRADING_CONFIG["max_positions"])


def _get_category_config() -> dict:
    paths = [
        os.path.join(os.path.dirname(__file__), "..", "strategies", "token_profiles.json"),
        os.path.join(os.path.dirname(__file__), "..", "..", "strategies", "token_profiles.json"),
    ]
    for path in paths:
        path = os.path.abspath(path)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            profiles = json.load(f)
        cats = profiles.get("categories", {})
        token_map = profiles.get("token_map", {})
        result = {}
        for sym, cat_name in token_map.items():
            cat_cfg = cats.get(cat_name, {})
            result[sym.upper()] = {
                "threshold": cat_cfg.get("score_threshold", TRADING_CONFIG["min_score"]),
                "risk_factor": cat_cfg.get("risk_factor", 1.0),
                "weight_boost": cat_cfg.get("weight_boost", 1.0),
                "max_pct": cat_cfg.get("max_position_pct", 10),
            }
        return result
    return {}


_CATEGORY_CACHE = None


def get_category_config() -> dict:
    global _CATEGORY_CACHE
    if _CATEGORY_CACHE is None:
        _CATEGORY_CACHE = _get_category_config()
    return _CATEGORY_CACHE


def get_symbol_threshold(symbol: str, fallback: float = 50) -> float:
    cfg = get_category_config()
    base = symbol.upper().replace("USDT", "")
    entry = cfg.get(base) or cfg.get(symbol.upper())
    return float(entry["threshold"]) if entry else fallback


_ENTRY_POLICY_CACHE = {"mtime": None, "data": None}


def _load_entry_policy() -> dict:
    path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "configs", "entry_policy.json"))
    if not os.path.exists(path):
        return {"rules": []}
    try:
        mtime = os.path.getmtime(path)
        if _ENTRY_POLICY_CACHE["mtime"] == mtime and _ENTRY_POLICY_CACHE["data"] is not None:
            return _ENTRY_POLICY_CACHE["data"]
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        _ENTRY_POLICY_CACHE["mtime"] = mtime
        _ENTRY_POLICY_CACHE["data"] = data
        return data
    except Exception:
        return {"rules": []}


def _feature_number(raw: dict, key: str, fallback: float = 0.0) -> float:
    for group in ("technical", "futures", "alpha", "depth"):
        value = (raw.get(group) or {}).get(key)
        if value is not None:
            try:
                return float(value)
            except Exception:
                return fallback
    try:
        return float(raw.get(key, fallback) or fallback)
    except Exception:
        return fallback


def evaluate_entry_policy(score_row, side: str | None) -> tuple[bool, str | None, list[dict]]:
    row = _ensure_dict(score_row)
    raw = _raw_features(row)
    policy = _load_entry_policy()
    matched = []
    for rule in policy.get("rules", []):
        if not rule.get("enabled", True):
            continue
        cond = rule.get("conditions") or {}
        rule_side = cond.get("side")
        if rule_side and side and str(rule_side).upper() != str(side).upper():
            continue
        ok = True
        if cond.get("rsi_gt") is not None and _feature_number(raw, "rsi", 50) <= float(cond["rsi_gt"]):
            ok = False
        if cond.get("funding_rate_gt") is not None and _feature_number(raw, "funding_rate", 0) <= float(cond["funding_rate_gt"]):
            ok = False
        price_terms = cond.get("price_position_contains") or []
        if price_terms:
            pos_text = str(row.get("price_position") or "").lower()
            if not any(str(term).lower() in pos_text for term in price_terms):
                ok = False
        if ok:
            matched.append(rule)
            action = rule.get("action") or {}
            if action.get("block"):
                return False, action.get("reason") or rule.get("name") or "entry_policy_block", matched
    return True, None, matched


def meets_safety_filters(score_row) -> tuple[bool, str]:
    row = _ensure_dict(score_row)
    raw = _raw_features(row)
    tech = raw.get("technical") or {}
    fut = raw.get("futures") or {}
    score = float(row.get("composite_score") or 0)
    symbol = row.get("symbol", "")
    entry_alpha = float(row.get("entry_alpha") or raw.get("entry_alpha") or 0)
    threshold = get_symbol_threshold(symbol, TRADING_CONFIG["min_score"])

    age = _age_minutes(row.get("time") or row.get("scan_time") or row.get("update_time"))
    max_age = TRADING_CONFIG.get("max_signal_age_minutes")
    if max_age and age is not None and age > float(max_age):
        return False, f"stale signal age={age:.1f}m"
    funding = float(fut.get("funding_rate") or 0)
    max_funding = float(HARD_FILTERS.get("max_funding_rate", 1))
    if abs(funding) > max_funding:
        return False, f"funding_rate {funding:.5f} > {max_funding:.5f}"
    # The real entry threshold is now owned by trader.entry_profiles per template.
    # Keep only an extreme floor here so weak rows do not waste live API checks.
    hard_score_floor = float(TRADING_CONFIG.get("hard_score_floor", 45))
    if score < hard_score_floor:
        return False, f"score {score:.1f} < hard_floor {hard_score_floor:.1f}"
    if entry_alpha and entry_alpha < 55:
        return False, f"entry_alpha {entry_alpha:.1f} < 55"
    return True, "OK"


def determine_side(score_row) -> str | None:
    """Return LONG/SHORT only when direction and context agree."""
    row = _ensure_dict(score_row)
    direction = str(row.get("trend_direction") or "")
    phase = str(row.get("chip_phase") or "")
    price_pos = str(row.get("price_position") or "")
    trend_state = str(row.get("trend_state") or "")
    score = float(row.get("composite_score") or 0)
    strength = float(row.get("relative_strength") or 50)
    raw = _raw_features(row)
    tech = raw.get("technical") or {}
    depth = raw.get("depth") or {}
    entry_alpha = float(row.get("entry_alpha") or 0)
    ret_6h = float(tech.get("return_6h") or 0)
    ret_24h = float(tech.get("return_24h") or tech.get("price_change_24h") or 0)
    depth_ratio = float(depth.get("depth_ratio") or 1)
    long_depth_ok = depth_ratio >= 0.80
    short_depth_ok = depth_ratio <= 1.25

    text = " ".join([direction, phase, price_pos, trend_state]).lower()
    is_uptrend = any(x in text for x in ("up", "向上", "上涨"))
    is_downtrend = any(x in text for x in ("down", "向下", "下跌"))
    is_low = any(x in text for x in ("低位", "偏低", "low"))
    is_high = any(x in text for x in ("高位", "偏高", "overbought", "high"))
    accumulating = any(x in text for x in ("accumulation", "reaccumulation", "吸筹", "蓄力", "筹码改善"))
    distributing = any(x in text for x in ("distribution", "出货", "派发"))

    if distributing and (is_downtrend or is_high) and short_depth_ok:
        return "SHORT"
    if accumulating and (is_uptrend or is_low) and not is_downtrend and entry_alpha >= 58 and strength >= 50 and long_depth_ok:
        return "LONG"
    if is_uptrend and not is_high and strength >= 55 and entry_alpha >= 58 and (ret_6h > 0 or ret_24h > 0) and long_depth_ok:
        return "LONG"
    if is_downtrend and not is_low and strength <= 45 and entry_alpha >= 55 and (ret_6h < 0 or ret_24h < 0) and short_depth_ok:
        return "SHORT"
    if score >= 75 and is_low and strength >= 60 and entry_alpha >= 62 and not distributing and not is_downtrend and long_depth_ok:
        return "LONG"
    if score >= 75 and is_high and strength <= 40 and entry_alpha >= 60 and not accumulating and short_depth_ok:
        return "SHORT"
    return None
