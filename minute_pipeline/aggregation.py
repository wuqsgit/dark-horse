from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping


INTERVAL_MINUTES = {
    "15m": 15,
    "30m": 30,
    "1h": 60,
    "4h": 240,
    "6h": 360,
    "1d": 1440,
}


def parse_utc(value) -> datetime:
    if isinstance(value, datetime):
        result = value
    else:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def format_utc(value: datetime) -> str:
    return (
        parse_utc(value)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def bucket_start(value, interval: str) -> datetime:
    minutes = INTERVAL_MINUTES[interval]
    current = parse_utc(value).replace(second=0, microsecond=0)
    minute_index = int(current.timestamp() // 60)
    return datetime.fromtimestamp(
        (minute_index // minutes) * minutes * 60,
        tz=timezone.utc,
    )


def bucket_end_open(value, interval: str) -> datetime:
    start = bucket_start(value, interval)
    return start + timedelta(minutes=INTERVAL_MINUTES[interval] - 1)


def closes_bucket(value, interval: str) -> bool:
    current = parse_utc(value).replace(second=0, microsecond=0)
    return current == bucket_end_open(current, interval)


def affected_buckets(value) -> list[tuple[str, datetime]]:
    return [
        (interval, bucket_start(value, interval))
        for interval in INTERVAL_MINUTES
        if closes_bucket(value, interval)
    ]


def aggregate_minutes(
    rows: Iterable[Mapping],
    *,
    market_kind: str,
    source_env: str,
    symbol: str,
    interval: str,
    start_time,
) -> dict | None:
    expected = INTERVAL_MINUTES[interval]
    start = bucket_start(start_time, interval)
    normalized = sorted(
        (dict(row) for row in rows),
        key=lambda row: parse_utc(row["time"]),
    )
    if len(normalized) != expected:
        return None
    expected_times = [
        start + timedelta(minutes=index)
        for index in range(expected)
    ]
    actual_times = [parse_utc(row["time"]) for row in normalized]
    if actual_times != expected_times:
        return None
    if any(not bool(row.get("is_closed", True)) for row in normalized):
        return None
    return {
        "market_kind": str(market_kind),
        "source_env": str(source_env).lower(),
        "symbol": str(symbol),
        "interval": interval,
        "time": format_utc(start),
        "open": float(normalized[0]["open"]),
        "high": max(float(row["high"]) for row in normalized),
        "low": min(float(row["low"]) for row in normalized),
        "close": float(normalized[-1]["close"]),
        "volume": sum(float(row.get("volume") or 0) for row in normalized),
        "quote_vol": sum(
            float(row.get("quote_vol") or 0) for row in normalized
        ),
        "trades": sum(int(row.get("trades") or 0) for row in normalized),
        "taker_buy_quote_vol": sum(
            float(row.get("taker_buy_quote_vol") or 0)
            for row in normalized
        ),
        "minute_count": len(normalized),
        "expected_count": expected,
        "is_complete": True,
    }

