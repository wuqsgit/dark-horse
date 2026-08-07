"""SQLite repository for Alpha Strategy V2 snapshots, states, and events."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from alpha_engine.strategy.models import (
    ActionType,
    AlphaSignalState,
    ApplyResult,
    StateRecord,
    TransitionResult,
)
from alpha_engine.strategy.feature_builder import AlphaFeatureSnapshot
from shared.db import get_conn


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(value) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _event_id(
    market_env: str,
    futures_symbol: str,
    transition: TransitionResult,
) -> str:
    raw = "|".join(
        (
            market_env,
            futures_symbol.upper(),
            transition.setup_id or "",
            transition.to_state.value,
            _iso(transition.candle_close_time) or "",
            str(transition.previous_version + 1),
        )
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class AlphaStrategyRepository:
    def save_feature_snapshot(self, snapshot: AlphaFeatureSnapshot) -> bool:
        conn = get_conn()
        try:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO alpha_feature_snapshots
                   (snapshot_id, market_env, alpha_symbol, futures_symbol,
                    candle_close_time, feature_schema_version,
                    data_quality_status, data_quality_json, features_json,
                    created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    snapshot.snapshot_id,
                    snapshot.market_env,
                    snapshot.alpha_symbol,
                    snapshot.futures_symbol,
                    _iso(snapshot.candle_close_time),
                    snapshot.feature_schema_version,
                    snapshot.quality.get("status") or "unknown",
                    json.dumps(dict(snapshot.quality), ensure_ascii=False),
                    json.dumps(dict(snapshot.features), ensure_ascii=False),
                    _iso(datetime.now(timezone.utc)),
                ),
            )
            conn.commit()
            return cursor.rowcount == 1
        finally:
            conn.close()

    def get_state(self, market_env: str, futures_symbol: str) -> StateRecord | None:
        conn = get_conn()
        try:
            row = conn.execute(
                """SELECT * FROM alpha_signal_states
                   WHERE market_env=? AND futures_symbol=?""",
                (market_env, futures_symbol.upper()),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        return StateRecord(
            market_env=row["market_env"],
            futures_symbol=row["futures_symbol"],
            alpha_symbol=row["alpha_symbol"],
            state=AlphaSignalState(row["state"]),
            setup_type=row["setup_type"],
            setup_id=row["setup_id"],
            state_version=int(row["state_version"]),
            started_at=_parse(row["started_at"]),
            updated_at=_parse(row["updated_at"]),
            expires_at=_parse(row["expires_at"]),
            last_candle_close_time=_parse(row["last_candle_close_time"]),
            snapshot_id=row["snapshot_id"],
            reference_price=row["reference_price"],
            base_low=row["base_low"],
            base_high=row["base_high"],
            breakout_level=row["breakout_level"],
            invalidation_price=row["invalidation_price"],
            setup_probability=row["p_setup_success"],
            followthrough_probability=row["p_followthrough"],
            fakeout_probability=row["p_fakeout"],
            expected_r=row["expected_r"],
            model_versions=json.loads(row["model_versions_json"] or "{}"),
            reasons=tuple(json.loads(row["reason_codes_json"] or "[]")),
            metrics=json.loads(row["metrics_json"] or "{}"),
        )

    def apply_transition(
        self,
        *,
        market_env: str,
        futures_symbol: str,
        alpha_symbol: str | None,
        transition: TransitionResult,
        strategy_mode: str = "signal",
    ) -> ApplyResult:
        if not transition.changed:
            return ApplyResult(False, None)
        symbol = futures_symbol.upper()
        event_id = _event_id(market_env, symbol, transition)
        next_version = transition.previous_version + 1
        conn = get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """SELECT state_version FROM alpha_signal_states
                   WHERE market_env=? AND futures_symbol=?""",
                (market_env, symbol),
            ).fetchone()
            if existing:
                cursor = conn.execute(
                    """UPDATE alpha_signal_states
                       SET alpha_symbol=?, state=?, setup_type=?, setup_id=?,
                           state_version=?, started_at=?, updated_at=?, expires_at=?,
                           last_candle_close_time=?, snapshot_id=?,
                           reference_price=?, base_low=?, base_high=?,
                           breakout_level=?, invalidation_price=?,
                           p_setup_success=?, p_followthrough=?, p_fakeout=?,
                           expected_r=?, model_versions_json=?,
                           reason_codes_json=?, metrics_json=?
                       WHERE market_env=? AND futures_symbol=? AND state_version=?""",
                    (
                        alpha_symbol,
                        transition.to_state.value,
                        transition.setup_type,
                        transition.setup_id,
                        next_version,
                        _iso(transition.started_at),
                        _iso(transition.candle_close_time),
                        _iso(transition.expires_at),
                        _iso(transition.candle_close_time),
                        transition.snapshot_id,
                        transition.reference_price,
                        transition.base_low,
                        transition.base_high,
                        transition.breakout_level,
                        transition.invalidation_price,
                        transition.setup_probability,
                        transition.followthrough_probability,
                        transition.fakeout_probability,
                        transition.expected_r,
                        json.dumps(dict(transition.model_versions), ensure_ascii=False),
                        json.dumps(list(transition.reasons), ensure_ascii=False),
                        json.dumps(dict(transition.metrics), ensure_ascii=False),
                        market_env,
                        symbol,
                        transition.previous_version,
                    ),
                )
                if cursor.rowcount != 1:
                    conn.rollback()
                    return ApplyResult(False, event_id)
            else:
                if transition.previous_version != 0:
                    conn.rollback()
                    return ApplyResult(False, event_id)
                try:
                    conn.execute(
                        """INSERT INTO alpha_signal_states
                           (market_env, futures_symbol, alpha_symbol, state,
                            setup_type, setup_id, state_version, started_at,
                            updated_at, expires_at, last_candle_close_time,
                            snapshot_id, reference_price, base_low, base_high,
                            breakout_level, invalidation_price, p_setup_success,
                            p_followthrough, p_fakeout, expected_r,
                            model_versions_json, reason_codes_json, metrics_json)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            market_env,
                            symbol,
                            alpha_symbol,
                            transition.to_state.value,
                            transition.setup_type,
                            transition.setup_id,
                            next_version,
                            _iso(transition.started_at),
                            _iso(transition.candle_close_time),
                            _iso(transition.expires_at),
                            _iso(transition.candle_close_time),
                            transition.snapshot_id,
                            transition.reference_price,
                            transition.base_low,
                            transition.base_high,
                            transition.breakout_level,
                            transition.invalidation_price,
                            transition.setup_probability,
                            transition.followthrough_probability,
                            transition.fakeout_probability,
                            transition.expected_r,
                            json.dumps(dict(transition.model_versions), ensure_ascii=False),
                            json.dumps(list(transition.reasons), ensure_ascii=False),
                            json.dumps(dict(transition.metrics), ensure_ascii=False),
                        ),
                    )
                except sqlite3.IntegrityError:
                    conn.rollback()
                    return ApplyResult(False, event_id)

            conn.execute(
                """INSERT OR IGNORE INTO alpha_signal_events
                   (event_id, market_env, strategy_mode, futures_symbol,
                    alpha_symbol, setup_id, from_state, to_state, state_version,
                    action_type, event_time, candle_close_time, snapshot_id,
                    reference_price, invalidation_price, max_position_factor,
                    expires_at, reason_codes_json, ai_decision_json, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_id,
                    market_env,
                    str(strategy_mode or "signal").lower(),
                    symbol,
                    alpha_symbol,
                    transition.setup_id,
                    transition.from_state.value,
                    transition.to_state.value,
                    next_version,
                    transition.action_type.value,
                    _iso(transition.candle_close_time),
                    _iso(transition.candle_close_time),
                    transition.snapshot_id,
                    transition.reference_price,
                    transition.invalidation_price,
                    transition.max_position_factor,
                    _iso(transition.expires_at),
                    json.dumps(list(transition.reasons), ensure_ascii=False),
                    json.dumps(
                        {
                            "p_setup_success": transition.setup_probability,
                            "p_followthrough": transition.followthrough_probability,
                            "p_fakeout": transition.fakeout_probability,
                            "expected_r": transition.expected_r,
                            "model_versions": dict(transition.model_versions),
                            "metrics": dict(transition.metrics),
                        },
                        ensure_ascii=False,
                    ),
                    _iso(datetime.now(timezone.utc)),
                ),
            )
            conn.commit()
            return ApplyResult(True, event_id)
        finally:
            conn.close()

    def save_observation(
        self,
        *,
        market_env: str,
        futures_symbol: str,
        alpha_symbol: str | None,
        transition: TransitionResult,
    ) -> bool:
        """Persist an evaluated closed candle without creating a signal event.

        Unchanged states are still operationally meaningful: persisting their
        candle cursor prevents the worker from evaluating the same closed bar
        every minute and makes collecting/idle symbols visible to monitoring.
        """
        if transition.changed:
            raise ValueError("save_observation only accepts unchanged transitions")
        symbol = futures_symbol.upper()
        state = transition.as_state_record(
            market_env,
            symbol,
            alpha_symbol,
        )
        conn = get_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                """SELECT state_version, last_candle_close_time
                   FROM alpha_signal_states
                   WHERE market_env=? AND futures_symbol=?""",
                (market_env, symbol),
            ).fetchone()
            if existing:
                if int(existing["state_version"]) != transition.previous_version:
                    conn.rollback()
                    return False
                previous_close = _parse(existing["last_candle_close_time"])
                if (
                    previous_close is not None
                    and transition.candle_close_time <= previous_close
                ):
                    conn.rollback()
                    return False
                cursor = conn.execute(
                    """UPDATE alpha_signal_states
                       SET alpha_symbol=?, state=?, setup_type=?, setup_id=?,
                           started_at=?, updated_at=?, expires_at=?,
                           last_candle_close_time=?, snapshot_id=?,
                           reference_price=?, base_low=?, base_high=?,
                           breakout_level=?, invalidation_price=?,
                           p_setup_success=?, p_followthrough=?, p_fakeout=?,
                           expected_r=?, model_versions_json=?,
                           reason_codes_json=?, metrics_json=?
                       WHERE market_env=? AND futures_symbol=? AND state_version=?""",
                    (
                        alpha_symbol,
                        state.state.value,
                        state.setup_type,
                        state.setup_id,
                        _iso(state.started_at),
                        _iso(state.updated_at),
                        _iso(state.expires_at),
                        _iso(state.last_candle_close_time),
                        state.snapshot_id,
                        state.reference_price,
                        state.base_low,
                        state.base_high,
                        state.breakout_level,
                        state.invalidation_price,
                        state.setup_probability,
                        state.followthrough_probability,
                        state.fakeout_probability,
                        state.expected_r,
                        json.dumps(dict(state.model_versions), ensure_ascii=False),
                        json.dumps(list(state.reasons), ensure_ascii=False),
                        json.dumps(dict(state.metrics), ensure_ascii=False),
                        market_env,
                        symbol,
                        transition.previous_version,
                    ),
                )
                if cursor.rowcount != 1:
                    conn.rollback()
                    return False
            else:
                if (
                    transition.previous_version != 0
                    or transition.from_state != AlphaSignalState.IDLE
                    or transition.to_state != AlphaSignalState.IDLE
                ):
                    conn.rollback()
                    return False
                try:
                    conn.execute(
                        """INSERT INTO alpha_signal_states
                           (market_env, futures_symbol, alpha_symbol, state,
                            setup_type, setup_id, state_version, started_at,
                            updated_at, expires_at, last_candle_close_time,
                            snapshot_id, reference_price, base_low, base_high,
                            breakout_level, invalidation_price, p_setup_success,
                            p_followthrough, p_fakeout, expected_r,
                            model_versions_json, reason_codes_json, metrics_json)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            market_env,
                            symbol,
                            alpha_symbol,
                            state.state.value,
                            state.setup_type,
                            state.setup_id,
                            0,
                            _iso(state.started_at),
                            _iso(state.updated_at),
                            _iso(state.expires_at),
                            _iso(state.last_candle_close_time),
                            state.snapshot_id,
                            state.reference_price,
                            state.base_low,
                            state.base_high,
                            state.breakout_level,
                            state.invalidation_price,
                            state.setup_probability,
                            state.followthrough_probability,
                            state.fakeout_probability,
                            state.expected_r,
                            json.dumps(
                                dict(state.model_versions),
                                ensure_ascii=False,
                            ),
                            json.dumps(list(state.reasons), ensure_ascii=False),
                            json.dumps(dict(state.metrics), ensure_ascii=False),
                        ),
                    )
                except sqlite3.IntegrityError:
                    conn.rollback()
                    return False
            conn.commit()
            return True
        finally:
            conn.close()

    def fetch_actionable_events(
        self,
        market_env: str,
        limit: int = 100,
        *,
        strategy_modes: tuple[str, ...] = (
            "testnet_live",
            "mainnet_canary",
            "mainnet_live",
        ),
    ) -> list[dict]:
        modes = tuple(
            str(mode).strip().lower()
            for mode in strategy_modes
            if str(mode).strip()
        )
        if not modes:
            return []
        placeholders = ",".join("?" for _ in modes)
        conn = get_conn()
        try:
            rows = conn.execute(
                f"""SELECT * FROM alpha_signal_events
                   WHERE market_env=?
                     AND strategy_mode IN ({placeholders})
                     AND action_type != 'NONE'
                   ORDER BY datetime(event_time), event_id
                   LIMIT ?""",
                (market_env, *modes, int(limit)),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def claim_event(
        self,
        account_id: int,
        event_id: str,
        action_type: ActionType,
    ) -> bool:
        conn = get_conn()
        try:
            cursor = conn.execute(
                """INSERT OR IGNORE INTO alpha_signal_consumptions
                   (account_id, event_id, action_type, status, updated_at)
                   VALUES (?, ?, ?, 'PENDING', ?)""",
                (
                    int(account_id),
                    event_id,
                    action_type.value,
                    _iso(datetime.now(timezone.utc)),
                ),
            )
            conn.commit()
            return cursor.rowcount == 1
        finally:
            conn.close()

    def fetch_account_events(
        self,
        *,
        account_id: int,
        market_env: str,
        strategy_modes: tuple[str, ...],
        limit: int = 100,
    ) -> list[dict]:
        modes = tuple(
            str(mode).strip().lower()
            for mode in strategy_modes
            if str(mode).strip()
        )
        if not modes:
            return []
        placeholders = ",".join("?" for _ in modes)
        conn = get_conn()
        try:
            rows = conn.execute(
                f"""SELECT e.*
                    FROM alpha_signal_events e
                    LEFT JOIN alpha_signal_consumptions c
                      ON c.account_id=? AND c.event_id=e.event_id
                     AND c.action_type=e.action_type
                    WHERE e.market_env=?
                      AND e.strategy_mode IN ({placeholders})
                      AND e.action_type != 'NONE'
                      AND c.event_id IS NULL
                      AND (
                          e.expires_at IS NULL
                          OR datetime(e.expires_at) > datetime('now')
                      )
                    ORDER BY datetime(e.event_time), e.event_id
                    LIMIT ?""",
                (int(account_id), market_env, *modes, int(limit)),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def update_consumption(
        self,
        *,
        account_id: int,
        event_id: str,
        action_type: ActionType | str,
        status: str,
        rejection_reason: str | None = None,
        client_order_id: str | None = None,
        position_id: str | None = None,
        quantity: float | None = None,
        order_id: str | None = None,
    ) -> None:
        action = (
            action_type.value
            if isinstance(action_type, ActionType)
            else str(action_type)
        )
        terminal = {
            "RISK_REJECTED",
            "FILLED",
            "FAILED",
            "EXPIRED",
            "SIGNAL_ONLY",
        }
        now = _iso(datetime.now(timezone.utc))
        conn = get_conn()
        try:
            conn.execute(
                """UPDATE alpha_signal_consumptions
                   SET status=?, rejection_reason=?, client_order_id=?,
                       position_id=?, quantity=?, order_id=?,
                       consumed_at=CASE WHEN ? THEN ? ELSE consumed_at END,
                       updated_at=?
                   WHERE account_id=? AND event_id=? AND action_type=?""",
                (
                    str(status),
                    rejection_reason,
                    client_order_id,
                    position_id,
                    quantity,
                    order_id,
                    int(str(status) in terminal),
                    now,
                    now,
                    int(account_id),
                    event_id,
                    action,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def recoverable_consumptions(self, account_id: int) -> list[dict]:
        conn = get_conn()
        try:
            rows = conn.execute(
                """SELECT c.*, e.market_env, e.strategy_mode, e.futures_symbol,
                          e.alpha_symbol, e.reference_price,
                          e.invalidation_price, e.max_position_factor,
                          e.expires_at,
                          e.reason_codes_json, e.ai_decision_json
                   FROM alpha_signal_consumptions c
                   JOIN alpha_signal_events e ON e.event_id=c.event_id
                   WHERE c.account_id=?
                     AND c.status IN (
                         'PENDING','PLANNED','SUBMITTED','PARTIALLY_FILLED'
                     )
                   ORDER BY datetime(c.updated_at), c.event_id""",
                (int(account_id),),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def release_unsubmitted_consumption(
        self,
        *,
        account_id: int,
        event_id: str,
        action_type: ActionType | str,
    ) -> bool:
        action = (
            action_type.value
            if isinstance(action_type, ActionType)
            else str(action_type)
        )
        conn = get_conn()
        try:
            cursor = conn.execute(
                """DELETE FROM alpha_signal_consumptions
                   WHERE account_id=? AND event_id=? AND action_type=?
                     AND status IN ('PENDING','PLANNED')""",
                (int(account_id), event_id, action),
            )
            conn.commit()
            return cursor.rowcount == 1
        finally:
            conn.close()

    def heartbeat(
        self,
        *,
        market_env: str,
        strategy_mode: str,
        worker_id: str,
        last_candle_close_time: datetime | None,
        processed_count: int,
        transition_count: int,
        skipped_count: int,
        error_count: int,
        duplicate_count: int,
        last_error: str | None,
        metrics: dict | None = None,
    ) -> None:
        now = _iso(datetime.now(timezone.utc))
        conn = get_conn()
        try:
            conn.execute(
                """INSERT INTO alpha_strategy_runtime
                   (market_env, strategy_mode, worker_id, heartbeat_at,
                    last_candle_close_time, processed_count, transition_count,
                    skipped_count, error_count, duplicate_count, last_error,
                    metrics_json, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(market_env) DO UPDATE SET
                     strategy_mode=excluded.strategy_mode,
                     worker_id=excluded.worker_id,
                     heartbeat_at=excluded.heartbeat_at,
                     last_candle_close_time=excluded.last_candle_close_time,
                     processed_count=excluded.processed_count,
                     transition_count=excluded.transition_count,
                     skipped_count=excluded.skipped_count,
                     error_count=excluded.error_count,
                     duplicate_count=excluded.duplicate_count,
                     last_error=excluded.last_error,
                     metrics_json=excluded.metrics_json,
                     updated_at=excluded.updated_at""",
                (
                    market_env,
                    strategy_mode,
                    worker_id,
                    now,
                    _iso(last_candle_close_time),
                    int(processed_count),
                    int(transition_count),
                    int(skipped_count),
                    int(error_count),
                    int(duplicate_count),
                    last_error,
                    json.dumps(metrics or {}, ensure_ascii=False),
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def strategy_status(self, market_env: str | None = None) -> dict:
        conn = get_conn()
        try:
            where = "WHERE market_env=?" if market_env else ""
            params = (market_env,) if market_env else ()
            runtime = [
                dict(row)
                for row in conn.execute(
                    f"""SELECT * FROM alpha_strategy_runtime {where}
                        ORDER BY market_env""",
                    params,
                ).fetchall()
            ]
            states = [
                dict(row)
                for row in conn.execute(
                    f"""SELECT market_env, state, COUNT(*) count
                        FROM alpha_signal_states {where}
                        GROUP BY market_env, state
                        ORDER BY market_env, state""",
                    params,
                ).fetchall()
            ]
            events = [
                dict(row)
                for row in conn.execute(
                    f"""SELECT market_env, strategy_mode, action_type,
                               COUNT(*) count, MAX(event_time) latest_event_time
                        FROM alpha_signal_events {where}
                        GROUP BY market_env, strategy_mode, action_type
                        ORDER BY market_env, strategy_mode, action_type""",
                    params,
                ).fetchall()
            ]
            consumptions = [
                dict(row)
                for row in conn.execute(
                    """SELECT status, COUNT(*) count
                       FROM alpha_signal_consumptions
                       GROUP BY status ORDER BY status"""
                ).fetchall()
            ]
            for row in runtime:
                row["metrics"] = json.loads(row.pop("metrics_json") or "{}")
            return {
                "runtime": runtime,
                "states": states,
                "events": events,
                "consumptions": consumptions,
            }
        finally:
            conn.close()
