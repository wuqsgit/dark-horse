import json
import unittest
from unittest.mock import patch

import trader.execution as execution
from trader.execution import ExecutionEngine


class DummyExchange:
    pass


class AlphaPositionManagementTest(unittest.TestCase):
    def _engine_with_regime(self, regime):
        engine = ExecutionEngine(DummyExchange())
        engine._latest_alpha_position_context = lambda symbol, hist: {
            "alpha_score": 83.0,
            "volume_price_state": "normal_review",
            "volume_price_action": "normal_review",
            "volume_price_metrics_json": json.dumps({
                "volume_regime": regime,
                "trend_score": 70,
                "ret_15m": 0.2,
                "ret_1h": 1.1,
                "ret_6h": 3.2,
                "spread_pct": 0.01,
            }),
            "volume_price_reasons_json": "[]",
        }
        engine._record_decision = lambda *args, **kwargs: None
        return engine

    def _action(self, engine, protected_regime=None):
        return engine._build_alpha_position_action(
            pos={"symbol": "B2USDT", "side": "LONG", "entry_price": 100.0},
            hist={
                "strategy_source": "alpha",
                "alpha_score": 83.0,
                "alpha_symbol": "ALPHA_162USDT",
                "alpha_entry_level": "candidate",
                "stop_pct": 0.10,
                "alpha_volume_protect_regime": protected_regime,
            },
            pnl_pct=0.5,
            mark_price=100.5,
            close_side="SELL",
            highest_price=101.0,
            atr=1.0,
            age_h=0.5,
        )

    def test_profitable_suspicious_alpha_volume_regime_partially_closes_20_to_30_percent(self):
        engine = ExecutionEngine(DummyExchange())
        engine._latest_alpha_position_context = lambda symbol, hist: {
            "alpha_score": 83.0,
            "volume_price_state": "normal_review",
            "volume_price_action": "normal_review",
            "volume_price_metrics_json": json.dumps({
                "volume_regime": "suspicious",
                "trend_score": 70,
                "ret_15m": 0.2,
                "ret_1h": 1.1,
                "ret_6h": 3.2,
                "spread_pct": 0.01,
            }),
            "volume_price_reasons_json": "[]",
        }

        action = engine._build_alpha_position_action(
            pos={"symbol": "B2USDT", "side": "LONG", "entry_price": 100.0},
            hist={
                "strategy_source": "alpha",
                "alpha_score": 83.0,
                "alpha_symbol": "ALPHA_162USDT",
                "alpha_entry_level": "candidate",
                "stop_pct": 0.10,
            },
            pnl_pct=0.5,
            mark_price=100.5,
            close_side="SELL",
            highest_price=101.0,
            atr=1.0,
            age_h=0.5,
        )

        self.assertIsNotNone(action)
        self.assertEqual(action["action"], "partial_close")
        self.assertIn("alpha_volume_regime_profit_protect", action["reason"])
        self.assertGreaterEqual(action["close_pct"], 0.20)
        self.assertLessEqual(action["close_pct"], 0.30)

    def test_same_volume_regime_does_not_reduce_position_twice(self):
        action = self._action(self._engine_with_regime("suspicious"), protected_regime="suspicious")
        self.assertIsNone(action)

    def test_worse_volume_regime_can_reduce_position_again(self):
        action = self._action(self._engine_with_regime("extreme"), protected_regime="suspicious")
        self.assertIsNotNone(action)
        self.assertEqual(action["action"], "partial_close")

    def _soft_loss_engine(self):
        engine = ExecutionEngine(DummyExchange())
        engine._latest_alpha_position_context = lambda symbol, hist: {
            "alpha_score": 72.0,
            "volume_price_state": "failed_breakout",
            "volume_price_action": "observe",
            "volume_price_metrics_json": json.dumps({
                "volume_regime": "normal",
                "trend_score": 45,
                "ret_15m": -0.8,
                "ret_1h": -1.2,
                "ret_6h": 0.5,
                "spread_pct": 0.01,
            }),
            "volume_price_reasons_json": "[]",
        }
        engine.recorded_decisions = []
        engine._record_decision = lambda *args, **kwargs: engine.recorded_decisions.append(kwargs)
        return engine

    def _soft_loss_action(self, engine, pnl_pct=-1.0, mark_price=99.0):
        return engine._build_alpha_position_action(
            pos={"symbol": "AKEUSDT", "side": "LONG", "entry_price": 100.0},
            hist={
                "position_id": "AKEUSDT-LONG-1",
                "strategy_source": "alpha",
                "alpha_score": 82.0,
                "alpha_symbol": "ALPHA_285USDT",
                "alpha_entry_level": "candidate",
                "stop_pct": 0.10,
            },
            pnl_pct=pnl_pct,
            mark_price=mark_price,
            close_side="SELL",
            highest_price=101.0,
            atr=2.0,
            age_h=0.5,
        )

    @staticmethod
    def _closed_candles(next_close=None):
        candles = [
            {"time": "2026-07-11T08:00:00Z", "low": 100.0, "close": 101.0},
            {"time": "2026-07-11T08:15:00Z", "low": 99.0, "close": 100.0},
            {"time": "2026-07-11T08:30:00Z", "low": 98.0, "close": 99.0},
        ]
        if next_close is not None:
            candles.append({
                "time": "2026-07-11T08:45:00Z",
                "low": min(97.5, next_close),
                "close": next_close,
            })
        return candles

    def test_alpha_small_loss_soft_signal_only_holds(self):
        engine = self._soft_loss_engine()
        with patch.object(execution, "_latest_alpha_soft_exit_confirmation", return_value=None, create=True), \
             patch.object(execution, "_fetch_closed_futures_15m", return_value=self._closed_candles(), create=True):
            action = self._soft_loss_action(engine)

        self.assertIsNone(action)
        self.assertTrue(any("alpha soft hold" in str(item.get("filter_reason", "")) for item in engine.recorded_decisions))

    def test_existing_soft_exit_confirmation_is_cancelled(self):
        engine = self._soft_loss_engine()
        pending = {
            "status": "pending",
            "position_id": "AKEUSDT-LONG-1",
            "trigger_candle_time": "2026-07-11T08:30:00Z",
            "trigger_low": 98.0,
        }
        with patch.object(execution, "_latest_alpha_soft_exit_confirmation", return_value=pending, create=True), \
             patch.object(execution, "_fetch_closed_futures_15m", return_value=self._closed_candles(), create=True):
            action = self._soft_loss_action(engine)

        self.assertIsNone(action)
        self.assertTrue(any(
            "loss_soft_exit_disabled" in str(item.get("filter_reason", ""))
            for item in engine.recorded_decisions
        ))

    def test_alpha_small_loss_does_not_close_after_minor_candle_break(self):
        engine = self._soft_loss_engine()
        pending = {
            "status": "pending",
            "position_id": "AKEUSDT-LONG-1",
            "trigger_candle_time": "2026-07-11T08:30:00Z",
            "trigger_low": 98.0,
        }
        with patch.object(execution, "_latest_alpha_soft_exit_confirmation", return_value=pending, create=True), \
             patch.object(execution, "_fetch_closed_futures_15m", return_value=self._closed_candles(next_close=97.8), create=True):
            action = self._soft_loss_action(engine, pnl_pct=-2.2, mark_price=97.8)

        self.assertIsNone(action)

    def test_alpha_clear_structural_breakdown_still_closes(self):
        engine = self._soft_loss_engine()
        engine._latest_alpha_position_context = lambda symbol, hist: {
            "alpha_score": 60.0,
            "volume_price_state": "breakdown_volume_long_only",
            "volume_price_action": "observe",
            "volume_price_metrics_json": json.dumps({
                "volume_regime": "normal",
                "trend_score": 38,
                "ret_15m": -2.0,
                "ret_1h": -3.5,
                "ret_6h": -9.0,
                "spread_pct": 0.01,
            }),
            "volume_price_reasons_json": "[]",
        }
        action = self._soft_loss_action(engine, pnl_pct=-6.0, mark_price=94.0)

        self.assertIsNotNone(action)
        self.assertEqual(action["action"], "close")
        self.assertIn("alpha_structural_breakdown", action["reason"])

    def test_alpha_small_loss_cancels_exit_when_next_15m_candle_recovers(self):
        engine = self._soft_loss_engine()
        pending = {
            "status": "pending",
            "position_id": "AKEUSDT-LONG-1",
            "trigger_candle_time": "2026-07-11T08:30:00Z",
            "trigger_low": 98.0,
        }
        with patch.object(execution, "_latest_alpha_soft_exit_confirmation", return_value=pending, create=True), \
             patch.object(execution, "_fetch_closed_futures_15m", return_value=self._closed_candles(next_close=98.6), create=True):
            action = self._soft_loss_action(engine, pnl_pct=-1.4, mark_price=98.6)

        self.assertIsNone(action)
        self.assertTrue(any(
            str(item.get("filter_reason", "")).startswith("alpha_soft_exit_cancelled")
            for item in engine.recorded_decisions
        ))

    def test_alpha_hard_stop_does_not_wait_for_15m_confirmation(self):
        engine = self._soft_loss_engine()
        with patch.object(execution, "_latest_alpha_soft_exit_confirmation", return_value=None, create=True), \
             patch.object(execution, "_fetch_closed_futures_15m", return_value=[], create=True):
            action = self._soft_loss_action(engine, pnl_pct=-10.5, mark_price=89.5)

        self.assertIsNotNone(action)
        self.assertIn("alpha_hard_stop", action["reason"])


if __name__ == "__main__":
    unittest.main()
