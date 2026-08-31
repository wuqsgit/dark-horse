import unittest

from trader import risk
from trader.risk import _dynamic_leverage, _position_sizing_config, calculate_position
from trader.selection import BluechipTrendSelector


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

    def test_probe_modes_are_observation_only(self):
        self.assertTrue(hasattr(risk, "should_execute_entry_mode"))
        self.assertFalse(risk.should_execute_entry_mode("probe"))
        self.assertFalse(risk.should_execute_entry_mode("normal_review_probe"))
        self.assertFalse(risk.should_execute_entry_mode("trend_probe"))
        self.assertTrue(risk.should_execute_entry_mode("confirmed"))
        self.assertTrue(risk.should_execute_entry_mode("strong"))

    def test_strong_bluechip_reports_two_percent_risk_budget_without_capping_margin(self):
        balance = 704.08386734

        result = calculate_position(
            AtrExchange(0.03),
            "SOLUSDT",
            price=100.0,
            balance=balance,
            score=88,
            category="core_bluechip",
            entry_mode="strong",
        )

        self.assertEqual(result["risk_per_trade_pct"], 0.02)
        self.assertAlmostEqual(result["risk_budget"], balance * 0.02)
        self.assertAlmostEqual(result["margin"], balance * 0.15)
        self.assertAlmostEqual(result["position_value"], balance * 0.15 * 3)

    def test_strong_entry_uses_fifteen_percent_balance_as_actual_margin(self):
        balance = 707.0

        result = calculate_position(
            AtrExchange(0.08),
            "SOLUSDT",
            price=100.0,
            balance=balance,
            score=72,
            category="large_cap",
            entry_mode="trend_confirmed",
        )

        self.assertEqual(result["leverage"], 2)
        self.assertLess(result["risk_notional"], 212.10)
        self.assertAlmostEqual(result["margin"], 106.05)
        self.assertAlmostEqual(result["position_value"], 212.10)

    def test_explosive_strong_entry_applies_size_factor_and_risk_cap(self):
        balance = 707.0

        result = calculate_position(
            AtrExchange(0.08),
            "STARUSDT",
            price=100.0,
            balance=balance,
            score=89,
            category="alpha",
            entry_mode="strong",
            size_multiplier=0.5,
            enforce_risk_budget=True,
        )

        self.assertLessEqual(result["margin"], balance * 0.075)
        self.assertLessEqual(
            result["position_value"] * result["stop_pct"],
            result["risk_budget"],
        )

    def test_high_quality_sol_snapshot_is_promoted_to_strong(self):
        row = {
            "symbol": "SOLUSDT",
            "composite_score": 64.0,
            "entry_alpha": 45.0,
            "relative_strength": 85.7,
            "raw_features": {
                "technical": {
                    "price_change_24h": 0.0501,
                    "return_6h": 0.4057,
                    "ema20_slope": 0.3409,
                    "ema20_50_ratio": 1.015,
                    "volume_change_pct": 0.8191,
                    "support_score": 72.1,
                    "absorption_score": 55.4,
                    "rsi_14": 50.7263,
                    "price_position_value": 0.7161,
                    "trend_score": 50.0,
                },
                "futures": {"oi_change_pct": 0.116817, "oi_score": 70},
                "depth": {"depth_ratio_score": 50.0, "big_order_score": 40.0},
            },
        }

        result = BluechipTrendSelector()._evaluate(row, set(), 0)

        self.assertEqual(result["bluechip_entry_mode"], "trend_confirmed")

    def test_weak_eth_snapshot_remains_observation_only(self):
        row = {
            "symbol": "ETHUSDT",
            "composite_score": 56.7,
            "entry_alpha": 45.0,
            "relative_strength": 70.7,
            "raw_features": {
                "technical": {
                    "price_change_24h": 0.0118,
                    "return_6h": 0.3457,
                    "ema20_slope": 0.1917,
                    "ema20_50_ratio": 1.0019,
                    "volume_change_pct": 0.2643,
                    "support_score": 73.3,
                    "absorption_score": 65.7,
                    "rsi_14": 62.4879,
                    "price_position_value": 0.7035,
                    "trend_score": 50.0,
                },
                "futures": {"oi_change_pct": -0.003766, "oi_score": 50},
                "depth": {"depth_ratio_score": 50.0, "big_order_score": 40.0},
            },
        }

        result = BluechipTrendSelector()._evaluate(row, set(), 0)

        self.assertEqual(result["bluechip_entry_mode"], "probe")


if __name__ == "__main__":
    unittest.main()
