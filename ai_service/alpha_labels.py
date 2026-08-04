from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from backtest.alpha_strategy_v2.labels import label_counterfactual_path


def _time(value) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class AlphaStrategyLabeler:
    def __init__(self, store, market_db_path, *, now_fn=None):
        self.store = store
        self.market_db_path = str(market_db_path)
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))

    def _future_candles(self, sample: dict, hours: int = 8) -> list[dict]:
        start = _time(sample["candle_close_time"])
        end = start + timedelta(hours=hours)
        conn = sqlite3.connect(
            f"file:{self.market_db_path}?mode=ro",
            uri=True,
            timeout=10,
        )
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """SELECT time, open, high, low, close
                   FROM futures_candles_15m
                   WHERE symbol=? AND source_env=? AND is_closed=1
                     AND datetime(time) >= datetime(?)
                     AND datetime(time) < datetime(?)
                   ORDER BY datetime(time)""",
                (
                    sample["futures_symbol"],
                    sample["market_env"],
                    _iso(start),
                    _iso(end),
                ),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def label_pending(
        self,
        *,
        market_env: str | None = None,
        limit: int = 1000,
    ) -> dict:
        cutoff = self.now_fn() - timedelta(hours=8)
        samples = self.store.pending_alpha_strategy_samples(
            before_time=_iso(cutoff),
            market_env=market_env,
            limit=limit,
        )
        result = {
            "checked": len(samples),
            "labeled": 0,
            "waiting_for_candles": 0,
            "missing": 0,
        }
        for sample in samples:
            features = sample.get("features") or {}
            entry = float(features.get("current_price") or 0)
            if entry <= 0:
                self.store.label_alpha_strategy_sample(
                    sample["id"],
                    {"reason": "missing_entry_price"},
                    status="missing",
                )
                result["missing"] += 1
                continue
            candles = self._future_candles(sample)
            required = 32 if sample["stage"] == "setup" else 16
            if len(candles) < required:
                result["waiting_for_candles"] += 1
                continue
            labels = label_counterfactual_path(
                stage=sample["stage"],
                entry_price=entry,
                invalidation_price=(
                    float(features["base_low_2h"]) * 0.99
                    if features.get("base_low_2h") is not None
                    else None
                ),
                breakout_level=features.get("breakout_level"),
                candles=candles,
            )
            self.store.label_alpha_strategy_sample(sample["id"], labels)
            result["labeled"] += 1
        return result

    def sync_execution_outcomes(self, *, limit: int = 2000) -> dict:
        """Mirror real Alpha V2 fills/outcomes for model calibration reports."""
        conn = sqlite3.connect(
            f"file:{self.market_db_path}?mode=ro",
            uri=True,
            timeout=10,
        )
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """SELECT o.account_id,
                          o.signal_event_id AS event_id,
                          o.alpha_stage AS action_type,
                          o.setup_id,
                          o.symbol AS futures_symbol,
                          o.position_id,
                          o.exchange_order_id,
                          o.quantity,
                          o.price AS entry_price,
                          e.invalidation_price,
                          o.ai_model_versions_json,
                          o.created_at AS submitted_at,
                          SUM(t.pnl) AS realized_pnl,
                          MAX(t.exit_reason) AS exit_reason,
                          MAX(t.created_at) AS closed_at
                   FROM orders o
                   LEFT JOIN alpha_signal_events e
                     ON e.event_id=o.signal_event_id
                   LEFT JOIN trades t
                     ON t.account_id=o.account_id
                    AND t.position_id=o.position_id
                   WHERE o.signal_event_id IS NOT NULL
                     AND o.alpha_stage IS NOT NULL
                     AND o.order_type='MARKET'
                   GROUP BY o.account_id, o.signal_event_id, o.alpha_stage
                   ORDER BY datetime(o.created_at) DESC
                   LIMIT ?""",
                (int(limit),),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            return {"synced": 0, "status": "unavailable", "error": str(exc)}
        finally:
            conn.close()
        outcomes = []
        for row in rows:
            item = dict(row)
            pnl = item.get("realized_pnl")
            entry = float(item.get("entry_price") or 0)
            invalidation = float(item.get("invalidation_price") or 0)
            quantity = float(item.get("quantity") or 0)
            risk_notional = abs(entry - invalidation) * quantity
            item["realized_r"] = (
                float(pnl) / risk_notional
                if pnl is not None and risk_notional > 0
                else None
            )
            item["status"] = "closed" if item.get("closed_at") else "open"
            try:
                item["model_versions"] = json.loads(
                    item.pop("ai_model_versions_json") or "{}"
                )
            except (TypeError, ValueError):
                item["model_versions"] = {}
            outcomes.append(item)
        synced = self.store.upsert_alpha_strategy_execution_outcomes(outcomes)
        return {"synced": synced, "status": "ok"}
