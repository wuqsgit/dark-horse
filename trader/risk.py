"""Risk and direction helpers for live execution."""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from typing import Any

from shared.directional_scoring import compute_short_entry_alpha
from trader.config import HARD_FILTERS, TRADING_CONFIG


_PROBE_ENTRY_MODES = {"probe", "normal_review_probe", "trend_probe"}


def should_execute_entry_mode(entry_mode: str | None) -> bool:
    """Probe signals are observation-only and never create live orders."""
    return str(entry_mode or "confirmed").lower() not in _PROBE_ENTRY_MODES


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
    enforce_risk_budget: bool = False,
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
    max_stop_pct = max(
        min_stop_pct,
        float(sizing.get("max_stop_pct", (cfg.get("dynamic_leverage") or {}).get("max_stop_pct", 0.10))),
    )
    atr_multiplier = float(sizing.get("atr_stop_multiplier", 2.5))
    raw_stop_pct = atr_pct * atr_multiplier
    # 1R is a price/structure risk, independent from leverage.  The old model
    # divided a margin-ROI stop by leverage, which made identical setups use a
    # different market stop whenever leverage changed.
    stop_pct = min(max_stop_pct, max(min_stop_pct, raw_stop_pct))
    stop_distance = price * stop_pct

    mode = str(entry_mode or "confirmed").lower()
    strong_entry = mode in {"strong", "trend_confirmed", "confirmed_strong"}
    if mode in _PROBE_ENTRY_MODES:
        margin_pct = float(sizing.get("probe_margin_pct", cfg.get("position_size_pct", 0.20)))
    elif strong_entry:
        margin_pct = float(sizing.get("strong_margin_pct", sizing.get("confirmed_margin_pct", cfg.get("position_size_pct", 0.20))))
    else:
        margin_pct = float(sizing.get("confirmed_margin_pct", cfg.get("position_size_pct", 0.20)))

    if not strong_entry:
        score_adj = 1.0 if score is None else min(1.15, max(0.85, float(score) / 80.0))
        margin_pct *= score_adj * max(0.1, min(1.5, float(size_multiplier or 1.0)))
    max_margin_pct = float(sizing.get("max_margin_pct", margin_pct))
    margin_pct = min(margin_pct, max_margin_pct)
    effective_size_multiplier = max(0.1, min(1.5, float(size_multiplier or 1.0)))
    target_margin = balance * margin_pct
    if enforce_risk_budget:
        target_margin *= effective_size_multiplier
    target_notional = target_margin * leverage

    base_risk_pct = sizing.get(
        "risk_per_trade_pct",
        cfg.get("risk_per_trade_pct", 0.005),
    )
    if mode in _PROBE_ENTRY_MODES:
        risk_per_trade_pct = float(
            sizing.get("probe_risk_per_trade_pct", base_risk_pct)
        )
    elif mode in {"strong", "trend_confirmed", "confirmed_strong"}:
        risk_per_trade_pct = float(
            sizing.get("strong_risk_per_trade_pct", base_risk_pct)
        )
    else:
        risk_per_trade_pct = float(
            sizing.get("confirmed_risk_per_trade_pct", base_risk_pct)
        )
    risk_budget = balance * risk_per_trade_pct
    risk_notional = risk_budget / stop_pct
    # Strong confirmations use the configured balance-based margin directly.
    # The risk budget remains observable, but does not silently shrink the order.
    position_value = (
        min(target_notional, risk_notional)
        if enforce_risk_budget or not strong_entry
        else target_notional
    )

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
        "stop_model": "structure_atr_1r",
        "stop_pct": round(stop_pct, 6),
        "raw_stop_pct": round(raw_stop_pct, 6),
        "min_stop_pct": round(min_stop_pct, 6),
        "max_stop_pct": round(max_stop_pct, 6),
        "hard_stop_pct": round(hard_stop_pct, 6),
        "hard_stop_price_pct": round(stop_pct, 6),
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
        "risk_per_trade_pct": risk_per_trade_pct,
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


def is_adverse_funding_rate(funding_rate: float, max_rate: float, side: str | None) -> bool:
    """Return whether funding is expensive for the intended position direction."""
    funding = float(funding_rate or 0)
    limit = abs(float(max_rate or 0))
    direction = str(side or "").upper()
    if direction == "LONG":
        return funding > limit
    if direction == "SHORT":
        return funding < -limit
    return abs(funding) > limit


def funding_position_factor(funding_rate: float, side: str | None) -> float:
    funding = float(funding_rate or 0)
    direction = str(side or "").upper()
    short_cfg = TRADING_CONFIG.get("short_trading") or {}
    reduce_at = float(short_cfg.get("negative_funding_reduce_at", -0.001))
    block_at = float(short_cfg.get("negative_funding_block_at", -0.003))
    if direction == "SHORT" and funding < reduce_at:
        return float(short_cfg.get("probe_position_factor", 0.5)) if funding >= block_at else 0.0
    return 1.0


def entry_alpha_for_side(score_row, side: str | None) -> float:
    row = _ensure_dict(score_row)
    raw = _raw_features(row)
    if str(side or "").upper() != "SHORT":
        return float(row.get("entry_alpha") or raw.get("entry_alpha") or 0)
    stored = row.get("short_entry_alpha")
    if stored is None:
        stored = raw.get("short_entry_alpha")
    if stored is not None:
        return float(stored or 0)
    return compute_short_entry_alpha(
        raw.get("technical"),
        raw.get("futures"),
        raw.get("depth"),
        raw.get("phase"),
        float(row.get("relative_strength") or 50),
    )


