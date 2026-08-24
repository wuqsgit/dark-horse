from __future__ import annotations

from dataclasses import dataclass

from shared.db import fetch_market_universe, fetch_tracked_position_symbols


@dataclass(frozen=True)
class MinuteUniverse:
    spot: tuple[str, ...]
    futures: tuple[str, ...]
    alpha: tuple[str, ...]


def _valid_usdt_futures_symbol(symbol: str) -> bool:
    value = str(symbol or "").upper()
    return value.endswith("USDT") and "_PERP" not in value


def load_minute_universe() -> MinuteUniverse:
    normal = [
        dict(row)
        for row in fetch_market_universe("normal")
        if row["selected"] or row["forced_position"]
    ]
    alpha = [
        dict(row)
        for row in fetch_market_universe("alpha")
        if row["selected"] or row["forced_position"]
    ]
    tracked = fetch_tracked_position_symbols()
    spot_symbols = {
        str(row.get("spot_symbol") or row.get("source_symbol") or "").upper()
        for row in normal
    }
    futures_symbols = {
        str(row.get("futures_symbol") or "").upper()
        for row in [*normal, *alpha]
    }
    futures_symbols.update(
        str(symbol).upper()
        for symbol in tracked
        if _valid_usdt_futures_symbol(symbol)
    )
    futures_symbols.add("BTCUSDT")
    alpha_symbols = {
        str(row.get("source_symbol") or "").upper()
        for row in alpha
    }
    return MinuteUniverse(
        spot=tuple(sorted(symbol for symbol in spot_symbols if symbol)),
        futures=tuple(
            sorted(
                symbol
                for symbol in futures_symbols
                if _valid_usdt_futures_symbol(symbol)
            )
        ),
        alpha=tuple(sorted(symbol for symbol in alpha_symbols if symbol)),
    )
