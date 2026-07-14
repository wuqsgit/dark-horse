FEATURE_NAMES = (
    "score", "trend_score", "entry_alpha", "hold_alpha", "relative_strength",
    "ret_15m", "return_6h", "return_24h", "atr_ratio", "volatility_score",
    "ema20_50_ratio", "volume_change_pct", "spot_volume_ratio_6h",
    "futures_volume_ratio_6h", "volume_sync_score", "funding_rate",
    "oi_change_pct", "spread_pct", "depth_ratio_score", "p_drawdown",
    "market_phase_confidence", "category_code",
)

CATEGORY_CODES = {
    "alpha": 1, "core_bluechip": 2, "large_cap": 3, "fundamental": 4,
    "narrative": 5, "meme": 6, "discovery": 7,
}


def canonical_features(features: dict, category: str | None = None) -> dict:
    source = features or {}
    result = {}
    for name in FEATURE_NAMES:
        if name == "category_code":
            result[name] = float(CATEGORY_CODES.get(str(category or "").lower(), 0))
            continue
        value = source.get(name, 0)
        try:
            result[name] = float(value or 0)
        except (TypeError, ValueError):
            result[name] = 0.0
    return result


def vectorize(features: dict, category: str | None = None) -> list[float]:
    values = canonical_features(features, category)
    return [values[name] for name in FEATURE_NAMES]
