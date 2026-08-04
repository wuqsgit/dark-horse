from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone


def _time(value: str | datetime) -> datetime:
    parsed = (
        value
        if isinstance(value, datetime)
        else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    )
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class SQLiteReplayFeatureSource:
    """Read an environment-isolated historical replay window from SQLite."""

    def __init__(self, db_path: str):
        self.db_path = str(db_path)

    def _connect(self):
        conn = sqlite3.connect(
            f"file:{self.db_path}?mode=ro",
            uri=True,
            timeout=10,
        )
        conn.row_factory = sqlite3.Row
        return conn

    def load(
        self,
        *,
        futures_symbol: str,
        market_env: str,
        start: str | datetime,
        end: str | datetime,
        alpha_symbol: str | None = None,
    ) -> dict:
        symbol = str(futures_symbol).upper()
        env = str(market_env).lower()
        if env not in {"testnet", "mainnet"}:
            raise ValueError(f"unsupported market_env: {market_env}")
        start_at = _time(start)
        end_at = _time(end)
        if end_at <= start_at:
            raise ValueError("end must be after start")
        history_start = start_at - timedelta(days=5)
        label_end = end_at + timedelta(hours=8)
        conn = self._connect()
        try:
            if not alpha_symbol:
                row = conn.execute(
                    """SELECT alpha_symbol, first_seen
                       FROM alpha_symbols
                       WHERE futures_symbol=?
                       ORDER BY datetime(last_seen) DESC LIMIT 1""",
                    (symbol,),
                ).fetchone()
                alpha_symbol = row["alpha_symbol"] if row else None
                listing_time = row["first_seen"] if row else None
            else:
                row = conn.execute(
                    """SELECT first_seen FROM alpha_symbols
                       WHERE alpha_symbol=?""",
                    (alpha_symbol,),
                ).fetchone()
                listing_time = row["first_seen"] if row else None

            def candles(table):
                return [
                    dict(row)
                    for row in conn.execute(
                        f"""SELECT * FROM {table}
                            WHERE symbol=? AND source_env=? AND is_closed=1
                              AND datetime(time) >= datetime(?)
                              AND datetime(time) < datetime(?)
                            ORDER BY datetime(time)""",
                        (
                            symbol,
                            env,
                            _iso(history_start),
                            _iso(label_end),
                        ),
                    ).fetchall()
                ]

            spot = []
            depth = []
            if alpha_symbol:
                spot = [
                    dict(row)
                    for row in conn.execute(
                        """SELECT * FROM alpha_candles_15m
                           WHERE alpha_symbol=? AND source_env='mainnet'
                             AND is_closed=1
                             AND datetime(time) >= datetime(?)
                             AND datetime(time) < datetime(?)
                           ORDER BY datetime(time)""",
                        (
                            alpha_symbol,
                            _iso(history_start),
                            _iso(label_end),
                        ),
                    ).fetchall()
                ]
                depth = [
                    dict(row)
                    for row in conn.execute(
                        """SELECT * FROM alpha_orderbook_snapshots
                           WHERE alpha_symbol=?
                             AND datetime(timestamp) >= datetime(?)
                             AND datetime(timestamp) < datetime(?)
                           ORDER BY datetime(timestamp)""",
                        (
                            alpha_symbol,
                            _iso(history_start),
                            _iso(label_end),
                        ),
                    ).fetchall()
                ]
            derivatives = [
                dict(row)
                for row in conn.execute(
                    """SELECT * FROM futures_data
                       WHERE symbol=? AND source_env=?
                         AND datetime(time) >= datetime(?)
                         AND datetime(time) < datetime(?)
                       ORDER BY datetime(time)""",
                    (
                        symbol,
                        env,
                        _iso(history_start),
                        _iso(label_end),
                    ),
                ).fetchall()
            ]
            return {
                "alpha_symbol": alpha_symbol,
                "futures_symbol": symbol,
                "market_env": env,
                "listing_time": listing_time,
                "candles_15m": candles("futures_candles_15m"),
                "candles_1h": candles("futures_candles_1h"),
                "spot_candles_15m": spot,
                "futures_snapshots": derivatives,
                "orderbook_snapshots": depth,
            }
        finally:
            conn.close()
