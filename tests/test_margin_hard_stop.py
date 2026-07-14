import unittest
from unittest.mock import patch

from trader.execution import ExecutionEngine
from trader.risk import calculate_position


class FixedAtrExchange:
    def get_atr(self, symbol):
        return 2.0

    def get_symbol_info(self, symbol):
        return {"step_size": 0.1, "min_qty": 0.1, "min_notional": 5.0}


class MarginHardStopTest(unittest.TestCase):
    def test_alpha_exchange_stop_uses_ten_percent_margin_roi(self):
        with patch("trader.risk._dynamic_leverage", return_value=3):
            result = calculate_position(
                FixedAtrExchange(), "B2USDT", 100.0, 5000.0,
                score=80, category="alpha", entry_mode="confirmed",
            )

        self.assertEqual(result["stop_model"], "margin_hard_stop")
        self.assertAlmostEqual(result["stop_pct"], 0.10 / 3, places=6)
        self.assertAlmostEqual(result["stop_loss"], 100.0 * 0.10 / 3, places=6)

    def test_normal_exchange_stop_uses_twelve_percent_margin_roi(self):
        with patch("trader.risk._dynamic_leverage", return_value=3):
            result = calculate_position(
                FixedAtrExchange(), "AAVEUSDT", 100.0, 5000.0,
                score=80, category="fundamental", entry_mode="confirmed",
            )

        self.assertEqual(result["stop_model"], "margin_hard_stop")
        self.assertAlmostEqual(result["stop_pct"], 0.12 / 3, places=6)
        self.assertAlmostEqual(result["stop_loss"], 4.0, places=6)

    def test_bluechip_uses_twelve_percent_current_margin_hard_stop(self):
        engine = ExecutionEngine(FixedAtrExchange())
        engine._record_decision = lambda *args, **kwargs: None
        position = {
            "symbol": "ETHUSDT", "side": "LONG", "quantity": 1,
            "entry_price": 100.0, "mark_price": 99.0, "leverage": 4,
            "unrealized_pnl": -3.01,
        }
        history = {
            "strategy_source": "normal", "signal_source": "bluechip_trend",
            "entry_score": 80, "stop_pct": 0.03, "atr_value": 2.0,
        }

        with patch("shared.db.get_position_history", return_value=history):
            actions = engine._build_position_actions([], [position], run_id="run1")

        self.assertEqual(actions[0]["action"], "close")
        self.assertIn("margin_hard_stop", actions[0]["reason"])
        self.assertIn("threshold=-12.00%", actions[0]["reason"])


if __name__ == "__main__":
    unittest.main()
