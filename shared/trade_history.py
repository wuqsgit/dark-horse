"""Pure helpers for reconstructing position cycles from exchange fills."""

import base64
import json
from collections.abc import Iterable, Mapping

from . import db


def _is_execution_fill(row):
    return str(row.get("side") or "").upper() in {"BUY", "SELL"}


def _fill_identity(row):
    trade_id = str(row.get("trade_id") or "").split(":")[-1]
    if trade_id:
        return (int(row["account_id"]), row["symbol"].upper(), trade_id)
    return (
        int(row["account_id"]),
        row["symbol"].upper(),
        row["side"].upper(),
        round(float(row.get("quantity") or 0), 12),
        round(float(row.get("price") or 0), 12),
        str(row.get("created_at") or ""),
    )


def _fill_sort_key(row):
    raw_id = row.get("id")
    id_text = str(raw_id or "")
    try:
        id_key = (0, int(raw_id))
    except (TypeError, ValueError):
        id_key = (1, id_text)
    return (
        str(row.get("created_at") or ""),
        id_key,
        str(row.get("trade_id") or ""),
    )


def deduplicate_fills(rows: Iterable[Mapping]) -> list[dict]:
    unique = {}
    for original in rows:
        row = dict(original)
        if not _is_execution_fill(row):
            continue
        unique.setdefault(_fill_identity(row), row)
    return sorted(unique.values(), key=_fill_sort_key)


def _weighted_price(rows):
    quantity = sum(float(row["quantity"]) for row in rows)
    return (
        sum(float(row["price"]) * float(row["quantity"]) for row in rows) / quantity
        if quantity > 0
        else None
    )


def _position_side(row):
    return str(row.get("position_side") or "BOTH").upper()


def _signed_quantity(row):
    quantity = abs(float(row.get("quantity") or 0))
    side = str(row.get("side") or "").upper()
    if side not in {"BUY", "SELL"}:
        return 0.0
    return quantity if side == "BUY" else -quantity


def _cycle_direction(position_side, signed_quantity):
    if position_side == "LONG":
        return "LONG"
    if position_side == "SHORT":
        return "SHORT"
    return "LONG" if signed_quantity > 0 else "SHORT"


def _split_fill(row, quantity):
    split = dict(row)
    split["quantity"] = quantity
    return split


def _new_cycle(row, direction):
    return {
        "account_id": int(row["account_id"]),
        "symbol": str(row["symbol"]).upper(),
        "direction": direction,
        "entry_fills": [],
        "exit_fills": [],
        "entry_time": None,
        "exit_time": None,
        "entry_quantity": 0.0,
        "exit_quantity": 0.0,
        "entry_price": None,
        "exit_price": None,
        "trade_ids": [],
        "complete": False,
        "_position": 0.0,
        "_direction_sign": 1.0 if direction == "LONG" else -1.0,
    }


def _append_trade_id(cycle, row):
    trade_id = row.get("trade_id")
    if trade_id and trade_id not in cycle["trade_ids"]:
        cycle["trade_ids"].append(trade_id)


def _add_entry(cycle, row, quantity):
    fill = _split_fill(row, quantity)
    cycle["entry_fills"].append(fill)
    cycle["entry_quantity"] += quantity
    if cycle["entry_time"] is None:
        cycle["entry_time"] = row.get("created_at")
    _append_trade_id(cycle, row)


def _add_exit(cycle, row, quantity):
    fill = _split_fill(row, quantity)
    cycle["exit_fills"].append(fill)
    cycle["exit_quantity"] += quantity
    cycle["exit_time"] = row.get("created_at")
    _append_trade_id(cycle, row)


def _complete_cycle(cycle):
    cycle["entry_price"] = _weighted_price(cycle["entry_fills"])
    cycle["exit_price"] = _weighted_price(cycle["exit_fills"])
    cycle["complete"] = True
    cycle.pop("_position")
    cycle.pop("_direction_sign")
    return cycle


def reconstruct_position_cycles(rows: Iterable[Mapping]) -> list[dict]:
    active = {}
    completed = []

    for row in deduplicate_fills(rows):
        position_side = _position_side(row)
        key = (int(row["account_id"]), str(row["symbol"]).upper(), position_side)
        signed_quantity = _signed_quantity(row)
        quantity = abs(signed_quantity)
        if quantity <= 0 or signed_quantity == 0:
            continue

        cycle = active.get(key)
        if cycle is None:
            cycle = _new_cycle(row, _cycle_direction(position_side, signed_quantity))
            active[key] = cycle

        remaining = quantity
        while remaining > 0:
            direction_sign = cycle["_direction_sign"]
            incoming_sign = 1.0 if signed_quantity > 0 else -1.0
            is_entry = incoming_sign == direction_sign
            current_magnitude = abs(cycle["_position"])

            if current_magnitude == 0:
                portion = remaining
            elif is_entry:
                portion = remaining
            else:
                portion = min(current_magnitude, remaining)

            if is_entry:
                _add_entry(cycle, row, portion)
            else:
                _add_exit(cycle, row, portion)

            cycle["_position"] += signed_quantity / quantity * portion
            remaining -= portion

            if abs(cycle["_position"]) <= 1e-12:
                if cycle["entry_fills"] and cycle["exit_fills"]:
                    completed.append(_complete_cycle(cycle))
                active.pop(key, None)
                if remaining > 0:
                    cycle = _new_cycle(row, _cycle_direction(position_side, signed_quantity))
                    active[key] = cycle
            elif not is_entry and remaining > 0:
                cycle = _new_cycle(row, _cycle_direction(position_side, signed_quantity))
                active[key] = cycle

    return completed


