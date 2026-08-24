"""Typed state and event models for Alpha Strategy V2."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping


class AlphaSignalState(str, Enum):
    IDLE = "IDLE"
    WATCH_ACCUMULATION = "WATCH_ACCUMULATION"
    WATCH_CONTINUATION = "WATCH_CONTINUATION"
    WATCH_RECLAIM = "WATCH_RECLAIM"
    ARMED = "ARMED"
    TRIGGER_PENDING = "TRIGGER_PENDING"
    PROBE_READY = "PROBE_READY"
    WAIT_RETEST = "WAIT_RETEST"
    ACCEPTANCE_PENDING = "ACCEPTANCE_PENDING"
    CONFIRMED = "CONFIRMED"
    RETEST_READY = "RETEST_READY"
    FAILED = "FAILED"
    COOLDOWN = "COOLDOWN"
    EXPIRED = "EXPIRED"


class ActionType(str, Enum):
    NONE = "NONE"
    PROBE_LONG = "PROBE_LONG"
    CONFIRM_LONG = "CONFIRM_LONG"
    RETEST_ADD = "RETEST_ADD"
    INVALIDATE_PROBE = "INVALIDATE_PROBE"


@dataclass(frozen=True)
class StrategyObservation:
    snapshot_id: str
    candle_close_time: datetime
    setup_type: str | None
    setup_detected: bool
    setup_probability: float
    followthrough_probability: float
    fakeout_probability: float
    trigger_detected: bool
    overheated: bool
    acceptance_confirmed: bool
    retest_confirmed: bool
    invalidated: bool
    data_ready: bool
    near_trigger_detected: bool = False
    reference_price: float | None = None
    base_low: float | None = None
    base_high: float | None = None
    breakout_level: float | None = None
    invalidation_price: float | None = None
    expected_r: float | None = None
    max_position_factor: float = 0.0
    reasons: tuple[str, ...] = ()
    model_versions: Mapping[str, str] = field(default_factory=dict)
    metrics: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StateRecord:
    market_env: str
    futures_symbol: str
    alpha_symbol: str | None
    state: AlphaSignalState
    setup_type: str | None
    setup_id: str | None
    state_version: int
    started_at: datetime
    updated_at: datetime
    expires_at: datetime | None
    last_candle_close_time: datetime | None
    snapshot_id: str | None
    reference_price: float | None = None
    base_low: float | None = None
    base_high: float | None = None
    breakout_level: float | None = None
    invalidation_price: float | None = None
    setup_probability: float | None = None
    followthrough_probability: float | None = None
    fakeout_probability: float | None = None
    expected_r: float | None = None
    model_versions: Mapping[str, str] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()
    metrics: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TransitionResult:
    from_state: AlphaSignalState
    to_state: AlphaSignalState
    action_type: ActionType
    changed: bool
    candle_close_time: datetime
    snapshot_id: str
    setup_type: str | None
    setup_id: str | None
    started_at: datetime
    expires_at: datetime | None
    reference_price: float | None
    base_low: float | None
    base_high: float | None
    breakout_level: float | None
    invalidation_price: float | None
    setup_probability: float | None
    followthrough_probability: float | None
    fakeout_probability: float | None
    expected_r: float | None
    max_position_factor: float
    reasons: tuple[str, ...]
    model_versions: Mapping[str, str]
    previous_version: int
    metrics: Mapping[str, Any] = field(default_factory=dict)

    def as_state_record(
        self,
        market_env: str,
        futures_symbol: str,
        alpha_symbol: str | None,
    ) -> StateRecord:
        version = self.previous_version + (1 if self.changed else 0)
        return StateRecord(
            market_env=market_env,
            futures_symbol=futures_symbol,
            alpha_symbol=alpha_symbol,
            state=self.to_state,
            setup_type=self.setup_type,
            setup_id=self.setup_id,
            state_version=version,
            started_at=self.started_at,
            updated_at=self.candle_close_time,
            expires_at=self.expires_at,
            last_candle_close_time=self.candle_close_time,
            snapshot_id=self.snapshot_id,
            reference_price=self.reference_price,
            base_low=self.base_low,
            base_high=self.base_high,
            breakout_level=self.breakout_level,
            invalidation_price=self.invalidation_price,
            setup_probability=self.setup_probability,
            followthrough_probability=self.followthrough_probability,
            fakeout_probability=self.fakeout_probability,
            expected_r=self.expected_r,
            model_versions=dict(self.model_versions),
            reasons=tuple(self.reasons),
            metrics=dict(self.metrics),
        )


@dataclass(frozen=True)
class ApplyResult:
    applied: bool
    event_id: str | None = None
