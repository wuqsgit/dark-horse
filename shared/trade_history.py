"""Pure helpers for reconstructing position cycles from exchange fills."""

import base64
import json
import math
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone

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


def _optional_number(value):
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive_number(value):
    number = _optional_number(value)
    return number if number is not None and number > 0 else None


def _strategy_source(value):
    source = str(value or "").strip()
    if source.lower() in {
        "",
        "unknown",
        "binance_user_trades",
        "binance_user_trades_fallback",
    }:
        return None
    return source


def _cycle_source(cycle):
    override = _strategy_source(cycle.get("_strategy_source_override"))
    if override:
        return override
    for fill in cycle["entry_fills"]:
        source = _strategy_source(
            fill.get("strategy_source") or fill.get("source")
        )
        if source:
            return source
    return None


def _income_identity(row):
    account_id = int(row.get("account_id") or 0)
    income_type = str(row.get("income_type") or "").upper()
    symbol = str(row.get("symbol") or "").upper()
    trade_id = _normalized_trade_id(row.get("trade_id"))
    if trade_id:
        return ("trade", account_id, symbol, income_type, trade_id)
    income_id = str(row.get("income_id") or "").strip()
    if income_id:
        prefix = f"A{account_id}:"
        normalized = (
            income_id[len(prefix):]
            if income_id.startswith(prefix)
            else income_id
        )
        return ("income", account_id, normalized)
    return (
        "facts",
        account_id,
        symbol,
        income_type,
        str(row.get("order_id") or ""),
        str(row.get("income_time") or ""),
        _optional_number(row.get("income")),
        str(row.get("asset") or ""),
    )


def _deduplicate_income(rows):
    unique = {}
    for original in rows:
        row = dict(original)
        if str(row.get("income_type") or "").upper() not in {
            "REALIZED_PNL",
            "COMMISSION",
            "FUNDING_FEE",
        }:
            continue
        unique.setdefault(_income_identity(row), row)
    return sorted(
        unique.values(),
        key=lambda row: (
            str(row.get("income_time") or ""),
            int(row.get("id") or 0),
        ),
    )


def _cycle_notional(rows):
    total = 0.0
    for row in rows:
        quantity = _positive_number(row.get("quantity"))
        price = _positive_number(row.get("price"))
        if quantity is None or price is None:
            return None
        total += quantity * price
    return total if rows else None


def _expected_realized_pnl(cycle):
    entry_notional = _cycle_notional(cycle["entry_fills"])
    exit_notional = _cycle_notional(cycle["exit_fills"])
    if entry_notional is None or exit_notional is None:
        return None
    if cycle["direction"] == "SHORT":
        return entry_notional - exit_notional
    return exit_notional - entry_notional


def _income_reconciles(actual, expected):
    tolerance = max(1e-8, abs(expected) * 1e-6)
    return abs(actual - expected) <= tolerance


def _allocate_income(cycles, income_rows):
    by_trade_id = {}
    for cycle in cycles:
        cycle["pnl"] = None
        cycle["income_count"] = 0
        cycle["_income_total"] = 0.0
        cycle["_realized_income"] = 0.0
        cycle["_realized_count"] = 0
        cycle["_invalid_income"] = False
        for trade_id in cycle["trade_ids"]:
            normalized = _normalized_trade_id(trade_id)
            if normalized:
                by_trade_id.setdefault((cycle["symbol"], normalized), cycle)

    for income in _deduplicate_income(income_rows):
        income_type = str(income.get("income_type") or "").upper()
        symbol = str(income.get("symbol") or "").upper()
        cycle = by_trade_id.get(
            (symbol, _normalized_trade_id(income.get("trade_id")))
        )
        if cycle is None:
            income_time = str(income.get("income_time") or "")
            matches = [
                item for item in cycles
                if item["symbol"] == symbol
                and item.get("entry_time")
                and item.get("exit_time")
                and item["entry_time"] <= income_time <= item["exit_time"]
            ]
            if len(matches) == 1:
                cycle = matches[0]
        if cycle is not None:
            cycle["income_count"] += 1
            amount = _optional_number(income.get("income"))
            if amount is None:
                cycle["_invalid_income"] = True
                continue
            cycle["_income_total"] += amount
            if income_type == "REALIZED_PNL":
                cycle["_realized_income"] += amount
                cycle["_realized_count"] += 1

    for cycle in cycles:
        expected = _expected_realized_pnl(cycle)
        if cycle.pop("_invalid_income") or not cycle["_realized_count"]:
            cycle["reconcile_status"] = "incomplete"
        elif expected is None:
            cycle["reconcile_status"] = "incomplete"
        elif _income_reconciles(cycle["_realized_income"], expected):
            cycle["pnl"] = cycle["_income_total"]
            cycle["reconcile_status"] = "ok"
        else:
            cycle["pnl"] = cycle["_income_total"]
            cycle["reconcile_status"] = "mismatch"
        cycle.pop("_income_total")
        cycle.pop("_realized_income")
        cycle.pop("_realized_count")


