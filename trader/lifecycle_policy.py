"""Side-effect-free lifecycle gates; suitable for event-time replay."""
from __future__ import annotations

import math
from datetime import datetime, timezone


def _time(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def evaluate_entry(action, mark_price, config, *, now):
    """Reject stale/chased entries against the original, immutable risk distance."""
    try:
        at = _time(action.get("signal_time"))
        age = (_time(now) - at).total_seconds() / 60
        if age < -1:
            return False, "signal_from_future", {}
        if age > float(config.get("signal_ttl_minutes", 75)):
            return False, "signal_expired", {"age_minutes": age}
        entry, stop, mark = (float(v) for v in (action["entry_price"], action["stop_loss"], mark_price))
        if not all(math.isfinite(v) and v > 0 for v in (entry, stop, mark)):
            return False, "invalid_entry_prices", {}
        direction = 1 if action["position_side"] == "LONG" else -1
        risk = direction * (entry - stop)
        if risk <= 0 or direction * (mark - stop) <= 0:
            return False, "entry_structure_invalidated", {}
        chase_r = direction * (mark - entry) / risk
        details = {"chase_r": chase_r, "age_minutes": age, "reference_entry": entry, "mark_price": mark}
        if chase_r > float(config.get("max_chase_r", 0.5)):
            return False, "entry_chased_wait_retest", details
        return True, "entry_lifecycle_ready", details
    except (ValueError, TypeError, KeyError):
        return False, "entry_context_missing", {}


def evaluate_add_risk(position, action, plan, config):
    try:
        qty = float(position["quantity"])
        add = float(action["quantity"])
        entry = float(position["entry_price"])
        mark = float(action["entry_price"])
        stop = float(action["stop_loss"])
        direction = 1 if action["position_side"] == "LONG" else -1
        budget = float(plan["initial_risk"]) * float(config.get("max_add_risk_multiple", 1))
        if not all(math.isfinite(v) for v in (qty, add, entry, mark, stop, budget)) or min(qty, add, entry, mark, stop, budget) <= 0:
            return False, "invalid_add_risk", {}
        if float(position.get("unrealized_pnl") or 0) <= 0:
            return False, "add_requires_profitable_position", {}
        # Do not offset fresh capital at risk with unbooked gains on the old leg.
        new_risk = max(0, direction * (mark - stop)) * add
        old_risk = max(0, direction * (entry - stop)) * qty
        if direction * (mark - stop) <= 0:
            return False, "add_stop_would_trigger", {}
        details = {"risk_at_stop": new_risk + old_risk, "risk_budget": budget}
        return (new_risk + old_risk <= budget), "add_risk_ready" if new_risk + old_risk <= budget else "add_risk_budget_exceeded", details
    except (TypeError, ValueError, KeyError):
        return False, "initial_risk_plan_missing", {}


def evaluate_management(position, state, config, *, now):
    """Single exit policy. Returned state is persisted before the next tick."""
    side = position["side"]
    direction = 1 if side == "LONG" else -1
    entry, mark = float(position["entry_price"]), float(position["mark_price"])
    risk_entry = float(state.get("risk_entry_price") or entry)
    initial_stop = float(state.get("initial_stop_loss") or 0)
    risk = float(state.get("risk_distance") or abs(risk_entry - initial_stop))
    if min(entry, mark, risk, initial_stop) <= 0:
        return None, {}, "initial_risk_state_missing"
    current_r = direction * (mark - risk_entry) / risk
    stop = float(state.get("current_stop_loss") or initial_stop)
    updates = {"r_multiple": current_r, "current_stop_loss": stop}

    def action(kind, reason, fraction=None):
        result = {"action": kind, "symbol": position["symbol"], "position_side": side,
                  "side": "SELL" if side == "LONG" else "BUY", "reason": reason,
                  "position_id": state.get("position_id"), "close_price": mark,
                  "strategy_source": state.get("strategy_source", "normal"),
                  "management_stage": reason}
        if fraction is not None:
            result["close_pct"] = fraction
        return result

    if direction * (mark - stop) <= 0:
        result = action("close", "lifecycle_structure_stop")
        result["is_stop"] = True
        return result, updates, "stop"
    margin = entry * float(position.get("quantity") or 0) / max(float(position.get("leverage") or 1), 1)
    if margin > 0 and float(position.get("unrealized_pnl") or 0) / margin <= -float(config.get("emergency_margin_stop_pct", 0.20)):
        result = action("close", "lifecycle_emergency_stop")
        result["is_stop"] = True
        return result, updates, "stop"
    runner = bool(config.get("runner_enabled"))
    peak_key = "highest_price" if side == "LONG" else "lowest_price"
    peak = float(state.get(peak_key) or mark)
    peak = max(peak, mark) if side == "LONG" else min(peak, mark)
    peak_r = direction * (peak - risk_entry) / risk
    updates[peak_key] = peak
    try:
        age = (_time(now) - _time(state["entry_time"])).total_seconds() / 3600
    except (KeyError, ValueError, TypeError):
        age = 0
    if age >= float(config.get("time_stop_hours", 12)) and current_r < float(config.get("time_stop_min_r", 0.25)) and not (runner and peak_r >= 1):
        return action("close", "lifecycle_time_stop"), updates, "time_stop"
    atr = float(state.get("atr_value") or risk / 2)
    if (peak_r if runner else current_r) >= 1:
        candidate = risk_entry + direction * risk_entry * float(config.get("break_even_buffer_pct", 0.002))
        stop = max(stop, candidate) if side == "LONG" else min(stop, candidate)
    if (peak_r if runner else current_r) >= 2 or state.get("trailing_enabled"):
        multiplier = float(config.get("trailing_atr_multiplier", 2.5)) if runner else float(state.get("trailing_atr_multiplier") or 2)
        candidate = peak - direction * atr * multiplier
        stop = max(stop, candidate) if side == "LONG" else min(stop, candidate)
        updates["trailing_enabled"] = 1
    updates["current_stop_loss"] = stop
    if direction * (mark - stop) <= 0:
        result = action("close", "lifecycle_trailing_stop")
        result["is_stop"] = True
        return result, updates, "stop"
    if not runner and current_r >= 1 and not state.get("tp1_hit"):
        return action("partial_close", "lifecycle_TP1", 0.25), updates, "tp1"
    if current_r >= 2 and not state.get("tp2_hit"):
        if runner:
            initial_quantity = float(state.get("initial_quantity") or position.get("quantity") or 0)
            if float(position.get("quantity") or 0) <= initial_quantity * 0.75:
                return None, updates, "runner_floor_already_reached"
            result = action("partial_close", "lifecycle_runner_TP2", 0.25)
            result["initial_quantity"] = initial_quantity
            result["min_remaining_fraction"] = 0.75
            return result, updates, "tp2"
        return action("partial_close", "lifecycle_TP2", 0.25), updates, "tp2"
    return None, updates, "hold"
