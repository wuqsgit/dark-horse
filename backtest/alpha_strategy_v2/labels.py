from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Mapping


def _time(value) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def label_counterfactual_path(
    *,
    stage: str,
    entry_price: float,
    invalidation_price: float | None,
    breakout_level: float | None,
    candles: Iterable[Mapping],
) -> dict:
    """Label a long-only counterfactual path with adverse same-bar priority."""
    entry = float(entry_price or 0)
    if entry <= 0:
        raise ValueError("entry_price must be positive")
    invalidation = float(invalidation_price or entry * 0.95)
    risk = max(entry * 0.005, entry - invalidation)
    stage_key = str(stage or "setup").lower()
    horizon = {"setup": 32, "trigger": 16, "acceptance": 16, "retest": 16}.get(
        stage_key,
        16,
    )
    rows = list(candles or [])[:horizon]
    mfe_r = 0.0
    mae_r = 0.0
    first_event = None
    fakeout = False
    time_to_1r = None
    time_to_2r = None
    start_time = _time(rows[0]["time"]) if rows else None
    final_close = entry
    horizons = {
        "30m": 2,
        "2h": 8,
        "4h": 16,
        "8h": 32,
    }
    horizon_metrics = {}

    for index, row in enumerate(rows):
        high = float(row.get("high") or entry)
        low = float(row.get("low") or entry)
        close = float(row.get("close") or entry)
        final_close = close
        favorable = (high - entry) / risk
        adverse = (low - entry) / risk
        mfe_r = max(mfe_r, favorable)
        mae_r = min(mae_r, adverse)
        if time_to_1r is None and favorable >= 1:
            time_to_1r = (index + 1) * 15
        if time_to_2r is None and favorable >= 2:
            time_to_2r = (index + 1) * 15
        if first_event is None:
            hit_target = favorable >= 2
            hit_stop = adverse <= -1
            if hit_stop:
                first_event = "minus_1r"
            elif hit_target:
                first_event = "plus_2r"
        if (
            index < 4
            and breakout_level is not None
            and (close < float(breakout_level) or adverse <= -0.75)
        ):
            fakeout = True
        for name, bars in horizons.items():
            if index + 1 == bars:
                horizon_metrics[f"mfe_{name}_r"] = round(mfe_r, 6)
                horizon_metrics[f"mae_{name}_r"] = round(mae_r, 6)

    followthrough = bool(
        first_event == "plus_2r"
        and (stage_key == "setup" or final_close > entry)
    )
    acceptance = bool(
        breakout_level is not None
        and final_close >= float(breakout_level)
        and mfe_r >= 1.5
        and mae_r > -0.75
    )
    completed = len(rows) >= horizon
    return {
        "stage": stage_key,
        "label_complete": completed,
        "label": int(
            acceptance
            if stage_key == "acceptance"
            else followthrough
        ),
        "setup_success": int(followthrough),
        "followthrough": int(followthrough),
        "fakeout": int(fakeout),
        "acceptance": int(acceptance),
        "first_event": first_event or "no_target",
        "mfe_r": round(mfe_r, 6),
        "mae_r": round(mae_r, 6),
        "time_to_1r_minutes": time_to_1r,
        "time_to_2r_minutes": time_to_2r,
        "final_return_pct": round((final_close / entry - 1) * 100, 6),
        "observed_from": (
            start_time.isoformat().replace("+00:00", "Z")
            if start_time
            else None
        ),
        "bars_observed": len(rows),
        **{
            f"{kind}_{name}_r": horizon_metrics.get(f"{kind}_{name}_r")
            for name in horizons
            for kind in ("mfe", "mae")
        },
    }
