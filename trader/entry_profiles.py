"""Per-symbol entry profile evaluation."""
from __future__ import annotations

import json
import os
from typing import Any

from trader.symbol_classifier import classify_symbol
from trader.symbol_risk import get_symbol_risk

PROFILE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "configs", "entry_profiles.json"))
_CACHE = {"mtime": None, "data": None}


def _load_profiles() -> dict:
    if not os.path.exists(PROFILE_PATH):
        return {"default_template": "breakout", "templates": {}, "symbols": {}}
    mtime = os.path.getmtime(PROFILE_PATH)
    if _CACHE["mtime"] == mtime and _CACHE["data"] is not None:
        return _CACHE["data"]
    with open(PROFILE_PATH, encoding="utf-8") as f:
        data = json.load(f)
    _CACHE["mtime"] = mtime
    _CACHE["data"] = data
    return data


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


def get_entry_profile(symbol: str, classified_profile: str | None = None) -> dict:
    data = _load_profiles()
    symbol = symbol.upper()
    symbol_cfg = dict(data.get("symbols", {}).get(symbol, {}))
    if symbol_cfg.get("template_locked"):
        template_key = symbol_cfg.get("template")
    elif data.get("auto_classification", True) and classified_profile:
        template_key = classified_profile
    else:
        template_key = symbol_cfg.get("template") or data.get("default_template", "breakout")
    template_key = template_key or data.get("default_template", "breakout")
    template = dict(data.get("templates", {}).get(template_key, {}))
    merged = {**template, **symbol_cfg}
    merged["template"] = template_key
    merged["template_name"] = merged.get("name") or template_key
    merged["config_version"] = data.get("version")
    merged["template_locked"] = bool(symbol_cfg.get("template_locked"))
    return merged


def _confirmation(label: str, ok: bool, passed_text: str, missing_text: str, *, required: bool = True, kind: str = "base") -> dict:
    return {
        "label": label,
        "ok": bool(ok),
        "required": bool(required),
        "kind": kind,
        "status": "passed" if ok else ("missing" if required else "optional"),
        "text": passed_text if ok else missing_text,
    }


def _confirmation_count(confirmations: list[dict], breakout_ok: bool, volume_ok: bool, oi_ok: bool, depth_ok: bool, short_structure_ok: bool) -> int:
    count = 0
    for ok in (breakout_ok, volume_ok, oi_ok, depth_ok, short_structure_ok):
        if ok:
            count += 1
    return count