def _normalized_trade_id(value):
    return str(value or "").split(":")[-1]


def _cycle_source(cycle):
    for fill in cycle["entry_fills"]:
        source = fill.get("strategy_source") or fill.get("source")
        if source:
            return str(source)
    return None


def _allocate_income(cycles, income_rows):
    by_trade_id = {}
    for cycle in cycles:
        cycle["pnl"] = 0.0
        cycle["income_count"] = 0
        for trade_id in cycle["trade_ids"]:
            normalized = _normalized_trade_id(trade_id)
            if normalized:
                by_trade_id.setdefault(normalized, cycle)

    for income in income_rows:
        if str(income.get("income_type") or "").upper() not in {
            "REALIZED_PNL",
            "COMMISSION",
            "FUNDING_FEE",
        }:
            continue
        cycle = by_trade_id.get(_normalized_trade_id(income.get("trade_id")))
        if cycle is None:
            symbol = str(income.get("symbol") or "").upper()
            income_time = str(income.get("income_time") or "")
            matches = [
                item for item in cycles
                if item["symbol"] == symbol
                and item["entry_time"] <= income_time <= item["exit_time"]
            ]
            if len(matches) == 1:
                cycle = matches[0]
        if cycle is not None:
            cycle["pnl"] += float(income.get("income") or 0)
            cycle["income_count"] += 1


def _cycle_margin(cycle):
    margins = {
        float(fill["margin"])
        for fill in cycle["entry_fills"]
        if fill.get("margin") not in (None, "")
    }
    if len(margins) == 1:
        return margins.pop()
    leverages = {
        float(fill["leverage"])
        for fill in cycle["entry_fills"]
        if fill.get("leverage") not in (None, "")
    }
    if len(leverages) != 1 or 0 in leverages:
        return None
    entry_notional = sum(
        float(fill["price"]) * float(fill["quantity"])
        for fill in cycle["entry_fills"]
    )
    return entry_notional / leverages.pop()


def _cycle_matches(cycle, direction=None, source=None, from_time=None, to_time=None):
    exit_time = str(cycle["exit_time"] or "")
    return (
        (direction is None or cycle["direction"] == direction)
        and (source is None or _cycle_source(cycle) == source)
        and (from_time is None or exit_time >= from_time)
        and (to_time is None or exit_time <= to_time)
    )


def _summary_from_cycles(cycles):
    grouped = {}
    for cycle in cycles:
        key = (cycle["account_id"], cycle["symbol"], cycle["direction"])
        summary = grouped.setdefault(
            key,
            {
                "account_id": cycle["account_id"],
                "symbol": cycle["symbol"],
                "side": cycle["direction"],
                "quantity": 0.0,
                "entry_notional": 0.0,
                "exit_notional": 0.0,
                "pnl": 0.0,
                "margin": 0.0,
                "has_exact_margin": True,
                "position_count": 0,
                "close_count": 0,
                "entry_time": cycle["entry_time"],
                "exit_time": cycle["exit_time"],
                "strategy_sources": set(),
            },
        )
        summary["quantity"] += cycle["entry_quantity"]
        summary["entry_notional"] += sum(
            float(fill["price"]) * float(fill["quantity"])
            for fill in cycle["entry_fills"]
        )
        summary["exit_notional"] += sum(
            float(fill["price"]) * float(fill["quantity"])
            for fill in cycle["exit_fills"]
        )
        summary["pnl"] += cycle["pnl"]
        margin = _cycle_margin(cycle)
        if margin is None:
            summary["has_exact_margin"] = False
        else:
            summary["margin"] += margin
        summary["position_count"] += 1
        summary["close_count"] += cycle.get("close_count", len(cycle["exit_fills"]))
        source = _cycle_source(cycle)
        if source is not None:
            summary["strategy_sources"].add(source)
        summary["entry_time"] = min(summary["entry_time"], cycle["entry_time"])
        summary["exit_time"] = max(summary["exit_time"], cycle["exit_time"])

    summaries = []
    for summary in grouped.values():
        quantity = summary.pop("quantity")
        entry_notional = summary.pop("entry_notional")
        exit_notional = summary.pop("exit_notional")
        margin = summary.pop("margin")
        has_exact_margin = summary.pop("has_exact_margin")
        summary["strategy_sources"] = sorted(summary["strategy_sources"])
        summary["quantity"] = int(quantity) if quantity.is_integer() else quantity
        summary["entry_price"] = entry_notional / quantity if quantity else None
        summary["exit_price"] = exit_notional / quantity if quantity else None
        summary["pnl_pct"] = (
            summary["pnl"] / margin * 100 if has_exact_margin and margin else None
        )
        summaries.append(summary)
    return summaries


