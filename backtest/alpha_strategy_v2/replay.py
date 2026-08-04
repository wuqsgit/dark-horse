from __future__ import annotations

from datetime import datetime
from typing import Callable, Iterable, Mapping

from alpha_engine.strategy.feature_builder import build_alpha_feature_snapshot
from alpha_engine.strategy.models import StrategyObservation
from alpha_engine.strategy.setup_rules import detect_setup
from alpha_engine.strategy.state_machine import AlphaStrategyStateMachine
from alpha_engine.strategy.trigger_rules import evaluate_trigger
from backtest.alpha_strategy_v2.event_clock import EventClock, parse_time
from backtest.alpha_strategy_v2.labels import label_counterfactual_path
from backtest.alpha_strategy_v2.metrics import replay_metrics


class AlphaStrategyReplay:
    """Replay the production feature builder and state machine at event time."""

    def __init__(
        self,
        *,
        machine: AlphaStrategyStateMachine | None = None,
        predictor: Callable[[dict], dict] | None = None,
    ):
        self.machine = machine or AlphaStrategyStateMachine()
        self.predictor = predictor

    @staticmethod
    def _rule_prediction(setup_score: float, trigger) -> dict:
        return {
            "p_setup_success": min(1.0, max(0.0, setup_score / 100)),
            "p_followthrough": 0.72 if trigger.trigger_detected else 0.0,
            "p_fakeout": 0.22 if trigger.trigger_detected else 0.5,
            "expected_r": 0.5 if trigger.trigger_detected else 0.0,
            "max_position_factor": 0.3 if trigger.trigger_detected else 0.0,
            "model_versions": {"replay": "rule-v1"},
        }

    @staticmethod
    def _market_context_at(
        market_context: Mapping | Callable[[datetime], Mapping] | None,
        cutoff: datetime,
    ) -> dict:
        if market_context is None:
            return {}
        if callable(market_context):
            return dict(market_context(cutoff) or {})
        context = dict(market_context)
        timeline = context.pop("timeline", None)
        if timeline is not None:
            eligible = [
                dict(row)
                for row in timeline
                if row.get("time") is not None
                and parse_time(row["time"]) < cutoff
            ]
            if not eligible:
                return {}
            latest = max(eligible, key=lambda row: parse_time(row["time"]))
            latest.pop("time", None)
            return latest
        as_of = context.pop("as_of", None)
        if as_of is None or parse_time(as_of) >= cutoff:
            # Untimestamped context is unsafe in historical replay.
            return {}
        return context

    def run(
        self,
        *,
        alpha_symbol: str,
        futures_symbol: str,
        market_env: str,
        candles_15m: Iterable[Mapping],
        candles_1h: Iterable[Mapping],
        spot_candles_15m: Iterable[Mapping] | None = None,
        futures_snapshots: Iterable[Mapping] | None = None,
        orderbook_snapshots: Iterable[Mapping] | None = None,
        market_context: Mapping | Callable[[datetime], Mapping] | None = None,
        listing_time: datetime | str | None = None,
    ) -> dict:
        c15 = [dict(row) for row in candles_15m]
        c1h = [dict(row) for row in candles_1h]
        ticks = EventClock().ticks(c15)
        current = None
        audit = []

        for cutoff in ticks:
            available_15m = [
                row for row in c15
                if parse_time(row["time"]) < cutoff
            ]
            available_1h = [
                row for row in c1h
                if parse_time(row["time"]) < cutoff
            ]
            if len(available_15m) < 32 or len(available_1h) < 24:
                continue
            snapshot = build_alpha_feature_snapshot(
                alpha_symbol=alpha_symbol,
                futures_symbol=futures_symbol,
                market_env=market_env,
                cutoff_time=cutoff,
                candles_15m=available_15m,
                candles_1h=available_1h,
                spot_candles_15m=spot_candles_15m,
                futures_snapshots=futures_snapshots,
                orderbook_snapshots=orderbook_snapshots,
                market_context=self._market_context_at(
                    market_context,
                    cutoff,
                ),
                listing_time=listing_time,
            )
            setup = detect_setup(snapshot.features)
            trigger = evaluate_trigger(snapshot.features, current)
            payload = {
                "stage": "setup" if current is None else current.state.value.lower(),
                "features": dict(snapshot.features),
                "feature_quality": dict(snapshot.quality),
            }
            prediction = (
                dict(self.predictor(payload) or {})
                if self.predictor
                else self._rule_prediction(setup.score, trigger)
            )
            base_low = snapshot.features.get("base_low_2h")
            observation = StrategyObservation(
                snapshot_id=snapshot.snapshot_id,
                candle_close_time=snapshot.candle_close_time,
                setup_type=setup.setup_type or (current.setup_type if current else None),
                setup_detected=setup.detected,
                setup_probability=float(prediction.get("p_setup_success") or 0),
                followthrough_probability=float(prediction.get("p_followthrough") or 0),
                fakeout_probability=float(
                    prediction.get("p_fakeout")
                    if prediction.get("p_fakeout") is not None
                    else 1
                ),
                trigger_detected=trigger.trigger_detected,
                overheated=trigger.overheated,
                acceptance_confirmed=trigger.acceptance_confirmed,
                retest_confirmed=trigger.retest_confirmed,
                invalidated=trigger.invalidated,
                data_ready=snapshot.quality.get("status") == "ready",
                reference_price=snapshot.features.get("current_price"),
                base_low=base_low,
                base_high=snapshot.features.get("base_high_2h"),
                breakout_level=snapshot.features.get("breakout_level"),
                invalidation_price=(
                    current.invalidation_price
                    if current and current.invalidation_price is not None
                    else float(base_low) * 0.99 if base_low is not None else None
                ),
                expected_r=prediction.get("expected_r"),
                max_position_factor=float(
                    prediction.get("max_position_factor") or 0
                ),
                reasons=tuple(setup.reasons) + tuple(trigger.reasons),
                model_versions=prediction.get("model_versions") or {},
            )
            transition = self.machine.transition(
                current,
                observation,
                now=snapshot.candle_close_time,
            )
            future = [
                row for row in c15
                if parse_time(row["time"]) >= cutoff
            ]
            label = label_counterfactual_path(
                stage=payload["stage"],
                entry_price=float(snapshot.features["current_price"]),
                invalidation_price=observation.invalidation_price,
                breakout_level=observation.breakout_level,
                candles=future,
            )
            audit.append(
                {
                    "candle_close_time": snapshot.candle_close_time.isoformat(),
                    "snapshot_id": snapshot.snapshot_id,
                    "from_state": transition.from_state.value,
                    "to_state": transition.to_state.value,
                    "action_type": transition.action_type.value,
                    "changed": transition.changed,
                    "setup_type": transition.setup_type,
                    "features": dict(snapshot.features),
                    "quality": dict(snapshot.quality),
                    "prediction": prediction,
                    "label": label,
                }
            )
            if transition.changed:
                current = transition.as_state_record(
                    market_env,
                    futures_symbol,
                    alpha_symbol,
                )

        return {
            "symbol": futures_symbol,
            "market_env": market_env,
            "rows": audit,
            "metrics": replay_metrics(audit),
        }
