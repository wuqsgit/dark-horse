from __future__ import annotations

from typing import Any, Iterable, Mapping


FEATURE_SCHEMA_VERSION = 2

FEATURE_NAMES = (
    "score", "trend_score", "entry_alpha", "hold_alpha", "relative_strength",
    "ret_15m", "return_6h", "return_24h", "atr_ratio", "volatility_score",
    "ema20_50_ratio", "volume_change_pct", "spot_volume_ratio_6h",
    "futures_volume_ratio_6h", "volume_sync_score", "funding_rate",
    "oi_change_pct", "spread_pct", "depth_ratio_score", "p_drawdown",
    "market_phase_confidence", "category_code",
)

MODEL_FEATURE_NAMES = tuple(name for name in FEATURE_NAMES if name != "category_code")

CATEGORY_CODES = {
    "alpha": 1, "core_bluechip": 2, "large_cap": 3, "fundamental": 4,
    "narrative": 5, "meme": 6, "discovery": 7,
}

FEATURE_PATHS = {
    "score": (
        "score", "alpha_score", "composite_score", "discovery_score",
        "alpha.score", "score_layers.display_score",
    ),
    "trend_score": (
        "trend_score", "technical.trend_score", "alpha_trend.trend_score",
        "alpha_trend.trend_continuation_score", "volume_price.trend_score",
    ),
    "entry_alpha": ("entry_alpha", "technical.entry_alpha"),
    "hold_alpha": ("hold_alpha", "technical.hold_alpha"),
    "relative_strength": (
        "relative_strength", "market_strength.score", "technical.relative_strength",
    ),
    "ret_15m": ("ret_15m", "returns.ret_15m", "technical.ret_15m", "technical.return_15m"),
    "return_6h": (
        "return_6h", "ret_6h", "returns.ret_6h", "technical.return_6h",
        "alpha.actual_return_6h",
    ),
    "return_24h": (
        "return_24h", "ret_24h", "pct_24h", "returns.pct_24h",
        "technical.return_24h", "alpha.actual_return_24h",
    ),
    "atr_ratio": ("atr_ratio", "atr_pct", "technical.atr_ratio", "technical.atr_pct"),
    "volatility_score": (
        "volatility_score", "technical.volatility_score", "technical.vol_quality_score",
    ),
    "ema20_50_ratio": ("ema20_50_ratio", "technical.ema20_50_ratio"),
    "volume_change_pct": (
        "volume_change_pct", "technical.volume_change_pct", "volume.volume_change_pct",
    ),
    "spot_volume_ratio_6h": (
        "spot_volume_ratio_6h", "alpha_spot_volume_ratio_6h",
        "dual_market_volume.alpha_spot_volume_ratio_6h",
        "volume.alpha_volume_growth_6h", "volume.volume_growth_6h",
    ),
    "futures_volume_ratio_6h": (
        "futures_volume_ratio_6h", "dual_market_volume.futures_volume_ratio_6h",
        "futures_sync.futures_volume_growth_6h",
    ),
    "volume_sync_score": (
        "volume_sync_score", "sync_score", "dual_market_volume.volume_sync_score",
        "volume_price.sync_score",
    ),
    "funding_rate": ("funding_rate", "futures.funding_rate", "futures_sync.funding_rate"),
    "oi_change_pct": (
        "oi_change_pct", "futures.oi_change_pct", "futures_sync.oi_change_pct",
        "futures_sync.oi_change_4h",
    ),
    "spread_pct": ("spread_pct", "depth.spread_pct", "orderbook.spread_pct"),
    "depth_ratio_score": (
        "depth_ratio_score", "depth.depth_ratio_score", "orderbook.depth_ratio_score",
        "depth.bid_ask_ratio",
    ),
    "p_drawdown": ("p_drawdown", "alpha.p_drawdown", "risk.p_drawdown"),
    "market_phase_confidence": (
        "market_phase_confidence", "market_phase.confidence",
    ),
}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _path_value(source: Mapping[str, Any], path: str) -> Any:
    value: Any = source
    for part in path.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return None
        value = value[part]
    return value


def _first_number(sources: Iterable[Mapping[str, Any]], paths: tuple[str, ...]) -> float | None:
    for source in sources:
        for path in paths:
            value = _number(_path_value(source, path))
            if value is not None:
                return value
    return None


def extract_feature_payload(
    *sources: Mapping[str, Any] | None,
    category: str | None = None,
) -> tuple[dict[str, float], dict[str, Any]]:
    usable_sources: list[Mapping[str, Any]] = []
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        usable_sources.append(source)
        nested = source.get("raw_features")
        if isinstance(nested, Mapping):
            usable_sources.append(nested)
    values: dict[str, float] = {}
    present: list[str] = []
    for name in MODEL_FEATURE_NAMES:
        value = _first_number(usable_sources, FEATURE_PATHS[name])
        if value is None:
            values[name] = 0.0
        else:
            values[name] = value
            present.append(name)
    values["category_code"] = float(CATEGORY_CODES.get(str(category or "").lower(), 0))

    missing = [name for name in MODEL_FEATURE_NAMES if name not in present]
    quality = {
        "schema_version": FEATURE_SCHEMA_VERSION,
        "present_count": len(present),
        "total_count": len(MODEL_FEATURE_NAMES),
        "coverage": round(len(present) / len(MODEL_FEATURE_NAMES), 4),
        "present_features": present,
        "missing_features": missing,
    }
    return values, quality


def canonical_features(features: dict, category: str | None = None) -> dict[str, float]:
    values, _ = extract_feature_payload(features or {}, category=category)
    return values


def vectorize(features: dict, category: str | None = None) -> list[float]:
    values = canonical_features(features, category)
    return [values[name] for name in FEATURE_NAMES]
