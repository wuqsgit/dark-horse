"""Conservative single-instrument replay of the production lifecycle policies.

Signals must be timestamped decisions, not retrospectively selected winners.
Close-derived decisions execute at the following bar's open. Stops known before
a bar are evaluated before its close-derived management decisions.
"""
from datetime import timedelta

from trader.action_coordinator import coordinate_actions
from trader.lifecycle_policy import _time, evaluate_entry, evaluate_management, evaluate_add_risk
from trader.roll_policy import evaluate_roll


def replay_lifecycle(bars, signals, *, config=None, fee_rate=0.0005, slippage=0.001, bar_minutes=15):
    config = dict(config or {})
    if not 0 <= fee_rate < 1 or not 0 <= slippage < 1 or bar_minutes <= 0:
        raise ValueError("invalid_replay_cost_or_time_parameters")
    bars = sorted((dict(b) for b in bars), key=lambda b: _time(b["time"]))
    if len({_time(b["time"]) for b in bars}) != len(bars):
        raise ValueError("duplicate_bar_time")
    signals = sorted((dict(s) for s in signals), key=lambda s: _time(s["signal_time"]))
    position, state, plan, pending = None, {}, None, []
    events, gross, fees, funding = [], 0.0, 0.0, 0.0
    signal_index = 0

    def fill(action, price, at):
        nonlocal position, state, plan, gross, fees
        opening = action["action"] in {"open", "roll_add"}
        direction = 1 if action["position_side"] == "LONG" else -1
        price *= 1 + direction * slippage * (1 if opening else -1)
        if opening:
            qty = float(action["quantity"])
            if action["action"] == "open":
                if position:
                    return
                allowed, reason, _ = evaluate_entry(action, price, config, now=at)
                if not allowed:
                    events.append({"time": str(at), "status": "blocked", "reason": reason})
                    return
                position = {"symbol": action["symbol"], "side": action["position_side"],
                            "quantity": qty, "entry_price": price, "mark_price": price, "unrealized_pnl": 0}
                risk = abs(price - float(action["stop_loss"]))
                state = {"position_id": f"replay:{at}", "initial_stop_loss": action["stop_loss"],
                         "current_stop_loss": action["stop_loss"], "risk_entry_price": price,
                         "risk_distance": risk, "initial_quantity": qty, "entry_time": at,
                         "atr_value": action.get("atr_value") or risk / 2}
                plan = {"initial_risk": risk * qty}
            else:
                if not position:
                    return
                candidate = {**action, "entry_price": price}
                allowed, reason, _ = evaluate_add_risk(position, candidate, plan, config)
                if not allowed:
                    events.append({"time": str(at), "status": "blocked", "reason": reason})
                    return
                old = position["quantity"]
                position["entry_price"] = (old * position["entry_price"] + qty * price) / (old + qty)
                position["quantity"] += qty
                state.update(roll_layer=action["roll_layer"], roll_pullback_armed=False, roll_cycle_peak_price=price)
        else:
            if not position:
                return
            qty = position["quantity"] * (float(action.get("close_pct", 1)) if action["action"] == "partial_close" else 1)
            gross += direction * (price - position["entry_price"]) * qty
            position["quantity"] -= qty
            if "TP1" in action.get("reason", ""):
                state["tp1_hit"] = 1
            if "TP2" in action.get("reason", ""):
                state["tp2_hit"] = 1
            if position["quantity"] <= 1e-12:
                position = None
        fees += price * qty * fee_rate
        events.append({"time": str(at), "status": "filled", "action": action["action"],
                       "price": price, "quantity": qty, "reason": action.get("reason")})

    for bar in bars:
        at = _time(bar["time"])
        if not (0 < bar["low"] <= min(bar["open"], bar["close"]) <= max(bar["open"], bar["close"]) <= bar["high"]):
            raise ValueError("invalid_ohlc")
        if position:
            side = position["side"]
            stop = state["current_stop_loss"]
            if (side == "LONG" and bar["open"] <= stop) or (side == "SHORT" and bar["open"] >= stop):
                fill({"action": "close", "symbol": position["symbol"], "position_side": side,
                      "reason": "exchange_gap_stop"}, float(bar["open"]), at)
                pending = []
        while signal_index < len(signals) and _time(signals[signal_index]["signal_time"]) <= at:
            pending.append(signals[signal_index])
            signal_index += 1
        for action in coordinate_actions(pending).accepted:
            fill(action, float(bar["open"]), at)
        pending = []
        if not position:
            continue
        direction = 1 if position["side"] == "LONG" else -1
        stop = state["current_stop_loss"]
        if (direction == 1 and bar["low"] <= stop) or (direction == -1 and bar["high"] >= stop):
            price = min(float(bar["open"]), stop) if direction == 1 else max(float(bar["open"]), stop)
            fill({"action": "close", "symbol": position["symbol"], "position_side": position["side"],
                  "reason": "exchange_stop"}, price, at)
            continue
        funding += float(bar.get("funding_payment") or 0)
        position["mark_price"] = float(bar["close"])
        position["unrealized_pnl"] = direction * (position["mark_price"] - position["entry_price"]) * position["quantity"]
        action, updates, _ = evaluate_management(position, state, config, now=at + timedelta(minutes=bar_minutes))
        state.update(updates)
        if action:
            pending.append(action)
        if config.get("enable_adds") and not action:
            roll = evaluate_roll(position, state, bar.get("technical") or {}, True,
                                 {**config, "require_pullback_recovery": True})
            state.update(roll_cycle_peak_price=roll.cycle_peak_price, roll_pullback_armed=roll.pullback_armed)
            if roll.eligible:
                pending.append({"action": "roll_add", "symbol": position["symbol"],
                                "position_side": position["side"], "side": "BUY" if direction == 1 else "SELL",
                                "entry_price": bar["close"], "stop_loss": state["current_stop_loss"],
                                "quantity": state["initial_quantity"] * 0.25,
                                "roll_layer": int(state.get("roll_layer") or 0) + 1})
    unrealized = position["unrealized_pnl"] if position else 0.0
    return {"gross_realized_pnl": gross, "fees": fees, "funding": funding,
            "net_realized_pnl": gross - fees - funding, "unrealized_pnl": unrealized,
            "equity_change": gross - fees - funding + unrealized, "events": events,
            "open_position": position, "pending_actions": pending,
            "assumptions": "next_open_fills; prior_stop_first; close_only_trailing; no_depth_model"}


if __name__ == "__main__":
    import argparse
    import json
    from pathlib import Path

    parser = argparse.ArgumentParser(description="Replay timestamped signals through production lifecycle rules")
    parser.add_argument("input", type=Path, help="JSON containing bars, signals and optional config")
    parser.add_argument("--fee-rate", type=float, default=0.0005)
    parser.add_argument("--slippage", type=float, default=0.001)
    arguments = parser.parse_args()
    payload = json.loads(arguments.input.read_text(encoding="utf-8"))
    print(json.dumps(replay_lifecycle(payload["bars"], payload["signals"],
                                     config=payload.get("config"), fee_rate=arguments.fee_rate,
                                     slippage=arguments.slippage), indent=2, default=str))
