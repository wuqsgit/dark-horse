"""Account-scoped consumer for Alpha Strategy V2 signal events."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from alpha_engine.strategy.models import ActionType
from alpha_engine.strategy.repository import AlphaStrategyRepository
from shared.db import (
    get_conn,
    get_position_history,
    is_market_entry_ready,
)
from trader.portfolio_risk import (
    check_category_position_limit,
    check_consecutive_losses,
    check_daily_loss_limit,
    check_portfolio_risk,
    symbol_risk_category,
)
from trader.risk import calc_tp_levels, calculate_position


LIVE_MODES = {"testnet_live", "mainnet_canary", "mainnet_live"}


def _json(value, default):
    if isinstance(value, type(default)):
        return value
    try:
        return json.loads(value or "")
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _client_order_id(account_id: int, event_id: str, action_type: str) -> str:
    code = {
        "PROBE_LONG": "P",
        "CONFIRM_LONG": "C",
        "RETEST_ADD": "R",
        "INVALIDATE_PROBE": "X",
    }.get(action_type, "A")
    digest = hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:16]
    return f"DH-A2-{int(account_id)}-{digest}-{code}"


class AlphaSignalConsumer:
    def __init__(
        self,
        exchange,
        *,
        config: dict,
        repository: AlphaStrategyRepository | None = None,
    ):
        self.exchange = exchange
        self.config = config
        self.strategy = config.get("alpha_strategy_v2") or {}
        self.repository = repository or AlphaStrategyRepository()

    @staticmethod
    def account_market_env(account: dict) -> str:
        return (
            "testnet"
            if str(account.get("environment") or "").lower() == "testnet"
            else "mainnet"
        )

    def _event_modes(self) -> tuple[str, ...]:
        mode = str(self.strategy.get("mode") or "shadow").lower()
        return (mode,) if mode in LIVE_MODES | {"signal"} else ()

    def _account_loss_state(self, account_id: int) -> tuple[float, int]:
        conn = get_conn()
        try:
            row = conn.execute(
                """SELECT COALESCE(SUM(pnl), 0) pnl
                   FROM trades
                   WHERE account_id=? AND date(created_at)=date('now')""",
                (int(account_id),),
            ).fetchone()
            recent = conn.execute(
                """SELECT pnl FROM trades WHERE account_id=?
                   ORDER BY datetime(created_at) DESC, id DESC LIMIT 20""",
                (int(account_id),),
            ).fetchall()
        finally:
            conn.close()
        consecutive = 0
        for item in recent:
            if float(item["pnl"] or 0) >= 0:
                break
            consecutive += 1
        return float(row["pnl"] or 0), consecutive

    def _reject(
        self,
        *,
        account_id: int,
        event: dict,
        reason: str,
        status: str = "RISK_REJECTED",
    ) -> None:
        self.repository.update_consumption(
            account_id=account_id,
            event_id=event["event_id"],
            action_type=event["action_type"],
            status=status,
            rejection_reason=reason,
        )

    def build_actions(
        self,
        *,
        account: dict,
        positions: list[dict],
        balance: float,
        engine,
        run_id: str,
        planned_actions: list[dict] | None = None,
    ) -> list[dict]:
        if not self.strategy.get("enabled", False):
            return []
        modes = self._event_modes()
        if not modes:
            return []
        if (
            any(mode in LIVE_MODES for mode in modes)
            and not bool(account.get("alpha_trading_enabled"))
        ):
            return []
        account_id = int(account["id"])
        market_env = self.account_market_env(account)
        configured_env = str(self.strategy.get("market_env") or "").lower()
        if configured_env and configured_env != market_env:
            return []
        events = self.repository.fetch_account_events(
            account_id=account_id,
            market_env=market_env,
            strategy_modes=modes,
        )
        if not events:
            return []

        actions = []
        preplanned = list(planned_actions or [])
        position_map = {
            str(position.get("symbol") or "").upper(): position
            for position in positions
        }
        trading_symbols = engine._get_trading_symbols()
        daily_pnl, consecutive_losses = self._account_loss_state(account_id)

        for event in events:
            action_type = str(event["action_type"])
            try:
                action_enum = ActionType(action_type)
            except ValueError:
                continue
            if not self.repository.claim_event(
                account_id,
                event["event_id"],
                action_enum,
            ):
                continue
            if event["strategy_mode"] == "signal":
                self._reject(
                    account_id=account_id,
                    event=event,
                    reason="signal_mode_record_only",
                    status="SIGNAL_ONLY",
                )
                continue
            symbol = str(event["futures_symbol"]).upper()
            if any(
                action.get("symbol") == symbol
                for action in [*preplanned, *actions]
            ):
                self._reject(
                    account_id=account_id,
                    event=event,
                    reason="conflicting_planned_action",
                )
                continue
            position = position_map.get(symbol)
            if symbol not in trading_symbols:
                self._reject(
                    account_id=account_id,
                    event=event,
                    reason="contract_not_tradable",
                )
                continue
            ready, data_error = is_market_entry_ready(
                symbol,
                "alpha",
                event.get("alpha_symbol"),
            )
            if not ready:
                self._reject(
                    account_id=account_id,
                    event=event,
                    reason=f"market_data_not_ready:{data_error}",
                )
                continue

            if action_type == "INVALIDATE_PROBE":
                if not position:
                    self._reject(
                        account_id=account_id,
                        event=event,
                        reason="no_position_to_invalidate",
                        status="EXPIRED",
                    )
                    continue
                hist = get_position_history(symbol) or {}
                if (hist.get("strategy_source") or "") != "alpha":
                    self._reject(
                        account_id=account_id,
                        event=event,
                        reason="position_not_owned_by_alpha",
                    )
                    continue
                action = {
                    "action": "close",
                    "symbol": symbol,
                    "side": "SELL" if position.get("side") == "LONG" else "BUY",
                    "position_side": position.get("side"),
                    "reason": "alpha_v2_structure_invalidated",
                    "strategy_source": "alpha",
                    "signal_source": "alpha_strategy_v2",
                    "alpha_signal_event_id": event["event_id"],
                    "alpha_action_type": action_type,
                    "run_id": run_id,
                    "close_price": float(position.get("mark_price") or 0),
                    "client_order_id": _client_order_id(
                        account_id,
                        event["event_id"],
                        action_type,
                    ),
                }
                actions.append(action)
                self._mark_planned(account_id, event, action)
                continue

            if action_type == "PROBE_LONG" and position:
                self._reject(
                    account_id=account_id,
                    event=event,
                    reason="probe_requires_no_existing_position",
                )
                continue
            if action_type == "RETEST_ADD" and not position:
                self._reject(
                    account_id=account_id,
                    event=event,
                    reason="retest_requires_existing_position",
                )
                continue
            if position:
                hist = get_position_history(symbol) or {}
                if (
                    position.get("side") != "LONG"
                    or (hist.get("strategy_source") or "") != "alpha"
                ):
                    self._reject(
                        account_id=account_id,
                        event=event,
                        reason="incompatible_existing_position",
                    )
                    continue
            else:
                max_positions = int(account.get("max_positions") or 5)
                if len(position_map) + len(
                    [
                        action
                        for action in [*preplanned, *actions]
                        if action["action"] == "open"
                    ]
                ) >= max_positions:
                    self._reject(
                        account_id=account_id,
                        event=event,
                        reason="account_max_positions",
                    )
                    continue
                alpha_position_count = sum(
                    1
                    for item in positions
                    if (
                        (get_position_history(item.get("symbol")) or {}).get(
                            "strategy_source"
                        )
                        == "alpha"
                    )
                )
                if alpha_position_count >= int(
                    self.strategy.get("max_alpha_positions") or 2
                ):
                    self._reject(
                        account_id=account_id,
                        event=event,
                        reason="alpha_max_positions",
                    )
                    continue
                category_ok, category_reason = check_category_position_limit(
                    positions,
                    symbol,
                    [*preplanned, *actions],
                )
                if not category_ok:
                    self._reject(
                        account_id=account_id,
                        event=event,
                        reason=category_reason,
                    )
                    continue

            daily_ok, daily_reason = check_daily_loss_limit(daily_pnl, balance)
            loss_ok, loss_reason = check_consecutive_losses(consecutive_losses)
            if not daily_ok or not loss_ok:
                self._reject(
                    account_id=account_id,
                    event=event,
                    reason=daily_reason if not daily_ok else loss_reason,
                )
                continue
            try:
                open_orders = self.exchange.get_open_orders(symbol)
            except Exception:
                open_orders = []
            conflicting = [
                order for order in open_orders
                if str(order.get("reduceOnly", "")).lower() not in {"true", "1"}
            ]
            if conflicting:
                self._reject(
                    account_id=account_id,
                    event=event,
                    reason="conflicting_open_order",
                )
                continue
            try:
                ob_ok, ob_reason, ob_info = engine._check_live_orderbook(
                    symbol,
                    "LONG",
                    {"status": "probe", "template": "alpha_strategy_v2"},
                )
            except Exception as exc:
                ob_ok, ob_reason, ob_info = False, str(exc), {}
            if not ob_ok:
                self._reject(
                    account_id=account_id,
                    event=event,
                    reason=f"orderbook:{ob_reason}",
                )
                continue

            price = float(self.exchange.get_mark_price(symbol) or 0)
            invalidation = float(event.get("invalidation_price") or 0)
            if price <= 0 or invalidation <= 0 or invalidation >= price:
                self._reject(
                    account_id=account_id,
                    event=event,
                    reason="invalid_reference_or_invalidation_price",
                )
                continue
            stage_cap = {
                "PROBE_LONG": float(self.strategy.get("probe_stage_cap") or 0.30),
                "CONFIRM_LONG": float(
                    self.strategy.get("confirmed_stage_cap") or 0.70
                ),
                "RETEST_ADD": float(
                    self.strategy.get("retest_stage_cap") or 1.00
                ),
            }[action_type]
            ai_factor = float(event.get("max_position_factor") or 0)
            final_factor = min(stage_cap, ai_factor)
            if event["strategy_mode"] == "mainnet_canary":
                final_factor *= float(
                    self.strategy.get("mainnet_canary_factor") or 0.25
                )
            if final_factor <= 0:
                self._reject(
                    account_id=account_id,
                    event=event,
                    reason="zero_bounded_position_factor",
                )
                continue
            entry_mode = "probe" if action_type == "PROBE_LONG" else "strong"
            pos_info = calculate_position(
                self.exchange,
                symbol,
                price,
                balance,
                score=80,
                category=symbol_risk_category(symbol),
                entry_mode=entry_mode,
                size_multiplier=final_factor,
            )
            risk_budget = balance * float(
                account.get("risk_per_trade_pct")
                or self.config.get("risk_per_trade_pct")
                or 0.015
            )
            structural_risk = price - invalidation
            risk_qty = risk_budget / structural_risk if structural_risk > 0 else 0
            target_qty = min(float(pos_info["quantity"]), risk_qty)
            existing_qty = float(position.get("quantity") or 0) if position else 0.0
            quantity = (
                max(0.0, target_qty - existing_qty)
                if position
                else target_qty
            )
            quantity = float(self.exchange.adjust_quantity(symbol, quantity))
            info = self.exchange.get_symbol_info(symbol)
            if (
                quantity < float(info.get("min_qty") or 0)
                or quantity * price < float(info.get("min_notional") or 0)
            ):
                self._reject(
                    account_id=account_id,
                    event=event,
                    reason="quantity_below_exchange_minimum",
                )
                continue
            invested = quantity * price / max(float(pos_info["leverage"]), 1)
            current_margin = sum(
                float(
                    item.get("margin")
                    or item.get("initial_margin")
                    or item.get("position_initial_margin")
                    or 0
                )
                for item in positions
            )
            max_usage = float(
                account.get("max_capital_usage_pct")
                or self.config.get("max_total_exposure_pct")
                or 0.80
            )
            if balance <= 0 or current_margin + invested > balance * max_usage:
                self._reject(
                    account_id=account_id,
                    event=event,
                    reason="account_capital_usage_limit",
                )
                continue
            risk_positions = [
                {
                    **item,
                    "invested": float(
                        item.get("margin")
                        or item.get("initial_margin")
                        or 0
                    ),
                    "category": symbol_risk_category(item.get("symbol")),
                }
                for item in positions
            ]
            portfolio_ok, portfolio_reason = check_portfolio_risk(
                risk_positions,
                balance,
                symbol,
                symbol_risk_category(symbol),
                invested,
            )
            if not portfolio_ok:
                self._reject(
                    account_id=account_id,
                    event=event,
                    reason=portfolio_reason,
                )
                continue

            stop_distance = min(
                price - invalidation,
                float(pos_info.get("stop_loss") or structural_risk),
            )
            stop_price = price - stop_distance
            tp = calc_tp_levels(price, "LONG", stop_distance / price)
            client_id = _client_order_id(
                account_id,
                event["event_id"],
                action_type,
            )
            common = {
                "symbol": symbol,
                "side": "BUY",
                "position_side": "LONG",
                "quantity": quantity,
                "entry_price": price,
                "leverage": int(pos_info["leverage"]),
                "strategy_source": "alpha",
                "signal_source": "alpha_strategy_v2",
                "alpha_symbol": event.get("alpha_symbol"),
                "alpha_profile": "ai_state_machine",
                "alpha_entry_level": action_type.lower(),
                "alpha_score": round(
                    float(
                        _json(event.get("ai_decision_json"), {}).get(
                            "p_followthrough"
                        )
                        or _json(event.get("ai_decision_json"), {}).get(
                            "p_setup_success"
                        )
                        or 0
                    )
                    * 100,
                    2,
                ),
                "alpha_suggested_position_pct": final_factor,
                "alpha_signal_event_id": event["event_id"],
                "alpha_setup_id": event.get("setup_id"),
                "alpha_action_type": action_type,
                "ai_model_versions": _json(
                    event.get("ai_decision_json"),
                    {},
                ).get("model_versions") or {},
                "client_order_id": client_id,
                "skip_entry_quality_gate": True,
                "run_id": run_id,
                "reason": f"alpha_strategy_v2:{action_type.lower()}",
                "category": symbol_risk_category(symbol),
            }
            if position:
                hist = get_position_history(symbol) or {}
                action = {
                    **common,
                    "action": "roll_add",
                    "position_id": hist.get("position_id"),
                    "roll_layer": int(hist.get("roll_layer") or 0) + 1,
                    "risk_before": {
                        "quantity": existing_qty,
                        "entry_price": position.get("entry_price"),
                    },
                    "risk_after": {
                        "target_quantity": existing_qty + quantity,
                        "invalidation_price": invalidation,
                        "stage_cap": stage_cap,
                    },
                }
            else:
                action = {
                    **common,
                    "action": "open",
                    "stop_loss": stop_price,
                    "tp1_price": tp["tp1_price"],
                    "tp2_price": tp["tp2_price"],
                    "tp1_qty_pct": tp["tp1_qty_pct"],
                    "tp2_qty_pct": tp["tp2_qty_pct"],
                    "atr_value": pos_info["atr_value"],
                    "stop_model": "alpha_v2_structure_bounded",
                    "stop_pct": stop_distance / price,
                    "trailing_atr_multiplier": pos_info[
                        "trailing_atr_multiplier"
                    ],
                    "invested": invested,
                }
            actions.append(action)
            self._mark_planned(account_id, event, action)
        return actions

    def _mark_planned(self, account_id: int, event: dict, action: dict) -> None:
        self.repository.update_consumption(
            account_id=account_id,
            event_id=event["event_id"],
            action_type=event["action_type"],
            status="PLANNED",
            client_order_id=action.get("client_order_id"),
            position_id=action.get("position_id"),
            quantity=action.get("quantity"),
        )

    def mark_submitted(self, account_id: int, actions: list[dict]) -> None:
        for action in actions:
            event_id = action.get("alpha_signal_event_id")
            if not event_id:
                continue
            self.repository.update_consumption(
                account_id=account_id,
                event_id=event_id,
                action_type=action["alpha_action_type"],
                status="SUBMITTED",
                client_order_id=action.get("client_order_id"),
                position_id=action.get("position_id"),
                quantity=action.get("quantity"),
            )

    def finalize(self, account_id: int, results: list[dict]) -> None:
        for result in results:
            event_id = result.get("alpha_signal_event_id")
            if not event_id:
                continue
            status = "FILLED" if result.get("status") == "ok" else "FAILED"
            self.repository.update_consumption(
                account_id=account_id,
                event_id=event_id,
                action_type=result["alpha_action_type"],
                status=status,
                rejection_reason=result.get("error"),
                client_order_id=result.get("client_order_id"),
                position_id=result.get("position_id"),
                quantity=result.get("quantity"),
                order_id=result.get("exchange_order_id"),
            )

    def recover(self, account: dict, positions: list[dict]) -> dict:
        account_id = int(account["id"])
        recovered = {
            "filled": 0,
            "failed": 0,
            "pending": 0,
            "retryable": 0,
            "expired": 0,
        }
        by_symbol = {
            str(position.get("symbol") or "").upper(): position
            for position in positions
        }
        for row in self.repository.recoverable_consumptions(account_id):
            symbol = str(row["futures_symbol"]).upper()
            position = by_symbol.get(symbol)
            if row["action_type"] in {
                "PROBE_LONG",
                "CONFIRM_LONG",
                "RETEST_ADD",
            } and position:
                self.repository.update_consumption(
                    account_id=account_id,
                    event_id=row["event_id"],
                    action_type=row["action_type"],
                    status="FILLED",
                    client_order_id=row.get("client_order_id"),
                    position_id=row.get("position_id"),
                    quantity=row.get("quantity"),
                )
                recovered["filled"] += 1
                continue
            order = None
            if row.get("client_order_id"):
                try:
                    order = self.exchange.get_order_by_client_id(
                        symbol,
                        row["client_order_id"],
                    )
                except Exception:
                    order = None
            order_status = str((order or {}).get("status") or "").upper()
            if order_status == "FILLED":
                self.repository.update_consumption(
                    account_id=account_id,
                    event_id=row["event_id"],
                    action_type=row["action_type"],
                    status="FILLED",
                    client_order_id=row.get("client_order_id"),
                    order_id=(order or {}).get("orderId"),
                )
                recovered["filled"] += 1
            elif order_status in {"CANCELED", "EXPIRED", "REJECTED"}:
                self.repository.update_consumption(
                    account_id=account_id,
                    event_id=row["event_id"],
                    action_type=row["action_type"],
                    status="FAILED",
                    rejection_reason=f"exchange_order_{order_status.lower()}",
                    client_order_id=row.get("client_order_id"),
                    order_id=(order or {}).get("orderId"),
                )
                recovered["failed"] += 1
            elif (
                not order
                and row["status"] in {"PENDING", "PLANNED"}
                and row.get("expires_at")
            ):
                expires_at = datetime.fromisoformat(
                    str(row["expires_at"]).replace("Z", "+00:00")
                )
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if expires_at <= datetime.now(timezone.utc):
                    self.repository.update_consumption(
                        account_id=account_id,
                        event_id=row["event_id"],
                        action_type=row["action_type"],
                        status="EXPIRED",
                        rejection_reason="event_expired_before_submission",
                        client_order_id=row.get("client_order_id"),
                    )
                    recovered["expired"] += 1
                elif self.repository.release_unsubmitted_consumption(
                    account_id=account_id,
                    event_id=row["event_id"],
                    action_type=row["action_type"],
                ):
                    recovered["retryable"] += 1
                else:
                    recovered["pending"] += 1
            elif not order and row["status"] in {"PENDING", "PLANNED"}:
                if self.repository.release_unsubmitted_consumption(
                    account_id=account_id,
                    event_id=row["event_id"],
                    action_type=row["action_type"],
                ):
                    recovered["retryable"] += 1
                else:
                    recovered["pending"] += 1
            else:
                recovered["pending"] += 1
        return recovered
