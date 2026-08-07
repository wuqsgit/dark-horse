"""Leakage-safe Alpha Feature Schema V3 builder."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from statistics import mean, median, pstdev
from typing import Any, Iterable, Mapping

from alpha_engine.strategy.market_data import (
    oi_change_at_horizon,
    resolve_market_env,
)


FEATURE_SCHEMA_VERSION = 4


@dataclass(frozen=True)
class AlphaFeatureSnapshot:
    snapshot_id: str
    market_env: str
    alpha_symbol: str | None
    futures_symbol: str
    candle_close_time: datetime
    feature_schema_version: int
    features: Mapping[str, float | None]
    quality: Mapping[str, Any]


def _parse_time(value) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _number(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _closed_rows(
    rows: Iterable[Mapping],
    cutoff: datetime,
) -> list[dict]:
    result = []
    for raw in rows or []:
        row = dict(raw)
        timestamp = _parse_time(row.get("time"))
        if timestamp is None or timestamp >= cutoff:
            continue
        if row.get("is_closed") is not None and not bool(row.get("is_closed")):
            continue
        row["_time"] = timestamp
        result.append(row)
    return sorted(result, key=lambda item: item["_time"])


def _pct_change(rows: list[dict], bars: int) -> float | None:
    if len(rows) <= bars:
        return None
    current = _number(rows[-1].get("close"))
    old = _number(rows[-bars - 1].get("close"))
    if not current or not old:
        return None
    return (current / old - 1) * 100


def _range_pct(rows: list[dict], bars: int) -> float | None:
    subset = rows[-bars:]
    highs = [_number(row.get("high")) for row in subset]
    lows = [_number(row.get("low")) for row in subset]
    highs = [value for value in highs if value is not None]
    lows = [value for value in lows if value is not None and value > 0]
    if not highs or not lows:
        return None
    return (max(highs) / min(lows) - 1) * 100


def _ema(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (period + 1)
    output = [values[0]]
    for value in values[1:]:
        output.append(value * alpha + output[-1] * (1 - alpha))
    return output


def _ema_slope(rows: list[dict], period: int = 20, lookback: int = 4) -> float | None:
    closes = [_number(row.get("close")) for row in rows]
    closes = [value for value in closes if value is not None and value > 0]
    if len(closes) < max(period, lookback + 1):
        return None
    series = _ema(closes, period)
    old = series[-lookback - 1]
    return (series[-1] / old - 1) * 100 if old else None


def _ema_distance(rows: list[dict], period: int = 20) -> float | None:
    closes = [_number(row.get("close")) for row in rows]
    closes = [value for value in closes if value is not None and value > 0]
    if len(closes) < period:
        return None
    value = _ema(closes, period)[-1]
    return (closes[-1] / value - 1) * 100 if value else None


def _ema_ratio(rows: list[dict], fast: int = 20, slow: int = 50) -> float | None:
    closes = [_number(row.get("close")) for row in rows]
    closes = [value for value in closes if value is not None and value > 0]
    if len(closes) < slow:
        return None
    fast_value = _ema(closes, fast)[-1]
    slow_value = _ema(closes, slow)[-1]
    return fast_value / slow_value if slow_value else None


def _atr_pct(rows: list[dict], period: int = 14) -> float | None:
    if len(rows) < period + 1:
        return None
    ranges = []
    for previous, current in zip(rows[-period - 1:-1], rows[-period:]):
        high = _number(current.get("high"))
        low = _number(current.get("low"))
        previous_close = _number(previous.get("close"))
        if high is None or low is None or previous_close is None:
            continue
        ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    close = _number(rows[-1].get("close"))
    if not ranges or not close:
        return None
    return sum(ranges) / len(ranges) / close * 100


def _higher_count(rows: list[dict], field: str, bars: int) -> float | None:
    values = [_number(row.get(field)) for row in rows[-bars:]]
    if len(values) < bars or any(value is None for value in values):
        return None
    return float(sum(1 for old, new in zip(values, values[1:]) if new > old))


def _volume_ratio(rows: list[dict], recent_bars: int, baseline_bars: int) -> float | None:
    if len(rows) < recent_bars + baseline_bars:
        return None
    recent = [_number(row.get("quote_vol")) for row in rows[-recent_bars:]]
    baseline = [
        _number(row.get("quote_vol"))
        for row in rows[-recent_bars - baseline_bars:-recent_bars]
    ]
    recent = [value for value in recent if value is not None]
    baseline = [value for value in baseline if value is not None and value > 0]
    if not recent or not baseline:
        return None
    baseline_value = median(baseline)
    return median(recent) / baseline_value if baseline_value else None


def _slope(values: list[float]) -> float | None:
    if len(values) < 2:
        return None
    first = values[0]
    return (values[-1] / first - 1) if first else None


def _zscore(values: list[float], current: float | None = None) -> float | None:
    cleaned = [float(value) for value in values if value is not None]
    if len(cleaned) < 5:
        return None
    deviation = pstdev(cleaned)
    if deviation <= 0:
        return 0.0
    return ((float(current) if current is not None else cleaned[-1]) - mean(cleaned)) / deviation


def _percentile(values: list[float], fraction: float) -> float | None:
    cleaned = sorted(float(value) for value in values if value is not None)
    if not cleaned:
        return None
    index = min(
        len(cleaned) - 1,
        max(0, int(math.ceil((len(cleaned) - 1) * float(fraction)))),
    )
    return cleaned[index]


def _latest_before(
    rows: Iterable[Mapping],
    cutoff: datetime,
    *,
    time_field: str = "time",
) -> list[dict]:
    result = []
    for raw in rows or []:
        row = dict(raw)
        timestamp = _parse_time(row.get(time_field))
        if timestamp is None or timestamp >= cutoff:
            continue
        row["_time"] = timestamp
        result.append(row)
    return sorted(result, key=lambda item: item["_time"])


def _return_for_rows(rows: list[dict], bars: int = 1) -> float | None:
    return _pct_change(rows, bars)


def _safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _distance_from_high(rows: list[dict], bars: int) -> float | None:
    subset = rows[-bars:]
    close = _number(rows[-1].get("close")) if rows else None
    highs = [_number(row.get("high")) for row in subset]
    highs = [value for value in highs if value is not None]
    if not close or not highs:
        return None
    return (close / max(highs) - 1) * 100


def _candle_quality(row: Mapping) -> dict[str, float | None]:
    open_ = _number(row.get("open"))
    high = _number(row.get("high"))
    low = _number(row.get("low"))
    close = _number(row.get("close"))
    if None in (open_, high, low, close) or high <= low or not open_:
        return {
            "body_return_pct": None,
            "true_range_pct": None,
            "close_location": None,
            "upper_wick_ratio": None,
            "lower_wick_ratio": None,
            "body_to_range_ratio": None,
        }
    width = high - low
    body_high = max(open_, close)
    body_low = min(open_, close)
    return {
        "body_return_pct": (close / open_ - 1) * 100,
        "true_range_pct": width / open_ * 100,
        "close_location": (close - low) / width,
        "upper_wick_ratio": (high - body_high) / width,
        "lower_wick_ratio": (body_low - low) / width,
        "body_to_range_ratio": abs(close - open_) / width,
    }


def build_alpha_feature_snapshot(
    *,
    alpha_symbol: str | None,
    futures_symbol: str,
    market_env: str,
    cutoff_time: datetime,
    candles_15m: Iterable[Mapping],
    candles_1h: Iterable[Mapping],
    spot_candles_15m: Iterable[Mapping] | None = None,
    futures_snapshots: Iterable[Mapping] | None = None,
    orderbook_snapshots: Iterable[Mapping] | None = None,
    market_context: Mapping[str, Any] | None = None,
    listing_time: datetime | str | None = None,
    square_sentiment: Mapping[str, Any] | None = None,
    alpha_discovery_score: float | None = None,
) -> AlphaFeatureSnapshot:
    cutoff = _parse_time(cutoff_time)
    if cutoff is None:
        raise ValueError("cutoff_time is required")
    env = resolve_market_env(market_env)
    c15 = _closed_rows(candles_15m, cutoff)
    c1h = _closed_rows(candles_1h, cutoff)
    spot15 = _closed_rows(spot_candles_15m or [], cutoff)
    derivative_rows = _latest_before(futures_snapshots or [], cutoff)
    depth_rows = _latest_before(
        orderbook_snapshots or [],
        cutoff,
        time_field="timestamp",
    )
    if not c15:
        raise ValueError("closed 15m candles are required")

    current = c15[-1]
    current_price = _number(current.get("close"))
    atr15 = _atr_pct(c15)
    atr1h = _atr_pct(c1h)
    candle = _candle_quality(current)
    volume_values = [
        value
        for value in (_number(row.get("quote_vol")) for row in c15[-3:])
        if value is not None
    ]
    taker = _number(current.get("taker_buy_quote_vol"))
    quote_volume = _number(current.get("quote_vol"))
    taker_ratio = (
        taker / quote_volume
        if taker is not None and quote_volume is not None and quote_volume > 0
        else None
    )
    volume_ratio_15m = _volume_ratio(c15, 1, 24)
    normalized_move = (
        abs(_pct_change(c15, 1) or 0) / atr15
        if atr15 is not None and atr15 > 0
        else None
    )
    raw_price_efficiency = (
        normalized_move / math.log1p(max(volume_ratio_15m, 1e-9))
        if normalized_move is not None
        and volume_ratio_15m is not None
        and volume_ratio_15m > 0
        else None
    )
    price_efficiency = (
        100.0 * (1.0 - math.exp(-raw_price_efficiency))
        if raw_price_efficiency is not None
        else None
    )
    range_2h = _range_pct(c15, 8)
    volume_activity = min(100.0, max(0.0, (volume_ratio_15m or 0) * 35))
    stability = (
        max(0.0, 100.0 - (range_2h / max(atr15 or 1.0, 0.1)) * 12)
        if range_2h is not None
        else None
    )
    low_hold = _higher_count(c15, "low", 8)
    low_hold_score = (low_hold / 7 * 100) if low_hold is not None else None
    absorption_parts = [
        value for value in (volume_activity, stability, low_hold_score)
        if value is not None
    ]
    absorption = (
        sum(absorption_parts) / len(absorption_parts)
        if len(absorption_parts) == 3
        else None
    )
    prior_breakout_rows = c15[-9:-1]
    prior_highs = [
        value
        for value in (_number(row.get("high")) for row in prior_breakout_rows)
        if value is not None
    ]
    breakout_level = max(prior_highs) if prior_highs else None
    breakout_distance = (
        (current_price / breakout_level - 1) * 100
        if current_price is not None and breakout_level
        else None
    )
    closes_above_breakout = (
        float(
            sum(
                1
                for row in c15[-3:]
                if (_number(row.get("close")) or 0) > breakout_level
            )
        )
        if breakout_level is not None
        else None
    )
    recent_highs = [
        value
        for value in (_number(row.get("high")) for row in c15[-8:])
        if value is not None
    ]
    recent_lows = [
        value
        for value in (_number(row.get("low")) for row in c15[-8:])
        if value is not None
    ]
    previous_close = _number(c15[-2].get("close")) if len(c15) >= 2 else None
    current_open = _number(current.get("open"))
    gap_from_previous_close = (
        (current_open / previous_close - 1) * 100
        if current_open is not None and previous_close
        else None
    )
    breakout_hold_bars = None
    if breakout_level is not None:
        breakout_hold_bars = 0.0
        for row in reversed(c15):
            close = _number(row.get("close"))
            if close is None or close < breakout_level:
                break
            breakout_hold_bars += 1.0
    retest_depth = (
        (min(recent_lows) / breakout_level - 1) * 100
        if recent_lows and breakout_level
        else None
    )
    quote_history = [
        value
        for value in (_number(row.get("quote_vol")) for row in c15[-96:])
        if value is not None
    ]
    taker_ratios = []
    for row in c15[-8:]:
        quote = _number(row.get("quote_vol"))
        buy = _number(row.get("taker_buy_quote_vol"))
        if quote and buy is not None:
            taker_ratios.append(buy / quote)
    trades = _number(current.get("trades"))
    average_trade_size = (
        quote_volume / trades
        if quote_volume is not None and trades is not None and trades > 0
        else None
    )
    historical_trade_sizes = []
    for row in c15[-25:-1]:
        row_quote = _number(row.get("quote_vol"))
        row_trades = _number(row.get("trades"))
        if row_quote is not None and row_trades is not None and row_trades > 0:
            historical_trade_sizes.append(row_quote / row_trades)
    average_trade_size_ratio = (
        average_trade_size / median(historical_trade_sizes)
        if average_trade_size is not None
        and historical_trade_sizes
        and median(historical_trade_sizes) > 0
        else None
    )

    latest_derivative = derivative_rows[-1] if derivative_rows else None
    oi_changes = {
        hours: oi_change_at_horizon(
            derivative_rows,
            hours=hours,
            as_of=latest_derivative["_time"] if latest_derivative else None,
        )
        if latest_derivative
        else None
        for hours in (0.25, 1, 4, 24)
    }
    funding_values = [
        value
        for value in (
            _number(row.get("funding_rate"))
            for row in derivative_rows[-96:]
        )
        if value is not None
    ]
    funding_rate = (
        _number(latest_derivative.get("funding_rate"))
        if latest_derivative
        else None
    )
    mark_price = (
        _number(latest_derivative.get("mark_price"))
        if latest_derivative
        else None
    )
    oi_1h = oi_changes[1]
    futures_ret_1h = _pct_change(c15, 4)
    if futures_ret_1h is None or oi_1h is None:
        price_oi_quadrant = None
    elif futures_ret_1h >= 0 and oi_1h >= 0:
        price_oi_quadrant = 1.0
    elif futures_ret_1h >= 0 and oi_1h < 0:
        price_oi_quadrant = 2.0
    elif futures_ret_1h < 0 and oi_1h >= 0:
        price_oi_quadrant = 3.0
    else:
        price_oi_quadrant = 4.0

    alpha_spot_ret_15m = _return_for_rows(spot15, 1)
    futures_ret_15m = _return_for_rows(c15, 1)
    spot_volume_ratio = _volume_ratio(spot15, 1, 24)
    futures_volume_ratio = volume_ratio_15m
    return_diff = (
        alpha_spot_ret_15m - futures_ret_15m
        if alpha_spot_ret_15m is not None and futures_ret_15m is not None
        else None
    )
    volume_sync_score = None
    if spot_volume_ratio is not None and futures_volume_ratio is not None:
        denominator = max(spot_volume_ratio, futures_volume_ratio, 1e-9)
        volume_sync_score = min(spot_volume_ratio, futures_volume_ratio) / denominator

    spreads = [
        value
        for value in (
            _number(row.get("spread_pct"))
            for row in depth_rows
        )
        if value is not None
    ]
    latest_depth = depth_rows[-1] if depth_rows else None
    bid_depth = _number(latest_depth.get("bid_depth")) if latest_depth else None
    ask_depth = _number(latest_depth.get("ask_depth")) if latest_depth else None
    spread_current = (
        _number(latest_depth.get("spread_pct"))
        if latest_depth
        else None
    )
    depth_imbalance = (
        _number(latest_depth.get("imbalance_ratio"))
        if latest_depth
        else None
    )
    imbalance_values = [
        value
        for value in (
            _number(row.get("imbalance_ratio"))
            for row in depth_rows[-15:]
        )
        if value is not None
    ]
    depth_imbalance_stability = (
        max(0.0, 1.0 - pstdev(imbalance_values))
        if len(imbalance_values) >= 3
        else None
    )
    total_depth = (
        bid_depth + ask_depth
        if bid_depth is not None and ask_depth is not None
        else None
    )
    estimated_slippage_probe = (
        max(0.0, (1000.0 / total_depth) * max(spread_current or 0.0001, 0.0001))
        if total_depth and total_depth > 0
        else None
    )
    estimated_slippage_confirmed = (
        max(0.0, (3000.0 / total_depth) * max(spread_current or 0.0001, 0.0001))
        if total_depth and total_depth > 0
        else None
    )
    context = dict(market_context or {})
    square = dict(square_sentiment or {})
    square_bearish_ratio = _number(square.get("bearish_ratio"))
    square_baseline = _number(square.get("baseline_bearish_ratio_24h"))
    liquidation_pressure = _number(context.get("liquidation_pressure"))
    if liquidation_pressure is None:
        # Binance does not expose a stable public historical liquidation feed
        # for every environment. Use a deterministic stress proxy instead of
        # silently training a permanently empty feature: falling OI, downside
        # velocity and exceptional volume are the observable liquidation
        # footprint available at the closed-candle boundary.
        oi_drop_pct = max(0.0, -float(oi_changes[0.25] or 0.0) * 100.0)
        downside_pct = max(0.0, -float(futures_ret_15m or 0.0))
        volume_excess = max(0.0, float(futures_volume_ratio or 0.0) - 1.0)
        liquidation_pressure = min(
            100.0,
            oi_drop_pct * 12.0 + downside_pct * 10.0 + volume_excess * 4.0,
        )
    listing_dt = _parse_time(listing_time) if listing_time is not None else None
    listing_age_hours = (
        max(0.0, (cutoff - listing_dt).total_seconds() / 3600)
        if listing_dt is not None
        else None
    )
    features: dict[str, float | None] = {
        "current_price": current_price,
        "ret_15m": _pct_change(c15, 1),
        "ret_30m": _pct_change(c15, 2),
        "ret_1h": _pct_change(c15, 4),
        "ret_2h": _pct_change(c15, 8),
        "ret_4h": _pct_change(c15, 16),
        "ret_6h": _pct_change(c15, 24),
        "ret_24h": _pct_change(c1h, 24),
        "range_2h_pct": range_2h,
        "base_high_2h": max(recent_highs) if recent_highs else None,
        "base_low_2h": min(recent_lows) if recent_lows else None,
        "range_6h_pct": _range_pct(c15, 24),
        "range_24h_pct": _range_pct(c1h, 24),
        "atr_15m_pct": atr15,
        "atr_1h_pct": atr1h,
        "compression_2h_vs_24h": None,
        "ema20_distance_15m": _ema_distance(c15, 20),
        "ema20_slope_15m": _ema_slope(c15, 20, 4),
        "ema20_50_ratio_1h": _ema_ratio(c1h, 20, 50),
        "ema20_slope_1h": _ema_slope(c1h, 20, 4),
        "higher_lows_8x15m": _higher_count(c15, "low", 8),
        "higher_lows_6x1h": _higher_count(c1h, "low", 6),
        "higher_highs_8x15m": _higher_count(c15, "high", 8),
        "higher_highs_6x1h": _higher_count(c1h, "high", 6),
        "distance_from_high_2h": _distance_from_high(c15, 8),
        "distance_from_high_6h": _distance_from_high(c15, 24),
        "distance_from_high_24h": _distance_from_high(c1h, 24),
        "breakout_level": breakout_level,
        "breakout_distance_pct": breakout_distance,
        "closes_above_breakout_level": closes_above_breakout,
        "gap_from_previous_close": gap_from_previous_close,
        "retest_depth_pct": retest_depth,
        "breakout_hold_bars": breakout_hold_bars,
        "quote_volume_ratio_15m": volume_ratio_15m,
        "quote_volume_ratio_1h": _volume_ratio(c15, 4, 24),
        "quote_volume_ratio_6h": _volume_ratio(c1h, 6, 6),
        "quote_volume_zscore_15m": _zscore(
            quote_history[:-1],
            quote_volume,
        ),
        "quote_volume_slope_3bars": _slope(volume_values),
        "pre_breakout_volume_contraction": _volume_ratio(c15, 8, 16),
        "trade_count_ratio": _volume_ratio(
            [
                {**row, "quote_vol": row.get("trades")}
                for row in c15
            ],
            1,
            24,
        ),
        "average_trade_size_ratio": average_trade_size_ratio,
        "taker_buy_quote_ratio": taker_ratio,
        "taker_buy_ratio_slope": _slope(taker_ratios),
        "price_efficiency_score": price_efficiency,
        "absorption_score": absorption,
        "oi_change_15m": oi_changes[0.25],
        "oi_change_1h": oi_changes[1],
        "oi_change_4h": oi_changes[4],
        "oi_change_24h": oi_changes[24],
        "price_oi_quadrant": price_oi_quadrant,
        "funding_rate": funding_rate,
        "funding_zscore": _zscore(funding_values[:-1], funding_rate),
        "mark_index_basis": (
            (mark_price / current_price - 1) * 100
            if mark_price is not None and current_price
            else None
        ),
        "liquidation_pressure": liquidation_pressure,
        "alpha_spot_return_15m": alpha_spot_ret_15m,
        "futures_return_15m": futures_ret_15m,
        "spot_futures_return_diff": return_diff,
        "spot_volume_ratio_15m": spot_volume_ratio,
        "futures_volume_ratio_15m": futures_volume_ratio,
        "volume_sync_score": volume_sync_score,
        "spot_leads_futures": (
            1.0 if return_diff is not None and return_diff > 0 else 0.0
            if return_diff is not None
            else None
        ),
        "futures_leads_spot": (
            1.0 if return_diff is not None and return_diff < 0 else 0.0
            if return_diff is not None
            else None
        ),
        "spread_pct_current": spread_current,
        "spread_pct_median_15m": median(spreads[-15:]) if spreads else None,
        "spread_pct_p95_1h": _percentile(spreads[-60:], 0.95),
        "bid_depth_usdt": bid_depth,
        "ask_depth_usdt": ask_depth,
        "depth_imbalance": depth_imbalance,
        "depth_imbalance_stability": depth_imbalance_stability,
        "estimated_slippage_probe": estimated_slippage_probe,
        "estimated_slippage_confirmed": estimated_slippage_confirmed,
        "btc_ret_1h": _number(context.get("btc_ret_1h")),
        "btc_ret_6h": _number(context.get("btc_ret_6h")),
        "market_breadth_1h": _number(context.get("market_breadth_1h")),
        "market_breadth_6h": _number(context.get("market_breadth_6h")),
        "alpha_universe_median_return": _number(
            context.get("alpha_universe_median_return")
        ),
        "category_relative_strength": _number(
            context.get("category_relative_strength")
        ),
        "listing_age_hours": listing_age_hours,
        "market_phase_code": _number(context.get("market_phase_code")),
        "setup_type_code": _number(context.get("setup_type_code")),
        "alpha_discovery_score": _number(alpha_discovery_score),
        "square_sentiment_available": 1.0 if square else 0.0,
        "square_bearish_ratio": square_bearish_ratio,
        "square_effective_post_count": _number(
            square.get("effective_post_count")
        ),
        "square_unique_authors": _number(square.get("unique_authors")),
        "square_top3_author_share": _number(
            square.get("top3_author_share")
        ),
        "square_bearish_shift_24h": (
            square_bearish_ratio - square_baseline
            if square_bearish_ratio is not None
            and square_baseline is not None
            else None
        ),
        "square_substantive_risk_count": _number(
            square.get("substantive_risk_count")
        ),
        "square_sentiment_age_minutes": _number(square.get("age_minutes")),
        **candle,
    }
    range_24h = features["range_24h_pct"]
    if range_2h is not None and range_24h is not None and range_24h > 0:
        features["compression_2h_vs_24h"] = range_2h / range_24h

    missing = [name for name, value in features.items() if value is None]
    present = [name for name, value in features.items() if value is not None]
    hard_required = (
        "current_price",
        "atr_15m_pct",
        "quote_volume_ratio_15m",
        "range_2h_pct",
        "breakout_level",
    )
    hard_missing = [name for name in hard_required if features.get(name) is None]
    required_ready = (
        len(c15) >= 32
        and len(c1h) >= 24
        and not hard_missing
    )
    quality = {
        "status": "ready" if required_ready else "insufficient",
        "closed_15m_count": len(c15),
        "closed_1h_count": len(c1h),
        "coverage": round(len(present) / len(features), 4),
        "present_features": present,
        "missing_features": missing,
        "hard_missing_features": hard_missing,
        "sources": {
            "futures_candles": env,
            "alpha_spot_candles": "mainnet" if spot15 else None,
            "futures_snapshots": env if derivative_rows else None,
            "orderbook": "alpha_mainnet" if depth_rows else None,
        },
        "latest_source_times": {
            "futures_15m": c15[-1]["_time"].isoformat(),
            "futures_1h": c1h[-1]["_time"].isoformat() if c1h else None,
            "alpha_spot_15m": spot15[-1]["_time"].isoformat() if spot15 else None,
            "futures_snapshot": (
                derivative_rows[-1]["_time"].isoformat()
                if derivative_rows
                else None
            ),
            "orderbook": depth_rows[-1]["_time"].isoformat() if depth_rows else None,
        },
    }
    # Stored Binance kline timestamps are bar-open times. Strategy identity and
    # deduplication use the actual close boundary.
    candle_close_time = c15[-1]["_time"] + timedelta(minutes=15)
    identity = {
        "market_env": env,
        "alpha_symbol": alpha_symbol,
        "futures_symbol": futures_symbol.upper(),
        "candle_close_time": candle_close_time.isoformat(),
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "features": features,
    }
    snapshot_id = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return AlphaFeatureSnapshot(
        snapshot_id=snapshot_id,
        market_env=env,
        alpha_symbol=alpha_symbol,
        futures_symbol=futures_symbol.upper(),
        candle_close_time=candle_close_time,
        feature_schema_version=FEATURE_SCHEMA_VERSION,
        features=features,
        quality=quality,
    )
