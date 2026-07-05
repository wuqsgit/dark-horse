"""Static symbol risk profiles for live entry gating and sizing."""
from __future__ import annotations

import json
import os
from typing import Any


PROFILE_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "configs", "symbol_risk_profiles.json"))
_CACHE: dict[str, Any] = {"mtime": None, "data": None}


def _load_profiles() -> dict:
    if not os.path.exists(PROFILE_PATH):
        return {"default_class": "narrative", "classes": {}, "symbols": {}}
    mtime = os.path.getmtime(PROFILE_PATH)
    if _CACHE["mtime"] == mtime and _CACHE["data"] is not None:
        return _CACHE["data"]
    with open(PROFILE_PATH, encoding="utf-8") as f:
        data = json.load(f)
    _CACHE["mtime"] = mtime
    _CACHE["data"] = data
    return data


def base_asset(symbol: str) -> str:
    value = str(symbol or "").upper().strip()
    for suffix in ("USDT", "USDC", "BUSD", "USD", "PERP"):
        if value.endswith(suffix) and len(value) > len(suffix):
            return value[: -len(suffix)]
    return value


def get_symbol_risk(symbol: str) -> dict:
    data = _load_profiles()
    base = base_asset(symbol)
    class_key = (data.get("symbols") or {}).get(base) or data.get("default_class") or "narrative"
    classes = data.get("classes") or {}
    cfg = dict(classes.get(class_key) or classes.get(data.get("default_class")) or {})
    cfg.setdefault("label", class_key)
    cfg.setdefault("min_score_offset", 0)
    cfg.setdefault("min_entry_alpha_offset", 0)
    cfg.setdefault("max_position_factor", 0.35)
    cfg.setdefault("probe_position_factor", cfg.get("max_position_factor", 0.35))
    cfg.setdefault("cooldown_multiplier", 1.0)
    cfg["class"] = class_key
    cfg["base_asset"] = base
    cfg["symbol"] = str(symbol or "").upper()
    cfg["config_version"] = data.get("version")
    return cfg


def allowed_for_template(symbol: str, template: str) -> tuple[bool, str | None, dict]:
    risk = get_symbol_risk(symbol)
    allowed = risk.get("allowed_templates")
    if allowed and template not in set(allowed):
        return False, f"risk_class {risk['class']} does not allow template {template}", risk
    return True, None, risk
