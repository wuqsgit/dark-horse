"""Market-data correctness helpers for Alpha Strategy V2."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Iterable, Mapping


MARKET_ENV = "mainnet"
MARKET_ENVS = {MARKET_ENV}


def resolve_market_env(value: str | None = None) -> str:
    """Resolve an explicit market-data environment.

    Market data is independent from account execution. Runtime defaults to
    mainnet so testnet accounts evaluate the same public market as production.
    Explicit values remain supported for historical replay and migrations.
    """
    raw = value
    if raw is None:
        raw = os.getenv("ALPHA_FUTURES_MARKET_ENV")
    if raw is None:
        raw = MARKET_ENV
    result = str(raw).strip().lower()
    if result not in MARKET_ENVS:
        raise ValueError(f"unsupported Alpha futures market environment: {value}")
    return result


def futures_rest_base(market_env: str) -> str:
    resolve_market_env(market_env)
    return "https://fapi.binance.com"


def is_closed_kline(row, *, now_ms: int | None = None) -> bool:
    """Return whether a Binance-style REST kline has closed."""
    if not row or len(row) <= 6:
        return False
    try:
        close_time = int(row[6])
    except (TypeError, ValueError):
        return False
    if now_ms is None:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return close_time <= int(now_ms)


def _parse_time(value) -> datetime | None:
    if value is None:
        return None
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


def oi_change_at_horizon(
    rows: Iterable[Mapping],
    *,
    hours: float,
    as_of: datetime | None = None,
    max_sample_gap_minutes: float = 30,
) -> float | None:
    """Compute OI change using timestamps rather than record offsets.

    The historical sample must exist at or before the requested horizon and be
    close enough to it. This prevents a sparse 10-minute snapshot series from
    pretending that four records represent four hours.
    """
    parsed_rows = []
    for row in rows or []:
        def value(key):
            if hasattr(row, "get"):
                return row.get(key)
            try:
                return row[key]
            except (KeyError, IndexError, TypeError):
                return None

        timestamp = _parse_time(value("time"))
        try:
            open_interest = float(value("open_interest"))
        except (TypeError, ValueError):
            continue
        if timestamp is not None and open_interest > 0:
            parsed_rows.append((timestamp, open_interest))
    if not parsed_rows:
        return None
    parsed_rows.sort(key=lambda item: item[0])
    current_time = _parse_time(as_of) if as_of is not None else parsed_rows[-1][0]
    eligible_current = [item for item in parsed_rows if item[0] <= current_time]
    if not eligible_current:
        return None
    latest_time, latest_value = eligible_current[-1]
    target = current_time - timedelta(hours=float(hours))
    historical = [item for item in parsed_rows if item[0] <= target]
    if not historical:
        return None
    old_time, old_value = historical[-1]
    if target - old_time > timedelta(minutes=float(max_sample_gap_minutes)):
        return None
    if latest_value <= 0 or old_value <= 0:
        return None
    return (latest_value - old_value) / old_value
