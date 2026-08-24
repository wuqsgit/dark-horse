import unittest
from datetime import datetime, timedelta, timezone

from alpha_engine.strategy.models import (
    ActionType,
    AlphaSignalState,
    StrategyObservation,
)
from alpha_engine.strategy.state_machine import AlphaStrategyStateMachine


NOW = datetime(2026, 7, 28, 4, 0, tzinfo=timezone.utc)


def _observation(**overrides):
    values = {
        "snapshot_id": "snap-1",
        "candle_close_time": NOW,
        "setup_type": "accumulation",
        "setup_detected": True,
        "setup_probability": 0.58,
        "followthrough_probability": 0.0,
        "fakeout_probability": 0.5,
        "trigger_detected": False,
        "overheated": False,
        "acceptance_confirmed": False,
        "retest_confirmed": False,
        "invalidated": False,
        "data_ready": True,
        "reference_price": 1.0,
        "base_low": 0.95,
        "base_high": 1.02,
        "breakout_level": 1.02,
        "invalidation_price": 0.94,
        "expected_r": 0.0,
        "max_position_factor": 0.0,
        "reasons": ("setup_detected",),
        "model_versions": {},
    }
    values.update(overrides)
    return StrategyObservation(**values)


class AlphaStrategyStateMachineTest(unittest.TestCase):
    def setUp(self):
        self.machine = AlphaStrategyStateMachine()

    def test_accumulation_progresses_to_probe_event(self):
        watch = self.machine.transition(None, _observation(), now=NOW)
        self.assertEqual(watch.to_state, AlphaSignalState.WATCH_ACCUMULATION)
        self.assertEqual(watch.action_type, ActionType.NONE)

        armed = self.machine.transition(
            watch.as_state_record("mainnet", "AKEUSDT", "AKEALPHAUSDT"),
            _observation(
                snapshot_id="snap-2",
                candle_close_time=NOW + timedelta(minutes=15),
                setup_probability=0.66,
            ),
            now=NOW + timedelta(minutes=15),
        )
        self.assertEqual(armed.to_state, AlphaSignalState.ARMED)

        probe = self.machine.transition(
            armed.as_state_record("mainnet", "AKEUSDT", "AKEALPHAUSDT"),
            _observation(
                snapshot_id="snap-3",
                candle_close_time=NOW + timedelta(minutes=30),
                setup_probability=0.72,
                followthrough_probability=0.75,
                fakeout_probability=0.20,
                trigger_detected=True,
                expected_r=0.4,
                max_position_factor=0.3,
            ),
            now=NOW + timedelta(minutes=30),
        )
        self.assertEqual(probe.to_state, AlphaSignalState.PROBE_READY)
        self.assertEqual(probe.action_type, ActionType.PROBE_LONG)

    def test_high_quality_setup_can_probe_from_idle_on_trigger_bar(self):
        result = self.machine.transition(
            None,
            _observation(
                setup_probability=0.75,
                followthrough_probability=0.68,
                fakeout_probability=0.30,
                trigger_detected=True,
                max_position_factor=0.3,
            ),
            now=NOW,
        )

        self.assertEqual(result.to_state, AlphaSignalState.PROBE_READY)
        self.assertEqual(result.action_type, ActionType.PROBE_LONG)
        self.assertIn("same_bar_probe_trigger_confirmed", result.reasons)

    def test_watch_can_probe_without_intermediate_armed_bar(self):
        watch = self.machine.transition(None, _observation(), now=NOW)

        result = self.machine.transition(
            watch.as_state_record("mainnet", "AKEUSDT", "AKEALPHAUSDT"),
            _observation(
                snapshot_id="snap-2",
                candle_close_time=NOW + timedelta(minutes=15),
                setup_probability=0.75,
                followthrough_probability=0.68,
                fakeout_probability=0.30,
                trigger_detected=True,
                max_position_factor=0.3,
            ),
            now=NOW + timedelta(minutes=15),
        )

        self.assertEqual(result.to_state, AlphaSignalState.PROBE_READY)
        self.assertEqual(result.action_type, ActionType.PROBE_LONG)
        self.assertIn("same_bar_probe_trigger_confirmed", result.reasons)

    def test_low_quality_idle_trigger_only_starts_watch(self):
        result = self.machine.transition(
            None,
            _observation(
                setup_probability=0.58,
                followthrough_probability=0.68,
                fakeout_probability=0.30,
                trigger_detected=True,
            ),
            now=NOW,
        )

        self.assertEqual(result.to_state, AlphaSignalState.WATCH_ACCUMULATION)
        self.assertEqual(result.action_type, ActionType.NONE)

    def test_overheated_idle_trigger_waits_for_retest(self):
        result = self.machine.transition(
            None,
            _observation(
                setup_probability=0.75,
                followthrough_probability=0.68,
                fakeout_probability=0.30,
                trigger_detected=True,
                overheated=True,
            ),
            now=NOW,
        )

        self.assertEqual(result.to_state, AlphaSignalState.WAIT_RETEST)
        self.assertEqual(result.action_type, ActionType.NONE)
        self.assertIn("same_bar_overheated_wait_retest", result.reasons)

    def test_overheated_trigger_waits_for_retest_instead_of_cooldown(self):
        current = self.machine.transition(
            None,
            _observation(setup_probability=0.7),
            now=NOW,
        ).as_state_record("mainnet", "AKEUSDT", "AKEALPHAUSDT")
        armed = self.machine.transition(
            current,
            _observation(
                snapshot_id="snap-2",
                candle_close_time=NOW + timedelta(minutes=15),
                setup_probability=0.7,
            ),
            now=NOW + timedelta(minutes=15),
        ).as_state_record("mainnet", "AKEUSDT", "AKEALPHAUSDT")

        result = self.machine.transition(
            armed,
            _observation(
                snapshot_id="snap-3",
                candle_close_time=NOW + timedelta(minutes=30),
                trigger_detected=True,
                overheated=True,
                followthrough_probability=0.8,
                fakeout_probability=0.15,
            ),
            now=NOW + timedelta(minutes=30),
        )

        self.assertEqual(result.to_state, AlphaSignalState.WAIT_RETEST)
        self.assertEqual(result.action_type, ActionType.NONE)

    def test_near_trigger_is_retained_and_next_hold_opens_small_probe(self):
        watch = self.machine.transition(
            None,
            _observation(setup_probability=0.7),
            now=NOW,
        )
        armed = self.machine.transition(
            watch.as_state_record("mainnet", "AKEUSDT", "AKEALPHAUSDT"),
            _observation(
                snapshot_id="snap-2",
                candle_close_time=NOW + timedelta(minutes=15),
                setup_probability=0.7,
            ),
            now=NOW + timedelta(minutes=15),
        )
        pending = self.machine.transition(
            armed.as_state_record("mainnet", "AKEUSDT", "AKEALPHAUSDT"),
            _observation(
                snapshot_id="snap-3",
                candle_close_time=NOW + timedelta(minutes=30),
                setup_probability=0.7,
                near_trigger_detected=True,
                reference_price=1.03,
            ),
            now=NOW + timedelta(minutes=30),
        )

        self.assertEqual(pending.to_state, AlphaSignalState.TRIGGER_PENDING)
        self.assertEqual(pending.action_type, ActionType.NONE)

        probe = self.machine.transition(
            pending.as_state_record("mainnet", "AKEUSDT", "AKEALPHAUSDT"),
            _observation(
                snapshot_id="snap-4",
                candle_close_time=NOW + timedelta(minutes=45),
                setup_probability=0.7,
                followthrough_probability=0.72,
                fakeout_probability=0.22,
                acceptance_confirmed=True,
                max_position_factor=0.7,
            ),
            now=NOW + timedelta(minutes=45),
        )

        self.assertEqual(probe.to_state, AlphaSignalState.PROBE_READY)
        self.assertEqual(probe.action_type, ActionType.PROBE_LONG)
        self.assertEqual(probe.max_position_factor, 0.15)
        self.assertIn("multi_bar_trigger_confirmed", probe.reasons)

        confirmed = self.machine.transition(
            probe.as_state_record("mainnet", "AKEUSDT", "AKEALPHAUSDT"),
            _observation(
                snapshot_id="snap-5",
                candle_close_time=NOW + timedelta(hours=1),
                setup_probability=0.7,
                followthrough_probability=0.75,
                fakeout_probability=0.20,
                acceptance_confirmed=True,
            ),
            now=NOW + timedelta(hours=1),
        )
        self.assertEqual(confirmed.to_state, AlphaSignalState.CONFIRMED)
        self.assertIsNone(confirmed.expires_at)

    def test_watch_invalidation_has_priority_without_exit_event(self):
        current = self.machine.transition(
            None,
            _observation(setup_probability=0.7),
            now=NOW,
        ).as_state_record("mainnet", "AKEUSDT", "AKEALPHAUSDT")

        result = self.machine.transition(
            current,
            _observation(
                snapshot_id="snap-2",
                candle_close_time=NOW + timedelta(minutes=15),
                trigger_detected=True,
                invalidated=True,
            ),
            now=NOW + timedelta(minutes=15),
        )

        self.assertEqual(result.to_state, AlphaSignalState.FAILED)
        self.assertEqual(result.action_type, ActionType.NONE)

    def test_idle_symbol_is_not_failed_by_unowned_structure_level(self):
        result = self.machine.transition(
            None,
            _observation(
                setup_detected=False,
                setup_probability=0.1,
                invalidated=True,
            ),
            now=NOW,
        )

        self.assertFalse(result.changed)
        self.assertEqual(result.to_state, AlphaSignalState.IDLE)
        self.assertNotIn("structure_invalidated", result.reasons)

    def test_probe_invalidation_emits_single_exit_event(self):
        watch = self.machine.transition(
            None,
            _observation(setup_probability=0.7),
            now=NOW,
        )
        armed = self.machine.transition(
            watch.as_state_record("mainnet", "AKEUSDT", "AKEALPHAUSDT"),
            _observation(
                snapshot_id="snap-2",
                candle_close_time=NOW + timedelta(minutes=15),
                setup_probability=0.7,
            ),
            now=NOW + timedelta(minutes=15),
        )
        probe = self.machine.transition(
            armed.as_state_record("mainnet", "AKEUSDT", "AKEALPHAUSDT"),
            _observation(
                snapshot_id="snap-3",
                candle_close_time=NOW + timedelta(minutes=30),
                setup_probability=0.7,
                followthrough_probability=0.75,
                fakeout_probability=0.2,
                trigger_detected=True,
            ),
            now=NOW + timedelta(minutes=30),
        )

        failed = self.machine.transition(
            probe.as_state_record("mainnet", "AKEUSDT", "AKEALPHAUSDT"),
            _observation(
                snapshot_id="snap-4",
                candle_close_time=NOW + timedelta(minutes=45),
                invalidated=True,
            ),
            now=NOW + timedelta(minutes=45),
        )

        self.assertEqual(failed.to_state, AlphaSignalState.FAILED)
        self.assertEqual(failed.action_type, ActionType.INVALIDATE_PROBE)

        cooldown = self.machine.transition(
            failed.as_state_record("mainnet", "AKEUSDT", "AKEALPHAUSDT"),
            _observation(
                snapshot_id="snap-5",
                candle_close_time=NOW + timedelta(hours=1),
                invalidated=True,
            ),
            now=NOW + timedelta(hours=1),
        )
        self.assertEqual(cooldown.to_state, AlphaSignalState.COOLDOWN)
        self.assertEqual(cooldown.action_type, ActionType.NONE)

        idle = self.machine.transition(
            cooldown.as_state_record("mainnet", "AKEUSDT", "AKEALPHAUSDT"),
            _observation(
                snapshot_id="snap-6",
                candle_close_time=NOW + timedelta(hours=2),
                invalidated=True,
            ),
            now=NOW + timedelta(hours=2),
        )
        self.assertEqual(idle.to_state, AlphaSignalState.IDLE)
        self.assertEqual(idle.action_type, ActionType.NONE)
        self.assertIsNone(idle.expires_at)

    def test_same_closed_candle_is_idempotent(self):
        first = self.machine.transition(None, _observation(), now=NOW)
        current = first.as_state_record("mainnet", "AKEUSDT", "AKEALPHAUSDT")

        duplicate = self.machine.transition(current, _observation(), now=NOW)

        self.assertFalse(duplicate.changed)
        self.assertEqual(duplicate.to_state, AlphaSignalState.WATCH_ACCUMULATION)

    def test_sentiment_reversal_can_emit_probe_from_idle(self):
        result = self.machine.transition(
            None,
            _observation(
                setup_type="sentiment_reversal",
                setup_probability=1.0,
                followthrough_probability=0.68,
                fakeout_probability=0.30,
                trigger_detected=True,
                max_position_factor=0.5,
                reasons=("square_extreme_bearishness",),
            ),
            now=NOW,
        )

        self.assertEqual(result.to_state, AlphaSignalState.PROBE_READY)
        self.assertEqual(result.action_type, ActionType.PROBE_LONG)
        self.assertIn("sentiment_reversal_probe_confirmed", result.reasons)


if __name__ == "__main__":
    unittest.main()