def evaluate_profile_entry(row: Any, v3_signals: dict | None = None, side: str | None = None) -> dict:
    row = _row_dict(row)
    raw = _raw_features(row)
    classification = classify_symbol(row, v3_signals, side)
    symbol = str(row.get("symbol") or "").upper()
    profile = get_entry_profile(symbol, classification.get("profile"))
    symbol_risk = get_symbol_risk(symbol)

    tech = raw.get("technical") or {}
    fut = raw.get("futures") or {}
    depth = raw.get("depth") or {}
    breakout = (v3_signals or {}).get("breakout") or {}
    cooldown = (v3_signals or {}).get("cooldown") or {}
    rr = (v3_signals or {}).get("rr") or {}

    score = _num(row.get("composite_score"))
    entry_alpha = _num(row.get("entry_alpha") or raw.get("entry_alpha"))
    rs = _num(row.get("relative_strength"), 50)
    atr_ratio = _num(tech.get("atr_ratio"))
    ret_6h = _num(tech.get("return_6h"))
    ret_24h = _num(tech.get("price_change_24h") if tech.get("price_change_24h") is not None else tech.get("return_24h"))
    funding_rate = _num(fut.get("funding_rate"))
    oi_change = _num(fut.get("oi_change_pct") if fut.get("oi_change_pct") is not None else fut.get("oi_change"))
    volume_change = _num(tech.get("volume_change_pct"))
    depth_ratio = _num(depth.get("depth_ratio"), 1.0)
    support_score = _num(tech.get("support_score"), 50)
    support_quality = str(tech.get("support_quality") or "")
    price_position = str(row.get("price_position") or tech.get("price_position") or "")
    trend_state = str(row.get("trend_state") or tech.get("trend_state") or "")
    trend_direction = str(row.get("trend_direction") or "")
    chip_phase = str(row.get("chip_phase") or tech.get("chip_phase") or "")
    context_text = " ".join([price_position, trend_state, trend_direction, chip_phase])

    rr_used = _num(rr.get("rr_used") if isinstance(rr, dict) else (v3_signals or {}).get("rr_ratio"))
    volume_ratio = _num(breakout.get("volume_ratio"))
    base_min_score = _num(profile.get("min_score"), 0)
    base_min_entry_alpha = _num(profile.get("min_entry_alpha"), 0)
    min_score = max(0.0, min(100.0, base_min_score + _num(symbol_risk.get("min_score_offset"), 0)))
    min_entry_alpha = max(0.0, min(100.0, base_min_entry_alpha + _num(symbol_risk.get("min_entry_alpha_offset"), 0)))
    min_vol = _num(profile.get("volume_multiplier"), 0)
    min_rr = _num(profile.get("min_rr"), 0)
    probe_min_rr = _num(profile.get("probe_min_rr"), 0)
    require_breakout = bool(profile.get("require_breakout"))
    allow_early_probe = bool(profile.get("allow_early_probe"))
    early_probe_max_distance = _num(profile.get("early_probe_max_distance_pct"), 0)
    early_probe_min_vol = _num(profile.get("early_probe_min_volume_multiplier"), min_vol)
    early_probe_min_score = _num(profile.get("early_probe_min_score"), min_score)
    early_probe_min_entry_alpha = _num(profile.get("early_probe_min_entry_alpha"), min_entry_alpha)

    is_low = _has(context_text, "低位", "偏低", "low")
    is_high = _has(context_text, "高位", "偏高", "high")
    is_down = _has(context_text, "向下", "下跌", "down")
    is_weak = rs <= _num(profile.get("require_weak_rs"), 45)
    downtrend_ok = is_down or (ret_6h < 0 and ret_24h < 0)
    depth_long_ok = depth_ratio >= 0.90
    depth_short_ok = depth_ratio <= 1.10
    support_ok = support_score >= 55 or _has(support_quality, "好", "强", "有效")
    breakout_ok = bool(breakout.get("ok"))
    volume_ok = volume_ratio >= min_vol if min_vol else True
    oi_ok = oi_change >= 0
    short_structure_ok = downtrend_ok and (is_high or ret_24h < 0)
    allowed_templates = symbol_risk.get("allowed_templates") or []
    risk_template_ok = not allowed_templates or profile.get("template") in set(allowed_templates)
    template_position_factor = _num(profile.get("position_size_factor"), 1.0)
    template_probe_factor = _num(profile.get("probe_position_size_factor"), template_position_factor)
    risk_position_factor = max(0.0, min(1.0, _num(symbol_risk.get("max_position_factor"), 1.0)))
    risk_probe_factor = max(0.0, min(1.0, _num(symbol_risk.get("probe_position_factor"), risk_position_factor)))
    effective_position_factor = round(template_position_factor * risk_position_factor, 4)
    effective_probe_factor = round(template_probe_factor * risk_probe_factor, 4)
    distance_to_breakout = _num(breakout.get("distance_to_breakout_pct"), 999)
    early_probe_ok = (
        allow_early_probe
        and profile.get("template") == "breakout"
        and side != "SHORT"
        and not breakout_ok
        and score >= early_probe_min_score
        and entry_alpha >= early_probe_min_entry_alpha
        and volume_ratio >= early_probe_min_vol
        and rr_used >= probe_min_rr
        and distance_to_breakout > 0
        and (not early_probe_max_distance or distance_to_breakout <= early_probe_max_distance)
        and depth_long_ok
        and oi_change >= 0
    )

    confirmations: list[dict] = []
    confirmations.append(_confirmation(
        "风险分类模板",
        risk_template_ok,
        f"risk_class {symbol_risk.get('class')} allows {profile.get('template')}",
        f"risk_class {symbol_risk.get('class')} does not allow template {profile.get('template')}",
        kind="risk",
    ))
    confirmations.append(_confirmation("综合分", score >= min_score, f"评分 {score:.1f} 已达到 {min_score:g}", f"评分 {score:.1f} 未达到 {min_score:g}", kind="base"))
    confirmations.append(_confirmation("开仓信号", entry_alpha >= min_entry_alpha, f"Entry Alpha {entry_alpha:.1f} 已达到 {min_entry_alpha:g}", f"Entry Alpha {entry_alpha:.1f} 未达到 {min_entry_alpha:g}", kind="base"))

    if profile.get("template") in ("short_breakdown", "weak_short"):
        confirmations.append(_confirmation("空头结构", short_structure_ok, "趋势/结构支持做空", "空头结构还不够明确", kind="template"))
        confirmations.append(_confirmation("盘口卖压", depth_short_ok, f"盘口卖压可接受 depth_ratio={depth_ratio:.2f}", f"盘口卖压不足 depth_ratio={depth_ratio:.2f}", required=False, kind="context"))
        if profile.get("require_weak_rs") is not None:
            confirmations.append(_confirmation("相对弱势", is_weak, f"RS {rs:.1f} 偏弱", f"RS {rs:.1f} 还不够弱", kind="template"))
    else:
        breakout_reason = breakout.get("reason") or "未突破前高"
        confirmations.append(_confirmation(
            profile.get("breakout_label") or "突破确认",
            breakout_ok,
            "已出现突破确认",
            breakout_reason if require_breakout else f"{breakout_reason}；当前模板不强制突破",
            required=require_breakout,
            kind="template",
        ))
        confirmations.append(_confirmation("盘口承接", depth_long_ok, f"盘口承接可接受 depth_ratio={depth_ratio:.2f}", f"盘口承接不足 depth_ratio={depth_ratio:.2f}", required=False, kind="context"))

    if min_vol:
        confirmations.append(_confirmation("成交量", volume_ok, f"成交量 {volume_ratio:.2f}x 已达到 {min_vol:.2f}x", f"成交量 {volume_ratio:.2f}x 未达到 {min_vol:.2f}x", required=False if profile.get("allow_probe") else True, kind="template"))
    if min_rr:
        confirmations.append(_confirmation("结构 R:R", rr_used >= min_rr, f"R:R {rr_used:.2f} 已达到 {min_rr:.2f}", f"R:R {rr_used:.2f} 未达到正常仓 {min_rr:.2f}", required=not (profile.get("allow_probe") and rr_used >= probe_min_rr), kind="template"))

    if allow_early_probe:
        confirmations.append(_confirmation(
            "early_probe",
            early_probe_ok,
            f"early probe ok: distance {distance_to_breakout * 100:.1f}%, vol {volume_ratio:.2f}x, R:R {rr_used:.2f}",
            f"early probe missing: distance {distance_to_breakout * 100:.1f}%, vol {volume_ratio:.2f}x, R:R {rr_used:.2f}",
            required=False,
            kind="template",
        ))

    max_atr = profile.get("max_atr_ratio")
    if max_atr is not None and atr_ratio:
        confirmations.append(_confirmation("波动率", atr_ratio <= _num(max_atr), f"ATR 占比 {atr_ratio * 100:.2f}% 在允许范围", f"ATR 占比超过 {float(max_atr) * 100:.1f}%", kind="risk"))

    if profile.get("require_low_position"):
        confirmations.append(_confirmation("价格位置", is_low, "价格处于低位", "价格位置不是低位", kind="template"))
        confirmations.append(_confirmation("支撑质量", support_ok, f"支撑质量达标 {support_score:.1f}", f"支撑质量不足 {support_score:.1f}", required=False, kind="context"))

    min_rs = profile.get("require_rs")
    if min_rs is not None:
        confirmations.append(_confirmation("相对强度", rs >= _num(min_rs), f"RS {rs:.1f} 达标", f"RS {rs:.1f} 未达到 {min_rs}", kind="template"))

    max_drop = profile.get("max_24h_drop")
    if max_drop is not None:
        confirmations.append(_confirmation("24h 跌幅", ret_24h <= _num(max_drop), "24h 跌幅满足回调条件", f"24h 跌幅未达到 {float(max_drop) * 100:.1f}%", kind="template"))

    max_funding = profile.get("max_funding_rate")
    if max_funding is not None:
        confirmations.append(_confirmation("资金费率", funding_rate <= _num(max_funding), "资金费率不过热", f"资金费率超过 {float(max_funding) * 100:.3f}%", kind="risk"))

    confirmations.append(_confirmation("冷却状态", not cooldown.get("in_cooldown"), "冷却状态正常", cooldown.get("reason") or "冷却中", kind="risk"))
    if oi_change < -3:
        confirmations.append(_confirmation("OI 改善", False, "OI 同步改善", "OI 仍在下降，合约参与度不足", required=False, kind="context"))
    elif oi_change > 0:
        confirmations.append(_confirmation("OI 改善", True, "OI 同步改善", "OI 暂无明显改善", required=False, kind="context"))
    else:
        confirmations.append(_confirmation("OI 改善", False, "OI 同步改善", "OI 暂无明显改善", required=False, kind="context"))

    passed = [x["text"] for x in confirmations if x["ok"]]
    missing = [x["text"] for x in confirmations if not x["ok"]]
    hard_missing = [x for x in confirmations if x["required"] and not x["ok"]]
    base_ok = score >= min_score and entry_alpha >= min_entry_alpha and not cooldown.get("in_cooldown")
    confirmation_count = _confirmation_count(confirmations, breakout_ok, volume_ok, oi_ok, depth_long_ok or depth_short_ok, short_structure_ok)
    confirmations_required = int(profile.get("confirmations_required_for_full_entry") or 0)

    if not hard_missing:
        if profile.get("allow_probe") and confirmations_required and confirmation_count < confirmations_required:
            status = "probe"
            reason = f"{profile['template_name']}基础条件成立，但确认不足，只允许小仓试探。"
        else:
            status = "pass"
            reason = f"{profile['template_name']}条件通过，可以进入实时盘口确认。"
    elif profile.get("allow_probe") and base_ok:
        probe_blockers = [
            x for x in hard_missing
            if not (
                (x["label"] == "结构 R:R" and rr_used >= probe_min_rr)
                or (
                    early_probe_ok
                    and x["kind"] == "template"
                    and x["label"] in {profile.get("breakout_label") or "breakout", "结构 R:R"}
                )
            )
        ]
        if not probe_blockers:
            status = "probe"
            if early_probe_ok:
                reason = f"{profile['template_name']} early probe: 20-period breakout not confirmed, but score/volume/OI/R:R meet probe conditions."
            else:
                reason = f"{profile['template_name']} reached probe conditions, but not full entry yet."
        elif profile.get("allow_observe_only"):
            status = "observe"
            reason = f"{profile['template_name']} basic conditions are close, but key confirmations are still missing."
        else:
            status = "block"
            reason = f"{profile['template_name']} conditions are insufficient; no entry."

    elif profile.get("allow_observe_only") and base_ok:
        status = "observe"
        reason = f"{profile['template_name']}基础条件成立，但还缺确认，进入观察。"
    else:
        status = "block"
        reason = f"{profile['template_name']}条件不足，暂不开仓。"

    template_message = profile.get("breakout_required_text")
    if not template_message:
        template_message = "本模板强制要求突破确认。" if require_breakout else "本模板不强制突破确认。"

    classification["profile_name"] = profile["template_name"]
    return {
        "ok": status in ("pass", "probe"),
        "status": status,
        "mode": "open" if status == "pass" else ("probe" if status == "probe" else ("observe" if status == "observe" else "block")),
        "template": profile["template"],
        "template_name": profile["template_name"],
        "template_locked": profile.get("template_locked", False),
        "classification": classification,
        "description": profile.get("description"),
        "template_message": template_message,
        "focus": profile.get("focus", []),
        "reason": reason,
        "passed": passed,
        "missing": missing,
        "confirmations": confirmations,
        "thresholds": {
            "min_score": min_score,
            "min_entry_alpha": min_entry_alpha,
            "base_min_score": base_min_score,
            "base_min_entry_alpha": base_min_entry_alpha,
            "effective_min_score": min_score,
            "effective_min_entry_alpha": min_entry_alpha,
            "volume_multiplier": profile.get("volume_multiplier"),
            "min_rr": profile.get("min_rr"),
            "probe_min_rr": profile.get("probe_min_rr"),
            "require_breakout": require_breakout,
            "position_size_factor": effective_position_factor,
            "probe_position_size_factor": effective_probe_factor,
            "base_position_size_factor": template_position_factor,
            "base_probe_position_size_factor": template_probe_factor,
            "risk_position_factor": risk_position_factor,
            "risk_probe_position_factor": risk_probe_factor,
            "effective_position_size_factor": effective_position_factor,
            "effective_probe_position_size_factor": effective_probe_factor,
            "confirmations_required_for_full_entry": confirmations_required,
            "allow_early_probe": allow_early_probe,
            "early_probe_max_distance_pct": profile.get("early_probe_max_distance_pct"),
            "early_probe_min_volume_multiplier": profile.get("early_probe_min_volume_multiplier"),
            "early_probe_min_score": profile.get("early_probe_min_score"),
            "early_probe_min_entry_alpha": profile.get("early_probe_min_entry_alpha"),
        },
        "risk_profile": symbol_risk,
        "metrics": {
            "score": score,
            "entry_alpha": entry_alpha,
            "relative_strength": rs,
            "atr_ratio": atr_ratio,
            "volume_ratio": volume_ratio,
            "volume_change_pct": volume_change,
            "rr_used": rr_used,
            "ret_24h": ret_24h,
            "funding_rate": funding_rate,
            "oi_change": oi_change,
            "depth_ratio": depth_ratio,
            "support_score": support_score,
            "confirmation_count": confirmation_count,
            "distance_to_breakout_pct": distance_to_breakout,
            "early_probe_ok": early_probe_ok,
            "side": side,
        },
    }
