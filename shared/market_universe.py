"""Deterministic market-universe selection and candle readiness checks."""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class CandleState:
    latest_15m: datetime | None
    latest_1h: datetime | None
    count_15m: int
    count_1h: int


@dataclass(frozen=True)
class ReadinessResult:
    ready: bool
    error: str | None


def _number(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _base_row(pool_type, source_symbol, spot_symbol, futures_symbol, spot_volume, futures_volume):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "pool_type": pool_type,
        "source_symbol": source_symbol,
        "spot_symbol": spot_symbol,
        "futures_symbol": futures_symbol,
        "spot_quote_volume_24h": _number(spot_volume),
        "futures_quote_volume_24h": _number(futures_volume),
        "effective_quote_volume_24h": min(_number(spot_volume), _number(futures_volume)),
        "universe_rank": None,
        "selected": True,
        "forced_position": False,
        "data_ready": False,
        "data_error": "not_checked",
        "data_checked_at": None,
        "selection_reason": None,
        "updated_at": now,
    }


def build_normal_universe(spot_markets, futures_markets, limit=150):
    rows = []
    for symbol, spot in spot_markets.items():
        future = futures_markets.get(symbol)
        if not future:
            continue
        if spot.get("status") != "TRADING" or future.get("status") != "TRADING":
            continue
        if future.get("contract_type", future.get("contractType")) != "PERPETUAL":
            continue
        row = _base_row(
            "normal", symbol, symbol, symbol,
            spot.get("quote_volume", spot.get("quoteVolume")),
            future.get("quote_volume", future.get("quoteVolume")),
        )
        row["selection_reason"] = "top150_dual_market"
        rows.append(row)
    rows.sort(key=lambda item: item["effective_quote_volume_24h"], reverse=True)
    rows = rows[: max(0, int(limit))]
    for rank, row in enumerate(rows, 1):
        row["universe_rank"] = rank
    return rows


def build_alpha_universe(alpha_markets, futures_markets, limit=80, futures_volume_floor=100_000):
    rows = []
    for alpha in alpha_markets:
        source_symbol = alpha.get("alpha_symbol")
        futures_symbol = alpha.get("futures_symbol")
        future = futures_markets.get(futures_symbol) if futures_symbol else None
        futures_volume = _number(future.get("quote_volume", future.get("quoteVolume"))) if future else 0
        if not source_symbol or not future or futures_volume < futures_volume_floor:
            continue
        if future.get("status") != "TRADING":
            continue
        if future.get("contract_type", future.get("contractType")) != "PERPETUAL":
            continue
        alpha_volume = _number(alpha.get("volume_24h", alpha.get("quote_volume")))
        row = _base_row("alpha", source_symbol, source_symbol, futures_symbol, alpha_volume, futures_volume)
        row["effective_quote_volume_24h"] = alpha_volume
        row["selection_reason"] = "top80_alpha_mapped"
        rows.append(row)
    rows.sort(key=lambda item: item["spot_quote_volume_24h"], reverse=True)
    rows = rows[: max(0, int(limit))]
    for rank, row in enumerate(rows, 1):
        row["universe_rank"] = rank
    return rows


def _utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def assess_dual_market_readiness(now, spot, futures):
    now = _utc(now)
    checks = [
        ("spot_15m_age", spot.latest_15m is not None and now - _utc(spot.latest_15m) <= timedelta(minutes=20)),
        ("spot_1h_age", spot.latest_1h is not None and now - _utc(spot.latest_1h) <= timedelta(minutes=75)),
        ("spot_15m_count", spot.count_15m >= 32),
        ("spot_1h_count", spot.count_1h >= 48),
        ("futures_15m_age", futures.latest_15m is not None and now - _utc(futures.latest_15m) <= timedelta(minutes=20)),
        ("futures_1h_age", futures.latest_1h is not None and now - _utc(futures.latest_1h) <= timedelta(minutes=75)),
        ("futures_15m_count", futures.count_15m >= 32),
        ("futures_1h_count", futures.count_1h >= 48),
    ]
    failed = [name for name, passed in checks if not passed]
    return ReadinessResult(not failed, ",".join(failed) or None)
