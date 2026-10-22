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
    def __init__(self, base_url="http://127.0.0.1:8010", timeout_seconds=2.0):
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


def _position_side(action):
    position_side = str(action.get("position_side") or "").upper()
    if position_side in {"LONG", "SHORT"}:
        return position_side
    order_side = str(action.get("side") or "").upper()
    if order_side in {"BUY", "LONG"}:
        return "LONG"
    if order_side in {"SELL", "SHORT"}:
        return "SHORT"
    return ""


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
    side = _position_side(action)
    features, feature_quality = extract_feature_payload(
        action_context,
        _json_object(action.get("ai_features")),
        row,
        _json_object(row.get("raw_features")),
        category=category,
        side=side,
    )
    return {
        "account_id": int(account_id),
        "model_key": "alpha" if is_alpha else "normal",
        "symbol": action["symbol"],
        "side": side,
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
    candidates = [
        build_candidate(action, scan_rows, account_id)
        for action in (actions or [])
        if action.get("action") == "open"
        and float(action.get("entry_price") or 0) > 0
        and (action.get("position_side") or action.get("side"))
    ]
    if not candidates:
        return {"sent": 0}
    try:
        observe(candidates)
        return {"sent": len(candidates)}
    except Exception as exc:
        logger.warning("AI candidate observation unavailable: %s", exc)
        return {"sent": 0, "error": str(exc)}


def apply_entry_quality_gate(
    actions,
    scan_rows,
    *,
    balance,
    exchange,
    account_id,
    evaluate=None,
    policy="advisory",
):
    evaluate = evaluate or AIEntryQualityClient().evaluate
    policy = "enforce" if str(policy).lower() == "enforce" else "advisory"
    filtered = []
    for action in actions:
        if action.get("action") != "open":
            filtered.append(action)
            continue
        if action.get("skip_entry_quality_gate"):
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
            if policy == "advisory" or action.get("event_type") == "explosive_breakout":
                action["ai_quality_decision"] = "reject_advisory"
                action["soft_gate_reasons"] = [
                    *(action.get("soft_gate_reasons") or []),
                    "ai_quality_reject",
                ]
                filtered.append(action)
                continue
            logger.info("AI rejected %s entry at quality=%s", action.get("symbol"), decision.get("quality_score"))
            continue
        if decision.get("decision") == "probe":
            if policy == "advisory":
                action["ai_quality_decision"] = "probe_advisory"
                action["soft_gate_reasons"] = [
                    *(action.get("soft_gate_reasons") or []),
                    "ai_quality_probe",
                ]
                filtered.append(action)
                continue
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
        elif policy == "enforce" and decision.get("position_factor") is not None:
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
