"""Binance Square contrarian sentiment rules for Alpha long candidates."""
from __future__ import annotations


def _num(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def evaluate_square_reversal(
    raw_features: dict | None,
    *,
    alpha_score: float,
) -> dict:
    """Return a bounded contrarian candidate; price structure is checked later."""
    raw = raw_features or {}
    sentiment = raw.get("square_sentiment") or {}
    if not sentiment:
        return {
            "candidate": False,
            "confirmed": False,
            "reasons": ["square_sentiment_unavailable"],
            "metrics": {},
        }

    bearish_ratio = _num(sentiment.get("bearish_ratio"))
    post_count = int(_num(sentiment.get("effective_post_count")))
    unique_authors = int(_num(sentiment.get("unique_authors")))
    top3_share = _num(sentiment.get("top3_author_share"), 1.0)
    baseline = _num(sentiment.get("baseline_bearish_ratio_24h"))
    bearish_shift = bearish_ratio - baseline
    substantive_risk_count = int(
        _num(sentiment.get("substantive_risk_count"))
    )
    age_minutes = _num(sentiment.get("age_minutes"), 999.0)

    volume = raw.get("volume") or {}
    futures = raw.get("futures_sync") or {}
    spot_volume = _num(
        volume.get(
            "alpha_volume_growth_6h",
            volume.get("volume_growth_6h"),
        ),
        1.0,
    )
    futures_volume = _num(
        futures.get("futures_volume_growth_6h"),
        1.0,
    )
    sync_score = _num(futures.get("sync_score"))
    spread_pct = _num((raw.get("depth") or {}).get("spread_pct"), 99.0)
    ret_15m = _num((raw.get("returns") or {}).get("ret_15m"))

    conditions = {
        "fresh_15m": age_minutes <= 15,
        "bearish_ratio_80": bearish_ratio >= 0.80,
        "effective_posts_20": post_count >= 20,
        "unique_authors_15": unique_authors >= 15,
        "author_concentration_ok": top3_share <= 0.35,
        "bearish_shift_25pp": bearish_shift >= 0.25,
        "no_substantive_risk": substantive_risk_count == 0,
        "alpha_score_80": _num(alpha_score) >= 80,
        "spot_volume_expanded": spot_volume >= 1.5,
        "futures_volume_expanded": futures_volume >= 1.5,
        "sync_score_65": sync_score >= 65,
        "spread_below_hard_limit": spread_pct < 0.35,
        "price_not_still_falling": ret_15m >= -1.0,
    }
    missing = [name for name, passed in conditions.items() if not passed]
    metrics = {
        "bearish_ratio": round(bearish_ratio, 4),
        "effective_post_count": post_count,
        "unique_authors": unique_authors,
        "top3_author_share": round(top3_share, 4),
        "baseline_bearish_ratio_24h": round(baseline, 4),
        "bearish_shift": round(bearish_shift, 4),
        "substantive_risk_count": substantive_risk_count,
        "age_minutes": round(age_minutes, 2),
        "alpha_score": round(_num(alpha_score), 2),
        "spot_volume_growth_6h": round(spot_volume, 4),
        "futures_volume_growth_6h": round(futures_volume, 4),
        "sync_score": round(sync_score, 2),
        "spread_pct": round(spread_pct, 6),
        "conditions": conditions,
    }
    if missing:
        return {
            "candidate": False,
            "confirmed": False,
            "reasons": ["square_reversal_missing:" + ",".join(missing)],
            "metrics": metrics,
        }
    return {
        "candidate": True,
        # The execution layer still requires the next futures bar to hold.
        "confirmed": False,
        "max_position_factor": 0.5,
        "reasons": [
            "square_extreme_bearishness_confirmed",
            "square_sample_quality_confirmed",
            "square_reversal_waiting_price_structure",
        ],
        "metrics": metrics,
    }
