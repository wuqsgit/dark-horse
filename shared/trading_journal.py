"""Durable execution intents and immutable initial risk plans."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone


SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_plans (
    account_id INTEGER NOT NULL, position_id TEXT NOT NULL,
    symbol TEXT NOT NULL, side TEXT NOT NULL, strategy_version TEXT NOT NULL,
    initial_entry REAL NOT NULL, initial_stop REAL NOT NULL,
    initial_quantity REAL NOT NULL, initial_risk REAL NOT NULL,
    plan_json TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(account_id, position_id)
);
CREATE TABLE IF NOT EXISTS execution_intents (
    account_id INTEGER NOT NULL, intent_id TEXT NOT NULL,
    symbol TEXT NOT NULL, position_side TEXT NOT NULL, action_type TEXT NOT NULL,
    client_order_id TEXT NOT NULL, status TEXT NOT NULL,
    action_json TEXT NOT NULL, order_json TEXT, error TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY(account_id, intent_id)
);
CREATE INDEX IF NOT EXISTS idx_execution_intents_pending
ON execution_intents(account_id, status, symbol, position_side);
CREATE TABLE IF NOT EXISTS strategy_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL,
    symbol TEXT NOT NULL, side TEXT, event_type TEXT NOT NULL,
    event_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_strategy_events_time
ON strategy_events(account_id, created_at);
CREATE TABLE IF NOT EXISTS execution_effects (
    account_id INTEGER NOT NULL, intent_id TEXT NOT NULL,
    effect_type TEXT NOT NULL, created_at TEXT NOT NULL,
    PRIMARY KEY(account_id, intent_id, effect_type)
);
"""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, default=str)


def intent_id(action):
    kind = action["action"]
    if kind == "open":
        anchor = action.get("alpha_setup_id") or action.get("setup_id") or action.get("scan_id")
    elif kind == "roll_add":
        if not action.get("position_id") or action.get("roll_layer") is None:
            raise ValueError("stable_action_identity_missing")
        anchor = f"{action.get('position_id')}:{action.get('roll_layer')}"
    else:
        if not action.get("position_id"):
            raise ValueError("stable_action_identity_missing")
        anchor = f"{action.get('position_id')}:{action.get('management_stage') or action.get('reason')}"
    if not anchor:
        raise ValueError("stable_action_identity_missing")
    raw = f"{action['symbol']}:{action['position_side']}:{kind}:{anchor}"
    return hashlib.sha256(raw.encode()).hexdigest()[:28]


class TradingJournal:
    def __init__(self, account_id):
        self.account_id = int(account_id)

    def event(self, action, event_type, details):
        from shared.db import get_conn
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO strategy_events(account_id,symbol,side,event_type,event_json,created_at) VALUES(?,?,?,?,?,?)",
                (self.account_id, action.get("symbol", "?"), action.get("position_side"), event_type, _json(details), _now()),
            )

    def claim(self, action):
        from shared.db import get_conn
        key = intent_id(action)
        client_id = action.get("client_order_id") or "DH-" + hashlib.sha256(f"{self.account_id}:{key}".encode()).hexdigest()[:28]
        now = _now()
        with get_conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            pending = conn.execute(
                "SELECT * FROM execution_intents WHERE account_id=? AND symbol=? AND position_side=? "
                "AND status IN ('submitting','unknown','filled') LIMIT 1",
                (self.account_id, action["symbol"], action["position_side"]),
            ).fetchone()
            existing = conn.execute(
                "SELECT * FROM execution_intents WHERE account_id=? AND intent_id=?", (self.account_id, key),
            ).fetchone()
            if existing or (pending and action["action"] in {"open", "roll_add"}):
                return False, dict(existing or pending)
            conn.execute(
                "INSERT INTO execution_intents VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (self.account_id, key, action["symbol"], action["position_side"], action["action"],
                 client_id, "submitting", _json(action), None, None, now, now),
            )
        return True, {"intent_id": key, "client_order_id": client_id, "status": "submitting"}

    def finish(self, key, status, order=None, error=None):
        from shared.db import get_conn
        with get_conn() as conn:
            conn.execute(
                "UPDATE execution_intents SET status=?,order_json=?,error=?,updated_at=? WHERE account_id=? AND intent_id=?",
                (status, _json(order) if order else None, str(error) if error else None, _now(), self.account_id, key),
            )

    def save_plan(self, action):
        from shared.db import get_conn
        entry, stop, qty = (float(action[k]) for k in ("entry_price", "stop_loss", "quantity"))
        direction = 1 if action["position_side"] == "LONG" else -1
        if min(entry, stop, qty) <= 0 or direction * (entry - stop) <= 0:
            raise ValueError("invalid_initial_risk_plan")
        with get_conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO trade_plans VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (self.account_id, action["position_id"], action["symbol"], action["position_side"],
                 "rules-lifecycle-v1", entry, stop, qty, abs(entry-stop)*qty, _json(action), _now()),
            )

    def plan(self, position_id):
        from shared.db import get_conn
        with get_conn() as conn:
            row = conn.execute("SELECT * FROM trade_plans WHERE account_id=? AND position_id=?", (self.account_id, position_id)).fetchone()
            return dict(row) if row else None
