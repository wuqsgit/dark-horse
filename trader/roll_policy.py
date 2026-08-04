from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any, Mapping


@dataclass(frozen=True)
class RollDecision:
    eligible: bool
    status: str
    current_r: float = 0.0
    cycle_peak_price: float = 0.0
    pullback_armed: bool = False
    trigger_mode: str = ""


def _number(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def evaluate_roll(
    position: Mapping[str, Any],
    state: Mapping[str, Any],
    technical: Mapping[str, Any],
    alpha_sync: bool,
    config: Mapping[str, Any],
) -> RollDecision:
    required = ("initial_quantity", "initial_stop_loss", "atr_value")
    if any(_number(state.get(field)) <= 0 for field in required):
        return RollDecision(False, "roll_state_incomplete")

    roll_layer = int(_number(state.get("roll_layer")))
    max_layers = int(_number(config.get("max_layers")) or 1)
    if roll_layer >= max_layers:
        return RollDecision(False, "roll_completed")
    if not bool(state.get("tp1_hit")):
        return RollDecision(False, "waiting_tp1")

    side = str(position.get("side") or "").upper()
    entry = _number(position.get("entry_price"))
    mark = _number(position.get("mark_price"))
    stop = _number(state.get("initial_stop_loss"))
    risk = abs(entry - stop)
    if side not in {"LONG", "SHORT"} or entry <= 0 or mark <= 0 or risk <= 0:
        return RollDecision(False, "roll_state_incomplete")

    favorable_move = mark - entry if side == "LONG" else entry - mark
    current_r = favorable_move / risk
    cycle_peak = _number(state.get("roll_cycle_peak_price"))
    pullback_armed = bool(state.get("roll_pullback_armed"))
    layer_triggers = config.get("layer_trigger_r") or []
    try:
        trigger_r = _number(layer_triggers[roll_layer])
    except (IndexError, TypeError):
        trigger_r = 0.0
    if trigger_r <= 0:
        trigger_r = (_number(config.get("trigger_r")) or 1.5) + roll_layer
    trigger_label = f"{trigger_r:g}".replace(".", "_")

    trigger_mode = ""
    if roll_layer == 0:
        if current_r < trigger_r:
            return RollDecision(False, f"waiting_{trigger_label}r", current_r)
        trigger_mode = "sustained_profit"
    else:
        atr = _number(state.get("atr_value"))
        anchor = _number(state.get("roll_price")) or entry
        cycle_peak = cycle_peak or anchor
        cycle_peak = max(cycle_peak, mark) if side == "LONG" else min(cycle_peak, mark)
        if current_r >= trigger_r:
            trigger_mode = "sustained_profit"
        else:
            pullback_atr = (
                (cycle_peak - mark) / atr
                if side == "LONG"
                else (mark - cycle_peak) / atr
            )
            min_pullback_atr = _number(config.get("repeat_pullback_atr")) or 0.75
            pullback_armed = pullback_armed or pullback_atr >= min_pullback_atr
            if not pullback_armed:
                return RollDecision(
                    False, f"waiting_{trigger_label}r_or_healthy_pullback",
                    current_r, cycle_peak, False,
                )

            repeat_min_r = _number(config.get("repeat_min_r")) or 1.0
            if current_r < repeat_min_r:
                return RollDecision(
                    False, "repeat_profit_buffer_too_low",
                    current_r, cycle_peak, True,
                )
            recovery_gap_atr = (
                (cycle_peak - mark) / atr
                if side == "LONG"
                else (mark - cycle_peak) / atr
            )
            recover_to_peak_atr = _number(config.get("repeat_recover_to_peak_atr")) or 0.25
            if recovery_gap_atr > recover_to_peak_atr:
                return RollDecision(
                    False, "waiting_pullback_recovery",
                    current_r, cycle_peak, True,
                )
            trigger_mode = "pullback_recovery"

    ema20 = _number(technical.get("ema20"))
    slope = _number(technical.get("ema20_slope"))
    trend_ok = (mark > ema20 and slope > 0) if side == "LONG" else (mark < ema20 and slope < 0)
    if ema20 <= 0 or not trend_ok:
        return RollDecision(
            False, "trend_not_confirmed", current_r, cycle_peak, pullback_armed,
        )

    is_alpha = str(position.get("strategy_source") or "").lower() == "alpha"
    profile = str(state.get("alpha_profile") or position.get("alpha_profile") or "").lower()
    if is_alpha and profile == "high_risk_watch":
        return RollDecision(
            False, "alpha_profile_blocked", current_r, cycle_peak, pullback_armed,
        )
    if is_alpha and not alpha_sync:
        return RollDecision(
            False, "alpha_not_synced", current_r, cycle_peak, pullback_armed,
        )

    return RollDecision(
        True, "ready", current_r, cycle_peak, pullback_armed, trigger_mode,
    )


def calculate_roll_quantity(
    initial_quantity: float,
    exchange_info: Mapping[str, Any],
    mark_price: float,
    config: Mapping[str, Any],
    roll_layer: int = 1,
    current_quantity: float | None = None,
) -> float:
    step = Decimal(str(exchange_info.get("step_size") or "0"))
    if step <= 0:
        return 0.0
    layer_pcts = config.get("layer_add_initial_qty_pct") or []
    try:
        layer_pct = layer_pcts[max(0, int(roll_layer) - 1)]
    except (IndexError, TypeError, ValueError):
        layer_pct = config.get("add_initial_qty_pct", 0.25)
    raw = Decimal(str(initial_quantity)) * Decimal(str(layer_pct))
    if current_quantity is not None:
        max_multiple = Decimal(str(config.get("max_total_qty_multiple", 1.1)))
        remaining = (
            Decimal(str(initial_quantity)) * max_multiple
            - Decimal(str(current_quantity))
        )
        raw = min(raw, max(Decimal("0"), remaining))
    quantity = (raw / step).to_integral_value(rounding=ROUND_DOWN) * step
    qty = float(quantity)
    min_qty = _number(exchange_info.get("min_qty"))
    min_notional = _number(exchange_info.get("min_notional"))
    if qty < min_qty or qty * _number(mark_price) < min_notional:
        return 0.0
    return qty


def calculate_protected_stop(side: str, blended_entry: float, config: Mapping[str, Any]) -> float:
    buffer_pct = _number(config.get("break_even_buffer_pct")) or 0.0015
    multiplier = 1 + buffer_pct if str(side).upper() == "LONG" else 1 - buffer_pct
    return float(blended_entry) * multiplier


def is_residual_position(
    quantity: float,
    mark_price: float,
    leverage: float,
    exchange_info: Mapping[str, Any],
    config: Mapping[str, Any],
) -> bool:
    notional = abs(_number(quantity) * _number(mark_price))
    lev = max(_number(leverage), 1.0)
    margin = notional / lev
    min_margin = _number(config.get("min_remaining_margin")) or 5.0
    min_notional = _number(exchange_info.get("min_notional"))
    multiplier = _number(config.get("min_notional_multiplier")) or 1.5
    return margin < min_margin or (min_notional > 0 and notional < min_notional * multiplier)
