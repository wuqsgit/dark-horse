from __future__ import annotations

import math


FEATURE_SCHEMA_VERSION = 3

FEATURE_NAMES = (
    "ret_15m", "ret_30m", "ret_1h", "ret_2h", "ret_4h", "ret_6h", "ret_24h",
    "range_2h_pct", "range_6h_pct", "range_24h_pct",
    "atr_15m_pct", "atr_1h_pct", "compression_2h_vs_24h",
    "ema20_distance_15m", "ema20_slope_15m",
    "ema20_50_ratio_1h", "ema20_slope_1h",
    "higher_lows_8x15m", "higher_lows_6x1h",
    "higher_highs_8x15m", "higher_highs_6x1h",
    "distance_from_high_2h", "distance_from_high_6h", "distance_from_high_24h",
    "breakout_distance_pct", "closes_above_breakout_level",
    "body_return_pct", "true_range_pct", "close_location",
    "upper_wick_ratio", "lower_wick_ratio", "body_to_range_ratio",
    "gap_from_previous_close", "retest_depth_pct", "breakout_hold_bars",
    "quote_volume_ratio_15m", "quote_volume_ratio_1h", "quote_volume_ratio_6h",
    "quote_volume_zscore_15m", "quote_volume_slope_3bars",
    "pre_breakout_volume_contraction", "trade_count_ratio",
    "average_trade_size_ratio", "taker_buy_quote_ratio",
    "taker_buy_ratio_slope", "price_efficiency_score", "absorption_score",
    "oi_change_15m", "oi_change_1h", "oi_change_4h", "oi_change_24h",
    "price_oi_quadrant", "funding_rate", "funding_zscore", "mark_index_basis",
    "liquidation_pressure", "alpha_spot_return_15m", "futures_return_15m",
    "spot_futures_return_diff", "spot_volume_ratio_15m",
    "futures_volume_ratio_15m", "volume_sync_score",
    "spot_leads_futures", "futures_leads_spot",
    "spread_pct_current", "spread_pct_median_15m", "spread_pct_p95_1h",
    "bid_depth_usdt", "ask_depth_usdt", "depth_imbalance",
    "depth_imbalance_stability", "estimated_slippage_probe",
    "estimated_slippage_confirmed", "btc_ret_1h", "btc_ret_6h",
    "market_breadth_1h", "market_breadth_6h",
    "alpha_universe_median_return", "category_relative_strength",
    "listing_age_hours", "market_phase_code", "setup_type_code",
)


def vectorize_alpha_features(
    features: dict,
    feature_names: tuple[str, ...] | list[str] = FEATURE_NAMES,
) -> list[float]:
    values = []
    for name in feature_names:
        value = features.get(name)
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = float("nan")
        values.append(parsed if math.isfinite(parsed) else float("nan"))
    return values
