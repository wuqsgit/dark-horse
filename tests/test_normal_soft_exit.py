import os
import tempfile
import unittest
from unittest.mock import patch

import shared.db as db
from trader.execution import ExecutionEngine, _normal_soft_exit_in_cooldown


class SoftExitExchange:
    def get_symbol_info(self, symbol):
        return {"step_size": 0.1, "min_qty": 0.1, "min_notional": 5.0}


def position(mark=103, pnl=300):
    return {
        "symbol": "XPINUSDT", "side": "LONG", "quantity": 100,
        "entry_price": 100, "mark_price": mark, "leverage": 1,
        "unrealized_pnl": pnl,
    }


def state(**overrides):
    result = {
        "strategy_source": "normal", "entry_score": 60,
        "stop_pct": 0.12, "initial_stop_loss": 88,
        "atr_value": 2, "tp1_hit": 0, "tp2_hit": 0,
    }
    result.update(overrides)
    return result


def score(technical, hold_alpha=27, composite_score=60):
    return [{
        "symbol": "XPINUSDT", "composite_score": composite_score,
        "hold_alpha": hold_alpha,
        "raw_features": {"technical": technical},
    }]


class NormalSoftExitTest(unittest.TestCase):
    def _actions(self, technical, *, hist=None, pos=None, cooldown=False, hold_alpha=27, composite_score=60):
        engine = ExecutionEngine(SoftExitExchange())
        engine._record_decision = lambda *args, **kwargs: None
        with patch("shared.db.get_position_history", return_value=hist or state()), \
             patch("trader.execution._normal_soft_exit_in_cooldown", return_value=cooldown):
            return engine._build_position_actions(
                score(technical, hold_alpha, composite_score),
                [pos or position()],
                run_id="run1",
            )

    def test_strong_trend_weak_hold_alpha_reduces_twenty_percent_once(self):
        actions = self._actions({
            "ema20": 101, "ema20_slope": 1.0, "ema20_50_ratio": 1.01,
            "return_6h": 0.02, "return_24h": 0.05,
        })
        self.assertEqual(actions[0]["action"], "partial_close")
        self.assertEqual(actions[0]["close_pct"], 0.20)
        self.assertIn("strong_trend", actions[0]["reason"])

    def test_confirmed_weak_trend_reduces_twenty_five_percent(self):
        actions = self._actions({
            "ema20": 104, "ema20_slope": -1.0, "ema20_50_ratio": 0.99,
            "return_6h": -0.02, "return_24h": -0.04,
        })
        self.assertEqual(actions[0]["close_pct"], 0.25)
        self.assertIn("trend_weak", actions[0]["reason"])

    def test_ambiguous_trend_holds_position(self):
        actions = self._actions({
            "ema20": 101, "ema20_slope": -0.1, "ema20_50_ratio": 1.01,
            "return_6h": 0.01, "return_24h": 0.02,
        })
        self.assertEqual(actions, [])

    def test_recent_soft_exit_blocks_repeated_weak_hold_reduction(self):
        actions = self._actions({
            "ema20": 101, "ema20_slope": 1.0, "ema20_50_ratio": 1.01,
            "return_6h": 0.02, "return_24h": 0.05,
        }, cooldown=True)
        self.assertEqual(actions, [])

    def test_recent_soft_exit_blocks_score_decay_reduction(self):
        actions = self._actions({
            "ema20": 101, "ema20_slope": 1.0, "ema20_50_ratio": 1.01,
            "return_6h": 0.02, "return_24h": 0.05,
        }, hist=state(entry_score=95), hold_alpha=60, composite_score=50, cooldown=True)
        self.assertEqual(actions, [])

    def test_hard_stop_bypasses_soft_exit_cooldown(self):
        actions = self._actions({
            "ema20": 95, "ema20_slope": -1.0, "ema20_50_ratio": 0.98,
            "return_6h": -0.05, "return_24h": -0.10,
        }, pos=position(mark=99, pnl=-1201), cooldown=True)
        self.assertEqual(actions[0]["action"], "close")
        self.assertIn("margin_hard_stop", actions[0]["reason"])

    def test_loss_above_hard_stop_ignores_ordinary_weak_signals(self):
        actions = self._actions({
            "ema20": 101, "ema20_slope": -1.0, "ema20_50_ratio": 0.98,
            "return_6h": -0.05, "return_24h": -0.10,
        }, pos=position(mark=94, pnl=-1100), hold_alpha=20)
        self.assertEqual(actions, [])

    def test_cooldown_reads_recent_planned_soft_exit_from_decision_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(db, "DB_PATH", os.path.join(tmp, "test.db")):
                db.init_db()
                conn = db.get_conn()
                conn.execute(
                    """INSERT INTO strategy_decisions
                       (symbol, decision_stage, decision_result, filter_reason)
                       VALUES ('XPINUSDT', 'position_management', 'planned_partial_close',
                               'normal_soft_exit strong_trend source=hold_alpha_27.0')"""
                )
                conn.commit()
                conn.close()
                self.assertTrue(_normal_soft_exit_in_cooldown("XPINUSDT", 60))
                self.assertFalse(_normal_soft_exit_in_cooldown("OTHERUSDT", 60))


if __name__ == "__main__":
    unittest.main()
