import json
import unittest
from datetime import datetime, timezone
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

    @staticmethod
    def _trend_weak_candles():
        return [
            {"high": 101.0, "low": 100.2, "close": 100.8, "quote_vol": 1000, "taker_buy_quote_vol": 620},
            {"high": 100.9, "low": 100.1, "close": 100.7, "quote_vol": 760, "taker_buy_quote_vol": 430},
            {"high": 100.8, "low": 100.0, "close": 100.6, "quote_vol": 720, "taker_buy_quote_vol": 380},
            {"high": 100.7, "low": 99.9, "close": 100.5, "quote_vol": 680, "taker_buy_quote_vol": 340},
        ]

    def test_three_closed_bars_without_new_high_and_fading_volume_reduce_30_percent(self):
        engine = self._engine_with_regime("normal")
        with patch.object(
            execution,
            "_fetch_closed_futures_15m",
            return_value=self._trend_weak_candles(),
        ), patch("shared.db.update_position_management"):
            action = self._action(engine)

        self.assertIsNotNone(action)
        self.assertEqual(action["action"], "partial_close")
        self.assertIn("alpha_trend_weak_profit_protect", action["reason"])
        self.assertAlmostEqual(action["close_pct"], 0.30)

    def test_trend_weak_reduce_has_thirty_minute_cooldown(self):
        engine = self._engine_with_regime("normal")
        history = {
            "strategy_source": "alpha",
            "alpha_score": 83.0,
            "alpha_symbol": "ALPHA_162USDT",
            "alpha_entry_level": "candidate",
            "stop_pct": 0.10,
            "alpha_stall_protect_time": datetime.now(timezone.utc).isoformat(),
        }
        with patch.object(
            execution,
            "_fetch_closed_futures_15m",
            return_value=self._trend_weak_candles(),
        ), patch("shared.db.update_position_management"):
            action = engine._build_alpha_position_action(
                pos={"symbol": "B2USDT", "side": "LONG", "entry_price": 100.0},
                hist=history,
                pnl_pct=0.5,
                mark_price=100.5,
                close_side="SELL",
                highest_price=101.0,
                atr=1.0,
                age_h=0.5,
            )

        self.assertIsNone(action)

    def test_volume_regime_alone_does_not_reduce_without_closed_bar_confirmation(self):
        engine = self._engine_with_regime("extreme")
        candles = [
            {"high": 100.0, "low": 99.0, "close": 99.8, "quote_vol": 500},
            {"high": 100.5, "low": 99.5, "close": 100.2, "quote_vol": 600},
            {"high": 101.0, "low": 100.0, "close": 100.8, "quote_vol": 700},
            {"high": 101.5, "low": 100.5, "close": 101.2, "quote_vol": 800},
        ]
        with patch.object(
            execution,
            "_fetch_closed_futures_15m",
            return_value=candles,
        ), patch("shared.db.update_position_management"):
            action = self._action(engine)

        self.assertIsNone(action)

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
             patch.object(execution, "_fetch_closed_futures_15m", return_value=self._closed_candles(next_close=98.2), create=True):
            action = self._soft_loss_action(engine, pnl_pct=-2.2, mark_price=98.2)

        self.assertIsNone(action)

    def test_probe_exits_after_one_hour_without_progress_when_hourly_trend_is_weak(self):
        engine = self._soft_loss_engine()
        history = {
            "position_id": "AKEUSDT-LONG-PROBE",
            "strategy_source": "alpha",
            "alpha_score": 82.0,
            "alpha_symbol": "ALPHA_285USDT",
            "alpha_entry_level": "probe",
            "stop_pct": 0.10,
        }
        with patch.object(
            execution,
            "_fetch_closed_futures_15m",
            return_value=self._closed_candles(),
        ), patch("shared.db.update_position_management"):
            action = engine._build_alpha_position_action(
                pos={"symbol": "AKEUSDT", "side": "LONG", "entry_price": 100.0},
                hist=history,
                pnl_pct=0.5,
                mark_price=100.5,
                close_side="SELL",
                highest_price=101.0,
                atr=2.0,
                age_h=1.1,
            )

        self.assertIsNotNone(action)
        self.assertEqual(action["action"], "close")
        self.assertIn("alpha_probe_no_progress_exit", action["reason"])

    def test_alpha_clear_structural_breakdown_exits_above_margin_hard_stop(self):
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
        candles = [
            {"high": 102.0, "low": 100.0, "close": 101.0, "quote_vol": 1000},
            {"high": 101.0, "low": 99.0, "close": 100.0, "quote_vol": 900},
            {"high": 100.0, "low": 98.0, "close": 99.0, "quote_vol": 800},
            {"high": 99.0, "low": 96.0, "close": 97.5, "quote_vol": 1000},
        ]
        with patch.object(
            execution,
            "_fetch_closed_futures_15m",
            return_value=candles,
        ), patch("shared.db.update_position_management"):
            action = self._soft_loss_action(engine, pnl_pct=-6.0, mark_price=94.0)

        self.assertIsNotNone(action)
        self.assertEqual(action["action"], "close")
        self.assertIn("alpha_trend_structure_exit", action["reason"])

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

    def test_alpha_structure_stop_does_not_wait_for_15m_confirmation(self):
        engine = self._soft_loss_engine()
        with patch.object(execution, "_latest_alpha_soft_exit_confirmation", return_value=None, create=True), \
             patch.object(execution, "_fetch_closed_futures_15m", return_value=[], create=True):
            action = self._soft_loss_action(engine, pnl_pct=-30.3, mark_price=89.9)

        self.assertIsNotNone(action)
        self.assertIn("alpha_structure_1r_stop", action["reason"])
        self.assertTrue(action["is_stop"])

    def test_margin_roi_stage1_protects_even_when_price_move_is_below_one_r(self):
        engine = ExecutionEngine(DummyExchange())
        with patch("shared.db.update_position_management"):
            action = engine._build_alpha_position_action(
                pos={
                    "symbol": "COAIUSDT",
                    "side": "LONG",
                    "entry_price": 100.0,
                    "mark_price": 103.34,
                    "quantity": 1.0,
                    "leverage": 3,
                    "unrealized_pnl": 3.34,
                },
                hist={
                    "strategy_source": "alpha",
                    "alpha_score": 84.0,
                    "alpha_entry_level": "candidate",
                    "stop_pct": 0.10,
                },
                pnl_pct=10.02,
                mark_price=103.34,
                close_side="SELL",
                highest_price=103.34,
                atr=2.0,
                age_h=0.5,
            )

        self.assertIsNotNone(action)
        self.assertEqual(action["action"], "partial_close")
        self.assertIn("alpha_profit_lock_stage1", action["reason"])
        self.assertAlmostEqual(action["close_pct"], 0.25)

    def test_former_ten_percent_winner_exits_at_profit_floor_not_hard_stop(self):
        engine = ExecutionEngine(DummyExchange())
        with patch("shared.db.update_position_management"):
            action = engine._build_alpha_position_action(
                pos={
                    "symbol": "COAIUSDT",
                    "side": "LONG",
                    "entry_price": 100.0,
                    "quantity": 1.0,
                    "leverage": 3,
                    "unrealized_pnl": 0.33,
                },
                hist={
                    "strategy_source": "alpha",
                    "alpha_score": 84.0,
                    "stop_pct": 0.10,
                    "max_floating_roi": 10.0,
                },
                pnl_pct=1.0,
                mark_price=100.33,
                close_side="SELL",
                highest_price=103.34,
                atr=2.0,
                age_h=1.0,
            )

        self.assertIsNotNone(action)
        self.assertEqual(action["action"], "close")
        self.assertIn("alpha_profit_lock_exit", action["reason"])
        self.assertNotIn("margin_hard_stop", action["reason"])

    def test_explosive_runner_survives_first_hour_profit_lock_pullback(self):
        engine = ExecutionEngine(DummyExchange())
        with patch("shared.db.update_position_management"):
            action = engine._build_alpha_position_action(
                pos={
                    "symbol": "LOBSTERUSDT",
                    "side": "LONG",
                    "entry_price": 100.0,
                    "quantity": 100.0,
                    "leverage": 2,
                    "unrealized_pnl": 0.43,
                },
                hist={
                    "strategy_source": "alpha",
                    "entry_reason": "explosive_breakout alpha_volume_price",
                    "alpha_score": 89.0,
                    "stop_pct": 0.08,
                    "initial_stop_loss": 92.0,
                    "current_stop_loss": 92.0,
                    "max_floating_roi": 13.7,
                    "alpha_profit_lock_stage": 1,
                },
                pnl_pct=0.86,
                mark_price=100.43,
                close_side="SELL",
                highest_price=106.85,
                atr=2.0,
                age_h=0.65,
            )

        self.assertIsNone(action)

    def test_explosive_stage1_preserves_runner_floor_metadata(self):
        engine = ExecutionEngine(DummyExchange())
        with patch("shared.db.update_position_management"):
            action = engine._build_alpha_position_action(
                pos={
                    "symbol": "LOBSTERUSDT",
                    "side": "LONG",
                    "entry_price": 100.0,
                    "quantity": 100.0,
                    "leverage": 2,
                    "unrealized_pnl": 5.1,
                },
                hist={
                    "strategy_source": "alpha",
                    "entry_reason": "explosive_breakout alpha_volume_price",
                    "alpha_score": 89.0,
                    "initial_quantity": 100.0,
                    "stop_pct": 0.08,
                    "initial_stop_loss": 92.0,
                    "current_stop_loss": 92.0,
                },
                pnl_pct=10.2,
                mark_price=105.1,
                close_side="SELL",
                highest_price=105.1,
                atr=2.0,
                age_h=0.18,
            )

        self.assertEqual(action["action"], "partial_close")
        self.assertAlmostEqual(action["close_pct"], 0.20)
        self.assertAlmostEqual(action["min_remaining_fraction"], 0.40)
        self.assertTrue(action["explosive_runner_grace"])

    def test_explosive_runner_uses_atr_to_widen_post_grace_profit_lock(self):
        engine = ExecutionEngine(DummyExchange())
        with patch("shared.db.update_position_management"):
            action = engine._build_alpha_position_action(
                pos={
                    "symbol": "LOBSTERUSDT",
                    "side": "LONG",
                    "entry_price": 100.0,
                    "quantity": 40.0,
                    "leverage": 2,
                    "unrealized_pnl": 0.8,
                },
                hist={
                    "strategy_source": "alpha",
                    "entry_reason": "explosive_breakout alpha_volume_price",
                    "alpha_score": 89.0,
                    "stop_pct": 0.08,
                    "initial_stop_loss": 92.0,
                    "current_stop_loss": 92.0,
                    "max_floating_roi": 20.0,
                    "alpha_profit_lock_stage": 2,
                },
                pnl_pct=2.0,
                mark_price=101.0,
                close_side="SELL",
                highest_price=110.0,
                atr=4.0,
                age_h=1.1,
            )

        self.assertIsNone(action)

    def test_explosive_position_exits_after_two_closed_bars_fail_entry(self):
        engine = ExecutionEngine(DummyExchange())
        engine._latest_alpha_position_context = lambda symbol, hist: {
            "alpha_score": 86.0,
            "volume_price_state": "explosive_volume_watch",
            "volume_price_action": "observe",
            "volume_price_metrics_json": json.dumps({
                "volume_regime": "normal",
                "trend_score": 62,
                "ret_15m": -0.8,
                "ret_1h": -1.2,
                "ret_6h": 2.0,
                "spread_pct": 0.01,
                "entry_conditions": {
                    "price_strong_15m": False,
                    "oi_expanded": False,
                },
            }),
            "volume_price_reasons_json": "[]",
        }
        engine._record_decision = lambda *args, **kwargs: None
        candles = [
            {"high": 101.0, "low": 99.0, "close": 100.5, "quote_vol": 1000},
            {"high": 100.2, "low": 98.0, "close": 98.8, "quote_vol": 800},
            {"high": 99.4, "low": 97.8, "close": 98.5, "quote_vol": 700},
            {"high": 99.0, "low": 97.5, "close": 98.2, "quote_vol": 650},
        ]

        with patch.object(
            execution,
            "_fetch_closed_futures_15m",
            return_value=candles,
        ), patch("shared.db.update_position_management"):
            action = engine._build_alpha_position_action(
                pos={
                    "symbol": "STARUSDT",
                    "side": "LONG",
                    "entry_price": 100.0,
                    "quantity": 10.0,
                    "leverage": 3,
                    "unrealized_pnl": -18.0,
                },
                hist={
                    "strategy_source": "alpha",
                    "entry_reason": "explosive_breakout alpha_volume_price",
                    "alpha_score": 89.0,
                    "initial_stop_loss": 92.0,
                    "current_stop_loss": 92.0,
                    "stop_pct": 0.08,
                },
                pnl_pct=-5.4,
                mark_price=98.2,
                close_side="SELL",
                highest_price=100.2,
                atr=2.0,
                age_h=0.75,
            )

        self.assertIsNotNone(action)
        self.assertEqual(action["action"], "close")
        self.assertIn("explosive_breakout_failed", action["reason"])

    def test_realized_partial_profit_limits_loss_on_remaining_position(self):
        engine = ExecutionEngine(DummyExchange())
        with patch("shared.db.update_position_management"):
            action = engine._build_alpha_position_action(
                pos={
                    "symbol": "XANUSDT",
                    "side": "LONG",
                    "entry_price": 1.0,
                    "quantity": 100.0,
                    "leverage": 3,
                    "unrealized_pnl": -4.0,
                },
                hist={
                    "strategy_source": "alpha",
                    "alpha_score": 82.0,
                    "stop_pct": 0.10,
                    "initial_quantity": 100.0,
                    "protected_profit": 10.0,
                },
                pnl_pct=-5.0,
                mark_price=0.9833,
                close_side="SELL",
                highest_price=1.01,
                atr=0.02,
                age_h=2.0,
            )

        self.assertIsNotNone(action)
        self.assertEqual(action["action"], "close")
        self.assertIn("alpha_trade_profit_budget_exit", action["reason"])

    def test_closed_candle_trend_weakness_requires_no_new_high_and_fading_flow(self):
        candles = [
            {"high": 110.0, "low": 104.0, "close": 108.0, "quote_vol": 1000.0, "taker_buy_quote_vol": 650.0},
            {"high": 109.0, "low": 105.0, "close": 107.0, "quote_vol": 900.0, "taker_buy_quote_vol": 500.0},
            {"high": 108.5, "low": 104.5, "close": 106.0, "quote_vol": 850.0, "taker_buy_quote_vol": 430.0},
            {"high": 108.0, "low": 103.5, "close": 105.0, "quote_vol": 820.0, "taker_buy_quote_vol": 400.0},
        ]

        detected, details = execution._alpha_trend_weak_detected(
            "LONG",
            candles,
            lookback=3,
        )

        self.assertTrue(detected)
        self.assertTrue(details["order_flow_faded"])

    def test_impossible_legacy_stop_falls_back_to_initial_stop(self):
        state = execution._position_r_state(
            "LONG",
            0.001,
            0.00101,
            {
                "strategy_source": "alpha",
                "stop_pct": 0.10,
                "initial_stop_loss": 0.0009,
                "current_stop_loss": 88.0,
            },
            atr=0.00002,
            highest_price=0.00101,
        )

        self.assertAlmostEqual(state["current_stop_loss"], 0.0009)
        self.assertFalse(state["stop_triggered"])


if __name__ == "__main__":
    unittest.main()
