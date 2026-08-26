"""Pure helpers for reconstructing position cycles from exchange fills."""

from collections.abc import Iterable, Mapping


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
    return (
        str(row.get("created_at") or ""),
        str(row.get("id") or ""),
        str(row.get("trade_id") or ""),
    )


def deduplicate_fills(rows: Iterable[Mapping]) -> list[dict]:
    unique = {}
    for original in rows:
        row = dict(original)
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
