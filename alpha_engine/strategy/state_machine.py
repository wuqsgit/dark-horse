"""Deterministic transition rules around probabilistic Alpha predictions."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from alpha_engine.strategy.models import (
    ActionType,
    AlphaSignalState,
    StateRecord,
    StrategyObservation,
    TransitionResult,
)


@dataclass(frozen=True)
class StateMachineConfig:
    setup_watch_threshold: float = 0.55
    setup_arm_threshold: float = 0.62
    trigger_followthrough_threshold: float = 0.65
    trigger_fakeout_max: float = 0.35
    acceptance_followthrough_threshold: float = 0.70
    acceptance_fakeout_max: float = 0.25
    watch_ttl_hours: float = 12
    armed_ttl_hours: float = 4
    acceptance_ttl_bars: int = 2
    wait_retest_ttl_hours: float = 4


WATCH_STATES = {
    AlphaSignalState.WATCH_ACCUMULATION,
    AlphaSignalState.WATCH_CONTINUATION,
    AlphaSignalState.WATCH_RECLAIM,
}

ACTIVE_SETUP_STATES = WATCH_STATES | {
    AlphaSignalState.ARMED,
    AlphaSignalState.PROBE_READY,
    AlphaSignalState.WAIT_RETEST,
    AlphaSignalState.ACCEPTANCE_PENDING,
    AlphaSignalState.CONFIRMED,
    AlphaSignalState.RETEST_READY,
}


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _watch_state(setup_type: str | None) -> AlphaSignalState:
    return {
        "continuation": AlphaSignalState.WATCH_CONTINUATION,
        "reclaim": AlphaSignalState.WATCH_RECLAIM,
    }.get(str(setup_type or "").lower(), AlphaSignalState.WATCH_ACCUMULATION)


def _setup_id(observation: StrategyObservation) -> str:
    raw = (
        f"{observation.setup_type or 'unknown'}:"
        f"{_utc(observation.candle_close_time).isoformat()}:"
        f"{observation.snapshot_id}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


class AlphaStrategyStateMachine:
    def __init__(self, config: StateMachineConfig | None = None):
        self.config = config or StateMachineConfig()

    def transition(
        self,
        current: StateRecord | None,
        observation: StrategyObservation,
        *,
        now: datetime | None = None,
    ) -> TransitionResult:
        now = _utc(now or observation.candle_close_time)
        bar_time = _utc(observation.candle_close_time)
        from_state = current.state if current else AlphaSignalState.IDLE
        previous_version = current.state_version if current else 0
        setup_id = current.setup_id if current else None
        setup_type = observation.setup_type or (current.setup_type if current else None)
        started_at = current.started_at if current else now
        expires_at = current.expires_at if current else None

        def result(
            to_state: AlphaSignalState,
            *,
            action: ActionType = ActionType.NONE,
            reasons: tuple[str, ...] | None = None,
            ttl_hours: float | None = None,
            clear_expiry: bool = False,
            force_changed: bool | None = None,
        ) -> TransitionResult:
            changed = (
                force_changed
                if force_changed is not None
                else to_state != from_state or action != ActionType.NONE
            )
            next_started = now if changed and to_state != from_state else started_at
            if clear_expiry:
                next_expires = None
            elif ttl_hours is not None:
                next_expires = now + timedelta(hours=ttl_hours)
            else:
                next_expires = expires_at
            return TransitionResult(
                from_state=from_state,
                to_state=to_state,
                action_type=action,
                changed=bool(changed),
                candle_close_time=bar_time,
                snapshot_id=observation.snapshot_id,
                setup_type=setup_type,
                setup_id=setup_id,
                started_at=next_started,
                expires_at=next_expires,
                reference_price=observation.reference_price,
                base_low=observation.base_low,
                base_high=observation.base_high,
                breakout_level=observation.breakout_level,
                invalidation_price=observation.invalidation_price,
                setup_probability=observation.setup_probability,
                followthrough_probability=observation.followthrough_probability,
                fakeout_probability=observation.fakeout_probability,
                expected_r=observation.expected_r,
                max_position_factor=observation.max_position_factor,
                reasons=reasons or observation.reasons,
                model_versions=dict(observation.model_versions),
                previous_version=previous_version,
            )

        if current and current.last_candle_close_time:
            if bar_time <= _utc(current.last_candle_close_time):
                return result(
                    from_state,
                    reasons=("duplicate_or_old_closed_candle",),
                    force_changed=False,
                )

        if not observation.data_ready:
            return result(
                from_state,
                reasons=("market_data_not_ready",),
                force_changed=False,
            )

        # Advance terminal states before re-checking the stale condition that
        # caused them. This prevents repeated invalidation events and allows a
        # cooldown to return to IDLE.
        if from_state in {AlphaSignalState.FAILED, AlphaSignalState.EXPIRED}:
            return result(
                AlphaSignalState.COOLDOWN,
                ttl_hours=1,
                reasons=("failure_cooldown_started",),
            )
        if from_state == AlphaSignalState.COOLDOWN:
            if current and current.expires_at and now >= _utc(current.expires_at):
                return result(
                    AlphaSignalState.IDLE,
                    clear_expiry=True,
                    reasons=("cooldown_completed",),
                )
            return result(from_state, force_changed=False)

        # Invalidity and expiry always outrank positive transitions.
        if observation.invalidated and from_state in ACTIVE_SETUP_STATES:
            invalidate_action = (
                ActionType.INVALIDATE_PROBE
                if from_state
                in {
                    AlphaSignalState.PROBE_READY,
                    AlphaSignalState.ACCEPTANCE_PENDING,
                    AlphaSignalState.CONFIRMED,
                    AlphaSignalState.RETEST_READY,
                }
                else ActionType.NONE
            )
            return result(
                AlphaSignalState.FAILED,
                action=invalidate_action,
                reasons=tuple(observation.reasons) + ("structure_invalidated",),
            )
        if current and current.expires_at and now >= _utc(current.expires_at):
            return result(
                AlphaSignalState.EXPIRED,
                reasons=("state_ttl_expired",),
            )

        if from_state == AlphaSignalState.IDLE:
            if (
                observation.setup_detected
                and observation.setup_probability >= self.config.setup_watch_threshold
            ):
                setup_id = _setup_id(observation)
                return result(
                    _watch_state(setup_type),
                    ttl_hours=self.config.watch_ttl_hours,
                    reasons=tuple(observation.reasons) + ("setup_watch_started",),
                )
            return result(from_state, force_changed=False)

        if from_state in WATCH_STATES:
            if observation.setup_probability >= self.config.setup_arm_threshold:
                return result(
                    AlphaSignalState.ARMED,
                    ttl_hours=self.config.armed_ttl_hours,
                    reasons=tuple(observation.reasons) + ("setup_armed",),
                )
            return result(from_state, force_changed=False)

        if from_state == AlphaSignalState.ARMED:
            if observation.trigger_detected:
                if observation.overheated:
                    return result(
                        AlphaSignalState.WAIT_RETEST,
                        ttl_hours=self.config.wait_retest_ttl_hours,
                        reasons=tuple(observation.reasons) + ("overheated_wait_retest",),
                    )
                if (
                    observation.followthrough_probability
                    >= self.config.trigger_followthrough_threshold
                    and observation.fakeout_probability
                    <= self.config.trigger_fakeout_max
                ):
                    return result(
                        AlphaSignalState.PROBE_READY,
                        action=ActionType.PROBE_LONG,
                        ttl_hours=max(
                            1,
                            int(self.config.acceptance_ttl_bars),
                        )
                        * 0.25,
                        reasons=tuple(observation.reasons) + ("probe_trigger_confirmed",),
                    )
            return result(from_state, force_changed=False)

        if from_state == AlphaSignalState.PROBE_READY:
            if self._acceptance_ready(observation):
                return result(
                    AlphaSignalState.CONFIRMED,
                    action=ActionType.CONFIRM_LONG,
                    reasons=tuple(observation.reasons) + ("breakout_accepted",),
                )
            return result(
                AlphaSignalState.ACCEPTANCE_PENDING,
                reasons=("awaiting_breakout_acceptance",),
            )

        if from_state in {
            AlphaSignalState.WAIT_RETEST,
            AlphaSignalState.ACCEPTANCE_PENDING,
        }:
            if self._acceptance_ready(observation):
                return result(
                    AlphaSignalState.CONFIRMED,
                    action=ActionType.CONFIRM_LONG,
                    reasons=tuple(observation.reasons) + ("breakout_accepted",),
                )
            return result(from_state, force_changed=False)

        if from_state == AlphaSignalState.CONFIRMED:
            if observation.retest_confirmed:
                return result(
                    AlphaSignalState.RETEST_READY,
                    action=ActionType.RETEST_ADD,
                    reasons=tuple(observation.reasons) + ("first_retest_confirmed",),
                )
            return result(from_state, force_changed=False)

        if from_state == AlphaSignalState.RETEST_READY:
            return result(
                AlphaSignalState.CONFIRMED,
                reasons=("retest_event_emitted",),
            )

        return result(from_state, force_changed=False)

    def _acceptance_ready(self, observation: StrategyObservation) -> bool:
        return bool(
            observation.acceptance_confirmed
            and observation.followthrough_probability
            >= self.config.acceptance_followthrough_threshold
            and observation.fakeout_probability
            <= self.config.acceptance_fakeout_max
        )
