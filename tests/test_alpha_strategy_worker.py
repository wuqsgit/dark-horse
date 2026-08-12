import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import shared.db as db
from alpha_engine.strategy.feature_builder import AlphaFeatureSnapshot
from alpha_engine.strategy.models import AlphaSignalState
from alpha_engine.strategy.worker import AlphaStrategyWorker


def _snapshot(snapshot_id="snap-1", close_time=None):
    close_time = close_time or datetime(2026, 7, 28, 4, 0, tzinfo=timezone.utc)
    return AlphaFeatureSnapshot(
        snapshot_id=snapshot_id,
        market_env="mainnet",
        alpha_symbol="AKEALPHAUSDT",
        futures_symbol="AKEUSDT",
        candle_close_time=close_time,
        feature_schema_version=4,
        features={
            "current_price": 1.0,
            "range_2h_pct": 1.9,
            "quote_volume_ratio_1h": 2.4,
            "quote_volume_ratio_15m": 1.2,
            "absorption_score": 78,
            "ret_2h": 0.4,
            "ret_15m": 0.2,
            "ret_1h": 0.4,
            "ret_6h": 1.0,
            "ema20_slope_1h": -0.1,
            "distance_from_high_24h": -2.0,
            "close_location": 0.7,
            "upper_wick_ratio": 0.2,
            "breakout_distance_pct": -0.3,
            "base_low_2h": 0.97,
            "base_high_2h": 1.01,
            "breakout_level": 1.01,
        },
        quality={"status": "ready", "coverage": 0.9},
    )


class AlphaStrategyWorkerTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp_dir.name) / "strategy.db")
        self.db_patch = patch.object(db, "DB_PATH", self.db_path)
        self.db_patch.start()
        db.init_db()
        self.calls = 0

        def evaluate(payload):
            self.calls += 1
            return {
                "status": "shadow",
                "applied": False,
                "model_versions": {"setup": "setup-v1"},
                "p_setup_success": 0.58 if self.calls == 1 else 0.68,
                "p_followthrough": 0.0,
                "p_fakeout": 0.5,
                "expected_r": 0.1,
                "recommended_action": "watch",
                "max_position_factor": 0.0,
                "reasons": ["ake_style_accumulation"],
            }

        self.worker = AlphaStrategyWorker(
            ai_evaluate=evaluate,
            mode="shadow",
            market_env="mainnet",
        )

    def tearDown(self):
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def test_processes_each_closed_candle_once(self):
        first = self.worker.process_snapshot(_snapshot())
        duplicate = self.worker.process_snapshot(_snapshot())

        self.assertTrue(first.applied)
        self.assertEqual(first.transition.to_state, AlphaSignalState.WATCH_ACCUMULATION)
        self.assertFalse(duplicate.applied)
        self.assertEqual(duplicate.reason, "duplicate_closed_candle")
        self.assertEqual(self.calls, 1)

    def test_next_candle_can_arm_existing_setup(self):
        self.worker.process_snapshot(_snapshot())
        second = self.worker.process_snapshot(
            _snapshot(
                "snap-2",
                datetime(2026, 7, 28, 4, 15, tzinfo=timezone.utc),
            )
        )

        self.assertTrue(second.applied)
        self.assertEqual(second.transition.to_state, AlphaSignalState.ARMED)

    def test_execution_mode_is_independent_from_market_data_environment(self):
        worker = AlphaStrategyWorker(
            ai_evaluate=lambda _: {},
            mode="testnet_live",
            market_env="mainnet",
        )

        self.assertEqual(worker.mode, "testnet_live")
        self.assertEqual(worker.market_env, "mainnet")

    def test_collecting_model_persists_idle_state_and_deduplicates_bar(self):
        calls = 0

        def collecting(_payload):
            nonlocal calls
            calls += 1
            return {
                "status": "collecting",
                "p_setup_success": None,
                "p_followthrough": None,
                "p_fakeout": None,
                "reasons": ["collecting Alpha Strategy V4 samples"],
            }

        worker = AlphaStrategyWorker(
            ai_evaluate=collecting,
            mode="testnet_live",
            market_env="mainnet",
        )
        first = worker.process_snapshot(_snapshot())
        duplicate = worker.process_snapshot(_snapshot())

        self.assertEqual(first.reason, "ai_prediction_not_ready")
        self.assertEqual(duplicate.reason, "duplicate_closed_candle")
        self.assertEqual(calls, 1)
        state = worker.repository.get_state("mainnet", "AKEUSDT")
        self.assertEqual(state.state, AlphaSignalState.IDLE)
        self.assertEqual(state.state_version, 0)
        self.assertEqual(
            state.last_candle_close_time,
            _snapshot().candle_close_time,
        )
        self.assertIn("ai_prediction_not_ready", state.reasons)

    def test_unchanged_state_advances_closed_candle_cursor(self):
        worker = AlphaStrategyWorker(
            ai_evaluate=lambda _payload: {
                "status": "live",
                "p_setup_success": 0.1,
                "p_followthrough": 0.0,
                "p_fakeout": 0.9,
                "reasons": ["setup_probability_low"],
            },
            mode="testnet_live",
            market_env="mainnet",
        )
        snapshot = _snapshot()

        result = worker.process_snapshot(snapshot)

        self.assertEqual(result.reason, "state_unchanged")
        state = worker.repository.get_state("mainnet", "AKEUSDT")
        self.assertEqual(state.state, AlphaSignalState.IDLE)
        self.assertEqual(state.last_candle_close_time, snapshot.candle_close_time)

    def test_mainnet_rule_fallback_emits_sentiment_probe_while_collecting(self):
        base = _snapshot()
        features = {
            **base.features,
            "square_sentiment_available": 1,
            "square_sentiment_age_minutes": 4,
            "square_bearish_ratio": 0.85,
            "square_effective_post_count": 24,
            "square_unique_authors": 20,
            "square_top3_author_share": 0.15,
            "square_bearish_shift_24h": 0.45,
            "square_substantive_risk_count": 0,
            "alpha_discovery_score": 86,
            "spot_volume_ratio_15m": 2.0,
            "futures_volume_ratio_15m": 1.8,
            "volume_sync_score": 0.9,
            "spread_pct_current": 0.10,
            "quote_volume_ratio_15m": 1.8,
            "ret_15m": 0.3,
            "close_location": 0.62,
            "upper_wick_ratio": 0.30,
        }
        worker = AlphaStrategyWorker(
            ai_evaluate=lambda _payload: {
                "status": "collecting",
                "p_setup_success": None,
                "p_followthrough": None,
                "p_fakeout": None,
                "reasons": ["collecting Alpha Strategy V4 samples"],
            },
            mode="testnet_live",
            market_env="mainnet",
            testnet_live_rule_fallback=True,
        )

        result = worker.process_snapshot(
            replace(base, features=features)
        )

        self.assertTrue(result.applied)
        self.assertEqual(result.transition.action_type.value, "PROBE_LONG")
        self.assertEqual(result.transition.max_position_factor, 0.5)


if __name__ == "__main__":
    unittest.main()
