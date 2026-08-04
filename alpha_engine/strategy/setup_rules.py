"""Broad, deterministic setup screening before AI ranking."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


def _num(features: Mapping, name: str, default=None):
    value = features.get(name)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class SetupEvaluation:
    detected: bool
    setup_type: str | None
    score: float
    reasons: tuple[str, ...]
    scores: Mapping[str, float]


def detect_setup(features: Mapping) -> SetupEvaluation:
    """Select one broad setup without granting entry permission."""
    range_2h = _num(features, "range_2h_pct")
    volume_1h = _num(features, "quote_volume_ratio_1h")
    absorption = _num(features, "absorption_score")
    ret_2h = _num(features, "ret_2h")
    ema_slope_1h = _num(features, "ema20_slope_1h")
    ema_distance_15m = _num(features, "ema20_distance_15m")
    distance_high_24h = _num(features, "distance_from_high_24h")
    contraction = _num(features, "pre_breakout_volume_contraction")
    higher_lows_1h = _num(features, "higher_lows_6x1h")
    close_location = _num(features, "close_location")
    ret_15m = _num(features, "ret_15m")
    volume_15m = _num(features, "quote_volume_ratio_15m")

    scores = {"accumulation": 0.0, "continuation": 0.0, "reclaim": 0.0}
    reasons = {name: [] for name in scores}

    def award(name: str, condition: bool, points: float, reason: str):
        if condition:
            scores[name] += points
            reasons[name].append(reason)

    award(
        "accumulation",
        range_2h is not None and range_2h <= 4.0,
        25,
        "two_hour_range_compressed",
    )
    award(
        "accumulation",
        volume_1h is not None and volume_1h >= 1.5,
        25,
        "turnover_expanded_while_compressed",
    )
    award(
        "accumulation",
        absorption is not None and absorption >= 60,
        30,
        "sell_pressure_absorbed",
    )
    award(
        "accumulation",
        ret_2h is not None and -3.0 <= ret_2h <= 3.0,
        20,
        "price_remained_stable",
    )

    award(
        "continuation",
        ema_slope_1h is not None and ema_slope_1h > 0,
        25,
        "hourly_ema_rising",
    )
    award(
        "continuation",
        ema_distance_15m is not None and ema_distance_15m >= 0,
        15,
        "price_above_short_ema",
    )
    award(
        "continuation",
        distance_high_24h is not None and -18 <= distance_high_24h <= -5,
        25,
        "healthy_pullback_from_day_high",
    )
    award(
        "continuation",
        contraction is not None and contraction <= 1.10,
        20,
        "volume_contracted_on_pullback",
    )
    award(
        "continuation",
        higher_lows_1h is not None and higher_lows_1h >= 3,
        15,
        "hourly_lows_rising",
    )

    award(
        "reclaim",
        distance_high_24h is not None and -25 <= distance_high_24h <= -8,
        25,
        "deep_pullback",
    )
    award(
        "reclaim",
        ret_15m is not None and ret_15m >= 2.0,
        25,
        "fast_price_reclaim",
    )
    award(
        "reclaim",
        volume_15m is not None and volume_15m >= 1.8,
        25,
        "reclaim_volume_expanded",
    )
    award(
        "reclaim",
        close_location is not None and close_location >= 0.65,
        25,
        "reclaim_closed_strong",
    )

    setup_type = max(scores, key=scores.get)
    score = min(100.0, scores[setup_type])
    detected = score >= 60
    return SetupEvaluation(
        detected=detected,
        setup_type=setup_type if detected else None,
        score=score,
        reasons=tuple(reasons[setup_type]) if detected else ("no_setup_above_floor",),
        scores=scores,
    )
