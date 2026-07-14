def label_path(entry_price: float, stop_pct: float, side: str, candles: list[dict]) -> dict:
    entry = float(entry_price or 0)
    stop = float(stop_pct or 0)
    if entry <= 0 or stop <= 0:
        raise ValueError("entry_price and stop_pct must be positive")

    risk_distance = entry * stop
    is_short = str(side or "LONG").upper() == "SHORT"
    mfe_r = 0.0
    mae_r = 0.0
    first_event = None

    for candle in candles or []:
        high = float(candle.get("high") or entry)
        low = float(candle.get("low") or entry)
        if is_short:
            favorable = (entry - low) / risk_distance
            adverse = (entry - high) / risk_distance
        else:
            favorable = (high - entry) / risk_distance
            adverse = (low - entry) / risk_distance
        mfe_r = max(mfe_r, favorable)
        mae_r = min(mae_r, adverse)

        if first_event is None:
            hit_profit = favorable >= 1.0
            hit_stop = adverse <= -1.0
            if hit_profit and hit_stop:
                first_event = "same_bar_stop_first"
            elif hit_stop:
                first_event = "minus_1r"
            elif hit_profit:
                first_event = "plus_1r"

    return {
        "label": 1 if first_event == "plus_1r" else 0,
        "first_event": first_event or "no_plus_1r",
        "mfe_r": round(mfe_r, 6),
        "mae_r": round(mae_r, 6),
    }
