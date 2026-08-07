import unittest
from datetime import datetime, timezone

from alpha_engine.strategy.models import (
    ActionType,
    AlphaSignalState,
    StateRecord,
    StrategyObservation,
)
from alpha_engine.strategy.projection import build_strategy_projection
from alpha_engine.strategy.state_machine import AlphaStrategyStateMachine


class AlphaStrategyProjectionTest(unittest.TestCase):
    def test_high_volatility_alpha_uses_wide_upside_map(self):
        projection = build_strategy_projection(
            {
                "current_price": 0.004125,
                "base_low_2h": 0.00385,
                "base_high_2h": 0.00420,
                "breakout_level": 0.00420,
                "range_2h_pct": 7.0,
                "range_6h_pct": 9.0,
                "range_24h_pct": 18.0,
                "atr_15m_pct": 1.7,
                "ret_1h": 5.2,
                "ret_4h": 3.8,
                "funding_rate": 0.00034,
                "oi_change_1h": -2.0,
                "taker_buy_quote_ratio": 0.54,
                "depth_imbalance": 0.30,
            },
            setup_type="sentiment_reversal",
        )

        self.assertEqual(projection["status"], "ready")
        self.assertTrue(projection["wide_bias"])
        self.assertGreaterEqual(projection["imagination_pct"], 24.0)
        extreme = projection["targets"][-1]["price"]
        self.assertGreaterEqual(extreme, 0.0051)
        self.assertLess(
            projection["failure"]["below"],
            0.00385,
        )

    def test_projection_metrics_survive_state_transition(self):
        now = datetime(2026, 8, 6, 10, 0, tzinfo=timezone.utc)
        machine = AlphaStrategyStateMachine()
        current = StateRecord(
            market_env="mainnet",
            futures_symbol="AKEUSDT",
            alpha_symbol="ALPHA_331USDT",
            state=AlphaSignalState.ARMED,
            setup_type="accumulation",
            setup_id="setup-1",
            state_version=3,
            started_at=now,
            updated_at=now,
            expires_at=None,
            last_candle_close_time=None,
            snapshot_id="snap-0",
        )
        observation = StrategyObservation(
            snapshot_id="snap-1",
            candle_close_time=now,
            setup_type="accumulation",
            setup_detected=True,
            setup_probability=0.75,
            followthrough_probability=0.72,
            fakeout_probability=0.22,
            trigger_detected=True,
            overheated=False,
            acceptance_confirmed=False,
            retest_confirmed=False,
            invalidated=False,
            data_ready=True,
            reference_price=0.004125,
            base_low=0.00385,
            base_high=0.00420,
            breakout_level=0.00420,
            invalidation_price=0.0038115,
            expected_r=0.5,
            max_position_factor=0.3,
            reasons=("probe_trigger_confirmed",),
            model_versions={"test": "v1"},
            metrics={"projection": {"status": "ready", "wide_bias": True}},
        )

        transition = machine.transition(current, observation, now=now)
        state = transition.as_state_record(
            "mainnet",
            "AKEUSDT",
            "ALPHA_331USDT",
        )

        self.assertEqual(transition.action_type, ActionType.PROBE_LONG)
        self.assertEqual(state.metrics["projection"]["wide_bias"], True)


if __name__ == "__main__":
    unittest.main()
