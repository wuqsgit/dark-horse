import unittest

from trader.risk import _dynamic_leverage, _position_sizing_config, calculate_position


class AtrExchange:
    def __init__(self, atr_pct):
        self.atr_pct = atr_pct

    def get_atr(self, symbol):
        return 100.0 * self.atr_pct


class DynamicLeverageTest(unittest.TestCase):
    def _leverage(self, symbol, atr_pct, category=None):
        _, sizing = _position_sizing_config(symbol, category)
        return _dynamic_leverage(atr_pct, sizing)

    def test_current_market_examples_use_neutral_formula_and_caps(self):
        cases = [
            ("BTCUSDT", 0.00966, None, 8),
            ("ETHUSDT", 0.01197, None, 6),
            ("SOLUSDT", 0.01369, None, 5),
            ("LINKUSDT", 0.01322, None, 5),
            ("AAVEUSDT", 0.02600, None, 3),
            ("DOGEUSDT", 0.01198, None, 3),
            ("STABLEUSDT", 0.01627, "alpha", 3),
        ]

        for symbol, atr_pct, category, expected in cases:
            with self.subTest(symbol=symbol):
                self.assertEqual(self._leverage(symbol, atr_pct, category), expected)

    def test_extreme_volatility_falls_to_two_times_leverage(self):
        self.assertEqual(self._leverage("BTCUSDT", 0.08), 2)

    def test_position_output_exposes_neutral_leverage_stop_proxy(self):
        result = calculate_position(
            AtrExchange(0.012),
            "BTCUSDT",
            price=100.0,
            balance=5000.0,
            score=80,
            category="core_bluechip",
            entry_mode="confirmed",
        )

        self.assertEqual(result.get("leverage"), 8)
        self.assertEqual(result.get("leverage_stop_pct"), 0.025)

    def test_expanded_probe_sizing_uses_new_margin_and_risk_caps(self):
        balance = 707.22739282
        result = calculate_position(
            AtrExchange(0.03),
            "SOLUSDT",
            price=100.0,
            balance=balance,
            score=64,
            category="core_bluechip",
            entry_mode="probe",
        )

        self.assertEqual(result["leverage"], 3)
        self.assertAlmostEqual(result["target_margin_pct"], 0.075 * 0.85)
        self.assertAlmostEqual(result["risk_per_trade_pct"], 0.0075)
        self.assertAlmostEqual(result["margin"], balance * 0.05)

    def test_all_sizing_classes_use_expanded_margin_bands(self):
        expected_risk = {
            "core_bluechip": 0.0075,
            "large_cap": 0.0075,
            "fundamental": 0.0075,
            "narrative": 0.00675,
            "meme": 0.00525,
            "alpha": 0.0075,
        }
        for category, risk_pct in expected_risk.items():
            with self.subTest(category=category):
                _, sizing = _position_sizing_config("TESTUSDT", category)
                self.assertEqual(sizing["probe_margin_pct"], 0.075)
                self.assertEqual(sizing["confirmed_margin_pct"], 0.10)
                self.assertEqual(sizing["strong_margin_pct"], 0.15)
                self.assertEqual(sizing["max_margin_pct"], 0.15)
                self.assertEqual(sizing["risk_per_trade_pct"], risk_pct)


if __name__ == "__main__":
    unittest.main()
