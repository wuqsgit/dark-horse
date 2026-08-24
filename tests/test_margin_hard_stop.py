import unittest
from unittest.mock import patch

from trader.execution import ExecutionEngine
from trader.risk import calculate_position


class FixedAtrExchange:
    def get_atr(self, symbol):
        return 2.0

    def get_symbol_info(self, symbol):
        return {"step_size": 0.1, "min_qty": 0.1, "min_notional": 5.0}


class StructureRiskStopTest(unittest.TestCase):
    def test_alpha_exchange_stop_uses_atr_structure_and_probe_risk_budget(self):
        with patch("trader.risk._dynamic_leverage", return_value=3):
            result = calculate_position(
                FixedAtrExchange(), "B2USDT", 100.0, 5000.0,
                score=80, category="alpha", entry_mode="probe",
            )

        self.assertEqual(result["stop_model"], "structure_atr_1r")
        self.assertAlmostEqual(result["stop_pct"], 0.06, places=6)
        self.assertAlmostEqual(result["stop_loss"], 6.0, places=6)
        self.assertAlmostEqual(result["risk_budget"], 15.0, places=6)
        self.assertLessEqual(result["position_value"] * result["stop_pct"], 15.0)

    def test_normal_exchange_stop_uses_atr_structure_and_half_percent_risk(self):
        with patch("trader.risk._dynamic_leverage", return_value=3):
            result = calculate_position(
                FixedAtrExchange(), "AAVEUSDT", 100.0, 5000.0,
                score=80, category="fundamental", entry_mode="confirmed",
            )

        self.assertEqual(result["stop_model"], "structure_atr_1r")
        self.assertAlmostEqual(result["stop_pct"], 0.05, places=6)
        self.assertAlmostEqual(result["stop_loss"], 5.0, places=6)
        self.assertAlmostEqual(result["risk_budget"], 25.0, places=6)
        self.assertLessEqual(result["position_value"] * result["stop_pct"], 25.0)

    def test_bluechip_closes_when_price_crosses_recorded_structure_stop(self):
        engine = ExecutionEngine(FixedAtrExchange())
        engine._record_decision = lambda *args, **kwargs: None
        position = {
            "symbol": "ETHUSDT", "side": "LONG", "quantity": 1,
            "entry_price": 100.0, "mark_price": 94.9, "leverage": 4,
            "unrealized_pnl": -5.1,
        }
        history = {
            "strategy_source": "normal", "signal_source": "bluechip_trend",
            "entry_score": 80, "stop_pct": 0.05,
            "initial_stop_loss": 95.0, "atr_value": 2.0,
        }

        with patch("shared.db.get_position_history", return_value=history):
            actions = engine._build_position_actions([], [position], run_id="run1")

        self.assertEqual(actions[0]["action"], "close")
        self.assertIn("structure_1r_stop", actions[0]["reason"])
        self.assertTrue(actions[0]["is_stop"])


if __name__ == "__main__":
    unittest.main()
