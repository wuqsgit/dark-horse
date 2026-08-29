from __future__ import annotations

import time
import sqlite3
from datetime import datetime, timezone

from shared.db import _serialized_write, get_conn


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _snapshot_version() -> str:
    return str(time.time_ns())


def _number(value, default=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _integer(value, default=0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return int(default)


@_serialized_write
def replace_live_account_snapshot(
    account_id: int,
    balance: dict,
    positions: list[dict],
    orders: list[dict],
    *,
    source: str,
    exchange_event_time: str | None = None,
) -> str:
    """Atomically replace one account's complete exchange current state."""
    account_id = int(account_id)
    version = _snapshot_version()
    updated_at = _utc_now()
    event_time = exchange_event_time or updated_at
    connection = get_conn()
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """INSERT INTO account_live_balances
               (account_id, asset, wallet_balance, equity, available_balance,
                unrealized_pnl, total_maint_margin, total_initial_margin,
                snapshot_version, exchange_event_time, source, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(account_id) DO UPDATE SET
                 asset=excluded.asset,
                 wallet_balance=excluded.wallet_balance,
                 equity=excluded.equity,
                 available_balance=excluded.available_balance,
                 unrealized_pnl=excluded.unrealized_pnl,
                 total_maint_margin=excluded.total_maint_margin,
                 total_initial_margin=excluded.total_initial_margin,
                 snapshot_version=excluded.snapshot_version,
                 exchange_event_time=excluded.exchange_event_time,
                 source=excluded.source,
                 updated_at=excluded.updated_at""",
            (
                account_id,
                str(balance.get("asset") or "USDT"),
                _number(balance.get("wallet_balance")),
                _number(balance.get("equity")),
                _number(balance.get("available_balance")),
                _number(balance.get("unrealized_pnl")),
                _number(balance.get("total_maint_margin")),
                _number(balance.get("total_initial_margin")),
                version,
                event_time,
                str(source),
                updated_at,
            ),
        )
        connection.execute(
            "DELETE FROM account_live_positions WHERE account_id=?",
            (account_id,),
        )
        for position in positions or []:
            quantity = abs(_number(position.get("quantity")))
            if quantity <= 0:
                continue
            connection.execute(
                """INSERT INTO account_live_positions
                   (account_id, symbol, position_side, side, quantity,
                    entry_price, mark_price, unrealized_pnl, leverage, margin,
                    initial_margin, maint_margin, position_initial_margin,
                    open_order_initial_margin, isolated_margin, notional,
                    margin_asset, margin_type, liquidation_price,
                    break_even_price, risk_api_version, snapshot_version,
                    exchange_event_time, source, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                           ?, ?, ?, ?, ?, ?, ?)""",
                (
                    account_id,
                    str(position.get("symbol") or "").upper(),
                    str(position.get("position_side") or position.get("positionSide") or "BOTH"),
                    str(position.get("side") or ""),
                    quantity,
                    _number(position.get("entry_price")),
                    _number(position.get("mark_price")),
                    _number(position.get("unrealized_pnl")),
                    _integer(position.get("leverage")),
                    _number(position.get("margin")),
                    _number(position.get("initial_margin")),
                    _number(position.get("maint_margin")),
                    _number(position.get("position_initial_margin")),
                    _number(position.get("open_order_initial_margin")),
                    _number(position.get("isolated_margin")),
                    abs(_number(position.get("notional"))),
                    position.get("margin_asset"),
                    position.get("margin_type"),
                    _number(position.get("liquidation_price")),
                    _number(position.get("break_even_price")),
                    position.get("risk_api_version"),
                    version,
                    event_time,
                    str(source),
                    updated_at,
                ),
            )
        connection.execute(
            "DELETE FROM account_live_orders WHERE account_id=?",
            (account_id,),
        )
        for order in orders or []:
            order_id = str(order.get("exchange_order_id") or order.get("orderId") or "")
            if not order_id:
                continue
            connection.execute(
                """INSERT INTO account_live_orders
                   (account_id, exchange_order_id, client_order_id, symbol,
                    side, position_side, order_type, status, quantity,
                    executed_quantity, price, stop_price, reduce_only,
                    close_position, time_in_force, snapshot_version,
                    exchange_event_time, source, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    account_id,
                    order_id,
                    order.get("client_order_id") or order.get("clientOrderId"),
                    str(order.get("symbol") or "").upper(),
                    order.get("side"),
                    order.get("position_side") or order.get("positionSide"),
                    order.get("order_type") or order.get("type"),
                    order.get("status"),
                    _number(order.get("quantity", order.get("origQty"))),
                    _number(order.get("executed_quantity", order.get("executedQty"))),
                    _number(order.get("price")),
                    _number(order.get("stop_price", order.get("stopPrice"))),
                    int(bool(order.get("reduce_only", order.get("reduceOnly")))),
                    int(bool(order.get("close_position", order.get("closePosition")))),
                    order.get("time_in_force") or order.get("timeInForce"),
                    version,
                    event_time,
                    str(source),
                    updated_at,
                ),
            )
        connection.execute(
            """INSERT INTO account_stream_state
               (account_id, status, ws_connected, snapshot_version,
                exchange_event_time, source, last_success_at, updated_at)
               VALUES (?, 'ok', COALESCE((SELECT ws_connected
                                           FROM account_stream_state
                                           WHERE account_id=?), 0), ?, ?, ?, ?, ?)
               ON CONFLICT(account_id) DO UPDATE SET
                 status='ok',
                 snapshot_version=excluded.snapshot_version,
                 exchange_event_time=excluded.exchange_event_time,
                 source=excluded.source,
                 last_success_at=excluded.last_success_at,
                 last_error=NULL,
                 updated_at=excluded.updated_at""",
            (
                account_id,
                account_id,
                version,
                event_time,
                str(source),
                updated_at,
                updated_at,
            ),
        )
        connection.commit()
        return version
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def fetch_live_account_snapshot(account_id: int) -> dict:
    account_id = int(account_id)
    connection = get_conn()
    try:
        try:
            balance = connection.execute(
                "SELECT * FROM account_live_balances WHERE account_id=?",
                (account_id,),
            ).fetchone()
        except sqlite3.OperationalError as exc:
            if "no such table" not in str(exc).lower():
                raise
            return {"balance": None, "positions": [], "orders": [], "state": None}
        positions = connection.execute(
            """SELECT * FROM account_live_positions
               WHERE account_id=? ORDER BY symbol, position_side""",
            (account_id,),
        ).fetchall()
        orders = connection.execute(
            """SELECT * FROM account_live_orders
               WHERE account_id=? ORDER BY symbol, exchange_order_id""",
            (account_id,),
        ).fetchall()
        state = connection.execute(
            "SELECT * FROM account_stream_state WHERE account_id=?",
            (account_id,),
        ).fetchone()
        return {
            "balance": dict(balance) if balance else None,
            "positions": [dict(row) for row in positions],
            "orders": [dict(row) for row in orders],
            "state": dict(state) if state else None,
        }
    finally:
        connection.close()


@_serialized_write
def update_account_stream_state(account_id: int, **fields) -> None:
    allowed = {
        "status", "ws_connected", "exchange_event_time", "source",
        "last_success_at", "last_error_at", "last_error",
        "listen_key_expires_at",
    }
    updates = {key: value for key, value in fields.items() if key in allowed}
    now = _utc_now()
    connection = get_conn()
    try:
        connection.execute(
            """INSERT OR IGNORE INTO account_stream_state
               (account_id, status, ws_connected, updated_at)
               VALUES (?, 'starting', 0, ?)""",
            (int(account_id), now),
        )
        if updates:
            updates["updated_at"] = now
            assignments = ", ".join(f"{key}=?" for key in updates)
            connection.execute(
                f"UPDATE account_stream_state SET {assignments} WHERE account_id=?",
                (*updates.values(), int(account_id)),
            )
        connection.commit()
    finally:
        connection.close()
