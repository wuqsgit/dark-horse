"""Closed-candle Alpha Strategy V2 worker.

The worker owns market-state transitions only. It never receives exchange
credentials and never submits an order.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Callable

from alpha_engine.strategy.ai_client import AlphaStrategyAIClient
from alpha_engine.strategy.feature_builder import (
    AlphaFeatureSnapshot,
    build_alpha_feature_snapshot,
)
from alpha_engine.strategy.models import StrategyObservation, TransitionResult
from alpha_engine.strategy.models import ActionType, AlphaSignalState
from alpha_engine.strategy.projection import build_strategy_projection
from alpha_engine.strategy.repository import AlphaStrategyRepository
from alpha_engine.strategy.setup_rules import detect_setup
from alpha_engine.strategy.state_machine import (
    AlphaStrategyStateMachine,
    StateMachineConfig,
)
from alpha_engine.strategy.trigger_rules import evaluate_trigger
from shared.db import (
    fetch_active_alpha_symbols,
    fetch_alpha_candles,
    fetch_alpha_orderbook_depth,
    fetch_futures,
    fetch_futures_candles,
    fetch_latest_alpha_scan,
    fetch_latest_alpha_square_sentiment,
)


logger = logging.getLogger("alpha_strategy_v2")
ALLOWED_MODES = {
    "off",
    "shadow",
    "signal",
    "testnet_live",
    "mainnet_canary",
    "mainnet_live",
}


@dataclass(frozen=True)
class WorkerResult:
    applied: bool
    reason: str
    transition: TransitionResult | None = None
    event_id: str | None = None


class AlphaStrategyWorker:
    def __init__(
        self,
        *,
        ai_evaluate: Callable[[dict], dict] | None = None,
        repository: AlphaStrategyRepository | None = None,
        machine: AlphaStrategyStateMachine | None = None,
        mode: str = "shadow",
        market_env: str = "mainnet",
        testnet_live_rule_fallback: bool = False,
    ):
        self.repository = repository or AlphaStrategyRepository()
        self.machine = machine or AlphaStrategyStateMachine()
        self.mode = str(mode or "shadow").lower()
        self.market_env = str(market_env or "mainnet").lower()
        self.testnet_live_rule_fallback = bool(testnet_live_rule_fallback)
        if self.mode not in ALLOWED_MODES:
            raise ValueError(f"unsupported Alpha Strategy mode: {self.mode}")
        if self.market_env != "mainnet":
            raise ValueError(
                "Alpha Strategy market_env must be mainnet"
            )
        self.ai_evaluate = ai_evaluate or AlphaStrategyAIClient().evaluate
        self.worker_id = "alpha-v2-" + uuid.uuid4().hex[:12]

    def process_snapshot(self, snapshot: AlphaFeatureSnapshot) -> WorkerResult:
        if snapshot.market_env != self.market_env:
            return WorkerResult(False, "market_environment_mismatch")
        current = self.repository.get_state(
            snapshot.market_env,
            snapshot.futures_symbol,
        )
        if (
            current
            and current.last_candle_close_time
            and snapshot.candle_close_time <= current.last_candle_close_time
        ):
            return WorkerResult(False, "duplicate_closed_candle")

        setup = detect_setup(snapshot.features)
        setup_name = setup.setup_type or (current.setup_type if current else None)
        enriched_features = dict(snapshot.features)
        enriched_features["setup_type_code"] = {
            "accumulation": 1.0,
            "continuation": 2.0,
            "reclaim": 3.0,
            "sentiment_reversal": 4.0,
        }.get(str(setup_name or "").lower(), 0.0)
        enriched_quality = dict(snapshot.quality)
        missing = [
            name
            for name, value in enriched_features.items()
            if value is None
        ]
        enriched_quality["missing_features"] = missing
        enriched_quality["present_features"] = [
            name
            for name, value in enriched_features.items()
            if value is not None
        ]
        enriched_quality["coverage"] = round(
            len(enriched_quality["present_features"])
            / max(1, len(enriched_features)),
            4,
        )
        snapshot = replace(
            snapshot,
            features=enriched_features,
            quality=enriched_quality,
        )
        self.repository.save_feature_snapshot(snapshot)
        trigger = evaluate_trigger(
            snapshot.features,
            current,
            setup_type=setup_name,
        )
        stage = self._stage(current)
        payload = {
            "request_id": (
                f"{snapshot.futures_symbol}:"
                f"{snapshot.candle_close_time.isoformat()}:"
                f"{stage}:v{snapshot.feature_schema_version}"
            ),
            "market_env": snapshot.market_env,
            "alpha_symbol": snapshot.alpha_symbol,
            "futures_symbol": snapshot.futures_symbol,
            "stage": stage,
            "setup_type": setup_name,
            "candle_close_time": snapshot.candle_close_time.isoformat().replace(
                "+00:00", "Z"
            ),
            "feature_schema_version": snapshot.feature_schema_version,
            "feature_quality": dict(snapshot.quality),
            "features": dict(snapshot.features),
        }
        try:
            prediction = dict(self.ai_evaluate(payload) or {})
        except Exception as exc:
            logger.warning(
                "Alpha Strategy AI unavailable for %s: %s",
                snapshot.futures_symbol,
                exc,
            )
            return WorkerResult(False, f"ai_unavailable:{exc}")

        prediction = self._rule_prediction_if_collecting(
            prediction,
            setup_score=setup.score,
            setup_type=setup_name,
            trigger=trigger,
            stage=stage,
        )
        if prediction.get("p_setup_success") is None:
            observation = self._collecting_observation(
                snapshot=snapshot,
                current=current,
                setup=setup,
                trigger=trigger,
                prediction=prediction,
            )
            self.repository.save_observation(
                market_env=snapshot.market_env,
                futures_symbol=snapshot.futures_symbol,
                alpha_symbol=snapshot.alpha_symbol,
                transition=observation,
            )
            return WorkerResult(
                False,
                "ai_prediction_not_ready",
                observation,
            )

        features = snapshot.features
        projection = build_strategy_projection(features, setup_type=setup_name)
        base_low = features.get("base_low_2h")
        base_high = features.get("base_high_2h")
        breakout_level = features.get("breakout_level") or base_high
        invalidation_price = (
            float(base_low) * 0.99 if base_low is not None else None
        )
        observation = StrategyObservation(
            snapshot_id=snapshot.snapshot_id,
            candle_close_time=snapshot.candle_close_time,
            setup_type=setup.setup_type or (current.setup_type if current else None),
            setup_detected=setup.detected,
            setup_probability=float(
                prediction.get("p_setup_success") or 0.0
            ),
            followthrough_probability=float(
                prediction.get("p_followthrough") or 0.0
            ),
            fakeout_probability=float(
                prediction.get("p_fakeout")
                if prediction.get("p_fakeout") is not None
                else 1.0
            ),
            trigger_detected=trigger.trigger_detected,
            overheated=trigger.overheated,
            acceptance_confirmed=trigger.acceptance_confirmed,
            retest_confirmed=trigger.retest_confirmed,
            invalidated=trigger.invalidated,
            data_ready=snapshot.quality.get("status") == "ready",
            reference_price=features.get("current_price"),
            base_low=base_low,
            base_high=base_high,
            breakout_level=breakout_level,
            invalidation_price=(
                current.invalidation_price
                if current and current.invalidation_price is not None
                else invalidation_price
            ),
            expected_r=prediction.get("expected_r"),
            max_position_factor=float(
                prediction.get("max_position_factor") or 0.0
            ),
            reasons=tuple(
                [
                    *setup.reasons,
                    *trigger.reasons,
                    *(prediction.get("reasons") or []),
                ]
            ),
            model_versions=prediction.get("model_versions") or {},
            metrics={"projection": projection},
        )
        transition = self.machine.transition(
            current,
            observation,
            now=snapshot.candle_close_time,
        )
        if not transition.changed:
            self.repository.save_observation(
                market_env=snapshot.market_env,
                futures_symbol=snapshot.futures_symbol,
                alpha_symbol=snapshot.alpha_symbol,
                transition=transition,
            )
            return WorkerResult(False, "state_unchanged", transition)
        applied = self.repository.apply_transition(
            market_env=snapshot.market_env,
            futures_symbol=snapshot.futures_symbol,
            alpha_symbol=snapshot.alpha_symbol,
            transition=transition,
            strategy_mode=self.mode,
        )
        return WorkerResult(
            applied.applied,
            "transition_applied" if applied.applied else "transition_conflict",
            transition,
            applied.event_id,
        )

    def run_once(self, *, limit: int = 200, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        symbols = [dict(row) for row in fetch_active_alpha_symbols(limit=limit)]
        _scan, score_rows = fetch_latest_alpha_scan()
        alpha_scores = {
            str(row["base_asset"] or "").upper(): float(
                row["alpha_score"] or 0
            )
            for row in score_rows
        }
        futures_symbols = sorted(
            {
                str(row.get("futures_symbol") or "").upper()
                for row in symbols
                if row.get("futures_symbol")
            }
        )
        if not futures_symbols:
            result = {
                "processed": 0,
                "applied": 0,
                "skipped": 0,
                "duplicates": 0,
                "errors": [],
                "last_candle_close_time": None,
            }
            self.repository.heartbeat(
                market_env=self.market_env,
                strategy_mode=self.mode,
                worker_id=self.worker_id,
                last_candle_close_time=None,
                processed_count=0,
                transition_count=0,
                skipped_count=0,
                error_count=0,
                duplicate_count=0,
                last_error=None,
                metrics={"universe_size": len(symbols)},
            )
            return result
        market_symbols = sorted(set(futures_symbols) | {"BTCUSDT"})
        rows_15m = fetch_futures_candles(
            "futures_candles_15m",
            market_symbols,
            hours=72,
            source_env=self.market_env,
            closed_only=True,
        )
        rows_1h = fetch_futures_candles(
            "futures_candles_1h",
            futures_symbols,
            hours=120,
            source_env=self.market_env,
            closed_only=True,
        )
        alpha_symbols = sorted(
            {
                str(row.get("alpha_symbol") or "")
                for row in symbols
                if row.get("alpha_symbol")
            }
        )
        spot_rows_15m = fetch_alpha_candles(
            "alpha_candles_15m",
            alpha_symbols,
            hours=72,
            source_env="mainnet",
            closed_only=True,
        )
        futures_snapshots = fetch_futures(
            futures_symbols,
            hours=72,
            source_env=self.market_env,
        )
        by_15m = {}
        by_1h = {}
        by_spot_15m = {}
        by_futures_snapshots = {}
        for row in rows_15m:
            by_15m.setdefault(row["symbol"], []).append(dict(row))
        for row in rows_1h:
            by_1h.setdefault(row["symbol"], []).append(dict(row))
        for row in spot_rows_15m:
            by_spot_15m.setdefault(row["alpha_symbol"], []).append(dict(row))
        for row in futures_snapshots:
            by_futures_snapshots.setdefault(row["symbol"], []).append(dict(row))

        context = self._market_context(by_15m)

        result = {
            "processed": 0,
            "applied": 0,
            "skipped": 0,
            "duplicates": 0,
            "errors": [],
            "last_candle_close_time": None,
            "reason_counts": {},
        }
        for symbol_row in symbols:
            futures_symbol = str(symbol_row.get("futures_symbol") or "").upper()
            if not by_15m.get(futures_symbol) or not by_1h.get(futures_symbol):
                result["skipped"] += 1
                continue
            try:
                symbol_return_1h = self._return_change(
                    by_15m[futures_symbol],
                    4,
                )
                symbol_context = {
                    **context,
                    "category_relative_strength": (
                        symbol_return_1h
                        - float(context["alpha_universe_median_return"])
                        if symbol_return_1h is not None
                        and context.get("alpha_universe_median_return") is not None
                        else 0.0
                    ),
                }
                snapshot = build_alpha_feature_snapshot(
                    alpha_symbol=symbol_row.get("alpha_symbol"),
                    futures_symbol=futures_symbol,
                    market_env=self.market_env,
                    cutoff_time=now,
                    candles_15m=by_15m[futures_symbol],
                    candles_1h=by_1h[futures_symbol],
                    spot_candles_15m=by_spot_15m.get(
                        symbol_row.get("alpha_symbol"),
                        [],
                    ),
                    futures_snapshots=by_futures_snapshots.get(
                        futures_symbol,
                        [],
                    ),
                    orderbook_snapshots=[
                        dict(row)
                        for row in fetch_alpha_orderbook_depth(
                            symbol_row.get("alpha_symbol"),
                            hours=6,
                        )
                    ]
                    if symbol_row.get("alpha_symbol")
                    else [],
                    market_context=symbol_context,
                    listing_time=symbol_row.get("first_seen"),
                    square_sentiment=fetch_latest_alpha_square_sentiment(
                        symbol_row.get("base_asset"),
                    ),
                    alpha_discovery_score=alpha_scores.get(
                        str(symbol_row.get("base_asset") or "").upper()
                    ),
                )
                processed = self.process_snapshot(snapshot)
                result["processed"] += 1
                result["applied"] += int(processed.applied)
                result["skipped"] += int(not processed.applied)
                result["duplicates"] += int(
                    processed.reason == "duplicate_closed_candle"
                )
                result["reason_counts"][processed.reason] = (
                    result["reason_counts"].get(processed.reason, 0) + 1
                )
                result["last_candle_close_time"] = max(
                    filter(
                        None,
                        (
                            result["last_candle_close_time"],
                            snapshot.candle_close_time,
                        ),
                    )
                )
            except Exception as exc:
                result["errors"].append(
                    {"symbol": futures_symbol, "error": str(exc)}
                )
                logger.exception(
                    "Alpha Strategy V2 failed for %s",
                    futures_symbol,
                )
        self.repository.heartbeat(
            market_env=self.market_env,
            strategy_mode=self.mode,
            worker_id=self.worker_id,
            last_candle_close_time=result["last_candle_close_time"],
            processed_count=result["processed"],
            transition_count=result["applied"],
            skipped_count=result["skipped"],
            error_count=len(result["errors"]),
            duplicate_count=result["duplicates"],
            last_error=(
                result["errors"][-1]["error"]
                if result["errors"]
                else None
            ),
            metrics={
                "universe_size": len(symbols),
                "reason_counts": result["reason_counts"],
                "ai_failure_count": sum(
                    count
                    for reason, count in result["reason_counts"].items()
                    if reason.startswith("ai_unavailable")
                ),
                "ai_not_ready_count": result["reason_counts"].get(
                    "ai_prediction_not_ready",
                    0,
                ),
            },
        )
        if result["last_candle_close_time"] is not None:
            result["last_candle_close_time"] = (
                result["last_candle_close_time"]
                .astimezone(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
        return result

    @staticmethod
    def _return_change(rows: list[dict], bars: int) -> float | None:
        if len(rows) <= bars:
            return None
        start = float(rows[-bars - 1].get("close") or 0)
        end = float(rows[-1].get("close") or 0)
        return (end / start - 1) * 100 if start > 0 and end > 0 else None

    @classmethod
    def _market_context(cls, by_15m: dict[str, list[dict]]) -> dict:
        btc_rows = by_15m.get("BTCUSDT") or []
        returns_1h = [
            value
            for rows in by_15m.values()
            if (value := cls._return_change(rows, 4)) is not None
        ]
        returns_6h = [
            value
            for rows in by_15m.values()
            if (value := cls._return_change(rows, 24)) is not None
        ]
        ordered = sorted(returns_1h)
        median_return = (
            ordered[len(ordered) // 2]
            if ordered
            else None
        )
        btc_ret_1h = cls._return_change(btc_rows, 4)
        btc_ret_6h = cls._return_change(btc_rows, 24)
        breadth_1h = (
                sum(value > 0 for value in returns_1h) / len(returns_1h)
                if returns_1h
                else None
            )
        breadth_6h = (
                sum(value > 0 for value in returns_6h) / len(returns_6h)
                if returns_6h
                else None
            )
        market_phase_code = 0.0
        if (
            (breadth_1h is not None and breadth_1h <= 0.35)
            or (btc_ret_1h is not None and btc_ret_1h <= -1.0)
        ):
            market_phase_code = -1.0
        elif (
            breadth_1h is not None
            and breadth_1h >= 0.60
            and (btc_ret_1h is None or btc_ret_1h >= 0)
        ):
            market_phase_code = 1.0
        return {
            "btc_ret_1h": btc_ret_1h,
            "btc_ret_6h": btc_ret_6h,
            "market_breadth_1h": breadth_1h,
            "market_breadth_6h": breadth_6h,
            "alpha_universe_median_return": median_return,
            "market_phase_code": market_phase_code,
        }

    @staticmethod
    def _stage(current) -> str:
        if current is None:
            return "setup"
        state = current.state.value
        if state.startswith("WATCH") or state in {"IDLE"}:
            return "setup"
        if state == "ARMED":
            return "trigger"
        if state in {
            "PROBE_READY",
            "WAIT_RETEST",
            "ACCEPTANCE_PENDING",
        }:
            return "acceptance"
        return "retest"

    def _rule_prediction_if_collecting(
        self,
        prediction: dict,
        *,
        setup_score: float,
        setup_type: str | None,
        trigger,
        stage: str,
    ) -> dict:
        if prediction.get("p_setup_success") is not None:
            return prediction
        allow_rule_fallback = self.mode == "shadow" or (
            self.mode == "testnet_live"
            and self.testnet_live_rule_fallback
        )
        if not allow_rule_fallback:
            return prediction
        followthrough = 0.0
        fakeout = 0.5
        if trigger.trigger_detected:
            followthrough, fakeout = 0.68, 0.30
        elif trigger.acceptance_confirmed:
            followthrough, fakeout = 0.72, 0.22
        max_position_factor = 0.0
        if trigger.trigger_detected:
            max_position_factor = 0.5 if (
                stage == "setup"
                and setup_type == "sentiment_reversal"
                and setup_score >= 100
            ) else 0.30
        elif trigger.acceptance_confirmed:
            max_position_factor = 0.70
        elif trigger.retest_confirmed:
            max_position_factor = 1.0
        fallback_name = (
            "rule-live-v1"
            if self.mode == "testnet_live"
            else "rule-shadow-v1"
        )
        return {
            **prediction,
            "status": "rule_shadow",
            "applied": False,
            "p_setup_success": min(1.0, max(0.0, setup_score / 100.0)),
            "p_followthrough": followthrough,
            "p_fakeout": fakeout,
            "expected_r": max(0.0, followthrough - fakeout),
            "max_position_factor": max_position_factor,
            "model_versions": {"fallback": fallback_name},
            "reasons": [
                *(prediction.get("reasons") or []),
                "AI model collecting; bounded rule probability used",
            ],
        }

    @staticmethod
    def _collecting_observation(
        *,
        snapshot: AlphaFeatureSnapshot,
        current,
        setup,
        trigger,
        prediction: dict,
    ) -> TransitionResult:
        """Represent an evaluated bar while models are still collecting."""
        state = current.state if current else AlphaSignalState.IDLE
        features = snapshot.features
        projection = build_strategy_projection(
            features,
            setup_type=setup.setup_type or (current.setup_type if current else None),
        )
        base_low = features.get("base_low_2h")
        base_high = features.get("base_high_2h")
        breakout_level = features.get("breakout_level") or base_high
        invalidation_price = (
            current.invalidation_price
            if current and current.invalidation_price is not None
            else (float(base_low) * 0.99 if base_low is not None else None)
        )
        reasons = tuple(
            dict.fromkeys(
                [
                    *setup.reasons,
                    *trigger.reasons,
                    *(prediction.get("reasons") or []),
                    "ai_prediction_not_ready",
                ]
            )
        )
        return TransitionResult(
            from_state=state,
            to_state=state,
            action_type=ActionType.NONE,
            changed=False,
            candle_close_time=snapshot.candle_close_time,
            snapshot_id=snapshot.snapshot_id,
            setup_type=(
                setup.setup_type
                or (current.setup_type if current else None)
            ),
            setup_id=current.setup_id if current else None,
            started_at=(
                current.started_at
                if current
                else snapshot.candle_close_time
            ),
            expires_at=current.expires_at if current else None,
            reference_price=features.get("current_price"),
            base_low=base_low,
            base_high=base_high,
            breakout_level=breakout_level,
            invalidation_price=invalidation_price,
            setup_probability=(
                current.setup_probability if current else None
            ),
            followthrough_probability=(
                current.followthrough_probability if current else None
            ),
            fakeout_probability=(
                current.fakeout_probability if current else None
            ),
            expected_r=current.expected_r if current else None,
            max_position_factor=0.0,
            reasons=reasons,
            model_versions=(
                dict(current.model_versions)
                if current
                else dict(prediction.get("model_versions") or {})
            ),
            previous_version=current.state_version if current else 0,
            metrics={"projection": projection},
        )