def _cycle_margin(cycle):
    margins = {
        _positive_number(fill.get("margin"))
        for fill in cycle["entry_fills"]
        if _positive_number(fill.get("margin")) is not None
    }
    if len(margins) == 1:
        return margins.pop()
    leverages = {
        _positive_number(fill.get("leverage"))
        for fill in cycle["entry_fills"]
        if _positive_number(fill.get("leverage")) is not None
    }
    if len(leverages) != 1:
        return None
    entry_notional = _cycle_notional(cycle["entry_fills"])
    if entry_notional is None:
        return None
    return entry_notional / leverages.pop()


def _cycle_is_covered(candidate, fill_cycle):
    if (
        candidate["account_id"] != fill_cycle["account_id"]
        or candidate["symbol"] != fill_cycle["symbol"]
        or candidate["direction"] != fill_cycle["direction"]
    ):
        return False
    candidate_entry = str(candidate.get("entry_time") or "")
    candidate_exit = str(candidate.get("exit_time") or "")
    fill_entry = str(fill_cycle.get("entry_time") or "")
    fill_exit = str(fill_cycle.get("exit_time") or "")
    if candidate_entry and candidate_exit and fill_entry and fill_exit:
        if (candidate_entry, candidate_exit) == (fill_entry, fill_exit):
            return True
        return max(candidate_entry, fill_entry) < min(candidate_exit, fill_exit)
    return bool(candidate_exit and candidate_exit == fill_exit)


def _parse_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _opening_order_source(order, cycle):
    source = _strategy_source(order.get("strategy_source"))
    if not source or str(order.get("order_type") or "").upper() != "MARKET":
        return None
    entry_side = "BUY" if cycle["direction"] == "LONG" else "SELL"
    if str(order.get("side") or "").upper() != entry_side:
        return None
    reason = str(order.get("reason") or "").lower()
    if any(marker in reason for marker in ("roll", "reduce", "close")):
        return None
    return source


def _unique_source(values):
    sources = {_strategy_source(value) for value in values}
    sources.discard(None)
    return sources.pop() if len(sources) == 1 else None


def _recover_cycle_sources(cycles, legacy_cycles, orders):
    for cycle in cycles:
        if _cycle_source(cycle):
            continue

        legacy_source = _unique_source(
            _cycle_source(item)
            for item in legacy_cycles
            if _cycle_is_covered(item, cycle)
        )
        if legacy_source:
            cycle["_strategy_source_override"] = legacy_source
            continue

        entry_order_ids = {
            str(fill.get("exchange_order_id") or "")
            for fill in cycle["entry_fills"]
            if fill.get("exchange_order_id") not in (None, "")
        }
        direct_source = _unique_source(
            _opening_order_source(order, cycle)
            for order in orders
            if str(order.get("symbol") or "").upper() == cycle["symbol"]
            and str(order.get("exchange_order_id") or "") in entry_order_ids
        )
        if direct_source:
            cycle["_strategy_source_override"] = direct_source
            continue

        entry_time = _parse_time(cycle.get("entry_time"))
        if entry_time is None:
            continue
        earliest = entry_time - timedelta(minutes=5)
        latest = entry_time + timedelta(minutes=1)
        temporal_source = _unique_source(
            _opening_order_source(order, cycle)
            for order in orders
            if str(order.get("symbol") or "").upper() == cycle["symbol"]
            and (order_time := _parse_time(order.get("created_at"))) is not None
            and earliest <= order_time <= latest
        )
        if temporal_source:
            cycle["_strategy_source_override"] = temporal_source


