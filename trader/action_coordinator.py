"""Pure arbitration shared by live execution and historical event replay."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


def position_side(action: Mapping) -> str:
    explicit = str(action.get("position_side") or "").upper()
    if explicit in {"LONG", "SHORT"}:
        order_side = str(action.get("side") or "").upper()
        if order_side in {"BUY", "SELL"}:
            opening = action.get("action") in {"open", "roll_add"}
            inferred = "LONG" if (order_side == "BUY") == opening else "SHORT"
            if inferred != explicit:
                raise ValueError("action_direction_conflict")
        return explicit
    side = str(action.get("side") or "").upper()
    if side in {"LONG", "SHORT"}:
        return side
    if side not in {"BUY", "SELL"}:
        raise ValueError("action_direction_missing")
    opening = action.get("action") in {"open", "roll_add"}
    return "LONG" if (side == "BUY") == opening else "SHORT"


def priority(action: Mapping) -> int:
    kind = action.get("action")
    if kind == "close":
        return 0 if action.get("is_stop") else 1
    if kind == "partial_close":
        return 2
    if kind == "roll_add":
        return 3
    if kind == "open":
        return 4
    raise ValueError("unsupported_action")


@dataclass
class CoordinatedActions:
    accepted: list[dict]
    rejected: list[dict]


def coordinate_actions(actions: list[dict]) -> CoordinatedActions:
    """One action per account/symbol/direction, risk exits always win."""
    winners = {}
    rejected = []
    for source in actions:
        action = dict(source)
        try:
            action["position_side"] = position_side(action)
            rank = priority(action)
            if not action.get("symbol"):
                raise ValueError("action_symbol_missing")
        except ValueError as exc:
            rejected.append({**action, "status": "blocked", "error": str(exc)})
            continue
        key = (action.get("account_id"), action["symbol"], action["position_side"])
        previous = winners.get(key)
        if previous is None:
            winners[key] = (rank, action)
        elif rank < previous[0]:
            rejected.append({**previous[1], "status": "blocked", "error": "higher_priority_action"})
            winners[key] = (rank, action)
        else:
            rejected.append({**action, "status": "blocked", "error": "higher_priority_action"})
    accepted = [item[1] for item in sorted(winners.values(), key=lambda item: item[0])]
    return CoordinatedActions(accepted, rejected)
