from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any, Mapping


@dataclass(frozen=True)
class RollDecision:
    eligible: bool
    status: str
    current_r: float = 0.0


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

    if int(_number(state.get("roll_layer"))) >= 1:
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
    trigger_r = _number(config.get("trigger_r")) or 1.5
    if current_r < trigger_r:
        return RollDecision(False, "waiting_1_5r", current_r)

    ema20 = _number(technical.get("ema20"))
    slope = _number(technical.get("ema20_slope"))
    trend_ok = (mark > ema20 and slope > 0) if side == "LONG" else (mark < ema20 and slope < 0)
    if ema20 <= 0 or not trend_ok:
        return RollDecision(False, "trend_not_confirmed", current_r)

    is_alpha = str(position.get("strategy_source") or "").lower() == "alpha"
    profile = str(state.get("alpha_profile") or position.get("alpha_profile") or "").lower()
    if is_alpha and profile == "high_risk_watch":
        return RollDecision(False, "alpha_profile_blocked", current_r)
    if is_alpha and not alpha_sync:
        return RollDecision(False, "alpha_not_synced", current_r)

    return RollDecision(True, "ready", current_r)


def calculate_roll_quantity(
    initial_quantity: float,
    exchange_info: Mapping[str, Any],
    mark_price: float,
    config: Mapping[str, Any],
) -> float:
    step = Decimal(str(exchange_info.get("step_size") or "0"))
    if step <= 0:
        return 0.0
    raw = Decimal(str(initial_quantity)) * Decimal(str(config.get("add_initial_qty_pct", 0.25)))
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
