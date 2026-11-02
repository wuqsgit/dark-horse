"""Read-only lifecycle audit report; never updates or resolves live orders."""
import argparse
import json
import sqlite3
from collections import Counter
from pathlib import Path


def report(path, account_id, limit=1000):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        pending = [dict(r) for r in conn.execute(
            "SELECT intent_id,symbol,position_side,action_type,status,client_order_id,updated_at "
            "FROM execution_intents WHERE account_id=? AND status!='completed' ORDER BY updated_at DESC LIMIT ?",
            (account_id, limit),
        )]
        outcomes, differences = Counter(), []
        for row in conn.execute(
            "SELECT symbol,side,event_json,created_at FROM strategy_events "
            "WHERE account_id=? AND event_type='management_comparison' ORDER BY id DESC LIMIT ?",
            (account_id, limit),
        ):
            event = json.loads(row["event_json"])
            outcomes[event["outcome"]] += 1
            proposed = event.get("proposed_action") or {}
            actual = event.get("actual_actions") or []
            proposed_kind = proposed.get("action", "hold")
            actual_kinds = [a["action"] for a in actual] or ["hold"]
            if actual_kinds != [proposed_kind]:
                differences.append({"symbol": row["symbol"], "side": row["side"], "time": row["created_at"],
                                    "old": actual_kinds, "new": proposed_kind, "reason": proposed.get("reason")})
        return {"account_id": account_id, "pending_intents": pending, "outcomes": dict(outcomes),
                "action_differences": differences, "note": "Decision comparison only; not a profitability evaluation"}
    finally:
        conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path(__file__).resolve().parents[1] / "alphadog.db")
    parser.add_argument("--account", type=int, required=True)
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()
    print(json.dumps(report(args.database, args.account, args.limit), indent=2))