def evaluate_short_setup(score_row) -> dict:
    """Identify a breakdown continuation or failed-rebound short setup."""
    row = _ensure_dict(score_row)
    raw = _raw_features(row)
    tech = raw.get("technical") or {}
    depth = raw.get("depth") or {}
    direction = str(row.get("trend_direction") or tech.get("trend_direction") or "")
    phase = str(row.get("chip_phase") or tech.get("chip_phase") or "")
    position = str(row.get("price_position") or tech.get("price_position") or "")
    ret_1h = float(tech.get("return_1h") or 0)
    ret_6h = float(tech.get("return_6h") or 0)
    ret_24h = float(
        tech.get("return_24h")
        if tech.get("return_24h") is not None
        else tech.get("price_change_24h") or 0
    )
    volume_change = float(tech.get("volume_change_pct") or 0)
    rsi = float(tech.get("rsi_14") or 50)
    ema20 = float(tech.get("ema20") or 0)
    ema_slope = float(tech.get("ema20_slope") or 0)
    price = float(row.get("market_price") or row.get("price") or tech.get("current_price") or 0)
    depth_ratio = float(depth.get("depth_ratio") or 1)
    is_down = direction == "向下" or (ret_6h < 0 and ret_24h < 0)
    is_low = "低位" in position or "偏低" in position
    distribution = phase in {"疑似出货", "筹码松动"}

    breakdown = bool(
        is_down
        and ret_6h < 0
        and ret_24h < 0
        and (volume_change >= 1.1 or distribution)
        and depth_ratio <= 1.25
    )
    failed_rebound = bool(
        is_down
        and ema20 > 0
        and price > 0
        and price < ema20
        and ema_slope < 0
        and ret_1h <= 0
        and not is_low
        and depth_ratio <= 1.20
    )
    chase_reasons = []
    if rsi < 25:
        chase_reasons.append(f"rsi={rsi:.1f}<25")
    if ret_6h <= -0.08:
        chase_reasons.append(f"return_6h={ret_6h:.2%}")
    if is_low and ret_6h <= -0.04:
        chase_reasons.append("low_position_after_sharp_drop")

    setup = "failed_rebound" if failed_rebound else "breakdown_continuation" if breakdown else None
    return {
        "eligible": bool(setup and not chase_reasons),
        "setup": setup,
        "breakdown": breakdown,
        "failed_rebound": failed_rebound,
        "anti_chase": bool(chase_reasons),
        "anti_chase_reasons": chase_reasons,
        "metrics": {
            "return_1h": ret_1h,
            "return_6h": ret_6h,
            "return_24h": ret_24h,
            "volume_change_pct": volume_change,
            "rsi": rsi,
            "ema20": ema20,
            "ema20_slope": ema_slope,
            "depth_ratio": depth_ratio,
        },
    }


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


def meets_safety_filters(score_row, side: str | None = None) -> tuple[bool, str]:
    row = _ensure_dict(score_row)
    raw = _raw_features(row)
    tech = raw.get("technical") or {}
    fut = raw.get("futures") or {}
    score = float(row.get("composite_score") or 0)
    symbol = row.get("symbol", "")
    entry_alpha = entry_alpha_for_side(row, side)
    threshold = get_symbol_threshold(symbol, TRADING_CONFIG["min_score"])

    age = _age_minutes(row.get("time") or row.get("scan_time") or row.get("update_time"))
    max_age = TRADING_CONFIG.get("max_signal_age_minutes")
    if max_age and age is not None and age > float(max_age):
        return False, f"stale signal age={age:.1f}m"
    funding = float(fut.get("funding_rate") or 0)
    max_funding = float(HARD_FILTERS.get("max_funding_rate", 1))
    funding_limit = (
        abs(float((TRADING_CONFIG.get("short_trading") or {}).get("negative_funding_block_at", -0.003)))
        if str(side or "").upper() == "SHORT"
        else max_funding
    )
    if is_adverse_funding_rate(funding, funding_limit, side):
        direction = str(side or "UNKNOWN").upper()
        return False, (
            f"adverse_funding_rate side={direction} rate={funding:.5f} "
            f"limit={funding_limit:.5f}"
        )
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
    short_entry_alpha = entry_alpha_for_side(row, "SHORT")
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

    short_setup = evaluate_short_setup(row)
    short_cfg = TRADING_CONFIG.get("short_trading") or {}
    if (
        bool(short_cfg.get("enabled", True))
        and short_entry_alpha >= float(short_cfg.get("min_entry_alpha", 65))
        and short_setup.get("eligible")
    ):
        return "SHORT"
    if accumulating and (is_uptrend or is_low) and not is_downtrend and entry_alpha >= 58 and strength >= 50 and long_depth_ok:
        return "LONG"
    if is_uptrend and not is_high and strength >= 55 and entry_alpha >= 58 and (ret_6h > 0 or ret_24h > 0) and long_depth_ok:
        return "LONG"
    if score >= 75 and is_low and strength >= 60 and entry_alpha >= 62 and not distributing and not is_downtrend and long_depth_ok:
        return "LONG"
    return None
