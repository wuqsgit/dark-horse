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


if __name__ == "__main__":
    unittest.main()
