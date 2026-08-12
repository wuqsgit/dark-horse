import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import shared.db as db
from alpha_engine.strategy.models import (
    ActionType,
    AlphaSignalState,
    TransitionResult,
)
from alpha_engine.strategy.repository import AlphaStrategyRepository


class AlphaStrategyRepositoryTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "strategy.db")
        self.db_patch = patch.object(db, "DB_PATH", self.db_path)
        self.db_patch.start()
        db.init_db()
        self.repo = AlphaStrategyRepository()

    def tearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_schema_contains_state_and_event_tables(self):
        conn = sqlite3.connect(self.db_path)
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            conn.close()

        self.assertIn("alpha_feature_snapshots", tables)
        self.assertIn("alpha_signal_states", tables)
        self.assertIn("alpha_signal_events", tables)
        self.assertIn("alpha_signal_consumptions", tables)

    def test_transition_is_atomic_and_event_is_idempotent(self):
        now = datetime(2026, 7, 28, 4, 0, tzinfo=timezone.utc)
        transition = TransitionResult(
            from_state=AlphaSignalState.IDLE,
            to_state=AlphaSignalState.PROBE_READY,
            action_type=ActionType.PROBE_LONG,
            changed=True,
            candle_close_time=now,
            snapshot_id="snap-1",
            setup_type="accumulation",
            setup_id="setup-1",
            started_at=now,
            expires_at=None,
            reference_price=1.0,
            base_low=0.95,
            base_high=1.02,
            breakout_level=1.02,
            invalidation_price=0.94,
            setup_probability=0.75,
            followthrough_probability=0.78,
            fakeout_probability=0.18,
            expected_r=0.4,
            max_position_factor=0.3,
            reasons=("trigger_confirmed",),
            model_versions={"trigger": "v1"},
            previous_version=0,
        )

        first = self.repo.apply_transition(
            market_env="mainnet",
            futures_symbol="AKEUSDT",
            alpha_symbol="AKEALPHAUSDT",
            transition=transition,
            strategy_mode="testnet_live",
        )
        second = self.repo.apply_transition(
            market_env="mainnet",
            futures_symbol="AKEUSDT",
            alpha_symbol="AKEALPHAUSDT",
            transition=transition,
            strategy_mode="testnet_live",
        )

        self.assertTrue(first.applied)
        self.assertFalse(second.applied)
        state = self.repo.get_state("mainnet", "AKEUSDT")
        self.assertEqual(state.state, AlphaSignalState.PROBE_READY)
        events = self.repo.fetch_actionable_events("mainnet")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["action_type"], "PROBE_LONG")
        self.assertEqual(events[0]["strategy_mode"], "testnet_live")

    def test_shadow_action_event_is_not_executable(self):
        now = datetime(2026, 7, 28, 4, 0, tzinfo=timezone.utc)
        transition = TransitionResult(
            from_state=AlphaSignalState.IDLE,
            to_state=AlphaSignalState.PROBE_READY,
            action_type=ActionType.PROBE_LONG,
            changed=True,
            candle_close_time=now,
            snapshot_id="shadow-snap-1",
            setup_type="accumulation",
            setup_id="shadow-setup-1",
            started_at=now,
            expires_at=None,
            reference_price=1.0,
            base_low=0.95,
            base_high=1.02,
            breakout_level=1.02,
            invalidation_price=0.94,
            setup_probability=0.75,
            followthrough_probability=0.78,
            fakeout_probability=0.18,
            expected_r=0.4,
            max_position_factor=0.3,
            reasons=("trigger_confirmed",),
            model_versions={"trigger": "v1"},
            previous_version=0,
        )

        self.repo.apply_transition(
            market_env="mainnet",
            futures_symbol="AKEUSDT",
            alpha_symbol="AKEALPHAUSDT",
            transition=transition,
            strategy_mode="shadow",
        )

        self.assertEqual(self.repo.fetch_actionable_events("mainnet"), [])

    def test_account_consumes_event_only_once(self):
        now = datetime(2026, 7, 28, 4, 0, tzinfo=timezone.utc)
        transition = TransitionResult(
            from_state=AlphaSignalState.IDLE,
            to_state=AlphaSignalState.PROBE_READY,
            action_type=ActionType.PROBE_LONG,
            changed=True,
            candle_close_time=now,
            snapshot_id="snap-1",
            setup_type="accumulation",
            setup_id="setup-1",
            started_at=now,
            expires_at=None,
            reference_price=1.0,
            base_low=0.95,
            base_high=1.02,
            breakout_level=1.02,
            invalidation_price=0.94,
            setup_probability=0.75,
            followthrough_probability=0.78,
            fakeout_probability=0.18,
            expected_r=0.4,
            max_position_factor=0.3,
            reasons=("trigger_confirmed",),
            model_versions={"trigger": "v1"},
            previous_version=0,
        )
        applied = self.repo.apply_transition(
            market_env="mainnet",
            futures_symbol="AKEUSDT",
            alpha_symbol="AKEALPHAUSDT",
            transition=transition,
        )
        event_id = applied.event_id

        first = self.repo.claim_event(1, event_id, ActionType.PROBE_LONG)
        second = self.repo.claim_event(1, event_id, ActionType.PROBE_LONG)

        self.assertTrue(first)
        self.assertFalse(second)


if __name__ == "__main__":
    unittest.main()