def _stats_from_cycles(cycles):
    pnl_values = [float(cycle.get("pnl") or 0) for cycle in cycles]
    win_count = sum(pnl > 0 for pnl in pnl_values)
    loss_count = sum(pnl < 0 for pnl in pnl_values)
    decided_count = win_count + loss_count
    return {
        "total_cycles": len(cycles),
        "position_count": len(cycles),
        "total_pnl": sum(pnl_values),
        "win_count": win_count,
        "loss_count": loss_count,
        "flat_count": len(cycles) - decided_count,
        "win_rate": win_count / decided_count * 100 if decided_count else None,
    }


def _decode_cursor(cursor):
    if cursor is None:
        return None
    try:
        encoded = str(cursor).encode("ascii")
        encoded += b"=" * (-len(encoded) % 4)
        value = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
        if (
            not isinstance(value, list)
            or len(value) != 3
            or not all(isinstance(item, str) for item in value)
        ):
            raise ValueError
        return tuple(value)
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("invalid_history_cursor") from None


def _encode_cursor(row):
    value = [str(row["exit_time"] or ""), row["symbol"], row["side"]]
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")


def _fallback_cycles(rows, direction=None, source=None, from_time=None, to_time=None):
    cycles = []
    for row in rows:
        item = dict(row)
        if item.get("symbol") == "ACCOUNT":
            continue
        side = str(item.get("side") or "").upper()
        if direction is not None and side != direction:
            continue
        if source is not None and item.get("strategy_source") != source:
            continue
        exit_time = str(item.get("exit_time") or "")
        if (from_time is not None and exit_time < from_time) or (
            to_time is not None and exit_time > to_time
        ):
            continue
        quantity = float(item.get("quantity") or 0)
        cycles.append(
            {
                "account_id": int(item["account_id"]),
                "symbol": str(item["symbol"]).upper(),
                "direction": side,
                "entry_fills": [{
                    "quantity": quantity,
                    "price": item.get("entry_price") or 0,
                    "strategy_source": item.get("strategy_source"),
                }],
                "exit_fills": [{"quantity": quantity, "price": item.get("exit_price") or 0}],
                "entry_quantity": quantity,
                "entry_time": item.get("entry_time"),
                "exit_time": item.get("exit_time"),
                "pnl": float(item.get("net_pnl") or 0),
                "income_count": int(item.get("income_count") or 1),
                "close_count": int(item.get("income_count") or 1),
            }
        )
    return cycles


def fetch_trade_history_summaries(
    account_id: int,
    *,
    cursor: str | None = None,
    limit: int = 20,
    symbol: str | None = None,
    direction: str | None = None,
    source: str | None = None,
    from_time: str | None = None,
    to_time: str | None = None,
) -> dict:
    account_id = int(account_id)
    limit = max(1, int(limit))
    direction = str(direction).upper() if direction else None
    source = str(source) if source else None
    from_time = str(from_time) if from_time else None
    to_time = str(to_time) if to_time else None
    cursor_value = _decode_cursor(cursor)

    fills = db.fetch_trade_history_fills(account_id, symbol=symbol)
    if fills:
        cycles = reconstruct_position_cycles(fills)
        _allocate_income(cycles, db.fetch_trade_history_income(account_id, symbol=symbol))
        cycles = [
            cycle for cycle in cycles
            if _cycle_matches(cycle, direction, source, from_time, to_time)
        ]
        reconcile_status = "ok"
    else:
        cycles = _fallback_cycles(
            db.fetch_trade_history_position_trades(account_id, symbol=symbol),
            direction,
            source,
            from_time,
            to_time,
        )
        reconcile_status = "incomplete"

    summaries = _summary_from_cycles(cycles)
    summaries.sort(
        key=lambda row: (str(row["exit_time"] or ""), row["symbol"], row["side"]),
        reverse=True,
    )
    if cursor_value is not None:
        summaries = [
            row for row in summaries
            if (str(row["exit_time"] or ""), row["symbol"], row["side"]) < cursor_value
        ]
    items = summaries[:limit]
    return {
        "items": items,
        "next_cursor": _encode_cursor(items[-1]) if len(summaries) > len(items) else None,
        "stats": _stats_from_cycles(cycles),
        "reconcile_status": reconcile_status,
    }