def _cycle_matches(cycle, direction=None, source=None, from_time=None, to_time=None):
    exit_time = str(cycle["exit_time"] or "")
    return (
        (direction is None or cycle["direction"] == direction)
        and (source is None or _cycle_source(cycle) == source)
        and (from_time is None or bool(exit_time) and exit_time >= from_time)
        and (to_time is None or bool(exit_time) and exit_time <= to_time)
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
                "exit_quantity": 0.0,
                "entry_notional": 0.0,
                "exit_notional": 0.0,
                "pnl": 0.0,
                "margin": 0.0,
                "has_exact_quantity": True,
                "has_exact_entry": True,
                "has_exact_exit": True,
                "has_complete_pnl": True,
                "has_exact_margin": True,
                "position_count": 0,
                "close_count": 0,
                "entry_time": cycle["entry_time"],
                "exit_time": cycle["exit_time"],
                "strategy_sources": set(),
            },
        )
        entry_quantity = _positive_number(cycle.get("entry_quantity"))
        exit_quantity = _positive_number(cycle.get("exit_quantity"))
        entry_notional = _cycle_notional(cycle["entry_fills"])
        exit_notional = _cycle_notional(cycle["exit_fills"])
        if entry_quantity is None:
            summary["has_exact_quantity"] = False
            summary["has_exact_entry"] = False
        else:
            summary["quantity"] += entry_quantity
        if exit_quantity is None:
            summary["has_exact_exit"] = False
        else:
            summary["exit_quantity"] += exit_quantity
        if entry_notional is None:
            summary["has_exact_entry"] = False
        else:
            summary["entry_notional"] += entry_notional
        if exit_notional is None:
            summary["has_exact_exit"] = False
        else:
            summary["exit_notional"] += exit_notional
        pnl = _optional_number(cycle.get("pnl"))
        if pnl is None:
            summary["has_complete_pnl"] = False
        else:
            summary["pnl"] += pnl
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
        entry_times = [
            value for value in (summary["entry_time"], cycle["entry_time"])
            if value
        ]
        exit_times = [
            value for value in (summary["exit_time"], cycle["exit_time"])
            if value
        ]
        summary["entry_time"] = min(entry_times) if entry_times else None
        summary["exit_time"] = max(exit_times) if exit_times else None

    summaries = []
    for summary in grouped.values():
        quantity = summary.pop("quantity")
        exit_quantity = summary.pop("exit_quantity")
        entry_notional = summary.pop("entry_notional")
        exit_notional = summary.pop("exit_notional")
        pnl = summary.pop("pnl")
        margin = summary.pop("margin")
        has_exact_quantity = summary.pop("has_exact_quantity")
        has_exact_entry = summary.pop("has_exact_entry")
        has_exact_exit = summary.pop("has_exact_exit")
        has_complete_pnl = summary.pop("has_complete_pnl")
        has_exact_margin = summary.pop("has_exact_margin")
        summary["strategy_sources"] = sorted(summary["strategy_sources"])
        summary["quantity"] = (
            int(quantity) if quantity.is_integer() else quantity
        ) if has_exact_quantity else None
        summary["entry_price"] = (
            entry_notional / quantity
            if has_exact_entry and quantity
            else None
        )
        summary["exit_price"] = (
            exit_notional / exit_quantity
            if has_exact_exit and exit_quantity
            else None
        )
        summary["pnl"] = pnl if has_complete_pnl else None
        summary["pnl_pct"] = (
            pnl / margin * 100
            if has_complete_pnl and has_exact_margin and margin
            else None
        )
        summaries.append(summary)
    return summaries


def _stats_from_cycles(cycles):
    pnl_values = [
        value
        for cycle in cycles
        if (value := _optional_number(cycle.get("pnl"))) is not None
    ]
    win_count = sum(pnl > 0 for pnl in pnl_values)
    loss_count = sum(pnl < 0 for pnl in pnl_values)
    decided_count = win_count + loss_count
    return {
        "total_cycles": len(cycles),
        "position_count": len(cycles),
        "total_pnl": (
            sum(pnl_values) if len(pnl_values) == len(cycles) else None
        ),
        "win_count": win_count,
        "loss_count": loss_count,
        "flat_count": sum(pnl == 0 for pnl in pnl_values),
        "win_rate": win_count / decided_count * 100 if decided_count else None,
    }


def _reconcile_status(cycles):
    statuses = {cycle.get("reconcile_status") for cycle in cycles}
    if "mismatch" in statuses:
        return "mismatch"
    if "incomplete" in statuses:
        return "incomplete"
    return "ok"


def _decode_cursor(cursor):
    if cursor is None:
        return None
    try:
        encoded = str(cursor).encode("ascii")
        encoded += b"=" * (-len(encoded) % 4)
        value = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
        if (
            not isinstance(value, dict)
            or value.get("version") != 1
            or not isinstance(value.get("after"), list)
            or len(value["after"]) != 3
            or not all(isinstance(item, str) for item in value["after"])
            or not isinstance(value.get("as_of"), dict)
            or set(value["as_of"]) != {
                "fills",
                "income",
                "position_trades",
                "orders",
            }
            or not all(
                isinstance(item, int) and not isinstance(item, bool) and item >= 0
                for item in value["as_of"].values()
            )
        ):
            raise ValueError
        return tuple(value["after"]), value["as_of"]
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("invalid_history_cursor") from None


