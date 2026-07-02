"""Structure metrics for chip, absorption and support quality.

These signals are intentionally explainable.  They use existing candles first
and leave room for futures/orderbook enrichment without blocking scoring.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


def _score(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return round(max(lo, min(hi, float(value))), 1)


def _safe_mean(values: Any, default: float = 0.0) -> float:
    arr = np.asarray(values, dtype=float)
    arr = arr[np.isfinite(arr)]
    if len(arr) == 0:
        return default
    return float(np.mean(arr))


def _position_score(position: float) -> float:
    if position <= 0.25:
        return 85
    if position <= 0.40:
        return 72
    if position <= 0.60:
        return 55
    if position <= 0.78:
        return 38
    return 20


def _volume_profile(close: np.ndarray, volume: np.ndarray, bins: int = 18) -> dict:
    if len(close) < 24 or np.max(close) <= np.min(close):
        return {"poc": float(close[-1]), "score": 50.0, "distance_pct": 0.0}
    hist, edges = np.histogram(close, bins=bins, weights=volume)
    idx = int(np.argmax(hist))
    poc = float((edges[idx] + edges[idx + 1]) / 2)
    price = float(close[-1])
    distance_pct = abs(price - poc) / price if price > 0 else 0.0
    if price >= poc and distance_pct <= 0.03:
        score = 78
    elif price >= poc and distance_pct <= 0.08:
        score = 64
    elif price < poc and distance_pct <= 0.04:
        score = 48
    else:
        score = 35
    return {"poc": poc, "score": score, "distance_pct": round(distance_pct, 4)}


def _up_down_volume_ratio(open_: np.ndarray, close: np.ndarray, volume: np.ndarray, lookback: int = 24) -> float:
    n = min(len(close), lookback)
    if n <= 0:
        return 1.0
    ret = close[-n:] / np.maximum(open_[-n:], 1e-12) - 1
    up = float(np.sum(volume[-n:][ret > 0]))
    down = float(np.sum(volume[-n:][ret <= 0]))
    return up / down if down > 0 else 2.0


def _volume_ratio(volume: np.ndarray, recent: int = 24, prior: int = 24) -> float:
    if len(volume) < recent + prior:
        return 1.0
    rv = _safe_mean(volume[-recent:], 0.0)
    pv = _safe_mean(volume[-recent - prior:-recent], rv)
    return rv / pv if pv > 0 else 1.0


def _wick_reclaim_score(open_: np.ndarray, high: np.ndarray, low: np.ndarray, close: np.ndarray, lookback: int = 24) -> dict:
    n = min(len(close), lookback)
    if n <= 0:
        return {"score": 50.0, "lower_wick_ratio": 0.0, "close_position": 0.5}
    ranges = np.maximum(high[-n:] - low[-n:], 1e-12)
    lower_wicks = np.minimum(open_[-n:], close[-n:]) - low[-n:]
    close_pos = (close[-n:] - low[-n:]) / ranges
    wick_ratio = _safe_mean(lower_wicks / ranges, 0.0)
    close_position = _safe_mean(close_pos, 0.5)
    score = 25 + wick_ratio * 70 + close_position * 35
    return {
        "score": _score(score),
        "lower_wick_ratio": round(wick_ratio, 4),
        "close_position": round(close_position, 4),
    }


def _higher_lows_score(low: np.ndarray, lookback: int = 24) -> dict:
    n = min(len(low), lookback)
    if n < 8:
        return {"score": 50.0, "higher_low_ratio": 0.5}
    lows = low[-n:]
    ok = 0
    total = 0
    for i in range(1, len(lows)):
        total += 1
        if lows[i] >= lows[i - 1] * 0.995:
            ok += 1
    ratio = ok / total if total else 0.5
    return {"score": _score(20 + ratio * 80), "higher_low_ratio": round(ratio, 4)}


def compute_chip_structure(df_1h: pd.DataFrame) -> dict:
    if df_1h is None or df_1h.empty or len(df_1h) < 48:
        return {
            "chip_score": None,
            "chip_phase": "数据不足",
            "chip_detail": {"reason": "need_at_least_48_1h_candles"},
        }
    df = df_1h.sort_values("time").tail(120)
    o = df["open"].astype(float).values
    h = df["high"].astype(float).values
    lo = df["low"].astype(float).values
    c = df["close"].astype(float).values
    v = df["volume"].astype(float).values
    price = float(c[-1])
    low_120 = float(np.min(lo))
    high_120 = float(np.max(h))
    position = (price - low_120) / (high_120 - low_120) if high_120 > low_120 else 0.5
    profile = _volume_profile(c, v)
    uv_ratio = _up_down_volume_ratio(o, c, v)
    vol_ratio = _volume_ratio(v)
    higher = _higher_lows_score(lo)
    range_pct = (high_120 - low_120) / low_120 if low_120 > 0 else 0.0
    compression_score = 75 if range_pct < 0.18 else 58 if range_pct < 0.30 else 38
    volume_balance_score = _score(50 + (min(2.2, uv_ratio) - 1.0) * 25)
    score = (
        _position_score(position) * 0.25
        + profile["score"] * 0.25
        + volume_balance_score * 0.20
        + higher["score"] * 0.15
        + compression_score * 0.10
        + (65 if 0.85 <= vol_ratio <= 1.45 else 50 if vol_ratio < 1.8 else 35) * 0.05
    )
    score = _score(score)
    if score >= 75:
        phase = "低位沉淀" if position <= 0.45 else "温和吸筹"
    elif score >= 60:
        phase = "筹码改善"
    elif score >= 45:
        phase = "中性震荡"
    elif score >= 30:
        phase = "筹码松动"
    else:
        phase = "疑似派发"
    return {
        "chip_score": score,
        "chip_phase": phase,
        "chip_detail": {
            "price_position_120h": round(position, 4),
            "volume_profile_poc": round(profile["poc"], 8),
            "volume_profile_distance_pct": profile["distance_pct"],
            "up_down_volume_ratio": round(uv_ratio, 4),
            "volume_ratio_recent_vs_prior": round(vol_ratio, 4),
            "higher_low_ratio": higher["higher_low_ratio"],
            "range_width_pct": round(range_pct, 4),
        },
    }


def compute_absorption_quality(df_1h: pd.DataFrame, df_15m: pd.DataFrame | None = None) -> dict:
    if df_1h is None or df_1h.empty or len(df_1h) < 48:
        return {
            "absorption_score": None,
            "abs_score": None,
            "absorption_quality": "数据不足",
            "absorption_detail": {"reason": "need_at_least_48_1h_candles"},
        }
    df = df_1h.sort_values("time").tail(96)
    o = df["open"].astype(float).values
    h = df["high"].astype(float).values
    lo = df["low"].astype(float).values
    c = df["close"].astype(float).values
    v = df["volume"].astype(float).values
    price = float(c[-1])
    low_96 = float(np.min(lo))
    high_96 = float(np.max(h))
    position = (price - low_96) / (high_96 - low_96) if high_96 > low_96 else 0.5
    low_position_score = _position_score(position)
    wick = _wick_reclaim_score(o, h, lo, c)
    higher = _higher_lows_score(lo)
    vol_ratio = _volume_ratio(v)
    ret_24h = (c[-1] - c[-25]) / c[-25] if len(c) >= 25 and c[-25] > 0 else 0.0
    if abs(ret_24h) <= 0.025 and vol_ratio >= 1.10:
        divergence_score = 78
    elif ret_24h < -0.03 and vol_ratio >= 1.25:
        divergence_score = 62
    elif ret_24h < -0.04 and vol_ratio < 0.8:
        divergence_score = 25
    else:
        divergence_score = 50

    short_score = 50.0
    if df_15m is not None and not df_15m.empty and len(df_15m) >= 24:
        s = df_15m.sort_values("time").tail(96)
        so = s["open"].astype(float).values
        sh = s["high"].astype(float).values
        sl = s["low"].astype(float).values
        sc = s["close"].astype(float).values
        short_score = _wick_reclaim_score(so, sh, sl, sc, 32)["score"]

    score = (
        low_position_score * 0.20
        + wick["score"] * 0.20
        + divergence_score * 0.20
        + higher["score"] * 0.15
        + short_score * 0.15
        + (65 if 0.9 <= vol_ratio <= 1.6 else 45) * 0.10
    )
    score = _score(score)
    if score >= 75:
        quality = "强承接"
    elif score >= 60:
        quality = "温和承接"
    elif score >= 45:
        quality = "正常"
    elif score >= 30:
        quality = "承接弱"
    else:
        quality = "放量下跌"
    return {
        "absorption_score": score,
        "abs_score": score,
        "absorption_quality": quality,
        "absorption_detail": {
            "price_position_96h": round(position, 4),
            "lower_wick_ratio": wick["lower_wick_ratio"],
            "close_position": wick["close_position"],
            "higher_low_ratio": higher["higher_low_ratio"],
            "volume_ratio_recent_vs_prior": round(vol_ratio, 4),
            "return_24h": round(ret_24h, 4),
            "short_reclaim_score": round(short_score, 1),
        },
    }


def compute_support_quality(df_1h: pd.DataFrame) -> dict:
    if df_1h is None or df_1h.empty or len(df_1h) < 48:
        return {
            "support_score": None,
            "support_quality": "数据不足",
            "support_detail": {"reason": "need_at_least_48_1h_candles"},
        }
    df = df_1h.sort_values("time").tail(120)
    h = df["high"].astype(float).values
    lo = df["low"].astype(float).values
    c = df["close"].astype(float).values
    v = df["volume"].astype(float).values
    price = float(c[-1])
    tr = np.maximum(h[1:] - lo[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(lo[1:] - c[:-1])))
    atr = _safe_mean(tr[-14:], price * 0.02)
    window = min(96, len(lo))
    lows = lo[-window:]
    support = float(np.percentile(lows, 18))
    distance_atr = (price - support) / atr if atr > 0 else 0.0
    tolerance = max(atr * 0.45, price * 0.003)
    tests = int(np.sum(np.abs(lows - support) <= tolerance))
    breakdowns = int(np.sum(c[-window:] < support - tolerance))
    bounce_returns = []
    offset = len(c) - window
    for i, low in enumerate(lows[:-4]):
        if abs(low - support) <= tolerance:
            start = offset + i
            future_high = float(np.max(h[start + 1:start + 5]))
            bounce_returns.append((future_high - low) / low if low > 0 else 0.0)
    avg_bounce = _safe_mean(bounce_returns, 0.0)
    higher = _higher_lows_score(lo, 36)
    support_vol_mask = np.abs(lows - support) <= tolerance
    support_vol = _safe_mean(v[-window:][support_vol_mask], 0.0) if np.any(support_vol_mask) else 0.0
    normal_vol = _safe_mean(v[-window:], 1.0)
    support_vol_ratio = support_vol / normal_vol if normal_vol > 0 else 1.0

    test_score = _score(min(tests, 6) / 6 * 100)
    bounce_score = _score(min(avg_bounce / 0.035, 1.0) * 100)
    breakdown_score = _score(100 - min(breakdowns, 5) * 22)
    if distance_atr < -0.2:
        distance_score = 10
    elif distance_atr <= 0.8:
        distance_score = 85
    elif distance_atr <= 1.8:
        distance_score = 68
    elif distance_atr <= 3.0:
        distance_score = 45
    else:
        distance_score = 25
    volume_score = _score(45 + min(support_vol_ratio, 2.0) * 25)
    score = (
        test_score * 0.25
        + bounce_score * 0.20
        + breakdown_score * 0.20
        + distance_score * 0.15
        + volume_score * 0.10
        + higher["score"] * 0.10
    )
    score = _score(score)
    if price < support - tolerance or breakdowns >= 4:
        quality = "跌破支撑"
        score = min(score, 35)
    elif score >= 75:
        quality = "强支撑"
    elif score >= 60:
        quality = "有效支撑"
    elif score >= 45:
        quality = "一般"
    elif score >= 30:
        quality = "弱支撑"
    else:
        quality = "跌破支撑"
    return {
        "support_score": _score(score),
        "support_quality": quality,
        "support_detail": {
            "support_price": round(support, 8),
            "distance_atr": round(distance_atr, 4),
            "test_count": tests,
            "breakdown_count": breakdowns,
            "avg_bounce_pct": round(avg_bounce, 4),
            "support_volume_ratio": round(support_vol_ratio, 4),
            "higher_low_ratio": higher["higher_low_ratio"],
        },
    }


def compute_structure_metrics(df_1h: pd.DataFrame, df_15m: pd.DataFrame | None = None) -> dict:
    result = {}
    result.update(compute_chip_structure(df_1h))
    result.update(compute_absorption_quality(df_1h, df_15m))
    result.update(compute_support_quality(df_1h))
    return result
