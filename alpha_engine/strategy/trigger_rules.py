"""Price-action trigger and invalidation checks for Alpha Strategy V2."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from alpha_engine.strategy.models import StateRecord


def _num(features: Mapping, name: str, default=None):
    value = features.get(name)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class TriggerEvaluation:
    trigger_detected: bool
    overheated: bool
    acceptance_confirmed: bool
    retest_confirmed: bool
    invalidated: bool
    reasons: tuple[str, ...]


def evaluate_trigger(
    features: Mapping,
    state: StateRecord | None = None,
) -> TriggerEvaluation:
    ret_15m = _num(features, "ret_15m", 0.0)
    ret_1h = _num(features, "ret_1h", 0.0)
    ret_6h = _num(features, "ret_6h", 0.0)
    volume_ratio = _num(features, "quote_volume_ratio_15m", 0.0)
    close_location = _num(features, "close_location", 0.0)
    upper_wick = _num(features, "upper_wick_ratio", 1.0)
    breakout_distance = _num(features, "breakout_distance_pct", -999.0)
    price_efficiency = _num(features, "price_efficiency_score")
    current_price = _num(features, "current_price")
    contraction = _num(features, "pre_breakout_volume_contraction")
    reasons = []

    overheated = ret_15m > 8 or ret_1h > 15 or ret_6h > 30
    if overheated:
        reasons.append("short_term_move_overheated")
    if close_location < 0.65:
        reasons.append("weak_close_location")
    if upper_wick > 0.35:
        reasons.append("upper_wick_rejection")
    if volume_ratio < 1.8:
        reasons.append("trigger_volume_insufficient")
    if breakout_distance <= 0:
        reasons.append("price_not_outside_setup_range")
    if price_efficiency is not None and price_efficiency < 60:
        reasons.append("price_efficiency_too_low")

    ignition = bool(
        ret_15m >= 2.5
        and volume_ratio >= 1.8
        and close_location >= 0.65
        and upper_wick <= 0.35
        and breakout_distance > 0
        and (price_efficiency is None or price_efficiency >= 60)
    )
    normal_ignition = ignition and not overheated and ret_15m <= 8
    if normal_ignition:
        reasons.append("normal_ignition_confirmed")
    elif ignition and overheated:
        reasons.append("overheated_ignition_detected")

    invalidated = bool(
        state
        and state.invalidation_price is not None
        and current_price is not None
        and current_price < float(state.invalidation_price)
    )
    if invalidated:
        reasons.append("price_below_invalidation")

    acceptance = bool(
        state
        and state.breakout_level is not None
        and current_price is not None
        and current_price >= float(state.breakout_level)
        and close_location >= 0.55
        and upper_wick <= 0.45
    )
    if acceptance:
        reasons.append("breakout_level_held")

    retest = bool(
        state
        and state.breakout_level is not None
        and current_price is not None
        and current_price >= float(state.breakout_level)
        and contraction is not None
        and contraction <= 1.0
        and ret_15m >= 0
    )
    if retest:
        reasons.append("retest_held_on_contracted_volume")

    return TriggerEvaluation(
        trigger_detected=ignition,
        overheated=overheated,
        acceptance_confirmed=acceptance,
        retest_confirmed=retest,
        invalidated=invalidated,
        reasons=tuple(reasons),
    )