def _encode_cursor(row, watermarks):
    value = {
        "version": 1,
        "as_of": watermarks,
        "after": [str(row["exit_time"] or ""), row["symbol"], row["side"]],
    }
    return base64.urlsafe_b64encode(
        json.dumps(value, separators=(",", ":"), sort_keys=True).encode()
    ).decode().rstrip("=")


def _legacy_cycle_identity(item):
    account_id = int(item.get("account_id") or 0)
    symbol = str(item.get("symbol") or "").upper()
    side = str(item.get("side") or "").upper()
    entry_time = str(item.get("entry_time") or "")
    exit_time = str(item.get("exit_time") or "")
    if symbol and side in {"LONG", "SHORT"} and entry_time and exit_time:
        return (
            "facts",
            account_id,
            symbol,
            side,
            entry_time,
            exit_time,
            _optional_number(item.get("entry_price")),
            _optional_number(item.get("exit_price")),
            _optional_number(item.get("quantity")),
            _optional_number(item.get("net_pnl")),
            int(item.get("income_count") or 0),
            str(item.get("strategy_source") or ""),
            str(item.get("source") or ""),
        )
    position_trade_id = str(item.get("position_trade_id") or "").strip()
    if position_trade_id:
        return ("position_trade_id", account_id, position_trade_id)
    return ("row", account_id, int(item.get("id") or 0))


def _deduplicate_legacy_rows(rows):
    unique = {}
    for original in rows:
        row = dict(original)
        unique.setdefault(_legacy_cycle_identity(row), row)
    return list(unique.values())


def _fallback_cycles(rows, direction=None, source=None, from_time=None, to_time=None):
    cycles = []
    for item in _deduplicate_legacy_rows(rows):
        if item.get("symbol") == "ACCOUNT":
            continue
        side = str(item.get("side") or "").upper()
        if side not in {"LONG", "SHORT"}:
            continue
        if direction is not None and side != direction:
            continue
        strategy_source = _strategy_source(item.get("strategy_source"))
        if source is not None and strategy_source != source:
            continue
        exit_time = str(item.get("exit_time") or "")
        if (from_time is not None and exit_time < from_time) or (
            to_time is not None and exit_time > to_time
        ):
            continue
        quantity = _positive_number(item.get("quantity"))
        entry_price = _positive_number(item.get("entry_price"))
        exit_price = _positive_number(item.get("exit_price"))
        pnl = _optional_number(item.get("net_pnl"))
        cycles.append(
            {
                "account_id": int(item["account_id"]),
                "symbol": str(item["symbol"]).upper(),
                "direction": side,
                "entry_fills": [{
                    "quantity": quantity,
                    "price": entry_price,
                    "strategy_source": strategy_source,
                }],
                "exit_fills": [{"quantity": quantity, "price": exit_price}],
                "entry_quantity": quantity,
                "exit_quantity": quantity,
                "entry_time": item.get("entry_time"),
                "exit_time": item.get("exit_time"),
                "pnl": pnl,
                "income_count": int(item.get("income_count") or 0),
                "close_count": int(item.get("income_count") or 0),
                "reconcile_status": "incomplete",
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
    cursor_data = _decode_cursor(cursor)
    if cursor_data is None:
        cursor_value = None
        watermarks = None
    else:
        cursor_value, watermarks = cursor_data

    snapshot = db.fetch_trade_history_snapshot(
        account_id, symbol=symbol, watermarks=watermarks
    )
    watermarks = snapshot["watermarks"]
    fills = snapshot["fills"]
    income_rows = snapshot["income"]
    legacy_rows = snapshot["position_trades"]
    orders = snapshot["orders"]

    fill_cycles = reconstruct_position_cycles(fills)
    legacy_cycles = _fallback_cycles(legacy_rows)
    _recover_cycle_sources(fill_cycles, legacy_cycles, orders)
    _allocate_income(fill_cycles, income_rows)

    selected_fill_cycles = [
        cycle for cycle in fill_cycles
        if _cycle_matches(cycle, direction, source, from_time, to_time)
    ]
    selected_legacy_cycles = [
        cycle for cycle in legacy_cycles
        if not any(
            _cycle_is_covered(cycle, fill_cycle)
            for fill_cycle in fill_cycles
        )
        and _cycle_matches(cycle, direction, source, from_time, to_time)
    ]
    cycles = selected_fill_cycles + selected_legacy_cycles
    reconcile_status = _reconcile_status(cycles)

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
        "next_cursor": (
            _encode_cursor(items[-1], watermarks)
            if len(summaries) > len(items)
            else None
        ),
        "stats": _stats_from_cycles(cycles),
        "reconcile_status": reconcile_status,
    }
