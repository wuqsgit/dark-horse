import json
import logging
from datetime import datetime, timezone

import httpx

from ai_service.features import FEATURE_SCHEMA_VERSION, extract_feature_payload


logger = logging.getLogger("trader.ai")


def _json_object(value):
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


class AIEntryQualityClient:
    def __init__(self, base_url="http://127.0.0.1:8010", timeout_seconds=0.3):
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = float(timeout_seconds)

    def evaluate(self, candidate):
        response = httpx.post(
            f"{self.base_url}/v1/entry-quality/evaluate",
            json=candidate,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    def observe_many(self, candidates):
        response = httpx.post(
            f"{self.base_url}/v1/entry-quality/observe",
            json={"candidates": candidates},
            timeout=max(3.0, self.timeout_seconds),
        )
        response.raise_for_status()
        return response.json()


def _flatten_features(value, result=None):
    result = result if result is not None else {}
    if not isinstance(value, dict):
        return result
    for key, item in value.items():
        if isinstance(item, dict):
            _flatten_features(item, result)
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            result.setdefault(key, item)
    return result


def build_learning_action(
    row,
    *,
    side,
    strategy_source="normal",
    category=None,
    symbol=None,
    price=None,
):
    if not side:
        return None
    source = dict(row or {})
    symbol = str(symbol or source.get("symbol") or source.get("futures_symbol") or "").upper()
    price = float(price or source.get("price") or source.get("market_price") or 0)
    if not symbol or price <= 0:
        return None
    raw = source.get("raw_features") or source.get("features") or {}
    flat = _flatten_features(raw)
    atr_ratio = float(flat.get("atr_ratio") or flat.get("atr_pct") or 0.025)
    if atr_ratio > 0.5:
        atr_ratio /= 100.0
    stop_pct = round(max(0.025, min(0.10, atr_ratio * 2.0)), 6)
    is_alpha = strategy_source == "alpha"
    return {
        "action": "observe",
        "symbol": symbol,
        "position_side": str(side).upper(),
        "entry_price": price,
        "stop_pct": stop_pct,
        "strategy_source": strategy_source,
        "category": "alpha" if is_alpha else (category or "unknown"),
        "score": float(source.get("alpha_score") or source.get("composite_score") or 0),
        "entry_mode": "pre_gate_candidate",
        "ai_sample_template": "alpha_entry" if is_alpha else "normal_entry",
        "ai_features": raw,
    }


def build_candidate(action, scan_rows, account_id):
    row = next((item for item in scan_rows if item.get("symbol") == action.get("symbol")), {})
    is_alpha = action.get("strategy_source") == "alpha"
    category = "alpha" if is_alpha else action.get("category") or row.get("category") or "unknown"
    action_context = {
        **action,
        "score": float(action.get("score") or row.get("composite_score") or 0),
        "entry_alpha": row.get("entry_alpha"),
        "hold_alpha": row.get("hold_alpha"),
        "relative_strength": row.get("relative_strength"),
    }
    features, feature_quality = extract_feature_payload(
        action_context,
        _json_object(action.get("ai_features")),
        row,
        _json_object(row.get("raw_features")),
        category=category,
    )
    return {
        "account_id": int(account_id),
        "model_key": "alpha" if is_alpha else "normal",
        "symbol": action["symbol"],
        "side": action.get("position_side") or ("LONG" if action.get("side") == "BUY" else "SHORT"),
        "template": action.get("ai_sample_template") or ("alpha_entry" if is_alpha else "normal_entry"),
        "category": category,
        "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "entry_price": float(action.get("entry_price") or 0),
        "stop_pct": float(action.get("stop_pct") or 0),
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_quality": feature_quality,
        "features": features,
    }


def observe_entry_quality_candidates(actions, scan_rows, *, account_id, observe=None):
    observe = observe or AIEntryQualityClient().observe_many
    learning_actions = list(actions or [])
    existing = {
        (
            str(action.get("symbol") or "").upper(),
            str(action.get("strategy_source") or "normal"),
        )
        for action in learning_actions
    }
    try:
        from trader.risk import determine_side
        from trader.symbol_risk import get_symbol_risk

        for row in scan_rows or []:
            symbol = str(row.get("symbol") or "").upper()
            key = (symbol, "normal")
            if not symbol or key in existing:
                continue
            action = build_learning_action(
                row,
                side=determine_side(row),
                strategy_source="normal",
                category=(get_symbol_risk(symbol) or {}).get("class"),
            )
            if action:
                learning_actions.append(action)
                existing.add(key)
    except Exception as exc:
        logger.warning("AI full-scan candidate expansion unavailable: %s", exc)

    candidates = [
        build_candidate(action, scan_rows, account_id)
        for action in learning_actions
    ]
    if not candidates:
        return {"sent": 0}
    try:
        observe(candidates)
        return {"sent": len(candidates)}
    except Exception as exc:
        logger.warning("AI candidate observation unavailable: %s", exc)
        return {"sent": 0, "error": str(exc)}


def apply_entry_quality_gate(actions, scan_rows, *, balance, exchange, account_id, evaluate=None):
    evaluate = evaluate or AIEntryQualityClient().evaluate
    filtered = []
    for action in actions:
        if action.get("action") != "open":
            filtered.append(action)
            continue
        try:
            decision = evaluate(build_candidate(action, scan_rows, account_id))
        except Exception as exc:
            logger.warning("AI entry-quality unavailable; use rule decision for %s: %s", action.get("symbol"), exc)
            action = dict(action)
            action["ai_quality_status"] = "fallback"
            action["ai_quality_decision"] = "rule_fallback"
            action["ai_quality_reasons"] = [str(exc)]
            filtered.append(action)
            continue

        action = dict(action)
        action["ai_quality_status"] = decision.get("status")
        action["ai_quality_decision"] = decision.get("decision")
        action["ai_quality_score"] = decision.get("quality_score")
        action["ai_model_version"] = decision.get("model_version")
        action["ai_quality_reasons"] = decision.get("reasons") or []
        action["ai_expected_r"] = decision.get("expected_r")
        action["ai_position_factor"] = decision.get("position_factor")

        if decision.get("applied") is False:
            filtered.append(action)
            continue
        if decision.get("decision") == "reject":
            logger.info("AI rejected %s entry at quality=%s", action.get("symbol"), decision.get("quality_score"))
            continue
        if decision.get("decision") == "probe":
            margin_pct = float(decision.get("target_margin_pct") or 0.05)
            price = float(action.get("entry_price") or 0)
            leverage = max(1.0, float(action.get("leverage") or 1))
            if price <= 0:
                logger.error("AI probe blocked %s because entry price is invalid", action.get("symbol"))
                continue
            target_quantity = exchange.adjust_quantity(
                action["symbol"], float(balance) * margin_pct * leverage / price,
            )
            action["quantity"] = min(float(action.get("quantity") or 0), float(target_quantity))
            if action["quantity"] <= 0:
                continue
            action["invested"] = round(price * action["quantity"], 2)
            action["ai_target_margin_pct"] = margin_pct
        elif decision.get("position_factor") is not None:
            factor = max(0.0, float(decision["position_factor"]))
            action["quantity"] = exchange.adjust_quantity(
                action["symbol"], float(action.get("quantity") or 0) * factor,
            )
            if action["quantity"] <= 0:
                continue
            action["invested"] = round(
                float(action.get("entry_price") or 0) * action["quantity"], 2,
            )
        filtered.append(action)
    return filtered
